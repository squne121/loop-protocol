import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolve as resolvePath, dirname } from 'node:path'
import { parse as parseYaml } from 'yaml'
import { describe, expect, it } from 'vitest'

import {
  RETRO_LIVE_VERIFICATION_ALLOWED_PATHS,
  buildManifest,
  canonicalJsonStringify,
  computeCanonicalCommentBody,
  fromSha256Prefixed,
  normalizeTrustedActorAllowlist,
  sha256Hex,
  toSha256Prefixed,
  validateManifestWithAjv,
} from '../scripts/generate-retro-live-verification.mjs'
import {
  evaluatePostWriteUniqueness,
  isPaginationRejected as isPostPaginationRejected,
  planPost,
} from '../scripts/post-retro-live-verification.mjs'
import {
  checkCanonicalComment,
  checkCaptureToPostLag,
  checkRuntimeProvenanceComplete,
  evaluateContextAssertionsBinding,
  evaluatePrReviewSurfaceBinding,
  isPaginationRejected,
  verifyPrReviewBindingLive,
} from '../scripts/check-retro-live-verification.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIXTURES_DIR = resolvePath(__dirname, 'fixtures/retro-live-verification')
const REPO_ROOT = resolvePath(__dirname, '..')

function loadFixture(name: string) {
  return JSON.parse(readFileSync(resolvePath(FIXTURES_DIR, name), 'utf-8'))
}

const BASE_ARGS = {
  repo: 'squne121/loop-protocol',
  targetType: 'pull_request',
  targetNumber: '1415',
  parentIssue: '1153',
  markerCommentUrl: 'https://github.com/squne121/loop-protocol/issues/1153#issuecomment-1',
  expectedDigest: 'a'.repeat(64),
  expectedPayloadDigest: 'c'.repeat(64),
  expectedMatchedCommentCount: '3',
  reviewArtifactRef: 'https://github.com/squne121/loop-protocol/pull/1415#pullrequestreview-1',
  reviewedHeadSha: 'b'.repeat(40),
  selectedReviewId: '1',
  issueNumber: '1415',
  trustedActor: 'squne121,github-actions[bot]',
  out: '-',
}

