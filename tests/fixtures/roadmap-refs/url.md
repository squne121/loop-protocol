この fixture は外部 URL を repo 内参照として受け入れないことを確認する異常系です。

```yaml
milestone_id: M3
spec_prerequisites:
  - docs/product/features/movement-projectile.md
spec_destination:
  - https://example.com/docs/product/features/movement.md — external link
```
