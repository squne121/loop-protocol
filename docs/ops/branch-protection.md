# main branch protection / ruleset

`main` ブランチへの直接 push を GitHub remote 側で技術的に拒否するための保護設定の記録。

## 背景

2026-05-24 に implementation-worker SubAgent が PR branch に push すべき修正 commit (`abbc61a`) を誤って `main` へ直接 push した。PR #358 で main の復旧は完了し、PR #355 で #324 の正規修正も merge 済み。本書は再発防止として、GitHub 側で main 直接 push を拒否する保護設定 (Issue #359) の正本となる。

関連:

- #358 — main 直接 push インシデントの復旧 PR (MERGED)
- #355 — #324 の正規実装 PR (MERGED)
- #359 — 本 hardening Issue
- #360 — SubAgent 側 PreToolUse push destination guard hook (OPEN, 補完策)

## 採用構成（多層防御）

GitHub Ruleset を一次保護とし、Branch protection rule を二次保護として併用する。両者の rule は集約され、より厳しい方が適用される（[About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)）。

### 一次: Ruleset `main-direct-push-protection`

- Ruleset ID: `16796903`
- HTML URL: https://github.com/squne121/loop-protocol/rules/16796903
- Target: `refs/heads/main`
- Enforcement: `active`
- Bypass actors: なし（`current_user_can_bypass: never` — repository owner も bypass 不可）
- Rules:
  - `deletion`: 削除禁止
  - `non_fast_forward`: force push 禁止
  - `required_linear_history`: linear history 必須
  - `pull_request`: PR 経由必須 / `required_review_thread_resolution: true` / `dismiss_stale_reviews_on_push: true`
  - `required_status_checks`: `typecheck`, `lint`, `test`, `build`, `python-test` を `strict_required_status_checks_policy: true` で必須化

### 二次: Branch protection rule (`/branches/main/protection`)

- `required_status_checks.contexts`: `typecheck`, `lint`, `test`, `build`, `python-test`（`strict: true`）
- `required_status_checks.checks[].app_id`: 全て `15368`（GitHub Actions app）。check source は GitHub Actions に固定されており、外部 PAT / 第三者 GitHub App による status 詐称は受け付けない（Blocker 3 対応）
- `enforce_admins.enabled`: `true`（admin bypass を禁止）
- `required_pull_request_reviews.dismiss_stale_reviews`: `true`
- `required_pull_request_reviews.required_approving_review_count`: `0`（**意図的に 0**。本設定は「main 更新を PR 経由に限定する」ことが目的であり、「承認 1 件以上を必須にする」ことは #359 のスコープ外。承認要件の引き上げは別 Issue として扱う）
- `required_pull_request_reviews.bypass_pull_request_allowances`: API レスポンスにフィールド自体が存在しない。個人 (user-owned) リポジトリでは GitHub が当該フィールドを返さず、PR review 要件を bypass できる users / teams / apps は構造上存在しない（Blocker 2 対応）
- `required_conversation_resolution.enabled`: `true`
- `required_linear_history.enabled`: `true`
- `allow_force_pushes.enabled`: `false`
- `allow_deletions.enabled`: `false`

### required_approving_review_count を 0 とした判断（Blocker 5 対応）

本設定は「main 更新を PR 経由に限定する」ことが目的であり、「承認 1 件以上を必須にする」ことは今回の #359 スコープ外。`required_approving_review_count` は意図的に 0 とする。

将来 agent 運用事故の再発防止として一段強める場合の候補:

- `required_approving_review_count: 1` 以上を必須化
- `require_last_push_approval: true` で push 後の再承認を必須化

いずれも別 Issue で議論し、本書とは独立して更新する。

### Required status checks の source pinning（Blocker 3 対応）

- Ruleset 側 `required_status_checks[].context` は status check 名のみ指定（context のみ）
- Branch protection 側 `required_status_checks.checks[]` で 5 contexts すべてを `app_id: 15368`（GitHub Actions app）に固定
- これにより、required check の status は GitHub Actions が報告したもののみが評価対象となる（外部 PAT / 別 GitHub App / 任意 user が `gh api .../statuses` で詐称した同名 status は無視される）
- Ruleset 側にも `integration_id` で source pinning できるが、二層構成のうち Branch protection 側で固定済みのため、Ruleset 側は context-only で運用する（多層防御として十分）

