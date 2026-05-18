---
name: implement-issue
description: 承認済みの implementation child issue を実装するときに使う。Issue contract と Allowed Paths を再確認し、verify、PR 作成、Issue comment への結果返却までを `1 Issue = 1 PR` で進める。
required_rules:
  - github-ops-workflow
  - issueops-common-guard
  - git-policy
  - file-edit-protocol
  - skill-sync-policy
  - issue-uncertainty-policy
  - skill-rule-boundary
---

# Implement Issue

承認済み contract に従って、implementation child issue を実装し、verify、PR、Issue 更新まで進める skill。

## Input

- `Issue番号` または `Issue URL`（必須）
- `issue-contract-review` の contract snapshot comment URL（必須）
- `proposal_only` draft delegation（任意）: Gemini CLI に下書きだけを委譲する場合は `tool_profile: proposal_only` と `output_sections: ["implementation_draft"]` を使う

## Use When

- `issue-contract-review` が完了し、人間が Go を返した後
- implementation child issue を Allowed Paths 内で実装したい
- PR 本文と linked issue comment を primary surface にしたい
- 「Issue ◯◯ 実装して」「implement issue」「この Issue やって」などの短文トリガー

## 責務範囲

- **実装 + verify + PR 作成**: Issue contract の AC を満たすコード変更、検証コマンド実行、Draft PR 作成
- **conflict resolve**: pr-reviewer から `LOOP_VERDICT: REQUEST_CHANGES + blockers: [merge_conflict]` を受け取った場合、`impl-review-loop` SKILL.md の CONFLICTING PR Escalation Runbook に従って resolve する
- **上記以外の責務委譲**: 検知（mergeable 状態検出）は test-runner、判断（AC 充足）は pr-reviewer、オーケストレーション（state tracking）は impl-review-loop に委譲

## Procedure

### Step 0: Rules Loading Preflight

orchestrator（または main conversation）は SubAgent 委譲前に以下の preflight を実行し、rules を context に inline 注入する。冪等マーカー `<active_rules ...>` により重複読込を防止する。

**実行手順:**

1. 本ファイル（`implement-issue/SKILL.md`）の frontmatter `required_rules:` を正本として rule-id を確認する。ハードコードリストではなく frontmatter を参照することで、`required_rules:` 変更時に手順側が自動的に追従する。

2. frontmatter `required_rules:` に列挙された rule-id を収集し、重複を除いた「現在の active rule set」を確定する。

3. **冪等チェック**: ローカルコンテキストの `<active_rules ...>` マーカーを確認する。
   - 既に同 rule-id が記録されている場合 → 再 Read しない（冪等性保証）
   - 未記録の rule-id がある場合 → `.agents/rules/<id>.md` を Read して context に追加する

4. 確定した rule-id セットをコンテキストに記録する:
   ```
   <active_rules github-ops-workflow, issueops-common-guard, git-policy, file-edit-protocol, skill-sync-policy, issue-uncertainty-policy, skill-rule-boundary>
   ```
   マーカー書式: `<active_rules id1, id2, id3>` （カンマ + スペース区切り、rule-id は `[a-z0-9_-]+` 形式）
   検出 regex: `<active_rules ([a-z0-9_-]+(?:, [a-z0-9_-]+)*)>`

5. **`implementation-worker` SubAgent 委譲時の inline 注入**: `implementation-worker` へ Agent tool で委譲する際、`prompt` の冒頭ブロックに以下を inline 展開する:
   ```
   <active_rules github-ops-workflow, issueops-common-guard, git-policy, ...>

   <rule id="github-ops-workflow">
   （.agents/rules/github-ops-workflow.md の本文）
   </rule>

   （解決した全 rule の本文を同様に展開）
   ```
   SubAgent 側からは `.agents/rules/*.md` を自律的に Read しない（既に注入済みのため）。

---

1. Issue contract を再確認する:
   - `Outcome`
   - `Acceptance Criteria`
   - `Verification Commands`
   - `Allowed Paths`
   - `Required Skills`（runtime dependency のみ列挙されているか確認する。`issue-contract-review` / `implement-issue` / `pr-review-judge` が列挙されていても停止せず、それらは暗黙的に適用されるワークフロースキルと扱う）
   - 最新の human answer
   - 最新の AI handoff
1.2. 不確実性識別子を確認する:
   - Issue の title / body / labels に `state/needs-investigation` または `調査:` があれば、`.agents/rules/issue-uncertainty-policy.md` を Read する
   - 識別子が残っている間は実装を開始せず、人間の解除または再分類を待つ
1.5. Issue 本文の Rules セクションを読む（存在する場合）:
   - Issue 本文に `## Rules` セクションがあれば、列挙された `.agents/rules/<file>` をすべて Read する
   - 実装時に遵守すべき制約・判断基準を把握する
1.6. 不確実性識別子を確認する:
   - Issue の現在の title prefix / labels に `実装:` prefix + `phase/implementation` + `state/queued` の canonical ready tuple がすべて揃っている場合のみ ready state を正本として扱い、残っている uncertainty alias は stale metadata とみなす
   - それ以外で、現在の title prefix / labels に `調査:` `phase/research` `人間確認:` `state/needs-human` `state/needs-investigation` のいずれかがあれば、未解決の不確実性があるサインとして扱う
   - `phase/research` は事実確認待ち、`state/needs-human` は人間判断待ち、`state/needs-investigation` は移行期の互換 alias として扱い、どれかが残っている Issue の実装は開始しない
   - 実装へ進めるのは、不確実性が解消されて `実装:` prefix + `phase/implementation` + `state/queued` の canonical ready tuple に揃った時だけとする
