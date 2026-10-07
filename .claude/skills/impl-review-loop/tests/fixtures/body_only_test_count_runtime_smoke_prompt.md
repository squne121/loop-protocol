Issue #2971 runtime smoke (test-runner が pytest 系 command の行に command 固有の test_count を返すことの delegation smoke の prompt).
<!-- このタイトル行は Issue #2971 の subagent-delegation runtime smoke を説明する日本語注記である。実 test-runner SubAgent へ無害な pytest 系 fixture command を 1 件委譲し、返却 report の該当行に command 固有の test_count が現れることを確認する。 -->

This is a harmless synthetic smoke. It does not use the network, does not call GitHub, does not run `git`, and does not write or mutate any file or any GitHub object.
Its only purpose is to confirm that a delegated `test-runner` SubAgent runs one pytest fixture command and returns a TEST_VERDICT_MACHINE/v2 report whose row for that command carries the optional `test_count` field, derived mechanically as defined in its agent definition.
<!-- この段落は、ネットワーク・GitHub・git・書込みを一切使わない合成 smoke であること、test-runner が pytest 系 fixture command を 1 件実行し、agent 定義で定めた機械的な導出規則に従って、その行に optional field の test_count を持つ report を返すことを確認する旨を説明する日本語注記である。 -->

Use the `Agent` tool exactly once, with `subagent_type: "test-runner"`, to launch the `test-runner` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で subagent_type が test-runner の SubAgent として起動し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
Objective: execute the single fixture Verification Command below as an independent read-only report, and return your final TEST_VERDICT_MACHINE/v2 report.
Fixture binding values (synthetic smoke values, not a real PR; do not call gh and do not run mergeable detection): issue_number 2971, pr_number 1, head_sha 0123456789abcdef0123456789abcdef01234567, reviewed_head_sha 0123456789abcdef0123456789abcdef01234567, diff_head_sha 0123456789abcdef0123456789abcdef01234567, contract_body_sha256 sha256:0000000000000000000000000000000000000000000000000000000000000000.
AC list: AC10.
Per-command (ac, command, command_hash) tuple. Echo the ac label, command and command_hash verbatim into runtime_ac_results (one command = one row, and never compute or guess a command_hash):
1. ac: AC10 | command: uv run --locked pytest .claude/skills/impl-review-loop/tests/test_label_authority_invariants.py -q | command_hash: sha256:eb38109438bcb9b96499e7d721b96ba4aa481bb0e76686c6a1768aa7ff8cd88d
Run the command verbatim exactly once. Right before you write the report, obtain generated_at by running exactly `date -u +%Y-%m-%dT%H:%M:%SZ` and put its output into generated_at as a quoted string. Never guess or reuse a fixed time.
Return the report in the "machine-valid example" form of your agent definition: a single yaml block starting with TEST_VERDICT:, no placeholders, strings quoted EXCEPT `schema` and `result`, which must be bare (unquoted) scalars exactly as in the machine-valid example (`schema: TEST_VERDICT_MACHINE/v2`, `result: PASS`), booleans as true/false, exit_code as an integer, result PASS only if the command passed, and no GitHub-derived field (no producer_kind, repository, run_id, run_url, workflow_run_id, workflow_run_attempt, check_run_id, artifact).
Because this command is a pytest command that passed, apply the `test_count` derivation rule of your agent definition to its row: add one extra line to that row, written in flow style on a single line, with the pytest target path copied from the command as `subject` and the integer N copied from the `<N> passed` of the final pytest summary line as `passed`, in the form `test_count: {subject: "<pytest target path>", passed: <N>}`. Never derive it from anything other than the real output of this run.
Do not run any other command, and do not modify any file.
```
<!-- このコードブロックは子 test-runner SubAgent へ送る英語の指示文である。合成の binding 値、1 件の (ac, command, command_hash) の組、date の exact 形による generated_at 取得、machine-valid example の形での report 返却を指示し、さらに pytest 系 command が pass した行へ agent 定義の導出規則どおりに test_count を 1 行の flow style で追加させる。件数は実際の実行出力だけから写す。 -->

After the SubAgent has completed and returned its final response, write your own final message as the SubAgent's full final response text copied verbatim, and nothing else.
Do not summarize, paraphrase, reformat, or omit any line of the report, and do not add any text of your own.
<!-- この段落は、SubAgent の最終応答を要約・言い換え・整形・省略せず、そのまま最終 message として転記し、自分の文章を追加しない要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、親自身は command を実行しない制約を説明する日本語注記である。 -->
