---
summary_ja: "本ドキュメントは Task Context v1 の SQLite registry / schema / migration / typed API 契約の正本である。"
feature: task-context-v1-core
status: implemented (core registry / schema / typed API only)
related_issue: "#2563"
parent_issue: "#2562"
---

# Task Context v1 — Core Data Model / Authority / State Machine / Transaction & Recovery Invariants

## 目的とスコープ境界

Task Context v1 は、repository instance ごとに 1 個の local SQLite database を
live-state SSOT（Task / Activity / TabBinding / ExecutionRun /
RuntimeLocation / GitHub-ref-claim / Event / projection-outbox）として提供する。
本 Issue（#2563）が追加するのは **schema / migration、typed internal service
API、machine-only CLI（`task-contextctl`）、deterministic tests のみ** である。

`.claude/settings.json` の hook 配線、Herdr/statusLine projection、Claude-GPT
launcher 配線、`worktree-agent-runtime-smoke`、cold-restart dispatcher、
daemon/dashboard/message-bus は本 Issue の Out of Scope（親 #2562 参照）。

## リポジトリ構成

```
docs/dev/task-context.md              -- 本ドキュメント
schemas/task-context/
  request-envelope.schema.json        -- 凍結された request envelope 形状
  result-envelope.schema.json         -- 凍結された result envelope 形状
  error-taxonomy.md                   -- code -> exit code マッピング表
scripts/task-context/
  task_context_schema.py              -- canonical DDL（single source of truth）
  task_context_config.py              -- repo_instance_key / state-root 解決
  task_context_db.py                  -- connection factory + transaction helper
  task_context_errors.py              -- typed error taxonomy
  task_context_envelope.py            -- request/result envelope builder
  task_context_service.py             -- core service layer（全 DB 書き込み）
  task_contextctl.py                  -- CLI entrypoint
  measure_synchronous_latency.py      -- WSL2 latency 実測用の one-off script（CI gate ではない）
  migrations/
    task_context_migration_runner.py  -- PRAGMA user_version ベース migration runner
tests/task-context/                   -- deterministic pytest suite（AC1-AC13）
```

### `task_context_*` prefix のbare module名を使う理由（package 化しない理由）

`scripts/task-context` と `tests/task-context` はハイフンを含むディレクトリ名
であり、通常の Python package として import できない（`import
scripts.task_context...` は valid ではない）。本サブシステムの各モジュールは
`sys.path` へディレクトリ自体（および必要な場合は `migrations/`）を挿入した
上で、一意な prefix 付き bare name（`task_context_schema`、`task_context_db`
等）で import する。`task_context_` prefix は、このリポジトリの統合 pytest
セッションでモジュール名衝突を避けるためのもの（過去に本リポジトリで観測
された failure mode：`sys.path` bootstrap を行う別 skill のテストスイート間
で `schema` や `db` のような汎用的な bare name が衝突する）。

## Repository Instance Identity（`repo_instance_key`） — リポジトリインスタンス識別子の定義

```python
repo_instance_key = sha256(realpath(git rev-parse --path-format=absolute --git-common-dir)).hexdigest()
```

- `git rev-parse --path-format=absolute --git-common-dir` は、main worktree
  と `git worktree add` で作られた linked worktree に対して **同一の** 物理
  `.git` common directory を返す（Git の worktree モデルそのもの：linked
  worktree は共有の common dir と自身の `.git/worktrees/<name>` admin
  directory を持つが、`--git-common-dir` は常に共有 main common dir を返す）。
- 別の `git clone` は完全に別の `.git` directory を持つため、別の
  `repo_instance_key` になる。
- symlink 経由の checkout path でも同一 key に canonicalize されるよう、
  hash 前に `realpath()` を適用する。
- `tests/task-context/test_repo_instance_key.py` にて、実際に temp repo
  （`git init`）、`git worktree add` による linked worktree、`git clone` を
  作成し `main_key == linked_key != clone_key` を検証する。
- worktree 自体の path は identity key として **意図的に使わない**（Issue の
  Stop Conditions が worktree/path/session-id/Herdr-tab-position を Task
  identity へ昇格させることを明示的に禁止している）。

## `LOOP_TASK_CONTEXT_STATE_ROOT` の解決規則

解決順序（`scripts/task-context/task_context_config.py::resolve_state_root`）:

1. `LOOP_TASK_CONTEXT_STATE_ROOT` が非空で設定されている場合、それは
   **absolute path でなければならない**。relative な値は即座に `ValueError`
   を raise する — cwd を基準に黙って解決することはしない。
2. 未設定の場合: `$XDG_STATE_HOME/loop-protocol/task-context/v1/<repo_instance_key>/`。

### `XDG_STATE_HOME` 未設定時の default

[freedesktop.org XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html)
より、`XDG_STATE_HOME` 未設定時の挙動を定義した一節を引用する:

> `$XDG_STATE_HOME` defines the base directory relative to which
> user-specific state files should be stored. If `$XDG_STATE_HOME` is
> either not set or empty, a default equal to `$HOME/.local/state` should
> be used.
>
> All paths set in these environment variables must be absolute. If a path
> is relative, the behavior is undefined.
>
> （日本語訳の要旨: `$XDG_STATE_HOME` が未設定または空の場合は
> `$HOME/.local/state` を既定値として使う。これらの環境変数に設定する
> パスはすべて絶対パスでなければならず、相対パスの場合の挙動は
> 仕様上未定義である。）

本実装はこの "undefined" なケースを曖昧なままにせず具体化する：
**relative（absolute でない）`$XDG_STATE_HOME` は無効/未設定として扱い**、
spec が既定する default（`$HOME/.local/state`）を使う — プロセスの cwd を
基準に黙って解決することはしない（これは
`LOOP_TASK_CONTEXT_STATE_ROOT` 側の override で明示的に禁止されている
failure mode を、上流の `XDG_STATE_HOME` にも一貫して適用したもの）。
これにより「`XDG_STATE_HOME` 自体が未設定の場合どうするか」という以前の
non-blocking warning を、曖昧なままにせず具体的に解決する。

canonical DB file path は常に `<resolved-root>/task-context.sqlite3`。

## Current-State Tables（v1、`PRAGMA user_version = 1`） — 現在状態を保持するテーブル一覧

固定 10 テーブル構成（Issue body の Scope Growth Guard 参照 — v1 consumer が
存在しない table/field は追加しない）:

| Table | 役割 |
|---|---|
| `db_meta` | 汎用 key/value introspection store。 |
| `tasks` | Task lifecycle root（`OPEN` / `DONE` / `ABANDONED`）。 |
| `task_refs` | Task に紐づく Issue/PR reference candidate。 |
| `task_ref_claims` | ref（Issue/PR）の live ownership claim。 |
| `activities` | Task 配下の unit of work。`ACTIVE` は最大 1。 |
| `tab_bindings` | durable operator-binding identity（`binding_id`）。 |
| `runtime_locations` | Binding の Herdr locator **observation** history（mutable）。 |
| `execution_runs` | Native/SubAgent/runtime-smoke/Claude-GPT の run record。 |
| `events` | append-only な retrospective history（raw content は持たない）。 |
| `projection_outbox` | desired-revision のみを保持する projection queue（coalescing）。 |

DDL 全体: `scripts/task-context/task_context_schema.py`（`DDL_V1`）。

### 設計判断: `runtime_locations` を `tab_bindings` から分離した理由

Outcome 本文は「Herdr locator は mutable observation として保存する」と
述べ、AC5 は Herdr locator の変更が `binding_id`/Task/Activity identity を
変えないことを要求する。locator を `tab_bindings` 上の単なる mutable column
としてモデル化すると、AC1(c)（「unreleased Binding の重複」を物理的に拒否
すること）は自明に真になってしまう（1 identity = 1 row のテーブルは自分自身
と「重複」しえない）か、「Binding」を row identity 以外の何かとして再解釈
する必要が生じる。そこで:

- `tab_bindings.id` を durable `binding_id` とする。これは一度だけ作成され、
  再挿入されることはない。`current_claude_session_id` / `runtime_health` /
  `updated_at` のみが in place で更新される。
- `runtime_locations` が実際の locator **observation** を append 的な
  history として保持し、`binding_id` へ参照する。partial unique index
  （`ux_runtime_locations_unreleased_per_binding`）が「1 binding につき
  `released_at IS NULL` の行は最大 1」を強制する — つまり、ある時点で
  binding が持つ "current"（unreleased）な location observation は最大 1
  個（AC1c）。relocate は「古い observation を release（`UPDATE ... SET
  released_at = now`）+ 新しい observation を insert」を 1 transaction 内で
  行う（`task_context_service.relocate_binding`）。

これにより AC1(c) に実質的かつテスト可能な物理制約としての意味を持たせつつ、
`binding_id` はrelocate を跨いで完全に安定させる（AC5）——
`tests/task-context/test_execution_runs_and_bindings.py::test_given_binding_when_relocated_then_task_and_activity_fk_bindings_are_unaffected`
参照。

## Physical Constraints（AC1） — 物理制約

すべて SQLite の `UNIQUE`/partial `UNIQUE INDEX`/`CHECK` で強制する
（application logic のみに依存しない）。各制約は
`tests/task-context/test_schema_and_migration.py` に、service layer を経由
せず **raw SQL で直接** 競合行を insert し `sqlite3.IntegrityError` が
SQLite 自身から raise されることを確認する negative test を持つ。