1.7. `proposal_only` で実装下書きを受ける場合の caller-side 境界を固定する:
   - Gemini CLI に下書きだけを委譲したい場合は、`gemini-cli-headless-delegation` の wrapper へ `tool_profile: proposal_only` と `output_sections: ["implementation_draft"]` を明示する。
   - 返却された `implementation_draft` は **proposal text** として扱い、そのまま適用済み実装や完了報告として扱わない。
   - final file edit / shell edit / GitHub mutation は引き続き Codex 側 worker または main thread が保持する。Gemini を default write-capable と扱ってはならない。
   - request に `post_to_issue_url`、direct file edit、shell execution、GitHub mutation 指示が混ざる場合は fail-closed とし、caller 側で request を修正してから再実行する。

### 複数 Issue 同時着手時の Allowed Paths 重複チェック（統合 PR 判定）

複数の implementation child issue を同時に着手する場合（例: batch 実装、関連 Issue の連続対応）、**実装開始前**に各 Issue の Allowed Paths の重複度を確認し、統合 PR の是非を判定する。

**判定基準（以下をすべて満たす場合は統合 PR を検討する）**:
- 各 Issue の Allowed Paths が 50% 以上重複している（特に同一ファイルへの変更が含まれる場合）
- 変更間に競合リスクが低い（同一ファイルでも変更箇所が異なる関数・セクション等）
- 各 Issue の Outcome が論理的に矛盾しない

**重複度の確認コマンド例**:

```bash
# 各 Issue の Allowed Paths を比較する（Issue #A と Issue #B の例）
PATHS_A=(".agents/skills/implement-issue/SKILL.md" "src/foo.py")
PATHS_B=("src/foo.py" "src/bar.py")

# 重複パスを抽出する
comm -12 <(printf '%s\n' "${PATHS_A[@]}" | sort) <(printf '%s\n' "${PATHS_B[@]}" | sort)
# → src/foo.py（重複あり）

# 重複率 = 重複パス数 / max(PATHS_A 数, PATHS_B 数) × 100
# この例: 1 / max(2, 2) = 50% → 統合 PR 検討の閾値に達する
```

**統合 PR を実施する場合**:
1. 統合実装用の worktree / branch を1本に集約する（例: `feat/issue-<A>-<B>-combined`）。
2. PR 本文の `## Linked Issue` に `Closes #A`, `Closes #B` を両方記載する。
3. AC・VC・Evidence は各 Issue ごとに分けて記載する。
4. 統合理由（重複パスと重複率）を PR 本文に明記する。

**統合 PR を実施しない場合（逐次実装のまま進む）**:
- 重複率が 50% 未満の場合
- 同一ファイルでも変更箇所が競合する可能性が高い場合
- 各 Issue の AC が独立して検証可能で、統合による複雑化リスクが高い場合

> 背景: PR #1253 では Issue #1242 / #1167 / #1182 の 3 件が同一ファイル（`ingestion_service.py`）への変更を含んでいたため統合 PR で実装した。この手順はその知見を制度化したもの（Issue #1256）。

### worktree 外作業禁止ガード（作業開始前に必ず実行）

外部（`impl-review-loop` 等）から `expected_worktree_path` と `canonical_repo_root` が渡された場合は、以下のガードを必ず実行してください:

```bash
# 1. 現在のディレクトリと worktree 状態を確認
pwd
git rev-parse --show-toplevel
git worktree list

# 2. main worktree（リポジトリルート）で作業していないことを確認
REPO_ROOT=$(git rev-parse --show-toplevel)
CURRENT_DIR=$(realpath "$(pwd)")
if [ "$CURRENT_DIR" = "$(realpath "$REPO_ROOT")" ]; then
  echo "[FATAL] main worktree（リポジトリルート）での作業を検知しました。"
  echo "  expected_worktree_path: $expected_worktree_path"
  echo "  current: $CURRENT_DIR"
  echo "停止します。expected_worktree_path 内で作業を再開してください。"
  exit 1
fi

# 3. 現在の git top-level が expected worktree 自身であることを確認
EXPECTED=$(realpath "$expected_worktree_path" 2>/dev/null || echo "$expected_worktree_path")
if [ "$CURRENT_DIR" != "$EXPECTED" ]; then
  echo "[FATAL] expected worktree path と現在ディレクトリが一致しません。"
  echo "  expected: $EXPECTED"
  echo "  current: $CURRENT_DIR"
  exit 1
fi
if [ "$(realpath "$REPO_ROOT")" != "$EXPECTED" ]; then
  echo "[FATAL] git top-level が expected worktree と一致しません。"
  echo "  expected: $EXPECTED"
  echo "  git_top_level: $(realpath "$REPO_ROOT")"
  echo "malformed worktree path または別 git repo への drift を検知したため停止します。"
  exit 1
fi

# 4. canonical repo の worktree registry に expected worktree が登録されていることを確認
CANONICAL_ROOT=$(realpath "$canonical_repo_root" 2>/dev/null || echo "$canonical_repo_root")
CANONICAL_TOPLEVEL=$(git -C "$CANONICAL_ROOT" rev-parse --show-toplevel 2>/dev/null || true)
if [ "$(realpath "$CANONICAL_TOPLEVEL" 2>/dev/null || echo "$CANONICAL_TOPLEVEL")" != "$CANONICAL_ROOT" ]; then
  echo "[FATAL] canonical_repo_root が canonical git repo を指していません。"
  echo "  canonical_repo_root: $CANONICAL_ROOT"
  echo "  canonical_git_top_level: $CANONICAL_TOPLEVEL"
  exit 1
fi
if ! git -C "$CANONICAL_ROOT" worktree list --porcelain | awk '/^worktree / {print substr($0,10)}' | while read -r wt; do realpath "$wt"; done | grep -Fx "$EXPECTED" >/dev/null; then
  echo "[FATAL] expected worktree path が canonical repo の worktree registry に存在しません。"
  echo "  canonical_repo_root: $CANONICAL_ROOT"
  echo "  expected_worktree_path: $EXPECTED"
  exit 1
fi

# 5. git common dir が canonical repo と一致することを確認
CURRENT_COMMON=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
CANONICAL_COMMON=$(git -C "$CANONICAL_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [ "$CURRENT_COMMON" != "$CANONICAL_COMMON" ]; then
  echo "[FATAL] git common dir が canonical repo と一致しません。"
  echo "  current_git_common_dir: $CURRENT_COMMON"
  echo "  canonical_git_common_dir: $CANONICAL_COMMON"
  exit 1
fi

# 6. `init` commit 系の別履歴へ drift していないことを確認
BASE_REF=main
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  BASE_REF=origin/main
fi
if ! git merge-base "$BASE_REF" HEAD >/dev/null 2>&1; then
  echo "[FATAL] expected worktree が $BASE_REF と共通 merge-base を持ちません。"
  echo "unexpected init commit ancestry または別履歴 repo の可能性があるため停止します。"
  exit 1
fi
```

