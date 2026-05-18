### Step 1.5: spec ドキュメントレビュー（オプション）

**実施条件**: Issue 本文または Allowed Paths に `.kiro/specs/<feature>/(requirements|design|tasks).md` パターンで spec ドキュメントパスが含まれる場合のみ実行する。該当がない場合はこのステップをスキップして Step 2 へ進む。

**実行フロー（fail-closed）:**

1. **トリガー判定**: Issue 本文と Allowed Paths に対して以下のコマンドで spec パスの有無を確認する。
   ```bash
   issue_body=$(gh issue view <Issue番号> --json body --jq '.body')
   printf '%s\n' "$issue_body" | grep -oE '\.kiro/specs/[^/]+/(requirements|design|tasks)\.md'
   printf '%s\n' "$issue_body" | awk '
     /^## Allowed Paths/ { in_section=1; next }
     /^## [^#]/ { if (in_section) exit }
     in_section { print }
   ' | grep -oE '\.kiro/specs/[^/]+/(requirements|design|tasks)\.md'
   ```
   いずれもヒットがない場合はこのステップをスキップする。

2. **feature 名解決**: Allowed Paths を優先して `.kiro/specs/<feature>/` から feature 名を抽出する。Allowed Paths にヒットがない場合は Issue 本文全体から最初にヒットした `.kiro/specs/<feature>/` を採用する。複数 feature が混在して主 feature を一意に決められない場合は、Step 1.5 全体をスキップする。

3. **feature 名妥当性確認**: 抽出した feature 名に対して以下を実行し、`spec.json` が存在しない場合はこのステップをスキップする。
   ```bash
   ls .kiro/specs/<extracted-feature>/spec.json 2>/dev/null || (echo "spec not found"; exit 1)
   ```

4. **存在する spec ドキュメントの特定**: 以下で対応ファイルの存在を確認する。
   ```bash
   [ -f ".kiro/specs/<feature>/requirements.md" ] && echo "requirements" || true
   [ -f ".kiro/specs/<feature>/design.md" ] && echo "design" || true
   [ -f ".kiro/specs/<feature>/tasks.md" ] && echo "tasks" || true
   ```

5. **SubAgent 委譲**: Step 1.5 を実行する場合、`spec-document-reviewer` SubAgent に以下を渡す。
   - `issue_number`: `<Issue番号>`
   - `feature_name`: 抽出した feature 名
   - `spec_files`: 存在するファイルのリスト（requirements / design / tasks のいずれか）
   - `model_overrides`（存在する場合）: 親の `model_overrides` を継承する。CodexCLI SubAgent へ委譲する場合は `spec-document-reviewer` の `model` と `model_reasoning_effort` をそのまま渡す

   `spec-document-reviewer` SubAgent の責務は以下とする。
   - 抽出した feature 名が正当か最終確認する
   - `spec_files` リストの各ファイルに対して対応レビュースキル（`cc-sdd-requirements-review` / `cc-sdd-design-review` / `cc-sdd-tasks-review`）を実行する
   - 各 spec の品質・一貫性・トレーサビリティを確認する
   - 結果を構造化レポートで返す

6. **出力フォーマット**: SubAgent からの結果を以下の形式で Issue にコメントする。
   ```bash
   gh issue comment <Issue番号> --body "$(cat <<'EOF'
   ## spec ドキュメントレビュー結果（iteration <N>）

   **feature**: <feature_name>
   **レビュー対象**: <requirements.md / design.md / tasks.md（存在するもの）>
   **判定**: <spec-document-reviewer の判定>

   <spec-document-reviewer からの詳細結果>

   ---
   *by spec-document-reviewer, $(date -u +%Y-%m-%dT%H:%M:%SZ)*
   EOF
   )"
   ```

**SubAgent の出力確認**:

- `INSUFFICIENT_CONTEXT` の場合: Step 1.5 を停止し、人間に欠落情報を列挙して確認を求める。Step 2 へは進まない。

**責務境界の明文化**:

| SubAgent / Skill | 責務 | 確認対象 |
|---|---|---|
| `spec-document-reviewer` + `cc-sdd-*-review` | spec ドキュメント品質・一貫性 | 要件記法（EARS形式等）・フェーズ間依存性・トレーサビリティ・consistency across requirements/design/tasks |
