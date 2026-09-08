#!/usr/bin/env python3
"""Helper subprocess for the AC11 concurrent-migration test.

Not a pytest test module itself (leading underscore keeps pytest from
collecting it). Connects to the given DB file and runs the migration
runner, retrying a bounded number of times on TEMPORARILY_UNAVAILABLE (the
correct caller-level behavior -- see docs/dev/task-context.md ## Busy/Retry
Budget). Prints the resulting JSON {"user_version": <int>} to stdout on
success, or {"error": "<code>"} and a non-zero exit code on failure.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts" / "task-context"
_MIGRATIONS_DIR = _SCRIPTS_DIR / "migrations"
for _dir in (str(_SCRIPTS_DIR), str(_MIGRATIONS_DIR)):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_db as db  # noqa: E402
import task_context_errors as errors  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402

MAX_RETRIES = 20
RETRY_SLEEP_SECONDS = 0.05


def main() -> int:
    db_file = pathlib.Path(sys.argv[1])
    last_error = None
    for _ in range(MAX_RETRIES):
        conn = db.connect(db_file)
        try:
            version = migration_runner.migrate(conn)
            print(json.dumps({"user_version": version}))
            return 0
        except errors.TemporarilyUnavailableError as exc:
            last_error = exc
            time.sleep(RETRY_SLEEP_SECONDS)
            continue
        except errors.TaskContextError as exc:
            print(json.dumps({"error": exc.code, "message": str(exc)}))
            return 1
        finally:
            conn.close()
    print(json.dumps({"error": "TEMPORARILY_UNAVAILABLE_RETRIES_EXHAUSTED", "message": str(last_error)}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
