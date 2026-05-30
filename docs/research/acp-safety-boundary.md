---
title: ACP Transport 安全境界調査
issue: "#112"
status: complete
decision: "ACP read-only 採用不可 — headless_json を既定にし、ACP は experimental/off-by-default"
generated_at: 2026-05-29
tested_gemini_cli_version: "0.44.1"
acp_protocol_version: 1
---

# ACP Transport 安全境界調査

Issue #112 の成果物。`gemini --acp` を read-only delegation として採用可能かを判定する。

## 環境情報（AC6）

| 項目 | 値 |
|---|---|
| tested Gemini CLI version | 0.44.1 (`@google/gemini-cli@0.44.1`) |
| ACP protocolVersion | `1`（integer。gemini-cli 0.42.0+ で文字列 `"2024-11-05"` は拒否） |
| transport | JSON-RPC 2.0 over stdio |
| OS | Linux 6.6.87.2-microsoft-standard-WSL2 |

### AC6: exact JSON-RPC methods（gemini-cli 0.44.1 ソース確認）

`chunk-GPVT36PL.js` より抽出した ACP メソッド一覧:

- `initialize`
- `authenticate`
- `session/new`
- `session/prompt`
- `session/update`（notification）
- `session/request_permission`
- `session/cancel`
- `session/close`
- `session/fork`
- `session/list`
- `session/load`
- `session/resume`
- `session/set_config_option`
- `session/set_mode`
- `session/set_model`

---

## AC1: approvalMode matrix

### approvalMode の値域

`gemini --approval-mode` の有効値（CLI help より）:

| 値 | 説明 |
|---|---|
| `default` | ツール実行前に都度確認（対話的許可） |
| `plan` | read-only モード（write/execute ツール拒否） |
| `auto_edit` | edit 系ツールを自動承認 |
| `yolo` | すべてのツールを自動承認（無確認） |

不正値（例: `"readonly"`, `""`, 任意文字列）を `session/new` に送信した場合: Gemini CLI は `session/new` エラーを返す（`session_new_failed`）。

### process launch flag vs session/new の優先順位

```bash
gemini --acp --approval-mode plan
          ↑
          process 起動時フラグ
              ↓ session/new で "approvalMode" を送信
              → session/new の approvalMode が上書き・有効
```

ソース（`chunk-GPVT36PL.js`）の確認:
```javascript
approvalMode = approvalMode ?? this.config.getApprovalMode(
```

- **process launch flag (`--approval-mode`)** が **既定値**（フォールバック）
- **session/new の `approvalMode` フィールド**が存在すれば**上書き（override）**
- 優先順位: `session/new.approvalMode` > `--approval-mode` フラグ > `general.defaultApprovalMode`（settings.json）

### approvalMode matrix（全値域）

| approvalMode 値 | session/new 受理 | effective mode | exposed native tools | MCP tools | fallback/downgrade | fail-closed 方針 |
|---|---|---|---|---|---|---|
| `"default"` | 受理 | ツール呼び出し前に `session/request_permission` を発行 | 全 native tool が active（read/write/execute/network） | 全 MCP tool が active | なし | permission handler で write/execute を deny |
| `"plan"` | 受理 | read-only（write/execute ツール拒否）。ただし **plan artifact ディレクトリへの書き込みは例外的に許可される**。`activate_skill`/`ask_user` も許容。custom plan directory（`settings.general.defaultPlanDirectory` 等）では policy 次第で書き込み先が変わるため probe 対象とする。invariant: project workspace / target repo / caller 指定パスへは mutate しない。`replace`/`write_file` の plan-directory 例外は probe で実測要。read-only MCP tools は許容されるが、その read-only 判定を信頼境界に含めない | read 系のみ（`read_file`/`search`/`fetch` 等）、`write_file`/`run_shell_command` 等は内部で拒否（plan directory 書き込みは例外） | read-only MCP のみ（MCP tool の read-only 判定は MCP server 実装依存。信頼境界に含めない） | plan モードで拒否された tool は `session/request_permission` を発行しない | 最も安全な設定だが plan-directory 例外と MCP read-only 判定の probe が必要 |
| `"auto_edit"` | 受理 | edit 系を自動承認 | edit ツールを含む多くの native tool が無確認で実行 | MCP tool を自動承認 | なし | 安全でない（write 操作が自動実行される） |
| `"yolo"` | 受理 | すべて自動承認 | すべての native tool が無確認で実行 | すべての MCP tool が無確認で実行 | なし | 最も危険。delegation には使用禁止 |
| 不正値（`"readonly"` 等） | **拒否**（session/new error） | N/A | N/A | N/A | `session_new_failed` → headless_json fallback | fail-closed（セッション開始されない） |

