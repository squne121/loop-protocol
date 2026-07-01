# agy CLI 契約の一次証跡 2026-07-01

## 環境

- 取得時刻: `2026-07-01T11:20:33Z`
- OS: `Linux DESKTOP-TB4VBD9 6.6.87.2-microsoft-standard-WSL2 x86_64 GNU/Linux`
- 実行環境: `WSL2 Ubuntu 24.04`
- TTY 条件: `non-tty`
- Redaction 方針:
  - stdout / stderr sample は短い抜粋だけを残す
  - token らしい文字列と `$HOME` 配下の path は redact する
  - prompt 全文は保存せず、検証に必要な sentinel だけを残す

## Live Evidence / 実機証跡

### `preflight_agy.py --json` の sanitized 出力

```json
{
  "schema": "agy_preflight_result/v1",
  "ok": true,
  "failure_reason": null,
  "failure_class": null,
  "recovery_action": null,
  "agy": {
    "bin": "agy",
    "resolved_path": "$HOME/.local/bin/agy",
    "version": "1.0.14"
  },
  "help": {
    "ok": true,
    "noninteractive_flags": {
      "-p": true,
      "--print": true,
      "--prompt": true
    },
    "unexpected_capabilities": [],
    "stdout_sample": "",
    "stderr_sample": "Usage of agy:\n  --add-dir                       Add a directory to the workspace (repeatable) (default [])\n  -c                              Short alias for --continue\n  --continue                      Continue the most recent conversation\n  --conversation                  Resume a previous conversation by ID\n  --dangerously-skip-permissions  Auto-approve all tool permission requests without prompting\n  -i                              Short alias for --prompt-interactive\n  --log-file            "
  },
  "smoke": {
    "ok": true,
    "argv": [
      "agy",
      "-p",
      "Return exactly: LOOP_AGY_SMOKE_OK"
    ],
    "exit_code": 0,
    "timed_out": false,
    "failure_reason": null,
    "failure_class": null,
    "stdout_sample": "LOOP_AGY_SMOKE_OK\n",
    "stderr_sample": ""
  },
  "warnings": []
}
```

### `agy --version` の結果
version 出力は次のとおり。

```text
1.0.14
```

### `agy --help` の結果

確認できた non-interactive flag は次の 3 つ。

```text
-p
--print
--prompt
```

### Sentinel Smoke / sentinel 確認手順

実行コマンドは次の形で固定した。

```text
cd <mktemp dir> && agy -p "Return exactly: LOOP_AGY_SMOKE_OK"
```

観測結果は次のとおり。

```text
stdout: LOOP_AGY_SMOKE_OK
exit_code: 0
```

判定:

- Sentinel exact match に成功した。
- Smoke は isolated temporary cwd から実行した。
- non-TTY 条件でも stdout に exact sentinel が出力された。