**このガードの目的**: `expected_worktree_path` 外（main worktree）での作業だけでなく、`expected_worktree_path` 自体が attached git worktree ではなく root repo や別 git repo を向いている異常、canonical repo の worktree registry に存在しない clone / `git init` repo、`unexpected init commit ancestry` のような poisoned tree drift も fail-close で止める。

**実行タイミング**: Step 2 の Git 境界確認の**前**に実行する。

2. Git 境界を確認する:
   - **外部（impl-review-loop 等）から `expected_branch`、`expected_worktree_path`、`canonical_repo_root` が渡された場合**: worktree は既に作成済みであるため、自前で `git worktree add` を実行しない。渡された値を使って以下の mismatch チェックを行い、一致した場合にその worktree 上で実装を開始する。
     ```bash
     # mismatch チェック（branch / path のいずれかが不一致でも同様に失敗する）
     git branch --show-current  # expected_branch と一致するか確認
     # path mismatch チェック時は realpath で canonicalize してから比較する
     ACTUAL=$(realpath "$(pwd)")
     EXPECTED=$(realpath "$expected_worktree_path" 2>/dev/null || echo "$expected_worktree_path")
     [ "$ACTUAL" != "$EXPECTED" ] && echo "path mismatch: expected $EXPECTED / actual $ACTUAL" && exit 1
     GIT_TOPLEVEL=$(realpath "$(git rev-parse --show-toplevel)")
     [ "$GIT_TOPLEVEL" != "$EXPECTED" ] && echo "git-top-level mismatch: expected $EXPECTED / actual $GIT_TOPLEVEL" && exit 1
     CANONICAL_ROOT=$(realpath "$canonical_repo_root" 2>/dev/null || echo "$canonical_repo_root")
     git -C "$CANONICAL_ROOT" worktree list --porcelain | awk '/^worktree / {print substr($0,10)}' | while read -r wt; do realpath "$wt"; done | grep -Fx "$EXPECTED" >/dev/null || {
       echo "worktree-registry mismatch: expected $EXPECTED is not registered under $CANONICAL_ROOT"
       exit 1
     }
     CURRENT_COMMON=$(git rev-parse --path-format=absolute --git-common-dir)
     CANONICAL_COMMON=$(git -C "$CANONICAL_ROOT" rev-parse --path-format=absolute --git-common-dir)
     [ "$CURRENT_COMMON" != "$CANONICAL_COMMON" ] && echo "git-common-dir mismatch: expected $CANONICAL_COMMON / actual $CURRENT_COMMON" && exit 1
     BASE_REF=main
     git rev-parse --verify origin/main >/dev/null 2>&1 && BASE_REF=origin/main
     git merge-base "$BASE_REF" HEAD >/dev/null 2>&1 || {
       echo "unexpected init commit ancestry: HEAD has no merge-base with $BASE_REF"
       exit 1
     }
     ```
     - `branch` 不一致（`git branch --show-current` が `expected_branch` と異なる）→ 停止して報告（branch 名の一致で issue 番号の整合性も確認される。issue 番号の整合性確認は orchestrator の責務）
     - `path` 不一致（`realpath "$(pwd)"` が `realpath "$expected_worktree_path"` と異なる）→ 停止して報告
     - `git top-level` 不一致（`git rev-parse --show-toplevel` が `expected_worktree_path` と異なる）→ 停止して報告。`wip/worktree-*` 風のディレクトリでも attached git worktree でなければ失敗させる
     - `worktree registry` 不一致（`git -C "$canonical_repo_root" worktree list --porcelain` に `expected_worktree_path` が存在しない）→ 停止して報告。別 clone / 別 git repo を fail-close する
     - `git common dir` 不一致（`git rev-parse --git-common-dir` が canonical repo と一致しない）→ 停止して報告。attached worktree ではない別 repo を fail-close する
     - `origin/main` または `main` と共通 merge-base を持たない（`unexpected init commit ancestry`）→ 停止して `git reflog --date=iso --all` / `git worktree list` / `git status --short --branch` を evidence として残す
   - **デフォルト（外部から worktree が渡されない場合）**: worktree を新規作成し、以降の作業はそのディレクトリ内で行う。
     ```bash
     git worktree add -b feat/issue-<N>-<slug> wip/worktree-issue-<N>-<slug> main
     cd wip/worktree-issue-<N>-<slug>  # 以降の Bash コマンドはこのディレクトリで実行する
     ```
   - ユーザーが「worktree を使わない」「直接ブランチで」などと**明示した場合のみ** `git checkout -b feat/issue-<N>-<slug>` を使う。
   - current session や repo policy が branch / PR を禁止する場合は、実装へ進まず Issue comment に `blocked` 理由を書く。

