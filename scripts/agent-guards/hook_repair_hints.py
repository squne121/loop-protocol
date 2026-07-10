#!/usr/bin/env python3
"""Shared HOOK_COMMAND_REPAIR_HINT_V1 formatter."""

from __future__ import annotations


def build_hook_command_repair_hint(
    *,
    blocked_command_class: str,
    reason_code: str,
    suggested_command: str | None = None,
    verification_command: str | None = None,
    expected_remote_head: str | None = None,
    current_remote_head: str | None = None,
    local_head: str | None = None,
    verified_head: str | None = None,
    declared_publish_head: str | None = None,
    allowed_paths_gate_status: str | None = None,
    target_branch: str | None = None,
    pr_number: str | None = None,
    remote_readback_source: str | None = None,
    decision_inputs_complete: bool | None = None,
    required_decisions: tuple[str, ...] = (),
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
    elif reason_code == "git_add_outside_allowed_paths":
        safe_action = "Issue contract の Allowed Paths に含まれる file だけを add する"
        suggested_command = suggested_command or "rtk git add <allowed-path-file>"
        verification_command = verification_command or "git diff --name-only"
    elif reason_code == "allowed_paths_missing_for_git_mutation":
        safe_action = "Allowed Paths を runtime に束縛してから mutation を再実行する"
        suggested_command = suggested_command or "git diff --cached --name-only"
        verification_command = verification_command or "git diff --cached --name-only"
    elif reason_code == "commit_staged_changes_outside_allowed_paths":
        safe_action = "staged diff を Allowed Paths subset に戻してから commit する"
        suggested_command = suggested_command or 'rtk git commit -m "issue-1241 update"'
        verification_command = verification_command or "git diff --cached --name-only"
    elif reason_code == "push_refspec_requires_active_branch":
        safe_action = "PUBLISH_LANE_DECISION_V1 status=allow_retry の allowed_command だけを使う"
        suggested_command = suggested_command or ""
        verification_command = verification_command or (
            "git ls-remote --refs --exit-code origin refs/heads/<active-branch>"
        )
        stop_condition = (
            "expected_remote_head / current_remote_head / local_head / verified_head が一致しない場合は "
            "PUBLISH_SAFETY_STOP_REPORT_V1 を残して停止"
        )
    elif reason_code in {"publish_guard_context_missing", "publish_guard_context_invalid"}:
        safe_action = "publish lane の decision inputs を全て live readback 証跡付きで揃える"
        suggested_command = suggested_command or ""
        verification_command = verification_command or (
            "git ls-remote --refs --exit-code origin refs/heads/<branch>"
        )
        stop_condition = "decision_inputs_complete != true の場合は safety stop"
    elif reason_code == "allowed_paths_gate_not_ok":
        safe_action = "Allowed Paths gate を ok にできる current-head 証跡を取得する"
        suggested_command = suggested_command or ""
        verification_command = verification_command or "allowed_paths_review_gate.py status == ok"
        stop_condition = "allowed_paths_gate_status != ok の場合は safety stop"
    elif reason_code == "stale_remote_head":
        safe_action = (
            "fetch/readback 後に expected_remote_head と current_remote_head を照合し、"
            "一致時のみ bounded publish lane を再試行する"
        )
        verification_command = verification_command or (
            "uv run --locked python3 scripts/agent-ops/git_ref_probe.py "
            f"--branch {target_branch or '<branch>'} --remote origin --json"
        )
        stop_condition = "expected_remote_head != current_remote_head の場合は safety stop"
        forbidden.extend(
            [
                "git " + "push --force-with-lease",
                "bash -lc 'git " + "push ...'",
                "rtk run git " + "push ...",
            ]
        )
    elif reason_code == "local_head_mismatch":
        safe_action = "declared publish head と local head を再同期し、review 済み head 以外は publish しない"
        verification_command = verification_command or "git rev-parse HEAD"
        stop_condition = "local_head != declared_publish_head の場合は safety stop"
    elif reason_code == "remote_head_scope_contamination":
        safe_action = "remote head の追加 commit を分離し、linked issue 専用 head に戻す"
        verification_command = verification_command or "git rev-list <expected_remote_head>..<current_remote_head>"
        stop_condition = "remote head に scope 外 commit がある場合は safety stop"
    elif reason_code == "remote_fast_forward_by_same_scope":
        safe_action = "remote fast-forward 差分の scope を確認し、linked issue 専用 head へ再同期する"
        verification_command = verification_command or "git rev-list <expected_remote_head>..<current_remote_head>"
        stop_condition = "remote head が expected head より進んでいる場合は safety stop"
    elif reason_code == "non_fast_forward_remote_rewrite":
        safe_action = "remote rewrite の有無を human decision route で確認する"
        verification_command = verification_command or "git merge-base --is-ancestor <expected> <current>"
        stop_condition = "expected_remote_head が current_remote_head の ancestor でない場合は safety stop"
    elif reason_code == "issue_context_required":
        safe_action = "active issue を解決できる worktree/cwd でだけ mutation を行う"
        suggested_command = suggested_command or "git worktree list"
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
        "boundary_layer": "worktree_scope_guard_denied",
        "safe_action": safe_action,
        "suggested_command": suggested_command,
        "forbidden_alternatives": forbidden,
        "verification_command": verification_command,
        "stop_condition": stop_condition,
        "expected_remote_head": expected_remote_head,
        "current_remote_head": current_remote_head,
        "local_head": local_head,
        "verified_head": verified_head,
        "declared_publish_head": declared_publish_head,
        "allowed_paths_gate_status": allowed_paths_gate_status,
        "target_branch": target_branch,
        "pr_number": pr_number,
        "remote_readback_source": remote_readback_source,
        "decision_inputs_complete": decision_inputs_complete,
        "required_decisions": list(required_decisions),
    }


