# required_auto_actions ルール

## 自動実行対象（mechanical true）

- `ensure_closing_keyword`: PR 本文に GitHub official closing keyword が無い
- `update_pr_body_hygiene`: PR 本文セクション欠落 / placeholder
- `update_branch`: `MERGEABLE` かつ `BEHIND`

## merge_ready 反映

`required_auto_actions` が 1 件でもある場合は `merge_ready: false`。

`follow_up_issue_requests` は `blocking_merge_ready: false` のみ。
