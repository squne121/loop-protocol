# Task Context v1 — Error/Result Code Taxonomy

Frozen for Issue #2563. Codes are business-result semantics carried inside
`result-envelope.schema.json`'s `code` field; they are **independent** of
the CLI process exit code (AC8 — "structured business decision と process
exit code を分離する").
この表は Issue #2563 で内容を凍結した業務結果コードの一覧であり、各値は
`code` フィールドに格納される構造化された業務判断を表す。ここで言う
「業務結果」とは、プロセスの成否そのものを示す exit code とは独立した
意味論であり、両者を混同しないことが AC8 の要件である。exit code は
あくまで CLI プロセス終了時の shell 互換な数値に過ぎず、業務上の意味
判断は必ず `code` フィールド側で表現する。

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
この対応関係（例外種別から業務結果コード、そして CLI の exit code への
変換ロジック）の正本は上記の実装ファイルである。この表と実装ファイルの
内容が食い違った場合は、常に実装ファイル側を正として扱う。
