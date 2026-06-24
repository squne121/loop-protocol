# CI Runtime Baseline — Issue #1064 (python-test pytest-xdist 並列化 migration)

このドキュメントは Issue #1064 の runtime 観測 AC（AC3 / AC4 / AC5 / AC9 / AC11）の committed evidence
artifact である。`docs/dev/runtime-verification-policy.md` の **immediate** 規約に従う。AC11 の base/head
20-run P50/P95 は **実 GitHub Actions runner で収集した実測値**であり（§4）、run ID 一覧で監査可能とする。

## 0. 測定環境サマリ

| 項目 | 値 |
|---|---|
| base commit（before） | `83622b88d600167586eb915bad627f90f91617f0`（origin/main、14 分割直列 pytest） |
| base 測定ブランチ | `bench-base-1064`（origin/main + bench dispatch のみ追加。python-test job は origin/main と同一） |
| head commit（after） | `88585899`（python-test-plan SSOT + xdist 並列 + serial lane） |
| pytest-xdist version | `3.8.0`（`pyproject.toml`: `pytest-xdist>=3.8,<4`、`uv.lock` 固定） |
| pytest / Python | `9.0.3` / `3.12` |
| worker 数（固定） | **`-n 4`**（`.github/ci/python-test-plan.json` の `xdist.workers`。CI runner の vCPU 数に一致する固定値。`-n auto` の CPU 依存を排除） |
| scheduler（採用） | **`loadscope`**（固定 -n4 比較で最速。§2） |
| CI runner image | GitHub Actions `ubuntu-latest` → `ubuntu24/20260615.205.1`（base/head の全 20-run で一致。§4） |
| ローカル証跡環境 | WSL2 Ubuntu 24-core（§2 scheduler 比較・§1 nodeid 等価の実測環境。CI runner とは別） |

## 1. AC3 — migration-set の collected nodeid 等価性（分割実行 ⇔ unified）

`collect_nodeids_plugin`（`pytest_collection_finish` で node ID を JSON 出力。stdout 解析を排除）で収集。

| 集合 | nodeid 件数 | sha256（sorted nodeids） |
|---|---|---|
| 移行前 14 分割 step の target union（legacy） | 4247 | `f81899a30ef7ebe0ca46ed1882fd26292ae31ca3c11b2a989b71a5fa0d5f1c77` |
| unified plan の scope（本 PR 追加 `scripts/ci/tests/` を除外） | 4247 | `f81899a30ef7ebe0ca46ed1882fd26292ae31ca3c11b2a989b71a5fa0d5f1c77` |

- `migrated_equals_legacy: true`。**件数・集合・hash 完全一致**。pytest 実行対象は分割→統合で不変。
- 本 PR は `scripts/ci/tests/`（loader の validation テスト 52 件）を**新規追加**する。これは migration による
  drift ではなく本 PR 自身の test であり、unified full scope は 4247 + 52 = 4299 件となる。
- unified serial（`-n 0`、full scope）run は PASS（4237 passed 相当 + 追加 loader テスト、ローカル実測）。
  CI では head の python-test job（§4、20-run すべて green）が unified 実行の pass を継続的に裏付ける。

## 2. AC4 — 固定 worker 数（`-n 4`）での scheduler 比較

`-n 4` 固定、各 scheduler 3 run（WSL2、CI=true、`parallel_exclude` を除いた parallel scope、4284 tests）。
**`-n auto`（CPU 依存）ではなく固定 worker 数で比較**（OWNER review 反映）。

| scheduler | runs (s) | P50 (s) | P95 (s) | all_pass |
|---|---|---|---|---|
| **`loadscope`（採用）** | 56.42 / 56.15 / 58.25 | **56.42** | **58.07** | true |
| `load` | 64.62 / 64.95 / 65.61 | 64.95 | 65.54 | true |
| `worksteal` | 71.48 / 69.61 / 69.64 | 69.64 | 71.30 | true |

### 採用 scheduler の選択理由（loadscope）

- 固定 `-n 4` で **`loadscope` が最速**（P50 56.4s、worksteal 比 ▲19%、load 比 ▲13%）。
- 理由は **module-scope fixture の再利用**: `loadscope` は同一 module/class の test を同一 worker に割り当てる
  ため、module 単位の重い fixture が worker 間で重複実行されない。本 suite は module 数（≈137）が worker 数
  （4）を大きく上回り粒度が細かいため、scope grouping による負荷偏りは生じず、fixture 再利用の利得のみが効く。
  この利得は CPU コア数ではなく fixture 実行回数に由来するため CI runner にも転移する。
- いずれの scheduler も all_pass（collection mismatch / worker crash なし）。

## 3. AC5 — 採用 parallel command の連続実行（race / crash / collection mismatch）

採用 command（`-n 4 --dist loadscope`）の連続実行で flake を検査する。

- **CI 上の 20 連続 run**（§4 head）がすべて green であり、20 回連続の race/crash/collection mismatch 無しを
  実証する（5 回要件を上回る）。
- timing-sensitive な `test_session_manifest_debounce.py` は `parallel_exclude` で xdist から除外し、専用
  serial lane（`-n 0`）で実行する（§5）。serial lane: 10 passed。

## 4. AC11 — base/head 同一 runner 条件 20 run の P50/P95・run ID・runner image

`python-test` job は step ごとの `elapsed_ms` を `measurements.jsonl`（→ `ci_runtime_baseline_v1` artifact）に
記録する。`python_test_bench` dispatch（python-test job のみ実行、runner 条件は通常 run と同一）で base/head
各 20 run を収集し、`runner_image` の `ImageVersion` が一致する run のみで P50/P95 を算出した。

比較指標は §「python-test 性能比較式」に従う:

