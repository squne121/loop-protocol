Issue #2848 runtime smoke (SubagentStop handbackReport marker provenance).
<!-- このタイトル行は Issue #2848 の runtime smoke を説明する日本語注記である。 -->

This is a harmless smoke test. It does not run any external code, does not use the network,
and does not mutate any file. Its only purpose is to confirm that the runner's SubAgent causal
evidence stays hook_id_correlated even when the SubagentStop hook payload has no
last_assistant_message, because the marker is recovered from the Agent tool handbackReport.
<!-- この段落は外部 code・network・file 変更を伴わない無害な検証であり、SubagentStop に last_assistant_message が無くても handbackReport から marker の由来を確認できることだけを検証する旨を説明する日本語注記である。 -->

Use the Task tool to launch the `pr-reviewer` SubAgent with exactly this message:
<!-- 次のコードブロックは SubAgent へ送信する指示文そのものであり、内容は変更しないこと。 -->

```text
Do not run any tool. Reply with exactly one line and nothing else: SUBAGENTSTOP_HANDBACK_PROVENANCE_SMOKE_OK
```

After the SubAgent returns, print its full final response text verbatim in your own final message,
so the runner's transcript capture can locate the marker `SUBAGENTSTOP_HANDBACK_PROVENANCE_SMOKE_OK`.
Do not summarize, paraphrase, or omit that literal string.
<!-- この段落は SubAgent の最終応答をそのまま転記する要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、自分で command も実行しない制約を説明する日本語注記である。 -->
