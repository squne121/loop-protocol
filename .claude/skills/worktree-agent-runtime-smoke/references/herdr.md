# herdr Pane／Agent API Reference

herdr は通常プロセスに対する pane API と、認識済み AI エージェントに対する agent API を
分離している。本 Skill は両者の責務を混同しない。

## pane API（終了型コマンド）

`claude -p` や `codex exec` のような終了型コマンドは `pane run` または direct subprocess で
実行する。structured lane は herdr を必須としない（`transport=direct` または
`transport=auto` かつ `HERDR_ENV` 未設定時）。

## agent API（対話 TUI）

Claude Code／Codex CLI の対話 TUI は `agent start` で起動し、`agent prompt` で prompt を
投入し、`agent wait` / `agent get` で lifecycle を待ち、`agent read` / `agent explain` で
出力を取得する。

```bash
herdr pane split --current --direction right --cwd "$WORKTREE" --no-focus
herdr agent start <unique-name> --kind claude|codex --pane <pane-id> --timeout <ms>
herdr agent prompt <unique-name> "<prompt>" --wait --timeout <ms>
herdr agent get <unique-name>
herdr agent explain <unique-name> --json
herdr agent read <unique-name> --source recent-unwrapped --lines <bounded>
herdr pane close <pane-id>
```

## herdr の hard dependency 化を避ける

- `mode=structured` は herdr 外でも direct subprocess で実行できる
- `transport=auto` は herdr 内なら pane を利用し、herdr 外では direct subprocess を利用する
- `mode=interactive` は herdr を必須とする
- `HERDR_ENV=1`、必要 CLI capability、running server がない場合、interactive lane は
  `SKIP:` と exit 77 を返す（`herdr status server` で running server を確認する）
- herdr unavailable を structured lane の失敗へ波及させない

## Safety Boundary

- 既存 pane の focus を奪わない（`--no-focus`）
- unique agent name は run-local とする
- `unknown` state を成功扱いしない
- `blocked` は自動承認せず証跡を取得して停止する
- timeout を必須とする
- cleanup では検証 pane だけを閉じる（caller pane、別 agent、別 workspace を変更しない）
- `--keep-pane` 明示指定時のみ pane を残す