### worktree 内での Edit ツール使用上の注意

worktree 内で Edit ツールを使う場合は、**必ず worktree 内の絶対パス**を指定してください。main リポジトリの絶対パス（例: `/home/squne/projects/KindleAudiobookMakeSystem/path/to/file`）を指定すると、worktree ではなく main ブランチのファイルが変更される危険があります。worktree 内での編集は必ず `/home/squne/projects/KindleAudiobookMakeSystem/wip/worktree-issue-<N>-.../path/to/file` のような worktree 内の絶対パスで指定してください。

3. 実装する:
   - Allowed Paths だけを変更する。
   - `proposal_only` から `implementation_draft` を受け取った場合でも、Codex 側で内容を査読し、必要に応じて修正してから final file edit / shell edit を実行する。
   - **調査の結果「変更不要」と判明した場合でも、PR を作成して AC 充足の証拠を記録すること**（KH from PR #1168）。`Changed Paths: なし` の旨を PR 本文に記載し、Verification Commands の実行結果を Evidence として添付する。「何もしなかった」という確認自体が成果物である。
   - scope delta が見えたら即停止し、以下の順で記録する:
     1. `templates/github-ops/scope-delta.md` の書式（`Trigger` / `Why original scope is insufficient` / `Impacted paths` / `Keep in current issue?` / `Proposed follow-up issue` / `Human decision needed` / `Decision`（人間回答後に記入））を埋める。
     2. 上記内容を実行中 Issue の `## Scope Delta（該当時のみ記載）` セクションに追記し、人間の判断（`Human decision needed`）を Issue comment に残して回答を待つ。
   - lambda 残存確認（KH-3 from PR#179）: 実装対象に `execute_coordinate_phase` のような phase 系メソッドが含まれる場合、実装後に以下で lambda 残存がないことを確認する:
     ```bash
     grep -n "execute_coordinate_phase.*lambda\|execute_uia_phase.*lambda" <target_path>
     ```
     lambda が残存している場合は、依存する呼び出し元を追跡して除去する（phase executor への lambda 渡しは serialization 違反を招く）。
4. verify を実行する（drift-check CI のための事前確認）:
   - `.agents/skills/` を変更した場合は、verify 実行前に skills sync drift チェックを先行実行する:
     ```bash
     bash scripts/sync-agent-skills.sh --check
     ```
     drift が検出された場合は Step 6 の sync 手順を先に実行してから verify へ進む（drift があると Skill Sync Check CI が FAILURE になり、verify 結果が無意味になる）。（drift-check from PR#190）
   - Issue contract の `Verification Commands` を優先する。
   - **VC の grep パターンと実際の出力の大文字/小文字を一致させること**: スキル定義の出力テンプレートに固定セクションヘッダー（例: `### Baseline Failure`）を追加する際、`grep -r "baseline"` は小文字のみにヒットし `Baseline` にはヒットしない。VC で確認したいキーワードは HTML コメント行などに小文字で含める、またはパターンに `-i` フラグを使うなど、大文字/小文字の整合性を事前確認すること（PR #803 KH）。
   - **`just check <target>` を必ず実行する**（Verification Commands の有無にかかわらず）:
     - `<target>` は Allowed Paths から推定する:
       - `src/` or `tests/` を含む → `just check`（デフォルト = `src tests`）
       - `wip/xxx/` を含む → `just check wip/xxx`
       - 両方含む → `just check "src tests wip/xxx"`
       - 上記いずれにも該当しない（テンプレート・ドキュメント・スキルのみ） → `just check` 対象外。Commands Run に「対象外：Allowed Paths に `src/` `tests/` `wip/` を含まないため」と理由を記録する
     - **出力の読み方（AI 向けガイド）**:
       ```
       just check の出力構造:
       1. [_guard-scope] スコープ検証 → PASS/FAIL
       2. [lint]         ruff check   → エラー件数・ファイル・行番号
       3. [typecheck]    pyright      → エラー件数・ファイル・行番号
       4. [test]         pytest       → passed/failed 件数
       5. [trace]        トレーサビリティ → coverage %
       全体結果: exit code 0 = 全 PASS、非 0 = いずれか FAIL
       ```
     - exit 0 → PASS。出力サマリーを Commands Run に記録する。
     - 非 0 → FAIL セクションのエラーを修正してから再実行する。
     - `just check` が環境依存で実行不能な場合は、`just lint <target>` + `just test <target>` を個別実行し、その旨を Commands Run に記録する。
     - `ci_summary.json` が生成された場合は、そのパスを `artifact refs` に記載する。
   - 追加で `git diff --check` を実行する際、事前に CRLF 検出→変換のサブ手順を行う（T-2）:
     ```bash
     # 1. 編集したファイルの CRLF 状態を確認する
     file <path>
     # 出力に "CRLF line terminators" が含まれれば変換する
     sed -i 's/\r//' <path>
     # 2. 変換後に git diff --check を実行する
     git diff --check
     ```
   - WSL 上で Windows 作成ファイルを編集した場合は特に CRLF 混在に注意する。
   - **大きなファイル（数百行以上）の CRLF→LF 一括変換は、内容変更と別コミットにする**。変換が全行 diff に現れ `git blame` が失われ、レビュアビリティが著しく低下する。
