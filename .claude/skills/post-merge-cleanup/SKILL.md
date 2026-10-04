---
name: post-merge-cleanup
description: PR マージ後のローカル cleanup と Git 整理を行うときに使う。未コミット確認 / main 整合 / worktree / branch 削除 / parent issue クローズ条件確認 / follow-up 起票候補列挙を `post-merge-cleanup-worker` SubAgent に委譲する。「クリーンアップ」「post merge」「マージ後の整理」のトリガー。
---

# Post Merge Cleanup / マージ後クリーンアップ

PR マージ後のローカル環境 cleanup と Git 整理を `post-merge-cleanup-worker` SubAgent に委譲して実行する。

Codex CLI では、このステップ専用の custom agent `post-merge-cleanup-worker` を起動する。root thread は直接ファイル編集・テスト実行・commit・push・review judgment を行わない。

## 3 本の経路（通常経路 / 復旧経路 / local-only 経路）

この orchestrator は、Task Context の記録状況に応じて次の 3 本の経路を分離して扱う。

| 経路 | 入口（adapter の `--phase`） | Task Context への書き込み | worker dispatch 条件 |
|---|---|---|---|
| 通常経路 | `merged` → `completed` | merge fact 受理・cleanup Activity の select / resume・final-success receipt がある場合だけ完了記録 | cleanup begin が `selected` |
| 復旧経路 | `recover`（明示要求のみ） | 欠けた claim と必要な Activity 遷移・復旧記録を 1 つの `BEGIN IMMEDIATE` で原子的に記録 | 復旧後に `merged` を 1 回だけ再実行し、その結果が `selected` |
| local-only 経路 | `local-only` | 一切書かない（Task Context を呼ばない） | `local-only` が `LOCAL_ONLY_PERMITTED` |

**dispatch 条件は 2 本立てである**: (a) 通常経路 = cleanup begin が `selected`、(b) local-only 経路 = `--phase local-only` が `LOCAL_ONLY_PERMITTED`。このいずれでもなければ `post-merge-cleanup-worker` を dispatch しない。復旧経路は dispatch 条件を持たず、復旧後の `--phase merged` が `selected` になった場合に限り通常経路 (a) として dispatch する。

**凍結 wire と今回変更する orchestration policy の区別**: Issue #2565 / #2790 の公開 signal wire（`signal_kind` 集合、closed key、`disposition` / `reason_code` の既存値）と、既存 `--phase merged` / `--phase completed` の出力意味、`cleanup_completed` の既存セマンティクスは凍結されており変更しない。変更するのは orchestrator の方針（Task Context に記録できない場合にローカル cleanup を継続できる）、adapter への additive な `--phase recover` / `--phase local-only`、復旧用の repository-local service 操作だけである。

## 通常経路: Task Context の信頼済み merge commit point（Issue #2565）

cleanup work を select、resume、または dispatch する前に、orchestrator は fresh merged-PR GraphQL snapshot を取得する。snapshot は
`closingIssuesReferences(first: 2) { nodes { number repository { nameWithOwner } } }` と `pullRequest.body`（`Refs`-bound PR の `non_closing_authority` の `pr_body_sha256` を束縛する本文。下記「`Refs` を使う PR を closing relation に依存せず対象 Issue へ束縛する規則」）を含め、candidate Issue は
repository identity と Issue number の組で照合する（同番号でも別 repository は non-mutating mismatch）。
`.claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py --phase merged` を呼び出す。merge signal が `applied` または同一 Task の
`duplicate_noop` の場合だけ durable cleanup selection を試行できる。続く cleanup-begin outcome が `selected`
（新規または非terminal instance の再選択）の場合だけ cleanup Activity/work を dispatch し、terminal instance の
`duplicate_noop/activity_terminal` を含む他の outcome では、通常経路としては dispatch せずこの invocation を停止する（下記の決定表が、停止ではなく復旧経路または local-only 経路を選べる結果を限定列挙する）。最終 cleanup work が成功した後に限り、同じ adapter を
`--phase completed` で呼び出す。completed invocation は worker の完全な `POST_MERGE_CLEANUP_REPORT_V1` を保存した
`--cleanup-receipt-file` を必須とし、closed-key validation 済みかつ `status: ok`、`human_review_required: false`、
`unresolved_cleanup_items: []`、`errors: []` の final-success receipt だけが signal を emit できる。receipt/partial/failed/human-review/no-proof
outcome は `cleanup_completed` を emit せず dispatch も再開しない。adapter outcome は diagnostic であり、完了済み producer operation を rollback しない。

### fresh snapshot 規則（`--phase recover` / `--phase local-only` 共通）

- 「fresh」とは、その invocation（`--phase recover` または `--phase local-only`）の直前に orchestrator が取得した snapshot を指す。adapter は GitHub I/O を行わないため、検証可能な定義として **snapshot file の mtime が adapter 起動時刻から 300 秒以内**を要求し、超過した場合は `deferred` / `SNAPSHOT_STALE`（書き込み 0）になる。
- **前回 invocation の snapshot の再利用は禁止**する。`--phase merged` で使った snapshot を `--phase recover` や `--phase local-only` へ流用せず、毎回直前に取得し直す。
- repository identity の束縛範囲は、snapshot 自身の `repository.nameWithOwner` と `closingIssuesReferences` node の `repository.nameWithOwner` との一致（snapshot identity と closing relation）だけである。adapter は GitHub I/O を行わず、ローカル checkout の remote は検証しないため、snapshot を正しい repository から取得する責務は orchestrator が負う。
- `--merge-identity <40 hex>` は必須で、snapshot の merge OID と完全一致し、かつ `^[0-9a-f]{40}$` に一致しなければ `rejected_evidence` / `MERGE_IDENTITY_MISMATCH` になる。PR 本文の `Closes #N` 文字列そのものは authority にしない。closing relation のある PR は `closingIssuesReferences` だけで binding し、closing relation の無い PR（`Refs`-bound）は下記の `non_closing_authority`（`--phase merged` / `--phase completed` に限る。`recover` / `local-only` は受理しない）だけで binding する。

