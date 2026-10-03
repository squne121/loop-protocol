Issue #2898 runtime smoke (impl-review-loop landing disposition prose の read-only 合成 dry-run).
<!-- このタイトル行は Issue #2898 の runtime smoke を説明する日本語注記である。 -->

This is a harmless, read-only, synthetic dry-run. It does not use the network, does not call GitHub,
does not run any external code, and does not mutate any file or any GitHub object. Its only purpose is
to confirm that the revised impl-review-loop Skill documents can be read by a delegated SubAgent in the
current Claude Code runtime, that the declared dry-run steps are observed in order, and that the final
output has the shape of the existing IMPL_REVIEW_LOOP_RESULT_V1 required fields.
<!-- この段落は GitHub 操作・外部 code・ファイル変更を伴わない read-only の合成 dry-run であり、改訂後の Skill 文書の読解、宣言順の手順 marker、既存出力契約の必須 field の形だけを確認する旨を説明する日本語注記である。 -->

Use the `Agent` tool exactly once to launch the `pr-reviewer` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で起動して pr-reviewer を呼び出し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
This is a read-only synthetic dry-run. Use only the Read tool, and run no other tool (no Bash, no Edit, no Write, no GitHub access). Read `.claude/skills/impl-review-loop/steps/preparation.md` and `.claude/skills/impl-review-loop/SKILL.md` once each, then reply with exactly these four lines and nothing else, in this order: IMPL_REVIEW_LOOP_SMOKE_STEP_DISPOSITION_READ then IMPL_REVIEW_LOOP_SMOKE_STEP_ROUTE_SELECTED then IMPL_REVIEW_LOOP_SMOKE_STEP_RESULT_EMITTED then IMPL_REVIEW_LOOP_LANDING_SMOKE_OK
```

After the SubAgent has completed and returned its final response, write your own final message in this exact shape and nothing else.
First, copy the SubAgent's full final response text verbatim (the four marker lines).
Second, add exactly one fenced `json` block containing exactly this JSON object, which is a synthetic dry-run result and not a real loop result:
<!-- この段落は SubAgent の最終応答をそのまま転記したうえで、合成 dry-run の結果として次の JSON ブロックを 1 つだけ追加する要件を説明する日本語注記である。 -->

```json
{"schema_version": 1, "status": "draft_pr_ready", "termination_reason": "approved", "merge_ready": false}
```

Do not summarize, paraphrase, or omit the literal marker strings.
<!-- この段落は marker 文字列を要約・言い換え・省略しない要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、自分で command も実行しない制約を説明する日本語注記である。 -->
