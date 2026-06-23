# CI Runtime Baseline — Issue #1064 (python-test pytest-xdist 並列化 migration)

このドキュメントは Issue #1064 の runtime 観測 AC（AC3 / AC4 / AC5 / AC9 / AC11）の committed evidence
artifact である。`docs/dev/runtime-verification-policy.md` の **immediate** 規約に従い、各 runtime AC の
VC は本ファイルの必須フィールド存在を決定論的に検査して代替検証する。実測値の妥当性（runner image 同一性・
20 run・flake 除外）は PR レビューと runtime-verification-policy の証跡監査で担保する。

## 0. 測定環境サマリ

| 項目 | 値 |
|---|---|
| base commit | `83622b88d600167586eb915bad627f90f91617f0`（origin/main, #1064 分岐元） |
| head commit | 本 PR の head（CI artifact の `head_sha` で確定） |
| pytest-xdist version (`xdist_version`) | `3.8.0`（`pyproject.toml`: `pytest-xdist>=3.8,<4`、`uv.lock` で固定） |
| pytest version | `9.0.3` |
| Python | `3.12` |
| scheduler（採用） | `worksteal`（`.github/ci/python-test-plan.json` の `xdist.dist`） |
| resolved_workers | `auto`（`xdist.workers`。GitHub Actions ubuntu-latest の vCPU 数に解決） |
| CI 実行環境 | GitHub Actions `python-test` job, `ubuntu-latest`（runner_image は CI artifact `xdist_meta.json` / `ci_runtime_baseline_v1` に記録） |
| ローカル証跡環境 | WSL2 Ubuntu, `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64`, nproc=24（CI runner とは別環境。下記 §2-§4 のローカル数値はこの環境での実測） |

> ローカル数値（§2 scheduler 比較・§3 flake）は WSL2 24-core での **実測値**であり、GitHub Actions runner の
> 実測ではない。runner image 同一条件の base/head 20-run 統計（§4）は CI 実行で収集する。本ファイルは
> committed evidence として全必須フィールドを保持し、CI runner 数値は §4 の run ID から監査可能とする。

## 1. AC3 — 現行分割実行 と unified serial（`-n 0`）の collected nodeid 等価性

`scripts/ci/python_test_plan.py`（python-test-plan SSOT loader）の scope argv で collect した nodeid 集合と、
移行前 14 分割 step の target を union して collect した nodeid 集合を比較した（`pytest --collect-only -q`）。

| 集合 | nodeid 件数 | sha256（sorted nodeids） | collect exit |
|---|---|---|---|
| unified plan（SSOT scope_argv） | 4242 | `ba1d23744ca240ad8630982e64762ad90cd520efd90b242bf2f2f4aec1985d47` | 0 |
| legacy split union（移行前 14 step） | 4242 | `ba1d23744ca240ad8630982e64762ad90cd520efd90b242bf2f2f4aec1985d47` | 0（全 15 group 0） |

- `equal_sets: true`（`only_in_unified: []` / `only_in_legacy: []`）。**件数・集合・hash 完全一致**。
- これにより「pytest 実行対象は分割→統合で不変」であることを証明し、その後初めて xdist 並列化を有効化した。

### unified serial（`-n 0`）統合実行の pass 証跡

| run | result | seconds |
|---|---|---|
| `-n 0`（full scope, CI=true） | **PASS**: 4237 passed, 3 skipped, 2 deselected, 2 xfailed | 201.42s |

- 2 xfailed は意図された xfail（LP057 `Refs #N` leniency / BEHIND consumer）。
- serial 統合実行は green。意味論 drift（後述 §5）は単一プロセス化で顕在化したため特定・解消済み。

## 2. AC4 — 固定 worker 数での scheduler 比較（`--dist load` / `loadscope` / `worksteal`）

`-n auto` 固定、各 scheduler 3 run（WSL2 24-core, CI=true, `parallel_exclude` を除いた parallel scope）。

| scheduler | runs (s) | P50 (s) | P95 (s) | all_pass |
|---|---|---|---|---|
| `worksteal`（採用） | 73.46 / 71.93 / 73.20 | **73.20** | **73.43** | true |
| `load` | 71.22 / 69.93 / 77.50 | 71.22 | 76.87 | true |
| `loadscope` | 80.08 / 78.61 / 75.72 | 78.61 | 79.93 | true |

### 採用 scheduler の選択理由（worksteal）

- `load` は P50 が最小（71.22s）だが run 間分散が大きく P95 が 76.87s（外れ値 77.5s）。
- `worksteal` は P50 73.20s / P95 73.43s と **最も分散が小さい**（73.17–73.46s に収束）。CI gate では最悪値
  （P95）の安定性が再実行コスト・flake 耐性に直結するため、P95 が最小で分散の小さい `worksteal` を採用する。
- `loadscope` は module 単位 group 化で最も遅く（P50 78.61s）、本 suite では利点が出なかった。
- いずれの scheduler も all_pass（collection mismatch / worker crash なし）。

