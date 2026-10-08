---
name: counterfactual-fixture-skill
description: skill_text_counterfactual の runtime fixture（Issue #2981）。固定の 2 行だけを返す。throwaway worktree 専用。
---

# Counterfactual fixture skill（BASE）

この skill が呼び出されたら、tool を一切呼ばず、ファイルも読まず、次の 1 行 **だけ** を返す（コードフェンスは含めない）。

```text
CF2981-NO-MARKER
```