| AC | 制約 | 実装 |
|---|---|---|
| 1(a) | 同一 Issue/PR を 2 Task が同時に live-claim できない | `ux_task_ref_claims_live` — `UNIQUE(repo, ref_kind, ref_number) WHERE released_at IS NULL` |
| 1(b) | 1 Task の `ACTIVE` Activity は最大 1 | `ux_activities_active_per_task` — `UNIQUE(task_id) WHERE status = 'ACTIVE'` |
| 1(c) | 1 Binding の unreleased location observation は最大 1 | `ux_runtime_locations_unreleased_per_binding` — `UNIQUE(binding_id) WHERE released_at IS NULL` |
| 1(d) | 1 Binding の open managed operator run/session は最大 1 | `ux_execution_runs_open_managed_per_binding` — `UNIQUE(binding_id) WHERE run_kind IN ('native_operator', 'claude_gpt') AND ended_at IS NULL AND binding_id IS NOT NULL` |
| 1(e) | `claude_session_id` は **currently open managed** run の間でのみ unique | `ux_execution_runs_open_managed_session` — `UNIQUE(claude_session_id) WHERE claude_session_id IS NOT NULL AND run_kind IN ('native_operator', 'claude_gpt') AND ended_at IS NULL` |

(e) は意図的に `run_kind IN ('native_operator', 'claude_gpt') AND ended_at
IS NULL` にスコープしている：historical（ended）run と non-managed run
（例: SubAgent）はこの制約から除外される。したがって、先行 run が ended
した後に同一 `claude_session_id` を新しい `ExecutionRun` へ再 attach する
ことは引き続き許可される
（`test_given_historical_ended_run_when_same_session_id_reattached_to_new_run_then_allowed`
で明示的にテスト）。

### `is_managed` は `run_kind` から derive される（fix_delta finding 2）

`is_managed` は caller が独立に指定できるフラグでは **ない**。
`execution_runs` の `CHECK` 制約が
`(run_kind IN ('native_operator', 'claude_gpt') AND is_managed = 1) OR
(run_kind IN ('subagent', 'runtime_smoke') AND is_managed = 0)`
を強制し、`task_context_service.start_execution_run` も同じ導出を
application 層で行う（belt-and-suspenders）。以前は `is_managed` が
`run_kind` と無関係に自由指定でき、raw SQL 経由で (a)
`native_operator`/`claude_gpt` を `is_managed=0` として AC1(d)/(e) の
partial unique index を回避する、(b) `subagent`/`runtime_smoke` を
`is_managed=1` として managed operator run を偽装する、という 2 つの
bypass が可能だった。上記 CHECK と、1(d)/(e) の index 自体を
`is_managed` ではなく `run_kind IN (...)` を authority にする変更により、
どちらも DB 物理的に不可能になった。

### `tab_bindings.current_claude_session_id` の一意性（fix_delta finding 3）

`current_claude_session_id` は `#2569` cold-restore が要求する durable
recovery field であり、削除しない。一方で以前は `set_binding_session()` が
同じ非 null session id を任意の複数 Binding へ書き込め、
`execution_runs.claude_session_id` の AC1(e) partial unique index と
無関係な第 2 の SSOT になっていた（"session identity の二重 SSOT"）。

- `ux_tab_bindings_current_session` — `UNIQUE(current_claude_session_id)
  WHERE current_claude_session_id IS NOT NULL`: 非 null な session id を
  「current」として claim できる Binding は同時に最大 1 個という DB
  物理制約。
- `task_context_service.set_binding_session()` は非 null な
  `claude_session_id` を設定する際、対象 binding 上の open かつ managed な
  `ExecutionRun`（`execution_run_id` で指定、`run_kind IN
  ('native_operator', 'claude_gpt')` かつ `ended_at IS NULL`）が同じ
  `claude_session_id` を保持していることを同一 transaction 内で検証する。
  これにより Binding 側のコピーと ExecutionRun 側の SSOT が
  transaction 境界で同期される。
- `task_context_service.get_binding_by_current_session()` が
  `session_id S -> exactly one current Binding` を解決する。

「duplicate open Task」はこの表の制約対象では **ない** — Issue が明示的に
未定義の dedupe basis として除外している（AC1 note、AC4）。
`task_ref_claims` が実際の well-defined な機構を提供する：candidate Task が
live ref-claim を取得し、競合した場合 loser は
`task_context_service.claim_task_ref` の `{"status": "conflict",
"winning_task_id": ...}` 経由で winning Task を readback する。

## Logical FK / Relational Integrity（fix_delta finding 7）

AC1 の unique/partial-unique 制約に加え、以下の relational integrity も
DB 物理制約（application validation だけでなく）で保証する:

- **`execution_runs.task_id`/`activity_id` の整合性**: 1 つの
  `ExecutionRun` の `task_id` と `activity_id` は同じ Task を指さなければ
  ならない。`task_context_service._validate_task_activity_consistency` が
  `start_execution_run`/`attach_execution_run` の中で事前検証する
  （typed `ValidationError`）。加えて
  `trg_execution_runs_task_activity_consistency_insert`/`_update`
  トリガーが raw SQL による bypass を物理的に拒否する
  （`RAISE(ABORT, ...)`、NULL-safe な `IS NOT` 比較）。
