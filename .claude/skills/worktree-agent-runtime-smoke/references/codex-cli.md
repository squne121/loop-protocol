# Codex CLI Runtime Reference

## Structured lane（既定）

```bash
codex exec \
  -C "$WORKTREE" \
  --json \
  --ephemeral \
  "$PROMPT"
```

- `-C <worktree>` で explicit worktree cwd を指定する
- `--json` で JSONL event を stdout に出力する
- `--ephemeral` で session を非永続化する
- current repository configuration の sandbox／permission profile をそのまま利用する。
  `--dangerously-bypass-approvals-and-sandbox`、`--yolo`、`danger-full-access` への自動変更を
  runner は行わない
- caller が指定していない `model` / `profile` を runner が上書きしない
- runner は `codex exec --help` を preflight し、`--json` / `--ephemeral` / `-C` のいずれかが
  存在しない runtime version では exit 77（SKIP）を返す

## Interactive herdr lane（必要時のみ）

```bash
herdr pane split --current --direction right --cwd "$WORKTREE" --no-focus
herdr agent start <unique-name> --kind codex --pane <pane-id>
herdr agent prompt <unique-name> "<prompt>" --wait --timeout <ms>
herdr agent get <unique-name>
herdr agent explain <unique-name> --json
herdr agent read <unique-name> --source recent-unwrapped --lines <bounded>
```

Codex TUI の `/status`、approval 画面、pane transcript の観測に使う。
PR #1864 の先行 herdr smoke（`/status`、Skill 一覧、filesystem read、pane transcript）を
一般化した手順だが、そのときの tracked artifact 形式・SHA256 manifest は再利用しない。

## 観測できる主な evidence

- structured lane: `item.completed` 等の JSONL event 件数、process exit code
- interactive lane: pane transcript（bounded／redacted）、`agent explain` の native detection response
