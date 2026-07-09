# Repository Folder Policy

repo 直下や `.claude/` 配下で AI エージェントが扱う一時フォルダの正本。
未承認 alias を advisory-only hook で誘導し、cleanup authority を deterministic に保つ。

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

## 運用ルール

- `.tmp/`、`.temp/`、`.tmp-*` は作業を block しない。ただし hook は `REPO_TEMP_FOLDER_ADVICE_V1` を返し、`tmp/` または `.claude/tmp/` への移行を案内する。
- `tmp/` と `.claude/tmp/` は repo-approved local temporary workspace として使えるが、終了時に自分の session subdirectory を削除するか、残置理由を報告する。
- `.claude/worktrees/` は managed worktree root であり、agent が ad hoc temporary workspace の代替として使わない。cleanup は `cleanup_exec.py` の認可境界に限定する。
- deploy/release/preview artifact へ temporary folder を含めたい場合は、この文書と consumer docs を同一 PR で更新し、別 issue で publication rule を明示する。

## フォルダ運用変更の変更動線

folder policy を変える PR では次を同時に更新する。

1. この `docs/dev/repository-folder-policy.md`
2. hook registration と advisory/blocker docs
3. `schemas/` と producer/tests
4. cleanup skill / related operator docs