### 決定表（`--phase merged` の結果 → 復旧 / local-only）

「明示復旧要求」とは、呼び出し元（人間、または明示要求を受けた Agent）が対象 PR・Issue・effective origin を指定して復旧を要求した場合を指す。復旧を暗黙に自動実行しない。表の「local-only」は、該当する outcome を `--task-context-outcome` に埋めて `--phase local-only` を呼ぶことを指す（許可集合は後述の「local-only 許可集合」と一致する）。

| `--phase merged` の結果 | 既定（復旧要求なし） | 明示復旧要求あり |
|---|---|---|
| `selected`（`CLEANUP_STARTED` / `CLEANUP_ALREADY_SELECTED`） | 通常経路: worker dispatch | 復旧不要。復旧を呼ばず通常経路 |
| `duplicate_noop/activity_terminal`、`late_noop/CLEANUP_ALREADY_BEGUN` | 停止（dispatch なし、local-only なし） | 復旧は呼ばない。停止 |
| `deferred/IMPLEMENTATION_NOT_READY` | local-only（outcome `deferred/IMPLEMENTATION_NOT_READY`）。復旧が可能であることを報告 | `--phase recover` を 1 回実行。`applied` / `duplicate_noop` → 同じ origin で `--phase merged` を 1 回だけ再実行して本表へ戻る。reject → local-only（outcome `recovery_rejected`、拒否理由と owner を報告） |
| `conflict/FACT_TASK_IDENTITY_CONFLICT`、`conflict/OUT_OF_ORDER_SIGNAL` | local-only（outcome は同名の `conflict/...`） | `--phase recover` を 1 回実行（claim 分裂なら reject）。reject → local-only（outcome `recovery_rejected`） |
| `deferred/unbound` | 既存 `signal diagnose-origin` で原因を取得し、下の diagnose-origin 結果表に従う | recover は origin 解決を要するため reject（`diagnose-origin` の reason code を返す）。以降は下の diagnose-origin 結果表に従う |
| snapshot 不正（`RELATION_UNAVAILABLE` / `MERGED_SNAPSHOT_INVALID` / `RELATION_ISSUE_MISMATCH` / `MERGE_OID_INVALID`）、`ADAPTER_UNAVAILABLE` など Task Context が応答しない結果 | 停止（local-only なし、削除なし）。Task Context 無応答は 1 回だけ bounded retry して停止 | 停止（recover も呼ばない） |

`recovery_rejected` は `--phase recover` が `conflict/*` を返した場合だけを指す。recover が `deferred/*`（`SNAPSHOT_STALE` / `unbound` など）、`rejected_evidence/*`、または `ADAPTER_UNAVAILABLE` を返した場合は `recovery_rejected` ではなく**停止**し、削除に進まない。復旧後の `--phase merged` 再実行結果が `selected` でない場合は、その結果が local-only 許可集合に含まれるときだけ**その結果を outcome として** local-only に進み、含まれなければ停止する。

### `Refs` を使う PR を closing relation に依存せず対象 Issue へ束縛する規則（Issue #2878）

post-merge で live evidence を待つ Issue（#2842 型）の PR は `Closes` ではなく `Refs #N` を使うため `closingIssuesReferences.nodes` が空になる。
adapter は GitHub I/O を行わず PR 本文の grammar も再実装しないので、orchestrator が fresh snapshot を取得し、`docs/dev/workflow.md` の entrypoint
（`uv run --locked python3 .claude/skills/open-pr/scripts/validate_pr_body.py --evaluate-reference-policy ...`、facts は gh で fresh 取得）を **snapshot の `pullRequest.body` と同じ本文 bytes** に対して実行し、
その出力のうち **7 key**（`decision` / `level` / `reason_code` / `repo` / `issue_number` / `pr_number` / `pr_body_sha256`。`effective_kind` / `body_verdict` / `body_reason` は含めない）を、
snapshot JSON object の **top-level key `non_closing_authority`**（`pullRequest` の兄弟であり内側ではない）として添える。`body_verdict` が `valid` の場合だけ添える。

