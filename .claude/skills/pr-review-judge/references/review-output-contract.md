# Review Output Contract

## Review Target

linked issue の contract と PR evidence を照合し、APPROVE または REQUEST_CHANGES を返す。self-authored PR では `gh pr review --comment` で verdict を記録する。

## Skill Intent Summary

`pr-review-judge` は GitHub surface に verdict を残す review skill である。canonical surface は self-authored PR では `gh pr review --comment`、他者の PR では `gh pr review --approve` / `--request-changes` である。

## Findings

- CI が fail の場合は `REQUEST_CHANGES`（CI fail は常に blocker）
- CI が pass の場合は非 CI 観点のみで APPROVE / REQUEST_CHANGES を判定する
- CI が `pending` のまま最終判定を出さない（`--watch` で完了まで待つ）
- blocker がある場合は `REQUEST_CHANGES`
- blocker がない場合は `APPROVE`

## Realization Path Assessment

- self-authored PR では `gh pr review --comment` を canonical surface とする
- 他者の PR では `gh pr review --approve` / `--request-changes` を使う

## Required Edits

- PR 本文の `Acceptance Criteria -> Evidence` を更新する
- `Commands Run` と `Changed Paths` を埋める

## Open Questions

- なし

## Validation Commands

- `gh pr review <PR番号> --comment --body "## Verdict: APPROVE ..."` で self-authored PR の verdict を記録する
- `gh pr review <PR番号> --comment --body "## Verdict: REQUEST_CHANGES ..."` で self-authored PR の verdict を記録する
- `gh pr review <PR番号> --approve --body "## Verdict: APPROVE ..."` で他者の PR を承認する
- `gh pr review <PR番号> --request-changes --body "## Verdict: REQUEST_CHANGES ..."` で他者の PR に変更を要求する

## Executable Handoff Commands

- `gh pr view <PR番号> --json body,files,comments`
- `gh pr diff <PR番号>`
- `gh pr review <PR番号> --comment --body "## Verdict: APPROVE ..."`
- `gh pr checks <PR番号>`
- `gh pr checks <PR番号> --watch`

## Handoff Prompt Draft

- `linked issue` `AC coverage` `Allowed Paths` `Verification` `Changed Paths` を GitHub surface に残す

## Gate Decision

- APPROVE または REQUEST_CHANGES（いずれも self-authored PR では `gh pr review --comment` で記録）