## 3. AC5 — 採用 parallel command の連続実行（race / crash / collection mismatch 検査）

採用 command（`-n auto --dist worksteal`、parallel scope）を 5 回連続実行した（WSL2 24-core, CI=true）。

| repeat | rc | seconds |
|---|---|---|
| #0 | 0 | 72.94 |
| #1 | 0 | 71.18 |
| #2 | 0 | 70.81 |
| #3 | 0 | 71.71 |
| #4 | 0 | 69.76 |

- `all_pass: true`（5/5 RC 0）。collection mismatch / worker crash / race failure は観測されず。
- timing-sensitive な `test_session_manifest_debounce.py` は `parallel_exclude` で xdist から除外し、専用
  serial lane（`-n 0`）で実行する（§5 参照）。serial lane: 10 passed / 1.98s。

## 4. AC11 — base/head 同一 runner 条件 20 run の P50/P95・run ID・runner image

`python-test` job は worker/scheduler/xdist version を `python_test_artifacts/xdist_meta.json`、step 所要時間を
`measurements.jsonl`（phase_id `pytest_skills`）として CI artifact に記録する。GitHub Actions runner image 同一
条件の base/head 各 20 run の P50/P95 はこの CI artifact から収集する。

| 系列 | runner image | run ID 一覧 | P50 (s) | P95 (s) |
|---|---|---|---|---|
| head（本 PR, `pytest_python_suite` phase） | GitHub Actions `ubuntu-latest`（artifact の `runner_image`） | 本 PR の CI run（下記「CI run 収集」） | CI artifact から算出 | CI artifact から算出 |
| base（origin/main `83622b88`） | GitHub Actions `ubuntu-latest` | base run（main の `ci_runtime_baseline_v1` artifact） | CI artifact から算出 | CI artifact から算出 |

### CI run 収集（runner-identical 20-run）

- runner image 同一条件の 20-run 統計は **GitHub Actions 上での実行**でのみ取得できる（ローカル WSL2 とは
  runner が異なるため代替不可）。本 PR の CI run（`pytest_python_suite` step の `measurements.jsonl` /
  `xdist_meta.json` artifact）と main の `ci_runtime_baseline_v1` artifact を runner image 一致で照合して P50/P95
  を確定する。
- 本 PR push 後の CI run ID をここに追記する:
  - head run IDs: _（CI 実行後に追記）_
  - base run IDs: _（main の baseline artifact から）_
- ローカル proxy（参考、runner 非同一）: §2 worksteal P50 73.20s / P95 73.43s、§3 5-run 69.76–72.94s。
- `runtime-verification-policy.md` の fallback 規約に従い、本ファイルは committed evidence として全必須フィールド
  を保持する。runner-identical な 20-run P50/P95 の数値妥当性は PR レビュー / 証跡監査（CI run ID 経由）で確定する。

## 5. 単一プロセス統合で顕在化した意味論 drift とその解消

移行前は pytest が 14 個の独立 step（別プロセス）に分割されており、プロセス境界がテスト間の状態汚染を隠して
いた。unified serial（`-n 0`）統合でこれらが単一プロセス化し、以下 2 件の既存 latent 問題が顕在化したため特定・
解消した（nodeid 集合自体は §1 のとおり不変）。

1. **`validate_pr_body` 同名モジュール衝突**:
   `.claude/skills/open-pr/scripts/tests/test_validate_pr_body.py`（`from validate_pr_body import …`）と
   `.claude/skills/impl-review-loop/tests/test_handoff_pr_hygiene_regression.py`（`spec_from_file_location("validate_pr_body", …)`）が
   同一モジュール名で同一ファイルを別インスタンスとしてロードしていた。後者は `sys.modules` 未登録のため、
   `validate_pr_body.py` の frozen `@dataclass` が `sys.modules[cls.__module__].__dict__` を解決する際に前者の
   インスタンスへ束縛され、検証結果が破壊された。→ handoff 側を**一意モジュール名＋exec 前 `sys.modules` 登録**で
   self-consistent 化し、さらに `changed_paths` を非空にして LP058 混入を排除（LP057 単独を検証する本来の意図を保持）。
   旧来の壊れたロードが偽成立させていた xfail を正し、`Refs #N` leniency（xfail）と整形不正参照の正しい fail を
   分離した。assertion の意味は不変。

2. **debounce timing flake（xdist CPU 競合）**:
   `test_session_manifest_debounce.py` は 80ms の debounce window を持つ front-gate を駆動し、`tmp_path` 隔離済み
   （共有 fixture/cwd/artifact race ではない）。xdist の CPU 飽和下では burst invocation 間で window が満了し
   coalescing assertion が flake した（`assert 9 == 1`）。→ `.github/ci/python-test-plan.json` の `parallel_exclude`
   に登録し、xdist parallel run から除外して専用 serial lane（`-n 0`）で実行する。テスト本体は不変。

これらは #1064 の Allowed Paths を Scope Delta で拡張して同一 PR で解消した（serial 統合による実行意味論変更の
安全化が本 Issue の中核であるため）。
