---
adr_id: "0004"
title: "人間プレイテスト動画証跡ポリシー — GitHub attachment + SHA256 を正本とし ffmpeg フレーム抽出でエージェントレビューする"
status: accepted
decision_date: "2026-06-03"
confirmed_date: null
related_issues:
  - "#576"
  - "#543"
  - "#570"
  - "#571"
  - "#577"
supersedes: []
superseded_by: null
---

# ADR 0004: 人間プレイテスト動画証跡ポリシー — GitHub attachment + SHA256 を正本とし ffmpeg フレーム抽出でエージェントレビューする

## Context

PR #570 のプレイテスト証跡レビューで、動画ファイルが `docs/playtest/` にローカル配置されており、GitHub 上でレビュー不能な状態だった。レビュアー（AI・人間ともに）はファイルパスを知っていても URL を経由してコンテンツにアクセスできないため、証跡としての役割を果たせなかった。

その後 Issue #543 コメントに動画を添付し、GitHub-hosted URL（`https://github.com/user-attachments/...`）を取得することで reviewable な証跡とすることができた。

また、Claude Code による動画の意味認識（動画ファイルを直接解析してゲーム状態を判定する手法）は不安定であり、再現性がない。ffmpeg でキーフレームを切り出し、静止画として人間が確認するフローが運用上安定している。

この判断を今後のプレイテスト運用全体に適用するため、方針を ADR として記録する。

## Considered Options

**Option A**: ローカルパス（`docs/playtest/*.mp4`）を証跡として許容し続ける
- メリット: 追加手順が不要
- デメリット: GitHub 上のレビュー不能が継続する。PR レビュアーが動画を確認できない

**Option B**: GitHub attachment URL を必須とし、ローカルパス単独を reviewable evidence から除外する
- メリット: GitHub URL 経由で誰でもアクセス可能。URL + SHA256 で内容同一性を検証できる
- デメリット: アップロード手順が必要。機密映像は誤って公開リポジトリに添付しないよう注意が必要

**Option C**: 外部ストレージ（S3, GCS 等）を使う
- メリット: サイズ上限や認証を柔軟に設定できる
- デメリット: プロジェクト規模に対してオーバーエンジニアリング。インフラ依存が増える

## Decision

**Option B を採用する。**

GitHub Issue / PR comment の attachment URL（`https://github.com/user-attachments/...`）を reviewable artifact の正本ロケーターとする。SHA256 を content identity として必須とする。

### Policy 1: Reviewable Artifact Locator

GitHub attachment URL が reviewable video evidence の必須要素である。具体的には `https://github.com/user-attachments/` または `https://private-user-images.githubusercontent.com/` 形式の URL を指す。

ローカルパス（例: `docs/playtest/foo.mp4`）は **temporary provenance** であり reviewability を満たさない。local-only path は証跡 YAML の `artifact_url` に記録してはならない。ローカルパスのみを持つ動画は reviewable evidence として扱わない。

### Policy 2: Content Identity = SHA256

artifact_url と artifact_sha256 の組み合わせが証跡の完全性を保証する。SHA256 は以下のコマンドで生成する:

```bash
sha256sum <video_file>
# または macOS の場合
shasum -a 256 <video_file>
```

### Policy 3: 動画意味認識を自動 gate に使わない

AI エージェントによる動画ファイルの意味認識（動画内容を直接解析してゲーム状態を判定する行為）を自動 gate として使用することを禁止する。理由: 再現性・安定性が低く、false positive / false negative が多い。

動画を証跡として用いる場合、機械 gate の主証跡は構造化 YAML / Markdown + 自動テストとする。動画は **監査補助** であり、機械 gate ではない。

### Policy 4: ffmpeg Frame Extraction Contract

エージェントが動画の特定シーンをレビューする場合は、**人間が指定した timestamp** に基づいて ffmpeg でフレームを抽出し、静止画として確認する。エージェントが自律的に動画全体をスキャンすることは禁止する。

#### 入力仕様

| フィールド | 説明 |
|---|---|
| `video_path` | GitHub attachment からダウンロードした動画ファイルのローカルパス |
| `timestamp` | 人間が指定した確認対象の時刻（`HH:MM:SS` 形式） |
| `tolerance_window` | ±1 秒（tolerance: ±1s）。指定 timestamp ±1s の範囲でフレームを抽出する |

#### コマンドテンプレート

