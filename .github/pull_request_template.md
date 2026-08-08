## Summary

- 

## Checks

- [ ] `pnpm typecheck`
- [ ] `pnpm lint`
- [ ] `pnpm test`
- [ ] `pnpm build`
- 上記コマンドをすべてローカルで実行し、成功したことを確認してからチェックしてください

## Schema Change Applicability

> PR が schema（producer-consumer 境界をまたぐ machine-readable contract）を変更する場合に記載してください。
> 該当しない場合は `not_schema_change` を選択して理由を 1 行記載してください。
> `schema_change` または `uncertain` の場合、下の `## Schema Consumer Inventory` セクションの記載が必須です（欠落は APPROVE 禁止）。

- decision: `schema_change` | `not_schema_change` | `uncertain`
- reason: （変更する schema の ID、または該当しない理由を記載）

## Schema Consumer Inventory

> `schema_change` または `uncertain` の場合に必須です。`not_schema_change` の場合は `N/A` と理由を記載してください。
> pr-review-judge が Schema Consumer Inventory 欠落を検出した場合、APPROVE 禁止（REQUEST_CHANGES）になります。

**変更対象 schema:** （例: `delegation_result/v1`）

**before/after 差分:**

```yaml
# before
# （変更前のキー名・フィールド・型）

# after
# （変更後のキー名・フィールド・型）
```

**Consumer 列挙（rg コマンド例と結果）:**

```bash
rg -l "<schema-id-or-key>" .
# 出力結果をここに貼り付ける
```

**Consumer 更新状況:**

| Consumer ファイル | 更新有無 | 備考 |
|---|---|---|
| （rg で列挙したファイル） | 更新済み / 不要（理由） / 未対応 |  |

### Compatibility Decision（互換性判定）

> schema 変更の互換性を明示してください。`not_schema_change` の場合は各フィールドに `N/A` と記載してください。

- change_type: `additive` | `rename` | `remove` | `type_change` | `semantic_change` | `N/A`
- compatibility: `backward_compatible` | `breaking` | `uncertain` | `N/A`
- migration_required: `yes` | `no` | `N/A`
- migration_or_followup: #N or `N/A`
- reason:
- 上記の各フィールドを漏れなく記載してください

## Visual Impact Declaration（視覚影響申告）

> UI（DOM/CSS）変更を含む PR は `VISUAL_IMPACT_DECLARATION_V1` を1個のみ、fenced ```yaml block として記載してください（Issue #2019）。
> UI 変更を含まない PR は該当ブロックを省略できます（`visual-impact-policy` job は対象 surface が無ければ何もしません）。
> このブロックは untrusted な自己申告として扱われ、CI が独立に生成する `VISUAL_IMPACT_DECISION_V1` のみが merge-ready 判定の正本です。

```yaml
schema: VISUAL_IMPACT_DECLARATION_V1
surfaces: []
```

## Safety Claim Matrix

> このセクションは安全境界・権限・サンドボックス・transport・auth・MCP・native tools・approvalMode・runtime verification・workflow gate に触れる PR で必須です。
> 該当しない場合は `N/A` と一行の理由を記載してください。
> pr-review-judge が Safety-sensitive PR と判定した場合、このセクションが欠落していると APPROVE 禁止（REQUEST_CHANGES）になります。

| Claim | Implemented? | Not controlled | Evidence | Follow-up |
|---|---|---|---|---|
|  | yes / no / partial |  | VC コマンド / 結果 / リンク | #Issue 番号 or N/A |

**記載ルール:**
- `Not controlled` が非空の場合、PR title / summary / docs で無限定の `safe` / `read-only` / `sandboxed` / `isolated` / `complete` を使わないこと（APPROVE 禁止条件）
- `Evidence` は本 PR の検証コマンド結果または linked issue の Verification Commands と対応させること
- `Not controlled` が非空の場合、`Follow-up` に open な後続 Issue を記載すること（必須）
- 「閉じる経路を正確に書く」原則: 主張の射程を実装済みの境界に限定すること（例: 「ACP client-side の fs/terminal proxy を提供しない」は許可、「read-only ACP transport」は禁止）

## Notes

- Related issue:
- MVP scope:
- AI / NotebookLM usage:
- 備考: 関連 Issue・MVP スコープ・AI 利用状況を記載してください
