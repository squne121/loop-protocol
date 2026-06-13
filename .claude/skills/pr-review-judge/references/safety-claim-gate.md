# Safety Claim Gate

## Safety-sensitive 判定（fail-closed）

以下のいずれかで safety-sensitive と判定。

- changed path が `transport|permission|sandbox|auth|mcp|tool` や `.github/workflows/**`、`.claude/skills/**`
- PR / diff / issue に safety 境界ワードを含む
- issue ラベル/本文に `safety`, `permission`, `runtime verification`, ... が含まれる

## 要求

- `Safety Claim Matrix` セクション必須
- `Claim / Implemented / Not controlled / Evidence / Follow-up` を確認

## 禁止条件

- `Not controlled` が非空なのに open な follow-up が無い
- `Evidence` が linked issue ならびに PR 証跡と不整合
- Not controlled と無限定 safe/read-only/main claim が衝突する主張