def render_hook_command_repair_hint(
    *,
    blocked_command_class: str,
    reason_code: str,
    suggested_command: str | None = None,
    verification_command: str | None = None,
    expected_remote_head: str | None = None,
    current_remote_head: str | None = None,
    local_head: str | None = None,
    verified_head: str | None = None,
    declared_publish_head: str | None = None,
    allowed_paths_gate_status: str | None = None,
    target_branch: str | None = None,
    pr_number: str | None = None,
    remote_readback_source: str | None = None,
    decision_inputs_complete: bool | None = None,
    required_decisions: tuple[str, ...] = (),
) -> list[str]:
    hint = build_hook_command_repair_hint(
        blocked_command_class=blocked_command_class,
        reason_code=reason_code,
        suggested_command=suggested_command,
        verification_command=verification_command,
        expected_remote_head=expected_remote_head,
        current_remote_head=current_remote_head,
        local_head=local_head,
        verified_head=verified_head,
        declared_publish_head=declared_publish_head,
        allowed_paths_gate_status=allowed_paths_gate_status,
        target_branch=target_branch,
        pr_number=pr_number,
        remote_readback_source=remote_readback_source,
        decision_inputs_complete=decision_inputs_complete,
        required_decisions=required_decisions,
    )
    return [
        "HOOK_COMMAND_REPAIR_HINT_V1:",
        f'  blocked_command_class: "{hint["blocked_command_class"]}"',
        f'  boundary_layer: "{hint["boundary_layer"]}"',
        f'  reason_code: "{hint["reason_code"]}"',
        f'  safe_action: "{hint["safe_action"]}"',
        f'  suggested_command: "{hint["suggested_command"] or ""}"',
        f'  forbidden_alternatives: {hint["forbidden_alternatives"]}',
        f'  verification_command: "{hint["verification_command"] or ""}"',
        f'  stop_condition: "{hint["stop_condition"]}"',
        f'  expected_remote_head: "{hint["expected_remote_head"] or ""}"',
        f'  current_remote_head: "{hint["current_remote_head"] or ""}"',
        f'  local_head: "{hint["local_head"] or ""}"',
        f'  verified_head: "{hint["verified_head"] or ""}"',
        f'  declared_publish_head: "{hint["declared_publish_head"] or ""}"',
        f'  allowed_paths_gate_status: "{hint["allowed_paths_gate_status"] or "indeterminate"}"',
        f'  remote_readback_source: "{hint["remote_readback_source"] or ""}"',
        f'  decision_inputs_complete: {str(bool(hint["decision_inputs_complete"])).lower()}',
    ]


def render_publish_safety_stop_report(
    *,
    issue_number: str,
    blocked_command_class: str,
    reason_code: str,
    target_branch: str,
    expected_remote_head: str | None,
    current_remote_head: str | None,
    local_head: str | None,
    verified_head: str | None,
    declared_publish_head: str | None,
    allowed_paths_gate_status: str | None,
    pr_number: str | None = None,
    remote_readback_source: str | None = None,
    decision_inputs_complete: bool | None = None,
    required_decisions: tuple[str, ...] = (),
) -> list[str]:
    redacted_command = "rtk git " + f"push origin HEAD:refs/heads/{target_branch}"
    required_decision_lines = list(required_decisions) or ["人間判断が必要"]
    return [
        "PUBLISH_SAFETY_STOP_REPORT_V1:",
        '  status: "safety_stop"',
        f"  issue_number: {issue_number}",
        f'  pr_number: "{pr_number or ""}"',
        f'  redacted_command: "{redacted_command}"',
        '  boundary_layer: "worktree_scope_guard_denied"',
        f'  reason_code: "{reason_code}"',
        f'  expected_remote_head: "{expected_remote_head or ""}"',
        f'  current_remote_head: "{current_remote_head or ""}"',
        f'  local_head: "{local_head or ""}"',
        f'  verified_head: "{verified_head or ""}"',
        f'  declared_publish_head: "{declared_publish_head or ""}"',
        f'  allowed_paths_gate_status: "{allowed_paths_gate_status or "indeterminate"}"',
        f'  remote_readback_source: "{remote_readback_source or ""}"',
        f"  decision_inputs_complete: {str(bool(decision_inputs_complete)).lower()}",
        f"  required_decision: {required_decision_lines}",
        f'  blocked_command_class: "{blocked_command_class}"',
    ]
