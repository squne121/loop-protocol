この fixture は M2 strict existence で missing target を検出する異常系です。

```yaml
milestone_id: M2
spec_prerequisites:
  - docs/product/features/movement-projectile.md
spec_destination:
  - docs/product/features/no-such-feature.md — missing target
  - docs/product/features/combat-core.md — combat-core
  - docs/product/features/sortie.md — sortie
```
