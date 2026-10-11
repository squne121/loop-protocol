# Claude Code Auto memory 保存判断ポリシー

本ドキュメントは、Claude Code / Claude-GPT の Auto memory に何を保存し、何を保存しないかを判断するための運用基準である。
Issue / PR の可変な進捗スナップショットを memory に重複して蓄積せず、将来再利用できる知見だけを残すことを目的とする。

- 本ドキュメントは **運用上の指針** であり、hook・schema・CI gate・wrapper のような強制機構ではない。Auto memory への書き込みを fail-closed でブロックするものでも、approval layer でもない。
- Auto memory は Claude が自分用に書くメモであり、Claude Code は内容を文脈として読み込むだけで、設定として強制しない（公式仕様）。
- `docs/dev/agent-skill-boundaries.md` が定義する SubAgent frontmatter `memory` の opt-in 仕様は再実装しない。本ドキュメントは参照するだけである。

## 公式仕様の前提（2026-10-11 確認）

次の記述は公式ドキュメントの内容であり、本ポリシーはこれと矛盾しない。

- 出典となる公式ドキュメントは次の 2 ページである: <https://code.claude.com/docs/en/memory>、<https://code.claude.com/docs/en/sub-agents>
- Auto memory は `type` frontmatter で 4 種類のメモを区別する。
  - `user`: 利用者の役割・専門性・作業上の好み
  - `feedback`: 利用者が与えた訂正と、確認された進め方
  - `project`: コードや Git 履歴から導出できない、進行中の作業・期限・意思決定
  - `reference`: Issue tracker やダッシュボードなど、プロジェクト外の情報の所在
- 保存先は `~/.claude/projects/<project>/memory/` で、`MEMORY.md`（索引。1 メモ 1 行）と topic file（1 メモ 1 ファイル）で構成される。
- 同一 git リポジトリの全 worktree とサブディレクトリは 1 つの Auto memory ディレクトリを共有する。Auto memory は machine-local であり、他のマシンや cloud 環境とは共有されない。
- セッション開始時に読み込まれるのは `MEMORY.md` の先頭 200 行または 25KB のうち、先に到達した方までである。この読み込み上限は `MEMORY.md` にのみ適用される。topic file は起動時に読み込まれず、Claude が必要になったときに標準のファイルツールで読む。
- `CLAUDE.md` / `CLAUDE.local.md` は、作業ディレクトリとその上位ディレクトリ分が起動時に読み込まれる。`paths` frontmatter のない `.claude/rules/` の rule は起動時に読み込まれ、`paths` 付きの rule は一致するファイルを扱うときに読み込まれる。サブディレクトリの `CLAUDE.md` は、そのサブディレクトリのファイルを読み書きしたときに読み込まれる。
- `AGENTS.md` は、既定の Project instructions 設定（`claude-md-or-agents-md`）では、作業ディレクトリまたは上位に `CLAUDE.md` / `.claude/CLAUDE.md` / `CLAUDE.local.md` が存在しない場合にだけ読み込まれる。これらが存在すると `CLAUDE.md` 系だけが読み込まれる。`CLAUDE.md` が `@AGENTS.md` を import していれば、import 経由で読み込まれる。`AGENTS.md` と `CLAUDE.md` の両方を読む `claude-md-and-agents-md` は、利用者が明示的に選ぶ別設定である。
- Claude は「コードから導出できるもの」「CLAUDE.md が既に述べているもの」を保存しない。毎セッション保存するわけではなく、将来の会話で有用かどうかで判断する。
- メインの会話の Auto memory は通常の subagent には読み込まれない（fork を除く）。subagent 自身の memory は frontmatter の `memory:` で有効化する別ディレクトリである。

公式文書は進捗スナップショットの揮発性を直接禁止していない。本ポリシーが進捗スナップショットの重複保存を抑制するのは、公式の「導出できるものは保存しない」という原則からの **運用上の推論** であり、公式の要求ではない。

## 保存先の決定表