## 検証手順

### 設定 snapshot の取得

```bash
# Ruleset 一覧と詳細
gh api repos/squne121/loop-protocol/rulesets --jq '.[] | {id, name, enforcement, target}'
gh api repos/squne121/loop-protocol/rulesets/16796903 \
  --jq '{id, name, target, enforcement, bypass_actors, current_user_can_bypass, conditions, rules}'

# Branch protection
gh api repos/squne121/loop-protocol/branches/main/protection \
  --jq '{
    required_status_checks,
    required_pull_request_reviews,
    required_conversation_resolution,
    enforce_admins,
    restrictions,
    allow_force_pushes,
    allow_deletions,
    required_linear_history
  }'

# bypass_pull_request_allowances の存在確認
gh api repos/squne121/loop-protocol/branches/main/protection/required_pull_request_reviews \
  --jq '.bypass_pull_request_allowances // "field_absent"'
```

### 2026-05-24 取得 raw snapshot（Blocker 1 対応・監査正本）

**Ruleset `16796903`** — `gh api .../rulesets/16796903 --jq '{id, name, target, enforcement, bypass_actors, current_user_can_bypass, conditions, rules}'`

```json
{
  "id": 16796903,
  "name": "main-direct-push-protection",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "current_user_can_bypass": "never",
  "conditions": {
    "ref_name": {
      "exclude": [],
      "include": ["refs/heads/main"]
    }
  },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    {
      "type": "pull_request",
      "parameters": {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": true,
        "required_reviewers": []
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "do_not_enforce_on_create": false,
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "typecheck" },
          { "context": "lint" },
          { "context": "test" },
          { "context": "build" },
          { "context": "python-test" }
        ]
      }
    }
  ]
}
```

**Branch protection** — `gh api .../branches/main/protection --jq '{...}'`（上記コマンド参照）

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["typecheck", "lint", "test", "build", "python-test"],
    "checks": [
      { "context": "typecheck", "app_id": 15368 },
      { "context": "lint", "app_id": 15368 },
      { "context": "test", "app_id": 15368 },
      { "context": "build", "app_id": 15368 },
      { "context": "python-test", "app_id": 15368 }
    ]
  },
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "require_last_push_approval": false,
    "required_approving_review_count": 0
  },
  "required_conversation_resolution": { "enabled": true },
  "enforce_admins": { "enabled": true },
  "restrictions": null,
  "allow_force_pushes": { "enabled": false },
  "allow_deletions": { "enabled": false },
  "required_linear_history": { "enabled": true }
}
```

**bypass_pull_request_allowances** — `gh api .../required_pull_request_reviews --jq '.bypass_pull_request_allowances // "field_absent"'`

```
field_absent
```

> このリポジトリは個人 (user-owned, `owner.type: User`) であり、GitHub REST は当該リポジトリの `required_pull_request_reviews` に `bypass_pull_request_allowances` フィールドを返さない。すなわち PR review 要件を bypass できる users / teams / apps は構造上存在しない（[REST API: Branch protection](https://docs.github.com/en/rest/branches/branch-protection?apiVersion=2022-11-28)）。組織リポジトリへ移管する場合は本フィールドが返るようになるため、その時点で本書を更新し空であることを再確認する。

### 直接 push 拒否の実測

#### A. AC6 契約文言準拠 (`HEAD:main`)

Issue #359 の AC6 と完全一致する形式での再現コマンド:

```bash
git fetch origin main
git checkout -b test-direct-push-main-protection origin/main
git commit --allow-empty -m "test: verify main direct push protection"
git push origin HEAD:main
# 期待: GH013 / remote rejected — 失敗で終わる
git checkout -  # 元のブランチへ戻る
git branch -D test-direct-push-main-protection
```

#### B. ローカル汚染を最小化する form（commit-tree 経由）

実際の 2026-05-24 実測ではローカルブランチを切らずに `update-ref` + `commit-tree` 経由で同等のテストを行った:

```bash
git fetch origin main
git update-ref refs/heads/test-direct-push-main-protection origin/main
TREE=$(git rev-parse refs/heads/test-direct-push-main-protection^{tree})
PARENT=$(git rev-parse refs/heads/test-direct-push-main-protection)
COMMIT=$(GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
         GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com \
         git commit-tree "$TREE" -p "$PARENT" -m "test: verify main direct push protection")
