"""
Validate the InputCommand TypeScript code block in docs/product/game-logic.md.

This script checks the documented token set for the current move/aim/fire schema.
It does not parse src/input/InputMapper.ts.

Exit 0 on success, non-zero on failure.
"""
from pathlib import Path
import re
import sys

s = Path("docs/product/game-logic.md").read_text(encoding="utf-8")
section = re.search(r"^## 入力 / Input\b(?P<body>.*?)(?=^## |\Z)", s, re.S | re.M)
if not section:
    raise SystemExit("## 入力 / Input section not found")
m = re.search(r"```typescript\n(.*?)```", section.group("body"), re.S)
if not m:
    raise SystemExit("InputCommand TypeScript block not found in ## 入力 / Input section")

block = m.group(1)
normalized = re.sub(r"\s+", " ", block)

required = [
    "{ type: 'move'; axisX: number; axisY: number",
    "{ type: 'aim'; x: number; y: number",
    "{ type: 'fire'",
]
for token in required:
    if token not in normalized:
        raise SystemExit(f"missing required token: {token}")

for forbidden in [
    "MoveIntent", "AimIntent", "FireIntent",
    "IssueAllyCommandIntent", "PauseIntent",
    "direction: Vec2", "angle:",
]:
    if forbidden in block:
        raise SystemExit(f"forbidden token remains in code block: {forbidden}")