- adapter が binding を認める条件（`--phase merged` / `--phase completed` のみ、全て満たす場合）: `closingIssuesReferences.nodes` が空、`non_closing_authority.decision == nonclosing_required` かつ `level` が A1 または A2、`repo`（大文字小文字を区別せず）/ `issue_number` / `pr_number` が snapshot と一致、`pr_body_sha256` が snapshot の `pullRequest.body`（欠落・非 string は拒否）の UTF-8 bytes の SHA-256 と一致。
- 上記以外は既存の reason code（`RELATION_ISSUE_MISMATCH` 等）で拒否し、書き込み 0。closing node が 1 件以上ある場合は従来規則のまま（別 Issue の node があれば拒否し、non-closing 判定へ fall through しない）。`--phase recover` / `--phase local-only` は non-closing binding を受理せず従来どおり停止する。
- `non_closing_authority` は orchestrator-attested であり、snapshot 自体と同じ trust 境界にある（adapter は snapshot の真正性を独立検証しない）。producer（`open_pr.py::classify_closing_issue_relation(..., non_closing_authority=...)`）は同じ 7 key の dict を引数で受け取り、snapshot → adapter の入力と同一の写像である。
- adapter は GitHub に対して close を一切行わず、close 権限も返さない。Issue の close は下記の close gate に従う operator / orchestrator の明示操作である。
- **worker への受け渡し（Issue #2891）**: worktree / branch 削除を実行する `scripts/agent-ops/cleanup_exec.py::_verify_linked_issue` は、`closingIssuesReferences` が空の PR に限り、既存の closing relation fast path・research fallback のどちらでも認可されなかった場合に、orchestrator が保存した `non_closing_authority`（上記と同一の 7 key）を評価する（検証内容は adapter / `open_pr.non_closing_authority_binds` と同一契約。`level: CLOSED` / A3 や hash・repo・番号の不一致は従来どおり `LINKED_ISSUE_MISMATCH`）。orchestrator は evaluator の `body_verdict` が `valid` の場合に限り、この 7 key を JSON object として一時ファイルに保存し（既存の `--snapshot-file` / `--cleanup-receipt-file` と同じ一時 path 規約に従う。未検証の evaluator 結果は保存せず、`decision == nonclosing_required` だけを根拠にしない）、そのファイルの **path のみ** を worker の Delegation message に `non_closing_authority_file` として渡す。worker は executor Skill の `cleanup_exec.py` コマンドの任意引数 `--non-closing-authority-file` にその path を渡す。orchestrator が `cleanup_exec` の argv 全体を組み立てて worker に渡してはならない。`body_verdict` が `valid` でない場合、または authority が無い場合はファイルを保存せず、`Refs`-bound PR の削除系 cleanup は従来どおり `LINKED_ISSUE_MISMATCH`（削除なし）で止まる。`linked_issue_number` を省略して認可を迂回する運用は採らない。
- **残る制限（#2910）**: 本変更が解消するのは `--phase merged` / `--phase completed` を経た通常経路の `cleanup_exec`（通常 lane・branch-only lane・同一 invocation 内の branch-only 再認可）だけである。`--phase recover` / `--phase local-only` の Task Context adapter は non-closing binding を引き続き受理しない（別 Outcome・follow-up #2910 が所有）ため、recover / local-only 経路で `Refs`-bound PR を束縛できない制限は残る。全 recovery path が解禁されたとは扱わない。discard レーン（`verify_discard_authorization`）は authority を使わず挙動も変えない。evaluator 実行後に Issue が CLOSED になった場合に cleanup 実行時に live Issue state を再 fetch して拒否する責務は追加しない（運用順序は cleanup 完了 → post-merge 証跡確認 → Issue close）。

### merge と Issue close の close gate（`docs/dev/workflow.md` と同一の正本表）

decision table と grammar の正本は `docs/dev/workflow.md` の「PR reference と Issue close の分離」であり、次の表はその写しである（2 文書の表は一致させる）。

#### PR reference decision table（判定表の正本）

| 順 | 条件 | decision | level | PR 本文の reference | merge 時の Issue |
|---|---|---|---|---|---|
| 0 | linked Issue が CLOSED | `nonclosing_required` | CLOSED | `Refs #N`（closing keyword は block） | 既に CLOSED（authority 評価なし） |
| 1 | A1 present かつ valid | `nonclosing_required` | A1 | `Refs #N`（Runtime Verification Applicability の状態に依存しない） | 本文は close しない（本文以外の自動 close 経路が無いと native auto-close risk check で確認できた場合に OPEN を維持） |
| 2 | A1 present かつ invalid（2 行以上を含む） | `fail_closed` | なし | 停止（A2 / A3 へ降格しない） | 停止 |
| 3 | A1 なし、A2 成立 | `nonclosing_required` | A2 | `Refs #N` | 本文は close しない（本文以外の自動 close 経路が無いと native auto-close risk check で確認できた場合に OPEN を維持） |
| 4 | A1 なし、A2 不成立、A3 成立 | `closing_required` | A3 | `Closes #N` | merge で auto-close |
| 5 | 上記以外（Issue state 取得不能、Runtime Verification Applicability の欠落・重複・解釈不能、facts 不正） | `fail_closed` | なし | 停止 | 停止 |

- merge は Issue の close を意味しない。`Refs` 本文だけでは OPEN 維持を保証せず、本文以外の自動 close 経路（native closing relation / 採用される merge message）が無いと native auto-close risk check で確認できた場合に OPEN が維持される。live evidence が未取得の間は Refs-bound Issue を OPEN に保ち、live evidence の取得・証跡へのリンク・残 AC の充足を確認した後にだけ operator / orchestrator が明示的に close する。
- merge 時の guard は final head の PR 本文に対する reviewer の fresh な evaluator 実行であり、orchestrator は merge 直前に entrypoint を再実行して `pr_body_sha256` を attested 値と照合する（不一致なら merge せず re-review）。`nonclosing_required` では加えて native auto-close risk check を final message / final native relation に対して再実行する（または検証済み message をそのまま使う）。PR 本文 / Issue 本文の hash だけでは本文以外の自動 close 経路を保証できない（`docs/dev/workflow.md` の「native auto-close risk check」）。

### `unbound` の原因別フォールバック/エスカレーション（Issue #2790 AC8、PR #2795 review fix_delta P2-C 改訂）

`task_context_workflow_signal.py` の呼び出し結果が `{"disposition": "deferred", "reason_code": "unbound"}` を返した場合、この公開 wire contract 自体は Issue #2565 の frozen 契約であり変更しない（Issue #2790 Out of Scope）。ただし `unbound` は Issue #2719 の内部 7 reason-code（`origin_session_missing` / `origin_run_not_found` / `origin_run_ended` / `origin_run_kind_mismatch` / `origin_task_unattached` / `origin_binding_session_mismatch` / `origin_ambiguous`）をすべて一つに畳んだ opaque な値であり、原因を区別しないまま毎回無条件停止すると、恒常的に binding できないセッションで毎回手動 override が必要になる。

