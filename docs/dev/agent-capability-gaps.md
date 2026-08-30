# Agent Capability Gaps

このドキュメントは、Claude-GPT lane（`scripts/claude-gpt/launch.sh` 経由の
ChatGPT subscription auth backend）と Native Claude lane の間で観測された
capability parity の検証結果を記録する。研究専用（research-only）の記録であり、
guard / launcher の実装修正はここでは行わない。実装修正が必要な場合は
follow-up implementation Issue を作成しリンクする。

## Issue #2436 の検証記録: Claude-GPT / Native Claude parity（agent-retrospective bash guard）

### 結果サマリ

```yaml
result_status: parity_failed
parity_verified: false
tested_head: aade97858e348985081156afab134c317faac67f
availability_probe: available
deterministic_guard_matrix_passed: true
claude_gpt_live_deny_passed: false
claude_gpt_live_allow_passed: false
nested_claude_proxy_transport_proven: false
```

- 検証日時（`timestamp`）: 2026-08-30（日本時間、セッション実施日）
- 対象リポジトリ（`tested_repository`）: squne121/loop-protocol
- 検証対象コミット（`tested_head`）: `aade97858e348985081156afab134c317faac67f`
  （`origin/main`、PR #2425 merge 後の直近 HEAD。`.claude/skills/agent-retrospective/**` /
  `scripts/claude-gpt/**` に PR #2425 以降の semantic change なし）
- 関連 Issue（`related_issues`）: #2436（本件）, #2419（guard incident の起源）,
  #2425（P0 bypass fix, merged）, #2204 / PR #2205（SKIP/transport evidence
  semantics の先例）, **#2445（本調査で作成した follow-up implementation issue）**
- 秘匿情報の取り扱い方針（`redaction_policy`）: token / OAuth credential /
  raw prompt / raw model response は記録しない。proxy 構造化ログの `reqId` /
  `path` / `status` / `transport` フィールドのみ参照する。

### AC1: 決定論的ガード検証（deterministic guard matrix）— PASS

```yaml
deterministic_guard_matrix_passed: true
```

```
uv run --locked pytest \
  .claude/skills/agent-retrospective/scripts/tests/test_bash_mutation_denial_canary.py \
  .claude/skills/agent-retrospective/scripts/tests/test_bash_readonly_pipeline_allowed_canary.py \
  -v
```

結果: **72 passed, 0 failed**（`tested_head` 上で fresh 実行）。

再現規約カバレッジ（PR #2425 の P0 bypass class を含む）を確認済み:

- プロセス置換（process substitution）の迂回拒否: `cat <(git merge stale-feature)`
- `find -exec` / `-execdir` 経由の迂回拒否: `find . -maxdepth 1 -exec git merge stale-feature ;`
- Python/uv 間接実行の迂回拒否: `python3 -c "os.system(...)"`, `uv run python3 script_that_merges.py`
- 改行区切り（newline separator）の迂回拒否: `ls\ngit merge stale-feature`
- git inline alias/config を使った迂回拒否: `git -c alias.merge-x=merge merge-x stale-feature`,
  `git --config alias.mx=merge mx stale-feature`
- git 未列挙 mutation（denylist 漏れ対策）の拒否: `git add`, `git hash-object -w`,
  `git bisect start`
- `gh` action-position spoof（位置引数偽装）の拒否: `gh pr checkout 1 --branch view`,
  `gh workflow run triage.yml --ref view`
- canonical AGY capability の許可確認: `test_bash_guard_hook_allows_canonical_agy_builder_invocation*`
- read-only な git pipeline / GitHub コマンド / `gh api` GET の許可確認
- hook 内の想定外例外に対する fail-closed 挙動の確認: `test_bash_guard_hook_exits_2_on_unexpected_exception`

この結果自体は、この検証で発見された下記の transport routing gap の影響を
受けない（`retrospective_bash_guard_hook.py` を直接 subprocess として起動する
決定論的テストであり、backend/proxy の識別とは独立している）。

### AC2: Claude-GPT 可用性確認（availability probe）— available（利用可能）

```yaml
availability_probe: available
```

```
scripts/claude-gpt/launch.sh --check-only
```

- 終了コード（exit code）: `0`
- ChatGPT 認証状態（`chatgpt_auth.available`）: `true`（`detail: "authenticated"`）
- proxy の絶対パス（`proxy.absolute_path`）: `/home/squne/.local/bin/claude-code-proxy`
- proxy のバージョン（`proxy.version`）: `claude-code-proxy 0.1.34`
- パス検証結果（`canonical_paths.ok` / `read_restriction.ok`）: いずれも `true`
- launcher の識別情報: `scripts/claude-gpt/launch.sh`
  の sha256 は `e006608d9999741adfcf7b3be230f0be8043128689c042413132fb22c905fa44`
- Claude CLI の識別情報: `2.1.251 (Claude Code)` (`/home/squne/.local/bin/claude`)

secret/token の値は記録していない。

### AC3/AC4/AC5: parity_failed — nested claude invocation に Claude-GPT proxy routing 手段が存在しない

```yaml
claude_gpt_live_deny_passed: false
claude_gpt_live_allow_passed: false
nested_claude_proxy_transport_proven: false
```

#### 実施内容と、当初の誤判定

