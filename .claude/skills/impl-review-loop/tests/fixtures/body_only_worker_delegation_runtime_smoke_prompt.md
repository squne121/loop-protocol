Issue #2971 runtime smoke (implementation-worker の body-only hygiene pre-mutation guard が stale な expected_live_body_sha256 を overwrite せず拒否することの delegation smoke の prompt 雛形).
<!-- このタイトル行は Issue #2971 の subagent-delegation runtime smoke を説明する日本語注記である。実 implementation-worker SubAgent へ update_pr_body_hygiene の request を委譲し、故意に不一致の expected_live_body_sha256 を guard が拒否することを確認する。 -->

This file is a static template. The operator lane (the pytest wrapper) renders every upper-case placeholder wrapped in double at-signs at run time with the live values of the current branch's pull request, writes the rendered prompt to a temporary file, and passes that file to the runtime smoke runner.
The body file referenced below has exactly the same content as the live pull request body, and `expected_live_body_sha256` is intentionally different from the live body hash, so even a missing guard would be an idempotent rewrite.
Do not run `gh pr edit`, do not edit any repository-tracked file, and do not run any command yourself in the parent session.
<!-- この段落は、本 file が静的な雛形であり、operator lane が実行時に現 branch の PR の live 値で二重 at-sign で囲んだ placeholder を具体化することを説明する日本語注記である。body file は live body と同一内容で、expected_live_body_sha256 は故意に不一致なので、仮に guard が無くても書き込みは冪等になる。親 session 自身は command を実行しない。 -->

Use the `Agent` tool exactly once, with `subagent_type: "implementation-worker"`, to launch the `implementation-worker` SubAgent in the foreground (do not run it in the background), with exactly this message. Wait for the SubAgent to finish and return its final response before doing anything else:
<!-- Agent ツールを 1 回だけ foreground で subagent_type が implementation-worker の SubAgent として起動し、子の完了と最終応答を待つ旨を示す日本語注記であり、次のコードブロックは SubAgent へ送信する指示文そのものなので内容は変更しないこと。 -->

```text
Objective: handle the IMPLEMENTATION_WORKER_REQUEST_V2 below in the update_pr_body_hygiene mode of your agent definition, and return your final IMPLEMENTATION_WORKER_RESULT_V2.
Your current working directory is the implementation worktree of pull request @@PR_NUMBER@@.
IMPLEMENTATION_WORKER_REQUEST_V2:
  mode: update_pr_body_hygiene
  required_auto_action:
    kind: update_pr_body_hygiene
  pr_number: @@PR_NUMBER@@
  issue_number: @@ISSUE_NUMBER@@
  expected_head_sha: @@EXPECTED_HEAD_SHA@@
  body_file_path: @@BODY_FILE_PATH@@
  body_file_sha256: @@BODY_FILE_SHA256@@
  expected_live_body_sha256: @@EXPECTED_LIVE_BODY_SHA256@@
Because this request carries the body file fields, follow the pre-mutation guard procedure of your agent definition exactly: run the guard immediately before any mutation and proceed to the update_pr.py wrapper only if the guard allows it. Never call gh pr edit directly and never modify the body file.
Do not run any command other than the ones required by that procedure, and do not modify any file.
```
<!-- このコードブロックは子 implementation-worker SubAgent へ送る英語の指示文である。body file field を伴う update_pr_body_hygiene の request を渡し、agent 定義の pre-mutation guard 手順どおりに mutation 直前の guard を実行させ、guard が許可した場合だけ update_pr.py wrapper へ進み、gh pr edit の直接呼出しと body file の改変をしないことを指示する。 -->

After the SubAgent has completed and returned its final response, write your own final message as the SubAgent's full final response text copied verbatim, and nothing else.
Do not summarize, paraphrase, reformat, or omit any line of the result, and do not add any text of your own.
<!-- この段落は、SubAgent の最終応答を要約・言い換え・整形・省略せず、そのまま最終 message として転記し、自分の文章を追加しない要件を説明する日本語注記である。 -->
