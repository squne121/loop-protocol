---
title: roadmap refs validator 正常系 fixture
description: この frontmatter は frontmatter 除外と日本語比率チェックの両方を満たすための説明です
---

この fixture は validator が成功すべき正常系サンプルです。

```yaml
milestone_id: M2
spec_prerequisites:
  - docs/product/features/movement-projectile.md
spec_destination:
  - docs/product/features/movement-projectile.md — movement-projectile
  - docs/product/features/combat-core.md — combat-core
  - docs/product/features/sortie.md — sortie
```

```yml
milestone_id: M3
spec_prerequisites:
  - docs/product/features/persistence.md
spec_destination:
  - docs/product/features/persistence.md — persistence
  - docs/product/features/accessibility-save-policy.md — accessibility
```