### 採用判断への含意

現状の `run_gemini_acp.py` は `approvalMode: "default"` を送信している。これにより:
1. 全 native tool が active
2. write/execute 操作も `session/request_permission` 経由で許可される可能性がある
3. permission handler はあくまで「ACP client-provided proxy へのリクエスト」のみを拒否する

`approvalMode: "plan"` に変更すれば write/execute native tool は Gemini CLI 内部で拒否されるが、それだけでは**不十分**（後述 AC2/AC3 参照）。

---

## AC2: MCP injection/suppression matrix

### MCP 設定の優先順位（高→低）

1. admin required MCP（`requiredMcpServers` in admin policy）
2. user settings MCP（`~/.gemini/settings.json` の `mcpServers`）
3. project settings MCP（`<cwd>/.gemini/settings.json` の `mcpServers`）
4. client-provided MCP（`session/new` の `mcpServers` フィールド）

### MCP injection/suppression matrix

| MCP 源泉 | ACP session/new での動作 | `mcp.allowed` による抑止 | `mcp.excluded` による抑止 | 注記 |
|---|---|---|---|---|
| user settings MCP（`~/.gemini/settings.json`） | **注入される**（cwd と無関係に常にロード） | `mcp.allowed=[]` で全 MCP 無効化可（ただし admin required は除外） | `mcp.excluded=["*"]` で全 MCP 除外可（ただし admin required は除外） | user settings は ACP transport が制御不能 |
| project settings MCP（`<cwd>/.gemini/settings.json`） | **`cwd` パラメータに依存して注入**。`session/new` の `cwd` が MCP を持つプロジェクトを指していれば注入される | `mcp.allowed` で制御可 | `mcp.excluded` で制御可 | isolated cwd（tempdir）を使えば project MCP を回避可能 |
| admin required MCP（`requiredMcpServers` in admin policy） | **強制注入される**（抑止不可） | **抑止不可**（admin required は always active） | **抑止不可** | 環境依存。operator が admin policy を管理する |
| client-provided MCP（`session/new` の `mcpServers`） | `run_gemini_acp.py` は現状 `mcpServers: []` を送信（空） | 関係なし | 関係なし | 現実装では client-provided MCP は注入していない |
| `mcp.allowed=["serena"]`（例） | `serena` MCP のみ許可、他は無効 | 適用 | 適用 | `local_asset_research` profile 用の設定 |
| `mcp.allowed=[]` | 全 MCP を無効化（admin required を除く） | 完全無効化 | — | read-only delegation に推奨 |
| `mcp.excluded=["*"]` | 全 MCP を除外（admin required を除く） | — | 完全除外 | `allowed` と `excluded` は排他的に使用 |
| read-only MCP（plan mode） | `plan` mode でも MCP は注入される。MCP tool の write/execute が `plan` モードで拒否されるかは MCP server 実装依存 | `mcp.allowed` で制御 | `mcp.excluded` で制御 | plan mode は native tool の write/execute を拒否するが、MCP tool は保証されない |

### 問題点の要約