**責務の所在（P2-C）**: 通常経路では、`signal apply` が `applied`/`duplicate_noop` 以外（`unbound` を含む）を返した場合、cleanup selection（`cleanup begin`）自体を試行せず、`post-merge-cleanup-worker` SubAgent は **dispatch されない**（同 SubAgent は別の `post-merge-cleanup-executor` procedure のみを読み、本 orchestrator 向け routing instruction を読み込まない設計のため、dispatch 前に止まった worker へ復旧責務を割り当てても実行可能な経路にならない）。この「dispatch されない」規約は Task Context の cleanup Activity 経路（通常経路）に限定した規約であり、Task Context に記録できない場合の local-only 経路は別の dispatch 条件（上記 (b)）で扱う。したがって、以下の診断・原因別対応・bounded retry・復旧要求・local-only の選択は **worker dispatch より前の orchestrator（本 SKILL を呼び出している root/main thread）自身の責務**であり、worker には委譲しない。

診断には `scripts/task-context/task_contextctl.py signal diagnose-origin`（`task_context_workflow_signals.diagnose_origin`、Issue #2790 AC3/AC7）を read-only に呼び出す。この診断呼び出しは `events` journal に何も書き込まず、DB/state-root を作成・migration もせず（`connect_readonly` 経由、PR #2795 review fix_delta P2-B）、`signal apply` 自身が返す公開 disposition/reason_code には一切影響しない。

**同一 effective origin の一貫性（P2-A）**: 診断は、直前に失敗した `signal apply`/`cleanup begin` 呼び出しと **同じ effective origin session** を対象にする。`signal diagnose-origin` は option を一切持たず、診断対象はそのプロセスの環境変数 `CLAUDE_CODE_SESSION_ID` だけで決まる（`--origin-session-id` は diagnose-origin の parser に存在せず、渡すと `unrecognized arguments` の parser error になる。diagnose-origin に `--origin-session-id` を渡す記述・呼び出しをしてはならない）。`task_context_workflow_signal.py --origin-session-id` に明示 origin を渡していた場合は、診断コマンドの環境変数にだけ同じ値を明示設定する次の canonical invocation を使う（`$ORIGIN_SESSION_ID` は直前の apply に渡した値と同一とする）:

```bash
CLAUDE_CODE_SESSION_ID="$ORIGIN_SESSION_ID" uv run --locked python3 scripts/task-context/task_contextctl.py signal diagnose-origin </dev/null
```

この環境変数の明示設定を省くと、親プロセス（親 shell / Agent）の ambient `CLAUDE_CODE_SESSION_ID` が診断対象になり、直前の apply とは無関係な別 session の原因を報告してしまう危険がある。直前の apply が明示 origin を使わず ambient session を使っていた場合に限り、環境変数の明示設定なしで呼んでよい。新しい option・wrapper・API は追加しない。

canonical flow（概念的な順序。`/task` はユーザーの直接入力 `UserPromptExpansion` を使う設計であり、Claude が単に Skill tool を呼ぶ経路とは異なるため、orchestrator が worker/Skill 呼び出しだけで同じ bootstrap が発火すると仮定しない）:

```text
merge signal apply が unbound
→ orchestrator が同一 effective origin を diagnose-origin で診断
→ 下表の reason-code に対応する既存 recovery、または actionable stop
→ 本当にユーザーによる明示的 /task <target> が必要な場合は、その必要性と対象を具体的に報告する
  （それ以外の read-only diagnosis / 既存 recovery / 再評価は不要な承認待ちを追加せず進める）
→ 復旧を確認できた場合だけ、同じ effective origin で signal を bounded に再適用
→ cleanup selection が selected の場合は通常経路として post-merge-cleanup-worker を dispatch
→ 復旧できず Task Context に記録できない原因（下の診断結果表で local-only と定めたもの）の場合だけ、診断後に cleanup を止めず local-only 経路へ進む
```

診断結果の `reason_code` に応じて、orchestrator は次の原因別経路を選択する（Task を推測して勝手に apply することは一切しない）。この表は診断・報告の指針として維持し、local-only は「診断後に cleanup を止めずに続ける」新しい選択肢として合成する:

| `diagnose_origin` の `reason_code` | 意味 | 対応 |
|---|---|---|
| `origin_session_missing` | 呼び出し元 session id 自体が無い | 既存 recovery（session id 環境変数の確認）へ。signal retry は無意味なので行わない。local-only（outcome `unbound/origin_session_missing`）に進める |
| `origin_run_not_found` | この session id に紐づく ExecutionRun が一件もない | 本当に unbound（binding 未作成）である可能性が高い。明示的な binding recovery（例: `/task <target>` による bootstrap、Issue #2790 AC2/AC5）が可能であることを報告し、signal を無条件 retry せず local-only（outcome `unbound/origin_run_not_found`）に進める |
| `origin_run_ended` | ExecutionRun は存在するが既に終了済み | 原因を人間/呼び出し元に表示し、既存の recovery 経路（新しい SessionStart による self-heal）へ委ねる。signal を無条件 retry せず local-only（outcome `unbound/origin_run_ended`）に進める |
| `origin_run_kind_mismatch` | ExecutionRun は存在するが managed run_kind（`native_operator`/`claude_gpt`）ではない | 原因を表示し、既存 recovery へ。この run から signal を適用しない。local-only（outcome `unbound/origin_run_kind_mismatch`）に進める |
| `origin_task_unattached` | ExecutionRun に Task が紐づいていない | 原因を表示し、既存 recovery へ。Task を推測して attach しない。local-only（outcome `unbound/origin_task_unattached`）に進める |
| `origin_binding_session_mismatch` | Binding の `current_claude_session_id` が一致しない | 原因を表示し、既存 recovery へ。別セッションの Binding を steal しない。local-only（outcome `unbound/origin_binding_session_mismatch`）に進める |
| `origin_ambiguous` | 複数の candidate が同時に条件を満たす（現在の DB 制約上は到達不能な defense-in-depth 分岐） | Task を推測せず即時停止し、人間判断を求める（`human_review_required: true`）。local-only に入らない |

