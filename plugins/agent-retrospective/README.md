# agent-retrospective（plugin 配布版）

継続的 retrospective の proposal-only orchestrator を、`.claude/` を持たない任意の repository でも
`claude --plugin-dir` 経由で実行可能にする、self-contained な Claude Code plugin distribution。

`.claude/skills/agent-retrospective/`（loop-protocol の project Skill 版）のポータブル版であり、
project Skill 本体はこの plugin では変更していない（Issue #2240 Out of Scope）。project Skill 版が
前提とする AGY role adapter（`gemini-cli-headless-delegation` skill 経由の外部 CLI 委譲）、Latitude
CLI enrichment、Claude-GPT `transport_log.py` には一切依存しない。この plugin の `codebase-
investigator`/`web-researcher` は Read/Grep/Glob・native WebSearch/WebFetch のみに依存する軽量な
独立実装として再設計されている。

## 構成

```text
plugins/agent-retrospective/
├── .claude-plugin/
│   └── plugin.json          # plugin manifest（`.claude-plugin/` 配下にはこの manifest のみを置く）
├── skills/
│   └── run/
│       ├── SKILL.md          # orchestrator の手順書（/agent-retrospective:run で起動）
│       ├── references/
│       ├── schemas/          # observer/evaluator/candidate の JSON Schema（バンドル済み）
│       └── scripts/          # run_retrospective.py 他、実行に必要な Python script 一式
├── agents/                   # 4 Agent（frontmatter は下記「未対応フィールド」参照）
├── pyproject.toml            # Python dependency closure（jsonschema のみ）
├── uv.lock
└── README.md                 # 本ファイル
```

`.claude-plugin/` 配下にコンポーネント（`skills/`/`agents/`/`hooks/`/`.mcp.json` 等）を置くことは
公式ドキュメントで common mistake として明記されている -- 本 plugin ではコンポーネントはすべて
`.claude-plugin/` の兄弟として plugin root 直下に配置している。

## インストールと起動

```bash
claude --plugin-dir /path/to/plugins/agent-retrospective
```

起動後、plugin 内の Skill は namespaced invocation `/agent-retrospective:run` で明示起動する
（`disable-model-invocation: true` のため自動起動しない）。実行本体は単一の Bash 呼び出しで
`skills/run/scripts/run_retrospective.py` を起動する（詳細は `skills/run/SKILL.md` を参照）。

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" --locked python3 \
  "${CLAUDE_PLUGIN_ROOT}/skills/run/scripts/run_retrospective.py" \
  --repo-root "${CLAUDE_PROJECT_DIR}" \
  --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
  --state-backend fixture