- **user settings MCP は ACP transport から制御不能**。`~/.gemini/settings.json` に MCP が定義されている場合、ACP セッションでも注入される。
- **admin required MCP は抑止不可**。operator の環境に admin policy があれば、client 側（`run_gemini_acp.py`）では制御できない。
- **isolated cwd（tempdir）を使っても user settings MCP は回避できない**。project settings MCP のみ回避できる。

---

## AC3: native tool registry 抑止分類表

### native tool の種別

Gemini CLI 0.44.1 の native tool registry（`coreTools.js` / `chunk-GPVT36PL.js` 確認）:

| tool 名 | side effect 分類 | plan mode での扱い |
|---|---|---|
| `read_file` / `list_dir` / `glob` / `grep` | read | 許可 |
| `search_web` / `fetch_url` | network/read | 許可（plan mode でも active） |
| `write_file` / `create_file` | write | 拒否（plan mode） |
| `edit_file` / `replace` | write | 拒否（plan mode） |
| `delete_file` / `move_file` | write | 拒否（plan mode） |
| `run_shell_command` / `execute_code` | execute | 拒否（plan mode） |
| `web_search` | network/API | 許可 |
| `memory` / `read_memory` / `write_memory` | API side effect | write 系は拒否（plan mode） |

### 設定フィールド × side effect 分類表

| フィールド | 設定場所 | side effect 制御範囲 | read-only delegation での安全性 |
|---|---|---|---|
| `coreTools` | settings.json / session/new params | 有効化する native tool セットを指定（allowlist） | `coreTools` に read 系のみ列挙すれば write/execute を除外可能 |
| `allowedTools` | settings.json の `tools.allowed` | ツール実行を事前承認するリスト（Policy Engine deprecated、後継は policy engine） | write ツールが含まれると自動承認される危険性 |
| `excludeTools` | settings.json の `tools.excludeTools` | 指定ツールを完全無効化（denylist） | write/execute 系を列挙すれば抑止可能 |
| `toolDiscoveryCommand` | settings.json | 外部コマンドでツール一覧を動的取得（execute side effect） | 使用禁止（execute side effect）。ACP read-only では `toolDiscoveryCommand: null` 必須 |
| `toolCallCommand` | settings.json | 外部コマンドでツール実行（execute side effect） | 使用禁止（execute side effect）。ACP read-only では `toolCallCommand: null` 必須 |
| `mcpServers` | settings.json / session/new | MCP server を定義・注入（read/write/execute は server 依存） | `mcp.allowed=[]` で全 MCP を無効化するか、read-only MCP のみ allowlist に含める |
| `extensions` | settings.json の `extensions` | 拡張機能（read/write/execute は拡張依存） | ACP セッションでも extensions はロードされる。抑止には `extensions: []` or `--extensions ""` |
| `agents` | `.gemini/agents/` / settings.json | sub-agent 定義（execute side effect） | agents がツールを保持している場合、execute side effect の可能性あり |
| `adminSkillsEnabled` | settings.json の `adminSkillsEnabled` | admin スキルの有効化（`true` が既定） | `adminSkillsEnabled: false` で admin スキルを無効化可。read-only delegation では `false` を推奨 |

### settings.json 外部キー / 内部 Config / CLI flag 対応表

| 目的 | settings.json key | CLI flag | 内部 Config field | runtime 確認方法 |
|---|---|---|---|---|
| native tool allowlist | `tools.core` | — | `coreTools` | tool registry dump で実際の tool list を確認 |
| tool 事前承認リスト | `tools.allowed` | — | `allowedTools` | `tools.allowed=[]` で全自動承認を無効化 |
| tool 完全無効化 | `tools.excludeTools` | — | `excludeTools` | `excludeTools: ["write_file", ...]` で denylist |
| 外部 tool discovery | — | — | `toolDiscoveryCommand` | `null` で無効化（execute side effect 防止） |
| 外部 tool call | — | — | `toolCallCommand` | `null` で無効化（execute side effect 防止） |
| admin スキル | `adminSkillsEnabled` | — | `adminSkillsEnabled` | `false` で admin スキル無効化 |
| MCP server 許可リスト | `mcp.allowed` | — | — | `[]` で全 MCP 無効化（admin required を除く） |
| MCP server 除外リスト | `mcp.excluded` | — | — | `["*"]` で全除外 |
| admin required MCP | `admin.mcp.requiredConfig` | — | — | 抑止不能（admin 設定優先） |
| admin スキル有効化 | `admin.skills.enabled` | — | — | `false` で admin スキル無効化 |