情報の性質ごとに、置き場所の正本を次のとおり決める。Auto memory は他の正本の代替 authority にならない。

### 保存先と読み込み経路の区別

正本への保存と、各セッションでの読み込みは別である。知見を instructions / SSOT / Skill / test へ昇格したという理由だけで、後続セッションへ確実に届くとはみなさない。

- 現行 repo のルート `CLAUDE.md` には `@AGENTS.md` の import がない。この import の整備は既存 Issue #1737 の責務であり、本ポリシーでは扱わない。
- そのため `AGENTS.md` への記載だけで、将来の全 Claude Code セッションに自動で読み込まれるとは保証しない。既定設定のまま `CLAUDE.md` と `AGENTS.md` が併存する場合、`AGENTS.md` は読み込まれない。
- `CLAUDE.md`、個人用 `~/.claude/CLAUDE.md`、`.claude/rules/` の読み込み条件は上の「公式仕様の前提」に従う。条件が不確かなときは断定せず、`/context` 等で読み込み条件を確認する。
- 利用者が `claude-md-and-agents-md` を選んでいる環境では `AGENTS.md` も読み込まれる。ただし本ポリシーはその設定を前提条件にしない。

| 情報の性質 | 置き場所 | 備考 |
|---|---|---|
| 共通の行動指示（出力形式・固定の開発方針） | repo 共通 `AGENTS.md`、Claude 専用 `CLAUDE.md`、個人横断 `~/.claude/CLAUDE.md`、適切な `.claude/rules/` | instructions / rules の層。ここは保存先の正本であり、セッションへの読み込みは別に確認する（下記「保存先と読み込み経路の区別」） |
| 複数ステップの手順 | 該当する `.claude/skills/**/SKILL.md` | skills の層 |
| role / tool の境界 | `.claude/agents/*.md` | agents の層 |
| Issue / PR / CI / HEAD / approval / AC 判定 / terminal history | live GitHub と、repository が管理する artifact / run-state | memory はその代替 authority にならない。毎回 fresh read する |
| 再発する defect | 修正 Issue と、SSOT / Skill / test への反映 | Auto memory への永続 workaround だけで解決済みとしない |
| 将来の会話で有用な `user` / `feedback` / `project` / `reference` の学び | Auto memory | `project` の短期情報は「GitHub 等から復元できず、将来有用」な場合に限り、出典・時点・再検証条件を併記する |

## 保存判断の 4 観点

保存・更新の前に次の 4 観点を確認する。

1. 将来の有用性（future utility）: 別セッションの判断を実際に助けるか。
2. 導出可能性（derivability）: コード、Git 履歴、GitHub、CI、artifact から復元できないか。復元できるなら正本を読めばよい。
3. 揮発性（volatility）: 数時間から数日で変わる状態（Draft / merged、CI の PASS / FAIL、HEAD、approval）ではないか。
4. authority: その情報の正本はどこか。正本が別にある場合、memory は参照先の所在を示すにとどめる。

時点と再検証条件を付ければ何でも保存してよい、という読み方はしない。上記の観点を通過したうえで、次の操作結果を選ぶ。

## 保存時の 4 つの操作結果

| 操作 | 適用条件 |
|---|---|
| 新規保存しない | GitHub / CI / artifact から復元できる可変の進捗スナップショット。必要になった時点で fresh read する |
| 既存へ統合 | 同一の有用な知見が既存 memory に既にある。新しい索引行や topic file を増やさず、既存を更新・統合する |
| 新規保存 | GitHub 等から再取得できず、将来有用な固有の知見だけ |
| 退役 / 参照化 | 正本（instructions / SSOT / Skill / test）へ反映済み、または失効した知見。ただし反映先の読み込み・参照経路が確立していることを確認できた場合に限り、整理・退役し、必要なら正本への参照だけを残すことを検討する。経路が未確立なら、有用な知見への参照を保持する |

