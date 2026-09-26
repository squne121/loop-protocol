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

## Claim-to-Evidence Coverage（有限 subclaim の直接証拠確認、Issue #2765）

Safety Claim Matrix の Claim が有限の複数 subclaim（例: 複数トークン形式、複数プラットフォーム、複数実行パス等）について all / each / 全対応 / 非回帰などの exhaustive completeness を主張する場合、常に test evidence を要求する設計にはせず、Evidence が各 subclaim を直接 support しているかを確認する claim-to-evidence coverage を適用する。

- Evidence が **test** の場合のみ、`references/ac-evidence-checks.md` の Enumerated / Exhaustive Claim Evidence Coverage rule（case identity AND relevant executable assertion が PASS していること）を適用する。
- Evidence が test 以外（source/configuration inspection、policy/config diff、permission declaration、runtime artifact、CI/CheckRun、deterministic validator output 等）の場合は、これらの非-test evidence を「test でない」という理由だけで不当に排除しない。各 subclaim に対応する具体的な evidence の所在と、Claim との対応関係（どの subclaim をどの evidence が support するか）を確認する。
- 単なる例示列挙（「例: A/B」等）は本 rule の対象外。