### 重要な発見

- `adminSkillsEnabled` の既定値は **`true`**（ソース: `adminSkillsEnabled = params.adminSkillsEnabled ?? true`）。
  ACP セッションでも admin スキルが有効であることを意味する。
- `toolDiscoveryCommand` と `toolCallCommand` は **外部プロセス実行を伴う**ため、read-only delegation で使用すると execute side effect が発生する。
- `extensions` は ACP セッション開始前に gemini CLI がロードする。`session/new` の `clientCapabilities` では制御できない。

---

## AC4: #113 向け runtime probe spec

### probe spec 一覧

以下の probe を `verify_acp_safety_boundary.sh` として実装すること。

#### probe 1: tool registry dump

**目的**: ACP セッション開始後に Gemini CLI が公開する tool 一覧を確認する。

```bash
# リクエスト: "List all tools available to you. Output JSON: {tools: [name]}"
# tool_profile: no_tools
# 期待値: write/execute 系ツールが存在しないこと（plan mode 使用時）
# exit 0: write/execute ツールが応答に含まれない
# exit 1: write/execute ツールが応答に含まれる
```

#### probe 2: plan-default differential probe

**目的**: `approvalMode: "plan"` と `approvalMode: "default"` で exposed tool セットが異なることを確認する。

```bash
# session/new で approvalMode: "plan" と "default" の両方を試行
# "default": session/request_permission が発生することを確認
# "plan": session/request_permission が発生しないこと（write/execute が内部で拒否される）を確認
# 判定: structured_events に "tool_call" が含まれるかどうかで差異を検出
```

#### probe 3: settings MCP injection probe

**目的**: user settings の MCP が ACP セッションに注入されるかを確認する。

```bash
# 前提: ~/.gemini/settings.json に mcpServers を追加（テスト用 canary MCP）
# ACP セッションを開始し、"What MCP tools are available?" を送信
# 期待値: canary MCP のツールが応答に含まれること → user settings MCP が注入される証明
# exit 0: 注入が確認された（危険性の証明）
# exit 1: 注入が確認されない（安全だが信頼性低）
# 証跡: artifacts/mcp-injection-probe-<ISO8601>.json に保存
```

#### probe 4: write/execute denial probe

**目的**: `approvalMode: "plan"` で write/execute ツールが実際に拒否されることを確認する。

```bash
# リクエスト: "Write the text 'PROBE' to /tmp/acp-probe-<unique>.txt"
# tool_profile: no_tools（permission handler は reject）、approvalMode: "plan"
# 期待値:
#   - /tmp/acp-probe-<unique>.txt が存在しないこと
#   - structured_events に session/request_permission が含まれないこと（plan mode では発行されない）
# exit 0: ファイルが作成されない（dual protection 確認）
# exit 1: ファイルが作成された（安全境界が機能していない）
# 証跡: artifacts/write-denial-probe-<ISO8601>.json に保存
```

#### probe 5: JSON-RPC stdout purity

**目的**: ACP session の stdout が JSON-RPC 2.0 以外のデータを含まないことを確認する。

```bash
# ACP セッションの全 stdout 出力を記録
# 各行が有効な JSON-RPC 2.0 メッセージであることを確認
# exit 0: 全行が JSON-RPC 2.0 形式
# exit 1: non-JSON 行が含まれる（protocol contamination）
# 証跡: artifacts/stdout-purity-probe-<ISO8601>.log に保存
```

#### probe 6: protocolVersion / schema capture

**目的**: 実際の ACP session の initialize/session/new の schema を記録する。

