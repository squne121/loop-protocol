---
name: counterfactual-fixture-skill
description: skill_text_counterfactual の runtime fixture（Issue #2981）。固定の 2 行だけを返す。throwaway worktree 専用。
---

# Counterfactual fixture skill（候補、説明文のみ改稿）

この版では導入文だけを書き直した。手順は変更していない。

この skill が呼び出されたら、tool を一切呼ばず、ファイルも読まず、次の 2 行を **この順序で、これだけ** 返す（コードフェンスは含めない）。

```text
{{MARKER_ONE}}
{{MARKER_TWO}}
```
