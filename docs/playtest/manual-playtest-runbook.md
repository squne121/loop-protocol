---
doc_id: manual-playtest-runbook
status: active
issue: "#577"
parent_issue: "#543"
required_os: "WSL2 + Ubuntu"
date: "2026-06-04"
adr: "docs/adr/0004-video-evidence-policy.md"
---

# 手動プレイテスト Runbook — WSL2/Ubuntu + Windows Chrome

## 目的

このドキュメントは、WSL2/Ubuntu + Windows Chrome 環境で手動プレイテストを実施し、ADR 0004 に準拠した動画証跡を GitHub へ提出するまでの操作手順を定める。

証跡の正本は **GitHub attachment URL + SHA256 + bytes** である。ローカルファイルパス（`docs/playtest/*.mp4` 等）は証跡の正本とならない。

---

## 前提環境

| 項目 | 要件 |
|------|------|
| OS | WSL2 + Ubuntu（必須） |
| ブラウザ | Windows Chrome（主経路） |
| Node | v20 以上（v22 推奨） |
| pnpm | v9–v10（Node 20）または v11+（Node 22+） |
| ffmpeg | フレーム抽出に必要（`ffmpeg -version` で確認） |

オプション（WSLg 経由）: Linux GUI ブラウザ（Ubuntu 上の Firefox 等）

---

## 対象 URL

### main（共有）URL

`main` へのコミットがマージされると、最新ビルドが以下に公開される:

```
https://squne121.github.io/loop-protocol/
```

### PR プレビュー URL

PR が作成または更新されると、プレビューが以下にデプロイされる:

```
https://squne121.github.io/loop-protocol/pr-<PR番号>/
```

`<PR番号>` を実際の数字に置き換える（例: PR #123 → `/pr-123/`）。プレビュー URL は PR のコメントに自動投稿される。

> PR クローズ後、クリーンアップワークフロー完了後にプレビュー URL は 404 になる。
> フォーク PR ではプレビューデプロイは生成されない。

### GitHub Pages セットアップ（初回のみ）

`Settings → Pages → Source → Deploy from a branch → gh-pages (root)` を設定する（リポジトリオーナーによる一回限りの手動手順）。

---

## 実行前チェック

プレイテスト開始前に環境確認スクリプトを実行する:

```bash
node scripts/check-manual-playtest-env.mjs
```

期待される出力: `[ok] All checks passed.`（終了コード 0）

終了コード 1（失敗）または 2（非対応環境）の場合は「トラブルシュート」セクションを参照する。

---

## Base Path Revalidation URLs

base path の解決を確認するための環境別 URL を以下に示す。

- Local preview: `http://localhost:4173/?playtest_evidence=1`
- GitHub Pages main: `https://squne121.github.io/loop-protocol/?playtest_evidence=1`
- GitHub Pages PR preview: `https://squne121.github.io/loop-protocol/pr-<PR番号>/?playtest_evidence=1`

### 環境別 Validation split

| 環境 | 目的 |
|------|------|
| Local preview | production bundle 動作確認（`pnpm build` + `pnpm preview` による） |
| GitHub Pages main | `/loop-protocol/` base 解決の確認 |
| GitHub Pages PR preview | `/loop-protocol/pr-<N>/` base 解決の確認 |

> **切り分け注記**: base path 修正だけでは #571 の deferred verification 完了を意味しない。Pages source / publish trigger fault は別問題として切り分ける。

---

## Evidence Panel で環境情報を取得する

Evidence Panel は `?playtest_evidence=1` クエリパラメータを付けることで有効化される。上記「Base Path Revalidation URLs」の URL を使用してアクセスする。

クエリパラメータなしでアクセスした場合、Evidence Panel は表示されない（opt-in 設計）。

### 操作手順

1. 上記 URL をブラウザで開く。
2. 画面右上に **Playtest Evidence Panel** が表示される。
3. YAML データがテキストエリアに自動生成される。

