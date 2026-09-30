#!/usr/bin/env python3
"""Issue #2827 narrow runtime verifier: ordinary user prompt -> ACTIVE Task
auto-rebind.

This is the ONE adapter that binds the four required runtime leaves. It does
not modify or extend the generic harness
(``task_context_runtime_smoke_verifier.orchestrate_runtime_smoke``): that
harness reports a scenario that was never supplied as ``skipped`` while its
aggregate ``status`` is still ``pass``, so an aggregate ``pass`` is NEVER
treated as evidence for any leaf here.

Required leaves (``--leaf <name>``):

- ``ordinary-prompt-rebind``            (AC1)  real user prompt A -> B
- ``internal-completion-negative-control`` (AC6, AC16) subagent / background
  shell completion that mentions B never rebinds
- ``workflow-signal-applied``           (AC5)  first ``refinement_approved``
  for B is ``applied`` after the prompt rebind
- ``slash-task-override``               (AC4, AC9) a user-typed ``/task`` goes
  through ``UserPromptExpansion(command_name == "task")``

Semantics: with no ``--leaf`` all four are required; a skipped / absent /
unbound leaf makes the run exit non-zero. ``--leaf X`` inspects only X (a
skipped/absent X exits non-zero, other leaves are not evaluated). A leaf only
counts as ``pass`` when it is bound to an actor, a session identity, a run
identity, before state, after state and a result.

Exit codes: 0 all required leaves pass, 1 a required leaf failed / skipped /
absent / unbound, 77 (with a ``SKIP:`` line) when the ``claude`` executable
is unavailable (never a PASS).

``--evidence-json PATH`` evaluates a previously collected evidence file
offline instead of driving a live runtime.

Evidence hygiene: only bounded metadata is persisted (ids, kinds, booleans,
leading envelope tag). Raw prompt text, transcripts and secrets are never
written to the artifact directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_HOOK_ENTRY_PATH = _REPO_ROOT / ".claude" / "hooks" / "task_context" / "hook_entry.py"
_REPO_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"
_TASK_SKILL_PATH = _REPO_ROOT / ".claude" / "skills" / "task" / "SKILL.md"
_DEFAULT_ARTIFACT_DIR = Path("artifacts") / "runtime-smoke" / "task-context-active-auto-rebind"

EXIT_SKIP = 77

LEAF_NAMES = (
    "ordinary-prompt-rebind",
    "internal-completion-negative-control",
    "workflow-signal-applied",
    "slash-task-override",
)

# AC -> leaf (documentation of the Issue contract; also used in reports).
AC_TO_LEAF = {
    "AC1": "ordinary-prompt-rebind",
    "AC4": "slash-task-override",
    "AC5": "workflow-signal-applied",
    "AC6": "internal-completion-negative-control",
    "AC9": "slash-task-override",
    "AC16": "internal-completion-negative-control",
}

# A 'pass' leaf must carry every one of these non-empty fields.
_LEAF_BINDING_FIELDS = ("actor", "session_id", "run_id", "before", "after", "result")

# Closed class list of In Scope 0 (never extended implicitly).
STOP_BLOCKING = "stop-blocking"
ACCEPTED_RESIDUAL = "accepted-residual"
POSITIVE_CONTROL = "positive-control"
PROVENANCE_CLASSES = (
    ("subagent_completion", STOP_BLOCKING),
    ("background_shell_completion", STOP_BLOCKING),
    ("peer_teammate_message", ACCEPTED_RESIDUAL),
    ("scheduled_prompt", ACCEPTED_RESIDUAL),
    ("agent_sent_herdr_pane_text", ACCEPTED_RESIDUAL),
    ("interactive_typed_prompt", POSITIVE_CONTROL),
)

_ENVELOPE_TAG_RE = re.compile(r"^\s*(<[A-Za-z][\w-]*>)")


# ---------------------------------------------------------------------------
# offline evaluation (pure)
# ---------------------------------------------------------------------------


def _leaf_violation(name: str, leaf: Any) -> str | None:
    """Why ``leaf`` does not count as a pass, or ``None`` when it does."""
    if not isinstance(leaf, dict):
        return f"leaf {name!r} is absent"
    status = leaf.get("status")
    if status != "pass":
        detail = leaf.get("reason") or "; ".join(leaf.get("violations") or []) or ""
        return f"leaf {name!r} status={status!r} (pass required){': ' + detail if detail else ''}"
    missing = [field for field in _LEAF_BINDING_FIELDS if not leaf.get(field)]
    if missing:
        return f"leaf {name!r} is not bound to {', '.join(missing)} (a pass must be bound)"
    return None


def evaluate_evidence(evidence: dict[str, Any], *, leaf: str | None = None) -> dict[str, Any]:
    """Evaluate collected evidence. ``evidence["aggregate"]`` (e.g. the
    generic harness' ``status``) is deliberately ignored."""
    leaves = evidence.get("leaves") if isinstance(evidence, dict) else None
    leaves = leaves if isinstance(leaves, dict) else {}
    if leaf is not None and leaf not in LEAF_NAMES:
        return {
            "status": "fail",
            "exit_code": 1,
            "violations": [f"unknown leaf {leaf!r} (expected one of {', '.join(LEAF_NAMES)})"],
            "leaf_results": {},
        }
    required = (leaf,) if leaf else LEAF_NAMES
    violations: list[str] = []
    leaf_results: dict[str, str] = {}
    for name in required:
        problem = _leaf_violation(name, leaves.get(name))
        leaf_results[name] = "pass" if problem is None else "not_pass"
        if problem:
            violations.append(problem)
    passed = not violations
    return {
        "status": "pass" if passed else "fail",
        "exit_code": 0 if passed else 1,
        "violations": violations,
        "leaf_results": leaf_results,
    }


# ---------------------------------------------------------------------------
# provenance capture matrix (pure over observations)
# ---------------------------------------------------------------------------


def _load_hook_entry():
    name = "hook_entry_under_active_rebind_verifier"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HOOK_ENTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_provenance_capture_matrix(observations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Class -> marker presence / capture availability matrix.

    ``observations[class]`` is either ``{"capture": "not_capturable", "reason": ...}``
    or ``{"capture": "captured", "prompt_head": <bounded leading text>,
    "prompt_id_present": bool}``. The derived provenance is computed by the
    production ``hook_entry.derive_input_provenance`` over a synthetic stdin
    made of the bounded observation, so the matrix cannot drift from the
    adapter. A stop-blocking class that was not captured, or that has no
    envelope marker (i.e. is indistinguishable from a user prompt), makes the
    matrix a Stop Condition; an accepted-residual class only records its
    result and never stops."""
    hook_entry = _load_hook_entry()
    rows: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    for name, kind in PROVENANCE_CLASSES:
        observation = observations.get(name) or {"capture": "not_capturable", "reason": "no observation supplied"}
        row: dict[str, Any] = {"class": name, "kind": kind, "capture": observation.get("capture", "not_capturable")}
        if row["capture"] == "captured":
            head = observation.get("prompt_head") or ""
            stdin = {
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "captured" if observation.get("prompt_id_present", True) else "",
                "prompt": head,
            }
            row["derived_provenance"] = hook_entry.derive_input_provenance(stdin)
            row["envelope_marker"] = _ENVELOPE_TAG_RE.match(head).group(1) if _ENVELOPE_TAG_RE.match(head) else None
            row["marker_present"] = row["derived_provenance"] == "internal_or_unknown" and bool(row["envelope_marker"])
        else:
            row["derived_provenance"] = None
            row["marker_present"] = None
            row["reason"] = observation.get("reason")
        if kind == STOP_BLOCKING:
            if row["capture"] != "captured":
                violations.append(f"stop-blocking class {name!r} could not be captured (Stop Condition)")
            elif not row["marker_present"]:
                violations.append(f"stop-blocking class {name!r} has no envelope marker (Stop Condition)")
        if (
            kind == POSITIVE_CONTROL
            and row["capture"] == "captured"
            and row["derived_provenance"] != "user_prompt_observed"
        ):
            violations.append(
                f"positive control {name!r} derived {row['derived_provenance']!r}, expected user_prompt_observed"
            )
        rows[name] = row
    return {
        "status": "stop_condition" if violations else "ok",
        "classes": rows,
        "violations": violations,
        "markers": list(hook_entry.INTERNAL_ENVELOPE_MARKERS),
    }


# ---------------------------------------------------------------------------
# bounded hook recorder (sibling hook, live runs only)
# ---------------------------------------------------------------------------


def record_hook(event: str) -> int:
    """Sibling hook: append ONE bounded JSON line describing the hook stdin
    (ids, key names, leading envelope tag, booleans -- never prompt text)."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
    watch_ref = os.environ.get("ACTIVE_REBIND_WATCH_REF") or ""
    tag = _ENVELOPE_TAG_RE.match(prompt)
    record = {
        "event": event,
        "t": time.time(),
        "session_id": data.get("session_id"),
        "prompt_id_present": bool(data.get("prompt_id")),
        "prompt_id": data.get("prompt_id"),
        "stdin_keys": sorted(data),
        "command_name": data.get("command_name"),
        "envelope_tag": tag.group(1) if tag else None,
        "mentions_watch_ref": bool(watch_ref and watch_ref in prompt),
        "prompt_is_slash": prompt.lstrip().startswith("/"),
    }
    log_path = os.environ.get("ACTIVE_REBIND_HOOK_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    return 0


# ---------------------------------------------------------------------------
# live collection
# ---------------------------------------------------------------------------


def _project_modules():
    """Import Task Context modules lazily (kept out of ``--record-hook``)."""
    if str(_THIS_DIR) not in sys.path:
        sys.path.insert(0, str(_THIS_DIR))
    import task_context_config as config
    import task_context_db as db
    import task_context_runtime_smoke_verifier as smoke

    return config, db, smoke


class Leaf:
    """One leaf's run-scoped isolated environment."""

    def __init__(self, name: str, run_id: str, artifact_dir: Path, claude_bin: str, timeout: float):
        config, _db, smoke = _project_modules()
        self.name = name
        self.run_id = f"{run_id}-{name}"
        self.dir = (artifact_dir / run_id / name).resolve()
        self.work = self.dir / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        skill_dir = self.work / ".claude" / "skills" / "task"
        skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_TASK_SKILL_PATH, skill_dir / "SKILL.md")
        self.state_root = smoke.build_isolated_state_root(self.dir, run_id="state")
        if smoke.is_state_root_materialized(self.state_root):
            raise RuntimeError(f"refusing to reuse a materialized state root: {self.state_root}")
        self.hook_log = self.dir / "hook-events.jsonl"
        self.settings_path = self.dir / "settings.json"
        self.session_id = str(uuid.uuid4())
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.turns: list[dict[str, Any]] = []
        self.watch_ref = ""
        self._config = config
        self._smoke = smoke
        self._write_settings()

    def _write_settings(self) -> None:
        def entry(event: str, matcher: str | None = None) -> list[dict[str, Any]]:
            item: dict[str, Any] = {
                "hooks": [
                    {
                        "type": "command",
                        "command": sys.executable,
                        "args": [str(Path(__file__).resolve()), "--record-hook", event],
                        "timeout": 10,
                    },
                    {
                        "type": "command",
                        "command": sys.executable,
                        "args": [str(_HOOK_ENTRY_PATH), event],
                        "timeout": 10,
                    },
                ]
            }
            if matcher:
                item["matcher"] = matcher
            return [item]

        settings = {
            "hooks": {
                "SessionStart": entry("SessionStart"),
                "UserPromptSubmit": entry("UserPromptSubmit"),
                "UserPromptExpansion": entry("UserPromptExpansion", "task"),
                "SubagentStart": entry("SubagentStart"),
                "SubagentStop": entry("SubagentStop"),
            }
        }
        self.settings_path.write_text(json.dumps(settings, indent=1), encoding="utf-8")

    def env(self) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        base.pop("CLAUDE_CODE_SESSION_ID", None)
        extra = {
            "HERDR_TAB_ID": f"smoke-tab-{self.run_id}",
            "HERDR_PANE_ID": f"smoke-pane-{self.run_id}",
            "ACTIVE_REBIND_HOOK_LOG": str(self.hook_log),
            "ACTIVE_REBIND_WATCH_REF": self.watch_ref,
        }
        return self._smoke.build_isolated_env(self.state_root, base_env=base, extra=extra)

    def turn(self, prompt: str, *, allowed_tools: list[str] | None = None) -> dict[str, Any]:
        first = not self.turns
        argv = [
            self.claude_bin,
            "-p",
            prompt,
            "--session-id" if first else "--resume",
            self.session_id,
            "--setting-sources",
            "project",
            "--settings",
            str(self.settings_path),
            "--permission-mode",
            "dontAsk",
            "--model",
            "haiku",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
        ]
        if allowed_tools:
            argv += ["--allowedTools", *allowed_tools]
        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=self.work,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            returncode, stderr_tail = proc.returncode, proc.stderr[-300:]
            (self.dir / f"turn-{len(self.turns) + 1}.stream.jsonl").write_text(
                _bounded_stream(proc.stdout), encoding="utf-8"
            )
        except subprocess.TimeoutExpired:
            returncode, stderr_tail = -1, "timeout"
        info = {
            "index": len(self.turns) + 1,
            "returncode": returncode,
            "seconds": round(time.time() - started, 1),
            "stderr_tail": stderr_tail,
        }
        self.turns.append(info)
        return info

    def hook_records(self) -> list[dict[str, Any]]:
        if not self.hook_log.exists():
            return []
        return [json.loads(line) for line in self.hook_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def snapshot(self) -> dict[str, Any]:
        """Bounded Task/Activity/Binding/claim snapshot of the isolated DB."""
        config = self._config
        db = _project_modules()[1]
        previous = os.environ.get(config.STATE_ROOT_ENV_VAR)
        os.environ[config.STATE_ROOT_ENV_VAR] = str(self.state_root)
        try:
            if not self.state_root.exists():
                return {"materialized": False}
            conn = db.connect(config.db_path())
            try:
                runs = [dict(r) for r in conn.execute("SELECT * FROM execution_runs ORDER BY started_at")]
                open_managed = [
                    r for r in runs if r["ended_at"] is None and r["run_kind"] in ("native_operator", "claude_gpt")
                ]
                claims = {
                    f"{r['repo']}#{r['ref_number']}": r["task_id"]
                    for r in conn.execute("SELECT * FROM task_ref_claims WHERE released_at IS NULL")
                }
                activities = [
                    {"task_id": r["task_id"], "kind": r["kind"], "status": r["status"]}
                    for r in conn.execute("SELECT * FROM activities ORDER BY started_at")
                ]
                events = [
                    json.loads(r["metadata_json"] or "{}") | {"event_type": r["event_type"]}
                    for r in conn.execute("SELECT * FROM events ORDER BY occurred_at")
                ]
                return {
                    "materialized": True,
                    "binding_task_id": open_managed[-1]["task_id"] if open_managed else None,
                    "binding_activity_id": open_managed[-1]["activity_id"] if open_managed else None,
                    "task_count": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    "claims": claims,
                    "activities": activities,
                    "event_reasons": [e.get("reason_code") for e in events if e.get("reason_code")],
                    "rebind_events": [e for e in events if e.get("reason_code") == "user_prompt_primary_target_rebind"],
                }
            finally:
                conn.close()
        finally:
            if previous is None:
                os.environ.pop(config.STATE_ROOT_ENV_VAR, None)
            else:
                os.environ[config.STATE_ROOT_ENV_VAR] = previous

    def signal_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        env = self.env()
        env["CLAUDE_CODE_SESSION_ID"] = self.session_id
        proc = subprocess.run(
            [sys.executable, str(_THIS_DIR / "task_contextctl.py"), "signal", "apply"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1]).get("data", {})
        except (ValueError, IndexError):
            return {"disposition": "transport_error", "stderr": proc.stderr[-200:]}


def _bounded_stream(stdout: str) -> str:
    """Keep only hook lifecycle lines' bounded fields; drop model text."""
    kept = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("type") == "system" and str(item.get("subtype", "")).startswith("hook"):
            kept.append(
                json.dumps(
                    {
                        "type": item.get("type"),
                        "subtype": item.get("subtype"),
                        "hook_event": item.get("hook_event"),
                        "exit_code": item.get("exit_code"),
                    }
                )
            )
    return "\n".join(kept)


def _actor(claude_version: str) -> str:
    return f"real `claude -p` {claude_version} (model haiku) with hook_entry.py wired as in .claude/settings.json"


def _sibling_hook_inventory(leaf: Leaf) -> dict[str, Any]:
    """AC1 observation: every hook matching UserPromptSubmit in the repo's
    effective settings and in this run's isolated settings, with whether it
    has a Task Context side effect. Deny / rollback is NOT verified here
    (AC18's fault-injection pytest owns that)."""

    def commands(settings_path: Path) -> list[str]:
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found: list[str] = []
        for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
            for hook in group.get("hooks", []):
                found.append(" ".join([str(hook.get("command", "")), *map(str, hook.get("args", []))]).strip())
        return found

    def entry(command: str) -> dict[str, Any]:
        mutates = "hook_entry.py" in command
        return {"command": re.sub(r"\S*/(?=[^/\s]+\s|$)", "", command)[:120], "task_context_side_effect": mutates}

    return {
        "repo_settings": [entry(c) for c in commands(_REPO_SETTINGS_PATH)],
        "isolated_settings": [entry(c) for c in commands(leaf.settings_path)],
    }


def _new_bound_leaf(leaf: Leaf, claude_version: str) -> dict[str, Any]:
    return {
        "status": "fail",
        "actor": _actor(claude_version),
        "session_id": leaf.session_id,
        "run_id": leaf.run_id,
        "before": None,
        "after": None,
        "result": None,
        "violations": [],
    }


def _user_prompt_events(records: list[dict[str, Any]], *, after_index: int = 0) -> list[dict[str, Any]]:
    return [r for r in records[after_index:] if r["event"] == "UserPromptSubmit"]


def _establish_a_then_prompt_b(leaf: Leaf, number_a: int, number_b: int) -> tuple[dict, dict, list[str]]:
    """Two real user-prompt turns: bind to A, then switch to B by an ordinary
    prompt. Returns (state after A, state after B, problems)."""
    problems: list[str] = []
    leaf.turn(f"owner/repo#{number_a} を対象に作業開始。返答は OK の一語のみ。")
    after_a = leaf.snapshot()
    leaf.turn(f"owner/repo#{number_b} を対象にレビューして。返答は OK の一語のみ。")
    after_b = leaf.snapshot()
    if any(t["returncode"] != 0 for t in leaf.turns):
        problems.append(f"a claude turn exited non-zero: {leaf.turns}")
    return after_a, after_b, problems


def leaf_ordinary_prompt_rebind(run_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    leaf = Leaf("ordinary-prompt-rebind", run_id, ctx["artifact_dir"], ctx["claude_bin"], ctx["timeout"])
    result = _new_bound_leaf(leaf, ctx["claude_version"])
    number_a, number_b = 910, 911
    before, after, problems = _establish_a_then_prompt_b(leaf, number_a, number_b)
    records = leaf.hook_records()
    task_a = before.get("claims", {}).get(f"owner/repo#{number_a}")
    task_b = after.get("claims", {}).get(f"owner/repo#{number_b}")
    if not task_a or before.get("binding_task_id") != task_a:
        problems.append("turn 1 did not autobind the session to Task A")
    if not task_b or after.get("binding_task_id") != task_b or task_b == task_a:
        problems.append("ordinary prompt did not move the Binding from Task A to the claim owner of B")
    if not any(
        e.get("source_task_id") == task_a and e.get("destination_task_id") == task_b
        for e in after.get("rebind_events", [])
    ):
        problems.append("no bounded user_prompt_primary_target_rebind event with pre/post Task identity")
    if any(r["event"] == "UserPromptExpansion" for r in records):
        problems.append("a /task expansion fired during the normal A -> B switch (slash count must be 0)")
    new_kinds = [a["kind"] for a in after.get("activities", []) if a["task_id"] == task_b]
    if new_kinds != ["refine"]:
        problems.append(f"Task B initial Activity kinds {new_kinds!r}, expected ['refine']")
    result.update(
        before=before,
        after=after,
        result={
            "slash_task_invocations": sum(1 for r in records if r["event"] == "UserPromptExpansion"),
            "hook_events": _bounded_records(records),
            "sibling_hooks": _sibling_hook_inventory(leaf),
            "turns": leaf.turns,
        },
        violations=problems,
        status="pass" if not problems else "fail",
    )
    return result


def _bounded_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = (
        "event",
        "session_id",
        "prompt_id_present",
        "envelope_tag",
        "mentions_watch_ref",
        "command_name",
        "prompt_is_slash",
    )
    return [{k: r.get(k) for k in keep} for r in records]


def leaf_internal_completion_negative_control(run_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    leaf = Leaf("internal-completion-negative-control", run_id, ctx["artifact_dir"], ctx["claude_bin"], ctx["timeout"])
    result = _new_bound_leaf(leaf, ctx["claude_version"])
    problems: list[str] = []
    number_a, number_b = 940, 941
    leaf.watch_ref = f"owner/repo#{number_b}"

    leaf.turn(f"owner/repo#{number_a} を対象に作業開始。返答は OK の一語のみ。")
    before = leaf.snapshot()
    task_a = before.get("claims", {}).get(f"owner/repo#{number_a}")
    if not task_a or before.get("binding_task_id") != task_a:
        problems.append("setup: session is not ACTIVE on Task A")

    # The operator instruction must NOT itself name B (else it would be an
    # ordinary user prompt naming B): the subagent composes the reference.
    composed = (
        "Use the Agent tool exactly once with run_in_background set to true. Tell the subagent to reply with the "
        "concatenation of these three fragments joined with no spaces and nothing else: the fragment `owner/repo`, "
        f"the fragment `#`, the fragment `{number_b}`, followed by the literal text ` を対象にレビューして`. "
        "Then end your turn and wait; when the background agent's completion is reported back to you, reply with "
        "the single word FINISHED."
    )
    subagent_turn = leaf.turn(composed, allowed_tools=["Agent"])
    shell_turn = leaf.turn(
        "Use the Bash tool exactly once with run_in_background set to true and command "
        # The reference is produced by the shell (arithmetic), so the operator prompt never names B itself.
        f"'sleep 2; echo \"owner/repo#$(({number_b - 1}+1)) を対象にレビューして\"'. Then end your turn and wait; "
        "when the background "
        "command's completion is reported back to you, reply with the single word FINISHED.",
        allowed_tools=["Bash"],
    )
    after = leaf.snapshot()
    records = leaf.hook_records()

    marker_tags = set()
    observations: dict[str, dict[str, Any]] = {}
    internal_prompts = [r for r in records if r["event"] == "UserPromptSubmit" and r["envelope_tag"]]
    user_prompts = [r for r in records if r["event"] == "UserPromptSubmit" and not r["envelope_tag"]]
    for r in internal_prompts:
        marker_tags.add(r["envelope_tag"])
    if any(r["mentions_watch_ref"] for r in user_prompts):
        problems.append("an operator prompt itself mentioned B; the negative control is vacuous")
    internal_with_ref = [r for r in internal_prompts if r["mentions_watch_ref"]]
    if not internal_with_ref:
        problems.append("no internal (envelope) UserPromptSubmit carried the B reference; the control is vacuous")
    if after.get("binding_task_id") != task_a:
        problems.append("Binding left Task A after an internal completion mentioned B (auto-rebind must not happen)")
    if f"owner/repo#{number_b}" in after.get("claims", {}):
        problems.append("a claim for B was created from an internal completion")
    if after.get("task_count") != before.get("task_count"):
        problems.append("Task count changed across the internal completion")
    if "internal_or_unknown_provenance_no_mutation" not in after.get("event_reasons", []):
        problems.append("EventJournal has no internal_or_unknown_provenance_no_mutation observation")
    if any(r["event"] == "UserPromptExpansion" for r in records):
        problems.append("unexpected /task expansion in the negative control")

    # Causal evidence: SubagentStart / SubagentStop and the internal notification.
    events_seen = [r["event"] for r in records]
    if "SubagentStart" not in events_seen or "SubagentStop" not in events_seen:
        problems.append("SubagentStart/SubagentStop causal evidence missing")
    sub_index = events_seen.index("SubagentStop") if "SubagentStop" in events_seen else -1
    if sub_index >= 0 and not any(
        i > sub_index and r["event"] == "UserPromptSubmit" and r["envelope_tag"] for i, r in enumerate(records)
    ):
        problems.append("no internal UserPromptSubmit followed the SubagentStop")

    # Capture observations (bounded) for the provenance matrix.
    def head_of(rec: dict[str, Any] | None) -> dict[str, Any]:
        if rec is None:
            return {"capture": "not_capturable", "reason": "no internal notification observed"}
        return {
            "capture": "captured",
            "prompt_head": (rec["envelope_tag"] or "") + "\n",
            "prompt_id_present": rec["prompt_id_present"],
            "stdin_keys": rec["stdin_keys"],
        }

    sub_notifications = [
        r
        for i, r in enumerate(records)
        if sub_index >= 0 and i > sub_index and r["event"] == "UserPromptSubmit" and r["envelope_tag"]
    ]
    observations["subagent_completion"] = head_of(sub_notifications[0] if sub_notifications else None)
    later = [r for r in internal_prompts if r not in sub_notifications[:1]]
    observations["background_shell_completion"] = head_of(later[0] if later else None)
    for name in ("peer_teammate_message", "scheduled_prompt", "agent_sent_herdr_pane_text", "interactive_typed_prompt"):
        observations[name] = {
            "capture": "not_capturable",
            "reason": (
                "no peer/teammate, scheduler, live Herdr pane or interactive lane "
                "in this isolated `claude -p` environment"
            ),
        }
    first_user = user_prompts[0] if user_prompts else None
    # Positive control proxy: the real `-p` stdin prompt (NOT the interactive lane).
    matrix_input = dict(observations)
    matrix = build_provenance_capture_matrix(matrix_input)
    if first_user is not None:
        matrix["classes"]["interactive_typed_prompt"]["proxy"] = {
            "source": "claude -p stdin prompt (not the interactive herdr lane)",
            "derived_provenance": _load_hook_entry().derive_input_provenance(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt_id": "x" if first_user["prompt_id_present"] else "",
                    "prompt": "user text",
                }
            ),
        }
    if matrix["status"] != "ok":
        problems.extend(matrix["violations"])

    # Marker set re-verification: every known marker maps to internal_or_unknown.
    hook_entry = _load_hook_entry()
    marker_check = {
        marker: hook_entry.derive_input_provenance(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt_id": "x",
                "prompt": f"\n  {marker} ref owner/repo#{number_b}",
            }
        )
        for marker in hook_entry.INTERNAL_ENVELOPE_MARKERS
    }
    if any(v != "internal_or_unknown" for v in marker_check.values()):
        problems.append(f"marker re-verification failed: {marker_check}")
    for tag in marker_tags:
        if tag not in hook_entry.INTERNAL_ENVELOPE_MARKERS:
            problems.append(f"observed envelope tag {tag!r} is not in INTERNAL_ENVELOPE_MARKERS")

    (leaf.dir / "provenance_capture_matrix.json").write_text(
        json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    if any(t["returncode"] != 0 for t in leaf.turns):
        problems.append(f"a claude turn exited non-zero: {leaf.turns}")
    result.update(
        before=before,
        after=after,
        result={
            "hook_events": _bounded_records(records),
            "provenance_capture_matrix": matrix,
            "marker_reverification": marker_check,
            "subagent_turn": subagent_turn,
            "shell_turn": shell_turn,
        },
        violations=problems,
        status="pass" if not problems else "fail",
    )
    return result


def leaf_workflow_signal_applied(run_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    leaf = Leaf("workflow-signal-applied", run_id, ctx["artifact_dir"], ctx["claude_bin"], ctx["timeout"])
    result = _new_bound_leaf(leaf, ctx["claude_version"])
    number_a, number_b = 920, 921
    before, after_rebind, problems = _establish_a_then_prompt_b(leaf, number_a, number_b)
    task_b = after_rebind.get("claims", {}).get(f"owner/repo#{number_b}")
    if not task_b or after_rebind.get("binding_task_id") != task_b:
        problems.append("prompt rebind to B did not happen; the workflow signal cannot be evaluated")
    sha = "a" * 64
    signal_b = leaf.signal_apply(
        {
            "signal_kind": "refinement_approved",
            "source": "issue-refinement-loop",
            "source_schema_version": "v1",
            "evidence": {"repo": "owner/repo", "issue_number": number_b, "approved_body_sha256": sha},
        }
    )
    stale_a = leaf.signal_apply(
        {
            "signal_kind": "refinement_approved",
            "source": "issue-refinement-loop",
            "source_schema_version": "v1",
            "evidence": {"repo": "owner/repo", "issue_number": number_a, "approved_body_sha256": "b" * 64},
        }
    )
    after = leaf.snapshot()
    if signal_b.get("disposition") != "applied":
        problems.append(f"first refinement_approved for B is {signal_b!r}, expected disposition == applied")
    b_activities = [(a["kind"], a["status"]) for a in after.get("activities", []) if a["task_id"] == task_b]
    if ("refine", "DONE") not in b_activities or ("implementation", "ACTIVE") not in b_activities:
        problems.append(f"Task B Activities {b_activities!r}: expected refine DONE and implementation ACTIVE")
    if after.get("binding_task_id") != task_b:
        problems.append("origin ExecutionRun did not follow Task B")
    if stale_a.get("disposition") == "applied":
        problems.append("a stale Task A signal was applied to B after the switch")
    result.update(
        before=before,
        after=after,
        result={
            "signal_b": signal_b,
            "stale_signal_for_a": stale_a,
            "task_b_activities": b_activities,
            "turns": leaf.turns,
        },
        violations=problems,
        status="pass" if not problems else "fail",
    )
    return result


def leaf_slash_task_override(run_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    leaf = Leaf("slash-task-override", run_id, ctx["artifact_dir"], ctx["claude_bin"], ctx["timeout"])
    result = _new_bound_leaf(leaf, ctx["claude_version"])
    problems: list[str] = []
    number_a, number_c = 930, 931
    leaf.turn(f"owner/repo#{number_a} を対象に作業開始。返答は OK の一語のみ。")
    before = leaf.snapshot()
    task_a = before.get("claims", {}).get(f"owner/repo#{number_a}")
    slash = leaf.turn(f"/task owner/repo#{number_c}")
    after = leaf.snapshot()
    records = leaf.hook_records()
    expansions = [r for r in records if r["event"] == "UserPromptExpansion" and r.get("command_name") == "task"]
    task_c = after.get("claims", {}).get(f"owner/repo#{number_c}")
    if len(expansions) != 1:
        problems.append(f"expected exactly one UserPromptExpansion(command_name == task), saw {len(expansions)}")
    if not task_c or after.get("binding_task_id") != task_c or task_c == task_a:
        problems.append("the user-typed /task did not rebind the Binding to the claim owner of the target")
    if "slash_task_rebind" not in after.get("event_reasons", []):
        problems.append("EventJournal has no slash_task_rebind observation (existing command result contract)")
    if slash["returncode"] != 0:
        problems.append("claude exited non-zero for the /task turn")
    # `Skill` tool invocations are not user-typed expansions: fail if the only evidence were a tool call.
    result.update(
        before=before,
        after=after,
        result={
            "expansion_events": _bounded_records(expansions),
            "procedure": (
                "UserPromptExpansion(command_name == task) -> on_user_prompt_expansion "
                "-> atomic rebind -> slash_task_rebind event"
            ),
            "hook_events": _bounded_records(records),
        },
        violations=problems,
        status="pass" if not problems else "fail",
    )
    return result


_LEAF_FUNCTIONS = {
    "ordinary-prompt-rebind": leaf_ordinary_prompt_rebind,
    "internal-completion-negative-control": leaf_internal_completion_negative_control,
    "workflow-signal-applied": leaf_workflow_signal_applied,
    "slash-task-override": leaf_slash_task_override,
}


def collect_live_evidence(leaf: str | None, args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """Drive the real runtime. Returns ``(evidence, skip_reason)``."""
    claude_bin = args.claude_bin or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin or not (Path(claude_bin).exists() or shutil.which(claude_bin)):
        return None, "claude executable is not available"
    try:
        version = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, "claude --version failed"
    if not version:
        return None, "claude --version returned nothing"
    run_id = args.run_id or uuid.uuid4().hex[:12]
    artifact_dir = Path(args.artifact_dir).resolve()
    ctx = {
        "artifact_dir": artifact_dir,
        "claude_bin": claude_bin,
        "claude_version": version,
        "timeout": float(args.timeout_seconds),
    }
    leaves: dict[str, Any] = {}
    for name in (leaf,) if leaf else LEAF_NAMES:
        try:
            leaves[name] = _LEAF_FUNCTIONS[name](run_id, ctx)
        except Exception as exc:  # noqa: BLE001 - a crashed leaf is a failed leaf, never a pass
            leaves[name] = {"status": "fail", "violations": [f"leaf crashed: {type(exc).__name__}: {exc}"]}
    evidence = {
        "run_id": run_id,
        "claude_code_version": version,
        "head_sha": _git_head(),
        "aggregate": {"status": "not_applicable", "note": "the generic harness aggregate is never a leaf substitute"},
        "leaves": leaves,
    }
    return evidence, None


def _git_head() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--record-hook":
        return record_hook(argv[1])

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--leaf", choices=LEAF_NAMES, help="inspect only this required leaf")
    parser.add_argument("--evidence-json", help="evaluate an already collected evidence file (offline)")
    parser.add_argument("--artifact-dir", default=str(_DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--claude-bin", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    args = parser.parse_args(argv)

    if args.evidence_json:
        evidence = json.loads(Path(args.evidence_json).read_text(encoding="utf-8"))
    else:
        evidence, skip_reason = collect_live_evidence(args.leaf, args)
        if evidence is None:
            print(f"SKIP: {skip_reason}; runtime AC is NOT passed", file=sys.stderr)
            return EXIT_SKIP

    verdict = evaluate_evidence(evidence, leaf=args.leaf)
    out_dir = Path(args.artifact_dir).resolve() / str(evidence.get("run_id") or "offline")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        **verdict,
        "run_id": evidence.get("run_id"),
        "claude_code_version": evidence.get("claude_code_version"),
        "head_sha": evidence.get("head_sha"),
        "evidence": evidence,
    }
    (out_dir / f"result{'-' + args.leaf if args.leaf else ''}.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(json.dumps({k: verdict[k] for k in ("status", "leaf_results", "violations")}, ensure_ascii=False))
    return verdict["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