`resolved: true`（diagnose_origin が実際に同一 effective origin を解決できた場合）は、直前の `signal apply` 呼び出しが別の理由（evidence 不整合等）で `unbound` 以外の outcome を返したケース、または診断の間に状態が復旧したケースであり、いずれも上表の原因別対応の対象外 -- 状態が既に復旧している可能性があるため、無条件の再評価・限定的な signal retry へ進めてよい（診断からもう一度状態が変わっていないか再確認したうえで進める）。

### diagnose-origin 結果表（`deferred/unbound` 時の結果空間の全列挙）

| diagnose-origin の結果 | 扱い |
|---|---|
| `resolved: true` | local-only に入らない。状態が復旧済みの可能性があるため `--phase merged` を 1 回だけ再評価（既存どおり） |
| `ADAPTER_UNAVAILABLE` / error envelope | local-only に入らない。1 回だけ bounded retry して停止（Task Context 状態不明のため削除に進まない） |
| `origin_ambiguous` | 停止して人間判断（`human_review_required: true`）。local-only に入らない |
| `origin_session_missing` | 既存の報告・recovery 案内を表示し、signal を無条件 retry せず、local-only（outcome `unbound/origin_session_missing`） |
| `origin_run_ended` | 既存の報告・recovery 案内を表示し、signal を無条件 retry せず、local-only（outcome `unbound/origin_run_ended`） |
| `origin_run_kind_mismatch` | 既存の報告・recovery 案内を表示し、signal を無条件 retry せず、local-only（outcome `unbound/origin_run_kind_mismatch`） |
| `origin_task_unattached` | 既存の報告・recovery 案内を表示し、signal を無条件 retry せず、local-only（outcome `unbound/origin_task_unattached`） |
| `origin_binding_session_mismatch` | 既存の報告・recovery 案内を表示し、signal を無条件 retry せず、local-only（outcome `unbound/origin_binding_session_mismatch`） |
| `origin_run_not_found` | 上記に加え、binding 未作成の可能性が高く、明示 `/task <target>` による bootstrap が可能であることを報告し、local-only（outcome `unbound/origin_run_not_found`） |

## 復旧経路: `--phase recover`（明示要求のみ）

Task Context に紐づかないままマージされた PR について、Issue claim / PR claim の欠落を **整合する場合だけ**原子的に復旧する。暗黙には実行せず、通常 hook・prompt 自動 rebind・暗黙 attach からは呼ばない。人間のキー入力は要求しないが、対象 PR・Issue・effective origin・復旧意図が明示された要求を受けた Agent の実行に限る。Task を推測して attach しない。

```bash
uv run --locked python3 .claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py \
  --snapshot-file "$SNAPSHOT" --issue-number "$ISSUE_NUMBER" --pr-number "$PR_NUMBER" \
  --phase recover --merge-identity "$MERGE_OID" --explicit-recovery --origin-session-id "$ORIGIN_SESSION_ID"
```

- `--explicit-recovery` が無ければ `rejected_evidence` / `EXPLICIT_RECOVERY_REQUIRED`、必須引数（`--merge-identity` / `--origin-session-id`）の欠落は `rejected_evidence` / `MISSING_REQUIRED_ARGUMENT`（いずれも書き込み 0）。`--origin-session-id` は必須で、ambient env へ fallback しない。
- 結果は `applied` / `RECOVERED`、同一 Task の再実行 `duplicate_noop` / `SAME_TASK_SAME_FACT`、claim 分裂などの `conflict`（owner Task id と保持 claim 番号を含む。recovery dedupe key または merge fact が別 Task 所有の場合は、その所有 Task を `conflicting_task_id` で返す）、`deferred`、`rejected_evidence` のいずれかになる。`conflict` で reject された場合は何も変更されない。
- 復旧は存在する claim の所有 Task を先に確認する。既存の `IMPLEMENTATION_NOT_READY` を「不足 claim を安全に追加できる」という判定に使わない。
- **Issue と PR の両方で `/task` を実行することを回避策として案内しない**。`/task <issue>` と `/task pr <N>` の二重実行は claim 分裂を起こすため、復旧手順として使わない。
- 復旧後は同じ effective origin で `--phase merged` を **1 回だけ**再実行して決定表へ戻る。復旧記録（`recovery:implementation_claims`）は `cleanup_completed` ではなく、親 Issue close や PR close の根拠にもならない。

## local-only 経路: `--phase local-only`

Task Context を復旧できない / しない場合に、Task Context の lifecycle を開始・完了したと偽らず、安全判定済みのローカル cleanup だけを継続する経路。adapter は Task Context への ctl 呼び出し・DB 書き込み・events 追加を一切行わない。

```bash
uv run --locked python3 .claude/skills/post-merge-cleanup/scripts/task_context_workflow_signal.py \
  --snapshot-file "$SNAPSHOT" --issue-number "$ISSUE_NUMBER" --pr-number "$PR_NUMBER" \
  --phase local-only --merge-identity "$MERGE_OID" --task-context-outcome "$OUTCOME" \
  --worktree-path "$WORKTREE_PATH" --branch-name "$BRANCH_NAME"
```

