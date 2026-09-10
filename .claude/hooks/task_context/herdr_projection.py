"""Task Context v1 — Herdr projection consumer (Issue #2564).

Outcome: "Herdr projection は既存 primitive をそのまま利用する:
`projection_outbox revision=N` consume -> live pane/tab resolve ->
`herdr tab rename` -> `herdr pane report-metadata` -> projection ack."

Design note on the durable locator: `HERDR_TAB_ID` is *not* used as the
durable `runtime_locations.herdr_locator` here -- a Herdr Tab id can change
if the operator moves the pane to a different Tab without restarting the
Claude process, while `HERDR_PANE_ID` (confirmed via the real, installed
`herdr` CLI: `herdr pane get <pane_id>` returns `result.pane.tab_id`) is the
stable per-process identity that can be resolved to its *current* live Tab
right before projecting. This module always re-resolves the live Tab id via
`herdr pane get <pane_id>` immediately before mutating anything, and never
caches a stale Tab id (Outcome: "Herdr へのprojection直前に current live
pane/tabを解決し、staleな旧Tabを更新しない").

This module performs Herdr I/O outside of any DB transaction (never inside
``task_context_service``, consistent with AC3's "External I/O は Write
Transaction 内で実行しない"). It never rolls back committed DB state on a
Herdr-side failure (AC10) -- on failure it simply does not ack the outbox
marker, so a later flush attempt retries.

Herdr CLI contract (PR #2615 fix_delta 1):

* ``herdr pane report-metadata --state-label STATUS=TEXT`` accepts only the
  fixed five-value ``STATUS`` vocabulary (``idle`` / ``working`` /
  ``blocked`` / ``done`` / ``unknown``). Arbitrary keys such as ``task=`` or
  ``activity=`` are NOT valid state labels -- free-form key/value custom
  metadata belongs on ``--token NAME=VALUE``.
* Both ``--state-label`` and ``--token`` are display-only presentation
  metadata; neither drives Herdr's own semantic lifecycle state.

Success/ack split: the Herdr **Tab label** is the required UX surface
(Issue #2564: "必須UXはHerdr Tab labelとClaude statusLineまでとする"), so
``herdr tab rename`` returning a non-zero exit code is a projection failure
and must NOT be acked. Custom ``--token`` metadata is an explicit
best-effort enhancement in the same Issue, so a ``pane report-metadata``
failure alone is logged as a warning and does not keep the outbox marker
pending forever.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

DEFAULT_HERDR_BIN = "herdr"
DEFAULT_TIMEOUT_SECONDS = 3.0

# The five fixed `--state-label STATUS=TEXT` statuses accepted by the Herdr
# CLI. Anything outside this set must be sent as a `--token NAME=VALUE`
# instead.
HERDR_STATE_LABEL_STATUSES = frozenset({"idle", "working", "blocked", "done", "unknown"})

# `tab_bindings.runtime_health` -> Herdr state-label status. This mapping is
# deliberately conservative and total over the DB CHECK constraint's value
# set (see task_context_schema.py `tab_bindings.runtime_health`).
RUNTIME_HEALTH_TO_HERDR_STATUS = {
    "ACTIVE": "working",
    "RESTORING": "working",
    "SUSPENDED": "idle",
    "RESTORE_BLOCKED": "blocked",
    "DETACHED": "unknown",
}


def resolve_current_tab_id(
    pane_id: str, *, herdr_bin: str = DEFAULT_HERDR_BIN, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> str | None:
    """Resolve the *current* live Tab id a Pane belongs to right now (never
    a cached/stale value) via ``herdr pane get <pane_id>``."""
    try:
        proc = subprocess.run(
            [herdr_bin, "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    pane = ((data.get("result") or {}).get("pane")) or {}
    tab_id = pane.get("tab_id")
    return tab_id if isinstance(tab_id, str) and tab_id else None


def build_tab_label(task: dict[str, Any] | None, activity: dict[str, Any] | None, task_refs: list) -> str:
    """`#N · refine|impl|cleanup`等の瞬間識別を優先する Herdr Tab label."""
    if task is None:
        return "task-ctx: unbound"
    if task_refs:
        ident = f"#{task_refs[0]['ref_number']}"
    else:
        ident = "adhoc"
    kind = activity["kind"] if activity else "-"
    return f"{ident} · {kind}"


def build_pane_tokens(
    task: dict[str, Any] | None, activity: dict[str, Any] | None, binding: dict[str, Any] | None
) -> dict[str, str]:
    """Free-form custom pane metadata, sent as ``--token NAME=VALUE``.

    These are *not* ``--state-label`` values: ``task`` / ``activity`` /
    ``health`` are arbitrary key names outside the fixed five-value
    ``--state-label`` STATUS vocabulary (fix_delta 1)."""
    tokens: dict[str, str] = {}
    if task is not None:
        tokens["task"] = task.get("title") or task["id"]
    if activity is not None:
        tokens["activity"] = activity["kind"]
    if binding is not None:
        tokens["health"] = binding.get("runtime_health", "ACTIVE")
    return tokens


def build_pane_state_label(binding: dict[str, Any] | None) -> tuple[str, str] | None:
    """Map ``tab_bindings.runtime_health`` onto Herdr's fixed five-value
    ``--state-label STATUS=TEXT`` vocabulary, or ``None`` when it cannot be
    mapped (never emit an out-of-vocabulary STATUS)."""
    if binding is None:
        return None
    health = binding.get("runtime_health") or "ACTIVE"
    status = RUNTIME_HEALTH_TO_HERDR_STATUS.get(health)
    if status is None or status not in HERDR_STATE_LABEL_STATUSES:
        return None
    return status, health.lower()


def _warn(message: str) -> None:
    print(f"[task-context] {message}", file=sys.stderr)


def project_to_herdr(
    pane_id: str,
    tab_label: str,
    pane_tokens: dict[str, str],
    *,
    revision: int,
    state_label: tuple[str, str] | None = None,
    herdr_bin: str = DEFAULT_HERDR_BIN,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Project the current Task Context onto the live Herdr Tab/pane.

    Returns ``True`` (ack the outbox marker) only when the live Tab was
    resolved AND ``herdr tab rename`` exited 0 -- the Tab label is the
    required UX surface, so its failure must leave the outbox marker in
    place for a later retry (AC10).

    ``herdr pane report-metadata`` (custom ``--token`` metadata plus the
    optional mapped ``--state-label``) is an explicitly best-effort
    enhancement in Issue #2564: its failure is warned about on stderr but
    does not by itself block the ack, so optional metadata can never pin the
    outbox into permanent retry."""
    tab_id = resolve_current_tab_id(pane_id, herdr_bin=herdr_bin, timeout=timeout)
    if tab_id is None:
        _warn("herdr projection skipped: could not resolve the current live Tab for this pane")
        return False

    try:
        rename = subprocess.run(
            [herdr_bin, "tab", "rename", tab_id, tab_label],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - transport failure must not raise into the hook
        _warn(f"herdr tab rename transport failure ({type(exc).__name__}); outbox marker kept for retry")
        return False
    if rename.returncode != 0:
        _warn(
            f"herdr tab rename exited {rename.returncode}; projection NOT acked "
            "(outbox marker kept for retry)"
        )
        return False

    metadata_cmd = [
        herdr_bin,
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        "task-context-v1",
        "--seq",
        str(revision),
    ]
    for key, value in pane_tokens.items():
        metadata_cmd += ["--token", f"{key}={value}"]
    if state_label is not None:
        status, text = state_label
        if status in HERDR_STATE_LABEL_STATUSES:
            metadata_cmd += ["--state-label", f"{status}={text}"]
    try:
        metadata = subprocess.run(metadata_cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001
        _warn(f"herdr pane report-metadata transport failure ({type(exc).__name__}); best-effort only")
        return True
    if metadata.returncode != 0:
        _warn(
            f"herdr pane report-metadata exited {metadata.returncode}; best-effort custom "
            "metadata skipped (Tab label projection succeeded)"
        )
    return True