describe('canonicalJsonStringify / sha256Hex', () => {
  it('GIVEN two objects with the same keys in different orders WHEN canonicalized THEN they serialize identically', () => {
    const a = { z: 1, a: { y: 2, x: 1 } }
    const b = { a: { x: 1, y: 2 }, z: 1 }
    expect(canonicalJsonStringify(a)).toBe(canonicalJsonStringify(b))
  })

  it('GIVEN an array WHEN canonicalized THEN element order is preserved', () => {
    expect(canonicalJsonStringify({ list: [3, 1, 2] })).toBe('{"list":[3,1,2]}')
  })

  it('GIVEN known input text WHEN hashed THEN the sha256 hex digest matches a fixed known value', () => {
    expect(sha256Hex('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
  })
})

describe('buildManifest / schema validation (AC1 docs/schemas/retro-live-verification.schema.json)', () => {
  it('GIVEN valid CLI-shaped inputs WHEN a manifest is built THEN it validates against retro_live_verification/v2', async () => {
    const manifest = buildManifest(BASE_ARGS)
    expect(manifest.schema).toBe('retro_live_verification/v2')
    const validation = await validateManifestWithAjv(manifest)
    expect(validation.valid).toBe(true)
    expect(validation.errors).toEqual([])
  })

  it('GIVEN the manifest allowed_paths WHEN compared to the Issue #1709 contract THEN they match exactly', () => {
    const manifest = buildManifest(BASE_ARGS)
    expect(manifest.execution_boundary.allowed_paths).toEqual([...RETRO_LIVE_VERIFICATION_ALLOWED_PATHS])
  })

  it('GIVEN an invalid target_type WHEN a manifest is built THEN it throws a usage error instead of silently coercing', () => {
    expect(() => buildManifest({ ...BASE_ARGS, targetType: 'not-a-real-type' })).toThrow()
  })

  it('GIVEN an expected_digest that is not a valid hex/prefixed digest WHEN a manifest is built THEN it throws', () => {
    expect(() => buildManifest({ ...BASE_ARGS, expectedDigest: 'not-hex' })).toThrow()
  })

  it('GIVEN a manifest missing a required top-level field WHEN validated against the schema THEN validation fails (never silently passes)', async () => {
    const manifest = buildManifest(BASE_ARGS)
    const withoutProfile = { ...manifest }
    delete (withoutProfile as Record<string, unknown>).canonicalization_profile
    const validation = await validateManifestWithAjv(withoutProfile)
    expect(validation.valid).toBe(false)
    expect(validation.errors.length).toBeGreaterThan(0)
  })

  it('GIVEN a bare expected_digest WHEN a manifest is built THEN it is normalized to the sha256:-prefixed representation (Issue #1709 PR review P0-4)', () => {
    const manifest = buildManifest(BASE_ARGS)
    expect(manifest.context_assertions.expected_digest).toBe(`sha256:${'a'.repeat(64)}`)
    expect(manifest.context_assertions.expected_payload_digest).toBe(`sha256:${'c'.repeat(64)}`)
  })
})

describe('normalizeTrustedActorAllowlist', () => {
  it('GIVEN a comma-separated list with duplicates WHEN normalized THEN it deduplicates and preserves valid GitHub logins', () => {
    expect(normalizeTrustedActorAllowlist('squne121, squne121,github-actions[bot]')).toEqual(['squne121', 'github-actions[bot]'])
  })

  it('GIVEN an empty allowlist WHEN normalized THEN it throws instead of defaulting to "trust everyone"', () => {
    expect(() => normalizeTrustedActorAllowlist('')).toThrow()
  })
})

describe('computeCanonicalCommentBody', () => {
  it('GIVEN the same assertions WHEN the canonical comment body is computed twice THEN the digest is stable', () => {
    const manifest = buildManifest(BASE_ARGS)
    const first = computeCanonicalCommentBody({
      contextAssertions: manifest.context_assertions,
      prReviewBinding: manifest.pr_review_binding,
      ownershipMarker: manifest.canonical_comment.ownership_marker,
    })
    const second = computeCanonicalCommentBody({
      contextAssertions: manifest.context_assertions,
      prReviewBinding: manifest.pr_review_binding,
      ownershipMarker: manifest.canonical_comment.ownership_marker,
    })
    expect(first.bodyDigest).toBe(second.bodyDigest)
    expect(first.bodyDigest).toBe(manifest.canonical_comment.body_digest)
  })
})

describe('required negative test 5: sha256 digest representation round trip (Issue #1709 PR review P0-4)', () => {
  const sample = loadFixture('digest-roundtrip-sample.json')

  it('GIVEN a bare 64-hex digest WHEN converted to the prefixed representation THEN it round trips through the existing chatgpt-retro-context:assert-live consumer format', () => {
    expect(toSha256Prefixed(sample.bare_hex)).toBe(sample.sha256_prefixed)
    expect(fromSha256Prefixed(sample.sha256_prefixed)).toBe(sample.bare_hex)
  })

  it('GIVEN an already-prefixed digest WHEN normalized again THEN it is idempotent', () => {
    expect(toSha256Prefixed(sample.sha256_prefixed)).toBe(sample.sha256_prefixed)
  })

  it('GIVEN a malformed digest string WHEN converted THEN both directions return null instead of a truncated/garbage value', () => {
    expect(toSha256Prefixed('not-a-digest')).toBeNull()
    expect(fromSha256Prefixed('not-a-digest')).toBeNull()
  })
})

describe('planPost (producer compare-and-swap / trusted actor / negative fixtures)', () => {
  const manifest = loadFixture('manifest.json')

  it('GIVEN no existing canonical comment and a null expected_previous_digest WHEN planned THEN the action is create', () => {
    const plan = planPost({ manifest, existingComments: [], currentActor: 'squne121' })
    expect(plan.ok).toBe(true)
    expect(plan.action).toBe('create')
  })

  it('GIVEN an actor outside the trusted_actor_allowlist WHEN planned THEN it is rejected before any comment inspection', () => {
    const plan = planPost({ manifest, existingComments: [], currentActor: 'some-random-fork-contributor' })
    expect(plan.ok).toBe(false)
    expect(plan.errorCode).toBe('retro_live_verification.untrusted_actor')
  })

  it('GIVEN a non-null expected_previous_digest but no existing comment WHEN planned THEN it is rejected (stale precondition)', () => {
    const manifestWithPreviousDigest = { ...manifest, canonical_comment: { ...manifest.canonical_comment, expected_previous_digest: 'd'.repeat(64) } }
    const plan = planPost({ manifest: manifestWithPreviousDigest, existingComments: [], currentActor: 'squne121' })
    expect(plan.ok).toBe(false)
    expect(plan.errorCode).toBe('retro_live_verification.expected_previous_digest_mismatch')
  })

  it('GIVEN an existing malformed canonical comment WHEN planned THEN posting is refused rather than overwritten', () => {
    const plan = planPost({ manifest, existingComments: loadFixture('malformed-digest-comments.json'), currentActor: 'squne121' })
    expect(plan.ok).toBe(false)
    expect(plan.errorCode).toBe('retro_live_verification.existing_comment_malformed')
  })

  it('GIVEN two existing comments matching the same ownership marker WHEN planned THEN it is rejected as a duplicate', () => {
    const manifestWithPreviousDigest = { ...manifest, canonical_comment: { ...manifest.canonical_comment, expected_previous_digest: manifest.canonical_comment.body_digest } }
    const plan = planPost({ manifest: manifestWithPreviousDigest, existingComments: loadFixture('duplicate-marker-comments.json'), currentActor: 'squne121' })
    expect(plan.ok).toBe(false)
    expect(plan.errorCode).toBe('retro_live_verification.duplicate_marker')
  })

  it('GIVEN an existing comment whose digest matches the manifest expectation WHEN planned THEN the action is noop', () => {
    const manifestWithPreviousDigest = { ...manifest, canonical_comment: { ...manifest.canonical_comment, expected_previous_digest: manifest.canonical_comment.body_digest } }
    const plan = planPost({ manifest: manifestWithPreviousDigest, existingComments: loadFixture('valid-comments.json'), currentActor: 'squne121' })
    expect(plan.ok).toBe(true)
    expect(plan.action).toBe('noop')
  })

  it('Issue #1415 AC4: GIVEN manifest WITH trusted_actor_id_allowlist WHEN --current-actor-id does not match THEN the trusted login alone is insufficient and posting is rejected', () => {
    const manifestWithIdAllowlist = { ...manifest, execution_boundary: { ...manifest.execution_boundary, trusted_actor_id_allowlist: [63350259] } }
    const plan = planPost({ manifest: manifestWithIdAllowlist, existingComments: [], currentActor: 'squne121', currentActorId: 111111 })
    expect(plan.ok).toBe(false)
    expect(plan.errorCode).toBe('retro_live_verification.untrusted_actor')
  })

  it('Issue #1415 AC4: GIVEN manifest WITH trusted_actor_id_allowlist WHEN both login and --current-actor-id match THEN posting proceeds', () => {
    const manifestWithIdAllowlist = { ...manifest, execution_boundary: { ...manifest.execution_boundary, trusted_actor_id_allowlist: [63350259] } }
    const plan = planPost({ manifest: manifestWithIdAllowlist, existingComments: [], currentActor: 'squne121', currentActorId: 63350259 })
    expect(plan.ok).toBe(true)
  })
})

describe('required negative test 7: concurrent-create post-write uniqueness (Issue #1709 PR review P1-1)', () => {
  const manifest = loadFixture('manifest.json')
  const ownershipMarker = manifest.canonical_comment.ownership_marker

  it('GIVEN two concurrent writers each successfully created their own comment WHEN the post-write re-list runs THEN neither run can pass (duplicate detected)', () => {
    const duplicates = loadFixture('duplicate-marker-comments.json')
    const firstWriterResult = evaluatePostWriteUniqueness({
      postWriteComments: duplicates,
      ownershipMarker,
      expectedCommentId: duplicates[0].id,
      expectedDigest: manifest.canonical_comment.body_digest,
    })
    const secondWriterResult = evaluatePostWriteUniqueness({
      postWriteComments: duplicates,
      ownershipMarker,
      expectedCommentId: duplicates[1].id,
      expectedDigest: manifest.canonical_comment.body_digest,
    })
    expect(firstWriterResult.ok).toBe(false)
    expect(firstWriterResult.errorCode).toBe('retro_live_verification.post_write_duplicate_detected')
    expect(secondWriterResult.ok).toBe(false)
    expect(secondWriterResult.errorCode).toBe('retro_live_verification.post_write_duplicate_detected')
  })

  it('GIVEN exactly one canonical comment matching this run\'s own comment id WHEN the post-write re-list runs THEN it passes', () => {
    const solo = loadFixture('valid-comments.json')
    const result = evaluatePostWriteUniqueness({
      postWriteComments: solo,
      ownershipMarker,
      expectedCommentId: solo[0].id,
      expectedDigest: manifest.canonical_comment.body_digest,
    })
    expect(result.ok).toBe(true)
  })
})

describe('required negative test 6: page_budget_exhausted must fail closed (Issue #1709 PR review P1-3)', () => {
  it('GIVEN page_budget_exhausted: true but pagination_exhausted: false WHEN checked THEN the verifier rejects the listing', () => {
    expect(isPaginationRejected({ pagination_exhausted: false, page_budget_exhausted: true })).toBe(true)
  })

  it('GIVEN pagination_exhausted: true but page_budget_exhausted: false WHEN checked THEN the verifier still rejects the listing', () => {
    expect(isPaginationRejected({ pagination_exhausted: true, page_budget_exhausted: false })).toBe(true)
  })

  it('GIVEN both flags false WHEN checked THEN the listing is accepted', () => {
    expect(isPaginationRejected({ pagination_exhausted: false, page_budget_exhausted: false })).toBe(false)
  })

  it('GIVEN the producer-side helper WHEN either flag is true THEN it also rejects (post-retro-live-verification.mjs, same fail-closed contract)', () => {
    expect(isPostPaginationRejected({ pagination_exhausted: false, page_budget_exhausted: true })).toBe(true)
    expect(isPostPaginationRejected({ pagination_exhausted: true, page_budget_exhausted: false })).toBe(true)
  })
})

describe('checkCanonicalComment (verifier / negative fixtures)', () => {
  const manifest = loadFixture('manifest.json')

  it('GIVEN a comment matching ownership marker, digest, trusted author, and a byte-exact recomputed payload WHEN checked THEN it passes', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('valid-comments.json') })
    expect(result.ok).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('GIVEN no comment matches the ownership marker WHEN checked THEN it fails with matched_comment_count_mismatch', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('no-match-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.matched_comment_count_mismatch')
  })

  it('GIVEN a malformed digest marker WHEN checked THEN it fails closed instead of treating it as absent', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('malformed-digest-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.malformed_canonical_comment')).toBe(true)
  })

  it('GIVEN a comment authored by a non-trusted actor WHEN checked THEN it fails with untrusted_marker_author', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('untrusted-author-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.untrusted_marker_author')).toBe(true)
  })

  it('GIVEN a comment with a stale (but well-formed) digest marker WHEN checked THEN it fails with stale_digest', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('stale-digest-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.stale_digest')).toBe(true)
  })

  it('GIVEN two comments matching the ownership marker WHEN checked THEN it fails with matched_comment_count_mismatch', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('duplicate-marker-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.matched_comment_count_mismatch')
  })

  it('required negative test 1: GIVEN the JSON payload is tampered but the digest marker line is preserved WHEN checked THEN the recomputed digest mismatch fails closed (Issue #1709 PR review P0-3)', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('tampered-payload-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.payload_digest_recompute_mismatch')).toBe(true)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.payload_context_assertions_mismatch')).toBe(true)
  })

  it('required negative test 2: GIVEN Markdown/instructions trail the closing JSON fence WHEN checked THEN it is rejected (Issue #1709 PR review P0-3)', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('trailing-content-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.payload_trailing_content')).toBe(true)
  })
})

