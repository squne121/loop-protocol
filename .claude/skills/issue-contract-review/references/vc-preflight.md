# VC Preflight (baseline_vc_preflight.py)

## 前提

`## Verification Commands` は fenced bash block 形式。

- コマンド行: `AC` マーカー直前に `# ACn`
- 1 行あたり 1 コマンド
- VC でない書式（インラインなど）は無視

## 実行

```bash
uv run python3 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py \
  --issue <番号> --repo <owner>/<repo>
```

## 判定

- `status: pass` → OK
- `status: blocked` → BLOCKED
- `status: human_judgment` → `human_escalation`

## Scope Classes

- `baseline_fail_expected`: 基本想定。`expected_fail` を go、想定外 pass は `blocked`
- `regression_gate`: `pnpm ...` / `uv run pytest` 等。pass は go、fail は blocked
- `pr_review_only`: skipped/go（`verification_owner` + `deferred_reason`）
- `runtime_only`: skipped/go（`verification_owner` + `deferred_reason`）

`pnpm build` は regression gate のまま扱うが、runner 側で fixed env delta `{CI:"true"}` を付けて `shell=False` 実行する。Issue body 側で `CI=true pnpm build` や `env CI=true pnpm build` を書いて回避しない。

## 主要カテゴリ

`expected_baseline_fail`, `unexpected_pass`, `env_missing_dep`, `command_not_allowed`, `unsupported_syntax`, `compound_command_disallowed`, `file_not_found_*`, `trivially_pass`, `regression_gate`, `package_manager_no_tty_prompt` など。

`package_manager_no_tty_prompt` は `ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY` / `Aborted removal of modules directory due to no TTY` を検出したときの dedicated category。body-author-fixable ではなく tooling/env blocker として扱う。

`scope_class` / `classification` / `decision` / `category` を別々に解釈。

## preflight-scope marker

```bash
# AC1
# preflight-scope: pr_review_only
$ <command>
```

- `pr_review_only` / `runtime_only` のみ有効
- 不正値は `classification: human_judgment`。
