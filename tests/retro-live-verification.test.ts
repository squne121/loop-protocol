import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve as resolvePath, dirname } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  RETRO_LIVE_VERIFICATION_ALLOWED_PATHS,
  buildManifest,
  canonicalJsonStringify,
  computeCanonicalCommentBody,
  normalizeTrustedActorAllowlist,
  sha256Hex,
  validateManifestWithAjv,
} from '../scripts/generate-retro-live-verification.mjs'
import { planPost } from '../scripts/post-retro-live-verification.mjs'
import {
  checkCanonicalComment,
  checkReviewThreadPagination,
} from '../scripts/check-retro-live-verification.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIXTURES_DIR = resolvePath(__dirname, 'fixtures/retro-live-verification')

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

  it('GIVEN an expected_digest that is not 64 lowercase hex characters WHEN a manifest is built THEN it throws', () => {
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
})

describe('checkCanonicalComment (verifier / negative fixtures)', () => {
  const manifest = loadFixture('manifest.json')

  it('GIVEN a comment matching ownership marker, digest, and a trusted author WHEN checked THEN it passes', () => {
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

  it('GIVEN a comment with a stale digest WHEN checked THEN it fails with stale_digest', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('stale-digest-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors.some((e: { code: string }) => e.code === 'retro_live_verification_check.stale_digest')).toBe(true)
  })

  it('GIVEN two comments matching the ownership marker WHEN checked THEN it fails with matched_comment_count_mismatch', () => {
    const result = checkCanonicalComment({ manifest, comments: loadFixture('duplicate-marker-comments.json') })
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.matched_comment_count_mismatch')
  })
})

describe('checkReviewThreadPagination (GraphQL errors / pagination completeness, negative fixtures)', () => {
  it('GIVEN pages that reach hasNextPage:false with no errors WHEN checked THEN it passes', () => {
    const result = checkReviewThreadPagination(loadFixture('review-pages-valid.json'))
    expect(result.ok).toBe(true)
    expect(result.totalThreadCount).toBe(3)
  })

  it('GIVEN a page carrying a non-empty GraphQL errors array WHEN checked THEN it fails closed even though the HTTP-level call succeeded', () => {
    const result = checkReviewThreadPagination(loadFixture('review-pages-graphql-errors.json'))
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.graphql_errors_present')
  })

  it('GIVEN pages that never reach hasNextPage:false WHEN checked THEN it fails with pagination_exhausted', () => {
    const result = checkReviewThreadPagination(loadFixture('review-pages-pagination-incomplete.json'))
    expect(result.ok).toBe(false)
    expect(result.errors[0].code).toBe('retro_live_verification_check.pagination_exhausted')
  })
})