describe('Issue #1415 AC4: marker authority numeric author id binding (additive, optional trusted_actor_id_allowlist)', () => {
  const manifest = loadFixture('manifest.json')
  const validComments = loadFixture('valid-comments.json')
  const trustedLogin = validComments[0].user.login
  const withAuthorId = (id: number | null) => [
    { ...validComments[0], user: { ...validComments[0].user, id } },
  ]

  it('GIVEN manifest WITHOUT trusted_actor_id_allowlist (v2 default) WHEN a comment has no numeric id THEN it still passes on login alone (backward compatible)', () => {
    const result = checkCanonicalComment({ manifest, comments: withAuthorId(null) })
    expect(result.ok).toBe(true)
  })

  it('GIVEN manifest WITH trusted_actor_id_allowlist WHEN the comment author login matches but the numeric id does NOT THEN it fails closed with untrusted_marker_author (a bare login match is insufficient once a numeric allowlist is declared)', () => {
    const manifestWithIdAllowlist = {
      ...manifest,
      execution_boundary: { ...manifest.execution_boundary, trusted_actor_id_allowlist: [999999] },
    }
    const result = checkCanonicalComment({ manifest: manifestWithIdAllowlist, comments: withAuthorId(63350259) })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.untrusted_marker_author')).toBe(true)
  })

  it('GIVEN manifest WITH trusted_actor_id_allowlist WHEN both login and numeric id match THEN it passes', () => {
    const manifestWithIdAllowlist = {
      ...manifest,
      execution_boundary: { ...manifest.execution_boundary, trusted_actor_id_allowlist: [63350259] },
    }
    const result = checkCanonicalComment({ manifest: manifestWithIdAllowlist, comments: withAuthorId(63350259) })
    expect(result.ok).toBe(true)
  })

  it('GIVEN two comments matching the ownership marker, one trusted and one authored by an untrusted login WHEN checked THEN the untrusted one is excluded from candidate/duplicate counting and the single trusted one is checked normally (not a false duplicate block)', () => {
    const untrustedDuplicate = { ...validComments[0], id: 900002, user: { login: 'not-trusted-actor' } }
    const result = checkCanonicalComment({ manifest, comments: [...validComments, untrustedDuplicate] })
    expect(result.ok).toBe(true)
    expect(trustedLogin).toBe('squne121')
  })

  it('GIVEN two trusted-author comments both matching the ownership marker WHEN checked THEN it still fails closed with matched_comment_count_mismatch (a trusted duplicate must never be treated as consensus)', () => {
    const trustedDuplicate = { ...validComments[0], id: 900003 }
    const result = checkCanonicalComment({ manifest, comments: [...validComments, trustedDuplicate] })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.matched_comment_count_mismatch')
  })
})

