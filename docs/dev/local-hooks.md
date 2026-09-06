---
doc_id: DOC-LOCAL-HOOKS-001
title: prek によるローカル pre-commit hook（staged TS/JS lint trial）
status: trial
related_issue: 2552
related_research_issue: 1933
related_parent_issue: 1932
last_updated_by_issue: 2552
---

# ローカル pre-commit hook（prek trial）

本ドキュメントは、[#1933](https://github.com/squne121/loop-protocol/issues/1933) の research 決定に基づき
[#2552](https://github.com/squne121/loop-protocol/issues/2552) で導入した、
[prek](https://prek.j178.dev/)（Rust 製の pre-commit 互換 Git hook manager）による
**staged TS/JS ファイルに対する軽量 ESLint チェックの小規模 trial** の運用方法を記載する。

本 trial は各開発者の **opt-in** であり、リポジトリの clone / worktree 作成だけでは
ローカル `.git/hooks` に一切の変更を加えない（後述「worktree 環境での挙動」参照）。

## 対象範囲（何をするか / しないか）

- 対象: `pre-commit` stage のみ。`git commit` 実行時に **staged** な TS/JS ファイルへ
  既存 ESLint 設定（`eslint.config.mjs`）で lint をかける。
- 非対象: `pre-push` stage は導入しない（理由は後述）。
- 非対象: full typecheck / test / build / LLM review / autofix はいずれも pre-commit hook に含めない。

設定本体は repo ルートの [`prek.toml`](../../prek.toml) を参照。

## install / bootstrap 手順

trial に参加する開発者は、自分の作業ディレクトリ（**worktree 単位ではなくメイン checkout 単位**、後述）で以下を実行する。

```bash
# 1. prek バイナリの用意（package.json devDependencies に pin 済みのバージョンを使う）
pnpm install

# 2. Git hook の bootstrap（.git/hooks/pre-commit を prek 経由の shim に置き換える）
pnpm exec prek install
```

`pnpm exec prek install` はデフォルトで `pre-commit` stage の shim のみを `.git/hooks/pre-commit` に
インストールする（`prek.toml` の `default_install_hook_types = ["pre-commit"]` により、
`pre-push` 等の他 stage の shim は作成されない）。

## version pin 方法

`prek` は npm パッケージ [`@j178/prek`](https://www.npmjs.com/package/@j178/prek) として配布されている。
`package.json` の `devDependencies` には pnpm/npm の [dependency alias 構文](https://pnpm.io/aliases)
（`npm:<package>@<version>`）を使い、キー名 `prek` で固定バージョンを pin している。

```jsonc
{
  "devDependencies": {
    "prek": "npm:@j178/prek@0.5.2"
  }
}
```

alias を使う理由は、npm の scoped package 名（`@j178/prek`）をそのまま `devDependencies` の
キーにすると `package.json` 中に quote 付きの `"prek"` という文字列が現れず、
Issue #2552 の Verification Commands（`rg -n "\"prek\"" package.json`）が意図した pin の
存在確認をできないため。alias 経由でも `node_modules/.bin/prek` は通常の
`@j178/prek` インストール時と同じバイナリ名で解決される。

バージョンを上げる場合は `package.json` の pin 値（`npm:@j178/prek@<version>`）と
`pnpm-lock.yaml` を同一 PR で更新する。`^0.5.2` のような range pin ではなく exact pin とし、
`pnpm install` 実行タイミングによる挙動差分を防ぐ。

## bypass 手順

一時的に hook を無効化してコミットしたい場合は Git 標準の bypass を使う。

```bash
git commit --no-verify -m "..."
```

`--no-verify` は `pre-commit` hook 全体（本 trial の ESLint チェックを含む）をスキップする。
CI 側の `pnpm lint` / `pnpm typecheck` / `pnpm test` / `pnpm build` は本 bypass の影響を受けず、
push 後・PR 上で独立に実行される（後述「test-runner・CI・open-pr validator との責務分離」）。

## uninstall 手順

trial から抜ける場合、または prek 経由の hook を無効化したい場合:

```bash
pnpm exec prek uninstall
```

`prek uninstall` は `prek install` によって書き換えられた `.git/hooks/pre-commit` を削除する。

## 既存 hook の保存・復元（`.legacy` 退避）

`prek install` 実行時に、既に `.git/hooks/pre-commit` に prek 管理外の hook スクリプトが
存在する場合、`prek` はそれを上書きする前に `.git/hooks/pre-commit.legacy` として退避する
（prek 本体の標準的な事前存在 hook 保護動作）。

- 既存 hook を残したまま prek 管理の hook と併用したい場合は、`.git/hooks/pre-commit.legacy`
  の内容を prek 管理の hook（`local` repo の hook 定義）または呼び出し元スクリプトへ
  手動で統合する。
- `prek uninstall` は `prek` が作成した shim のみを削除する。`.git/hooks/pre-commit.legacy`
  への自動復元は行わないため、必要であれば手動で `mv .git/hooks/pre-commit.legacy .git/hooks/pre-commit`
  を実行して復元する。

## worktree 環境での挙動

`.git/hooks` は `git worktree` 間で **共有される**（worktree ごとに独立した `.git/hooks` は
持たない。`git worktree` の内部実装上、各 worktree は共通の `$GIT_DIR/hooks` を参照する）。

このため:

- 本 Issue（#2552）のスコープでは、**実際の開発者端末の実 `.git/hooks` へ `prek install` を
  実行しない**。動作確認はすべて `scripts/dev-hooks/verify_precommit_trial.sh` が作成する
  isolated temporary git repository（メインリポジトリとは無関係な `mktemp -d` 配下の別 repo）
  で完結させる。
- 実端末へ `prek install` を実行するかどうかは、本 trial 導入後に **各開発者の opt-in 判断**
  とする。ある worktree で `prek install` を実行すると、同じ `.git` を共有する他の worktree
  にも同じ hook shim が適用される点に留意する。

## 依存不足時の挙動（実機検証結果 — 2 通りに分かれる）

`prek` バイナリが利用できない場合の挙動は、**hook をインストール済みかどうかで結果が異なる**。
以下は `scripts/dev-hooks/verify_precommit_trial.sh` の isolated fixture 上で実機検証した結果であり、
いずれも捏造ではなく実際の exit code / 出力に基づく。

### ケース1: `prek install` を一度も実行していない（デフォルト状態、fail-open）

`git clone` / `pnpm install` しただけの状態では `.git/hooks/pre-commit` に prek の shim が
存在しない。この場合 Git は何の hook も実行しないため、staged 内容に lint エラーがあっても
**コミットはそのまま成立する（fail-open）**。これは「本 trial は opt-in であり、
`prek install` を実行しない限り何も変わらない」という設計そのものであり、意図した挙動である。

### ケース2: `prek install` 実行済みだが `prek` バイナリが後から解決できなくなった（fail-closed）

`prek install` が生成する shim（`.git/hooks/pre-commit`）は、install 時点で見つかった
`prek` バイナリの絶対パスをハードコードし、それが実行不可になった場合のみ `PATH` 上の
`prek` にフォールバックする。**インストール時の絶対パスも `PATH` 上の `prek` も
どちらも解決できない場合、生成された shim スクリプト自身が
`exec: prek: not found` 相当のエラーで異常終了し、Git は非 0 exit code を検知して
コミットを中断する（fail-closed）**。

実機検証ログ（抜粋、isolated fixture）:

```text
.git/hooks/pre-commit: 13: exec: prek: not found
EXIT_CODE=1
```

- これは「lint エラーを検知してブロックした」のではなく、**hook 実行基盤そのものが
  壊れているために全てのコミットがブロックされる**という状態である。lint 内容とは無関係に
  ブロックされる点に注意する。
- `node_modules` の削除・別環境への worktree 複製・`pnpm install` 未実行などで
  この状態に陥る可能性がある。
- 復旧手段: `pnpm install` を再実行して `prek` バイナリを復元するか、
  `pnpm exec prek uninstall`（またはリポジトリを再 clone した端末で `prek` が
  解決できるなら通常の uninstall 手順）で shim を取り除くか、応急的に
  `git commit --no-verify` で bypass する。

まとめると、本 trial の依存不足時の安全側動作は「**install しなければ何も起きない
（fail-open）**」であり、「**install 済みで壊れた場合は lint 内容に関係なく
コミット不能になる（fail-closed）**」という非対称な挙動を持つ。**「`prek` バイナリ不在時は
常に fail-open で安全側に倒れる」という単純化した理解は誤り**であり、本 trial の
運用者はこの非対称性を理解した上で導入判断を行う。

## partial-stage 挙動（staged ファイルのみが対象）

`pre-commit` stage の hook は、Git の staging area にある内容（`git add` 済みの内容）に対して
実行される。作業ツリー上の unstaged な変更は hook の対象外である。

- `git add` していないファイルの変更は lint 対象に含まれない。
- 同一ファイル内で staged 部分と unstaged 部分が混在する場合（`git add -p` 等での部分ステージ）、
  prek は commit 対象のファイル全体を lint にかける（ファイル単位の filter であり、
  hunk 単位のフィルタリングではない）。unstaged な hunk のみを個別に lint することはできない。

## pre-push を今回導入しない理由

[#1933](https://github.com/squne121/loop-protocol/issues/1933) の AC6 決定に基づき、本 trial では
`pre-push` stage を導入しない。理由:

- `pre-push` は push 対象コミット全体を対象にした重い検証（type check / test / build 相当）
  との親和性が高いが、それらは既に CI（GitHub Actions）で確実に実行されており、
  ローカル `pre-push` での重複実行は開発者の push 待ち時間を増やすコストに見合う効果が薄い。
- ローカル hook はネットワーク遮断・bypass（`--no-verify`）・端末間差異の影響を受けるため、
  push ブロックの正本には向かない。push ブロックの正本は branch protection / required checks
  であるべきという既存方針（`docs/dev/workflow.md` の CI・required checks 方針）と整合させる。
- 本 trial はまず pre-commit の staged lint という最小スコープで効果測定を行い、
  `pre-push` 拡張の要否は trial 結果を踏まえた別判断とする（本 Issue のクローズ後の
  follow-up で判断、#1932 Remaining Parent Gaps 参照）。

## #1934・#1935・#1860 との責務境界

- [#1934](https://github.com/squne121/loop-protocol/issues/1934)（AI エージェントによる
  ゲート改変対策）とは独立。本 trial の `prek.toml` / `.git/hooks` はローカル開発者の
  git 操作を補助するものであり、エージェント実行環境のゲート改変対策（hook の tamper 検知、
  permission gate 等）を代替しない。`prek install` を実行していない・`prek` が見つからない
  環境でも、AI エージェント実行環境における既存のゲート（`.claude/hooks/*`、CI required
  checks 等）は本 trial と無関係にそのまま機能する。
- [#1935](https://github.com/squne121/loop-protocol/issues/1935)（シナリオテストの
  required/nightly 分離）とは独立。本 trial は E2E / VRT / シナリオテストの実行方針や
  required/nightly 分類には一切関与しない。pre-commit hook は ESLint のみを実行し、
  Playwright / Vitest 等のテストスイートは呼び出さない。
- [#1860](https://github.com/squne121/loop-protocol/issues/1860)（minimal-harness 方針）とは
  整合させる形で、本 trial は新しい ledger・control-plane・benchmark harness を新設せず、
  既存 ESLint 設定（`eslint.config.mjs`）をそのまま呼び出す最小構成に留めている。

## test-runner・CI・open-pr validator との責務分離

**pre-commit hook（本 trial）の成功は、CI / test-runner / open-pr validator による検証の
代替にはならない。**

| レイヤー | 実行タイミング | 対象 | bypass 可否 |
|---|---|---|---|
| 本 trial（prek pre-commit） | `git commit` 時（ローカル） | staged TS/JS ファイルの ESLint のみ | `--no-verify` で bypass 可、`prek` 未導入環境では fail-open |
| `test-runner` SubAgent | 実装 SubAgent の Verification Commands 実行時 | `pnpm typecheck` / `pnpm lint` / `pnpm test` / `pnpm build` 全件 | bypass 不可（Issue 実装フローの gate） |
| CI（GitHub Actions） | push / PR 作成・更新時 | 上記 4 コマンド相当 + 追加ジョブ（`check-japanese.yml` 等） | bypass 不可（branch protection / required checks） |
| `open-pr` の PR body validator | `gh pr create` / `gh pr edit` 直前 | PR 本文構造・日本語比率 | bypass 不可（fail-closed） |

本 trial はあくまで開発者体験（早期フィードバック）の改善が目的であり、
`impl-review-loop` / `pr-review-judge` / CI が担う正本の検証責務を置き換えるものではない。
pre-commit hook がスキップされた・bypass された・`prek` 未導入だったことを理由に
CI や pr-review-judge の検証を省略してはならない。

## 動作確認（isolated fixture）

実際の pre-commit 挙動（lint エラーによる block・`--no-verify` bypass・`prek` バイナリ不在時の
挙動）は、メインリポジトリの `.git/hooks` を一切変更せず、
[`scripts/dev-hooks/verify_precommit_trial.sh`](../../scripts/dev-hooks/verify_precommit_trial.sh)
が `mktemp -d` で作成する isolated temporary git repository 上で確認する。

```bash
./scripts/dev-hooks/verify_precommit_trial.sh
```

## Runtime Verification Applicability

本 Issue（#2552）の `## Runtime Verification Applicability` は `not_applicable`
（ゲーム/プロダクトのランタイム挙動を変更しない開発ツール設定・ドキュメント追加のため）。
動作確認は上記 isolated fixture 上の VC スクリプトで完結する。
