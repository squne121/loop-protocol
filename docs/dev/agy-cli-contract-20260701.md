# agy CLI Contract Evidence 2026-07-01

## Environment

- Captured at: `2026-07-01T11:20:33Z`
- OS: `Linux DESKTOP-TB4VBD9 6.6.87.2-microsoft-standard-WSL2 x86_64 GNU/Linux`
- Runtime: `WSL2 Ubuntu 24.04`
- TTY mode: `non-tty`
- Redaction policy:
  - stdout/stderr samples are stored only as short excerpts
  - token-like strings and `$HOME`-prefixed paths are redacted
  - prompt text is not copied beyond the sentinel phrase required for verification

## Live Evidence

### `agy --version`

```text
1.0.14
```

### `agy --help`

Observed non-interactive flags:

```text
-p
--print
--prompt
```

### Sentinel Smoke

Command shape:

```text
cd <mktemp dir> && agy -p "Return exactly: LOOP_AGY_SMOKE_OK"
```

Observed result:

```text
stdout: LOOP_AGY_SMOKE_OK
exit_code: 0
```

Adjudication:

- Sentinel exact match succeeded.
- Smoke was executed from an isolated temporary cwd.
- Probe ran in non-TTY mode and still produced the exact sentinel on stdout.