describe('evaluateContextAssertionsBinding (context_assertions delegation to existing assert-live, Issue #1709 PR review P0-4)', () => {
  it('GIVEN a resolved, matching assert-live result WHEN evaluated THEN it passes', () => {
    const spawnResult = { status: 0, stdout: JSON.stringify({ assertion_status: 'pass' }), stderr: '' }
    const result = evaluateContextAssertionsBinding(spawnResult)
    expect(result.ok).toBe(true)
  })

  it('GIVEN assert-live reports a non-"pass" assertion_status WHEN evaluated THEN it fails closed', () => {
    const spawnResult = { status: 1, stdout: JSON.stringify({ assertion_status: 'fail', errors: [{ code: 'x' }] }), stderr: '' }
    const result = evaluateContextAssertionsBinding(spawnResult)
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.context_assertions_live_binding_failed')
  })

  it('required negative test 3: GIVEN the subprocess produced no parsable JSON (as a null/malformed repository/pullRequest/reviewThreads GraphQL shape would) WHEN evaluated THEN it is rejected rather than defaulting to pass (Issue #1709 PR review P1-2)', () => {
    const spawnResult = { status: 2, stdout: '', stderr: 'boom' }
    const result = evaluateContextAssertionsBinding(spawnResult)
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.context_assertions_invalid_json_output')
  })

  it('GIVEN the subprocess failed to spawn WHEN evaluated THEN it is rejected rather than silently skipped', () => {
    const spawnResult = { error: new Error('ENOENT'), stdout: null, stderr: null }
    const result = evaluateContextAssertionsBinding(spawnResult)
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.context_assertions_spawn_failed')
  })
})