- 正本への保存と各セッションでの読み込みは別である。退役 / 参照化の前に、保存先に応じた読み込み・参照経路（「保存先と読み込み経路の区別」を参照）を確認する。経路が未確立なら元の memory を退役せず、有用な知見への参照を保持する。「保存されたが読まれないまま元 memory を退役した」状態を作らない。
- `project` memory の短期情報を一律には禁止しない。上の 4 観点を通過したものは保存できる。
- 「新規保存しない」は新規に書かないという意味であり、既存 memory の削除を自動で要求するものではない。既存項目の扱いは後述の棚卸し手順で、人間が判断する。

## 判別 fixture

次の fixture は **2026-10-11 時点の観測に基づく例示** であり、現在状態の断定ではない。各事例の現行状態の真偽は、memory から断定せず fresh read（GitHub を読み直すこと）で確認する。各 fixture には期待される操作を併記する。

| 事例 | 観測した内容 | 期待操作 |
|---|---|---|
| #3015 / #3026 の stale Draft | memory が「PR #3026 は Draft・人間 merge 待ち」と記録。観測時点で #3026 は merged だった | 新規保存しない。Draft や merged の状態は fresh read する。既存項目があれば退役 / 参照化を検討する |
| #3006 / #3025 の AC6 / AC7 重複進捗 | 同じ #3006 / PR #3025 を扱う topic file が複数の索引行から参照され、異なる時点の停止理由が並存していた。PR 本文に残る過去の検証 HEAD は current HEAD の証拠ではなく、fresh read で得た値でも current-head evidence とは扱わない | 新しい topic file・索引行を作らない。既存項目へ統合するか、GitHub に正本があるものは退役 / 参照化する |
| #2872 の blocker | pre-merge では検証できない AC を、人間判断待ちの blocker として記録していた | GitHub（Issue / PR コメント）から復元できる blocker は新規保存しない |
| `set +e` の guard workaround | secret guard が `set +e` を含むコマンドをブロックするため、終了コードは `cmd; PE=$?` で取る、という回避策を memory に残していた | 再発する defect の修正候補として SSOT / Skill / test へ昇格する。昇格後は、昇格先の読み込み・参照経路を確認できた場合に限り memory の退役 / 参照化を検討する。経路が未確立なら参照を保持する（memory だけで恒久修正済みとしない） |
| `CLAUDE.md` と `AGENTS.md` の併存 | repo に `CLAUDE.md` と `AGENTS.md` があり、`CLAUDE.md` に `@AGENTS.md` の import がなく、instruction loading は既定設定。再利用できるユーザー訂正を `AGENTS.md` へ昇格しようとしている | `AGENTS.md` への保存だけを「後続セッションで必ず利用可能」と判定しない。元の memory を退役せず参照を保持する。または、確実に読み込まれる経路（`CLAUDE.md` や `paths` なしの `.claude/rules/` 等）へ反映する |

### positive control（新規保存してよい例）

次は新規保存してよい例（positive control）である。これらが保存対象から漏れないことも、本ポリシーの要件である。

- 利用者による、再利用できる訂正（例: 「標準出力は最小限にし、URL のみを返す」のような `feedback`）。GitHub にも正本にも書かれていない。
- GitHub 等に未記録の判断理由（例: ある設計を採らなかった背景）。コードや履歴から復元できない `project` の意思決定。
- 内容そのもののコピーではなく、参照先の所在を示す情報（例: 「ダッシュボードはここにある」という `reference`）。

## `set +e` のような知見の分類（再発修正候補）

同じ回避策を何度も memory から参照して凌いでいる場合、それは memory の問題ではなく、修正されていない defect である。

1. 当面の回避策として memory に残すのは許容する。ただし出典・時点・再検証条件を併記する。
2. 再発している defect は修正 Issue を起票し、SSOT / Skill / test のいずれかへ反映する。
3. 反映後は、反映先の読み込み・参照経路を確認できた場合に限り、その memory を退役 / 参照化の対象として棚卸しで扱う。経路が未確立なら参照を保持する。
4. memory に回避策があることを理由に、defect を解決済みと扱わない。

## operator 向けの確認方法

