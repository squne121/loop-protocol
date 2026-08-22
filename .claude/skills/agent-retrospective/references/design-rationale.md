# 設計判断の根拠（Issue #2237）

## orchestration owner の一意化

`SKILL.md` と `run_retrospective.py` の二重所有を解消し、`orchestration_owner` を root Skill
（main conversation）に一意化した。`run_retrospective.py` は `Agent` tool を一切呼ばない
deterministic phase engine としてのみ動作する。理由: 二重所有は「どちらが実際に Agent を起動する
権限を持つか」を曖昧にし、leaf 制約の enforcement 境界を不明瞭にする（OWNER review #2、P0-1）。

## production Agent invocation 経路（headless CLI subprocess）

現行 `pyproject.toml` は `pyyaml`/`jsonschema` のみに依存し、Claude Agent SDK 依存を追加すると
Allowed Paths 外（`pyproject.toml`/lockfile）への変更が必要になる。既存 Allowed Paths を維持できる
`claude -p --output-format json --json-schema <schema>` の subprocess 経路を採用した
（`invoke_agent`/`build_agent_invocation_argv`）。`--bare`（API key 必須）は採用せず、既存の
Claude Code subscription login を前提とする。

## PreviousStateProvider を read-only port として先に固定する理由

delta 算出（`new`/`resolved`/`recurrent`/`regressed`/`unchanged`）の責務は本 Issue が持つが、
実際の永続化読み取り（optimistic concurrency 含む）は #2238 の責務である。両者が同じ port を
実装できるよう、本 Issue では `PreviousStateProvider` の型・5 状態を fixture/in-memory provider
（`FixturePreviousStateProvider`）として先に固定した。

## PublishRequest を proposal-only envelope にする理由

`run_retrospective.py` は GitHub/Issue へのいかなる mutation も実行しない。人間承認は
`PublishRequest` 自体には含まれない別の trusted channel（`HUMAN_AUTHORIZATION_RECEIPT_V1`、#2238）
で供給される。`authorized`/`authorized_by_human`/`authorization_token`/`mutation_capability` を
schema レベルで禁止することで、mutation authority が誤ってこの envelope に紛れ込む余地を構造的に
なくした。