5. Session Retrospective（知見収穫）を実行する:
   - まず簡易判定を行う: セッション中に以下のいずれかがあったか確認する。
     - エラー回避・未文書化制約の発見
     - 効率的なデバッグ・操作手順の工夫
     - ドキュメントと異なる挙動の確認
     - 判断に迷った箇所・今後の判断基準となる気づき
   - 該当なし → このステップをスキップしてよい。
   - 該当ありの場合、以下の情報を整理する:
     - 実装差分の概要（`git diff main...HEAD` の要点）
     - セッション中に遭遇したエラーや回避策
     - 判断済みの方針・未解決点
   - 知見ごとに以下のルーティング分類を適用する（`.agents/rules/skill-rule-boundary.md` と `.kiro/steering/custom-steering-index.md` を参照）:
     | 知見の性質 | 反映先（具体的なファイルパスまたは新規作成提案を含む） |
     |---|---|
     | 「この状況で、これをしてよいか？」（制約・判断基準） | `.agents/rules/xxx.md`（既存 or 新規） |
     | 「このタスクを、どう実行するか？」（手順・操作手順書） | `.agents/skills/xxx/SKILL.md`（既存 or 新規） |
     | プロジェクト横断のパターン・方針 | `.kiro/steering/xxx.md`（既存 or 新規） |
     | 該当なし / 一時的 | 記載しない |
   - 抽出した知見を次のステップの PR 本文 `Knowledge Harvesting` セクションに構造化して記載する。
6. `.agents/skills/` 変更時の sync を実行する（Allowed Paths のいずれかが `.agents/skills/` 配下のパスである場合のみ）:
   - 以下のコマンドを順番に実行する:
     ```bash
     bash scripts/sync-agent-skills.sh
     git add .claude/skills/
     git commit -m "chore(skills): .agents/skills/ 変更を .claude/skills/ に同期する"
     bash scripts/sync-agent-skills.sh --check
     ```
   - `--check` で drift なしが確認できた場合のみ次へ進む。drift があれば原因を調査して再実行する。
   - この手順を省くと Skill Sync Check CI が FAILURE になるため、PR 作成前に必ず実行する。
7. PR を作成する（Phase 3 では `open-pr` へ実委譲）:
   - **Phase 3 正本**: `open-pr` を PR 作成責務の正本とし、`implement-issue` 側で `gh pr create` の手順を重複定義しない。
   - `implement-issue` 側の責務は以下に限定する:
     1. PR 本文（`.github/PULL_REQUEST_TEMPLATE.md` 準拠、`Closes #<issue_number>` を含む）を組み立てる
     2. `publish: yes` を確認する
     3. `open-pr` へ `pr_title` / `linked_issue` / `pr_body` / `publish` を渡して委譲する
     4. `open-pr` の output（`PR_URL` と template guard / idempotency / downgrade の結果）を linked issue comment と handoff に反映する
   - 明示的に ready-for-review 指示がない限り draft 既定で作成する（draft 指定は `open-pr` 側の正本手順に従う）。
   - `templates/github-ops/pr-evidence.md` の項目は `implement-issue` 側で引き続き満たす。

### PR 作成: open-pr スキルへの委譲（Phase 3 正本）

本手順の Step 7（PR 作成）は、Phase 3 では `open-pr` スキルへ委譲する。移行フェーズの正本は `.agents/skills/open-pr/SKILL.md` の「段階的移行計画」と「Output Contract」であり、このセクションでは呼び出し側（`implement-issue`）で必要な入力・回収項目のみを定義する。

**移行判断基準（open-pr 正本への参照）:**

段階的リファクタリングの観点から、以下の条件を確認し、詳細なフェーズ進行条件は `open-pr` の「移行判断基準」表に従ってください:

1. **publish ゲート完成度**: 人間承認フロー（`publish: yes` 確認）が確定している
2. **PR テンプレート安定性**: `.github/PULL_REQUEST_TEMPLATE.md` が実運用 PR で複数回確認済み
3. **Idempotency 確認**: 同一ブランチへの重複 PR 生成が観測されていない
4. **Closes/Refs 自動判定**: issue state 判定ロジックが stale state を生成していない

**委譲手順（open-pr Phase 2 以降の実運用候補）:**

1. Issue contract の AC を充足した後、Step 3～6（実装・verify）は通常通り実行
2. Step 7（PR 作成）を以下のように replace する:

   ```bash
   # PR 本文を生成する（implement-issue の手順通り）
   pr_body=$(cat <<'EOF'
   ## Linked Issue
   Closes #<issue_number>

   ## Summary
   ...（以下省略）
   EOF
   )

   # open-pr スキルへ CLI 委譲（実行可能: `scripts/open-pr`）
   scripts/open-pr \
     --pr_title "<conventional_commits_形式のタイトル>" \
     --linked_issue <issue_number> \
     --pr_body "$pr_body" \
     --publish yes \
     --canonical_pr_url "$canonical_pr_url" \
     --superseded_prs "$superseded_prs_json" \
  --repair_context "$(jq -n --arg previous_pr_url "$previous_pr_url" --arg reason "$repair_reason" \
    '{reason: $reason, previous_pr_url: $previous_pr_url, mode: "create-replacement"}')" \
     --dry_run false
```

