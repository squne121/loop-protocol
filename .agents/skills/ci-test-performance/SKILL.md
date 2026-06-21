---
name: ci-test-performance
description: CI / テストパフォーマンスのレーン分類・hotspot 分析・意思決定を行う Codex CLI 向けブリッジ。GitHub Actions、python-test、Ruff、pytest-xdist、ci_runtime_baseline_v1、ci_test_selection/v1 に関する変更前に読む。PR レビューで CI 最適化の証跡が必要な時にも使う。
---

# Ci Test Performance (Codex CLI Bridge)

This file is a derived/non-canonical thin wrapper for the Codex CLI repo-local discovery surface.
Before executing this skill, read the canonical body at `../../../.claude/skills/ci-test-performance/SKILL.md`.
Do not treat this wrapper as the workflow procedure body.

The canonical skill defines:
- 4 CI test lanes: fast-static / python-unit / contract-artifact / integration
- `CI_TEST_PERFORMANCE_DECISION_V1` output contract
- Operative Status (current CI implementation) vs Target Policy distinction
- Consumer routing for implementation-worker / test-runner / pr-reviewer / issue-contract-review
