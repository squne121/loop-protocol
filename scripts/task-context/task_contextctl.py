#!/usr/bin/env python3
"""``task-contextctl`` — machine-only typed CLI surface for Task Context v1.

Subcommands (Issue #2563 contract; ``projection ack`` and the
``query current`` ``session_id`` selector are additive Issue #2564
extensions -- see Issue #2564 Stop Conditions carve-out permitting additive
typed API/CLI operations within this directory):

    task-contextctl hook <event>
    task-contextctl signal apply
    task-contextctl query current           # payload: {task_id} or {session_id}
    task-contextctl projection flush
    task-contextctl projection ack
    task-contextctl smoke seed

Wire contract: stdin/stdout carry **exactly one UTF-8 JSON object** (the
request envelope on stdin if present, the result envelope on stdout,
always -- even on business failure). stderr is diagnostics-only and is
never parsed by callers. The process exit code is a *separate* signal from
the JSON ``status``/``code`` fields (AC8) -- see ``EXIT_CODE_BY_ERROR_CODE``
below.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MIGRATIONS_DIR = os.path.join(_THIS_DIR, "migrations")
for _dir in (_THIS_DIR, _MIGRATIONS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402

import task_context_envelope as envelope  # noqa: E402
import task_context_errors as errors  # noqa: E402
import task_context_hook_flows as hook_flows  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_service as service  # noqa: E402
import task_context_workflow_signals as workflow_signals  # noqa: E402

# Empty/degraded projection shape returned by the read-only `query current`
# session-selector path (AC9) when there is no DB yet or no Binding
# currently claims the given session_id. Never an error -- a statusLine
# renderer must be able to show "no Task Context yet" without crashing.
_EMPTY_SESSION_PROJECTION = {
    "task": None,
    "activity": None,
    "binding": None,
    "runtime_location": None,
    "task_refs": [],
    "execution_run_id": None,
    "attention": None,
}

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1

EXIT_CODE_BY_ERROR_CODE = {
    "OK": EXIT_OK,
    errors.ValidationError.code: errors.ValidationError.exit_code,
    errors.TemporarilyUnavailableError.code: errors.TemporarilyUnavailableError.exit_code,
    errors.ConflictError.code: errors.ConflictError.exit_code,
    errors.NotFoundError.code: errors.NotFoundError.exit_code,
    errors.CorruptDatabaseError.code: errors.CorruptDatabaseError.exit_code,
    errors.SchemaTooNewError.code: errors.SchemaTooNewError.exit_code,
    "UNKNOWN_OPERATION": EXIT_INTERNAL_ERROR,
}


def _read_stdin_object(raw: str) -> dict:
    """Parse generic CLI input as one JSON object (or ``{}`` when empty)."""
    if not raw.strip():
        return {}
    try:
        obj = workflow_signals.strict_json_loads(raw)
    except json.JSONDecodeError as exc:
        raise errors.ValidationError(f"stdin must contain exactly one UTF-8 JSON object: {exc}") from exc
    if not isinstance(obj, dict):
        raise errors.ValidationError("stdin JSON object must be a JSON object (mapping)")
    return obj


def _parse_signal_apply_input(raw: str) -> tuple[dict | None, dict | None]:
    """Classify direct public signal JSON before generic CLI-envelope parsing.

    A valid legacy request envelope remains on the generic path. Every other
    non-empty direct ``signal apply`` input receives the v1 envelope taxonomy,
    including malformed JSON and a non-object JSON root.
    """
    try:
        parsed = workflow_signals.strict_json_loads(raw)
    except json.JSONDecodeError:
        return None, {"disposition": "rejected_envelope", "reason_code": "MALFORMED_JSON"}
    if envelope.is_valid_request_envelope(parsed):
        return None, None
    return workflow_signals.validate_public_signal(parsed)


def _open_db_and_migrate(cwd: str | None = None):
    db_file = config.db_path(cwd=cwd)
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    return conn


def _dispatch_query_current_by_session(session_id: str) -> dict:
    """AC9: the statusLine `query current` session-selector path is genuinely
    read-only -- it never creates the state-root directory, never creates or
    migrates the DB file, and never mutates projection/GitHub/Herdr state.
    ``task_context_db.connect_readonly`` returns ``None`` when the DB file
    does not exist yet, which we turn into an empty/degraded projection
    rather than creating one."""
    db_file = config.db_path()
    conn = db.connect_readonly(db_file)
    if conn is None:
        data = dict(_EMPTY_SESSION_PROJECTION)
        data["degraded"] = True
        data["degraded_reason"] = "no_state_db"
        return envelope.build_ok_result(data)
    try:
        try:
            projection = service.get_current_projection_for_session(conn, session_id)
        except errors.NotFoundError:
            data = dict(_EMPTY_SESSION_PROJECTION)
            data["degraded"] = True
            data["degraded_reason"] = "no_binding_for_session"
            return envelope.build_ok_result(data)
        projection["degraded"] = False
        projection["degraded_reason"] = None
        return envelope.build_ok_result(projection)
    finally:
        conn.close()


def _dispatch(operation: str, payload: dict) -> dict:
    if operation == "query_current" and payload.get("session_id") and not payload.get("task_id"):
        return _dispatch_query_current_by_session(payload["session_id"])

    if (
        operation == "hook"
        and payload.get("event") in hook_flows.EVENT_HANDLERS
        and not payload.get("herdr_tab_id")
    ):
        # AC11: non-Herdr canonical interactive Claude is observe-only for
        # *every* Native operator lifecycle event, not just SessionStart --
        # short-circuit here so a plain (non-Herdr) Claude Code session
        # never materializes the Task Context state-root/DB file at all as
        # a side effect of a no-op hook firing.
        return envelope.build_ok_result({"decision": "pass", "reason_code": "observe_only_non_herdr"})

    if operation == "smoke_seed" and not config.is_runtime_smoke_scope():
        # Issue #2568 AC6: `smoke seed` is only ever permitted for a caller
        # that has explicitly opted into the isolated runtime-smoke scope
        # (LOOP_TASK_CONTEXT_SCOPE=runtime_smoke). Rejecting here -- strictly
        # before _open_db_and_migrate() below -- means a scope-mismatched
        # caller never materializes the state-root directory, the SQLite DB
        # file, or any migration side effect (proven by a deterministic test
        # asserting the state-root path does not exist afterward). This
        # mirrors the non-Herdr hook early-return pattern immediately above:
        # the DB is never opened for a request this dispatcher already knows
        # to reject.
        raise errors.ValidationError(
            "smoke seed is only permitted when "
            f"{config.SCOPE_ENV_VAR}={config.RUNTIME_SMOKE_SCOPE_VALUE!r}",
            scope=config.resolve_task_context_scope() or None,
        )

    if operation == "smoke_seed" and not os.environ.get(config.STATE_ROOT_ENV_VAR, ""):
        # Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1 (atomic
        # carrier integrity): scope alone is not sufficient -- a runtime-
        # smoke-scoped caller that omitted (or emptied)
        # LOOP_TASK_CONTEXT_STATE_ROOT must be rejected here, strictly
        # before _open_db_and_migrate() below, so the canonical state-root
        # directory/DB file is never materialized or migrated as a side
        # effect. (config.resolve_state_root() also independently raises
        # ValueError in this exact situation -- this explicit check gives
        # the caller a typed VALIDATION_ERROR result/exit code instead of
        # falling through to the generic INTERNAL_ERROR catch-all in
        # _run() below, and documents the invariant at the call site that
        # owns the DB-open decision.)
        raise errors.ValidationError(
            "smoke seed requires an explicit, non-empty, absolute "
            f"{config.STATE_ROOT_ENV_VAR} when "
            f"{config.SCOPE_ENV_VAR}={config.RUNTIME_SMOKE_SCOPE_VALUE!r}",
        )

    conn = _open_db_and_migrate()
    try:
        if operation == "hook":
            event = payload.get("event", "unknown")
            result = hook_flows.dispatch_hook_event(conn, event, payload)
            return envelope.build_ok_result(result)

        if operation == "signal_apply":
            # The public payload is frozen by #2565.  Its caller-visible
            # shape contains no identity selector; the CLI obtains the only
            # permitted origin from the invoking Claude session environment.
            if not payload:
                raise errors.ValidationError("signal apply requires the v1 workflow signal payload")
            result = workflow_signals.apply_workflow_signal(
                conn,
                payload,
                origin_session_id=os.environ.get("CLAUDE_CODE_SESSION_ID"),
            )
            return envelope.build_ok_result(result)

        if operation == "cleanup_begin":
            required = ("repo", "issue_number", "pr_number", "merge_identity")
            if any(key not in payload for key in required):
                raise errors.ValidationError("cleanup begin requires repo, issue_number, pr_number, merge_identity")
            result = workflow_signals.begin_cleanup_lifecycle(
                conn,
                origin_session_id=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                repo=payload["repo"],
                issue_number=payload["issue_number"],
                pr_number=payload["pr_number"],
                merge_identity=payload["merge_identity"],
            )
            return envelope.build_ok_result(result)

        if operation == "query_current":
            task_id = payload.get("task_id")
            if not task_id:
                raise errors.ValidationError("query current requires task_id in payload")
            task = service.get_task(conn, task_id)
            return envelope.build_ok_result({"task": task})

        if operation == "projection_flush":
            projection_key = payload.get("projection_key")
            if not projection_key:
                raise errors.ValidationError("projection flush requires projection_key in payload")
            row = service.flush_projection(conn, projection_key)
            return envelope.build_ok_result({"projection": row})

        if operation == "projection_ack":
            projection_key = payload.get("projection_key")
            read_revision = payload.get("read_revision")
            if not projection_key or read_revision is None:
                raise errors.ValidationError("projection ack requires projection_key and read_revision in payload")
            result = service.ack_projection(conn, projection_key, int(read_revision))
            return envelope.build_ok_result(result)

        if operation == "smoke_seed":
            task = service.create_task(conn, title=payload.get("title") or "task-context smoke seed")
            binding = service.create_binding(conn)
            activity = service.transition_activity(conn, task["id"], kind="smoke")
            run = service.start_execution_run(
                conn,
                run_kind="runtime_smoke",
                task_id=task["id"],
                activity_id=activity["id"],
                binding_id=binding["id"],
            )
            return envelope.build_ok_result(
                {"task_id": task["id"], "activity_id": activity["id"], "binding_id": binding["id"], "run_id": run["id"]}
            )

        raise errors.ValidationError(f"unknown operation {operation!r}")
    finally:
        conn.close()


def _run(operation: str, payload: dict) -> tuple[dict, int]:
    try:
        result = _dispatch(operation, payload)
        return result, EXIT_OK
    except errors.TaskContextError as exc:
        result = envelope.build_error_result(exc.code, exc.message, exc.details)
        return result, EXIT_CODE_BY_ERROR_CODE.get(exc.code, EXIT_INTERNAL_ERROR)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        result = envelope.build_error_result("INTERNAL_ERROR", str(exc))
        return result, EXIT_INTERNAL_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-contextctl")
    sub = parser.add_subparsers(dest="command", required=True)

    hook_p = sub.add_parser("hook")
    hook_p.add_argument("event")

    signal_p = sub.add_parser("signal")
    signal_sub = signal_p.add_subparsers(dest="signal_command", required=True)
    signal_sub.add_parser("apply")

    cleanup_p = sub.add_parser("cleanup")
    cleanup_sub = cleanup_p.add_subparsers(dest="cleanup_command", required=True)
    cleanup_sub.add_parser("begin")

    query_p = sub.add_parser("query")
    query_sub = query_p.add_subparsers(dest="query_command", required=True)
    query_sub.add_parser("current")

    projection_p = sub.add_parser("projection")
    projection_sub = projection_p.add_subparsers(dest="projection_command", required=True)
    projection_sub.add_parser("flush")
    projection_sub.add_parser("ack")

    smoke_p = sub.add_parser("smoke")
    smoke_sub = smoke_p.add_subparsers(dest="smoke_command", required=True)
    smoke_sub.add_parser("seed")

    args = parser.parse_args(argv)

    # The CLI subcommand/argv shape deterministically decides the operation
    # *before* stdin is even read -- this is the "argv から決定される
    # operation" that the request envelope's `operation` field (if a full
    # envelope is provided on stdin) is validated against below (fix_delta
    # finding 1).
    if args.command == "hook":
        operation = "hook"
    elif args.command == "signal" and args.signal_command == "apply":
        operation = "signal_apply"
    elif args.command == "cleanup" and args.cleanup_command == "begin":
        operation = "cleanup_begin"
    elif args.command == "query" and args.query_command == "current":
        operation = "query_current"
    elif args.command == "projection" and args.projection_command == "flush":
        operation = "projection_flush"
    elif args.command == "projection" and args.projection_command == "ack":
        operation = "projection_ack"
    elif args.command == "smoke" and args.smoke_command == "seed":
        operation = "smoke_seed"
    else:  # pragma: no cover - argparse enforces this is unreachable
        result = envelope.build_error_result("VALIDATION_ERROR", "unrecognized command")
        print(json.dumps(result), file=sys.stdout)
        return errors.ValidationError.exit_code

    raw_stdin = sys.stdin.read()
    try:
        payload = None
        if operation == "signal_apply" and raw_stdin.strip():
            payload, rejection = _parse_signal_apply_input(raw_stdin)
            if rejection is not None:
                print(json.dumps(envelope.build_ok_result(rejection)), file=sys.stdout)
                return EXIT_OK
        if payload is None:
            stdin_obj = _read_stdin_object(raw_stdin)
            # signal apply's public v1 wire payload is intentionally direct:
            # it has exactly four top-level fields. Retain the request-envelope
            # transport only as an internal compatibility wrapper for hook clients.
            if operation == "signal_apply" and stdin_obj and (
                "signal_kind" in stdin_obj or "source" in stdin_obj or "evidence" in stdin_obj
            ):
                payload = stdin_obj
            else:
                payload = (
                    envelope.validate_and_unwrap_request(stdin_obj, expected_operation=operation) if stdin_obj else {}
                )
    except errors.ValidationError as exc:
        result = envelope.build_error_result(exc.code, exc.message, exc.details)
        print(json.dumps(result), file=sys.stdout)
        return exc.exit_code

    if args.command == "hook":
        payload.setdefault("event", args.event)

    result, exit_code = _run(operation, payload)
    print(json.dumps(result), file=sys.stdout)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
