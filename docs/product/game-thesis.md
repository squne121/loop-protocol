---
doc_id: DOC-PRODUCT-GAME-THESIS-001
title: Game Thesis
status: draft
issue: "#281"
parent_issue: "#254"
canonical_source: docs-product
sdd_boundary: docs-ssot-wins
---

# Game Thesis

This document defines the core concept, target player, design pillars, non-goals, and design hypotheses for LOOP_PROTOCOL as a product.

## Pitch

A top-down action RTS where the player intervenes in autonomous AI-to-AI combat as an elite unit, analyzing defeated enemy technology and incorporating it into growing military capability. Leadership centers on short, localized interventions rather than grand strategy.

## Target Player

Players seeking moment-to-moment action agency and the satisfaction of reverse-engineering—not intricate macro-management of large armies. The appeal lies in direct control of a high-leverage unit, lightweight tactical direction, and the progression loop of combat → data extraction → weapon enhancement.

## Design Pillars

### 1. Combat Readability

Instant visual comprehension of enemy positions, allied units, projectile threats, and danger zones via shape, silhouette, and form—not color alone. Combat judgments must never be obscured by UI or excessive visual noise.

### 2. Industrial and Unfinished Aesthetics

Eschew polished character art. Instead employ monochromatic design—black, white, gray schematics, blueprint visuals, sensor readouts, and procedural soundscapes—to establish the austere atmosphere of autonomous warfare.

### 3. Reverse Engineering Satisfaction

Reframe enemy defeat not as score increment, but as data extraction and analysis. Visualize the progression from unknown enemy technology to enhanced player weaponry, tying immediate combat success to tangible equipment growth.

## Non-Goals

- Territory expansion, node-based campaign progression, or complex base building
- Online network multiplayer or peer-to-peer multiplayer gameplay
- Full voice acting, orchestral soundtracks, or high-fidelity asset-dependent presentation
- Direct imitation of existing robot / mecha franchises or their proprietary terminology
- Color as the sole channel for information or faction identification

## Design Hypotheses

Grounded in the MDA framework, these hypotheses link mechanics to observable dynamics and aesthetic targets. Each is a testable assumption, falsifiable through playtest observation and downstream spec refinement.

```yaml
design_hypotheses:
  - id: HYP-001-ace-intervention
    aesthetic: agency, competence, leverage
    mechanics: |
      Self-directed player unit with superior mobility/damage vs. allied AI
      Localized crisis trigger (friendly line at risk)
      Lightweight macro-directive system for allied guidance
      Real-time decision window (seconds, not minutes)
    expected_dynamics: |
      Friendly AI maintains nominal defense
      Player presence in sector shifts tactical balance
      Successful intervention observable: ally survival, threat elimination
      Failure felt: allies retreat when player cannot respond in time
    falsification_signal: |
      Player success relies primarily on allied AI strength
      Intervention mechanic confuses player agency
      Playtest shows preference for macro-strategy over action
    downstream_owner: game-design, game-logic, mvp-scope, playtest-protocol

  - id: HYP-002-tech-extraction-loop
    aesthetic: discovery, progression, mastery
    mechanics: |
      Enemy defeat yields analyzable technology data
      Preparation phase: player selects upgrades from data
      Visible connection: upgrades feed into next combat
    expected_dynamics: |
      Repeated combat → gradual escalation of player capabilities
      Early sorties defensive; later sorties allow offensive initiative
      Player perception: each sortie contributes, creating momentum
    falsification_signal: |
      Upgrades perceived as cosmetic
      Preparation phase tedious rather than strategic
      Tech extraction conflicts with story coherence
    downstream_owner: game-logic, mvp-scope, playtest-protocol
```

## Trace Links

This document is grounded in and traces to:

- GitHub Issue `#281` — this issue
- GitHub Issue `#254` — parent SDD rollup issue
- `docs/product/requirements.md` — global MVP requirements and non-goals
- `docs/product/game-overview.md` — non-normative experience overview
- `docs/dev/product-spec-lifecycle.md` — compact spec creation and lifecycle policy
- `docs/adr/0002-sdd-tool-adoption.md` — SDD policy and docs-ssot-wins boundary