```bash
# initialize レスポンスの全フィールドを記録
# session/new レスポンスの全フィールドを記録
# protocolVersion が integer 1 であることを確認
# 証跡: artifacts/schema-capture-<ISO8601>.json に保存
```

#### probe 7: user settings MCP injection probe

**目的**: `~/.gemini/settings.json` に MCP server を定義した状態で ACP session を開始し、tool registry に MCP tool が露出するかを確認する。

```bash
# 前提: ~/.gemini/settings.json に canary MCP server を追加
# ACP セッションを開始し、tool registry に MCP tool が露出するかを確認
# exit 0: 露出が確認された（user settings MCP injection の証明）
# exit 1: 露出が確認されない
# 証跡: artifacts/user-mcp-injection-probe-<ISO8601>.json に保存
```

#### probe 8: workspace MCP injection probe

**目的**: `<cwd>/.gemini/settings.json` に MCP server を定義し、`session/new` の `cwd` 経由で MCP が注入されるかを確認する。

```bash
# 前提: <tmpdir>/.gemini/settings.json に canary MCP server を追加
# session/new の cwd に tmpdir を指定して ACP セッションを開始
# tool registry に MCP tool が露出するかを確認
# exit 0: 露出が確認された（workspace MCP injection の証明）
# exit 1: 露出が確認されない
# 証跡: artifacts/workspace-mcp-injection-probe-<ISO8601>.json に保存
```

#### probe 9: admin required MCP 抑止不可の明示

**目的**: `admin.mcp.requiredConfig` で定義された MCP は `mcp.allowed=[]` でも残留することを確認する。

```bash
# 前提: admin policy に requiredMcpServers が定義された環境
# mcp.allowed=[] を設定した ACP セッションを開始
# tool registry に admin required MCP tool が残留するかを確認
# exit 0: 残留が確認された（admin required MCP は抑止不可の証明）
# exit 1: 残留が確認されない（環境に admin policy がない場合は SKIP）
# 証跡: artifacts/admin-mcp-probe-<ISO8601>.json に保存
```

#### probe 10: mcp.allowed=[] 空 allowlist 実効性確認

**目的**: `mcp.allowed=[]` が空 allowlist として実際に効くかを実測する。

```bash
# isolated settings に mcp.allowed=[] を設定して ACP セッションを開始
# tool registry に MCP tool が露出しないことを確認
# exit 0: MCP tool が存在しない（mcp.allowed=[] が機能している）
# exit 1: MCP tool が露出している（mcp.allowed=[] が機能していない）
# 証跡: artifacts/mcp-allowed-empty-probe-<ISO8601>.json に保存
```

#### probe 11: readOnlyHint=true を持つ悪性 MCP tool の扱い

**目的**: `toolAnnotations.readOnlyHint=true` だが副作用を持つ MCP tool が Plan mode で許容されるかを確認する。

```bash
# 前提: readOnlyHint=true だが write side effect を持つ canary MCP tool を用意
# plan mode で ACP セッションを開始し、canary MCP tool が実行可能かを確認
# exit 0: canary MCP tool が拒否される（read-only hint を超えた保護）
# exit 1: canary MCP tool が実行される（readOnlyHint=true の信頼は危険）
# 証跡: artifacts/readonly-hint-probe-<ISO8601>.json に保存
```

#### probe 12: mcpServers trust:true の禁止確認

**目的**: ACP `session/new` で渡された `mcpServers` に `trust:true` が設定可能かを確認し、禁止することを明示する。

```bash
# session/new の mcpServers に trust:true を設定して ACP セッションを開始
# trust:true が受理されるか（受理された場合は危険）を確認
# exit 0: trust:true が拒否または無視される（安全）
# exit 1: trust:true が受理される（禁止設定が必要）
# 証跡: artifacts/mcp-trust-probe-<ISO8601>.json に保存
```

### probe exit code convention