```bash
# 指定 timestamp の前後 1 フレームを抽出する
ffmpeg -ss <timestamp> -i <video_path> -vframes:v 1 -q:v 2 <output_frame_path>
```

パラメータ:
- `-ss <timestamp>`: シーク位置（`HH:MM:SS` 形式または秒数）
- `-i <video_path>`: 入力動画ファイルパス
- `-vframes:v 1`: 1 フレームのみ抽出
- `-q:v 2`: JPEG 品質（2 = 高品質）
- `<output_frame_path>`: 出力ファイルパス（例: `artifacts/frame_<timestamp>.jpg`）

tolerance ±1s の検証が必要な場合は `-ss` を `<timestamp - 1s>` / `<timestamp + 1s>` に変えて 3 フレームを比較する。

#### 出力仕様

| フィールド | 説明 |
|---|---|
| `output_frame_path` | 抽出フレームのローカルパス（`artifacts/` 配下） |
| `frame_sha256` | 抽出フレームの SHA256 ハッシュ |
| `observed_result` | 人間によるフレーム確認の結果テキスト |

#### 自律スキャン禁止

エージェントは動画全体を自律的にスキャン・解析してはならない。timestamp は人間が PR レビューコメントまたは runbook 手順として明示的に指定する。

### Policy 5: Evidence Schema

証跡 YAML の必須フィールドを以下のように定義する。このスキーマは #571 collector 出力スキーマと key を揃える。

```yaml
# playtest evidence schema v1
artifact_url: "https://github.com/user-attachments/..."    # GitHub attachment URL（必須）
artifact_sha256: "<sha256hex>"                              # 動画ファイルの SHA256（必須）
artifact_bytes: <integer>                                   # 動画ファイルのバイト数
mime_type: "video/mp4"                                     # MIME type（推奨: video/mp4）
codec_profile: "H.264"                                     # コーデック（推奨: H.264）
tested_commit: "<git sha>"                                  # テスト対象コミット SHA
manual_operator: "<GitHub username>"                        # 実施した人間のアカウント
executed_at: "<ISO 8601 datetime>"                          # 実施日時（UTC）
scenario: "<scenario name>"                                 # プレイテストシナリオ名
human_confirmed_claims:                                     # 人間が確認した主張のリスト
  - "<claim1>"
  - "<claim2>"
```

### Policy 6: Project Video Size Policy

プロジェクトポリシーとして動画は **≤10MB** に圧縮してから GitHub に添付する。これは GitHub の制限（free plan: 10MB、paid plan: 100MB）とは独立したプロジェクト運用方針である。GitHub plan の上限が変わってもこのポリシーは維持する。

理由: PR レビュー時のダウンロード時間を短縮し、フレーム抽出の処理時間を削減するため。

### Policy 7: 推奨コーデック

動画フォーマットは **H.264 エンコードの mp4**（`video/mp4`）を推奨する。H.264 は ffmpeg・ブラウザ・主要プラットフォームで広く対応しており、フレーム抽出の再現性が高い。

## Consequences

### 肯定的影響

- GitHub URL 経由で AI・人間の両レビュアーが動画にアクセス可能になる
- SHA256 による content identity で証跡の改ざん検出が可能になる
- ffmpeg の人間指定 timestamp フローで再現性・安定性が向上する
- evidence schema が #571 collector 出力と key を揃えることで、将来の自動化が容易になる

### 否定的影響 / トレードオフ

- 動画をローカルに置くだけでなく GitHub に添付するという手順が増える
- 機密性のあるプレイテスト映像は誤って公開リポジトリに添付しないよう注意が必要（公開リポジトリの attachment は認証不要でアクセス可能）
- ffmpeg がローカルにインストールされていない環境ではフレーム抽出ができない

### 一時的な不整合

現行の `docs/playtest/manual-playtest-runbook.md` は動画保存先として `docs/playtest/` を許容しており、この ADR の Policy 1 と一時的に不整合がある。この不整合は **#577** が runbook を更新することで解消する。

## References

- Issue #576（本 ADR の実装 Issue）
- Issue #543（動画証跡管理の問題発見と GitHub attachment 解決）
- PR #570（ローカルパス配置の問題が発覚したプレイテスト PR）
- Issue #571（環境メタデータ自動採取・証跡 YAML 化 — evidence schema との整合対象）
- Issue #577（runbook 更新 — `manual-playtest-runbook.md` を本 ADR に沿って更新する）
