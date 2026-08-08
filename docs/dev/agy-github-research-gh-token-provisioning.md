# AGY github_research: GH_TOKEN Provisioning / Credential Boundary (Issue #2012)

Issue: `#2012`（対象 Issue、parent goal ref: `#1886`）
Provider/profile: `provider=agy + tool_profile=github_research`（プロバイダ / プロファイル）
Captured at: `2026-08-08T08:56:29Z`（取得日時、記録は evidence artifact の `generated_at` と一致）

## 設計決定の記録（Design Decision）

OWNER の HUMAN_DECISION_V1（[issuecomment-5225265837](https://github.com/squne121/loop-protocol/issues/2012#issuecomment-5225265837)）に基づき、`GH_TOKEN` provisioning gap を次の subprocess-boundary 設計で解消した。

1. **E2E/orchestrator process (`run_agy_github_research_e2e.py`) は GitHub token を取得・保持しない。**
   `_preflight()` は GH_TOKEN/GITHUB_TOKEN を一切 `os.environ.get()` しない。broker を実行して読み取り専用の `get_repo` probe が成功するかどうかだけを観測する（`_execute_via_broker_subprocess()`）。
2. **broker (`run_agy_github_research_broker.py`) は実際の独立 OS subprocess として起動される。**
   `run_agy_github_research_broker.py execute <operation> --params-json ... --host github.com --repo <repo> --gh-bin <gh>` という CLI 呼び出しが `subprocess.run()`（`shell=False`、独自の `argv`）として起動される。E2E process はこの broker subprocess の `execute_operation()` を in-process 関数呼び出しでは一切呼ばない。
3. **credential bootstrap は broker subprocess 内部だけで完結する。**
   `resolve_gh_token()` は、broker subprocess 自身の起動時 env に `GH_TOKEN`/`GITHUB_TOKEN` が明示的に存在すればそれを優先して使う。存在しなければ `bootstrap_gh_token()` が `gh auth token --hostname github.com` を broker subprocess の**内部だけ**で実行し、stored credential（ambient `gh` credential store。research 用 `gh` 実行に使う isolated `GH_CONFIG_DIR` とは別）を取得する。取得した token は broker subprocess のメモリ内だけに保持され、stdout・stderr・診断出力・IPC result のいずれにも出現しない。
4. **credential bootstrap 失敗は fail-closed。**
   `gh` CLI 不在・`gh auth token` 非ゼロ終了・空 token・malformed 出力のいずれも、構造化された `{"ok": false, "reason": "<reason>"}` を返し（token 値は一切含まない）、SKIP へ倒れる。PASS へ昇格することはない。
5. **research 用 `gh` 実行は credential bootstrap 後も isolated（fresh・empty）な `GH_CONFIG_DIR` を使う。**
   bootstrap は ambient `GH_CONFIG_DIR`（stored credential 保管場所）を読み取るだけで一切書き換えない。research コマンド自体は毎回新規に作成される空の `GH_CONFIG_DIR`（`tempfile.TemporaryDirectory(prefix="agy-github-research-broker-")`）の下で実行される。

## Command（実行コマンド）

以下は shell で `GH_TOKEN`/`GITHUB_TOKEN` を明示的に unset した状態（このマシンの実行環境ではそもそも未設定）かつ、`gh auth login` 済みの authenticated stored credential が利用可能な状態で実行した genuine live 実行である。Gemini / direct fallback / fixture 注入は一切行っていない。

```bash
# 事前状態: GH_TOKEN / GITHUB_TOKEN は環境変数として未設定。
# `gh auth status` は github.com に authenticated（stored credential）。
$ gh auth status
github.com
  ✓ Logged in to github.com account squne121 (stored credential file)
  - Active account: true
  - Git operations protocol: https
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'

$ uv run --locked python3 \
    .claude/skills/gemini-cli-headless-delegation/scripts/run_agy_github_research_e2e.py \
    --prompt "Look up the title of GitHub issue #1920 in squne121/loop-protocol and summarize it in one sentence."
```

## Sanitized Result（サニタイズ済み結果、token 値は含まない）

```yaml
agy_github_research_evidence_v1:
  run_id: "agy-github-research-66226f97e46a"
  generated_at: "2026-08-08T08:56:29Z"
  status: pass
  skip_reason: null
  provider:
    requested: agy
    observed: "1.1.11"
  profile: github_research
  repository_binding:
    host: github.com
    repo: squne121/loop-protocol
  close_evidence:
    positive_run:
      observed: true
      exit_code: 0
      iteration_count: 1
      adaptive_next_command_observed: false
    negative_probes:
      - probe_class: mutation
        denied_pre_execution: true
      - probe_class: cross_repository
        denied_pre_execution: true
      - probe_class: alternate_host
        denied_pre_execution: true
      - probe_class: compound_shell
        denied_pre_execution: true
      - probe_class: credential_display
        denied_pre_execution: true
  iterations:
    - index: 0
      command_requested:
        argv: ["issue", "view", "1920", "--repo", "github.com/squne121/loop-protocol"]
      decision: allow
      exit_code: 0
      truncated: false
  digest_binding:
    agy_version: "1.1.11"
    pr_1994_schema_version: "agy_permission_boundary_e2e/v1"
  redaction_status: checked_no_secret_pattern
  raw_credential_included: false
```

Full artifact（token 値を含まないことを `secret_exposure_scanner.py` で確認済み）:
`.claude/artifacts/agent-provider-route/agy-github-research-66226f97e46a/agy_github_research_evidence.json`

## Boundary Claim（境界主張）

- この実行は、shell から `GH_TOKEN`/`GITHUB_TOKEN` を明示的に provisioning することなく、`gh auth login` 済みの stored credential のみを使って genuine `status: pass` に到達したことを示す。
- E2E/orchestrator process (`run_agy_github_research_e2e.py`) は実行を通じて GH_TOKEN/GITHUB_TOKEN を一度も `os.environ.get()` していない（`_preflight()` / `run_github_research_route()` はいずれもこれを読まない）。
- broker (`run_agy_github_research_broker.py`) は `execute` CLI サブコマンドとして実際の独立 OS subprocess で起動され、credential resolution（`gh auth token --hostname github.com`）はその subprocess 内部だけで完結した。
- 取得した token 値は、stdout・stderr・IPC result・route artifact（上記 sanitized result 参照）のいずれにも一度も出現していない（`secret_exposure_scanner.py` の `scan_file()` による static scan と、hermetic token-shaped-sentinel テストの両方で確認済み — `.claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_credential_bootstrap.py`）。
- credential-unavailable 環境（stored credential 不在、`gh` CLI 不在等）では `status: skip` に倒れ、`skip` が `pass` として扱われることはない（`.claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_credential_bootstrap.py::test_ac7_live_credential_resolution_pass_vs_skip_distinction` で hermetic に検証済み）。