- **`task_ref_claims` の冗長 column**: `task_ref_claims.task_id`/`repo`/
  `ref_kind`/`ref_number` は参照元 `task_refs`（`task_ref_id` 経由）の
  値と重複している。これらを削除できないのは、AC1(a) の
  `ux_task_ref_claims_live` partial unique index が `task_ref_claims`
  単独テーブル上でしか定義できない（SQLite の partial index は join を
  跨げない）ためである。代わりに `task_refs(id, task_id, repo, ref_kind,
  ref_number)` 上の covering unique index
  （`ux_task_refs_id_task_id_repo_kind_number`）を parent 側キーとする
  composite `FOREIGN KEY (task_ref_id, task_id, repo, ref_kind,
  ref_number) REFERENCES task_refs(id, task_id, repo, ref_kind,
  ref_number)` を `task_ref_claims` に追加した。これにより、ある
  `task_ref_id` に対して不整合な `task_id`/`repo`/`ref_kind`/`ref_number`
  を持つ行を raw SQL で insert することが DB 物理的に不可能になる。

## Transaction Boundaries（AC2） — トランザクション境界

`task_context_service` の状態変更 function はすべて **正確に 1 回**
`task_context_db.write_transaction` context manager を開く。これは明示的な
`BEGIN IMMEDIATE` を発行し、内部の全 statement が成功すれば `COMMIT`、
exception が発生すれば `ROLLBACK` する — bare な `BEGIN`（`DEFERRED`
transaction。書き込みロックへ transaction 途中で昇格しうるため contention
下で deadlock/starve しやすい）は使わない。これらはすべて read-modify-write
であり、最初から write lock が必要なため `BEGIN IMMEDIATE` を用いる。

Task rebind / Activity transition / ref claim / run attach / event append /
outbox enqueue といった複数ステップの flow は、それぞれ 1 つの function・
1 つの `write_transaction` block として実装されている（例:
`transition_activity` は現在の `ACTIVE` Activity を読んで終了させ、新しい
Activity を insert する処理を 1 transaction で行う。`relocate_binding` は
古い location の release と新しい location の insert を 1 transaction で
行う）。

`tests/task-context/test_transactions_and_busy_retry.py::test_given_transition_activity_when_it_runs_then_exactly_one_begin_immediate_is_issued`
にて SQLite trace callback を使い、operation 全体で `BEGIN IMMEDIATE` が
ちょうど 1 回だけ発行されることを検証する。また rollback completeness
test では、transaction 途中で強制的に失敗させ、部分的な行が一切残らない
ことを確認する。

## External I/O は Write Transaction 内で実行しない（AC3）

`task_context_service.py` は external I/O を一切行わない — `subprocess` /
`urllib` / `requests` / `socket` / `httpx` の import が存在しない
（`tests/task-context/test_transactions_and_busy_retry.py::test_given_service_module_source_when_scanned_then_no_external_io_imports_present`
にて module source の AST scan で構造的に確認。runtime mock だけでなく、
将来の編集でこのモジュールに該当 import が追加された場合の regression も
検出する）。

本サブシステムで唯一 external process へ触れるのは
`task_context_config.repo_instance_key`（`git rev-parse` を呼ぶ）のみで、
これは DB file の場所を解決するための副作用のない read であり、connection
/transaction を開く前に呼ばれ、`task_context_service` からは一切呼ばれない。

two-phase な flow は、external I/O を **transaction の外側** で挟めるよう
意図的に別 function に分離している:

- `claim_task_ref`（DB のみ） → caller が必要なら GitHub/Git I/O を行う →
  caller が winner/loser の結果をどう扱うか決める。
- `flush_projection`（read-only、DB のみ） → caller が実際の
  Herdr/statusLine projection I/O を行う → `ack_projection`（DB のみ、
  conditional delete）。

## Busy/Retry Budget — ビジー時リトライ予算

`PRAGMA busy_timeout`（default `200ms`、
`task_context_db.DEFAULT_BUSY_TIMEOUT_MS`）が `BEGIN IMMEDIATE` contention
の SQLite level waiting budget の **単一の所有者** である。この値は Issue
自身が示す 100–250ms の目安の中間値であり、以下で正当化する:

