### Step 4: PRレビュー（`pr-reviewer` SubAgent）

#### Step 4 の責務境界（相互参照禁止）
pr-reviewer の責務: AC 充足判定 + mergeability 判定 + Allowed Paths 検査
**adversarial-reviewer（Step 3）の結果を参照しないこと（相互参照禁止）。**

adversarial-reviewer と pr-reviewer は依存なしで並列起動可。

**pr-reviewer の審査対象外（委任プロンプトに明記すること）:**
- AC 妥当性検証（「そもそも AC 自体が適切か」）→ `issue-contract-review` で着手前に審査済み
- project convention 適合（architecture-fit）→ Step 3 adversarial-reviewer の責務
- 信頼性リスク / セキュリティリスク → Step 3 adversarial-reviewer の責務

Step 4 は Step 3 と並列独立実行する（Step 3 完了を待たない）。
Step 4 の実行条件は **TEST_VERDICT: PASS または PARTIAL**（Step 2 の検証結果）のみとする。ADV_VERDICT は待たない。

MEDIUM/LOW のみの NEEDS_FIX は Step 4 実行を妨げない。MEDIUM/LOW 指摘は pr-reviewer の判断に委ねる。

### Step 4 補助参照（`local-ci/just-check`）

- Step 4 完了前に PR head SHA の Commit Status から `local-ci/just-check` を取得し、state と `head_sha` の一致を確認する。
- `state != success`、status missing、または head SHA 不一致の場合は `REQUEST_CHANGES` ブロッカーとして扱う。
- `head_sha` 不一致は `reviewed_head_sha` と PR head の整合不一致として扱い、Step 5 の `reviewed_head_sha` 条件（未レビュー commit ガード）と合わせて再検証を要求する。

`TEST_VERDICT: PARTIAL` の場合は、pr-reviewer への SubAgent に「live 検証保留」の旨を必ず伝達し、pr-reviewer が live 検証の要否を判断できるようにすること。

TEST_VERDICT: FAIL の場合は Step 5（判定）へスキップしてフィードバックループへ入る。

### 最終統合判定

Step 3.5 と Step 4 の両方が完了した後、オーケストラレータが統合判定を行う。
統合判定の条件: 「normalized CRITICAL/HIGH 件数 = 0 AND pr-reviewer Verdict = APPROVE」の場合のみ LOOP_VERDICT: APPROVE とする。
いずれかが未完了の場合は統合判定を実施しない。

### INLINE_COMMENT と LOOP_VERDICT の関係

`pr-reviewer` が gh api の inline review comment で line-specific な補助指摘を投稿している場合でも、Step 5 の LOOP 判定は `LOOP_VERDICT` と `reviewed_head_sha` のみを canonical source（正本）として扱う。
inline comment は修正の根拠や再現性を高める補助 evidence であり、ループ終了の可否判定そのものには影響しない。

`pr-review-judge` スキルに従って PR を review し、verdict を PR にコメントする。

**SubAgent への必須渡し情報:**
- PR番号
- Linked issue番号
- `REVIEWED_HEAD_SHA`（Step 3 pre-fetch で固定した PR head SHA）。pr-reviewer は LOOP_VERDICT YAML に **必ず** `reviewed_head_sha: <SHA>` を記載すること。**YAML ブロック内に必ず記載し、ブロック外への記載は禁止**。記載がない LOOP_VERDICT は Step 5 で APPROVE と見なさない。YAML ブロック外への記載はガイドライン違反であり、Step 5 パーサーはブロック内外を区別せず行単位で読み取る（pr-review-judge/SKILL.md の【重要な制約】参照）。
- `model` / `model_reasoning_effort`: `model_overrides["pr-reviewer"]` に指定がある場合は、その値を CodexCLI SubAgent 委譲プロンプトへ明示して渡す

**pr-reviewer の手順:**
1. `pr-review-judge` の手順に従う。
2. verdict を PR にコメントする（self-authored PR の場合は `gh pr review --comment`）。LOOP_VERDICT YAML には `reviewed_head_sha` を必須フィールドとして記載すること:
   ```bash
   gh pr review <PR番号> --comment --body "## Verdict: [APPROVE / REQUEST_CHANGES]

   ### Baseline Failure（既存問題 — 今回差分と無関係）
   <!-- baseline failure: main ブランチで既存する問題・技術的負債。実装者が今回対応不要のもの -->
   - <既知問題の一覧、または「なし」>

   ### 今回差分 Blocker（今回の変更に起因する blocker）
   <!-- diff blocker: この PR でマージをブロックする問題。実装者が今すぐ修正すべき対象 -->
   - <blocker の一覧、または「なし」>

   ### Non-blockers
   - <推奨・改善事項の一覧、または「なし」>

   ## LOOP_VERDICT
   \`\`\`yaml
   verdict: APPROVE   # または REQUEST_CHANGES
   blockers: []       # 今回差分 Blocker リスト（APPROVE の場合は空）
   mergeable: MERGEABLE
   mergeStateStatus: CLEAN
   reviewed_head_sha: <REVIEWED_HEAD_SHA>  # オーケストラレータから渡された PR head SHA を必ず転記
   \`\`\`"
   ```

   **【実投稿時の注意】**: 実際の `gh pr review --body` コマンド文字列を生成する際は、コードフェンス（` ``` `）を `\` でエスケープしないこと。`\`\`\`yaml` ではなく ``` ` を直接書く。heredoc 内でもエスケープは不要。エスケープした場合、GitHub PR コメントに `\`\`\`yaml` が literal で表示され、Step 5 の自動パーサが YAML ブロックを抽出できなくなる。

SubAgent 完了後、オーケストラレータが LOOP_STATE を更新する:
```bash
gh issue comment <Issue番号> --body "$(cat <<'EOF'
## LOOP_STATE
\`\`\`yaml
iteration: <N>
phase: reviewed
status: running
pr_url: <PR_URL>
last_verdict: <APPROVE or REQUEST_CHANGES>
\`\`\`
EOF
)"
```

---


**SubAgent レート制限フォールバック:**

`pr-reviewer` SubAgent がレート制限（エラーまたは空レスポンス）に達した場合、オーケストラレータは以下を実施する:

1. レート制限を検出し、`pr-review-judge` SubAgent を直接呼び出す。
2. PR・linked issue・REVIEWED_HEAD_SHA 情報を渡し、review・verdict を実行させる。
3. 結果を PR にコメントする（`LOOP_VERDICT` YAML 形式）。
4. LOOP_STATE に記録する。

（理由: `pr-reviewer` SubAgent は内部で `pr-review-judge` スキルを呼び出すため、レート制限時は直接 `pr-review-judge` に委譲する）