```text
python_pytest_total_ms = pytest_parallel_ms + pytest_serial_ms   # head（after）
python_pytest_total_ms = Σ(phase_id==pytest_skills, step != codex)  # base（before, 14 分割直列）
```

| 系列 | runner image | matched run | P50 (ms) | P95 (ms) |
|---|---|---|---|---|
| **base**（before、origin/main 14 分割直列） | `ubuntu24/20260615.205.1` | 20 / 20 | **221130.5** | **228195.5** |
| **head**（after、`-n 4 --dist loadscope` + serial lane） | `ubuntu24/20260615.205.1` | 20 | **77882.0** | **80564.4** |

### before / after

- **P50: 221130.5 ms → 77882.0 ms（~2.84×（221130.5 / 77882.0））**
- **P95: 228195.5 ms → 80564.4 ms（~2.83×（228195.5 / 80564.4））**

base/head とも全 20 run が同一 runner image `ubuntu24/20260615.205.1`（image 一致 20/20）であり、runner 条件は
同一。

### run ID 一覧（監査用）

- base（20 run、bench-base-1064、ImageVersion `ubuntu24/20260615.205.1`）:
  28036088936, 28036083400, 28036078505, 28036073564, 28036068600, 28036063458, 28036057865, 28036052729,
  28036047923, 28036042860, 28036037200, 28036032311, 28036026938, 28036021307, 28036016215, 28036011387,
  28036006341, 28036001500, 28035996603, 28035990860
- base の totals_ms（参考、昇順）: 114744, 213330, 217162, 217395, 217506, 219518, 219618, 220233, 220710,
  220823, 221438, 222012, 222491, 222764, 222919, 223894, 224931, 226884, 228138, 229287。先頭 1 件
  (114744) は他より大幅に低い外れ値だが、P50/P95 は中央寄り統計のため影響を受けない（残り 19 run は
  213k–229k に収束）。
- head（20 run、`-n 4 --dist loadscope`、ImageVersion `ubuntu24/20260615.205.1`）:
  28037323602, 28037317697, 28037312573, 28037307642, 28037302157, 28037297001, 28037291865, 28037286924,
  28037281668, 28037276784, 28037271611, 28037266197, 28037260648, 28037254828, 28037249553, 28037244464,
  28037239035, 28037233589, 28037228035, 28037221774

## 5. python-test step 構成と性能比較式（phase 一意化）

python-test の pytest は 3 つの識別可能な step（別 `phase_id`）に分かれる。旧 `pytest_skills` 単一 phase は
parallel/serial/codex を混在させ before/after 比較を曖昧にしていたため分離した（OWNER review 反映）。

| step_id | phase_id | 内容 |
|---|---|---|
| `pytest_parallel` | `pytest_parallel` | python-test-plan SSOT を `-n 4 --dist loadscope` で実行（JUnit `junit-parallel.xml`） |
| `pytest_serial` | `pytest_serial` | `parallel_exclude`（debounce）を `-n 0` で実行（JUnit `junit-serial.xml`） |
| `codex_execpolicy_matrix` | `codex_execpolicy` | codex execpolicy matrix + `tests/codex/`（pytest 比較には含めない） |

```text
python_pytest_total_ms = pytest_parallel_ms + pytest_serial_ms
```

### 単一プロセス統合で顕在化した test-isolation 2 件（Scope Delta で解消）

1. `validate_pr_body` 同名モジュール衝突 → handoff テストを一意モジュール名 + `sys.modules` 事前登録 +
   `changed_paths` 非空化（LP058 切り分け）で解消。assertion 意味は不変。
2. debounce timing flake（80ms window の xdist CPU 競合、`tmp_path` 隔離済で共有 race ではない）→ plan
   `parallel_exclude` + serial lane 分離。テスト本体は不変。

## 6. AC9 — 配布 artifact が python-test 全体を表すこと

- `pytest_parallel` と `pytest_serial` がそれぞれ JUnit XML（`junit-parallel.xml` / `junit-serial.xml`）と
  `--durations` ログを生成し、`python-test-junit-<attempt>` artifact として upload（`if-no-files-found: error`）。
- `verify_python_test_manifest.py` が **parallel + serial の testcase 合計 == scope collected nodeids** を検証:
  ローカル実測で parallel 4289 + serial 10 = **4299 == scope collected 4299**（union_equals_scope: true）。
  これにより JUnit が全体結果を表すこと（serial lane の debounce 10 件を含む）を機械検証する。
- `resolved_workers` は `collect_nodeids_plugin` の controller-side `numprocesses` probe で実測（固定 `-n 4` =
  `resolved_workers: 4`）。`nproc` を worker 数の proxy にしない。`xdist_meta.json` に
  `resolved_workers` / `scheduler` / `xdist_version` を記録。
- `verify_lane_union.py` が `parallel ∪ serial == scope` かつ `parallel ∩ serial == ∅` を CI で fail-closed
  検証する。

## 7. ci_test_selection の false-green 除去（B1）

`generate_ci_test_selection_artifact.py` は change detection を branch 名 `main`（shallow checkout で不在）に
依存せず、**explicit `--base-sha`/`--head-sha` の git diff** で行う。`diff_status`（rc/timeout/stderr/ok）を
artifact に記録し、`diff_status.ok != true` で exit 2（fail-close）。G1 は `collection_status.ok` AND
`diff_status.ok` を必須とする。checkout は `fetch-depth: 0`。変更テスト判定は pytest 規約（`test_*.py` /
`*_test.py`）に厳格化し source ファイル誤検出を排除。実 CI（run 28034942311）で `diff_status.ok: true`、
`changed_test_files` に本 PR の変更テスト 3 件を列挙、`uncovered_changed_test_files: []` を確認済み。