`scripts/claude-gpt/lib.sh` の `claude_gpt_build_proxy_env` と同一の env
allowlist（`PATH`/`HOME`/`CCP_CONFIG_DIR`/`XDG_STATE_HOME`/`CCP_BIND_ADDRESS`/
`CCP_LOG_STDERR`/`CCP_CODEX_TRANSPORT`）で実 `claude-code-proxy 0.1.34` を
起動し（`chatgpt_auth: authenticated` 済み）、呼び出し元シェルに
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 等を
export した状態で、production `run_retrospective.py` の `invoke_agent()` /
`build_agent_invocation_argv()` 経由（run-scoped `--settings` で
`retrospective_bash_guard_hook.py` を PreToolUse hook として注入する経路）で
既存の live canary test（`test_run_retrospective_live_cli.py` の
`test_real_claude_cli_bash_guard_denies_git_merge_and_repo_unchanged` /
`test_real_claude_cli_bash_guard_allows_readonly_pipeline`）を呼び出した。

当初、live allow は1回目の実行で PASS（独立計算した sha256 と model 報告値が
一致）、live deny は計2回の実行（6 trial）が self-refusal で skip した後、
3回目の実行で PASS（`permission_denials` に Bash `merge` denial 記録、
disposable repo の ground truth 不変も確認）した。またこれらの実行前後の
byte-offset window で `scripts/claude-gpt/transport_log.py` を実行したところ
`ok: true`（`http_count: 31`）が得られたため、一時的に
`result_status: verified` として記録した。

#### 独立レビュー（SubAgent C）による是正

その後の敵対的レビューで、上記の「Claude-GPT lane を経由した」という前提
そのものが誤りであることが判明した。根拠（`tested_head` 上でソースコードから
静的に確定可能。実行時トレース不要）:

```
grep -n "_ENV_PASSTHROUGH_ALLOWLIST\|_RUN_SCOPED_ENV_PREFIX\|sanitize_subprocess_env" \
  .claude/skills/agent-retrospective/scripts/run_retrospective.py
# 2951:_ENV_PASSTHROUGH_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
# 2954:_RUN_SCOPED_ENV_PREFIX = "AGENT_RETROSPECTIVE_"
# 3354:    def sanitize_subprocess_env(self, env: dict[str, str]) -> dict[str, str]:
```

`DelegatedAgentPermissionPolicy.sanitize_subprocess_env()`
(`run_retrospective.py:3354-3368`) は、nested `claude` subprocess へ実際に
渡す env を `_ENV_PASSTHROUGH_ALLOWLIST`（`PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ`）
と `_RUN_SCOPED_ENV_PREFIX`（`AGENT_RETROSPECTIVE_`）のみに限定する。
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` /
`ANTHROPIC_DEFAULT_*_MODEL` / `CLAUDE_CONFIG_DIR` はいずれにも該当せず、
呼び出し元プロセスの `os.environ` に何を設定していても nested `claude`
subprocess には伝播しない。`HOME` は通過するが、`scripts/claude-gpt/launch.sh`
が実際に claude-gpt 用 settings を書き込む先は `HOME` とは独立した
`CLAUDE_CONFIG_DIR`（`claude_gpt_claude_config_dir()`）であるため、`HOME` の
通過だけでは claude-gpt 側の proxy 設定を継承できない。

結論: **production `run_retrospective.py` の nested-invocation 経路
(`invoke_agent()`) には、Claude-GPT proxy へ nested claude を routing する
手段が構造的に存在しない。** 上記の live deny/allow 実行は、実際には
Native Claude lane（呼び出し元セッションの ambient 実 HOME 配下の通常
credential）で行われていた可能性が高く、Claude-GPT lane の証拠としては
成立しない。同様に、観測された proxy log 上の 31 件の `/v1/messages`
リクエストは、同一 Unix user 上で並行動作していた無関係な別の claude-gpt
セッション由来である可能性が高く（`<CLAUDE_GPT_HOME>/state/claude-code-proxy/proxy.log`
は port/pid 等の instance 識別子を持たない、Unix user 単位で共有される
単一ファイルであることを確認済み）、本検証の nested invocation に
帰属する証拠として採用できない。

よって:

- `claude_gpt_live_deny_passed`: `true` → **`false`** に訂正（Native lane で
  実行された疑いが強く、Claude-GPT lane での deny 証明として採用不可）
- `claude_gpt_live_allow_passed`: `true` → **`false`** に訂正（同上）
- `nested_claude_proxy_transport_proven`: `true` → **`false`** に訂正
  （観測された proxy トラフィックを本検証の nested invocation に
  帰属させる証拠がない）

ガード自体（AC1）にリグレッションは一切ない。これは guard/security の
回帰ではなく、**production の nested-invocation adapter に Claude-GPT
routing の実装が存在しない**という capability gap である。

### Follow-up

Issue #2436 の docs-only / research-only contract に従い、本 Issue の PR では
guard/launcher/adapter の実装修正を行わない。実装修正は follow-up
implementation Issue へ切り出した:

- **#2445**: `run_retrospective.py` の nested claude invocation に
  Claude-GPT proxy env passthrough が存在しない
  （`sanitize_subprocess_env()` の allowlist 拡張 or 代替設計を要検討）。
  再現手順・失敗クラス（`transport_routing_gap`）・関連 Issue
  （#2436 / #2419 / #2425）を Issue 本文に記載済み。
