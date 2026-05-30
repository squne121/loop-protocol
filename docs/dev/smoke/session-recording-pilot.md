---
pilot_date: "2026-05-30"
recording_method: hook-based-metadata-ledger
parent_issue: "#246"
status: in-progress
---

# Session Recording Pilot Smoke Test

## 概要

#246 (research/pilot: AI 駆動 session 記録 pilot smoke test) のパイロット実走記録。
Hook-based metadata ledger 方式による session 記録の動作確認を行う。

## 採用方式

- **方式**: Hook-based Metadata Ledger
- **概要**: Claude Code の hooks（PreToolUse / PostToolUse / Stop 等）を利用して session メタデータを記録する
- **証跡保存先**: `artifacts/` ディレクトリ

## 実行フェーズ記録

### Phase 1: 環境確認

- [ ] hooks 設定の確認
- [ ] artifacts ディレクトリの書き込み権限確認
- [ ] recording スクリプトの動作確認

### Phase 2: パイロット実走

- [ ] session 開始時のメタデータ記録
- [ ] ToolUse イベントの記録
- [ ] session 終了時の証跡出力

### Phase 3: 結果検証

- [ ] 証跡ファイルの生成確認
- [ ] メタデータの正確性確認
- [ ] #246 の AC 達成確認

## 備考

- 実走後にこのファイルに結果を記録する
- 証跡ファイルは `artifacts/session-recording-pilot/` に保存する