### Copy YAML to Clipboard

- **Copy YAML to Clipboard** ボタンをクリックすると YAML がクリップボードにコピーされる。
- ボタンが「Copied!」に変わったらコピー成功。
- Clipboard API が利用できない場合は「Use textarea to copy manually」と表示される。その場合はテキストエリアを手動で選択してコピーする。

### Download YAML

- **Download YAML** ボタンをクリックすると `loop-protocol-playtest-evidence-<ISO8601>.yaml` ファイルがダウンロードされる。

### Evidence Panel 出力スキーマと ADR 提出スキーマの対応

Evidence Panel が生成する YAML と、GitHub への最終提出 YAML はスキーマキーが異なる。以下の対応に従ってマッピングする。

**Evidence Panel 出力（`playtest_evidence_schema_version: v1`）:**

```yaml
playtest_evidence_schema_version: v1
browser:
  name: "Chrome"
  version: "125.0.0.0"
  version_source: "automatic"
  unknown_reason: null
environment:
  viewport: "1920x1080"
  device_pixel_ratio: 1
  os: "Windows 11 + WSL2/Ubuntu"
  commit: "abc1234..."
  commit_unknown_reason: null
```

**GitHub 最終提出（`schema_version: playtest_evidence_v1`）:**

```yaml
schema_version: "playtest_evidence_v1"
environment:
  browser: "Chrome 125.0.0.0"
  version_source: "automatic"
  unknown_reason: null
  viewport: "1920x1080"
  device_pixel_ratio: 1
  os: "Windows 11 + WSL2/Ubuntu"
```

**マッピング規則:**

| Evidence Panel フィールド | ADR 提出フィールド |
|--------------------------|-------------------|
| `browser.name` + `browser.version` | `environment.browser`（結合して "Name Version" 形式） |
| `browser.version_source` | `environment.version_source` |
| `browser.unknown_reason` | `environment.unknown_reason` |
| `environment.viewport` | `environment.viewport` |
| `environment.device_pixel_ratio` | `environment.device_pixel_ratio` |
| `environment.os` | `environment.os` |
| `environment.commit` | `tested_commit`（トップレベルフィールド） |
| `environment.commit_unknown_reason` | `tested_commit_unknown_reason`（トップレベル） |

### Chrome でブラウザバージョンが `unknown` になる場合

`navigator.userAgentData.getHighEntropyValues()` は experimental API であり、Chrome でも取得できない場合がある。`unknown` となった場合は、以下を手動で記録し `version_source` と `unknown_reason` を明示する（無条件 pass は禁止）:

```yaml
environment:
  browser: "Chrome 125.0.0.0"      # chrome://version で確認
  version_source: "manual_input"   # automatic / manual_input / unknown
  unknown_reason: null             # version_source が unknown の場合は必須記述
```

### ローカル環境メタデータ採取（CLI）

GitHub Pages を使わずローカルでビルドした場合は、CLI スクリプトで環境メタデータを採取できる:

```bash
# YAML 出力（デフォルト）
node scripts/collect-playtest-env.mjs

# JSON 出力
node scripts/collect-playtest-env.mjs --json
```

---

## 動画証跡を録画する

### Windows（推奨）

- **Xbox Game Bar** (`Win + G`) → キャプチャ → 録画開始（ブラウザウィンドウを選択）
- **OBS Studio**: ウィンドウキャプチャソースでブラウザを指定して録画

### WSLg（オプション）

```bash
# recordmydesktop 等（WSLg が利用可能な場合のみ）
recordmydesktop --output playtest.ogv
```

### 動画の要件

- フォーマット: **H.264 エンコードの mp4**（`video/mp4`）を推奨
- サイズ: **10,000,000 bytes 以下**（プロジェクトポリシー）
- ファイル名例: `playtest-577-2026-06-04-victory.mp4`

---

## GitHub コメントへ動画を添付する