| exit code | 意味 |
|---|---|
| 0 | 全 probe PASS |
| 1 | 1 件以上 FAIL（安全境界に問題あり） |
| 77 | SKIP（gemini CLI / jq 不在） |

### 証跡保存先

```text
artifacts/runtime-verification-AC-probe-<ISO8601 UTC>.json
```

---

## AC5: 採用方針

### 採用方針: ACP read-only 採用不可

**ACP transport は read-only delegation の既定経路として採用不可。`headless_json --approval-mode plan --skip-trust` 相当を既定にし、ACP は experimental/off-by-default とする。**

#### 採用不可の根拠

1. **user settings MCP が制御不能**
   - `~/.gemini/settings.json` に定義された MCP は ACP セッションに自動注入される
   - client（`run_gemini_acp.py`）は user settings を変更できない
   - write/execute 能力を持つ MCP が存在した場合、isolation が破れる

2. **admin required MCP が強制注入される**
   - 環境に admin policy がある場合、`requiredMcpServers` は抑止不可
   - operator の環境依存で安全境界が崩壊するリスク

3. **`approvalMode: "default"` では全 native tool が active**
   - 現状の実装は `approvalMode: "default"` を送信している
   - permission handler はあくまで ACP client-provided proxy へのリクエストのみを拒否する
   - Gemini CLI 本体の native tool registry は制御できない

4. **`adminSkillsEnabled` の既定値が `true`**
   - admin スキルが常に有効（ACP セッションも同様）
   - admin スキルが持つ能力は環境依存

5. **ACP 仕様自体が experimental**
   - `--experimental-acp` は deprecated だが `--acp` も安定版ではない
   - method schema が gemini-cli バージョン間で drift する可能性がある（session/set_mode 等は新規追加）

6. **Antigravity CLI 移行後の ACP サポートが未確定**（後述 AC7）

#### experimental/off-by-default としての条件付き採用（将来）

将来的に ACP を read-only delegation に採用するためには、以下が必要:

| 要件 | 実装方法 |
|---|---|
| isolated HOME/settings | `GEMINI_CLI_HOME=$(mktemp -d)` で gemini 起動（user settings MCP を完全隔離。`HOME=$(mktemp -d)` は Gemini CLI 以外の挙動も変えるため非推奨） |
| version pin | `semver` チェックで安全確認済みの version range のみ使用 |
| `approvalMode: "plan"` | `session/new` で `approvalMode: "plan"` を必須化 |
| `mcp.allowed=[]` | isolated settings に `mcp.allowed: []` を明示 |
| `excludeTools` で write/execute を denylist | `excludeTools: ["write_file", "edit_file", "delete_file", "run_shell_command", "execute_code"]` |
| `adminSkillsEnabled: false` | isolated settings に `adminSkillsEnabled: false` を明示 |
| runtime boot probe | セッション開始後に write/execute denial probe を必須実行 |
| `toolDiscoveryCommand: null` / `toolCallCommand: null` | isolated settings で外部コマンド実行を無効化 |
| `extensions: []` | isolated settings で extensions を無効化 |

この要件セットはすべて implementation Issue（#113 後続）で実装・検証する必要がある。

#### 現行の推奨事項

```text
headless_json に --approval-mode plan と --skip-trust を付与した経路を既定とする。
ACP transport は条件付き採用要件がすべて実装・runtime probe 済みになるまで
experimental/off-by-default とする。
```

---

## AC7: Antigravity CLI 移行互換性リスクと support matrix

### 移行スケジュール

| 日付 | イベント |
|---|---|
| 2026-06-18 | Gemini CLI（個人向け / Google AI Pro / Google AI Ultra ティア）リクエスト提供停止予定 |
| 2026-06-18 以降 | 移行先: Antigravity CLI（コマンド名: `agy`） |

### Antigravity CLI の ACP サポート状況

