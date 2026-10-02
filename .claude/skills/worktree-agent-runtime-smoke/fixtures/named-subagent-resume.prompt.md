# Named SubAgent resume 固定試験 prompt

これは `worktree-agent-runtime-smoke` の `--named-subagent-resume` scenario が使う、公開用の固定試験入力です。次の手順を、記載した順序のまま、実際の tool 呼び出しで実行してください。ファイル編集、Bash 実行、外部通信は行わないでください。

1. `Agent` tool を 1 回だけ呼び出します。引数は `name` に `named-resume-worker`、`subagent_type` に `general-purpose`、`run_in_background` に `false` を指定します。子 SubAgent への依頼は「`NAMED_RESUME_FIRST_DONE` という文字列だけを返答してください」とします。
2. 子 SubAgent の初回 completion を受け取るまで待ちます。
3. `SendMessage` tool を呼び出します。`to` には手順 1 で指定した name（`named-resume-worker`）をそのまま指定し、依頼は「`NAMED_RESUME_SECOND_DONE` という文字列だけを返答してください」とします。このとき新しい `Agent` 呼び出しは行いません。`SendMessage` が deferred tool として一覧に出ていない場合は、ToolSearch で `select:SendMessage` を読み込んでから呼び出します。
4. resume された同じ SubAgent の completion 結果が親 session に届くまで待ちます。結果が届く前に最終回答を出してはいけません。
5. 最終回答には、resume 後の SubAgent が返した文字列をそのまま含めます。