ローカルに保存した動画ファイルを **GitHub の Issue または PR コメント欄にドラッグ＆ドロップ**（またはファイル選択ダイアログ）で添付する。

添付が完了すると、コメント欄に以下のような URL が挿入される:

```
https://github.com/user-attachments/assets/<UUID>/playtest-577-2026-06-04-victory.mp4
```

この URL が証跡の正本ロケーターとなる。コメントを投稿する前に URL が挿入されたことを確認する。

> **重要**: ローカルパス（例: `docs/playtest/foo.mp4`）は証跡の正本ではない。GitHub attachment URL を必ず取得すること。

> **URL 記録ルール（自動挿入のみ・手入力禁止）**: `artifact_url` には GitHub がコメント欄に**自動挿入した URL をそのまま**記録すること。URL の手入力・推測・整形は禁止。`artifact_url` と `artifact_sha256` は必ず同一ファイルに対応させること（別ファイルの URL を使い回さない）。

---

## 添付 URL・bytes・SHA256 を検証する

GitHub attachment URL を取得した後、以下のコマンドで内容を検証する:

```bash
artifact_url="https://github.com/user-attachments/assets/<UUID>/playtest.mp4"

# ダウンロード
curl -L --fail --output evidence.mp4 "$artifact_url"

# SHA256 ハッシュ取得
actual_sha256="$(sha256sum evidence.mp4 | awk '{print $1}')"

# バイト数取得
actual_bytes="$(wc -c < evidence.mp4 | tr -d ' ')"

echo "sha256: $actual_sha256"
echo "bytes:  $actual_bytes"
```

取得した値を証跡 YAML の `artifact.sha256` と `artifact.bytes` に記録する。

動画収集時のハッシュ生成（ローカルファイルから）:

```bash
sha256sum <video_file>
# macOS の場合
shasum -a 256 <video_file>
```

---

## タイムスタンプを記録する

確認したいシーンのタイムスタンプ（`HH:MM:SS` 形式）を記録する。

例:
- `00:00:12` — 全敵撃破 → ビクトリー表示

タイムスタンプは証跡 YAML の `scenarios[].review_points[].timestamp` に記録し、ffmpeg フレーム抽出の基準点として使用する。

---

## ffmpeg で確認フレームを抽出する

ADR 0004 の ffmpeg フレーム抽出コントラクトに従い、指定 timestamp の前後 1 秒（`tolerance_window_sec: 1`）を抽出する:

```bash
timestamp="00:00:12"
video_path="evidence.mp4"

# timestamp の前後 1 秒を計算
timestamp_minus_1s="00:00:11"
timestamp_plus_1s="00:00:13"

# 3 点抽出
ffmpeg -hide_banner -loglevel error -ss "$timestamp_minus_1s" -i "$video_path" -frames:v 1 -q:v 2 "frame_minus_1s.jpg"
ffmpeg -hide_banner -loglevel error -ss "$timestamp"          -i "$video_path" -frames:v 1 -q:v 2 "frame_exact.jpg"
ffmpeg -hide_banner -loglevel error -ss "$timestamp_plus_1s"  -i "$video_path" -frames:v 1 -q:v 2 "frame_plus_1s.jpg"

# フレームの SHA256 確認
sha256sum frame_minus_1s.jpg frame_exact.jpg frame_plus_1s.jpg
```

> ffmpeg の `-ss` は入力シークのため exact frame を常に保証しない。そのため 3 点抽出による tolerance-based review を標準とする（ADR 0004 Policy 4）。

エージェントによる動画全体の自律スキャン・解析は禁止。timestamp は人間が明示的に指定する。

---

## 提出コメントの YAML schema

GitHub コメントに以下の形式で証跡 YAML を貼り付ける（`schema_version: playtest_evidence_v1`）:

````markdown
## Playtest Evidence

