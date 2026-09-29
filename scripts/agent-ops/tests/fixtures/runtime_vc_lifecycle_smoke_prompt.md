Issue #2839 AC10 runtime smoke (runner lifecycle non-regression).
<!-- このタイトル行は Issue #2839 の AC10 runtime smoke を説明する日本語注記である。 -->

This is a harmless smoke test. It does not use the approval carrier, does not run any external code,
and does not mutate any file. Its only purpose is to confirm that the runner still observes the
SubagentStart and SubagentStop lifecycle events for one child SubAgent after the runner was changed.
<!-- この段落は承認 carrier を使わない無害な検証であり、runner の SubAgent 起動と停止の観測が退行していないことだけを確認する旨を説明する日本語注記である。 -->

Use the Task tool to launch the `pr-reviewer` SubAgent with exactly this message:
<!-- 次のコードブロックは SubAgent へ送信する指示文そのものであり、内容は変更しないこと。 -->

```
Do not run any tool. Reply with exactly one line and nothing else: RUNTIME_VC_LIFECYCLE_SMOKE_OK
```

After the SubAgent returns, print its full final response text verbatim in your own final message,
so the runner's transcript capture can locate the marker `RUNTIME_VC_LIFECYCLE_SMOKE_OK`.
Do not summarize, paraphrase, or omit that literal string.
<!-- この段落は SubAgent の最終応答をそのまま転記する要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、自分で command も実行しない制約を説明する日本語注記である。 -->
