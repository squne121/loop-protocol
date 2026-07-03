# Live AGY Native WebSearch Evidence

Issue: `#1266`（対象 Issue）
Provider/profile: `provider=agy + tool_profile=grounded_research`（プロバイダ / プロファイル）
Captured at: `2026-07-03T17:40:13Z`（取得日時）

## Command（実行コマンド）

```bash
uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py --grounded-research --json
```

## Sanitized Result（サニタイズ済み結果）

```yaml
agy_web_grounding_evidence_v1:
  grounding_actor: antigravity_cli
  grounding_backend: agy_native_websearch
  prompt_shape: bounded_websearch_probe
  agy_cli_version: "1.0.16"
  command_exit_code: 0
  web_tool_call_count: 1
  search_query_count: 1
  url_citation_count: 1
  search_queries:
    - "Search for: latest reliable news and return exactly one source URL."
  citations:
    - url: "https://www.reuters.com"
      title: null
      cited_text_snippet: null
  transcript_evidence:
    - source_kind: agy_stdout_or_artifact_excerpt
      excerpt: "https://www.reuters.com  ***  ### Summary of Work * **Search Execution**: Queried the web for reliable news sources, identifying Reuters as a highly-rated global wire service. * **Result**: Returned the exact homepage URL for Reuters."
      sha256: "ac8223645845b1d798b16653e918b35a550a652a554ace138c8e4c59d1100aa6"
  redaction_status: checked_no_secret_pattern
  raw_transcript_included: false
  raw_credential_included: false
  repo_absolute_path_included: false
  failure_class: null
```

## Boundary Claim（境界主張）

This evidence was produced by AGY native `agy -p` execution through `preflight_agy.py --grounded-research`.
It is not Gemini API Google Search grounding, not wrapper-side web retrieval, and not fixture-only evidence.
この証跡は AGY ネイティブの `agy -p` 実行を通じて取得したものであり、Gemini API の Google Search grounding でも wrapper 側の Web 取得でもなく、fixture のみの証跡でもないことを明示する。
