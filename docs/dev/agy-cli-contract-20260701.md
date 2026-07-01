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
