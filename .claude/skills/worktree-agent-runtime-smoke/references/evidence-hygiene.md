# Evidence Hygiene Reference

## 保存先

`<worktree>/artifacts/runtime-smoke/<run-id>/` に保存する（worktree-local untracked
directory。schema、receipt、digest、publisher、approval authority ではない）。
`--output-dir` は排他的作成（既存ディレクトリ・symlink は拒否）とする。

```text
summary.md   # 唯一の永続 evidence ファイル。allowlist-only、redacted。
```

raw native event dump（`native-events.jsonl`）、raw pane transcript
（`pane-output.txt`）、`herdr agent explain` の生 JSON（`agent-detection.json`）、
session-log metadata の raw dump（`session-log-metadata.txt`）は保存しない
（PR #1921 human OWNER fix-delta: 先行 PR #1864 で pane transcript にアカウント情報等が
残留した実績があるため、summary.md への allowlist-only 集約へ縮小した）。

## Main-session agent identity evidence の status enum（識別子証跡の状態列挙、Issue #2046）

`main_agent_identity` / `agent_definition` / `skill_evidence` / `mutation_boundary` /
`settings_provenance` の各 status フィールドは、必ず以下 4 値のいずれかを取る:

- `declared`: static な自己構成事実（frontmatter 宣言、この runner 自身が決定論的に
  生成した session-local payload など）。runtime observation ではない
- `observed`: native stream-json event（hook payload、tool_use/tool_result ペア）から
  直接観測した事実
- `derived_from_observed`: 他の observed evidence から間接的に導出した事実
- `unavailable`: 上記いずれも成立しない場合。値を推測・捏造しない

`skill_evidence.preload` は常に `unavailable`（Skill preload を直接確認する native
event channel が存在しないため）。これを `observed` に昇格させることは禁止 — declared
な事実（`skill_evidence.declaration`）や observed な事実（`skill_evidence.canonical_read`）
を preload の代わりに使わない。

## `summary.md` に保存可能な証跡

- runtime 名と version
- 検証対象の tested HEAD
- repo-relative な worktree のパス
- 対象 branch 名
- 実行 lane（`direct` または `herdr_isolated_session`）
- process exit code
- isolated herdr session 名／pane ID／agent name（識別子のみ。transcript は含まない）
- observed lifecycle state（観測された状態）
- 検出された agent kind／confidence（`herdr agent explain` から抽出した 2 フィールドのみ）
- native event の件数・terminal event の有無
- caller 指定の expected marker の有無
- filesystem／Git postcondition（事後条件）の差分一覧
- isolated session cleanup の試行有無・消失確認可否
- session-log metadata の allowlist キー該当件数（値そのものは含まない）

## Main-session agent identity evidence として保存可能な追加証跡（Issue #2046）

- main-session の requested/observed agent identity（persona 名のみ。生の hook payload
  本体は含まない）
- candidate Agent definition の repo-relative path・sha256 digest（hermetic lane では
  生成 payload の digest も）
- Skill declaration/canonical Read の status・normalized path・sha256 digest・
  tool_use_id（Read tool の input/tool_result 本体テキストは含まない）
- mutation-capable tool_use event の tool 名件数・一覧（command 本体・prompt 本体は
  含まない）
- 実効 argv（redaction 済み。絶対パス・長い base64-like token は `<redacted>`）
- settings source・digest（settings 本体の内容は含まない）

## 既定で保存しないもの

- raw prompt 全文
- raw transcript 全文（native event 本体、pane 出力本体）
- reasoning
- tool output 全文
- credential
- environment dump
- 認証情報
- HOME を含む絶対パス
- native session log の worktree への複製

## Redaction 規則

- 絶対パス（`/home/*`、`/root/*`、`/Users/*` 等）は `<redacted>` へ置換する
- 40 文字以上の base64-like token は `<redacted>` へ置換する
- `summary.md` に含める process エラーメッセージ（stderr 抜粋）は上記 redaction を必ず通す

## Session-log metadata boundary（session-log metadata の扱いの境界。#1887 Design Decision 5）

- structured event または herdr output で判定可能な case では session log を必須にしない
- caller が明示的に要求した場合のみ metadata を読み取る（`--inspect-session-log-metadata`）
- allowlist metadata キー（`type`、`event`、`role`、`subagent`、`label`、`timestamp`、`ts`）の
  該当有無だけをカウントし、`summary.md` へは件数のみを記録する。値そのもの・`cwd`・
  `session_id` は抽出・保存しない（PR #1921 P1 fix-delta: 過度に許容的な allowlist を縮小）
- `reasoning`、raw prompt、tool output 全文は allowlist に含まれず抽出しない
- undocumented な log path または record shape を stable schema として扱わない
- log が見つからないことを runtime 成功・失敗へ自動変換しない
- `--require-session-log-metadata` が指定された case だけ、取得不能を exit 77（SKIP）とする
