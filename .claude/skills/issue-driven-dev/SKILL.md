---
name: issue-driven-dev
description: LOOP PROTOCOL プロジェクト固有の Issue 駆動開発ワークフロー（EPIC フロー）を強制するスキル。GitHub Issue を読み、実装 Plan を提示して人間の承認を待ち、承認後に実装し、pnpm typecheck/lint/test/build を通過させてから PR を作成する。「#数字」「Issue を実装」「Issue駆動」「PR化」「GitHub Issue」「implement」などのキーワードを含む依頼、または Issue 番号を示す発言に対して必ずこのスキルを使用すること。1 Issue = 1 PR ルールを厳守する。承認前のコード変更は絶対に行ってはならない。
---

# Issue 駆動開発ワークフロー（LOOP PROTOCOL 専用）

このスキルは **EPIC フロー（Explore → Plan → Implement → Commit）** を強制する。  
目的はアーキテクチャ破壊とスコープ肥大化（ハルシネーション）の防止にある。

---

## フェーズ 0: Issue の読み込み

**コードに一切触れる前に必ず実行する。**

```bash
gh issue view <番号> --repo squne121/loop-protocol
gh issue view <番号> --repo squne121/loop-protocol --comments
```

確認すること：
- タイトル・本文・受け入れ条件
- コメント（仕様変更や方針転換が含まれることがある）
- 関連 Issue（「関連」セクション）

---

## フェーズ 1: Explore（探索）

Issue で言及されているファイル・システムを調査する。

**必ずCLAUDE.md の制約を確認してから探索を始める：**

```bash
cat CLAUDE.md
```

CLAUDE.md に定義されたアーキテクチャ制約：
- `src/state` と `src/render` は完全分離（相互参照禁止）
- `src/systems` は DOM / Canvas API に依存してはならない
- 武器・敵パラメータ等のデータは `src/data` 配下の外部ファイルで管理
- `assets/` と `LICENSES/` は人間が手動管理 → **AI は直接編集禁止**

---

## フェーズ 2: Plan（計画）

> **絶対ルール：Plan への明示的な承認を得るまで、コードを一切変更してはならない。**

以下の形式で Plan を提示し、人間の返答を待つ：

```
## 実装計画 — Issue #<番号>: <タイトル>

### 変更対象ファイル
- `path/to/file.ts` — 変更理由

### 変更しないファイル（スコープ外）
- `assets/` — ライセンス管理領域（手動管理）
- （その他あれば列挙）

### 実装ステップ
1. 〜を追加する
2. 〜を修正する
3. 〜にテストを追加する

### CLAUDE.md 制約チェック
- [ ] src/state と src/render の分離を維持する
- [ ] src/systems は DOM/Canvas に依存しない
- [ ] データ定義は src/data に配置する
- [ ] assets/ を直接編集しない

### 受け入れ条件への対応方針
- [ ] （Issue の受け入れ条件を転記し、対応方法を記述）

### 非ゴール（やらないこと）
- （Issue の非ゴールセクションをそのまま転記）

承認をいただければ実装を開始します。
```

---

## フェーズ 3: Implement（実装）

「OK」「進めて」「承認」等の明示的な返答を確認してから実装を開始する。

実装中の制約：
- Plan 外のファイルを勝手に変更しない
- スコープ外の改善・リファクタリングは行わない（別 Issue で管理する）
- 1 Issue = 1 PR を厳守する（別の問題を発見しても現 Issue のみ対応）

---

## フェーズ 4: 検証（Commit 前）

実装後、以下を **この順序で** 実行する。エラーが出た場合は修正してから次へ進む。

```bash
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```

4 コマンド全て通過してから PR を作成する。途中で失敗した場合は PR を作らない。

---

## フェーズ 5: PR 作成

```bash
# 変更ファイルのみを明示的に add する（git add -A は使わない）
git add <変更ファイルのパス>

git commit -m "$(cat <<'EOF'
<コミットメッセージ>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"

gh pr create --title "<タイトル>" --body "$(cat <<'EOF'
## Summary
- 変更内容の要点（箇条書き）

## 受け入れ条件の達成状況
- [x] 条件 1 — 達成（根拠）
- [ ] 条件 2 — 未達成（理由）

## 検証コマンド結果
- `pnpm typecheck`: ✅ 通過
- `pnpm lint`: ✅ 通過
- `pnpm test`: ✅ 通過
- `pnpm build`: ✅ 通過

## 関連 Issue
Closes #<番号>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## よくある失敗パターン（避けること）

| アンチパターン | 正しい行動 |
|---|---|
| Issue を読まずに実装を始める | 必ずフェーズ 0 から始める |
| Plan を提示せずにコードを書く | Plan 提示 → 承認 → 実装 の順を厳守 |
| 複数の Issue を 1 PR にまとめる | 1 Issue = 1 PR を厳守 |
| `assets/` を直接編集する | 人間に依頼するか明示的な指示を待つ |
| 検証コマンドをスキップして PR を作る | 4 コマンド全て通過を確認してから PR |
| スコープ外の改善を行う | 新しい Issue を立てて別 PR で対応 |
| `git add -A` や `git add .` を使う | 変更ファイルのパスを明示的に指定する |