3. `open-pr` の実行結果（`PR_URL`, `CANONICAL_PR_URL`, `CANONICAL_PR_SOURCE`, `SUPERSEDED_PRS`）を取得し、Issue comment に記載する

**デバッグ用 dry_run 活用（open-pr Phase 1 の互換確認）:**

PR テンプレートが未完成な段階では、`open-pr` の `dry_run: true` モードで PR 本文を検証できます:

```bash
# 実装後、PR 作成前に dry_run で検証する（実行可能 CLI のハンドシェイク）
scripts/open-pr \
  --pr_title "feat(skills/open-pr): ..." \
  --linked_issue 1770 \
  --pr_body "$pr_body" \
  --publish yes \
  --dry_run true  # ← PR は作成しない

# ハンドシェイク出力で PR 本文の構成を確認
# → 内容確認後、publish: yes で再度実行（dry_run: false）
```

**移行の適用条件:**

- `open-pr` スキルの dry_run テスト・実運用テストが、`open-pr` 側の件数ベース基準を満たしている
- `impl-review-loop` / `pr-reviewer` への統合テストが完了している
- open-pr への委譲後も `Closes` / `Refs` downgrade が正常に動作することを確認している

**委譲後の責務分離:**

| 責務 | 担当スキル |
|---|---|
| Issue contract AC 確認・実装・verify | `implement-issue` |
| PR テンプレート生成・Evidence 集約 | `implement-issue` |
| publish ゲート確認 | `implement-issue` または `impl-review-loop` |
| PR 作成・Idempotency 確認・Closes/Refs 自動判定 | **`open-pr`** |
| PR review・verdict 判定 | `pr-review-judge` |

**段階的移行の例（時間軸は例示であり、進行条件ではない）:**

1. `open-pr` スキルマージ直後は implement-issue が PR 作成（保守的）
2. dry_run モードで `open-pr` 呼び出し検証（`open-pr` Phase 1）
3. 実運用で `open-pr` に委譲開始（`open-pr` Phase 2）
4. `impl-review-loop` の test-runner との統合まで含めた完全委譲（`open-pr` Phase 3）

8. linked issue へ返却する:
   - `verified outcome`
   - `commands run`
   - `artifact refs`
   - `linked PR`
   - `unresolved items`
   - `next action`
   を comment で残す。
9. PR マージ後のクリーンアップ依頼時に parent issue のクローズ判定を行う:
   - linked issue が closed であることを確認する（open の場合はスキップ）。
   - linked issue の sub-issue relationship に parent issue が存在するか確認する。
   - 存在する場合は `issueops-operations` の「Issue Completion」ステップ 6（親 Issue クローズ条件確認）を実行する。

## Output Contract

**GitHub surface: PR 本文 + linked issue comment**

| surface | 内容 |
|---------|------|
| PR 本文 | `pr-evidence.md` テンプレートを使う（`Closes #<N>` 必須） |
| Linked issue comment | verify 結果 + linked PR URL を書く |

**Linked issue comment 必須項目:**

- `verified outcome`
- `commands run`（コマンドと出力）
- `artifact refs`
- `linked PR`（URL）
- `unresolved items`
- `next action`

### blocked（branch / PR 作れない）fail-closed 例

Issue comment に以下を書いて停止する:

    ## Implement: BLOCKED
    branch / PR を作成できない制約があります。

    ### 実装済みファイル
    - <path>: <変更概要>

    ### Verify 結果
    <コマンド>
    → <出力>

    ### 次の人間アクション
    - [ ] `git push origin HEAD && gh pr create --draft --title "..." --body "..."` で PR を手動作成する
    - [ ] または branch / PR 制約緩和の判断を返す（worktree 運用 Issue を完了させる等）
    - [ ] または main 直コミットを承認する

## Handoff to pr-review-judge

`implement-issue` が PR 作成後、`pr-review-judge` へ渡す必須項目:

- PR URL
- Linked issue URL
- Commands Run の結果（テキスト）
- Changed Paths リスト

## Workaround 制度化前の根本原因確認ガード（KH-3 from PR#190）

実装中に workaround（暫定対処）が必要になった場合、制度化（コードや skill/rule への永続化）する前に以下を確認する:

1. **根本原因の特定**: workaround が解決しようとしている問題の根本原因を明記する（例: 「WSL 上で pynput が evdev を要求して失敗する」）。
2. **根本修正の実現性判断**: 根本原因を今回の PR スコープで修正できるかを判断する。できる場合は workaround ではなく根本修正を行う。
3. **workaround の制度化条件**: 以下のすべてを満たす場合のみ workaround を rule/skill/code に反映する:
   - 根本原因が今回スコープ外（別 Issue として追跡済み）
   - workaround が副作用なく安全に適用できる
   - workaround 適用の条件・解除条件が明示できる
4. **追跡 Issue の確認**: workaround を制度化する場合は、根本修正を追跡する Issue 番号を workaround コメント（または rule の `出典` 行）に記載する。

## カスタム例外の多段 except で先行 re-raise が必要なパターン（KH from PR#1175）

Python でカスタム例外を多段 `except` ブロック内で扱う場合、汎用 `except Exception` ブロックより**前に**カスタム例外の `except` を記述しないと、汎用ブロックが先に捕捉してしまう。

