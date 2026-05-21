## Summary

- 

## Checks

- [ ] `pnpm typecheck`
- [ ] `pnpm lint`
- [ ] `pnpm test`
- [ ] `pnpm build`

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
