# CI 証拠チェック

```bash
HEAD_SHA=$(gh pr view <PR> --json headRefOid --jq .headRefOid)
uv run python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary.py --pr <PR> --repo <owner>/<repo> --expected-head-sha "$HEAD_SHA"
```

Exit と意味:

- 0: all_pass（補助証拠として参照可）
- 10: failed（blocker）
- 20: pending_or_queued（blocker）
- 30: stale_head_sha（stale blocker）
- 40: gh_error（blocker）

`ci_verdict_summary.py` が使えない場合のみ `gh pr checks` fallback を許容し、その旨を記録。
