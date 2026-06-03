---
adr_id: "0004"
title: "人間プレイテスト動画証跡ポリシー — GitHub attachment + SHA256 を正本とし ffmpeg フレーム抽出でエージェントレビューする"
status: accepted
decision_date: "2026-06-03"
confirmed_date: "2026-06-03"
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

その後 Issue #543 コメントに動画を添付し、GitHub-hosted URL を取得することで reviewable な証跡とすることができた。

また、Claude Code による動画の意味認識（動画ファイルを直接解析してゲーム状態を判定する手法）は不安定であり、再現性がない。ffmpeg でフレームを切り出し、静止画として人間が確認するフローが運用上安定している。

この判断を今後のプレイテスト運用全体に適用するため、方針を ADR として記録する。

## Considered Options

**Option A**: ローカルパス（`docs/playtest/*.mp4`）を証跡として許容し続ける
- メリット: 追加手順が不要
- デメリット: GitHub 上のレビュー不能が継続する。PR レビュアーが動画を確認できない

**Option B**: GitHub attachment URL を必須とし、ローカルパス単独を reviewable evidence から除外する
- メリット: GitHub URL 経由で誰でもアクセス可能。hash と bytes を再検証できる
- デメリット: アップロード手順が必要。機密映像は誤って公開リポジトリに添付しないよう注意が必要

**Option C**: 外部ストレージ（S3, GCS 等）を使う
- メリット: サイズ上限や認証を柔軟に設定できる
- デメリット: プロジェクト規模に対してオーバーエンジニアリング。インフラ依存が増える

## Decision

**Option B を採用する。**

GitHub Issue / PR comment に添付された **GitHub-hosted attachment URL** を reviewable artifact の正本ロケーターとする。SHA256 は content identity の必須要素であり、URL 記録だけでなく **ダウンロード後の hash / bytes 検証手順まで** を evidence contract に含める。

### Policy 1: Reviewable Artifact Locator

GitHub attachment URL が reviewable video evidence の必須要素である。正本条件は特定ドメイン固定ではなく、**GitHub-hosted issue/PR attachment URL** であることとする。

受理例:

```yaml
artifact_url:
  requirement: "GitHub-hosted issue/PR attachment URL"
  accepted_examples:
    - "https://github.com/user-attachments/..."
    - "https://*.githubusercontent.com/..."
```

ローカルパス（例: `docs/playtest/foo.mp4`）は **temporary provenance** であり reviewability を満たさない。local-only path は証跡 YAML の `artifact.url` に記録してはならない。ローカルパスのみを持つ動画は reviewable evidence として扱わない。

### Policy 2: Content Identity = SHA256 + Bytes Verification

`artifact.url` と `artifact.sha256` は記録必須だが、完全性は **ダウンロード後に検証して初めて成立する**。`artifact.bytes` も同じく取得後に検証する。

標準検証手順:

```bash
curl -L --fail --output evidence.mp4 "$artifact_url"
actual_sha256="$(sha256sum evidence.mp4 | awk '{print $1}')"
actual_bytes="$(wc -c < evidence.mp4 | tr -d ' ')"
test "$actual_sha256" = "$artifact_sha256"
test "$actual_bytes" = "$artifact_bytes"
```

収集時の hash 生成は以下で行う:

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

`ffmpeg -ss` は入力シーク位置やコンテナ構造に依存して exact frame を常に保証するものではない。したがって本 ADR では exact-frame gate を採らず、**timestamp を中心とした tolerance-based review** を標準とする。

#### 入力仕様

| フィールド | 説明 |
|---|---|
| `video_path` | GitHub attachment からダウンロードした動画ファイルのローカルパス |
| `timestamp` | 人間が指定した確認対象の時刻（`HH:MM:SS` 形式） |
| `tolerance_window_sec` | 1。指定 timestamp の前後 1 秒を許容範囲とする |
| `source_video_sha256` | ダウンロード済み動画の SHA256 |
| `expected_observation` | 人間が確認したい観察対象 |

#### コマンドテンプレート

```bash
ffmpeg -hide_banner -loglevel error -ss "$timestamp_minus_1s" -i "$video_path" -frames:v 1 -q:v 2 "$out_minus_1s.jpg"
ffmpeg -hide_banner -loglevel error -ss "$timestamp"          -i "$video_path" -frames:v 1 -q:v 2 "$out_exact.jpg"
ffmpeg -hide_banner -loglevel error -ss "$timestamp_plus_1s"  -i "$video_path" -frames:v 1 -q:v 2 "$out_plus_1s.jpg"
sha256sum "$out_minus_1s.jpg" "$out_exact.jpg" "$out_plus_1s.jpg"
```

#### 出力仕様

