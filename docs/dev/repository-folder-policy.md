# Repository Folder Policy

repo 直下や `.claude/` 配下で AI エージェントが扱う一時フォルダの正本。
未承認 alias を advisory-only hook で誘導し、cleanup authority の運用ルールを deterministic に定義する。
root temporary residue の read-only 分類は `scripts/agent-ops/temp_residue_classifier.py`
（`temp_residue_classification/v1`、Issue #1417）が実装済みだが、これは read-only な advisory
observation にとどまる。「ownership marker 付き session subdirectory だけを削除候補にできる」
「marker 不明 residue は report-only」という運用制約は、実削除 executor が別途実装されるまで変わらない。

## REPOSITORY_FOLDER_POLICY_V1

```yaml
REPOSITORY_FOLDER_POLICY_V1:
  - path: "tmp/"
    folder_class: repo_approved_temporary_workspace
    git_tracking: ignored
    lifecycle: per_session_or_per_issue
    cleanup_authority: delete_only_owned_session_subdirectory_or_report
    publication_rule: never_publish_without_explicit_whitelist
    guidance: repo-approved local temporary workspace

  - path: ".claude/tmp/"
    folder_class: repo_approved_temporary_workspace
    git_tracking: ignored
    lifecycle: per_session_or_per_issue
    cleanup_authority: delete_only_owned_session_subdirectory_or_report
    publication_rule: never_publish_without_explicit_whitelist
    guidance: repo-approved local temporary workspace

  - path: ".claude/worktrees/"
    folder_class: managed_worktree_root
    git_tracking: ignored
    lifecycle: per_issue
    cleanup_authority: cleanup_exec_only
    publication_rule: never_publish
    guidance: managed worktree root

  - path: ".tmp/"
    folder_class: root_temporary_alias
    git_tracking: untracked_residue
    lifecycle: cleanup_required_or_report_only
    cleanup_authority: report_only_without_owned_marker
    publication_rule: never_publish
    guidance: advisory_only_replace_with_tmp

  - path: ".temp/"
    folder_class: root_temporary_alias
    git_tracking: untracked_residue
    lifecycle: cleanup_required_or_report_only
    cleanup_authority: report_only_without_owned_marker
    publication_rule: never_publish
    guidance: advisory_only_replace_with_tmp

  - path: ".tmp-*/"
    folder_class: root_temporary_alias
    git_tracking: untracked_residue
    lifecycle: cleanup_required_or_report_only
    cleanup_authority: report_only_without_owned_marker
    publication_rule: never_publish
    guidance: advisory_only_replace_with_tmp
```

## Root Class / Marker Effect Matrix（Issue #1417）

`temp_residue_classifier.py` が各 root class・child の `recommendation` をどう決定するかの正本表。
`eligible_for_delete` は常に advisory であり、削除権限そのものではない。

| Root class | Root 自体 | valid marker child | marker 不明 child |
|---|---|---|---|
| `tmp/` | report_only | eligible_for_delete | report_only |
| `.claude/tmp/` | report_only | eligible_for_delete | report_only |
| `.tmp/` | report_only | report_only（policy） | report_only |
| `.temp/` | report_only | report_only（policy） | report_only |
| `.tmp-*/` | report_only | report_only（policy） | report_only |
| `.claude/worktrees/` | cleanup_exec のみ | classifier 対象外 | classifier 対象外 |

denied alias（`.tmp/` `.temp/` `.tmp-*/`）配下は、有効な `temp_residue_owner/v1` marker があっても
初期実装では常に `report_only` とする。自動削除候補化は approved roots（`tmp/` / `.claude/tmp/`）配下の
明示的な session directory layout に限定する。

## 運用ルール

- `.tmp/`、`.temp/`、`.tmp-*` は作業を block しない。ただし hook は `REPO_TEMP_FOLDER_ADVICE_V1` を返し、`tmp/` または `.claude/tmp/` への移行を案内する。
- `tmp/` と `.claude/tmp/` は repo-approved local temporary workspace として使えるが、終了時に自分の session subdirectory を削除するか、残置理由を報告する。owned session subdirectory かどうかは `temp_residue_owner/v1` ownership marker（`scripts/agent-ops/temp_residue_marker.py`）で判定できる。
- `.claude/worktrees/` は managed worktree root であり、agent が ad hoc temporary workspace の代替として使わない。cleanup は `cleanup_exec.py` の認可境界に限定する。
- root temporary residue の read-only 分類は `scripts/agent-ops/temp_residue_classifier.py`（`temp_residue_classification/v1`）が担う。ownership marker 不明の `.tmp/**` / `.temp/**` / `.tmp-*/**` は report-only とし、classifier 自体は削除を実行しない。実削除 executor は別 scope。
- deploy/release/preview artifact へ temporary folder を含めたい場合は、この文書と consumer docs を同一 PR で更新し、別 issue で publication rule を明示する。

## フォルダ運用変更の変更動線

この folder policy を変更する PR では、関連する定義と運用文書の整合を崩さないため、次の更新を同じ変更セットでそろえる。

1. この `docs/dev/repository-folder-policy.md`
2. hook registration と advisory/blocker docs。hook の案内文や判定条件が policy とずれないようにする。
3. `schemas/` と producer/tests。機械可読な定義と検証系も同じ policy に追従させる。
4. cleanup skill / related operator docs。cleanup 実行者が参照する手順書や運用説明も同時に更新する。