describe('verifyPrReviewBindingLive (pr_review_binding identity check, Issue #1709 PR review P0-4)', () => {
  const manifest = loadFixture('manifest.json')

  it('GIVEN a live review object matching reviewed_head_sha / selected_review_id / review_artifact_ref WHEN verified THEN it passes', () => {
    const review = loadFixture('pr-review-valid.json')
    const result = verifyPrReviewBindingLive({ prReviewBinding: manifest.pr_review_binding, review })
    expect(result.ok).toBe(true)
  })

  it('required negative test 4: GIVEN a live review whose commit_id / html_url do not match reviewed_head_sha / review_artifact_ref WHEN verified THEN it is rejected (Issue #1709 PR review P0-4)', () => {
    const review = loadFixture('pr-review-mismatch.json')
    const result = verifyPrReviewBindingLive({ prReviewBinding: manifest.pr_review_binding, review })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.pr_review_binding_head_sha_mismatch')).toBe(true)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.pr_review_binding_artifact_ref_mismatch')).toBe(true)
  })

  it('GIVEN a null/missing live review response (e.g. a 404 or malformed API shape) WHEN verified THEN it fails closed rather than defaulting to "nothing to check" (Issue #1709 PR review P1-2)', () => {
    const result = verifyPrReviewBindingLive({ prReviewBinding: manifest.pr_review_binding, review: null })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.pr_review_binding_missing')
  })

  it('GIVEN selected_review_id is null (no specific review bound) WHEN verified THEN the check is skipped rather than failing', () => {
    const noBinding = { ...manifest.pr_review_binding, selected_review_id: null }
    const result = verifyPrReviewBindingLive({ prReviewBinding: noBinding, review: null })
    expect(result.ok).toBe(true)
    expect(result.skipped).toBe(true)
  })
})

describe('required negative test 9: retro-live-verification.yml workflow contract (Issue #1709 PR review P0-1 / P0-2 / P2)', () => {
  const workflowPath = resolvePath(REPO_ROOT, '.github/workflows/retro-live-verification.yml')
  const workflowText = readFileSync(workflowPath, 'utf-8')
  const workflow = parseYaml(workflowText) as {
    jobs: Record<string, { env?: Record<string, unknown>; if?: unknown; steps: Array<Record<string, unknown>> }>
  }
  const postJob = workflow.jobs['post-canonical-comment']

  it('GIVEN the post-canonical-comment job WHEN parsed THEN GH_TOKEN is set at the job level (not omitted)', () => {
    expect(postJob.env).toBeDefined()
    expect(postJob.env?.GH_TOKEN).toBeTruthy()
  })

  it('GIVEN the verify step WHEN parsed THEN its condition is the Boolean-safe negation, not a broken string comparison', () => {
    const verifyStep = postJob.steps.find((step) => String(step.name) === 'Verify canonical comment landed correctly')
    expect(verifyStep).toBeDefined()
    const condition = String(verifyStep?.if ?? '')
    expect(condition).not.toContain("== 'false'")
    expect(condition.replace(/\s+/gu, '')).toContain('!inputs.dry_run')
  })

  it('GIVEN the post-canonical-comment job WHEN parsed THEN it is gated to the protected default branch', () => {
    const condition = String(postJob.if ?? '')
    expect(condition).toContain("github.ref == 'refs/heads/main'")
  })

  it('GIVEN every run: string in the post-canonical-comment job WHEN scanned THEN no workflow_dispatch input is interpolated directly inline (env indirection only)', () => {
    for (const step of postJob.steps) {
      const run = String(step.run ?? '')
      expect(run).not.toMatch(/\$\{\{\s*inputs\./u)
    }
  })

  it('GIVEN the checkout step in the post-canonical-comment job WHEN parsed THEN persist-credentials is explicitly false', () => {
    const checkoutStep = postJob.steps.find((step) => String(step.uses ?? '').startsWith('actions/checkout@'))
    expect(checkoutStep).toBeDefined()
    expect((checkoutStep?.with as Record<string, unknown> | undefined)?.['persist-credentials']).toBe(false)
  })

  it('GIVEN every `uses:` reference in the post-canonical-comment job WHEN parsed THEN it is pinned to a full commit SHA, not a mutable tag', () => {
    for (const step of postJob.steps) {
      const uses = step.uses
      if (typeof uses !== 'string') {
        continue
      }
      const [, ref] = uses.split('@')
      expect(ref).toMatch(/^[0-9a-f]{40}$/u)
    }
  })
})


describe('required negative test 3 (end-to-end subprocess): repository/pullRequest/reviewThreads null-shape resolve result must fail closed through the real check-retro-live-verification.mjs process (Issue #1709 PR review P1-2, #1718 REQUEST_CHANGES follow-up)', () => {
  const checkScriptPath = resolvePath(REPO_ROOT, 'scripts/check-retro-live-verification.mjs')
  const manifestPath = resolvePath(FIXTURES_DIR, 'manifest.json')
  const validCommentsPath = resolvePath(FIXTURES_DIR, 'valid-comments.json')
  const prReviewValidPath = resolvePath(FIXTURES_DIR, 'pr-review-valid.json')
  const resolveResultNullShapePath = resolvePath(FIXTURES_DIR, 'resolve-result-null-shape.json')
  const resolveResultValidPath = resolvePath(FIXTURES_DIR, 'resolve-result-valid.json')

  it('GIVEN the checked-in resolve-result-null-shape.json fixture (a resolved-but-null GraphQL repository/pullRequest/reviewThreads shape) WHEN check-retro-live-verification.mjs is actually spawned as a subprocess THEN the real process exits non-zero and reports verification_status "fail" via the context_assertions live-binding delegation, never a pass', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json', manifestPath,
        '--execution-profile', 'fixture',
        '--fixture-comments-json', validCommentsPath,
        '--fixture-resolve-result-json', resolveResultNullShapePath,
        '--fixture-pr-review-json', prReviewValidPath,
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(1)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('fail')
    expect(
      payload.errors.some((error: { code: string }) => error.code === 'retro_live_verification_check.context_assertions_live_binding_failed'),
    ).toBe(true)
  })

  it('GIVEN the checked-in resolve-result-valid.json fixture (matching the manifest\'s own context_assertions) WHEN check-retro-live-verification.mjs is actually spawned as a subprocess THEN the real process exits zero and reports verification_status "pass"', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json', manifestPath,
        '--execution-profile', 'fixture',
        '--fixture-comments-json', validCommentsPath,
        '--fixture-resolve-result-json', resolveResultValidPath,
        '--fixture-pr-review-json', prReviewValidPath,
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(0)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('pass')
    expect(payload.errors).toEqual([])
  })
})

