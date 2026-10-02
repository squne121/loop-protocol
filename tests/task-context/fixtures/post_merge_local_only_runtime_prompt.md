# post-merge-cleanup 経路選択の runtime 確認（Issue #2817 AC11）

あなたは read-only の確認セッションです。ファイルの編集、`git` の変更系コマンド、`gh` の実行、`scripts/agent-ops/cleanup_exec.py` と Task Context adapter（`task_context_workflow_signal.py`）の実行、`post-merge-cleanup-worker` の起動は一切行いません。次の 3 手順を宣言順に実行してください。

## 手順 1: SKILL の読み取り

Read ツールで `.claude/skills/post-merge-cleanup/SKILL.md` を読んでください。

## 手順 2: 読み取り専用の SubAgent を 1 件だけ起動

Agent ツールで `Explore` の SubAgent をちょうど 1 件だけ起動してください。ほかの SubAgent（特に `post-merge-cleanup-worker`）は起動しません。`Explore` には、次の「Explore への依頼」の節の本文を、そのまま依頼文として渡してください（ファイル編集もコマンド実行も依頼しません）。

`Explore` は必ず foreground（同期）で起動してください。Agent ツールの `run_in_background` は指定せず、バックグラウンドや非同期のタスクとして起動してはいけません。`Explore` の結果が返ってくるまで待ち、結果を受け取る前に次の手順へ進んではいけません。手順 3 で引用する 3 行は、`Explore` の最終回答が返ってきた後にだけ、その最終回答の末尾 3 行をそのまま使います。

この依頼文には期待する経路の答えを一切含めません。経路は `Explore` 自身が SKILL 本文だけから導きます。

### Explore への依頼

`.claude/skills/post-merge-cleanup/SKILL.md` を読み、その本文だけを根拠に、次の 3 つの合成 merge 結果について、orchestrator が選ぶ経路を決めてください。復旧要求は明示的に出されていないものとします。

- A: `--phase merged` の結果が `selected`（`CLEANUP_STARTED`）。
- B: `--phase merged` の結果が `deferred/IMPLEMENTATION_NOT_READY`。復旧要求はなし。
- C: `--phase merged` の結果が `deferred/unbound` で、`diagnose-origin` の原因が `origin_ambiguous`。

経路は `normal_dispatch`（通常経路で worker を dispatch する）、`local_only`（`--phase local-only` へ進む）、`stop_human_review`（停止して人間判断を求める）のいずれか 1 つで答えてください。

回答の最後は、必ず次の形式（下のコードブロックは形式の例示であり、実際の回答にコードフェンスは付けません）の 3 行ちょうどで終えてください。`<route>` の位置には、あなた自身が SKILL 本文から導いた経路名（`normal_dispatch`、`local_only`、`stop_human_review` のいずれか）を 1 つ入れます。A、B、C の順に 1 行ずつ、先頭から行末までこの形式にし、コードフェンス・箇条書き記号・引用符・行頭や行末の余計な文字は付けません。この 3 行より後ろには何も書かないでください。経路を導いた理由を書く場合は、この 3 行より前に簡潔に書いてください。

```text
POST_MERGE_ROUTE_A=<route>
POST_MERGE_ROUTE_B=<route>
POST_MERGE_ROUTE_C=<route>
```

## 手順 3: 最終応答

`Explore` の回答を踏まえ、最終応答の末尾に、`Explore` の回答の最後にある 3 行と同じ内容の 3 行を、この順序（A、B、C）で 1 行ずつ、先頭から行末まで次の形式でそのまま出力してください。`<route>` の位置には、各ケースの経路名を入れます。

```text
POST_MERGE_ROUTE_A=<route>
POST_MERGE_ROUTE_B=<route>
POST_MERGE_ROUTE_C=<route>
```

`Explore` の回答が SKILL 本文の定める経路と食い違う場合は、その食い違いを 3 行より前の別の行で報告し、3 行の内容は SKILL 本文が定める経路に基づいて決めてください。

この確認は read-only です。3 行以外に、実在する worktree や branch を削除するコマンドを出力してはいけません。