### local-only 許可集合（closed enum、10 値）

`--task-context-outcome` は次の 10 値だけが許可される（「Task Context に記録できない」場合のみ）。adapter の closed enum と一致させる。

- `deferred/IMPLEMENTATION_NOT_READY` — Issue / PR claim または implementation Activity が未整備
- `conflict/FACT_TASK_IDENTITY_CONFLICT` — claim が別 Task に分裂している
- `conflict/OUT_OF_ORDER_SIGNAL` — merge fact の前提が順序不整合
- `recovery_rejected` — `--phase recover` が `conflict/*` で拒否された
- `unbound/origin_session_missing` — origin の session id が無い
- `unbound/origin_run_not_found` — origin の ExecutionRun が見つからない
- `unbound/origin_run_ended` — origin の ExecutionRun が終了済み
- `unbound/origin_run_kind_mismatch` — origin の run_kind が managed ではない
- `unbound/origin_task_unattached` — origin に Task が紐づいていない
- `unbound/origin_binding_session_mismatch` — Binding の session が一致しない

許可集合外（`refused` になる例）: `selected`、`duplicate_noop/activity_terminal`、`late_noop/CLEANUP_ALREADY_BEGUN`、`unbound/origin_ambiguous`、`unbound/resolved`、`adapter_unavailable`、未知の値。許可集合外では `{"disposition": "refused", "reason_code": "LOCAL_ONLY_NOT_PERMITTED"}` だけが出力され、fields も authority も出ない。

**`--task-context-outcome` は caller 申告値**である。orchestrator が直前の `--phase merged` / `diagnose-origin` / `--phase recover` の実結果から埋める値であり、adapter はそれを検証できない。この gate は fail-closed な local guardrail であって security boundary ではない。snapshot 不正・stale・merge identity 不一致では local-only の結果を出さず、削除に進まない。

### local-only の結果と dispatch 後の規則

- 成功時の結果は `disposition: "local_only"` / `reason_code: "LOCAL_ONLY_PERMITTED"` / `task_context: "unrecorded"` / `authority`（`cleanup_completed` / `parent_issue_close` / `superseded_pr_close` がすべて `false`）/ `repo` / `issue_number` / `pr_number` / `merge_commit_oid` / `worktree_path` / `branch_name` だけで構成される。`cleanup_exec_argv` は出さない（worker が自身の executor 手順で `cleanup_exec.py` を呼ぶため、orchestrator が argv を組み立てない）。
- worker の Delegation message には、この結果の fields（`repo` / `issue_number` / `pr_number` / `merge_commit_oid` / `worktree_path` / `branch_name`）を**そのまま（verbatim）**埋め込む（Materialization rule と同じ）。
- local-only 経路では dispatch 後に **`--phase completed` を呼ばない**。Task Context の cleanup lifecycle を開始・完了したことにしない。
- local-only 経路では step 3 のうち、`parent_issue_status` による `gh issue close`、`superseded_prs` による `gh pr close` / `gh pr comment` を**実行せず、候補として報告するだけ**にする。follow-up 起票は Task Context と無関係な dedupe_key 起票なので従来どおり実行してよい。
- 報告は下の「報告区分」表に従い、worker が返した実行結果で決める。`LOCAL_ONLY_PERMITTED` は dispatch を許可する routing decision（実行許可）であり、cleanup が成功した根拠ではない。いずれの区分でも `cleanup_completed`、親 Issue close、別 PR close の根拠にはならない。
- 削除は `scripts/agent-ops/cleanup_exec.py` の既存認可境界（merge 状態、exact な worktree / branch、未コミット変更）だけを通る。dirty worktree・対象不明・merge 状態を確認できない場合は削除しない。bare な Git 削除や `rm -rf`、別の cleanup 機構は使わない。
- 復旧が reject された場合の local-only 報告には、reject 結果の owner Task id・保持 claim 番号を含める。local-only 後は Task / implementation Activity が ACTIVE のまま残る既知の残余状態であり、この経路はそれらを cleanup しない（人間が owner Task を解決する）。
- local-only 実行後に復旧が明示要求された場合は `--phase recover` を実行してよいが、同一 invocation で worker の再 dispatch や `--phase completed` は行わない。以後の通常経路での cleanup は別 invocation の明示呼び出しで行う。

### local-only の報告区分（実行許可と実行結果の分離）

`LOCAL_ONLY_PERMITTED` は「Task Context に記録できない状況でも worker の dispatch を許可する」という実行許可（routing decision）だけを表す。cleanup が実際に完了したかどうかは、worker が返す `POST_MERGE_CLEANUP_REPORT_V1` の実行結果だけで決める。`LOCAL_ONLY_PERMITTED` 単独を成功の根拠にしてはならない。どの区分でも末尾に `Task Context 未記録` を付け、Task Context lifecycle の完了を偽装しない。

| worker の実行結果 | 報告区分 |
|---|---|
| `status: ok`、かつ `unresolved_cleanup_items` が空・`errors` が空・cleanup 完了を確認できた | ローカル cleanup 成功 / Task Context 未記録 |
| `status: partial`、または `status: ok` でも `unresolved_cleanup_items` に残件がある | ローカル cleanup 部分成功 / 残件と理由 / Task Context 未記録 |
| `status: failed`、worker が refused（拒否）、report 欠落・不正、または `errors` 非空などで完了を確認できない | ローカル cleanup 未完了 / 理由 / Task Context 未記録 |

## Delegation / 委譲

