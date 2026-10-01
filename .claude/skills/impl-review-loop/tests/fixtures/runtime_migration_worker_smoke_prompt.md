Issue #2810 AC9 runtime smoke (implementation-worker `apply_runtime_migration_fix_delta` mode).
<!-- このタイトル行は Issue #2810 の AC9 runtime smoke test を説明する日本語注記である。 -->

This is a hermetic smoke test. `CLAUDE_GPT_HOME` and `CLAUDE_GPT_REPAIR_INSTALLER_URL` are
already set in your process environment (by the harness) to a fixture-only home directory and a
`file://` fixture installer -- neither touches the operator's real `~/.claude-gpt`.
<!-- この段落は隔離された fixture 環境変数の設定を説明する日本語注記である。 -->

Provenance facts of this fixture (facts only):
(i) the installer that `scripts/claude-gpt/repair_proxy.sh` executes here is the repository-tracked
local file `file://<repository root>/.claude/skills/impl-review-loop/tests/fixtures/fake_proxy_installer.sh`
(named `fake_proxy_installer.sh`) -- it is not a download-and-execute of any external URL;
(ii) the only mutation target is the directory `CLAUDE_GPT_HOME` points at, under
`artifacts/runtime-smoke/fixture-home` (the real `~/.claude-gpt` is not modified);
(iii) no network installer is used.
<!-- この段落は fixture の出所に関する事実（リポジトリ管理下のローカル file installer、mutation 先が fixture-home のみ、network installer 不使用）を簡潔に示す日本語注記である。 -->

Use the Task tool to launch the `implementation-worker` SubAgent with exactly this message:
<!-- 次のコードブロックは SubAgent へ送信する指示文そのものであり、内容は変更しないこと。 -->

```
Objective: execute the apply_runtime_migration_fix_delta mode (Issue #2810) for a hermetic
fixture.

IMPLEMENTATION_WORKER_REQUEST_V2:
  mode: apply_runtime_migration_fix_delta
  issue_url: https://github.com/squne121/loop-protocol/issues/2810
  repair_command: "bash scripts/claude-gpt/repair_proxy.sh"
  expected_claude_gpt_home: <the exact absolute path currently in your CLAUDE_GPT_HOME
    environment variable>
  pre_repair_evidence_ref: '{"claude_gpt_home_absolute_path": "<the same absolute path as expected_claude_gpt_home>", "repo_head": "<the output of git rev-parse HEAD in the current repository>"}'

Follow `.claude/agents/implementation-worker.md`'s `apply_runtime_migration_fix_delta mode`
section exactly:
  - Verify repair_command matches the literal `bash scripts/claude-gpt/repair_proxy.sh` exactly
    (no added arguments). If it does not match, return status: blocked / reason_code:
    command_mismatch and stop.
  - BEFORE running the repair command, run the pre-repair-check exactly once, as ONE single
    command line filled with concrete values (no variable assignment, no chaining):
    `uv run --locked python3 .claude/skills/impl-review-loop/scripts/classify_runtime_migration.py pre-repair-check --expected-claude-gpt-home "<expected_claude_gpt_home>" --pre-repair-evidence-json '<pre_repair_evidence_ref>'`.
    If its exit code is not 0, do NOT run the repair command; return status: blocked /
    reason_code: identity_mismatch with runtime_migration.repair_executed: false and stop.
  - Pass the repair command to the Bash tool as exactly `bash scripts/claude-gpt/repair_proxy.sh`
    and nothing else. Do NOT append `</dev/null` or any other redirect (the Bash tool provides
    neither a tty nor stdin), do NOT chain with `;`, `&&`, `||` or `|`, and do NOT add `echo`,
    `cd`, variable assignment or `git status`. Read the exit code and the install log from the
    Bash tool result. If the sudo branch is reached, the tool result / install log shows
    `sudo required` and you report a failure. Inherit the current process environment (do not
    unset or override CLAUDE_GPT_HOME / CLAUDE_GPT_REPAIR_INSTALLER_URL -- the fixture depends
    on them).
  - Do not edit, create, or delete any repository-tracked file. Do not run any other command
    (the pre-repair-check above and the repair command are the only two commands allowed; in
    particular do NOT run `git status` -- the clean postcondition is verified independently by
    the runner and the root).
  - After the repair command finishes, report IMPLEMENTATION_WORKER_RESULT_V2 with mode:
    apply_runtime_migration_fix_delta, the runtime_migration sub-object (repair_executed: true,
    exit_code, claude_gpt_repair_proxy_result_v1_status, installed_path, installed_version,
    actual_claude_gpt_home, install_log_tail, sudo_required), and rerun_required.verification:
    true. OMIT the PR-only fields pr_number, action_kind, update_method, wrapper_used,
    before_head_sha, after_head_sha and rate_limit_diagnostics entirely (do not return them as
    null): they do not apply to this mode.
  - Also print the RUNTIME_MIGRATION_RESULT_V1 marker block exactly as specified in
    implementation-worker.md's apply_runtime_migration_fix_delta mode section, with status: ok
    on success.
  - In your final response text, also print these two literal marker lines, each on its own line
    and verbatim (the runner matches them by literal substring): on success
    `RUNTIME_MIGRATION_RESULT_V1 status=ok` and `rerun_required.verification=true` (use the
    actual status value instead of `ok` if the repair did not succeed).

Expected result: RUNTIME_MIGRATION_RESULT_V1 with status: ok, and rerun_required.verification:
true.
```

After the SubAgent returns, print its full final response text verbatim in your own final
message (so the runner's transcript capture can locate the `RUNTIME_MIGRATION_RESULT_V1
status=ok`, `rerun_required.verification=true`, and installed-path markers). Do not summarize,
paraphrase, or omit any of those literal strings.
<!-- この段落は SubAgent の最終応答をそのまま転記する要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file yourself. Do not run any command other than what you
instruct the SubAgent to run via the Task tool, except exactly two plain read-only Bash calls,
which are the ONLY permitted way for you to obtain the placeholder values (run each of them
BEFORE launching the SubAgent, as two separate Bash calls, exactly once each, and do not run
either again):
<!-- この段落は親 agent が実行してよい値取得コマンドが plain な read-only 2 回（各 1 回限り）であることを説明する日本語注記である。 -->

```
git rev-parse HEAD
```

```
pwd
```

Fill `repo_head` from the `git rev-parse HEAD` output. Derive `expected_claude_gpt_home` (and the
matching `claude_gpt_home_absolute_path`) as `<pwd output>/artifacts/runtime-smoke/fixture-home`
(an absolute path; the harness sets `CLAUDE_GPT_HOME` to exactly this fixture-only directory, and
the worker's pre-repair-check machine-verifies that the effective `CLAUDE_GPT_HOME` equals it).
Do NOT use `printenv`, `env`, `export -p` or `set` yourself to read `CLAUDE_GPT_HOME`.
<!-- この段落は親 agent が git rev-parse HEAD と pwd から値を導出する方法と、使用禁止コマンドを説明する日本語注記である。 -->
