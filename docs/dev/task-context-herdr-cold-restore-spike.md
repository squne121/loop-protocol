---
issue: "#2571"
parent_issue: "#2562"
dependent_issue: "#2569"
status: completed
feasibility_verdict: partially_feasible_with_constraints
executed_at: "2026-09-08"
---

# Herdr Cold Restart Capability Spike (#2571)

## 目的

Herdr cold restart経路でのproject-scoped dispatcher/PATH-shim interceptionが実現可能かを、
disposable named Herdr sessionに対する実際のcold restart canaryのcausal runtime proofで判定する。
production code実装（#2569）は本spikeのスコープに含まない。

## Baseline

| 項目 | 値 |
|---|---|
| main HEAD（実行時） | `2dd034a9` |
| Herdr version | `herdr 0.8.2` |
| Herdr integration status | `claude: current (v8)`（Claude Code integration インストール済み、`herdr integration status` 出力） |
| Claude Code version | `2.1.263 (Claude Code)` |
| real native claude 絶対path（pre-shim解決） | `/home/squne/.local/bin/claude` |
| herdr binary絶対path | `/home/squne/.local/bin/herdr` |

## 前回試行との差分（環境ブロッカーの解消）

前回試行（[コメント](https://github.com/squne121/loop-protocol/issues/2571#issuecomment-5584459859)）は、Herdr TUI clientをPython `subprocess.Popen` の子として非ttyで起動した結果、`ratatui` の terminal 初期化panic、および `setsid script` 経由でも `ghostty error -2` で local runtime blocker に到達し `not_determined_environment_blocked` として停止した。

今回は **outer pane 方式**（Herdr upstream の throwaway reproduction パターン相当）を採用し、このblockerを回避した：

1. 人間のdefault Herdr session内に、このrun専用のouter workspace/paneを1つ作成する（`herdr workspace create --label lp2571-canary-outer --no-focus`）。
2. そのouter paneには実PTYが割り当てられているため、`herdr pane run <outer-pane-id> "herdr --session <disposable-name>"` で disposable named Herdr TUIをそのPTY上で起動しても `ratatui`/`ghostty` の初期化に失敗しない。
3. 親からのsession targetingは `HERDR_SESSION` 環境変数ではなく、global `--session <name>` flagを使用する。**`HERDR_SESSION` 環境変数単独では named session へのルーティングが確実でないことを実機で確認した**（後述）。
4. 全てのmutating操作の前に readiness gate（`herdr session list --json` でexact session name存在確認、socket file実在確認 `test -S`、read-only API callでのdisposable session固有の応答確認）を通す。

## 安全上の発見（前回インシデントの根本原因特定）

前回、`HERDR_SESSION=<存在しないsession名>` を指定した `workspace create` が human default sessionへ暗黙fallbackする事象が発生した。今回、**実際に存在し running 状態の disposable session に対しても** `HERDR_SESSION` 環境変数だけでは正しくルーティングされないことを確認した：

```
$ HERDR_SESSION=lp2571-repro-4945a854 herdr workspace list
# → human default sessionのworkspace一覧（w4, wS）が返る（誤り）

$ herdr --session lp2571-repro-4945a854 workspace list
# → disposable sessionの正しいworkspace一覧（w1）が返る（正しい）
```

**結論**: named session targetingには `HERDR_SESSION` 環境変数ではなく、必ずglobal `--session <name>` flagを使うこと。`scripts/agent-ops/run_worktree_agent_runtime_smoke.py` の `create_isolated_session()` 系ヘルパーが `isolated_env["HERDR_SESSION"] = session_name` のみに依存している箇所は、本spikeで確認したのと同じ潜在的リスク（意図しないdefault session操作）を持つ可能性があるため、#2569 実装時にはこの経路を `--session` flagベースへ見直すことを推奨する（本spikeのスコープでは `run_worktree_agent_runtime_smoke.py` 自体は変更しない）。

## Cold Restart Canary 実行結果（causal process-boundary evidence）

### disposable session

- 命名: `lp2571-repro-4945a854`（run固有nonce付き、人間の`default` sessionとは名前空間上完全分離）
- collision確認: `herdr session list --json` で事前に存在しないことを確認してから作成
- lifecycle: create → (canary実行) → cold restart(stop→再起動) → (canary継続) → stop → delete
- **human default session は無傷**: canary実行前後で `herdr workspace list`（default session）は一貫して人間の実ワークスペース `w4`（pane_count=7）のみであることを確認済み。outer workspace（`wS`、このrun専用）は実行後に `herdr workspace close` で除去済み。

### Pre-restart baseline

- disposable session内に `herdr --session <name> workspace create --env PATH=<shim>:<decoy>:<system-path>` でworkspace作成（pane `w2:p1`）
- **重要な発見**: `--env PATH=...` によるherdr側のprocess env注入だけでは、対話bashシェルの起動時に `~/.local/bin` 等がPATH先頭へ再prependされるため、`command -v claude` は意図したforwarderではなく real native claude (`/home/squne/.local/bin/claude`) に解決された。receipt file・decoy markerともに未生成（forwarder/decoyどちらも呼ばれず）。これはcold restart以前の時点で既に発生する、ambient PATH-onlyアプローチの限界を示すcausal evidenceである。
- **project-scoped bootstrap相当の追加PATH再主張**（shell初期化後に明示的に `export PATH="<shim>:<decoy>:$PATH"` を実行、global `.bashrc`/`.profile`等は一切変更していない）を行った結果:
  - `command -v claude` → `/tmp/lp2571-canary2/shim/claude`（意図したforwarder）に解決
  - `claude --version` 実行 → forwarder経由で real claude (`2.1.263 (Claude Code)`) が正しく応答
  - receipt file に run-scoped nonce (`5d6d7e20ee2e446e90cf5421ff820d51`) が正しく書き込まれ、事前生成したnonceと一致
  - ambient decoy（別ディレクトリの偽`claude`）は未発火（`decoy_marker.txt` 不存在）

### Resumable Claude conversation

- `claude -p --output-format json` でforwarder経由の実conversationを作成
- session ID を取得（本文書では `run_worktree_agent_runtime_smoke.py` の既存方針＝session_idはevidenceに含めない、PR #1921 P1 fix-deltaに合わせてredact。以下 `<session-id>` と表記）
- canary token（例: `lp2571tokenXXXXXX` 形式、機密ではないrun固有token）を記憶させる1ターンを実行

### 実際のcold restart

1. `herdr session stop lp2571-repro-4945a854 --json` → `"stopped": true`
2. 同一named sessionをouter pane内で再起動（`herdr pane run <outer-pane-id> "herdr --session lp2571-repro-4945a854"`）
3. `herdr --session lp2571-repro-4945a854 workspace list` → 直前のworkspace（`w1`, `w2`）がsnapshotから復元されていることを確認（Herdr側のsession-state restore機構が正常動作）

### Post-restart PATH再構築（人間による再注入なし）

- restore直後の `w2:p1` pane で、**一切のPATH再注入を行わず** `command -v claude` を実行
  → 結果: `/home/squne/.local/bin/claude`（real native claude）に解決。**意図したforwarderは自動的には復元されない**。
- これは事前のweb調査（Herdr公式ドキュメント: session restoreはworkspaces/tabs/panes/cwd/layout/focusのみを復元し、PATH等のenvironment varsは復元しない。Linux/WSLでは`shell_mode=auto`によりnon-login shellが既定）と完全に整合する実機causal proof。

### Post-restart: project-scoped bootstrap再適用後の再検証

- global config を一切変更せず、post-restart shell内で再度 `export PATH="<shim>:<decoy>:$PATH"` を実行（project-scoped bootstrapが担う想定の操作を模擬）
  → `command -v claude` → 意図したforwarderに再解決
- **`claude --resume <session-id>`** をforwarder経由で実行し、事前に記憶させたcanary tokenを尋ねるprompt送信
  → 応答: 正確なtoken文字列を返答（**同一Claude conversation/session continuity確認済み**）
  → receipt file のnonce一致を再確認
  → ambient decoy不発火を再確認

### Native Claude絶対path pinning（self-recursion回避）

forwarder scriptは `command -v claude` へのfallbackを一切行わず、pre-shim段階（shimをPATHへ追加する前）に解決した real native claude絶対path (`/home/squne/.local/bin/claude`) を forwarder script生成時点で固定的にexec対象へ埋め込んでいる（`exec '/home/squne/.local/bin/claude' "$@"`）。このためforwarder自身が自己参照するself-recursionのリスクは構造的に排除されている（`CLAUDE_GPT_CLAUDE_BIN` pre-shim resolutionパターン、commit `205ec3f5` と同型の設計）。

## Cleanup

- disposable named Herdr session: `herdr session stop` → `herdr session delete` 実行、`herdr session list --json` で消失確認済み（最終状態は `default` セッションのみ）。
- outer workspace（`wS`、このrun専用）: `herdr workspace close` で除去済み。
- run-scoped temp artifacts（nonce/receipt/shim/decoy/session出力等、`/tmp/lp2571-canary2/` 配下）: 削除済み。
- human default session: canary実行前後で `w4`（人間の実ワークスペース、pane_count=7）以外のworkspaceが存在しないことを確認済み。stop/restart/削除等の破壊的操作は一切行っていない。

## feasibility_verdict

```yaml
feasibility_verdict: partially_feasible_with_constraints
```

### 判定理由

Herdr cold restart経路でのproject-scoped `claude` forwarder interceptionは、**以下の制約付きで**causal runtime proofにより実現可能であることを実証した：

1. **interception自体・実際のresume・同一conversation continuityは全てcausalに成立する**: real forwarder（`exec <absolute-path> "$@"` 型、symlink不使用）+ run-scoped nonce receipt + ambient decoy negative controlの3点セットで、意図したforwarderのみが実行されたことをprocess-boundary evidenceとして確認済み。cold restart後の `claude --resume <session-id>` が実際に実行され、同一conversationのcontinuity（canary tokenの正確な想起）も確認済み。

2. **ただし、ambient herdr `--env PATH=...` 注入だけでは不十分**: 対話シェルの起動時rc処理（`~/.local/bin` 等の既知pathの再prepend）により、cold restart前の時点で既にshimは上書きされる。

3. **cold restart後は、herdr自体がPATHを一切保存・復元しない**（Herdr公式ドキュメントと実機の両方で確認済み）ため、restore直後のfresh shellは常に「shim未適用」の状態から始まる。

4. したがって #2569 でこのアーキテクチャを採用する場合、**repository-owned bootstrap機構（例: project-scoped `.envrc`/direnv相当、または明示的なdispatcher起動スクリプトによるPATH再主張）が、通常のshell起動時とcold restart後の両方で、shell rc処理より後に確実に実行される**という追加制約が必須になる。単純な「herdrの`--env`でPATHを渡すだけ」の設計は採用できない。

この制約は、Issue #2571 の概念図に示された `dispatcher → scripts/claude-gpt/launch.sh --claude-bin "$REAL_CLAUDE" -- --resume "$SESSION_ID"` という、PATH shadowingに依存せず明示的にdispatcherを起動する設計（launch.shの `--claude-bin` 明示指定パターン）であれば、そもそもPATH解決の順序問題を回避できる可能性が高い。#2569 実装時は、ambient PATH-shim方式ではなく、この明示dispatcher呼び出し方式を優先することを推奨する。

## Next Action（Handoff）

- #2569 は本判定（`partially_feasible_with_constraints`）を前提に着手可能。ただし上記の追加制約（repository-owned bootstrap機構によるPATH再主張、または明示dispatcher呼び出し方式の採用）を設計に反映すること。
- `run_worktree_agent_runtime_smoke.py` の `HERDR_SESSION` 環境変数依存箇所は、#2569実装時に `--session` flagベースへの見直しを検討すること（本spikeでは変更していない）。
