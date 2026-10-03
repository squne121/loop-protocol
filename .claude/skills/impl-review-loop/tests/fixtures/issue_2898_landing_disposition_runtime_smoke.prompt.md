Issue #2898 runtime smoke (impl-review-loop landing disposition prose の read-only 合成 dry-run).
<!-- このタイトル行は Issue #2898 の runtime smoke を説明する日本語注記である。 -->

This is a harmless, read-only, synthetic dry-run. It does not use the network, does not call GitHub,
does not run any external code, and does not mutate any file or any GitHub object. Its only purpose is
to confirm that a delegated read-only SubAgent can Read the two revised impl-review-loop Skill documents
in the current Claude Code runtime, that it reports only what it actually observed in those documents,
and that the final output has the shape of the existing IMPL_REVIEW_LOOP_RESULT_V1 required fields.
This smoke does not prove a formal Skill load, route selection, or execution of the documented procedure's business semantics.
<!-- この段落は GitHub 操作・外部 code・ファイル変更を伴わない read-only の合成 dry-run であり、改訂後の Skill 文書が子 SubAgent に読解できること、観測できた事実だけを報告すること、既存出力契約の必須 field の形だけを確認する旨を説明する日本語注記である。この smoke は Skill の正式 load・route 選択・手順の業務意味の実行を証明しない。 -->

Use the `Agent` tool exactly once, with `subagent_type: "Explore"`, to launch the generic read-only `Explore` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で subagent_type が Explore の汎用 read-only SubAgent として起動し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
This is a read-only synthetic dry-run. Use only the Read tool, and run no other tool (no Bash, no Edit, no Write, no GitHub access). Read `.claude/skills/impl-review-loop/steps/preparation.md` and `.claude/skills/impl-review-loop/SKILL.md` once each. Then reply with up to four lines, in this order, and each marker line only when your own observation supports it:
1. IMPL_REVIEW_LOOP_SMOKE_STEP_DOCS_READ - only if you actually read both files with the Read tool.
2. IMPL_REVIEW_LOOP_SMOKE_STEP_HEADINGS_OBSERVED - only if the heading string `Bounded candidate exclusions` actually exists in preparation.md and the same string also actually exists in SKILL.md.
3. IMPL_REVIEW_LOOP_SMOKE_STEP_SHAPE_EMITTED - emit this after the two lines above.
4. IMPL_REVIEW_LOOP_LANDING_SMOKE_OK - only if all of the above conditions held.
If you could not observe a condition, do not emit the corresponding marker (nor any later marker that depends on it), and instead reply with one line stating the reason.
```
<!-- このコードブロックは子 SubAgent へ送る英語の指示文である。Read tool のみを使い、2 つの file を読み、実際に観測できた事実に条件付けて marker を返し、観測できなければ marker を返さず理由を 1 行で返す旨を示す。 -->

After the SubAgent has completed and returned its final response, write your own final message in this exact shape and nothing else.
First, copy the SubAgent's full final response text verbatim (the marker lines it returned).
Second, add exactly one fenced `json` block containing exactly this JSON object, which is a synthetic dry-run result and not a real loop result:
<!-- この段落は SubAgent の最終応答をそのまま転記したうえで、合成 dry-run の結果として次の JSON ブロックを 1 つだけ追加する要件を説明する日本語注記である。この JSON は実 loop の結果ではなく、合成 dry-run の形だけの出力である。 -->

```json
{"schema_version": 1, "status": "draft_pr_ready", "termination_reason": "approved", "merge_ready": false}
```
<!-- この JSON ブロックは合成 dry-run の出力であり、実在する IMPL_REVIEW_LOOP_RESULT_V1 の必須 4 項目の形だけを示す。実 loop result ではなく、Skill の正式 load・route 選択・手順の業務意味の実行を証明するものでもない。 -->

Do not summarize, paraphrase, or omit the literal marker strings.
<!-- この段落は marker 文字列を要約・言い換え・省略しない要件を説明する日本語注記である。 -->

Do not modify any repository-tracked file. Do not run any command yourself.
<!-- この段落は repository 内の file を変更せず、自分で command も実行しない制約を説明する日本語注記である。 -->