describe('Issue #1415 AC2: context_assertions binding runs for issue targets too, not only pull_request targets', () => {
  const checkScriptPath = resolvePath(REPO_ROOT, 'scripts/check-retro-live-verification.mjs')
  const manifestPath = resolvePath(FIXTURES_DIR, 'gate-manifest.json')
  const commentsPath = resolvePath(FIXTURES_DIR, 'gate-comments.json')
  const resolveResultIssueValidPath = resolvePath(FIXTURES_DIR, 'gate-resolve-result-issue.json')
  const resolveResultIssueMismatchPath = resolvePath(FIXTURES_DIR, 'gate-resolve-result-issue-mismatch.json')

  it('GIVEN an issue-target manifest with a resolve-result fixture that MATCHES its context_assertions WHEN checked THEN the process passes', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json', manifestPath,
        '--execution-profile', 'fixture',
        '--fixture-comments-json', commentsPath,
        '--fixture-resolve-result-json', resolveResultIssueValidPath,
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(0)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('pass')
  })

  it('GIVEN an issue-target manifest with a resolve-result fixture whose matched_comment_count does NOT match its context_assertions WHEN checked THEN the process fails closed (regression guard: before the AC2 fix, this block was skipped entirely for target_type "issue" and always defaulted to ok:true)', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json', manifestPath,
        '--execution-profile', 'fixture',
        '--fixture-comments-json', commentsPath,
        '--fixture-resolve-result-json', resolveResultIssueMismatchPath,
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(1)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('fail')
    expect(
      payload.errors.some((error: { code: string }) => error.code === 'retro_live_verification_check.context_assertions_live_binding_failed'),
    ).toBe(true)
  })

  it('GIVEN an issue-target manifest in fixture mode WITHOUT --fixture-resolve-result-json WHEN checked THEN it is a usage error, never a silent skip', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json', manifestPath,
        '--execution-profile', 'fixture',
        '--fixture-comments-json', commentsPath,
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(2)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('error')
    expect(payload.error_code).toBe('retro_live_verification_check.fixture_resolve_result_json_required')
  })
})

