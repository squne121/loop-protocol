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
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

DEFAULT_HERDR_BIN = "herdr"
DEFAULT_TIMEOUT_SECONDS = 3.0


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


def build_pane_state_labels(
    task: dict[str, Any] | None, activity: dict[str, Any] | None, binding: dict[str, Any] | None
) -> dict[str, str]:
    labels: dict[str, str] = {}
    if task is not None:
        labels["task"] = task.get("title") or task["id"]
    if activity is not None:
        labels["activity"] = activity["kind"]
    if binding is not None:
        labels["health"] = binding.get("runtime_health", "ACTIVE")
    return labels


def project_to_herdr(
    pane_id: str,
    tab_label: str,
    pane_state_labels: dict[str, str],
    *,
    revision: int,
    herdr_bin: str = DEFAULT_HERDR_BIN,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Best-effort Herdr Tab label + pane metadata projection. Returns
    ``True`` only if the live Tab could be resolved and both Herdr calls
    were attempted without raising -- callers must only ``ack`` the outbox
    marker on ``True`` (AC10: never lose a projection update on failure)."""
    tab_id = resolve_current_tab_id(pane_id, herdr_bin=herdr_bin, timeout=timeout)
    if tab_id is None:
        return False
    try:
        subprocess.run(
            [herdr_bin, "tab", "rename", tab_id, tab_label],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
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
        for key, value in pane_state_labels.items():
            metadata_cmd += ["--state-label", f"{key}={value}"]
        subprocess.run(metadata_cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return False
    return True
