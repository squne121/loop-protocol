Issue #2923 runtime smoke (expect-marker quoted literal).
<!-- このタイトル行は Issue #2923 の runtime smoke を説明する日本語注記である。 -->

This is a harmless smoke test. It does not modify any file, does not use the network, does not run git, and does not run `uv run`. Its only purpose is to confirm that a `--expect-marker` literal containing double quotes is observed in the child's own final message and is accepted as marker provenance in the same evidence.
<!-- この段落は file 変更・network・git・uv run を一切伴わない無害な検証であり、二重引用符を含む marker が子 SubAgent 自身の最終 message から観測され、同一 evidence 内で由来も成立することだけを確認する旨を説明する日本語注記である。 -->

Use the `Agent` tool exactly once to launch the `test-runner` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で起動して test-runner を呼び出し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
This is a read-only smoke check with no Issue contract. Run only the single allowed command `echo expect-marker-quoted-smoke` and do not run any other command. Then give your normal short report. After the report, end your final message with exactly these two lines, each on its own line, copied character for character including the double quotes, and nothing after them:
quoted_marker_probe: "ok-2923"
status_probe: "pass"
```

After the SubAgent has completed and returned its final response, copy that full final response text verbatim into your own final message. Do not summarize, paraphrase, or omit the two literal lines, and do not add anything of your own after them.
<!-- この段落は SubAgent の最終応答を逐語で転記し、二重引用符つきの 2 行を要約・省略・追記しない要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、親自身では command を実行しない制約を説明する日本語注記である。 -->

## Operator-only: expected marker list
<!-- この節は operator lane 専用の節であり、子 SubAgent への message には含まれず、親 session も実行や転記の対象にしてはならない旨を示す日本語注記である。 -->

This section is NOT part of the child message and is not an instruction to the parent session. Ignore it when running the smoke. The operator passes each literal below (one per line, without the surrounding fence) to the runner as a separate `--expect-marker` argument:
<!-- この段落は直下のコードブロックが operator が --expect-marker に 1 行ずつ渡す literal の一覧であり、子 message の一部ではないことを説明する日本語注記である。 -->

```text
quoted_marker_probe: "ok-2923"
status_probe: "pass"
```