describe('argument-free CLI wrapper fallback (Issue #1709 PR review P0-5, AC5/AC7)', () => {
  const generateScriptPath = resolvePath(REPO_ROOT, 'scripts/generate-retro-live-verification.mjs')
  const checkScriptPath = resolvePath(REPO_ROOT, 'scripts/check-retro-live-verification.mjs')

  it('GIVEN generate-retro-live-verification.mjs invoked with zero CLI args WHEN run THEN it succeeds against the checked-in fixture defaults instead of failing with a required-option usage error', () => {
    const result = spawnSync(process.execPath, [generateScriptPath], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
    })
    expect(result.status).toBe(0)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.status).toBe('ok')
    expect(payload.schema).toBe('retro_live_verification/v2')
  })

  it('GIVEN check-retro-live-verification.mjs invoked with zero CLI args WHEN run THEN it succeeds against the checked-in fixture defaults instead of failing with a required-option usage error', () => {
    const result = spawnSync(process.execPath, [checkScriptPath], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
    })
    expect(result.status).toBe(0)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('pass')
    expect(payload.execution_profile).toBe('fixture')
  })

  it('GIVEN check-retro-live-verification.mjs invoked with explicit CLI args WHEN run THEN the explicit args override the zero-arg fixture defaults', () => {
    const result = spawnSync(
      process.execPath,
      [
        checkScriptPath,
        '--manifest-json',
        resolvePath(FIXTURES_DIR, 'gate-manifest.json'),
        '--execution-profile',
        'fixture',
        '--fixture-comments-json',
        resolvePath(FIXTURES_DIR, 'stale-digest-comments.json'),
        // Issue #1415 AC2: context-assertions binding now runs unconditionally
        // (both issue and pull_request targets), so fixture mode always
        // requires a fixture-resolve-result-json even when this test's intent
        // is to exercise the *comment-check* failure path below.
        '--fixture-resolve-result-json',
        resolvePath(FIXTURES_DIR, 'gate-resolve-result-issue.json'),
      ],
      { cwd: REPO_ROOT, encoding: 'utf-8' },
    )
    expect(result.status).toBe(1)
    const payload = JSON.parse(result.stdout.trim())
    expect(payload.verification_status).toBe('fail')
    expect(payload.errors.some((error: { code: string }) => error.code === 'retro_live_verification_check.matched_comment_count_mismatch')).toBe(true)
  })
})

describe('Issue #1415 AC13: checkRuntimeProvenanceComplete', () => {
  const COMPLETE_PROVENANCE = {
    generated_at: '2026-07-25T06:00:00Z',
    generator_commit: '1bb6aa2f'.padEnd(40, '0'),
    generator_version: '1.0.0',
    node_version: 'v24.15.0',
    pnpm_version: '11.7.0',
    pnpm_lockfile_digest: 'sha256:' + 'a'.repeat(64),
    gh_cli_version: '2.63.0',
    github_api_version: null,
    workflow_run_identity: null,
  }

  it('passes when all required-non-null fields are present and non-null, and optional-null keys are present', () => {
    const result = checkRuntimeProvenanceComplete(COMPLETE_PROVENANCE)
    expect(result.ok).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('fails closed when a required-non-null field is missing entirely', () => {
    const rest: Record<string, unknown> = { ...COMPLETE_PROVENANCE }
    delete rest.node_version
    const result = checkRuntimeProvenanceComplete(rest)
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.runtime_provenance_missing_field' && error.message.includes('node_version'))).toBe(true)
  })

  it('fails closed when a required-non-null field is explicitly null', () => {
    const result = checkRuntimeProvenanceComplete({ ...COMPLETE_PROVENANCE, pnpm_version: null })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.message.includes('pnpm_version'))).toBe(true)
  })

  it('fails closed when an optional-null key (github_api_version) is absent rather than explicitly null', () => {
    const rest: Record<string, unknown> = { ...COMPLETE_PROVENANCE }
    delete rest.github_api_version
    const result = checkRuntimeProvenanceComplete(rest)
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.message.includes('github_api_version'))).toBe(true)
  })

  it('fails closed for an empty/undefined runtime_provenance object', () => {
    const result = checkRuntimeProvenanceComplete(undefined)
    expect(result.ok).toBe(false)
    expect(result.errors.length).toBeGreaterThanOrEqual(6)
  })
})

describe('Issue #1415 AC15: checkCaptureToPostLag', () => {
  it('passes when posted-at is within the max lag window', () => {
    const result = checkCaptureToPostLag({
      captureIso: '2026-07-25T06:00:00Z',
      postedIso: '2026-07-25T06:10:00Z',
      maxLagSeconds: 900,
    })
    expect(result.ok).toBe(true)
    expect(result.lagSeconds).toBe(600)
  })

  it('fails closed when the lag exceeds max-capture-to-post-lag-seconds', () => {
    const result = checkCaptureToPostLag({
      captureIso: '2026-07-25T06:00:00Z',
      postedIso: '2026-07-25T06:16:00Z',
      maxLagSeconds: 900,
    })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.capture_to_post_lag_exceeded')
  })

  it('fails closed when posted-at is earlier than capture (negative lag), even if within magnitude of max lag', () => {
    const result = checkCaptureToPostLag({
      captureIso: '2026-07-25T06:10:00Z',
      postedIso: '2026-07-25T06:00:00Z',
      maxLagSeconds: 900,
    })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.capture_to_post_lag_negative')
  })

  it('fails closed on an invalid capture timestamp', () => {
    const result = checkCaptureToPostLag({ captureIso: 'not-a-date', postedIso: '2026-07-25T06:00:00Z', maxLagSeconds: 900 })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.capture_timestamp_invalid')
  })

  it('fails closed on an invalid posted timestamp', () => {
    const result = checkCaptureToPostLag({ captureIso: '2026-07-25T06:00:00Z', postedIso: 'not-a-date', maxLagSeconds: 900 })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.posted_timestamp_invalid')
  })
})

