import { createHash } from 'crypto'
import { URL } from 'node:url'

/**
 * Normalize a URL by stripping common tracking parameters and extracting
 * GitHub fragment (e.g. #issuecomment-123) as a structured dedup key component.
 *
 * Blocker 6 fix: GitHub fragments (#issuecomment-...) are extracted as
 * `fragment_id` so that two refs differing only in fragment are treated as
 * distinct (e.g. different comments on the same issue).
 *
 * @param {string} url
 * @returns {{ normalizedUrl: string, fragmentId: string }}
 */
function canonicalizeUrl(url) {
  if (typeof url !== 'string' || !url.startsWith('http')) {
    return { normalizedUrl: url ?? '', fragmentId: '' }
  }
  try {
    const parsed = new URL(url)
    // Remove tracking params
    for (const param of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'ref', 'referrer']) {
      parsed.searchParams.delete(param)
    }
    // Extract fragment (e.g. #issuecomment-123, #discussion_r456)
    const fragmentId = parsed.hash ?? ''
    // Remove fragment from URL so normalized URL is fragment-free
    parsed.hash = ''
    return { normalizedUrl: parsed.toString(), fragmentId }
  } catch {
    return { normalizedUrl: url, fragmentId: '' }
  }
}

/**
 * Compute a short digest of a string for deduplication purposes.
 * @param {string} text
 * @returns {string}
 */
function shortDigest(text) {
  if (!text) return ''
  return createHash('sha256').update(String(text)).digest('hex').slice(0, 16)
}

/**
 * Build the dedupe key for an evidence ref.
 * Key components: kind + canonical_url_without_fragment + fragment_id + comment_id_or_artifact_id + body_digest_or_artifact_digest
 *
 * Blocker 6 fix: fragment_id is now included in the dedup key so that
 * GitHub fragment variants (e.g. #issuecomment-123 vs #issuecomment-456
 * on the same issue URL) are treated as distinct refs unless all components match.
 *
 * @param {object} ref
 * @returns {string}
 */
export function buildDedupeKey(ref) {
  const kind = String(ref.kind ?? '')
  const { normalizedUrl, fragmentId } = canonicalizeUrl(ref.ref ?? ref.workflow_run_url ?? '')
  const commentOrArtifactId = String(ref.comment_id ?? ref.artifact_id ?? '')
  const bodyOrArtifactDigest = String(ref.digest ?? ref.body_digest ?? ref.artifact_digest ?? '')

  return [kind, normalizedUrl, fragmentId, commentOrArtifactId, bodyOrArtifactDigest].join('\x00')
}

/**
 * Deduplicate evidence refs.
 * Returns an array where duplicates have `duplicate_of` set to the first occurrence's dedupeKey digest,
 * and all refs have `used_by_sections` and `canonical_key_digest` fields.
 *
 * Blocker 6 fix: duplicate refs now include canonical_key_digest for machine-readable
 * identification. The caller's renderer outputs these fields per AC5 contract.
 *
 * @param {object[]} refs
 * @returns {object[]}
 */
export function dedupeEvidenceRefs(refs) {
  const seen = new Map()
  const result = []

  for (const ref of refs) {
    const key = buildDedupeKey(ref)
    const keyDigest = shortDigest(key)

    if (seen.has(key)) {
      result.push({
        ...ref,
        used_by_sections: ref.used_by_sections ?? [],
        duplicate_of: seen.get(key),
        canonical_key_digest: keyDigest,
      })
    } else {
      seen.set(key, keyDigest)
      result.push({
        ...ref,
        used_by_sections: ref.used_by_sections ?? [],
        duplicate_of: null,
        canonical_key_digest: keyDigest,
      })
    }
  }

  return result
}
