#!/usr/bin/env python3
"""``task-contextctl`` — machine-only typed CLI surface for Task Context v1.

Subcommands (Issue #2563 contract):

    task-contextctl hook <event>
    task-contextctl signal apply
    task-contextctl query current
    task-contextctl projection flush
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
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_service as service  # noqa: E402

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


def _read_stdin_object() -> dict:
    """Read stdin as a raw parsed JSON object (or ``{}`` if stdin is empty).

    This is the *envelope* object -- it is validated/unwrapped separately by
    ``envelope.validate_and_unwrap_request`` in ``main()`` (fix_delta finding
    1). Renamed from the former ``_read_stdin_payload`` because it no longer
    returns an operation payload directly.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise errors.ValidationError(f"stdin must contain exactly one UTF-8 JSON object: {exc}") from exc
    if not isinstance(obj, dict):
        raise errors.ValidationError("stdin JSON object must be a JSON object (mapping)")
    return obj


def _open_db_and_migrate(cwd: str | None = None):
    db_file = config.db_path(cwd=cwd)
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    return conn


def _dispatch(operation: str, payload: dict) -> dict:
    conn = _open_db_and_migrate()
    try:
        if operation == "hook":
            event = payload.get("event", "unknown")
            result = service.append_event(
                conn,
                event_type=f"hook:{event}",
                task_id=payload.get("task_id"),
                activity_id=payload.get("activity_id"),
                binding_id=payload.get("binding_id"),
                execution_run_id=payload.get("execution_run_id"),
                metadata=payload.get("metadata") or {},
            )
            return envelope.build_ok_result({"event_id": result["id"], "event_type": result["event_type"]})

        if operation == "signal_apply":
            task_id = payload.get("task_id")
            repo = payload.get("repo")
            ref_kind = payload.get("ref_kind")
            ref_number = payload.get("ref_number")
            if not (task_id and repo and ref_kind and ref_number is not None):
                raise errors.ValidationError(
                    "signal apply requires task_id, repo, ref_kind, ref_number in payload"
                )
            result = service.claim_task_ref(conn, task_id, repo, ref_kind, int(ref_number))
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

    query_p = sub.add_parser("query")
    query_sub = query_p.add_subparsers(dest="query_command", required=True)
    query_sub.add_parser("current")

    projection_p = sub.add_parser("projection")
    projection_sub = projection_p.add_subparsers(dest="projection_command", required=True)
    projection_sub.add_parser("flush")

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
    elif args.command == "query" and args.query_command == "current":
        operation = "query_current"
    elif args.command == "projection" and args.projection_command == "flush":
        operation = "projection_flush"
    elif args.command == "smoke" and args.smoke_command == "seed":
        operation = "smoke_seed"
    else:  # pragma: no cover - argparse enforces this is unreachable
        result = envelope.build_error_result("VALIDATION_ERROR", "unrecognized command")
        print(json.dumps(result), file=sys.stdout)
        return errors.ValidationError.exit_code

    try:
        stdin_obj = _read_stdin_object()
        # Empty stdin (`{}` from a completely empty pipe/no input) means "no
        # request envelope provided" -- some operations (e.g. `smoke seed`)
        # need no payload at all. Any *non-empty* stdin content, however,
        # MUST be a fully valid request envelope: this is where the frozen
        # `{schema_version, operation, request_id, payload}` shape is
        # actually enforced end-to-end against the real CLI (fix_delta
        # finding 1) -- unwrapping `payload` as the operation-specific
        # input and rejecting mismatched/missing/extra top-level fields.
        payload = envelope.validate_and_unwrap_request(stdin_obj, expected_operation=operation) if stdin_obj else {}
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