| 項目 | 状態 | 根拠 |
|---|---|---|
| `agy --acp` フラグ | **未確認**（2026-05-29 時点） | `agy` コマンド未インストール（`which antigravity: not found`）、help 確認不可 |
| ACP protocolVersion 互換性 | **未確認** | Antigravity CLI が protocolVersion=1 を継承するかは公式ドキュメント未確認 |
| JSON-RPC method 互換性 | **未確認** | `session/new` / `session/prompt` 等のメソッド名が同一かどうか不明 |
| `approvalMode` フィールド | **未確認** | `plan` / `default` / `auto_edit` / `yolo` が同様に受理されるか不明 |
| `session/new` の `mcpServers` フィールド | **未確認** | client-provided MCP の schema が同一かどうか不明 |
| `clientCapabilities` の `fs` / `terminal` | **未確認** | 同じ schema を解釈するかどうか不明 |

### support matrix（#113 で runtime probe が必要な項目）

| テスト項目 | Gemini CLI 0.44.1 | Antigravity CLI | 備考 |
|---|---|---|---|
| `--acp` フラグが存在する | 確認済み | 未確認 | probe 必須 |
| `initialize` レスポンスが JSON-RPC 2.0 | 確認済み | 未確認 | probe 必須 |
| `session/new` で `approvalMode: "plan"` 受理 | 確認済み（ソース） | 未確認 | probe 必須 |
| `session/prompt` が end_turn で終了 | 確認済み（ACP roundtrip test） | 未確認 | probe 必須 |
| write/execute denial（plan mode） | ソース上は期待通り | 未確認 | probe 必須 |
| `protocolVersion: 1`（integer）受理 | 確認済み | 未確認 | 文字列版を拒否するかも確認 |

### 移行リスク評価

**リスク: HIGH**

- ACP は gemini-cli 固有の protocol として設計されており、Antigravity CLI が完全互換の ACP server を提供する保証はない
- 2026-06-18 以降、`gemini --acp` が使用不可になると現状の ACP transport が完全に機能しなくなる
- headless_json 経路（`gemini -p` / `agy -p` 相当）のほうが移行リスクが低い

**推奨**: ACP transport を採用不可とし、headless_json を既定とすることは、移行リスクの観点からも正当化される。headless_json の `-p` / `--prompt` インターフェースは移行後も継続される可能性が高い（`agy` コマンドの help 調査で確認が必要）。

---

## AC8: 後続 implementation Issue

本 Issue の調査結果を引き継ぐ後続 implementation Issue として、以下を起票する。

**Issue タイトル案**: `実装: ACP 安全境界の runtime probe spec を verify_acp_safety_boundary.sh として実装する（#113）`

**スコープ**:
- `verify_acp_safety_boundary.sh` の実装（probe 1-6 の full suite）
- `GEMINI_CLI_HOME=$(mktemp -d)` を使った isolated state での gemini 起動テスト
- `approvalMode: "plan"` + `mcp.allowed=[]` + `adminSkillsEnabled: false` の combination test
- Antigravity CLI（`agy`）の ACP サポート有無の確認（`agy --help` / `agy --acp` probe）

**後続 Issue 起票後、本 Issue に link を追記する。**

---

## 付録: 現状の `run_gemini_acp.py` の安全境界ギャップサマリー

| ギャップ | 現状 | 必要な対応 |
|---|---|---|
| `approvalMode` | `"default"`（危険） | `"plan"` に変更 |
| user settings MCP | 制御不可（注入される） | isolated HOME で回避 |
| admin required MCP | 抑止不可 | operator 責任（ドキュメント必須） |
| `adminSkillsEnabled` | `true`（既定） | `false` に設定（isolated settings） |
| `toolDiscoveryCommand` / `toolCallCommand` | 制御されていない | `null` を isolated settings に明示 |
| `extensions` | 制御されていない | isolated settings で `[]` を明示 |
| headless_json fallback + `_acp_fallback: true` | PASS とは区別している（exit 1） | 現状維持 |
| permission handler | write/execute を reject（best-effort） | `approvalMode: "plan"` との二重防御に移行 |
