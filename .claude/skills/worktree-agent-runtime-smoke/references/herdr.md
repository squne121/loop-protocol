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

## `agent_prompt_stalled`（Claude Code multi-line prompt）

`herdr agent prompt` は、送信後 5000ms 以内に agent lifecycle state の変化が
観測できないと `agent_prompt_stalled` を返す（herdr 自身の固定挙動、
runner 側のタイムアウト値ではない）。

Claude Code の対話 TUI では、改行を含む複数行 prompt を送信すると、
入力欄が `[Pasted text #N +M lines]` という折りたたみ表示のまま **未送信**
になることがある（bracketed-paste の終端シーケンスが送信用の Enter を
吸収してしまうため）。この場合 `agent_status` は `idle` のまま変化せず、
herdr は `agent_prompt_stalled` を返す。Codex CLI の対話 TUI は複数行
ペーストを自動送信するため、この事象は Claude Code 固有で発生する。

runner はこの stall を検知した場合に限り、`herdr agent send-keys <name> enter`
で保留中の送信を完了させ、`herdr agent wait <name> --timeout <残り時間>` で
lifecycle state を再観測する（1 回だけ）。回復に成功した場合は
evidence の `prompt_stall_recovered: true` として記録し、成功を偽装しない。
回復にも失敗した場合は SKIP へ降格せず exit 1 を返す。

## Safety Boundary

- 既存 pane の focus を奪わない（`--no-focus`）
- unique agent name は run-local とする
- `unknown` state を成功扱いしない
- `blocked` は自動承認せず証跡を取得して停止する
- timeout を必須とする
- cleanup では検証 pane だけを閉じる（caller pane、別 agent、別 workspace を変更しない）
- `--keep-pane` 明示指定時のみ pane を残す
- `agent_prompt_stalled` からの回復は `send-keys enter` 1 回のみとし、
  無限リトライや SKIP への降格で失敗を隠さない