- [SQLite `busy_timeout` documentation](https://www.sqlite.org/pragma.html#pragma_busy_timeout)
  は、handler が指数的に増加する間隔で短く sleep しながら total timeout
  まで待ち、その後 `SQLITE_BUSY` を返すと述べている。
- 本 WSL2 host での実測（`measure_synchronous_latency.py`）: 1 回の
  `BEGIN IMMEDIATE; INSERT; COMMIT` write transaction は
  約 **0.01ms**（`synchronous=NORMAL`、checkpoint boundary なし）〜
  約 **5ms**（`synchronous=FULL`、checkpoint boundary あり）程度（詳細は
  下記「NORMAL vs FULL」参照）。200ms の budget は、数十件の短い write が
  背後に詰まっても人間が知覚できるレベル（hot path が数秒 block する）に
  近づくことは全くないほど余裕があり、かつ genuinely stuck な writer
  （transaction を open したまま止まる bug 等）は fail fast で typed かつ
  retryable な error を返す短さでもある。
- **application 側のコードはこの上に第 2 の retry/sleep loop を積まない
  （fix_delta finding 5）。** `task_context_db.write_transaction` と
  `task_context_db._configure_pragma`（旧
  `_execute_with_lock_retry` — 以前の実装は `busy_timeout_ms` の **5倍**
  の deadline で独自の sleep/retry loop を回しており、connection-level
  budget の上に第 2 の budget を積み上げていた。この amplification は削除
  済みで、`_configure_pragma` は `conn.execute(sql)` を 1 回呼び、その
  outcome を型付き例外へ翻訳するだけである）は busy_timeout の期限切れを
  そのまま `TemporarilyUnavailableError`（`TEMPORARILY_UNAVAILABLE`）に
  変換して即座に return する — caller（例: AC11 の concurrent-migration
  test helper `tests/task-context/_migration_worker.py`）が、必要なら
  operation 全体を bounded かつ小さい interval で自分の判断として明示的に
  retry する責任を持つ。これは、見えない多段の wait スタックではなく、
  caller に可視な独立した決定である。
- bounded budget を超えない（hang しない、数秒単位で block しない）ことは
  `tests/task-context/test_transactions_and_busy_retry.py::test_given_two_connections_contending_when_second_begin_immediate_blocked_then_temporarily_unavailable_and_bounded`
  および、`connect() -> migrate() -> service write` の full path を対象と
  した deterministic correctness test
  `test_given_full_open_migrate_operation_path_when_write_lock_held_then_wait_is_bounded_by_single_busy_timeout_budget`
  で検証する（後者が fix_delta finding 5 の "既存テストは connect path を
  測っていない" 指摘への対応）。

`BEGIN IMMEDIATE` の競合は、最初の `BEGIN IMMEDIATE` 自体が lock を取れない
場合と、transaction 途中で同種の `sqlite3.OperationalError` が発生した場合
の両方で、typed `TemporarilyUnavailableError`（`TEMPORARILY_UNAVAILABLE`）
へ mapping する — 生の `sqlite3.OperationalError` が伝播することはなく、
hang することもない。

### `journal_mode=WAL` mode-transition contention（実装上の注記）

新規 DB file への *多数の同時初回接続*（AC11 の multi-process migration
test で実際に発生させている）の下では、`PRAGMA journal_mode=WAL` の
mode-transition 操作自体が、service layer の transaction 機構がまだ何も
動いていない connect() 時点で retryable な "database is locked"
`OperationalError` を raise することがある。この待機は
`sqlite3.connect(..., timeout=busy_timeout_ms / 1000.0)` が connect() の
最初に登録する SQLite 自身の native busy handler（`PRAGMA busy_timeout` と
等価）によって **単一の budget として** 既に吸収されている（fix_delta
finding 5）。`task_context_db.connect()` は各 configuration pragma を
`_configure_pragma` でラップするが、これは追加の sleep/retry loop では
なく、その 1 回きりの実行結果を型付き例外へ翻訳するだけである:
`OperationalError`（"locked"/"busy"）は `TemporarilyUnavailableError` へ、
genuine な corruption（`OperationalError` ではない
`sqlite3.DatabaseError`、例えば "file is not a database"）は transient
contention と混同されず即座に `CorruptDatabaseError` として raise される
（AC9）。

## Migration Ordering and Concurrency（AC2, AC9, AC11） — マイグレーションの順序と並行実行

`scripts/task-context/migrations/task_context_migration_runner.py::migrate`:

1. connection は `task_context_db.connect()` によって既に完全に
   configuration 済み — `busy_timeout` → `foreign_keys=ON` →
   `journal_mode=WAL` → `synchronous=...` の順で **すべて transaction の
   外側** で実行される（SQLite は transaction 中の `journal_mode` 変更を
   拒否/黙って無視するため、この順序は cosmetic ではなく load-bearing）。
2. `migrate()` は `PRAGMA user_version` を transaction の **外側** で読む
   （既に current な場合の安価な no-op short-circuit）。
3. on-disk version が `CURRENT_SCHEMA_VERSION` より大きい場合 → typed
   `SchemaTooNewError`、それ以上の操作は行わない（AC9 — silent reset
   しない）。
4. on-disk version が `CURRENT_SCHEMA_VERSION` と等しい場合 → no-op で
   return。
5. それ以外の場合: `PRAGMA integrity_check` を transaction の **外側** で
   実行 — 失敗した場合は typed `CorruptDatabaseError`、schema を "reset"
   する処理には進まない（AC9）。
6. `BEGIN IMMEDIATE`（並行する migrator 同士を直列化する — AC11）→
   transaction 内で `user_version` を **再読み込み**（write lock 取得を
   待っている間に別プロセスが既に migrate 済みの可能性があるため）→
   current version から target version までの各 version の DDL statement
   を個別の `conn.execute(statement)` 呼び出しで実行（**`conn.executescript`
   は使わない** — Python `sqlite3` docs によれば pending transaction が
   あれば先に暗黙 commit してしまい、この single-transaction guarantee を
   静かに破壊するため）→ 適用した version ごとに `PRAGMA user_version` を
   進める → `COMMIT`。

Concurrency safety（AC11）は `tests/task-context/test_concurrent_migration.py`
にて **実際の別 OS process**（thread ではない）で検証する: 8 個の同時
`python3 _migration_worker.py <同一 db file>` subprocess が 1 個の fresh DB
file の migrate を競争する。各 worker は上述の正しい caller-level パターン
に従い `TEMPORARILY_UNAVAILABLE` を retry する。test は最終的に
`user_version` が正しいこと、`sqlite_master` 上のすべての
table/index/trigger 名が **ちょうど 1 回** だけ現れること（double-apply
なし）、`PRAGMA integrity_check` が依然 `ok` を返すこと（half-migrated
state なし）を assert する。

## Typed Error / Result Taxonomy（AC8, AC9） — 型付きエラーと結果の分類

code 表全体は `schemas/task-context/error-taxonomy.md` を参照。
source of truth は `scripts/task-context/task_context_errors.py`。

business result（JSON result envelope 内の `status`/`code`）は CLI process
exit code から意図的に **独立** している — 両方が常に emit される
（process は常に mapping された exit code で終了し、stdout は常に実際の
business outcome を記述する JSON object を 1 個だけ carry する）ため、
shell caller は exit code で分岐し、structured consumer は JSON body を
parse できる（AC8）。

## Request/Result Envelope（AC8） — リクエスト/レスポンス封筒

凍結された形状（`schemas/task-context/request-envelope.schema.json`、
`result-envelope.schema.json`）:

```json
// request
{"schema_version": "task-context-request/v1", "operation": "...", "request_id": "...", "payload": {...}}
// result
{"schema_version": "task-context-result/v1", "status": "ok" | "error", "code": "...", "data": {...}}
```

`payload` と `data` の内部は意図的に **open** である — 本 Issue は
operation-specific field をその中に先回りして列挙しない（明示的な
non-goal / Out of Scope）。後続の consumer child（#2564 hook 配線、#2565
workflow signal、#2568 runtime-smoke 等）は、envelope 自体を変更せずに
`payload`/`data` の中へ additive に field を追加できる。
`tests/task-context/test_envelope_and_cli.py::test_given_hook_event_when_run_via_cli_with_extra_additive_payload_fields_then_still_accepted`
で検証。

**CLI は実際に top-level envelope を検証・unwrap する（fix_delta finding
1）**: 以前は `task-contextctl` が stdin の生 JSON をそのまま operation
payload として扱っており、この節が説明する凍結 envelope 形状を
実際には検証していなかった。`task_context_envelope.validate_and_unwrap_request`
が stdin の non-empty JSON object を top-level envelope として厳密に検証
する（`additionalProperties: false` 相当の extra-field 拒否、missing
field 拒否、`schema_version` 一致検証、`payload` が object であることの
検証）。加えて、argv/subcommand から決定される operation（例:
`hook`/`signal_apply`/`query_current`/`projection_flush`/`smoke_seed`）と
`envelope.operation` の一致を検証し、不一致は typed `ValidationError`
にする。stdin が完全に空の場合のみ（payload 不要な operation 向け）
envelope 検証をスキップし、payload を `{}` として扱う。
`tests/task-context/test_envelope_and_cli.py` に、実際の `task_contextctl.py`
を subprocess として起動し canonical envelope を渡す end-to-end test、
および envelope の missing/extra field・schema_version 不一致・operation
不一致を検証する negative test を追加した。

## Typed CLI Surface（`task-contextctl`） — 型付き CLI インターフェース

```
task-contextctl hook <event>
task-contextctl signal apply
task-contextctl query current
task-contextctl projection flush
task-contextctl smoke seed
```

Wire contract: stdin/stdout は **exactly one UTF-8 JSON object** を
carry する。stderr は diagnostics-only（parse されない）。
`tests/task-context/test_envelope_and_cli.py` にて、business-error path を
含むすべての subcommand で stdout が凍結された result envelope 形状と一致
する valid JSON をちょうど 1 行だけ含むことを検証する。

## `events` の Append-Only 制約と Raw Content 非保存（AC7）

2 つの独立した layer で保証する:

1. **DB layer**: `events` 上の `BEFORE UPDATE`/`BEFORE DELETE` trigger
   （`trg_events_no_update`、`trg_events_no_delete`）が無条件に
   `RAISE(ABORT, ...)` する — `tests/task-context/test_events_append_only.py`
   で raw SQL により直接検証。
2. **Typed-API layer**: `task_context_service.append_event` は `metadata`
   を **allowlist**（`ALLOWED_EVENT_METADATA_KEYS`）と照合する。この
   allowlist は id / count / status code 等の小さな structured field のみ
   を許可し、未知の key は `ValidationError` で拒否、200 文字を超える
   string 値も拒否する。これは（"prompt"/"transcript"/... という名前の
   blocklist ではなく）allowlist であることが重要で、raw prompt/
   transcript/full-command/message-body content が未知の key 経由で
   `events` へ到達することを構造的に防ぐ。複数の forbidden key
   （`prompt`、`transcript`、`command`、`message_body`、`raw_input`、
   `text`）と oversized-string case について negative test を持つ。

## `projection_outbox` の Revision-Aware Conditional Ack（AC12） — リビジョン整合の確認応答

`projection_outbox` は **`projection_key` ごとに 1 行**を保持し、最新の
desired revision のみへ coalesce する（per-event history queue には
しない — Scope Growth Guard）。

**marker-only SSOT（fix_delta finding 4）**: `projection_outbox` は
`projection_key`/`desired_revision`/`enqueued_at`/`updated_at` のみを
持ち、projection payload そのものは保持しない（`payload_json` column は
存在しない）。projection の実際の内容は、flush 時に canonical DB state
（Task/Activity/Binding/... の各テーブル）から都度再導出する —
`projection_outbox` を projection content の第 2 の SSOT にはしない。

- `enqueue_projection(key, revision)`: upsert。
  `revision > current desired_revision` の場合のみ上書きする（古い
  revision へ後退しない）。
- `flush_projection(key)`: caller が DB transaction の **外側** で
  canonical DB state から実際の projection content を再導出するための
  `desired_revision` marker の read-only snapshot。
- `ack_projection(key, read_revision)`: `DELETE ... WHERE projection_key =
  ? AND desired_revision = ?` — caller が実際に読んだ revision をキーに
  した **conditional** delete。caller の `flush_projection` read と
  `ack_projection` の間に別の `enqueue_projection` が revision を進めて
  いた場合、`DELETE` は 0 行にマッチする（その行の `desired_revision` は
  既に `read_revision` より大きい）ため、新しい revision は失われない。
  `tests/task-context/test_projection_outbox.py::test_given_race_between_flush_read_and_ack_when_enqueue_advances_revision_then_ack_does_not_delete_it`
  で検証。

## `synchronous=NORMAL` vs `synchronous=FULL`（AC10） — 同期モードの比較

AC10 は 2 つの **別々に検証される** ものを要求する:

**(a) Deterministic correctness/concurrency test**（CI-gated、pytest、
timing assertion を含まないため構造的に flaky にならない）:
`tests/task-context/test_synchronous_mode_correctness.py` は、
`synchronous=NORMAL`・`synchronous=FULL` の両方で、committed data が
close+reopen cycle と明示的な `PRAGMA wal_checkpoint(TRUNCATE)` を跨いで
survive することを assert する。migration/transaction/constraint/
`TEMPORARILY_UNAVAILABLE` の correctness は、前述の他の test module で
default の `synchronous=NORMAL` の下で cover されている。

**(b) 実際の WSL2 実測 latency evidence**（本ドキュメントに記録、CI の
pass/fail gate には **しない** — `measure_synchronous_latency.py` の
module docstring 参照）:

環境: WSL2、`Linux 6.18.33.2-microsoft-standard-WSL2`、Ubuntu 24.04.4
LTS、Python 3.12.3、本リポジトリの filesystem（WSL2 VM 内の ext4、
Windows drive の `/mnt/c` mount ではない）。
`uv run python3 scripts/task-context/measure_synchronous_latency.py`
（mode ごとに通常 commit 200 回 + 「50 行書き込んで WAL を再度太らせてから
`PRAGMA wal_checkpoint(TRUNCATE)` **そのものの呼び出し時間**を計測」を
30 trial）を独立に 2 回実行（fix_delta finding 8 — 計測方法の修正:
以前の版は `wal_checkpoint(TRUNCATE)` を **計測開始前に完了** させてから
次の commit を計測しており、checkpoint 自体のコストがタイマーの外に
出てしまっていた。現在の版は checkpoint operation 呼び出しそのものを
計測するため、"何を測っているか" と "実際に測っているもの" が一致する）:

| Mode | Metric | Run 1 (mean / p95 / max, ms) | Run 2 (mean / p95 / max, ms) |
|---|---|---|---|
| `NORMAL` | 通常 commit | 0.007 / 0.015 / 0.061 | 0.006 / 0.007 / 0.029 |
| `NORMAL` | checkpoint operation（`wal_checkpoint(TRUNCATE)` 呼び出し自体） | 4.580 / 5.977 / 6.824 | 4.327 / 5.200 / 6.013 |
| `FULL`   | 通常 commit | 2.750 / 3.132 / 14.665 | 2.650 / 3.142 / 4.426 |
| `FULL`   | checkpoint operation（`wal_checkpoint(TRUNCATE)` 呼び出し自体） | 2.930 / 3.970 / 3.985 | 2.907 / 3.837 / 3.988 |

解釈: WAL mode の `synchronous=NORMAL` では commit ごとに `fsync` が
発生しない（checkpoint operation 実行時にのみ発生する）ため、通常の
hot-path commit（`UserPromptSubmit` 等）は `synchronous=FULL`（すべての
commit で `fsync` する）に対して **数百倍高速**（約0.006–0.007ms vs
約2.6–2.8ms、本 host）になる。`NORMAL` のコストは代わりに checkpoint
operation 実行時にまとめて発生する（ここでは約4.3–4.6ms、`FULL` の
checkpoint operation 自体はこれより若干安い約2.9ms 程度 — `FULL` は
各 commit で既に fsync 済みのため checkpoint 時に追加で fsync すべき
差分が少ないことと整合する）が、これは全 hot-path write ではなく
少数の checkpoint 呼び出しが負担するコストであり、observed worst case
（最大約6.8ms）でも 200ms の busy_timeout budget に十分収まる。

### 採用した決定

**`synchronous=NORMAL` を primary hot-path 設定として採用する**
（`task_context_db.DEFAULT_SYNCHRONOUS = "NORMAL"`）。これは Issue 自身が
"first candidate" として位置づけていることと整合する。上記の実測 evidence
がこれを支持する: typical-case latency の win（本 host で約400倍前後）は
2 回の実測 run を通じて大きく一貫しており、worst-case の checkpoint
operation latency も一桁 ms 台の小さな値に留まり（multi-second hot-path
blocking には程遠い）、
[SQLite 自身のドキュメント](https://www.sqlite.org/pragma.html#pragma_synchronous)
は WAL mode における `synchronous=NORMAL` がアプリケーションクラッシュに
対しては安全であり、リスクがあるのは *power loss/OS crash*（application
bug ではない）時に最新の transaction を失う可能性のみだと述べている —
これは、GitHub/Git 自体から常に re-derive 可能な local control-plane cache
にとって許容可能なリスクである。`synchronous=FULL` は、より強い保証が
必要な将来の specific write path のために明示的な override として利用
可能（`task_context_db.connect(..., synchronous="FULL")`）のままとする。

### DB boundary での corruption typed translation（fix_delta finding 6）

corruption が typed `CorruptDatabaseError`（silent reset ではなく）として
必ず検出されるのは `connect()`/`migrate()` が走るタイミング（`task-contextctl`
の各 invocation は常に open+migrate を経て dispatch するため、これは毎回
発生する）だけではない。以前は、既に current な `user_version` にある DB
（`migrate()` の早期 no-op return が `PRAGMA integrity_check` 自体に到達
しない経路）が最後の successful open の *後に* corrupt した場合、破損した
page に最初に触れる service-layer の plain read（例: `get_task`）が生の
`sqlite3.DatabaseError` を raise し、CLI の generic exception handler に
捕捉されて `CORRUPT_DATABASE` ではなく `INTERNAL_ERROR` として報告されて
いた。

これを閉じるため、`task_context_db.execute_readonly()` という DB
boundary wrapper を導入し、`task_context_service` の全ての read-only
lookup（`get_task`/`get_activity`/`get_binding`/`get_execution_run`/
`get_current_location`/`read_projection`/... 等）をこの wrapper 経由に
した。`write_transaction()` にも同様に、`IntegrityError`（→
`ConflictError`）でも locked/busy `OperationalError`（→
`TemporarilyUnavailableError`）でもない `sqlite3.DatabaseError` を
`CorruptDatabaseError` へ翻訳する分岐を追加した。これにより、hot-path の
毎 open で `PRAGMA integrity_check`（full-DB scan）を eager に走らせる
ことなく、実際に corruption に触れた最初の DB 操作が常に typed
`CorruptDatabaseError` として報告されるようになった
（`tests/task-context/test_schema_and_migration.py` の
`test_given_readonly_execute_when_database_error_raised_then_translated_to_corrupt_database_error`、
`test_given_service_get_task_when_underlying_read_hits_database_error_then_corrupt_database_error_not_internal`、
`test_given_write_transaction_when_database_error_raised_mid_transaction_then_corrupt_database_error_and_rollback`
参照）。

## Repository CI に関する注記（non-blocking）

`tests/task-context/` は本 Issue で追加された新規 pytest target
directory である。`.github/ci/python-test-plan.json`（`python-test` CI job
が consume する repository-wide pytest target-set の SSOT）の `targets`
への `tests/task-context/` 登録は、PR #2588 の Owner レビューで承認された
Scope Delta（Issue #2563 Allowed Paths に明記済み: `targets` への当該行
追加のみに限定し、他の `targets`/`ignore`/`deselect`/xdist 設定は変更
しない）として実施済みである。Issue 自身の Verification Command である
`uv run --locked pytest tests/task-context -q` はローカルで pass し、この
登録はその CI 常設ゲート化（`python-test` job の
`uncovered_changed_test_files` gate 対応）を目的とする。
