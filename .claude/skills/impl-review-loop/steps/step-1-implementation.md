### Step 1: 実装（`implementation-worker` SubAgent）

`implement-issue` スキルに従って実装を委任し、PR 作成は `open-pr` への委譲経路を使う。

#### 直接実装可能条件（オーケストレータによる直接実装）

以下の **3 条件がすべて** 満たされる場合、オーケストレータは `implementation-worker` SubAgent への委譲を省略し、自身で直接実装してよい:

| 条件 | 閾値 |
|------|------|
| 変更行数（追加・削除の合計） | ≤ 20 行 |
| 変更ファイル数 | ≤ 2 件 |
| 新規 API / contract 変更 | なし（既存インタフェースへの追記・修正のみ） |

**いずれか 1 つでも条件を満たさない場合は、`implementation-worker` SubAgent に委譲すること。**

直接実装を選択した場合も、worktree の作成・branch 切り出し・commit・push・PR 作成は本 Skill の通常手順に従う。

**SubAgent への必須渡し情報:**
- Issue番号
- Issue contract（Outcome, AC, Allowed Paths, Verification Commands）
- 「git push および PR 作成は事前承認済みである（オーケストラレータから明示承認）」
- `publish: yes`（push/PR 作成の許可）
- PR 作成は `open-pr` の正本手順（publish gate / template guard / idempotency / Closes/Refs 判定）を使うこと
- `expected_branch`: 事前準備 Step 3 で確定した branch 名（例: `feat/issue-<N>-<slug>`）
- `expected_worktree_path`: 事前準備 Step 3 で確定した worktree の絶対パス
- `canonical_repo_root`: canonical repo root の絶対パス。`git worktree list --porcelain` membership と `git-common-dir` 照合に使う
- `canonical_pr_url`: 事前準備で確定した canonical PR URL（なければ `null`）
- `canonical_pr_source`: `exact-branch|same-issue-open-pr|caller-specified|repair-replacement|new-pr`
- `superseded_prs`: close / destination mapping 対象の PR 一覧
- `repair_context`: `reason`, `previous_pr_url`, `mode=create-replacement|reuse-existing`
- drift capture contract: `git reflog --date=iso --all`, `git worktree list`, `git status --short --branch`, current head / expected head / branch, detached verify head switch decision
- `model` / `model_reasoning_effort`: `model_overrides["implementation-worker"]` に指定がある場合は、その値を CodexCLI SubAgent 委譲プロンプトへ明示して渡す。未指定時は `.codex/agents/implementation-worker.toml` の role-level pin を既定として扱う
- スキル編集時は `skill-creator` 相当の観点（progressive disclosure / validation integrity / bundled resources）を参照するよう明示する

