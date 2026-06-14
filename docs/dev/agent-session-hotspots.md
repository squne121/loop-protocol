---
title: Agent Session Hotspots
status: active
related_issue: "798"
---

# Agent Session Hotspots

`scripts/summarize_agent_transcript.py` は Claude / Codex の transcript JSONL から
token/context 浪費 hotspot を抽出し、`AGENT_SESSION_HOTSPOTS_V1` JSON artifact を生成する。

## 使用方法

```bash
# 基本実行
uv run python scripts/summarize_agent_transcript.py \
  --transcript /path/to/transcript.jsonl

# manifest も指定する場合
uv run python scripts/summarize_agent_transcript.py \
  --transcript /path/to/transcript.jsonl \
  --manifest /path/to/manifest.json

# redaction 有効（path / token 類をマスク）
uv run python scripts/summarize_agent_transcript.py \
  --transcript /path/to/transcript.jsonl \
  --redact
```

## 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--transcript` | 必須 | transcript JSONL ファイルのパス |
| `--manifest` | 任意 | agent session manifest JSON のパス |
| `--redact` | 任意 | absolute path / secret 類を redact する |

## exit code 規約

| code | 意味 |
|---|---|
| 0 | pass — artifact 生成成功 |
| 1 | warn — parser_warnings あり または partial coverage |
| 2 | missing_input — transcript path 欠落 / 読み取り不可 |
| 3 | parse_error — transcript が解析不可 |

stdout の `STATUS:` 行も同じ値に一致する。

## artifact 出力先

```
tmp/agent-session-hotspots/<transcript-stem>-<timestamp>.json
```

`tmp/` は `.gitignore` で除外済みのため、artifact はコミットされない。

## AGENT_SESSION_HOTSPOTS_V1 schema

```yaml
schema: AGENT_SESSION_HOTSPOTS_V1        # スキーマ識別子（固定値）
generated_at: iso8601                    # 生成日時（UTC）
producer:
  script: scripts/summarize_agent_transcript.py
  version: string                        # スクリプトバージョン
input_refs:
  transcript_path:
    value: string | null                 # redact 時は "<PATH>" に変換
    sha256: string | null                # ファイル SHA256
  manifest_path:
    value: string | null
    sha256: string | null
privacy:
  raw_transcript_included: false         # 常に false（raw 行は保存しない）
  redaction_enabled: boolean             # --redact フラグに対応
  public_projection_safe: boolean        # redaction_enabled と同値
metrics:
  tool:
    name: string | {availability: unknown, value: null}
    version: string | {availability: unknown, value: null}
  model:
    name: string | {availability: unknown, value: null}
    reasoning_effort: string | {availability: unknown, value: null}
  subagents:
    spawned_count: integer
  tokens:
    prompt: integer | {availability: unknown, value: null}
    completion: integer | {availability: unknown, value: null}
    total: integer | {availability: unknown, value: null}
  hooks:
    fired_count: integer
    blocked_count: integer
    skipped_count: integer
  commands:
    failed_count: integer
  reads:
    repeated_read_count: integer
  compaction:
    marker_seen: boolean
  human_intervention:
    count: integer
evidence:
  event_counts: object                   # type 別イベント数
  parser_warnings: list[string]          # 解析警告
```

### 欠落 metadata の表現

transcript に情報がない場合は推測せず、以下の wrapper で明示する:

```json
{"availability": "unknown", "value": null}
```

これにより数値フィールドへの文字列混入を防ぎ、downstream での null チェックが容易になる。

## privacy / redaction ポリシー

- **raw_transcript_included は常に false** — artifact に raw JSONL 行を保存しない
- artifact には event type / count / SHA256 のみを保存する
- `--redact` 有効時: absolute path を `<PATH>` に、GitHub token / OpenAI key / AWS key / PEM key をそれぞれのプレースホルダに置換
- `public_projection_safe: true` は `redaction_enabled: true` の場合のみ

## input_source_policy

```yaml
hook_input_schema:
  claude:
    trusted_fields:
      - session_id
      - transcript_path
      - cwd
      - hook_event_name
      - agent_transcript_path
      - last_assistant_message
  codex:
    trusted_fields:
      - turn_id
      - tool_name
      - agent_transcript_path
      - last_assistant_message
transcript_jsonl:
  policy: fixture_observed_only
  rule: |
    unknown keys は private artifact metadata に保存のみ。
    parser が fixture coverage を持つ場合のみ schema metrics に昇格する。
```

## 出力フォーマット（stdout）

stdout には以下のセクションのみ含む。raw transcript 本文は含まない。

```
STATUS: <exit_code>

SUMMARY:
  tool: <name>
  model: <name>
  ...

BLOCKERS:          # warnings がある場合のみ
  - <message>

ARTIFACT: <path>

NEXT_ACTION: <guidance>

EVIDENCE:          # warnings がある場合のみ
  ...
```

## 運用手順

### セッション後の分析

1. agent session 終了後、transcript JSONL のパスを確認する
2. `summarize_agent_transcript.py --transcript <path> --redact` を実行する
3. `tmp/agent-session-hotspots/` に artifact が生成される
4. SUMMARY セクションの hotspot（failed_commands / repeated_reads / hooks_blocked）を確認する
5. 高い値の項目から cost 削減施策を検討する

### CI での利用

pytest テストは `tests/test_summarize_agent_transcript.py` に集約されている:

```bash
uv run --locked pytest tests/test_summarize_agent_transcript.py -q
```

このテストは `.github/workflows/ci.yml` の `python-test` job で常設ゲートとして実行される。
