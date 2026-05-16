# Game Overview

トップビューのアクション RTS として必要な体験、主要ループ、画面要件を整理するための文書。

## MVP Loop

- 1 戦闘ごとの sortie を短時間で遊べること。
- プレイヤーは Canvas 上で自機を操作し、戦場へ局所介入する。
- 戦闘結果は resource として残り、次の強化導線へ接続できること。
- UI は DOM、戦闘表示は Canvas に分離すること。

## Current Play Slice

- 戦闘キャンバス
- HUD と telemetry
- quick save / reset
- 武器強化やキャンペーンはまだ仮置き

## Non Goals For Now

- 複雑な campaign / territory 管理
- 本格的な audio 実装
- network / multiplayer
- 高品質アセット前提の演出