main thread は以下の static call shape で SubAgent に委譲する:

```yaml
spawn_agent:
  task_name: post_merge_cleanup_pr{merged_pr_number}_i{attempt}
  agent_type: post-merge-cleanup-worker
  fork_turns: none
  message: |
    Objective: classify and perform the bounded post-merge cleanup contract for the actual merged PR.
    Live reference: bind the actual merged PR number and linked Issue number.
    Bounded scope: bind the canonical cleanup scripts, actual worktree, actual branch, and follow-up candidates.
    Expected result: POST_MERGE_CLEANUP_REPORT_V1 with cleanup and human-review facts.
```

### Materialization rule（実値を具体化する規則）

`task_name` は実行直前に実際の merged PR number と非負 attempt で `post_merge_cleanup_pr{merged_pr_number}_i{attempt}` から materialize する。たとえば固定の PR 番号を用いず、同一 root session 内で既に保存済みの canonical task name を再利用してはならない。`fork_turns: none` のため、root は message に実際の merged PR number、linked Issue number、worktree path、branch name、canonical cleanup scripts、follow-up candidates を値として埋め込む。`merged PR number` の自然言語参照、変数名、波括弧・山括弧の placeholder を child message に渡してはならない。この static template 自体を tool call として送信してはならない。

local-only 経路では、`--phase local-only` 結果の `repo` / `issue_number` / `pr_number` / `merge_commit_oid` / `worktree_path` / `branch_name` を実値のまま（verbatim）message に埋め込む。orchestrator が `cleanup_exec` の argv を組み立てて渡してはならない。

`Refs`-bound PR（`body_verdict == valid` の `non_closing_authority` を保存した場合）では、その path だけを `non_closing_authority_file` として message に埋め込む。ファイルの内容（7 key）を message に複製せず、`cleanup_exec` の argv も組み立てない。

