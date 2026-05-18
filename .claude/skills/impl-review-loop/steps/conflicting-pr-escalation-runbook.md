## CONFLICTING PR Escalation Runbook

**呼び出し主体: implementation-worker (`implement-issue` SKILL.md からの参照)**

PR が main との CONFLICTING 状態になった場合、以下の手動対応手順を実施してください。

### 原因と判断基準

**PR が CONFLICTING になるケース（main-side 起因の例）:**
- Iteration N の実装が main に未マージ・未リバート状態のまま、main 上で別の変更が merge された場合
- 同一ファイルの別行を編集した場合（non-overlapping conflict）
- 同一行を編集した場合（overlapping conflict）

### 手動 Resolve 手順（non-overlapping / overlapping 両対応）

1. **Conflict 詳細を確認する:**
   ```bash
   git -C "$WORKTREE_ABS" fetch origin main
   git -C "$WORKTREE_ABS" rebase origin/main
   # または 
   git -C "$WORKTREE_ABS" merge origin/main  # conflict が発生する場合は以下の手順へ
   ```

2. **non-overlapping conflict（異なる行の編集）の場合:**
   - Git が自動 merge を試みるが失敗した場合、以下で手動 resolve する:
     ```bash
     # conflict マーク（<<<<<<, ======, >>>>>>）が挿入されたファイルを確認
     git -C "$WORKTREE_ABS" status
     # conflict marker を確認・修正（実装側と main 側の diff を比較して統合）
     # diff を確認し、non-overlapping な場合は両方の変更を保持する
     git -C "$WORKTREE_ABS" add <resolved_file>
     git -C "$WORKTREE_ABS" rebase --continue  # rebase 中の場合
     # または
     git -C "$WORKTREE_ABS" commit  # merge 中の場合
     ```

3. **overlapping conflict（同一行の編集）の場合 - 判断ポイント:**
   ```bash
   # git diff --ours/--theirs は git diff では存在しないため、git show で index stage を参照する
   git -C "$WORKTREE_ABS" show :2:<file>   # ours（index stage 2）: PR ブランチ側の version
   git -C "$WORKTREE_ABS" show :3:<file>   # theirs（index stage 3）: main 側の version
   # あるいは両者の diff を直接確認
   git -C "$WORKTREE_ABS" diff :2:<file> :3:<file>
   # どちらの変更を優先するかを判断
   # - Issue contract が指定していない場合は、両方が意味をなす方を選択
   # - または実装者に判断を委譲
   ```

4. **Conflict が複雑な場合の戦略選択（推奨）:**

   Conflict が複雑な場合（3 ファイル以上の同時 conflict など）は、以下のいずれかの戦略を選択してください:
   
   - **戦略 A（推奨: 削除）**: stash 戦略は commit 済み変更に対しては効果がないため削除。複雑な場合は人間判断で rebase 戦略 or merge commit 戦略を選択してください。
   
   - **戦略 B（正しい手順）**: commit 済みの実装変更を soft reset で index に戻してから stash を使う場合:
     ```bash
     # 前提: 現在の HEAD は PR の実装 commit を含み、origin/main は進んでいる状態
     # PR ブランチの commit を soft reset してインデックスに戻す
     # （この時点で HEAD は origin/main に揃うが、index/worktree は実装差分を保持）
     git -C "$WORKTREE_ABS" reset --soft origin/main
     # index + worktree を退避（退避対象は PR の実装差分）
     git -C "$WORKTREE_ABS" stash
     # stash pop で origin/main 上へ実装差分を再適用（ここで conflict が発火する）
     git -C "$WORKTREE_ABS" stash pop
     # conflict を resolve（non-overlapping / overlapping の判定は戦略 A/本 runbook §1-3 を参照）
     # 全 conflict 解決後、再コミット
     git -C "$WORKTREE_ABS" add -A && git -C "$WORKTREE_ABS" commit -m "rebase: resolve conflicts"
     # **test-runner を再実行して PASS 確認してから push する（必須）**
     ```
     備考: soft reset 直後の HEAD は既に `origin/main` と一致しているため、ここで `git rebase origin/main` を実行しても no-op で conflict は解消されない。conflict は `git stash pop` の段階で発火するため、rebase を挟まずに直接 stash pop する。

5. **Force push（conflict 解決後、必ず test-runner 再実行後に実行）:**
   
   **重要**: force-push 前に必ず test-runner を再実行し、PASS を確認してから実行してください。
   ```bash
   # 1. test-runner を再実行して conflict 解決後の正確性を確認
   # ...test-runner verify...
   # 2. テスト PASS 確認後に force-push
   git -C "$WORKTREE_ABS" push origin "$expected_branch" --force-with-lease
   # PR が自動更新される
   ```

### PR #1103 実例

PR #1103（merged）では、以下のケースで CONFLICTING が発生しました:
- **状況**: Iteration 5 の実装が PR ブランチに commit されていたが、同じ期間に main 側で別の PR（#981）がマージされた
- **原因**: main 側の変更と PR #1103 側の変更がクラッシュ
- **解決**: 戦略 B（soft reset → stash → stash pop で conflict 解消 → 再 commit）で非重複マージを実施。最終的に force-push で main への merge を実現（当時は `rebase origin/main` 手順で記録されていたが、soft reset 後の rebase は no-op のため手順を整理して本 runbook に反映済み）

詳細: PR #1103 の「Iteration 6 rebase resolution」commit メッセージを参照。

### Halt with Escalation: 人間判断が必要な場合

以下に該当する場合は、escalation ポーズして人間判断を求めてください:

1. **overlapping conflict で、どちらの変更を優先すべきか不明な場合**
   - LOOP_STATE に `phase: escalation_conflicting_decision_required` を記録
   - Issue comment で conflict 詳細・両バージョンの diff を提示
   - 人間が判断を返すまで待機

2. **Conflict が過度に複雑（3 ファイル以上の同時 conflict など）**
   - マージ戦略の見直しが必要
   - escalation 記録後、Issue で新 Issue（e.g., `merge-strategy-review`）の作成を検討

---