**委任プロンプト例（iteration 1）:**
```
Issue #<N> を `implement-issue` スキルに従って実装し、Draft PR を作成してください。
git push および PR 作成はオーケストラレータから事前承認済みです（publish: yes）。
人間承認の根拠: ユーザーが `/impl-review-loop <N>` を実行し、実装を承認済みです。
CodexCLI SubAgent 指定:
  model: <model_overrides["implementation-worker"].model>
  model_reasoning_effort: <model_overrides["implementation-worker"].model_reasoning_effort>
  ※未指定時は `.codex/agents/implementation-worker.toml` の role-level pin を使用

## 作業開始前の必須確認（worktree 外作業禁止ガード）
作業開始前に必ず以下を確認してください:
\`\`\`bash
pwd
git rev-parse --show-toplevel
git branch --show-current
git worktree list
\`\`\`
- main worktree（リポジトリルート）で実行中を検知した場合は**即時停止**してオーケストラレータに報告する
- `git branch --show-current` が `expected_branch` と一致しない場合も**即時停止**する
- `git rev-parse --show-toplevel` が `expected_worktree_path` と一致しない場合も**即時停止**する
- `git -C "$canonical_repo_root" worktree list --porcelain` に `expected_worktree_path` が存在しない場合も**即時停止**する
- `git rev-parse --git-common-dir` が canonical repo と一致しない場合も**即時停止**する
- `origin/main`（なければ `main`）と共通 merge-base を持たない場合は `unexpected init commit ancestry` として**即時停止**する
expected branch 名: feat/issue-<N>-<slug>
expected worktree path: /path/to/repo/wip/worktree-issue-<N>-<slug>
canonical repo root: /path/to/repo
canonical_pr_url: <canonical PR URL or null>
canonical_pr_source: <exact-branch|same-issue-open-pr|caller-specified|repair-replacement|new-pr>
superseded_prs:
  - <PR URL or none>
repair_context:
  reason: <reason or none>
  previous_pr_url: <PR URL or none>
  mode: <create-replacement|reuse-existing|none>
worktree / branch drift を検知した場合は、停止前に以下を必ず回収してください:
- `git reflog --date=iso --all`
- `git worktree list`
- `git status --short --branch`
- current head / expected head / branch
- git top-level / expected worktree path
- `detached verify head switch decision`
また、`unexpected init commit ancestry`, `outside.txt`, `wip/demo/module.py` のような drift signature が見えたら handoff artifact に残し、`#1948 / PR #1977` と `#1978` のスコープ外修正へ広げないこと。
PR 本文は `.github/PULL_REQUEST_TEMPLATE.md` の形式に従うこと。必須セクション:
- `## Linked Issue` に `Closes #<Issue番号>` を記載すること
- `## Acceptance Criteria -> Evidence` に各 AC と対応する Evidence を記載すること
- `## Commands Run` に実行コマンドと数値終了コードを記載すること
- `## Follow-ups Intentionally Deferred` / `## Knowledge Harvesting` / `## Long-form Evidence` を省略しないこと
スキル編集時は `skill-creator` 相当の観点（progressive disclosure / validation integrity / bundled resources）を適用し、過剰説明を避けること。
PR 作成は `open-pr` スキルへ委譲し、`open-pr` の出力である `PR_URL=<url>`、`CANONICAL_PR_URL=<url>`、`CANONICAL_PR_SOURCE=<source>`、`SUPERSEDED_PR_URL=<url or none>` を必ず回収して報告すること。
Issue contract:
[Outcome / AC / Allowed Paths / Verification Commands をインライン展開]
```

**委任プロンプト例（iteration ≥ 2、ループ継続時）:**
```
Issue #<N> を `implement-issue` スキルに従って修正を実装してください。
git push はオーケストラレータから事前承認済みです（publish: yes）。
人間承認の根拠: ユーザーが `/impl-review-loop <N>` を実行し、実装を承認済みです。
CodexCLI SubAgent 指定:
  model: <model_overrides["implementation-worker"].model>
  model_reasoning_effort: <model_overrides["implementation-worker"].model_reasoning_effort>
  ※未指定時は `.codex/agents/implementation-worker.toml` の role-level pin を使用