```python
# BAD: CropFailedException が except Exception に飲み込まれる
try:
    ...
except Exception as e:
    log.error("unexpected", error=str(e))
    raise
except CropFailedException:   # ← 到達しない
    raise

# GOOD: カスタム例外を先に捕捉する
try:
    ...
except CropFailedException:   # ← 先に re-raise
    raise
except Exception as e:
    log.error("unexpected", error=str(e))
    raise
```

出典: PR #1175 / Issue #922（`ocr_service.py` での `CropFailedException` 多段 except）

## 数値ページ番号の存在チェックは `is not None` を使う（KH from PR#1175）

`if page_num`（falsy check）は `page_num=0` を「存在しない」と誤評価し、ページ 0 での失敗ログに番号が出力されない observability 劣化を引き起こす。

```python
# BAD: page_num=0 が False と評価されログが出ない
if page_num:
    log.warning("crop failed", page=page_num)

# GOOD: None チェックで 0 を正しく扱う
if page_num is not None:
    log.warning("crop failed", page=page_num)
```

適用対象: ページ番号・インデックス・カウンタなど「0 が有効値」の整数パラメータ全般。

出典: PR #1175 / Issue #922（`detect_content_area()` の `page_num` falsy 判定問題、追跡 Issue #1181）

## constructor 直結テストで factory 回帰を固定するパターン（KH from Issue #1156, PR #1157）

`RecipeBootstrap(log_root=...)` のような factory / constructor の出力値を回帰させたい場合は、constructor に env override を渡して `.log_root` 等の属性を直接 assert するテストを書く。

```python
def test_recipe_bootstrap_log_root_uses_log_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    rb = RecipeBootstrap(recipe_dir=..., config=...)
    assert rb.log_root == PathResolver().resolve_log_dir(env_override=str(tmp_path))
```

**Why:** `PathResolver` 経由の共通化後は、利用側（bootstrap）が本当に同じ変換規則を踏むかを constructor 直結で押さえないと、利用側のバグが長期間見逃されやすい。

出典: Issue #1156, PR #1157（`RecipeBootstrap.log_root` と `PathResolver.resolve_log_dir` の整合テスト）

## run_process フェイクは env= を受け取れるようにしておく（KH from Issue #1156, PR #1157）

bootstrap の typecheck 系テストで `run_process` をモックする場合、`env=` キーワード引数を受け取れるシグネチャにしておくと、実装側が `run_process(..., env={...})` を追加したときにテストが壊れにくい。

```python
def fake_run_process(cmd, *, cwd=None, env=None, **kwargs):
    return CompletedProcess(cmd, 0, stdout="", stderr="")
```

**Why:** `env=` なしの strict なフェイクだと、bootstrap が env 渡しを追加した瞬間に `TypeError: unexpected keyword argument` でテスト全体が落ちる。受け取るだけで何もしないフェイクにしておくと、機能テストは別で書きつつ型シグネチャの破壊を防げる。

出典: Issue #1156, PR #1157（`test_bootstrap_recipe.py` の `run_process` モック修正から）

## `just check` 初回実行が環境構築の副作用で exit 1 になる場合がある（KH from Issue #1215, PR #1275）

`just check src` の初回実行時、uv による仮想環境作成が走ることで exit 1 になることがある。2回目を実行すると PASS する場合は環境構築の副作用であり、実装コードの問題ではない。Commands Run には「初回 exit 1（環境構築）、2回目 exit 0」と明記することで、test-runner や adversarial-reviewer が誤判断しないようにする。

**Why:** CI 環境では仮想環境が存在しない状態で `just check` を実行するため、初回は環境構築中に一見するとビルドエラーに見える出力が発生することがある。2回目実行で確認してから結果を報告することで誤報を防げる。

出典: Issue #1215, PR #1275（実装者が初回 exit 1 を観測し、2回目 PASS で確認した事例）

## Rich `console.print()` は JSON 出力に使わない（KH from Issue #2, PR #1277）

`rich.Console.print()` はターミナル幅に応じてパス文字列を自動折り返しするため、JSON を出力すると改行が混入してパース不能になる。JSON 出力には標準の `print()` を使うこと。

**Why:** `console.print(json_str)` で出力した JSON を `json.loads()` に渡すとパースエラーになる実例が PR #1277 で発生した。CLI コマンドの JSON 出力は `--format json` の場合に `print()` を使い、`console.print()` は人間向け（Markdown / テーブル）出力のみに限定する。

出典: Issue #2, PR #1277

## Guardrails

- Issue contract にない範囲へ広げない。
- `Allowed Paths` を越えたら止まる。
- PR を作れない制約があるのに、黙って direct push へ落とさない。
- verify 未実行のまま完了扱いにしない。
- `## Required Skills` に `issue-contract-review` / `implement-issue` 等の暗黙的ワークフロースキルが列挙されていても、「このスキルが preload されていないため実装を開始できない」とは判断しない（詳細: `.agents/rules/github-ops-workflow.md` KH-N6）。
- `## Rules` セクションが Issue 本文に存在しない場合でも、ルール参照なしで実装を開始してよい（Rules セクションは任意）。
- template mismatch（Issue 本文が最新テンプレートと一致しない）の自動警告機能は未整備であり、create-issue の Issue Template Guard と issue-contract-review の BLOCKED が現時点の主な手段である。Issue 本文がテンプレートと一致しない箇所があっても、contract（Outcome/AC/VC/Allowed Paths）が揃っている場合は実装を開始してよい。

## Related

