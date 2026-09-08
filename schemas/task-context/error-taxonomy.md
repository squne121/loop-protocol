# Task Context v1 — Error/Result Code Taxonomy

Frozen for Issue #2563. Codes are business-result semantics carried inside
`result-envelope.schema.json`'s `code` field; they are **independent** of
the CLI process exit code (AC8 — "structured business decision と process
exit code を分離する").

| `code`                 | Meaning                                                                                                   | CLI exit code |
|------------------------|-------------------------------------------------------------------------------------------------------------|---------------|
| `OK`                   | Operation succeeded.                                                                                         | 0             |
| `VALIDATION_ERROR`     | Request payload / argument failed validation before touching the DB.                                         | 2             |
| `TEMPORARILY_UNAVAILABLE` | `BEGIN IMMEDIATE` could not acquire the write lock within the bounded `busy_timeout` budget. Retry later. | 3             |
| `CONFLICT`             | A DB physical constraint (unique index / CHECK) rejected the write.                                          | 4             |
| `NOT_FOUND`            | Referenced entity (Task/Activity/Binding/Run/...) does not exist.                                            | 5             |
| `CORRUPT_DATABASE`     | `PRAGMA integrity_check` failed. Never silently reset to an empty DB (AC9).                                  | 6             |
| `SCHEMA_TOO_NEW`       | On-disk `user_version` is newer than this tool's known schema version. Never silently reset (AC9).           | 7             |
| `INTERNAL_ERROR`       | Unclassified failure (defensive catch-all).                                                                   | 1             |

Source of truth for the exception → code → exit-code mapping:
`scripts/task-context/task_context_errors.py` and
`scripts/task-context/task_contextctl.py::EXIT_CODE_BY_ERROR_CODE`.
