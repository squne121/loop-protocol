# post-merge-cleanup 経路選択の runtime 確認（Issue #2817 AC11）

あなたは read-only の確認セッションです。ファイルの編集、`git` の変更系コマンド、`gh` の実行、`scripts/agent-ops/cleanup_exec.py` と Task Context adapter（`task_context_workflow_signal.py`）の実行、`post-merge-cleanup-worker` の起動は一切行いません。次の 3 手順を宣言順に実行してください。

## 手順 1: SKILL の読み取り

Read ツールで `.claude/skills/post-merge-cleanup/SKILL.md` を読んでください。

## 手順 2: 読み取り専用の SubAgent を 1 件だけ起動

Agent ツールで `Explore` の SubAgent をちょうど 1 件だけ起動してください。ほかの SubAgent（特に `post-merge-cleanup-worker`）は起動しません。`Explore` には次の依頼を渡し、SKILL 本文だけを根拠に答えさせてください（ファイル編集もコマンド実行も依頼しません）。

`.claude/skills/post-merge-cleanup/SKILL.md` の本文だけから、次の 3 つの合成 merge 結果について、orchestrator が選ぶ経路を答えてください。復旧要求は明示的に出されていないものとします。

- A: `--phase merged` の結果が `selected`（`CLEANUP_STARTED`）。
- B: `--phase merged` の結果が `deferred/IMPLEMENTATION_NOT_READY`。復旧要求はなし。
- C: `--phase merged` の結果が `deferred/unbound` で、`diagnose-origin` の原因が `origin_ambiguous`。

経路は `normal_dispatch`（通常経路で worker を dispatch する）、`local_only`（`--phase local-only` へ進む）、`stop_human_review`（停止して人間判断を求める）のいずれかで答えさせてください。

## 手順 3: 最終応答

`Explore` の回答を踏まえ、最終応答に次の 3 行を、この順序（A、B、C）で 1 行ずつ出力してください。各行は先頭から行末まで、次の文字列をそのまま出力します。`Explore` の回答が下記の期待と食い違う場合は、その食い違いを別の行で報告し、3 行の内容は SKILL 本文が定める経路に基づいて決めてください。

```text
POST_MERGE_ROUTE_A=normal_dispatch
POST_MERGE_ROUTE_B=local_only
POST_MERGE_ROUTE_C=stop_human_review
```

この確認は read-only です。3 行以外に、実在する worktree や branch を削除するコマンドを出力してはいけません。