Claude Code の標準コマンドで、現在の状態を読み取り専用で確認できる。

- `/memory`: CLAUDE.md、`CLAUDE.local.md`、その他の memory ファイルの場所を一覧し、Auto memory の on / off 切り替えと、Auto memory フォルダを開く操作を提供する。保存先の確認に使う。
- `/context`: 起動時に読み込まれた CLAUDE.md と rules を確認する。本ポリシーを参照する always-on rule が読み込まれているかの確認にも使える。

これらは advisory な確認手段であり、新たな gate ではない。Auto memory への保存判断はモデルに依存するため、保存結果の再現性を強制するものではない。

## 可逆的な手動棚卸し手順

棚卸しは operator が手動で行う。`MEMORY.md` の索引を整理するだけで完了としない。次の順序で進める。

1. **保存先の確認**: `/memory` で現在の Auto memory の保存先を確認する。`autoMemoryDirectory` 等で既定の場所と異なる場合がある。
2. **backup**: `MEMORY.md` と対象 topic file の本文の両方を backup する。backup は memory ディレクトリの **外**（例: 作業用の一時ディレクトリ）に置く。memory ディレクトリ内に置くと、自身が memory として読み込まれ得るため。
3. **内容の確認**: 索引と topic 本文の両方を読む。索引の 1 行だけで判断しない。
4. **分類**: GitHub / CI / artifact から復元できる進捗と、固有の知見を分ける。
5. **保持 / 統合 / 退役の判断**: 各項目を、前述の 4 つの操作結果に当てはめる。GitHub 等に該当情報が見つからない項目は **未共有** 情報として扱い、保留・保全する（削除しない）。
6. **整合の確認**: 索引と topic file の対応が取れていること（索引にあって実体がない、実体があって索引にない、が無いこと）、および `MEMORY.md` が 200 行 / 25KB を超えていないことを確認する。

- GitHub への履歴登録が必要な情報が見つかった場合は、既存の workflow に戻す。本手順で publisher 等の仕組みを新設しない。
- 次の操作は行わない: 一括削除、自動 cleanup、リポジトリ側からの local memory の直接改変。
- 各ステップは backup から復元できる範囲で行う。判断に迷う項目は削除せず保留する。

## Native Claude Code と Claude-GPT の責務

- Auto memory は Claude Code harness の機能である。Claude-GPT（`scripts/claude-gpt/launch.sh` が起動する Claude Code）でも同じ substrate を使う。GPT backend 固有の第二のメモリシステムとして扱わない。
- `scripts/claude-gpt/launch.sh` は `HOME` / `XDG` / `CLAUDE_CONFIG_DIR` を隔離しない薄い launcher であり、本ポリシーはこれを変更しない。
- ただし、利用者環境によっては Native と Claude-GPT で memory の実体の場所が異なる場合がある。また、モデルが何を書きやすいかも異なり得る。両者で同一の内容・同一の保存挙動は保証しない。
- 責務の分担: 何を保存するかの原則（本ポリシー）は共通。保存挙動と memory 実体の場所は、それぞれの harness / backend / 利用者環境に依存する。

## main の Auto memory と subagent 自身の memory の違い

- main の Auto memory は、通常の subagent には読み込まれない。
- subagent 自身の memory は frontmatter の `memory:`（`user` / `project` / `local`）による opt-in であり、main とは別のディレクトリである。両者は独立しており、混同しない。
- `autoMemoryEnabled` を off にすると、`memory:` の指定も効果を持たない。
- 根拠なく既存の SubAgent 定義へ `memory: project` を一括付与しない。`memory:` の自動一律付与は行わない。付与が必要な場合は、その SubAgent ごとに専用 Issue で判断する。

## 関連

- `docs/dev/agent-skill-boundaries.md`: SubAgent frontmatter `memory` の opt-in と `autoMemoryEnabled` の区別
- `docs/dev/claude-gpt-runtime-prerequisites.md`: Claude-GPT の実行前提
- `docs/dev/ssot-registry.md`: 本ドキュメントの登録先