完了の扱いは4 site 共通の [Common Completion Protocol](../impl-review-loop/steps/step-4-pr-review.md#common-completion-protocol) に従う。

1. `post-merge-cleanup-worker` SubAgent を Agent tool で起動する（dispatch 条件は前述の 2 本立て: 通常経路で cleanup begin が `selected`、または local-only 経路で `--phase local-only` が `LOCAL_ONLY_PERMITTED`）。

2. SubAgent は `POST_MERGE_CLEANUP_REPORT_V1` YAML を返却する

3. main thread が返却された YAML に応じて以下を実行（local-only 経路では `parent_issue_status` による `gh issue close` と `superseded_prs` による `gh pr close` / `gh pr comment` を実行せず候補として報告するだけにし、follow-up 起票は継続する。詳細は「local-only 経路」節）:
   - `human_review_required: true` → 不明事項を人間に判断委ね
   - `follow_up_issue_requests` あり → main thread が **即時** `issue-creator` SubAgent に委譲して `create-issue` 経由で自動起票する（dedupe_key ベースで重複チェック。SubAgent 内では起票しない。候補列挙のみ）
   - `superseded_prs` あり → `gh pr close` / `gh pr comment` を実行
   - `parent_issue_status.recommended_action == "close"` かつ `parent_issue_status.all_children_closed == true` かつ `parent_issue_status.parent_issue_number` が正の整数（1 以上）のときに限り `gh issue close` を実行する。`recommended_action` は必須フィールドであるため単純な非 null 判定（「あり」）で close してはならない。`recommended_action` が `keep_open` または `n/a` の場合、`all_children_closed` が `false` の場合、または `parent_issue_number` が正の整数でない場合は `gh issue close` を実行しない
   - `stash_restored: false` → `stash_entry_ref` を確認、人間判断

### follow_up_issue_requests の自動起票フロー

`follow_up_issue_requests` が空でない場合、main thread は SubAgent から YAML を受け取った直後に以下を実行する:

```
for each request in follow_up_issue_requests:
  1. dedupe チェック: dedupe_key で既存 Issue を検索（open / closed すべて対象）
     gh issue list --repo squne121/loop-protocol --state all \
       --search '"<dedupe_key>"' --json number,title,url,state,stateReason,labels
  2. 重複なし → issue-creator SubAgent に委譲して create-issue skill 経由で起票
     ※ Issue 本文に ## Source セクション（dedupe_key を含む）を必須で付与
  3. 重複あり（open）→ スキップ（既存 Issue 番号をレポートに記録、status: reused_open）
  4. 重複あり（closed / not_planned）→ 起票せずスキップ（status: skipped_closed_not_planned）
  5. 重複あり（closed / completed）→ 起票せずスキップ（status: skipped_closed_completed）
  6. 重複あり（closed / duplicate）→ 起票せずスキップ（status: skipped_closed_duplicate）
  ※ closed Issue を open に差し戻して再利用する場合は human escalation が必要（自動起票不可）
```

起票・スキップした follow-up Issue の情報を終了コメントの `follow_up_issues` フィールドに列挙する（`FOLLOW_UP_MATERIALIZATION_RESULT_V1` 形式。詳細スキーマは `docs/dev/agent-skill-boundaries.md` 参照）。

終了コメントのテンプレート（`FOLLOW_UP_MATERIALIZATION_RESULT_V1` を含む）:

````markdown
## post-merge-cleanup: 完了 (<timestamp>)

- status: ok | partial | failed
- 次アクション: <親 Issue クローズ / 人間判断 等>

```yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  schema_version: 1
  materialized_by: post-merge-cleanup
  follow_up_issues:
    - request_dedupe_key: "..."
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed
      issue:
        number: 123
        url: "https://github.com/..."
      reason: null

  note_only_observations:
    - dedupe_key: "..."
      source_url: "..."
      source_note_id: "..."
      summary: "..."
```
````

## 責務分界

| 責務 | 担当 |
|---|---|
| git / gh 出力分類・cleanup 実行 | SubAgent（fail-close） |
| CONFLICT 検出時の即時停止 | SubAgent |
| follow-up Issue 起票 | main thread（`create-issue` 経由。dedupe ヒット時はスキップ）|
| parent issue クローズ実行 | main thread |
| superseded PR close / comment 実行 | main thread |
| 人間判断が必要な事象の最終判断 | 人間 |

## Executor 手順の参照（instruction boundary — Issue #1733）

worker が実行する mechanical executor procedure（8 ステップの deterministic cleanup commands、
git/gh 結果分類、worktree/branch binding 検証、cleanup failure taxonomy、
`POST_MERGE_CLEANUP_REPORT_V1` の生成）は本 orchestrator Skill の本文に保持しない。

worker（`post-merge-cleanup-worker`）は `.claude/agents/post-merge-cleanup-worker.md`
の `skills: [post-merge-cleanup-executor]` frontmatter 経由で
`post-merge-cleanup-executor` Skill（canonical body: `.claude/skills/post-merge-cleanup-executor/SKILL.md`、
repo-local discovery surface: `.agents/skills/post-merge-cleanup-executor/SKILL.md`。Issue #2161 の
native Codex CLI retirement 以前は Codex CLI が `.codex/agents/post-merge-cleanup-worker.toml` の
`repo_local_skill_surface` 経由で本 symlink 越しに同じ canonical body を参照していたが、当該 agent 定義は
撤去済みである）を参照し、
本 orchestrator の main-thread 向け routing instruction（worker 起動、follow-up 起票実行、
parent close 実行、superseded PR close 実行）を読み込まない。

orchestrator（本 Skill）が知っておくべき worker 出力の要点（`POST_MERGE_CLEANUP_REPORT_V1` の
routing に使うフィールドのみ）は上記「main thread が返却された YAML に応じて以下を実行」節に
列挙済みである。フィールドの完全な型定義・生成手順・validator は
`post-merge-cleanup-executor` Skill 側の Output セクションおよび
`scripts/check_post_merge_cleanup_boundary.py` を正本とする。

executor（`post-merge-cleanup-executor` Skill）は branch / worktree の状態確認に
`scripts/agent-ops/git_ref_probe.py` と `scripts/agent-ops/git_worktree_probe.py` を使う
（raw `git for-each-ref` / raw `git worktree list --porcelain` を直接呼ばない）。
probe script の呼び出し手順そのものは executor 側の canonical body を参照し、本 orchestrator には
複製しない。

## Local-only unpublished commit discard lane（未公開 commit の破棄。Issue #1523）

dedicated worktree に merged PR の head SHA を超える local-only commit が残っている場合（`cleanup_exec.py` が `pr_head_oid_mismatch` を返す状態のうち、PR head が local branch tip の祖先である候補）、worker / SubAgent は bare `git worktree remove --force` や bare `git branch -D` を実行しない。

handoff は **executor（`materialize_cleanup_contract.py` / `cleanup_exec.py`）が生成した explicit human confirmation command だけ**とする。具体的には:

1. worker は `materialize_cleanup_contract.py --operation local_only_discard ...`（引数なし＝発行のみ）を実行し、対象 PR・worktree realpath・branch・PR head SHA・local tip SHA・nonce・expiry に束縛された one-shot confirmation contract を発行する。この発行自体は破壊的操作ではなく、**agent 自身の承認とはみなさない**（one-shot contract の発行または検証だけでは削除は実行されない）。
2. 実際に破棄を実行する `materialize_cleanup_contract.py --operation local_only_discard --consume` の実行は、人間が明示的に確認・実行する。
3. `--consume` は claim-first の one-shot consume であり、confirmation 不在・期限切れ・replay・target SHA 不一致では拒否され、破壊的操作は一切実行されない。

worker / SubAgent が独自に `git worktree remove --force` や `git branch -D` を直接組み立てて実行することは禁止する。

## Guardrails / ガードレール（orchestrator 側）

- follow-up 起票は main thread（本 orchestrator）でのみ実行する。worker / executor 側は候補列挙のみで `gh issue create` を直接呼び出さない
- parent issue close / superseded PR close の実行は main thread（本 orchestrator）でのみ行う
- local-only 経路では `--phase completed` を呼ばず、`gh issue close` / `gh pr close` / `gh pr comment` を実行しない（候補報告のみ）
- local-only 経路では `LOCAL_ONLY_PERMITTED` を cleanup 成功の根拠にせず、worker の実行結果（`status` / `unresolved_cleanup_items` / `errors`）で報告区分を決める
- worker（`post-merge-cleanup-worker`）を再起動する指示、または nested delegation（`Agent` tool 経由・Bash 経由の外部 agent CLI 起動）を worker に要求しない

## Related / 関連

- `.claude/agents/post-merge-cleanup-worker.md` — 本 skill が起動する SubAgent
- `.claude/skills/post-merge-cleanup-executor/SKILL.md` — worker が実行する mechanical executor procedure（canonical body）
- `.agents/skills/post-merge-cleanup-executor/SKILL.md` — Codex CLI 向け thin wrapper
- `.claude/skills/create-issue/SKILL.md` — follow-up 起票委譲先
- `scripts/check_post_merge_cleanup_boundary.py` — orchestrator/executor 責務境界と `POST_MERGE_CLEANUP_REPORT_V1` の validator
- `docs/dev/repository-folder-policy.md` — folder class / cleanup authority の正本
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`POST_MERGE_CLEANUP_REPORT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
