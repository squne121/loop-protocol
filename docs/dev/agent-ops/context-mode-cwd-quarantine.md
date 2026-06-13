---
schema: context_mode_cwd_quarantine_doc_v1
issue: "#826"
parent_issue: "#813"
status: quarantine_continue
generated_at: 2026-06-13T14:30:00Z
---

# context-mode execution-like tools: CWD Quarantine Matrix

## 背景

upstream [mksglu/context-mode#756](https://github.com/mksglu/context-mode/issues/756) では、Claude Code が worktree にいるにもかかわらず `ctx_execute` が main checkout 側で実行される silent wrong-directory bug が報告されている。read-only コマンドであっても、cwd が違えば `rg` / `git log` / test log 参照結果が誤る。

本文書は「`ctx_execute` を許可できるか判断する」ことではなく、**「許可しない理由を runtime evidence として固定する」** ことを目的とする。

## Execution-like Tools Quarantine Matrix

| tool | policy | cwd_probe_required | quarantine 理由 |
|---|---|---|---|
| `ctx_execute` | deny | yes | upstream #756 OPEN; MCP server process.cwd() ≠ session cwd; explicitly deny-listed |
| `ctx_batch_execute` | deny | yes | 複数コマンド実行; ctx_execute と同一 cwd リスク; sibling tool 未評価 |
| `ctx_execute_file` | deny | yes | repo-relative ファイル解決リスク; sibling tool 未評価 |

## Quarantine 継続理由

以下のいずれかに該当する限り、quarantine_continue とする:

1. **upstream #756 が OPEN** — ctx_execute の wrong-tree 実行が upstream で未修正
2. **local probe 不可** — 通常 profile では ctx_execute が deny-listed のため、isolated profile なしには比較不能
3. **sibling tools 未評価** — ctx_batch_execute / ctx_execute_file の実行相当性が未確定
4. **probe 不一致** — probe 実施時に Bash と ctx_execute の cwd が一致しない場合

## build / test / git / repo grep / log summary の移送禁止方針

**build / test / git write / repo-wide grep / log summary を context-mode execution-like tools へ移さない。**

理由:
- upstream #756 が open の間、ctx_execute の cwd は MCP server の spawn-time process.cwd() に固定されており、session cwd と一致しない
- read-only コマンドであっても wrong-tree なら証跡が誤る（例: `rg` が main checkout を対象にし worktree の変更を見逃す）
- build / test は正確な cwd が前提; wrong-tree build は silent success を返しうる

この方針は upstream #756 が closed かつ project policy で allow に変更されるまで維持する。

## CLAUDE_PROJECT_DIR の扱い

`CLAUDE_PROJECT_DIR` は Claude Code が stdio MCP server に渡す stable project root であり、session cwd と同一ではない（Claude Code docs 参照）。

- cwd の正否判定に `CLAUDE_PROJECT_DIR` を使わない
- `informational_only.claude_project_dir` として artifact に記録するにとどめる
- `env | grep CLAUDE_PROJECT_DIR` は環境変数 dump のため使用しない; `printf '%s\n' "${CLAUDE_PROJECT_DIR:-<unset>}"` を使う

## probe 手順（将来参照用）

isolated throwaway profile で比較する場合は固定 read-only command のみを使う:

```bash
pwd
printf 'CLAUDE_PROJECT_DIR=%s\n' "${CLAUDE_PROJECT_DIR:-<unset>}"
git rev-parse --show-toplevel
git rev-parse --is-inside-work-tree
git branch --show-current
git rev-parse HEAD
git status --short --branch
```

probe profile は commit しない (`profile_committed: false`)。deny が復元されたことを artifact に記録する。

## 関連 Issues

- parent: #813
- #824 (CLOSED): context-mode 導入・バージョン確認
- #825 (CLOSED): context-mode deny rule fail-closed 化
- upstream: [mksglu/context-mode#756](https://github.com/mksglu/context-mode/issues/756)

## Artifact

`.claude/artifacts/context-mode/cwd-comparison-result.json` — verdict: `probe_blocked_quarantine_continue`
