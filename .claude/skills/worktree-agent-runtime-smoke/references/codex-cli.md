# Codex CLI Runtime Reference

## Structured lane（既定）

```bash
echo "$PROMPT" | codex exec \
  -C "$WORKTREE" \
  --json \
  --ephemeral \
  -
```

- `-C <worktree>` で explicit worktree cwd を指定する
- `--json` で JSONL event を stdout に出力する
- `--ephemeral` で session を非永続化する
- prompt は positional argument に渡さず、`-`（stdin）で渡す。argv に prompt 全文が
  乗ると process list に露出しうるため（PR #1921 P1 fix-delta）
- current repository configuration の sandbox／permission profile をそのまま利用する。
  `--dangerously-bypass-approvals-and-sandbox`、`--yolo`、`danger-full-access` への自動変更を
  runner は行わない
- caller が指定していない `model` / `profile` を runner が上書きしない
- runner は `codex exec --help` を preflight し、`--json` / `--ephemeral` / `-C` のいずれかが
  存在しない runtime version では exit 77（SKIP）を返す

## Interactive herdr lane（必要時のみ）

呼び出し元の Herdr session ではなく、実行のたびに新規生成する isolated named session の
中で agent lifecycle を駆動する（詳細は `references/herdr.md` 参照）。

```bash
HERDR_SESSION=<isolated-name> herdr workspace create --cwd "$WORKTREE" --no-focus
HERDR_SESSION=<isolated-name> herdr agent start <unique-name> --kind codex --pane <pane-id>
HERDR_SESSION=<isolated-name> herdr agent prompt <unique-name> "<prompt>" --wait --timeout <ms>
HERDR_SESSION=<isolated-name> herdr agent get <unique-name>
HERDR_SESSION=<isolated-name> herdr agent explain <unique-name> --json
HERDR_SESSION=<isolated-name> herdr agent read <unique-name> --source recent-unwrapped --lines <bounded>
herdr session stop <isolated-name> --json && herdr session delete <isolated-name> --json
```

Codex TUI の `/status`、approval 画面、pane transcript の観測に使う。
PR #1864 の先行 herdr smoke（`/status`、Skill 一覧、filesystem read、pane transcript）を
一般化した手順だが、そのときの tracked artifact 形式・SHA256 manifest は再利用しない。

## 観測できる主な evidence

- structured lane: `item.completed` 等の JSONL event 件数・terminal event 有無・process exit code を確認できる
- interactive lane: `agent explain` から抽出した detected agent／confidence と observed lifecycle state
  （`summary.md` の allowlist フィールドのみ。raw pane transcript は保存しない）