| フィールド | 説明 |
|---|---|
| `timestamp_requested` | 人間が指定した基準 timestamp |
| `tolerance_window_sec` | 1 |
| `ffmpeg_version` | 抽出時の ffmpeg バージョン |
| `source_video_sha256` | 入力動画の SHA256 |
| `review_points` | `timestamp_minus_1s` / `timestamp` / `timestamp_plus_1s` ごとの出力 |
| `output_frame_path` | 抽出フレームのローカルパス（`artifacts/` 配下） |
| `frame_sha256` | 抽出フレームの SHA256 ハッシュ |
| `observed_result` | 人間によるフレーム確認の結果テキスト |

#### 自律スキャン禁止

エージェントは動画全体を自律的にスキャン・解析してはならない。timestamp は人間が PR レビューコメントまたは runbook 手順として明示的に指定する。

### Policy 5: Evidence Schema

証跡 YAML の必須フィールドを以下のように定義する。#571 は環境メタデータ collector を提供する Issue であり、本 ADR は **#571 の環境メタデータに artifact / scenario 情報を追加した上位 schema** を定義する。将来の collector / runbook / PR 証跡はこの schema に追従する。

```yaml
schema_version: "playtest_evidence_v1"
artifact:
  url: "https://github.com/user-attachments/..."
  sha256: "<sha256hex>"
  bytes: 1234567
  mime_type: "video/mp4"
  codec_profile: "H.264"
tested_commit: "<git sha>"
manual_operator: "<GitHub username>"
executed_at: "<ISO 8601 datetime>"
environment:
  browser: "<browser/version from #571 collector>"
  viewport: "<viewport payload from #571 collector>"
  device_pixel_ratio: 1
  os: "<platform from #571 collector>"
scenarios:
  - id: "all_enemies_defeated_victory"
    status: confirmed
    human_confirmed_claims:
      - "victory overlay is visible"
    review_points:
      - timestamp: "00:00:12"
        tolerance_window_sec: 1
        output_frame_path: "artifacts/all-enemies-defeated-victory-exact.jpg"
        frame_sha256: "<sha256hex>"
        observed_result: "victory overlay and HUD result match"
  - id: "hp_zero_defeat"
    status: deferred
    deferred_reason: "reviewable timestamp/frame evidence not yet attached"
```

### Policy 6: Project Video Size Policy

プロジェクトポリシーとして動画は **10,000,000 bytes 以下** に圧縮してから GitHub に添付する。これは GitHub の制限（free plan: 10MB、paid plan: 100MB）とは独立したプロジェクト運用方針である。GitHub plan の上限が変わってもこのポリシーは維持する。

理由: PR レビュー時のダウンロード時間を短縮し、フレーム抽出の処理時間を削減するため。

### Policy 7: 推奨コーデック

動画フォーマットは **H.264 エンコードの mp4**（`video/mp4`）を推奨する。H.264 は ffmpeg・ブラウザ・主要プラットフォームで広く対応しており、フレーム抽出の再現性が高い。

### Policy 8: Public Attachment Safety

public repository の attachment URL は、URL を知る者が認証なしで取得できる前提で扱う。したがって、private / sensitive media を public attachment にアップロードしてはならない。

機密性のある動画が必要な場合は、本 ADR の対象外として **private/authenticated storage を人間承認のうえで別 Issue に分離する**。

## Consequences

### 肯定的影響

- GitHub URL 経由で AI・人間の両レビュアーが動画にアクセス可能になる
- hash / bytes の取得後検証により、証跡の再取得と改ざん検出を機械的に確認できる
- ffmpeg の 3 点抽出フローで tolerance-based review を標準化できる
- evidence schema を #571 collector の上位 schema として定義することで、将来の自動化が容易になる

### 否定的影響 / トレードオフ

- 動画をローカルに置くだけでなく GitHub に添付するという手順が増える
- 機密性のあるプレイテスト映像は誤って公開リポジトリに添付しないよう注意が必要
- ffmpeg がローカルにインストールされていない環境ではフレーム抽出ができない

### 一時的な不整合

現行の `docs/playtest/manual-playtest-runbook.md` は動画保存先として `docs/playtest/` を許容しており、この ADR の Policy 1 と一時的に不整合がある。この不整合は **#577** が runbook を更新することで解消する。

## References

- Issue #576（本 ADR の実装 Issue）
- Issue #543（動画証跡管理の問題発見と GitHub attachment 解決）
- PR #570（ローカルパス配置の問題が発覚したプレイテスト PR）
- Issue #571（環境メタデータ collector。将来は本 ADR schema に追従して artifact / scenario 情報を拡張する）
- Issue #577（runbook 更新 — `manual-playtest-runbook.md` を本 ADR に沿って更新する）
