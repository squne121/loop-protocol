---
doc_id: issue-reviewer-runtime-evidence
status: stable
related_issue: 1754
---

# issue-reviewer runtime evidence

`issue-reviewer` の Claude Code / Haiku runtime gate は、fixture の成功や
self-report を runtime PASS の根拠にしない。対象 host で実際の `claude -p`
が SubagentStop を発火した場合だけ runtime evidence を収集する。

## Receipt boundary

`validate_issue_reviewer_compact_output.py` は hook stdin を一回だけ読み、既存
child intermediate grammar を検証する。`CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1` は
atomic に private runtime-receipts へ保存し、payload、message、transcript、prompt
を保存しない。保存してよいのはそれらの sha256 と safe identifier、attempt、
decision、reason、時刻、hook/settings digest に限る。

- initial invalid: receipt の後に `decision: block`
- retry valid: retry receipt の後に allow
- retry invalid: retry receipt に `parent_fail_close_required` を保存し、再 block
  しない。未変更の応答は parent validator が fail-close する。

## Probe and collection

`run_issue_reviewer_runtime_probe.py` は trusted host provenance と real `claude`
がそろわない場合、`SKIP:` と exit 77 を返す。SKIP は PASS ではない。probe の
stream/debug は temporary input として digest 化した後に破棄する。

`collect_issue_reviewer_runtime_evidence.py` は current HEAD、receipt decision列、
probe digest を独立に検査し、self-report は一致比較だけを行う。
`TEST_VERDICT_MACHINE/v2` 候補は head-bound である。publisher は sanitized summary
だけを controlled `update_pr.py` 経路で投稿し、PR URL・body hash・HEAD を readback
する。local transcript を artifact や PR に投稿してはならない。
