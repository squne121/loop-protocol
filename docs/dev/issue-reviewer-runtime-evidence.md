---
doc_id: issue-reviewer-runtime-evidence
doc_title_ja: issue-reviewer 実行時証跡
status: stable
related_issue: 1754
---

# issue-reviewer runtime evidence（実行時証跡）

`issue-reviewer` の Claude Code / Haiku runtime gate は、fixture の成功や
self-report を runtime PASS の根拠にしない。対象 host で実際の `claude -p`
が SubagentStop を発火した場合だけ runtime evidence を収集する。

## Receipt boundary（receipt の境界）

`validate_issue_reviewer_compact_output.py` は hook stdin を一回だけ読み、既存
child intermediate grammar を検証する。`CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1` は
atomic に private runtime-receipts へ保存し、payload、message、transcript、prompt
を保存しない。保存してよいのはそれらの sha256 と safe identifier、attempt、
decision、reason、時刻、hook/settings digest に限る。

- initial invalid: receipt の後に `decision: block`
- retry valid: retry receipt の後に allow
- retry invalid: retry receipt に `parent_fail_close_required` を保存し、再 block
  しない。未変更の応答は parent validator が fail-close する。

## Probe and collection（probe と収集）

`run_issue_reviewer_runtime_probe.py` は trusted host provenance と real `claude`
がそろわない場合、`SKIP:` と exit 77 を返す。SKIP は PASS ではない。probe の
stream/debug は temporary input として digest 化した後に破棄する。

trusted hostでの `claude -p --output-format stream-json` は、実sessionが生成した
`CLAUDE_ISSUE_REVIEWER_RUNTIME_SELF_REPORT_V1` を一件だけ返す。reportにはschema、HEAD、
allow/block-repair結果、receipt集合digestだけを許可し、raw transcript・prompt・path・secretを
含めない。JSONLはassistant envelopeの`message.content`に含まれるtextだけを解析し、unknown・
malformed・複数report・assistant以外由来のmarkerはFAILである。`collect_issue_reviewer_runtime_evidence.py` は current HEAD、receipt decision列、
probe digestを独立に検査し、session reportを比較対象としてのみ照合する。fixtureやcaller
controlled値はruntime evidence sourceになれず、欠落・無効・不一致はFAILである。
`TEST_VERDICT_MACHINE/v2` 候補は head-bound である。publisher は sanitized summary
だけを controlled `update_pr.py` 経路で投稿し、PR URL・body hash・HEAD を readback
する。local transcript を artifact や PR に投稿してはならない。
