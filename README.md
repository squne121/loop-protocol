# LOOP_PROTOCOL

PC ブラウザ向けのトップビュー型アクション RTS を目指す、Vite + TypeScript + Canvas ベースのゲーム制作リポジトリです。

既存作品の固有名詞、画像、音声、キャラクター、テキストなどは使用せず、トップビュー戦闘、戦闘後の成長、移動拠点的な進行構造といった抽象的なゲーム構造だけを参考にしたオリジナル企画として進めています。

## 現在の状態

- 現在は MVP の基礎実装段階です。
- Canvas 戦闘領域、DOM HUD、固定タイムステップのシミュレーション骨格があります。
- quick save / reset と localStorage ベースの最小保存境界があります。
- Vitest による最小 unit test と GitHub Actions の build / test CI があります。

## MVP 範囲

- 1 戦闘ごとの短い sortie を成立させる
- プレイヤーが Canvas 上で自機を操作して戦場へ局所介入する
- 戦闘結果を resource として保持する
- DOM UI と Canvas 表示を分離する

## 非ゴール

- 既存作品の再現
- 複雑な campaign / territory 管理
- 本格的な audio 実装
- network / multiplayer
- 高品質アセット前提の演出

## ディレクトリ構造

```text
LOOP_PROTOCOL/
├── src/
│   ├── data/       # 武器・敵・ユニットなどの定義
│   ├── entities/   # ID とコンポーネントの基礎型
│   ├── input/      # DOM 入力の正規化
│   ├── render/     # Canvas 描画
│   ├── state/      # GameState と snapshot
│   ├── storage/    # localStorage 境界
│   ├── systems/    # 固定 tick の更新ロジック
│   └── ui/         # DOM HUD / コマンド UI
├── tests/          # Vitest
├── docs/           # 仕様と構成ルール
├── .github/        # CI
├── .devcontainer/  # 開発コンテナ定義
├── assets/         # 人手管理アセット
└── LICENSES/       # ライセンス情報
```

詳細は `docs/dev/directory-structure.md` を参照してください。

## 開発コマンド

```bash
corepack enable
pnpm install
pnpm dev
pnpm test
pnpm build
pnpm preview
```

## アーキテクチャ方針

- `state` と `render` は分離する
- `systems` は DOM / Canvas API に依存しない
- 描画は `requestAnimationFrame`、更新は固定タイムステップ 60Hz で進める
- UI は DOM、戦闘表示は Canvas に分ける
- データ定義は `src/data` に寄せる

より詳細な制約は `CLAUDE.md` と `docs/adr/0001-architecture-baseline.md` にあります。

## AI / NotebookLM 運用

- Claude Code を前提に、壊れにくい責務分離と小さな差分で開発します。
- NotebookLM は外部の調査、README 構成レビュー、ディレクトリ構造レビュー用途で使います。
- NotebookLM のローカル資産は Git 管理しません。
- 実装変更は Issue / PR 単位で扱い、`main` には直接積まず PR 経由で統合します。

## 今後の予定

1. 戦闘ループの具体化
2. 報酬と強化導線の接続
3. データ定義の具体化
4. テスト拡張とブラウザ検証の追加

## ライセンスとアセット

- ソースコードとアセットは分離して扱います。
- `assets/` は人手管理であり、AI は明示指示なしに直接編集しません。
- ライセンスの詳細は `LICENSES/` と各ファイルの情報を確認してください。
- 脆弱性報告は公開 Issue ではなく `SECURITY.md` の方針に従ってください。
