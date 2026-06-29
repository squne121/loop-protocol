#!/usr/bin/env python3
"""Shared HOOK_COMMAND_REPAIR_HINT_V1 formatter."""

from __future__ import annotations


def build_hook_command_repair_hint(
    *,
    blocked_command_class: str,
    reason_code: str,
    suggested_command: str | None = None,
    verification_command: str | None = None,
) -> dict[str, object]:
    safe_action = "linked issue worktree と allow 済みの exact command shape を確認する"
    forbidden = ["git add .", "git add -A", "git push --force"]
    stop_condition = "safe な single command に直せない場合は人間判断"

    if reason_code == "direct_bypass_requires_rtk":
        safe_action = "direct git ではなく issue worktree 内の rtk git command に切り替える"
        suggested_command = suggested_command or "rtk git add <allowed-path-file>"
    elif reason_code == "rtk_unknown_inner":
        safe_action = "allow 済みの rtk git add/commit/push の exact shape に揃える"
        suggested_command = suggested_command or "rtk git add <allowed-path-file>"
    elif reason_code == "git_add_requires_explicit_pathspec":
        safe_action = "broad pathspec をやめて 1 file 単位の pathspec を使う"
        suggested_command = suggested_command or "rtk git add <allowed-path-file>"
        verification_command = verification_command or "git diff --name-only"
    elif reason_code == "push_refspec_requires_active_branch":
        safe_action = "active branch と一致する refspec だけを使う"
        suggested_command = suggested_command or "rtk git push origin HEAD:refs/heads/<active-branch>"
        verification_command = verification_command or "git branch --show-current"
    elif reason_code == "target_dir_outside_worktree":
        safe_action = "active issue worktree の配下だけを対象にする"
        suggested_command = suggested_command or "git status --short"
    elif reason_code in {"no_matching_worktree", "ambiguous_worktree"}:
        safe_action = "issue worktree を 1 つに特定してから再実行する"
        suggested_command = suggested_command or "git worktree list"
    elif reason_code == "rtk_git_commit_requires_message":
        safe_action = "bounded な commit message 付きで再実行する"
        suggested_command = suggested_command or 'rtk git commit -m "issue-1241 update"'
        verification_command = verification_command or "git diff --cached --name-only"

    return {
        "blocked_command_class": blocked_command_class,
        "reason_code": reason_code,
        "safe_action": safe_action,
        "suggested_command": suggested_command,
        "forbidden_alternatives": forbidden,
        "verification_command": verification_command,
        "stop_condition": stop_condition,
    }


def render_hook_command_repair_hint(
    *,
    blocked_command_class: str,
    reason_code: str,
    suggested_command: str | None = None,
    verification_command: str | None = None,
) -> list[str]:
    hint = build_hook_command_repair_hint(
        blocked_command_class=blocked_command_class,
        reason_code=reason_code,
        suggested_command=suggested_command,
        verification_command=verification_command,
    )
    return [
        "HOOK_COMMAND_REPAIR_HINT_V1:",
        f'  blocked_command_class: "{hint["blocked_command_class"]}"',
        f'  reason_code: "{hint["reason_code"]}"',
        f'  safe_action: "{hint["safe_action"]}"',
        f'  suggested_command: "{hint["suggested_command"] or ""}"',
        f'  forbidden_alternatives: {hint["forbidden_alternatives"]}',
        f'  verification_command: "{hint["verification_command"] or ""}"',
        f'  stop_condition: "{hint["stop_condition"]}"',
    ]
