# capability doc drift snippet (negative control)

本ファイルは #1221 P0-3 の doc<->schema cross-check が drift を deny することを確認するための負例である。windsurf という 4 つ目の surface と enum 外の verdict を含む。

```yaml
agent_observation_capability/v1:
  verdict_enum: [supported, partial, unsupported, unverified]
```

```yaml
surface: claude_code
claimed_verdict: unverified
```

```yaml
surface: codex_cli
claimed_verdict: unsupported
```

```yaml
surface: google_antigravity
claimed_verdict: unverified
```

```yaml
surface: windsurf
claimed_verdict: totally_bogus
```