## 作業開始前の必須確認（worktree 外作業禁止ガード）
作業開始前に必ず以下を確認してください:
\`\`\`bash
pwd
git rev-parse --show-toplevel
git branch --show-current
git worktree list
\`\`\`
- main worktree（リポジトリルート）で実行中を検知した場合は**即時停止**してオーケストラレータに報告する
- `git branch --show-current` が `expected_branch` と一致しない場合も**即時停止**する
- `git rev-parse --show-toplevel` が `expected_worktree_path` と一致しない場合も**即時停止**する
- `git -C "$canonical_repo_root" worktree list --porcelain` に `expected_worktree_path` が存在しない場合も**即時停止**する
- `git rev-parse --git-common-dir` が canonical repo と一致しない場合も**即時停止**する
- `origin/main`（なければ `main`）と共通 merge-base を持たない場合は `unexpected init commit ancestry` として**即時停止**する
expected branch 名: feat/issue-<N>-<slug>
expected worktree path: /path/to/repo/wip/worktree-issue-<N>-<slug>
canonical repo root: /path/to/repo
canonical_pr_url: <canonical PR URL>
canonical_pr_source: <exact-branch|same-issue-open-pr|caller-specified|repair-replacement|new-pr>
superseded_prs:
  - <PR URL or none>
repair_context:
  reason: <reason or none>
  previous_pr_url: <PR URL or none>
  mode: <create-replacement|reuse-existing|none>
worktree / branch drift を検知した場合は、停止前に以下を必ず回収してください:
- `git reflog --date=iso --all`
- `git worktree list`
- `git status --short --branch`
- current head / expected head / branch
- git top-level / expected worktree path
- `detached verify head switch decision`
また、`unexpected init commit ancestry`, `outside.txt`, `wip/demo/module.py` のような drift signature が見えたら handoff artifact に残し、`#1948 / PR #1977` と `#1978` のスコープ外修正へ広げないこと。
新規 PR を作成しないこと（重複 PR 禁止）。既存 Draft PR <PR_URL> に追加コミットを push してください。
修正後にファイル全体を読み返して構造的一貫性を確認すること。
Issue contract:
[Outcome / AC / Allowed Paths / Verification Commands をインライン展開]
前回フィードバック:
[フィードバック内容をインライン展開]
前回の handoff 正本:
  handoff_artifact: <前回 Feedback コメント URL>
  supersedes: <前々回の Feedback コメント URL or none>
再依頼 ledger:
  agent_thread_reuse: true
  previous_agent_id_or_task: <前回の agent id または task 名>
  reuse_method: send_input
  previous_findings:
    - <前回の主要指摘>
  fix_delta:
    - <今回適用する修正差分>
  handoff_artifact: <前回 Feedback コメント URL>
上記コメント URL の指摘内容を重点的に参照し、修正を実施すること。
スキル編集時は `skill-creator` 相当の観点（progressive disclosure / validation integrity / bundled resources）を適用し、過剰説明を避けること。
```

**SubAgent 停止・失敗時の repair / redelegate 手順（直接実装禁止）:**

`implementation-worker` SubAgent がレート制限（エラーまたは空レスポンス）に達した場合、オーケストラレータは以下を実施する:

1. 停止理由を分類する（rate limit / handoff 欠落 / worktree mismatch / thread closed）。
2. `send_input` が可能なら前回 thread に再依頼する（`agent_thread_reuse: true`, `reuse_method: send_input`）。
3. thread が closed なら `resume_agent` を実施してから `send_input` する（`reuse_method: resume_agent + send_input`）。
4. thread 再利用が不可能なら新規 `implementation-worker` を起動する（`reuse_method: new_agent`）。
5. 2〜4 が成立しない場合のみ停止し、LOOP_STATE と Issue comment に停止理由を記録する。停止時は `git reflog --date=iso --all`, `git worktree list`, `git status --short --branch`, current head / expected head / branch, `detached verify head switch decision` を含む canonical evidence bundle を handoff artifact に残す。**オーケストラレータが直接実装を代行してはならない**。

完了条件: `open-pr` の出力から `PR_URL`, `CANONICAL_PR_URL`, `CANONICAL_PR_SOURCE` が取得できること。replacement PR 作成時は `SUPERSEDED_PR_URL` も回収すること。

LOOP_STATE を更新する:
```bash
gh issue comment <Issue番号> --body "$(cat <<'EOF'
## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: implemented
status: running
pr_url: <PR_URL>
last_verdict: null
canonical_pr_url: <CANONICAL_PR_URL>
canonical_pr_source: <CANONICAL_PR_SOURCE>
superseded_prs:
  - <SUPERSEDED_PR_URL or none>
\`\`\`
EOF
)"
```
