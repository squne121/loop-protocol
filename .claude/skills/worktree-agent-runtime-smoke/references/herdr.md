# herdr Pane／Agent API Reference

herdr は通常プロセスに対する pane API と、認識済み AI エージェントに対する agent API を
分離している。本 Skill は両者の責務を混同しない。

## pane API（終了型コマンド）

`claude -p` や `codex exec` のような終了型コマンドは direct subprocess で実行する。
structured lane は herdr を一切経由しない（`mode=structured` は常に direct subprocess）。

## agent API（対話 TUI）— 常に isolated named session 内で実行する

`mode=interactive` は、呼び出し元が現在接続している Herdr session（人間の Herdr session を
含む）を一切使わない。実行のたびに高エントロピーな named session を新規生成し、その
session の中だけで workspace／pane／agent lifecycle を駆動する（PR #1921 human OWNER
fix-delta iteration 5）。

```bash
# 1. 名前衝突がないことを確認する
herdr session list --json

# 2. 高エントロピーな名前を生成し、HERDR_SESSION を isolated session 名に固定した
#    環境（継承された HERDR_SESSION／HERDR_SOCKET_PATH／HERDR_PANE_ID／HERDR_TAB_ID／
#    HERDR_WORKSPACE_ID は除去済み）で、その session 内に workspace を作成する
HERDR_SESSION=<isolated-name> herdr workspace create --cwd "$WORKTREE" --no-focus
# -> result.workspace.root_pane.pane_id を使う

# 3. 同じ isolated 環境で agent lifecycle を駆動する
HERDR_SESSION=<isolated-name> herdr agent start <unique-name> --kind claude|codex --pane <pane-id> --timeout <ms>
HERDR_SESSION=<isolated-name> herdr agent prompt <unique-name> "<prompt>" --wait --timeout <ms>
HERDR_SESSION=<isolated-name> herdr agent get <unique-name>
HERDR_SESSION=<isolated-name> herdr agent explain <unique-name> --json
HERDR_SESSION=<isolated-name> herdr agent read <unique-name> --source recent-unwrapped --lines <bounded>

# 4. 終了時は session そのものを終了し、消失を確認する（成功／失敗・SIGINT／SIGTERM を問わない）
herdr session stop <isolated-name> --json
herdr session delete <isolated-name> --json
herdr session list --json   # <isolated-name> が含まれないことを確認する
```

## herdr の hard dependency 化を避ける

- `mode=structured` は herdr を必要としない（常に direct subprocess）
- `mode=interactive` は herdr を必須とする（常に isolated named session）
- `HERDR_ENV=1`、必要 CLI capability、running server がない場合、interactive lane は
  `SKIP:` と exit 77 を返す（`herdr status server` で running server を確認する）
- herdr unavailable を structured lane の失敗へ波及させない

## `allow_nested`（herdr 自身が持つ nested-session 制限）

herdr は既定で「既に herdr session 内で実行されているシェルから、別の named session を
新規に起動すること」を拒否する（`config.toml` の `allow_nested = false` が既定値。
実 herdr v0.7.5 で確認済み: `error: nested herdr is disabled by default.`）。

本 runner が `## herdr の hard dependency 化を避ける` の isolated session 生成に失敗した
場合（この nested 制限を含む）、**人間の使用中 session へフォールバックすることは絶対にしない**。
`herdr session list --json` で新規 session の存在を確認できなければ、それだけで
exit 77（SKIP）とする。isolated session を確実に生成できる環境（`allow_nested = true`
を明示設定した herdr、または nested-session 制限のない environment）でのみ
`mode=interactive` を PASS まで実行できる。この制約自体は本 Skill の安全設計の一部であり、
バグではない（PR #1921 human OWNER fix-delta iteration 5 で実 herdr に対して確認済み）。

## `agent_prompt_stalled`（Claude Code の複数行 prompt で発生する事象）

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

## Safety Boundary（安全境界）

- 人間の使用中 Herdr session・pane・agent・workspace を一切変更しない（常に新規 isolated
  named session を使う）
- unique agent name は run-local とする
- `unknown` state を成功扱いしない
- `blocked` は自動承認せず証跡を取得して停止する
- timeout を必須とする
- cleanup は isolated session 全体（`session stop` -> `session delete`）を対象とし、
  `session list --json` での消失確認が取れない場合は fail-closed で exit 1 とする
- opt-out（pane／session を残すオプション）は提供しない。SIGINT／SIGTERM を含む全ての
  終了経路で cleanup を実行する
- 複数 run を並列実行しても、各 run は自分が生成した session だけを cleanup する
  （他 run の session 名を対象にしない）
- `agent_prompt_stalled` からの回復は `send-keys enter` 1 回のみとし、
  無限リトライや SKIP への降格で失敗を隠さない

## isolated session lifecycle 全体で同一の explicit identity を使う（Issue #2176 P0-3）

collision check（`new_isolated_session_name`）・session 作成
（`create_isolated_session`）・socket lookup（`_session_socket_path`）に加えて、
cleanup（`session stop` -> `session delete` -> `session list --json` 消失確認）も、
同一の明示的な stripped `isolated_env`（`HERDR_SESSION`／`HERDR_SOCKET_PATH` を
この run 自身の値に固定した環境）で実行する。cleanup だけが暗黙の ambient
環境（呼び出し元プロセスが継承した `HERDR_SESSION`／`HERDR_SOCKET_PATH`）に
フォールバックする非対称は許容しない。

`--require-session-baseline-preservation` を指定すると、isolated session を
作成する前後で `herdr session list --json` の全件（この run が自分で作成した
session を除く）を比較し、既存の人間 session を含む pre-existing session が
一件でも変化・消失していれば fail-closed で FAIL とする（snapshot 自体の取得
失敗も FAIL 扱いとし、黙って skip しない）。省略時（既定）は従来どおりこの
検証を行わない。