```

- `${CLAUDE_PLUGIN_ROOT}`: この plugin 自身のインストール先（bundled asset -- スクリプト・schema・
  Agent 定義 -- の解決に使う）
- `${CLAUDE_PROJECT_DIR}`: 解析対象 repository（呼び出し元セッションが現在アタッチしている project）
- `${CLAUDE_PLUGIN_DATA}`: 本 plugin は永続データを書き込まない（proposal-only、artifact は
  run-scoped temp dir へのみ書き込み、run 終了時に必ず削除する）ため使用しない

`--repository-id`（省略時は `git remote get-url origin` から自動導出）・`--target-issue`（省略時は
issue-less run）・`--request-id`/`--idempotency-key`（省略時は UUID 自動生成）はすべて任意である。

## Plugin Agent frontmatter の未対応フィールド

**対象範囲の正確な区別（Issue #2240 AC3）**: plugin **自体**（`.claude-plugin/plugin.json` の
兄弟として plugin root 直下に置く）は `hooks/hooks.json` や `.mcp.json` を持てる -- plugin レベルの
hooks/MCP server 定義はサポート対象である。**未対応なのは、個々の Agent frontmatter（`agents/*.md`
の YAML frontmatter）内の `hooks` / `mcpServers` / `permissionMode` の 3 フィールドのみ**である。
plugin-shipped Agent の frontmatter にこれら 3 フィールドを書いても、Claude Code の Agent
frontmatter パーサはこれらを認識しない（`claude plugin validate --strict` はこれらのキーの有無を
理由に fail しないが、これは「サポートされている」ことを意味しない -- 単に validator がこの制約を
静的に検証しないだけである）。

この理由により、本 plugin の 4 Agent 定義ファイル（`agents/retrospective-runtime-observer.md`、
`agents/retrospective-evaluator.md`、`agents/codebase-investigator.md`、`agents/web-researcher.md`）
の frontmatter は、コピー元（project Skill 版の `.claude/agents/*.md`）が持っていた `hooks: {}` /
`mcpServers: {}` / `permissionMode: dontAsk` を**意図的に含めていない**。

### mutation protection はこれらのフィールドに依存しない

本 plugin の proposal-only mutation boundary（`docs/adr/0007-agent-retrospective-boundaries.md`
参照）は、以下の独立した機構だけで担保されている（`hooks`/`mcpServers`/`permissionMode` の
どれにも依存しない）:

1. **`tools`/`disallowedTools`**（公式 frontmatter フィールド、plugin-shipped Agent でもサポート
   対象）: `retrospective-runtime-observer`/`retrospective-evaluator` は `tools: []` かつ
   `disallowedTools` に `Agent`/`Skill`/`Write`/`Edit`/`MultiEdit`/`NotebookEdit`/`Bash`/
   `WebFetch`/`WebSearch` を全指定する leaf。`codebase-investigator` は `Read`/`Grep`/`Glob` のみ、
   `web-researcher` は `WebSearch`/`WebFetch` のみを許可し、他はすべて `disallowedTools`。4 Agent
   すべてで `Bash`/`Write`/`Edit`/`MultiEdit`/`Agent`/`Skill` のいずれかの mutation 経路が技術的に
   ブロックされている。
2. **`run_retrospective.py` 自体が GitHub/Issue mutation コードパスを一切持たない**: `finalize()`
   phase は proposal-only `PublishRequest` を返す純関数であり、I/O・ネットワーク呼び出し・
   filesystem 書き込みを一切行わない（`skills/run/scripts/run_retrospective.py` の `finalize()`
   docstring 参照）。
3. **`DelegatedAgentPermissionPolicy`**: 実際の subprocess env（mutation credential を除去した
   allowlist）と argv（`--disallowedTools`）へ直接反映される実行時ガードレール。`git commit`/
   `git push`/`gh issue`/`gh pr`/filesystem write/対象 run 外セッション resume を拒否する。

新規の security/permission harness（plugin 側専用の独自 sandbox・独自 Bash allowlist 再構築等）は
この plugin に追加していない -- mutation protection は上記 3 機構と、公式 frontmatter フィールド
だけで完結している。

## Python dependency closure（Python 依存関係クロージャ）

`pyproject.toml`/`uv.lock` は `jsonschema` のみを依存として固定する。`uv run --project
"${CLAUDE_PLUGIN_ROOT}" --locked` で解決されるこの closure は、host repository（`.claude/` を持つ
project）側の `pyproject.toml`/`uv.lock` から独立している -- `.claude/` を持たない repository へ
インストールしても、この plugin 自身の依存解決だけで動作する。

## base_sha resolution（default branch 名を決め打ちしない）

`skills/run/scripts/run_retrospective.py` の base_sha resolver は `git rev-parse main` を決め打ち
しない。既定は `git rev-parse HEAD`（snapshot authority として現在の checkout を使う）であり、
呼び出し側は `--base-ref` で明示的に別の ref を指定できる。`skills/run/scripts/
clean_install_smoke.sh`（Issue #2240 AC5）は default branch 名が `main` ではない一時 repository
（`git init -b portability-smoke`）でこの smoke を実施し、この portability defect を隠さないことを
確認する。

## Marketplace / semver / rollback（Out of Scope、対象範囲外）

Marketplace への公開申請・審査対応、明示的な semver version フィールドの必須化・version bump 運用
手順の docs 化、update/rollback 専用 machinery、`clean_install_smoke_receipt.json` 等の証跡ファイル
の commit は本 plugin のスコープに含めない（Issue #2240 Out of Scope）。`plugin.json` の `version`
フィールドは `claude plugin validate --strict` の警告を避けるために設定してあるが（Claude Code
仕様上 `version` は optional）、semver 運用ポリシーそのものは未確定であり、rollback が必要になった
場合は既知の正常 commit SHA に pin して再インストールする方針で足りるとしている。
