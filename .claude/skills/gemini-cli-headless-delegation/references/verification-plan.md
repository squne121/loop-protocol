# Verification Plan: gemini-cli-headless-delegation

**対象スキル**: `.agents/skills/gemini-cli-headless-delegation/`
**策定基準**: Issue #238（検証計画策定）、Issue #124（評価マトリクス・task class 定義）
**作成日**: 2026-04-05

---

## 1. 検証概要

改善 PR (#219, #220, #221) マージ後の `gemini-cli-headless-delegation` について、
**deterministic-ish（mock ベース、CI 実行可能）** と **grounded/live（実 Gemini CLI、ユーザー環境依存）** の
2 系統に分離して検証を実施する。

> **Issue #1968 addendum**: caller-facing return surface は `response_text` 全文の再注入ではなく、`result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を優先する。既存の `response_text` 検証は long-form evidence 互換性の確認として維持する。

### 1.1 検証層の構成

| 層 | 内容 | 既存カバレッジ |
|----|------|----------------|
| wrapper contract 層 | request validation → fail-closed → retry → envelope parse → result 生成 | テスト 17 件（単体カバー済み） |
| context→Gemini CLI 層 | prompt 品質、context_files 反映、CLI 引数の正しさ | 一部（`test_build_prompt_includes_context_and_sections`） |
| Gemini CLI 出力品質層 | task class 別の出力が期待品質を満たすか | 未検証（live のみ） |

---

## 2. Golden Task 定義

### 2.1 R0 Golden Task: ビルドログ失敗要約

**目的**: build log から失敗要約と根拠 5 件を抽出する

**Request JSON テンプレート**:
```json
{
  "schema": "delegation_request_v1",
  "objective": "ビルドログ logs/build.log の失敗原因を特定し根拠を列挙する",
  "instructions": [
    "ビルド失敗の主因を 1 文で要約してください。",
    "失敗の根拠となる行を 5 件以上、行番号付きで列挙してください。",
    "context_files 以外の情報源を参照しないでください。"
  ],
  "tool_profile": "no_tools",
  "output_sections": ["Summary", "Findings", "Evidence"],
  "context_files": ["fixtures/r0_build.log"],
  "model": "gemini-3-flash-preview",
  "timeout_sec": 120
}
```

**期待成果物（`delegation_result_v1`）**:
- `ok == true`
- `response_text` に `Summary`、`Findings`、`Evidence` セクションが存在する
- `response_text` に context_files に存在しない外部 URL や根拠外参照が含まれない

**Fixture**: `tests/fixtures/r0_build.log`（後続の検証実行 issue で作成）
- 内容要件: pytest や CI が失敗する典型的なログ、10 行以上の失敗行を含む

---

### 2.2 R1 Golden Task: pytest テストスケルトン草案

**目的**: 既存 pytest pattern に合わせた test skeleton 草案を生成する

**Request JSON テンプレート**:
```json
{
  "schema": "delegation_request_v1",
  "objective": "run_gemini_headless.py の _build_raw_command 関数に対する pytest テストスケルトンを生成する",
  "instructions": [
    "既存の test_run_gemini_headless.py のパターン（import 構造、monkeypatch 使用法、assert 記法）に従ってください。",
    "テスト名は test_ prefix で始め、各テストは単一の観点を検証してください。",
    "実際のアサーション内容は placeholder でよいが、構造（import、fixture、assert 文）は完全なものにしてください。"
  ],
  "tool_profile": "no_tools",
  "output_sections": ["Draft", "Notes"],
  "context_files": [
    "tests/test_run_gemini_headless.py",
    "scripts/run_gemini_headless.py"
  ],
  "model": "gemini-3-flash-preview",
  "timeout_sec": 120
}
```

**期待成果物（`delegation_result_v1`）**:
- `ok == true`
- `response_text` に以下が含まれる:
  - `import pytest` または `from __future__ import annotations` の記述
  - `test_` で始まる関数名が 1 件以上
  - `assert` 文が 1 件以上
- `response_text` に `unittest` や他のフレームワーク固有 import が含まれない（pytest パターン追従）

---

### 2.3 R2 Golden Task: ファイルリネーム案

**目的**: 指定ディレクトリ 3 ファイル以内のリネーム・import fix 案を生成する

**Request JSON テンプレート**:
```json
{
  "schema": "delegation_request_v1",
  "objective": "run_gemini_headless.py の _build_raw_command を build_raw_command にリネームする patch draft を作成する",
  "instructions": [
    "対象ファイルのみの変更に限定し、それ以外のファイルへの影響を列挙してください。",
    "リネームに伴う呼び出し元の変更箇所を全て列挙してください。",
    "検証のために実行すべきコマンドを提示してください。"
  ],
  "tool_profile": "no_tools",
  "output_sections": ["PatchDraft", "ImpactScope", "ValidationCommands"],
  "context_files": [
    "scripts/run_gemini_headless.py",
    "tests/test_run_gemini_headless.py"
  ],
  "model": "gemini-3-flash-preview",
  "timeout_sec": 120
}
```

**期待成果物（`delegation_result_v1`）**:
- `ok == true`
- `response_text` に `PatchDraft`、`ImpactScope`、`ValidationCommands` セクションが存在する
- `PatchDraft` が対象ファイル（`run_gemini_headless.py`, `test_run_gemini_headless.py`）のみを言及する
  （bounded: スコープ外ファイルへの変更提案が含まれない）
- `ValidationCommands` に具体的なコマンド（`pytest`、`grep` 等）が 1 件以上含まれる

---

## 3. Deterministic-ish 検証項目（mock ベース、CI 実行可能）

### 3.1 Request Validation 検証

| ID | 検証内容 | 合格条件 | 参照テスト |
|----|---------|---------|------------|
| D-1 | R0 golden task の request JSON が `validate_request` を通過する | `errors == []` | 新規 |
| D-2 | R1 golden task の request JSON が `validate_request` を通過する | `errors == []` | 新規 |
| D-3 | R2 golden task の request JSON が `validate_request` を通過する | `errors == []` | 新規 |

### 3.2 Prompt 構築検証

| ID | 検証内容 | 合格条件 | 参照テスト |
|----|---------|---------|------------|
| D-4 | `build_prompt` が context_files 内容を prompt に反映する | prompt に context file の内容が含まれる | `test_build_prompt_includes_context_and_sections`（既存） |
| D-5 | R0 の `output_sections`（Summary/Findings/Evidence）が prompt に含まれる | `"- Summary"`, `"- Findings"`, `"- Evidence"` が prompt に存在 | 新規 |
| D-6 | R1 の `output_sections`（Draft/Notes）が prompt に含まれる | `"- Draft"`, `"- Notes"` が prompt に存在 | 新規 |
| D-7 | `inline_context` が指定された場合に prompt に含まれる（`build_prompt` L224: `if request.get("inline_context"):` 参照） | prompt に `inline_context` の文字列が含まれる | 新規 |

### 3.3 CLI 引数検証

| ID | 検証内容 | 合格条件 | 参照テスト |
|----|---------|---------|------------|
| D-8 | `_build_raw_command` が `--approval-mode plan` を含む | `"--approval-mode"` と `"plan"` が command リストに存在 | 新規 |
| D-9 | `_build_raw_command` が `--output-format json` を含む | `"--output-format"` と `"json"` が command リストに存在 | 新規 |
| D-10 | `_build_raw_command` が `--model <model>` を含む | `"--model"` と指定モデル名が command リストに存在 | 新規 |
| D-11 | `tool_profile=grounded_research` の場合に `build_prompt` が Google Search 許可文言を含む（`--tools` CLI フラグは存在しない。`build_prompt` L214-216: `"- Google Search grounding is allowed..."` の注入で実装） | prompt に `"Google Search grounding is allowed"` が含まれ、`no_tools` 時は含まれない | 新規 |

### 3.4 Result Shape 検証（mock Gemini 経由）

| ID | 検証内容 | 合格条件 | 参照テスト |
|----|---------|---------|------------|
| D-12 | R0 golden task: mock 成功応答で `delegation_result_v1` の必須フィールドが存在 | `ok`, `requested_model`, `actual_model`, `tool_profile`, `exit_code`, `response_text`, `stats`, `stderr`, `warnings`, `raw_command` 全て存在 | `test_run_delegation_normalizes_successful_envelope`（既存・拡張） |
| D-13 | `ok == true` かつ `response_text` に Summary/Findings/Evidence が含まれる（R0 mock） | 各セクションヘッダが存在 | 新規 |
| D-13a | `result_surface` が artifact-first / summary-first で正規化される | `mode == "artifact-first"`、`summary` 非空、`primary_artifact` と `next_action` が存在 | 新規 |
| D-14 | `actual_model` が mock envelope の `stats.models` キーと一致する | `actual_model == "gemini-3-flash-preview"` | 既存テストで確認済み |

### 3.5 Fail-closed / エラー系検証

既存テスト 17 件のうち以下が該当（引き続き CI で確認）:

| ID | 検証内容 | 参照テスト |
|----|---------|------------|
| D-15 | auth failure で `ok == false`、`warnings` に理由が入る | `test_auth_failure_returns_not_ok` |
| D-16 | 429 retry: `RETRY_LIMIT + 1` 回試行後に `ok == false` | `test_model_capacity_exhausted_retries_and_fails` |
| D-17 | context file 欠損で `ok == false` | `test_missing_context_file_returns_not_ok` |
| D-18 | JSON parse 失敗で `ok == false` | `test_json_parse_failure_returns_not_ok` |
| D-19 | stderr が `warnings` リストに正規化される | `test_stderr_warnings_normalized_into_warnings_list` |

---

## 4. Grounded/Live 検証項目（実 Gemini CLI、ユーザー環境依存）

> **前提**: Gemini CLI が認証済みで `gemini --version` が成功すること（preflight pass）

### 4.1 Preflight 検証

| ID | 検証内容 | 合格条件 |
|----|---------|---------|
| L-1 | `gemini --version` が正常終了する | exit code 0 |
| L-2 | `gemini --help` に必須フラグが含まれる | `--model`, `--prompt`, `--output-format`, `--approval-mode` が存在 |
| L-3 | smoke command が `ok == true` を返す | `gemini --model gemini-3-flash-preview --approval-mode plan --prompt 'Do not use any tools. Reply with OK only.' --output-format json` の exit code 0 かつ JSON parse 成功 |

### 4.2 R0 Golden Task 検証

| ID | 検証内容 | 合格条件 |
|----|---------|---------|
| L-4 | 実行して `ok == true` を得る | `delegation_result_v1.ok == true` |
| L-4a | `result_surface.summary` が full report の短い要約として返る | summary が非空で、caller が `response_text` 全文なしでも次判断可能 |
| L-5 | `response_text` に Summary/Findings/Evidence セクションが存在する | 各セクションヘッダが含まれる |
| L-6 | context_files 以外の外部参照がない | `response_text` に `http://`, `https://` の URL が含まれないこと（または context 内に存在する URL に限定） |
| L-7 | `actual_model` が `unknown` でない | `actual_model != "unknown"` |

### 4.3 R1 Golden Task 検証

| ID | 検証内容 | 合格条件 |
|----|---------|---------|
| L-8 | 実行して `ok == true` を得る | `delegation_result_v1.ok == true` |
| L-9 | 生成 draft に pytest の import が含まれる | `import pytest` または `from pytest` が含まれる |
| L-10 | `test_` prefix の関数が 1 件以上含まれる | 正規表現 `^def test_` にマッチする行が存在 |
| L-11 | `assert` 文が 1 件以上含まれる | `assert` キーワードが含まれる |
| L-12 | `warnings` / `stderr` に想定外エラーがない | `warnings` が空、または認識済み警告のみ |

### 4.4 R2 Golden Task 検証

| ID | 検証内容 | 合格条件 |
|----|---------|---------|
| L-13 | 実行して `ok == true` を得る | `delegation_result_v1.ok == true` |
| L-14 | patch draft が bounded である | 対象外ファイルへの変更提案が含まれない（`run_gemini_headless.py` と `test_run_gemini_headless.py` のみ） |
| L-15 | validation command が 1 件以上提示される | `ValidationCommands` セクションに `pytest`、`grep`、または `python` コマンドが含まれる |
| L-16 | `warnings` / `stderr` に想定外エラーがない | `warnings` が空、または認識済み警告のみ |

### 4.5 grounded_research 動作確認

| ID | 検証内容 | 合格条件 |
|----|---------|---------|
| L-17 | `tool_profile=grounded_research` で Google Search grounding が有効になる | `stats` または `response_text` に grounding 使用の痕跡が存在、または `actual_model` が `unknown` でない |
| L-18 | `post_to_issue_url` 付き成功時に `result_surface.primary_artifact` が comment URL を優先する | `primary_artifact_type == "github_comment_url"` |

---

## 5. 受け入れ基準

### 5.1 Deterministic-ish 検証（CI gate 可能）

- D-1 〜 D-19 の全項目が **pass** であること
- 特に D-8〜D-10（CLI 引数検証）、D-11（grounded_research prompt 注入検証）および D-15〜D-19（エラー系）は既存テストとの回帰整合を必須とする
- `uv run --no-project --with pytest python3 -m pytest .agents/skills/gemini-cli-headless-delegation/tests -q` で 全テスト pass

### 5.2 Grounded/Live 検証（ユーザー環境依存）

- L-1〜L-3 (preflight) が pass であること（前提条件）
- L-4〜L-7 (R0): `ok == true` かつ出力に Summary/Findings/Evidence セクションが存在すること
- L-8〜L-12 (R1): `ok == true` かつ生成 draft が pytest パターンに追従していること
- L-13〜L-16 (R2): `ok == true` かつ patch draft が bounded であること
- L-17 (grounded): grounding 動作が確認できること（観察記録で可）

---

## 6. 検証実行の前提条件・注意事項

### 6.1 Fixture データ（後続 issue で作成）

以下の fixture は **検証実行 child issue** で作成する（本 issue のスコープ外）:
- `tests/fixtures/r0_build.log`: R0 golden task 用の疑似 CI ビルドログ
- `tests/fixtures/r1_context/test_run_gemini_headless.py`: R1 用（既存ファイルのコピー）
- `tests/fixtures/r1_context/run_gemini_headless.py`: R1 用（既存ファイルのコピー）
- `tests/fixtures/r2_context/run_gemini_headless.py`: R2 用（既存ファイルのコピー）
- `tests/fixtures/r2_context/test_run_gemini_headless.py`: R2 用（既存ファイルのコピー）

### 6.2 Request JSON ファイルの配置

golden task を実行する場合、`request.json` は対応する fixtures ディレクトリに配置する:
- `tests/fixtures/r0_request.json`
- `tests/fixtures/r1_request.json`
- `tests/fixtures/r2_request.json`

`context_files` の相対パスは `request.json` の配置ディレクトリを基準に解決される
（`usage-contract.md` の「relative `context_files` are resolved against the directory containing `request.json`」参照）。

### 6.3 Grounded/Live 検証の実行環境

- Gemini CLI が認証済みであること（preflight L-1〜L-3 で確認）
- Windows（PowerShell）または WSL2 上の Python で `run_gemini_headless.py` を直接実行する:
  ```bash
  uv run --no-project python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
    --request tests/fixtures/r0_request.json \
    --output tests/fixtures/r0_result.json
  ```
- 出力の `r0_result.json` を L-4〜L-7 の各観点で手動または自動検証する

### 6.4 Stop Conditions

以下の状況が発生した場合、検証実行を停止し後続 issue に引き継ぐ:
- golden task 実行時に Gemini CLI 自体が起動しない（preflight 失敗）
- `ok == false` が繰り返し発生し、`warnings` に認識済み以外のエラーが含まれる
- `response_text` の品質が著しく低く、task class 別の合否判定が困難な場合

---

## 7. 規範的参照

| 参照先 | 用途 |
|--------|------|
| `SKILL.md` | `grounded_research` の制約ポリシー（Core Rules）・tool_profile 定義の出典 |
| `references/delegation-task-classes.md` | task class 定義・評価マトリクス・Golden Tasks 参考例 |
| `references/usage-contract.md` | `delegation_request_v1` / `delegation_result_v1` スキーマ |
| `references/runtime-portability.md` | 実行環境差異（Windows/WSL2）に関する注意事項 |
| `scripts/run_gemini_headless.py` | 実装本体（`validate_request`, `build_prompt`, `_build_raw_command`, `run_delegation`） |
| `tests/test_run_gemini_headless.py` | 既存テスト 17 件（wrapper contract 層） |
| Issue #124 | 評価マトリクス・task class 定義の出典 |
| Issue #125 | current-state bundle |
| Issue #126 | 問題点検討・validation 観点の根拠 |
