# LOOP_PROTOCOL Claude Code 運用入口

この文書は、`LOOP_PROTOCOL` で AI が最初に読む短い入口です。
詳細な実装手順や docs の更新規則は `.claude/rules/project-constitution.md` を参照してください。

## まず読む順序

1. 対象の GitHub Issue 本文と、必要ならコメント
2. この `CLAUDE.md`
3. `.claude/rules/project-constitution.md`
4. `docs/product/requirements.md`
5. `docs/dev/current-focus.md`
6. 関連する `docs/adr/*.md`、`docs/product/features/<feature>.md`、`docs/dev/directory-structure.md`
7. プレイテスト関連作業を行う場合: `docs/product/playtest-protocol.md`, `docs/product/playtest-log.md`

### agent ops task の read plan 例外

`agent-ops-review` タスク（Codex agent 設定の検証・棚卸し等）を実施する場合は、
通常の読み順序より先に以下を参照する:
- `scripts/agent_ops_inventory.py --task-kind agent-ops-review` で inventory artifact を生成する
- 詳細な MUST_READ / DO_NOT_READ_INITIAL_ONLY は同スクリプトの `--help` と artifact schema を参照する
- `DO_NOT_READ_INITIAL_ONLY` は初期読込除外のみであり、追加読込は禁止ではない


## 不変のアーキテクチャ原則

- `src/state` と `src/render` は完全に分離する。
- `src/systems` の更新ロジックは DOM や Canvas API に依存しない。
- 描画は `requestAnimationFrame`、シミュレーションは固定タイムステップ 60Hz のアキュムレータで進める。
- UI は DOM、戦闘表示は Canvas として分離する。
- 武器、敵、ユニットなどのデータ定義は `src/data` に寄せる。
- 永続化は `src/storage` の snapshot 境界を通す。

## 絶対非ゴール

- 既存作品の固有名詞、画像、音声、キャラクター、テキストの流用
- 既存作品の直接再現
- Issue や spec にない campaign / territory / network / multiplayer の追加
- 高品質アセット前提の演出を先に作ること
- `systems` 層から描画や DOM を直接触ること

## 保護領域

- `assets/` と `LICENSES/` は人手管理とし、明示指示がない限り直接編集しない。
- ライセンス判断が必要な変更は人間判断へ戻す。

## 現在の開発フェーズ

- 現在は `M4: Upgrade Loop (v0.4.x)` を進行中。
- M3 の実装と自動検証は完了済みだが、M3 parent close / milestone readback の最終判断は #733 の closure gate に従い、この文書だけで close 済み扱いしない。
- 現在は resource consumption と最小 upgrade loop を優先する。
- 詳細な優先順位は `docs/dev/current-focus.md` を参照する。

## 最低限の確認コマンド

- `pnpm typecheck`
- `pnpm lint`
- `pnpm test`
- `pnpm build`
- Python テスト: `uv run pytest <テストパス>`（推奨既定）
  - 例外: POSIX shell 互換性・CI イメージ制約・ツール未導入環境では `python3 -m pytest` を許容。その場合は VC または PR 本文に理由を明記する

## 詳細ルールの置き場

- 実装手順と検証: `.claude/rules/project-constitution.md`
- 全体要件と非ゴール: `docs/product/requirements.md`
- 現在の優先順位: `docs/dev/current-focus.md`
- 構成ルール: `docs/dev/directory-structure.md`
- アーキテクチャ理由: `docs/adr/0001-architecture-baseline.md`
- 動作検証 AC の運用規約: `docs/dev/runtime-verification-policy.md`
- Secret Inventory と no-secret 運用境界: `docs/dev/secret-policy.md`
- `docs/product/**` を変更する場合: `docs/product/CLAUDE.md` の最小ルールを参照する

## エージェントランレポートと振り返りインデックス

エージェントランの完了後は以下の 3 つのアーティファクトで記録・分析を行う:

| アーティファクト | 責務 |
|---|---|
| `agent_session_manifest` | セッション中の内部追跡（読み取りファイル・ツール呼び出し） |
| `agent_run_report` (`agent_run_report/v1`) | 個別ランの公開可能要約・AC 達成状況・`evidence_refs`・`commands_summary` |
| `agent_retro_index` | 複数ランの横断インデックス・friction パターン・`follow_up_issues` 集約 |

詳細な責務差分・phase stop conditions・review correction 手順・follow-up Issue 記録方法は
`docs/dev/agent-run-report.md` を参照する。
レトロインデックスの schema と運用手順は `docs/dev/agent-retro-index.md` を参照する。

## Hook Boundary と post-run verifier

hook（PreToolUse / PreWrite 等）は **diagnostic/prevention レイヤー** であり、カノニカルゲートではない。
**post-run verifier が canonical gate** である。hook の通過は AC 達成の証明にならない。
詳細は `docs/dev/agent-run-report.md` の「Hook Boundary Policy」セクションおよび `docs/dev/hook-boundaries.md` を参照する。