git update-ref refs/heads/test-direct-push-main-protection "$COMMIT"
git push origin refs/heads/test-direct-push-main-protection:refs/heads/main
# 期待: remote rejected — 失敗で終わる
git branch -D test-direct-push-main-protection
```

A と B は宛先 ref が同じ `refs/heads/main` であり、push 評価対象として GitHub remote 側は同一の保護 rule に照合する。

#### Stop Condition: 想定外の push 成功

上記 A / B のどちらの form でも、**push が成功した場合は即時 incident として扱う**:

1. 直ちに作業を停止し、追加の push / 変更を一切行わない
2. `origin/main` の HEAD を確認し、誤って進んだ commit を特定する
3. PR #358 と同様の手順で人間判断で revert PR を起票する
4. Ruleset (`16796903`) と Branch protection 設定を `gh api` で再取得し、`enforcement`, `bypass_actors`, `enforce_admins.enabled`, `required_status_checks` の差分を確認する
5. 本書の AC マッピングを再検証する

成功してしまった test commit は tree が同一であっても main 履歴を 1 commit 進める **main 汚染** であり、`origin/main` の前進が ruleset 違反の徴候となる。

### 2026-05-24 実測ログ（AC6 evidence）

```
remote: error: GH013: Repository rule violations found for refs/heads/main.
remote: Review all repository rules at https://github.com/squne121/loop-protocol/rules?ref=refs%2Fheads%2Fmain
remote:
remote: - Changes must be made through a pull request.
remote:
remote: - 5 of 5 required status checks are expected.
remote:
To https://github.com/squne121/loop-protocol.git
 ! [remote rejected] test-direct-push-main-protection -> main (push declined due to repository rule violations)
error: failed to push some refs to 'https://github.com/squne121/loop-protocol.git'
```

`origin/main` は push 試行前後で `2bd03a1` のまま不変。

## Acceptance Criteria 充足マッピング

| AC | 状態 | 根拠 |
|---|---|---|
| AC1: active ruleset / branch protection が存在 | OK | Ruleset `16796903` active + branch protection 設定済み |
| AC2: PR 経由必須 / 直接 push 不可 | OK | Ruleset `pull_request` rule + branch protection `required_pull_request_reviews` |
| AC3: required checks に 5 種が含まれる | OK | 両層に `typecheck/lint/test/build/python-test` 設定済み |
| AC4: conversation resolution 必須 | OK | Ruleset `required_review_thread_resolution: true` + branch protection `required_conversation_resolution.enabled: true` |
| AC5: admin / bypass actor 禁止 | OK | Ruleset `bypass_actors: []` (`current_user_can_bypass: never`) + branch protection `enforce_admins.enabled: true` + `bypass_pull_request_allowances` フィールド構造上不在（個人 repo） |
| AC6: 直接 push 拒否の実測 | OK | 「直接 push 拒否の実測」セクション A (`HEAD:main` form) / B (commit-tree form) と 2026-05-24 実測ログ参照 |
| AC7: snapshot と検証手順を docs に記録 | OK | 「2026-05-24 取得 raw snapshot」セクションに ruleset / branch protection の raw JSON、`bypass_pull_request_allowances` 確認結果、`required_status_checks.checks[].app_id` による source pinning を記録 |

## 補完策との関係

- **#360**: PreToolUse hook で local 側に `git push <branch>:main` 系を `exit 2` で止める SubAgent ガード。本書の GitHub remote 側保護と二重で防御する。本書が remote authoritative、#360 が local pre-flight。
- merge queue 導入、release / staging branch 保護、CI workflow 変更などは本書のスコープ外。

## 運用変更時の手順

1. 変更内容を別 Issue として起票する。
2. Ruleset / branch protection を `gh api` で更新後、本書の snapshot と AC マッピングを更新する。
3. 直接 push 拒否の実測ログを再度添付する。