- rule: `.agents/rules/github-ops-workflow.md`
- rule: `.agents/rules/skill-rule-boundary.md`
- rule: `.agents/rules/issue-uncertainty-policy.md`
- rule: `.agents/rules/issueops-common-guard.md`
- rule: `.agents/rules/issue-uncertainty-policy.md`
- skill: `.agents/skills/issue-contract-review/SKILL.md`
- template: `templates/github-ops/pr-evidence.md`
- template: `templates/github-ops/scope-delta.md`
- steering: `.kiro/steering/custom-steering-index.md`

## inline review comment / review thread 対応（追加手順）

- オーケストレータから `pr-reviewer` の inline feedback を受け取る場合、**最低限以下の情報を渡す**（不足時は作業停止し、返却時に問い合わせる）:
  - `pr_number`: 対象 PR 番号
  - `pr_number` の `head.sha`（`reviewed_head_sha` と比較可能な値）
  - `thread_id`（GraphQL の `ReviewThread.id`、または実行時に対応 thread の `node.id`）
  - `path`: 参照対象ファイルパス（相対）
  - `line`: 指摘対象開始行
  - `end_line`: 指摘対象終了行（未指定可）
  - `finding_ref`: （任意）`FindingRef` / メタ情報の参照キー
  - `comment_body`: inline 指摘本文（最小限）
  - `actionable`: true/false（対象外なら skip）
  - `requested_fix`: 修正方針（ある場合）
  - `target_state`: `open` / `resolved` などの要求状態
  - `deadline_reason`（任意）

- 手順: `implement-issue` は `pr_number` と `thread_id` を受け取り、対象 `thread` の `actionable` フィールドが true かつ `path` / `line` が AC に追える範囲なら実装に反映する。対象外条件（以下）は **必ず確認して** skip し、`thread_id` を resolve しない。
  - `thread_id` が空
  - `path` が `Allowed Paths` 外
  - `pr_number` のレビュー状態が今回対象 thread 外
  - `actionable` が false または `finding_ref` が別 Issue 依存

- inline 指摘は `execute -> 修正 -> 検証` の順で扱い、`LOOP_VERDICT` / `reviewed_head_sha` の再評価とは分離する。`reviewed_head_sha` の更新可否や `LOOP_VERDICT` の判定は実装ループ終了判定の**canonical**として残し、thread の resolve は実装完了の evidence としてのみ扱う（正本の置換はしない）。

- 指摘反映後は、Issue contract の `Verification Commands` を再実行し、再実行結果を `Commands Run` と `Acceptance Criteria` エビデンスに追記する。

- `thread_id` が与えられた場合の完了処理（`gh api graphql`）:
  - thread 取得例:
    ```bash
    gh api graphql \
      -F owner=squne121 \
      -F repo=KindleAudiobookMakeSystem \
      -F pr_number=<PR_NUMBER> \
      -f query='query($owner: String!, $repo: String!, $pr_number: Int!){
        repository(owner:$owner, name:$repo) {
          pullRequest(number: $pr_number) {
            reviewThreads(first: 50) {
              nodes {
                id
                isResolved
                path
                line
                startLine
                isOutdated
                comments(first: 1) {
                  nodes {
                    id
                    body
                  }
                }
              }
            }
          }
        }
      }'
    ```
  - 対象 thread を修正後に resolve する例:
    ```bash
    gh api graphql \
      -f query='mutation($threadId: ID!) {
        resolveReviewThread(input: {threadId: $threadId}) {
          clientMutationId
        }
      }' \
      -F threadId=<threadId>
    ```
  - 同一 PR の全 thread を対象にしない。actionable な対象 `thread_id` のみを resolve する。

- `resolve` 判定が必要な場合は PR 本文に API 例 (`resolveReviewThread`) と threadId/対象ハンドオフを残し、次回 `pr-reviewer` に引き継ぐ。

## resolveReviewThread 実行前の fail-closed gate

`thread_id` を受け取った時点で、以下を必ず実行して一致しない場合は resolve を行わず skip する。スキップ理由は PR/Issue evidence に記録し、`thread_id` 単位で完了状態を更新する。

1. `current_head_sha` の一致確認
   - `reviewed_head_sha` が受領されている場合は、`current_head_sha == reviewed_head_sha` が**必須**（`pr.head.sha` への fallback は不可）
   - `reviewed_head_sha` 未受領の場合のみ、`current_head_sha == pr.head.sha` を許可
2. `thread_id` 一致確認
   - GraphQL の reviewThreads ノード `id` が受領 `thread_id` と完全一致していること
3. 対象範囲の一致確認（Allowed Paths）
   - `path` が Allowed Paths 内であること
   - `path` 不一致は対象外として resolve しない
4. 行情報の一致確認
   - `line` / `startLine` / `endLine` が現在の thread 情報と一致すること（少なくとも一方でも相違があれば skip）
5. 指摘内容の識別確認
   - `comment_id` または `comment_body` のいずれかが一致し、誤 thread の resolve を避けられること
6. thread 状態の一致確認
   - `isResolved == false` であること
   - `isOutdated == false`（または運用で許容可能な値であること）であること
7. 要求状態の一致確認
   - `target_state` が `resolved` など期待値と一致していること

上記のいずれかが不一致の場合:
- `resolveReviewThread` は実行しない
- `skip` した場合は「どの比較項目が不一致だったか」を `PR body` と `Issue comment` の evidence に明記する
- `thread_id` は受領順序外に解決しない

`resolveReviewThread` 実行は補助エビデンスであり、`LOOP_VERDICT` / `reviewed_head_sha` は引き続きループ終了の正本として扱う。