describe('Issue #1415 AC5-7: evaluatePrReviewSurfaceBinding', () => {
  const HEAD_SHA = 'b'.repeat(40)
  const REVIEW_ID = 111
  const COMMENT_ID = 222
  const THREAD_NODE_ID = 'PRRT_thread1'

  function makeSurface(overrides = {}) {
    return {
      headRefOid: HEAD_SHA,
      reviews: [{ databaseId: REVIEW_ID, state: 'COMMENTED', commit: { oid: HEAD_SHA } }],
      reviewThreads: [
        {
          id: THREAD_NODE_ID,
          isResolved: true,
          comments: { nodes: [{ databaseId: COMMENT_ID, pullRequestReview: { databaseId: REVIEW_ID } }] },
        },
      ],
      ...overrides,
    }
  }

  function makeBinding(overrides = {}) {
    return {
      reviewed_head_sha: HEAD_SHA,
      selected_review_id: REVIEW_ID,
      selected_review_comment_id: COMMENT_ID,
      selected_review_thread_node_id: THREAD_NODE_ID,
      ...overrides,
    }
  }

  it('passes when review/comment/thread are mutually bound, resolved, and the head sha matches', () => {
    const result = evaluatePrReviewSurfaceBinding({ surface: makeSurface(), prReviewBinding: makeBinding() })
    expect(result.ok).toBe(true)
    expect(result.errors).toEqual([])
  })

  it('fails closed when there are zero reviews', () => {
    const result = evaluatePrReviewSurfaceBinding({ surface: makeSurface({ reviews: [] }), prReviewBinding: makeBinding({ selected_review_id: null, selected_review_comment_id: null, selected_review_thread_node_id: null }) })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_zero_reviews')).toBe(true)
  })

  it('fails closed when there are zero review comments', () => {
    const surface = makeSurface({ reviewThreads: [{ id: THREAD_NODE_ID, isResolved: true, comments: { nodes: [] } }] })
    const result = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: makeBinding({ selected_review_comment_id: null, selected_review_thread_node_id: null }) })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_zero_review_comments')).toBe(true)
  })

  it('fails closed when there are zero resolved threads', () => {
    const surface = makeSurface({ reviewThreads: [{ id: THREAD_NODE_ID, isResolved: false, comments: { nodes: [{ databaseId: COMMENT_ID, pullRequestReview: { databaseId: REVIEW_ID } }] } }] })
    const result = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: makeBinding() })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_zero_resolved_threads')).toBe(true)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_thread_unresolved')).toBe(true)
  })

  it('fails closed when selected_review_id does not exist in the live surface', () => {
    const result = evaluatePrReviewSurfaceBinding({ surface: makeSurface(), prReviewBinding: makeBinding({ selected_review_id: 999 }) })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_selected_review_missing')).toBe(true)
  })

  it('fails closed on stale PR head: live headRefOid diverges from manifest reviewed_head_sha', () => {
    const result = evaluatePrReviewSurfaceBinding({ surface: makeSurface({ headRefOid: 'c'.repeat(40) }), prReviewBinding: makeBinding() })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_stale_pr_head')).toBe(true)
  })

  it('fails closed when the selected review comment does not belong to the selected review', () => {
    const surface = makeSurface({
      reviewThreads: [
        {
          id: THREAD_NODE_ID,
          isResolved: true,
          comments: { nodes: [{ databaseId: COMMENT_ID, pullRequestReview: { databaseId: 999 } }] },
        },
      ],
    })
    const result = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: makeBinding() })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_comment_review_mismatch')).toBe(true)
  })

  it('fails closed when the selected thread is unresolved', () => {
    const surface = makeSurface({
      reviewThreads: [
        {
          id: THREAD_NODE_ID,
          isResolved: false,
          comments: { nodes: [{ databaseId: COMMENT_ID, pullRequestReview: { databaseId: REVIEW_ID } }] },
        },
      ],
    })
    const result = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: makeBinding() })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_thread_unresolved')).toBe(true)
  })

  it('fails closed when the selected comment is not a member of the selected thread', () => {
    const surface = makeSurface({
      reviewThreads: [
        {
          id: THREAD_NODE_ID,
          isResolved: true,
          comments: { nodes: [{ databaseId: 333, pullRequestReview: { databaseId: REVIEW_ID } }] },
        },
        {
          id: 'PRRT_other_thread',
          isResolved: true,
          comments: { nodes: [{ databaseId: COMMENT_ID, pullRequestReview: { databaseId: REVIEW_ID } }] },
        },
      ],
    })
    const result = evaluatePrReviewSurfaceBinding({ surface, prReviewBinding: makeBinding() })
    expect(result.ok).toBe(false)
    expect(result.errors.some((error) => error.code === 'retro_live_verification_check.pr_review_surface_comment_not_in_thread')).toBe(true)
  })

  it('skips the selected-id cross-checks (but still enforces the >=1 minimums) when selected_review_id is null', () => {
    const result = evaluatePrReviewSurfaceBinding({ surface: makeSurface(), prReviewBinding: makeBinding({ selected_review_id: null, selected_review_comment_id: null, selected_review_thread_node_id: null }) })
    expect(result.ok).toBe(true)
  })
})