```yaml
schema_version: "playtest_evidence_v1"
artifact:
  url: "https://github.com/user-attachments/assets/<UUID>/playtest.mp4"
  sha256: "<sha256hex>"
  bytes: 1234567
  mime_type: "video/mp4"
  codec_profile: "H.264"
tested_commit: "<git sha または unknown>"
tested_commit_unknown_reason: null  # tested_commit が unknown の場合は必須記述
manual_operator: "<GitHub username>"
executed_at: "<ISO 8601 datetime>"
environment:
  browser: "Chrome 125.0.0.0"
  version_source: "manual_input"   # automatic / manual_input / unknown
  unknown_reason: null             # version_source が unknown の場合は必須記述
  viewport: "1920x1080"
  device_pixel_ratio: 1
  os: "Windows 11 + WSL2/Ubuntu"
scenarios:
  - id: "all_enemies_defeated_victory"
    status: confirmed
    human_confirmed_claims:
      - "victory overlay is visible"
    review_points:
      - timestamp: "00:00:12"
        tolerance_window_sec: 1
        frames:
          minus_1s:
            output_frame_path: "artifacts/all-enemies-defeated-victory-minus_1s.jpg"
            frame_sha256: "<sha256hex>"
            observed_result: "victory overlay visible (approaching)"
          exact:
            output_frame_path: "artifacts/all-enemies-defeated-victory-exact.jpg"
            frame_sha256: "<sha256hex>"
            observed_result: "victory overlay and HUD result match"
          plus_1s:
            output_frame_path: "artifacts/all-enemies-defeated-victory-plus_1s.jpg"
            frame_sha256: "<sha256hex>"
            observed_result: "victory overlay still visible (fading)"
  - id: "hp_zero_defeat"
    status: deferred
    deferred_reason: "reviewable timestamp/frame evidence not yet attached"
```
````

### tested_commit の記録方法

```yaml
# ローカルビルドの場合: git rev-parse HEAD で取得
tested_commit: "abc1234def5678..."
tested_commit_unknown_reason: null

# GitHub Pages / PR Preview の場合: commit SHA を取得できないときは以下を明示
tested_commit: "unknown"
tested_commit_unknown_reason: "GitHub Pages では実行時に commit SHA を取得できないため"
```

`tested_commit` を空欄のままにすることは禁止。commit SHA が不明な場合は必ず `"unknown"` + `tested_commit_unknown_reason` を記録する。

Evidence Panel の `environment.commit` フィールドが `unknown` になった場合も同様に `tested_commit_unknown_reason` を明記する。

### 必須フィールド一覧

| フィールド | 説明 |
|-----------|------|
| `schema_version` | `playtest_evidence_v1` 固定 |
| `artifact.url` | GitHub attachment URL（ローカルパス禁止・自動挿入のみ） |
| `artifact.sha256` | ダウンロード後に `sha256sum` で取得 |
| `artifact.bytes` | ダウンロード後に `wc -c` で取得 |
| `tested_commit` | git SHA または `"unknown"`（空欄禁止） |
| `tested_commit_unknown_reason` | `tested_commit` が `"unknown"` の場合は必須記述 |
| `review_points` | scenarios ごとの timestamp + tolerance_window_sec + frames（3 点） |
| `tolerance_window_sec` | 1（ADR 0004 固定値） |
| `frames.minus_1s` / `frames.exact` / `frames.plus_1s` | tolerance-based review の 3 フレームすべて必須 |

---

## pass / fail / deferred 判定

| 状態 | 条件 |
|------|------|
| **pass** | `artifact.url` が GitHub attachment URL、`sha256`/`bytes` が検証済み、すべての scenario に `review_points`（timestamp + frame）が添付されている |
| **fail** | `artifact.url` がローカルパス、`sha256` 検証失敗、動画 URL はあるが `review_points` なし |
| **accepted_with_deferred** | 一部 scenario が `status: deferred` だが `deferred_reason` を明記し、残りは `confirmed` |

### `unknown` フィールドの扱い

`environment.browser` 等が `unknown` になった場合、**無条件 pass は禁止**。以下を必ず明示する:

```yaml
browser: "unknown"
version_source: "unknown"
unknown_reason: "userAgentData.getHighEntropyValues() が利用不可かつ手動入力もできなかった"
```

`version_source: unknown` かつ `unknown_reason` が空の場合は fail 扱いとする。

---

## トラブルシュート

### `pnpm: command not found`

```bash
# Node 20.x
corepack enable pnpm
corepack prepare pnpm@latest-10 --activate

# Node 22+
corepack enable pnpm
corepack prepare pnpm@latest-11 --activate
```

または:

```bash
npm install -g pnpm
```

### `Port 4173 is already in use`

```bash
lsof -ti:4173 | xargs kill -9
```

その後 `pnpm preview -- --host 127.0.0.1 --port 4173 --strictPort` を再実行。

### ブラウザが `ERR_CONNECTION_REFUSED`

1. プレビューサーバーが起動中か確認（`➜ Local: http://127.0.0.1:4173/` の表示を確認）。
2. `http://` を使用しているか確認（`https://` ではない）。
3. VPN やファイアウォールが localhost をブロックしていないか確認。
4. `http://127.0.0.1:4173` を直接試す。

### WSL2 `localhost` が Windows に転送されない

```powershell
$wslIp = (wsl.exe -d Ubuntu hostname -I).Trim().Split()[0]
netsh interface portproxy add v4tov4 listenport=4173 listenaddress=0.0.0.0 connectport=4173 connectaddress=$wslIp
```

または `%UserProfile%\.wslconfig` の `localhostForwarding=false` を削除して `wsl --shutdown` を実行。

### `pnpm build` が TypeScript エラーで失敗

```bash
pnpm typecheck
```

で読みやすいエラーメッセージを確認してから修正し、再度 `pnpm build` を実行。

### `check-manual-playtest-env.mjs` 終了コード 2（非対応環境）

非 WSL2 環境（ネイティブ Linux、macOS、Windows CMD 等）が検出された。本 Runbook は WSL2/Ubuntu 専用。

### `ffmpeg: command not found`

```bash
sudo apt install ffmpeg
```

### `pnpm-lock.yaml` が見つからない

```bash
pnpm install
```

で `pnpm-lock.yaml` を再生成する。

### Evidence Panel が表示されない

URL に `?playtest_evidence=1` が付いているか確認。ローカルプレビューの場合は `http://localhost:4173/?playtest_evidence=1` でアクセスする。

### GitHub コメントへの動画添付がアップロードエラーになる

動画が 10,000,000 bytes を超えていないか確認。超えている場合は圧縮してから再添付する:

```bash
# H.264 再エンコードで圧縮（例）
ffmpeg -i input.mp4 -vcodec libx264 -crf 28 output.mp4
```

### gh-pages ブランチの古いアーティファクト削除

```bash
git clone --branch gh-pages --single-branch https://github.com/squne121/loop-protocol.git gh-pages-work
cd gh-pages-work
ls -la
# 不要なファイルを確認して削除
git rm <stale-file>
git commit -m "chore: remove stale artifact from gh-pages root"
git push origin gh-pages
```

---

## 関連 Issue / ADR

| リンク | 内容 |
|--------|------|
| [ADR 0004 (0004-video-evidence-policy)](../adr/0004-video-evidence-policy.md) | 動画証跡ポリシー正本（GitHub attachment + SHA256 必須、ffmpeg フレーム抽出コントラクト、`playtest_evidence_v1` schema） |
| Issue #576 | ADR 0004 実装 Issue |
| Issue #543 | 人間プレイテスト動画証跡管理の問題発見と GitHub attachment 解決 |
| Issue #570 | ローカルパス配置の問題が発覚したプレイテスト PR |
| Issue #571 | 環境メタデータ collector（Evidence Panel 実装） |
| Issue #577 | 本 Runbook 更新（日本語化 + ADR 0004 証跡フロー統合） |
