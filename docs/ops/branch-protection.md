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
- `enforce_admins.enabled`: `true`（admin bypass を禁止）
- `required_pull_request_reviews.dismiss_stale_reviews`: `true`
- `required_pull_request_reviews.required_approving_review_count`: `0`（PR フローは必須だが承認件数は 0。reviewer は SubAgent / 人間の運用判断で運用）
- `required_conversation_resolution.enabled`: `true`
- `required_linear_history.enabled`: `true`
- `allow_force_pushes.enabled`: `false`
- `allow_deletions.enabled`: `false`

## 検証手順

### 設定 snapshot の取得

```bash
# Ruleset
gh api repos/squne121/loop-protocol/rulesets --jq '.[] | {id, name, enforcement, target}'
gh api repos/squne121/loop-protocol/rulesets/16796903

# Branch protection
gh api repos/squne121/loop-protocol/branches/main/protection \
  --jq '{required_status_checks, required_pull_request_reviews, required_conversation_resolution, enforce_admins, restrictions, allow_force_pushes, allow_deletions, required_linear_history}'
```

### 直接 push 拒否の実測

```bash
# ローカル test ブランチで origin/main に直接 push を試みる
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
| AC5: admin / bypass actor 禁止 | OK | Ruleset `bypass_actors: []` (`current_user_can_bypass: never`) + branch protection `enforce_admins.enabled: true` |
| AC6: 直接 push 拒否の実測 | OK | 上記「直接 push 拒否の実測」ログ参照 |
| AC7: snapshot と検証手順を docs に記録 | OK | 本文書 |

## 補完策との関係

- **#360**: PreToolUse hook で local 側に `git push <branch>:main` 系を `exit 2` で止める SubAgent ガード。本書の GitHub remote 側保護と二重で防御する。本書が remote authoritative、#360 が local pre-flight。
- merge queue 導入、release / staging branch 保護、CI workflow 変更などは本書のスコープ外。

## 運用変更時の手順

1. 変更内容を別 Issue として起票する。
2. Ruleset / branch protection を `gh api` で更新後、本書の snapshot と AC マッピングを更新する。
3. 直接 push 拒否の実測ログを再度添付する。
