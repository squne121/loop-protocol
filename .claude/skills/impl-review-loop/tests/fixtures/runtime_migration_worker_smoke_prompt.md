Issue #2810 AC9 runtime smoke (implementation-worker `apply_runtime_migration_fix_delta` mode).

This is a hermetic smoke test. `CLAUDE_GPT_HOME` and `CLAUDE_GPT_REPAIR_INSTALLER_URL` are
already set in your process environment (by the harness) to a fixture-only home directory and a
`file://` fixture installer -- neither touches the operator's real `~/.claude-gpt`.

Use the Task tool to launch the `implementation-worker` SubAgent with exactly this message:

```
Objective: execute the apply_runtime_migration_fix_delta mode (Issue #2810) for a hermetic
fixture.

IMPLEMENTATION_WORKER_REQUEST_V2:
  mode: apply_runtime_migration_fix_delta
  issue_url: https://github.com/squne121/loop-protocol/issues/2810
  repair_command: "bash scripts/claude-gpt/repair_proxy.sh"
  expected_claude_gpt_home: <the exact absolute path currently in your CLAUDE_GPT_HOME
    environment variable>
  pre_repair_evidence_ref: "runtime-smoke-ac9-fixture"

Follow `.claude/agents/implementation-worker.md`'s `apply_runtime_migration_fix_delta mode`
section exactly:
  - Verify repair_command matches the literal `bash scripts/claude-gpt/repair_proxy.sh` exactly
    (no added arguments). If it does not match, return status: blocked / reason_code:
    command_mismatch and stop.
  - Run exactly that command, non-interactively (stdin from /dev/null), inheriting the current
    process environment (do not unset or override CLAUDE_GPT_HOME /
    CLAUDE_GPT_REPAIR_INSTALLER_URL -- the fixture depends on them).
  - Do not edit, create, or delete any repository-tracked file. Do not run any other command.
  - After the repair command finishes, report IMPLEMENTATION_WORKER_RESULT_V2 with mode:
    apply_runtime_migration_fix_delta, the runtime_migration sub-object (exit_code,
    claude_gpt_repair_proxy_result_v1_status, installed_path, installed_version,
    actual_claude_gpt_home, install_log_tail, sudo_required), and rerun_required.verification:
    true.
  - Also print the RUNTIME_MIGRATION_RESULT_V1 marker block exactly as specified in
    implementation-worker.md's apply_runtime_migration_fix_delta mode section, with status: ok
    on success.

Expected result: RUNTIME_MIGRATION_RESULT_V1 with status: ok, and rerun_required.verification:
true.
```

After the SubAgent returns, print its full final response text verbatim in your own final
message (so the runner's transcript capture can locate the `RUNTIME_MIGRATION_RESULT_V1
status=ok`, `rerun_required.verification=true`, and installed-path markers). Do not summarize,
paraphrase, or omit any of those literal strings.

Do not modify any repository-tracked file yourself. Do not run any command other than what you
instruct the SubAgent to run via the Task tool.
