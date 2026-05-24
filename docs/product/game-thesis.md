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

**Status Note**: While this document is in `status: draft`, it is not yet a normative reference for downstream implementation. It becomes normative only after Human Product Review acceptance, when status advances to `status: accepted`. Until then, the design hypotheses serve as working assumptions subject to refinement.

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
    player_promise: |
      Player feels that their direct intervention, not passive allied AI strength,
      determines the local battle outcome and can reverse a losing sector in real time.
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
    validation_method: |
      In early-session playtest, ask players which factors changed the outcome
      without prompting (self-explanation test). Record whether they spontaneously
      credit their intervention vs. allied AI. Observe engagement during time pressure.
    downstream_owner: game-design, game-logic, mvp-scope, playtest-protocol

  - id: HYP-002-tech-extraction-loop
    aesthetic: discovery, progression, mastery
    player_promise: |
      Each enemy defeated teaches the player a concrete new capability.
      The upgrade loop feels like reverse engineering and growth, not cosmetic reward.
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
    validation_method: |
      Observe playtest sessions across 3–5 consecutive sorties.
      Record whether players describe upgrades as "meaningful growth" or "just numbers."
      Measure time spent in preparation phase and correlation with next-combat loadout choices.
    downstream_owner: game-logic, mvp-scope, playtest-protocol
```

## Intent

Define the product-level judgment standard for downstream game-design, game-logic, mvp-scope, and playtest specifications. This thesis establishes the MDA framework linking player aesthetics to game mechanics and validates that proposed gameplay features serve the core fantasy of localized intervention and technology-driven growth.

## Open Questions

- Does player intervention produce perceived ace-unit agency in early playtest, or does allied AI strength dominate player perception of outcome?
- Does tech extraction and upgrade loop read as meaningful progression in gameplay, or as cosmetic reward?
- Do players prefer moment-to-moment action intervention over macro-level strategy in the sortie structure?
- How much guidance does the player need before the lightweight macro-directive system becomes intuitive?

## Playtest Hypotheses

- HYP-001-ace-intervention
- HYP-002-tech-extraction-loop

## Acceptance Criteria Boundary

This thesis does not define implementation-level EARS acceptance criteria. Concrete timing, input binding, upgrade count, sortie duration, and win/loss rules belong to downstream game-design, game-logic, and mvp-scope specs. The design hypotheses serve as validation targets for playtest feedback, not as implementation tasks.

## Trace Links

This document is grounded in and traces to:

- GitHub Issue `#281` — this issue
- GitHub Issue `#254` — parent SDD rollup issue
- `docs/product/requirements.md` — global MVP requirements and non-goals
  - `MVP-003` (Combat MVP short sortie and localized intervention)
  - `MVP-004` (Data-driven definitions and progression/resource boundary)
- `docs/product/game-overview.md` — non-normative experience overview
- `docs/dev/product-spec-lifecycle.md` — compact spec creation and lifecycle policy
- `docs/adr/0002-sdd-tool-adoption.md` — SDD policy and docs-ssot-wins boundary
