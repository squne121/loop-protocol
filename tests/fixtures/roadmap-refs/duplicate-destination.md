この fixture は description が異なっても同じ destination path を重複として落とすためのものです。

```yaml
milestone_id: M3
spec_prerequisites:
  - docs/product/features/movement-projectile.md
spec_destination:
  - docs/product/features/persistence.md — persistence
  - docs/product/features/persistence.md — duplicate destination
```
