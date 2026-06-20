import { createHash } from 'crypto'

/**
 * Normalize a URL by stripping common tracking parameters.
 * @param {string} url
 * @returns {string}
 */
function canonicalizeUrl(url) {
  if (typeof url !== 'string' || !url.startsWith('http')) {
    return url ?? ''
  }
  try {
    const parsed = new URL(url)
    // Remove tracking params
    for (const param of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'ref', 'referrer']) {
      parsed.searchParams.delete(param)
    }
    return parsed.toString()
  } catch {
    return url
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
 * Key components: kind + canonical_url_without_tracking + comment_id_or_artifact_id + body_digest_or_artifact_digest
 *
 * @param {object} ref
 * @returns {string}
 */
export function buildDedupeKey(ref) {
  const kind = String(ref.kind ?? '')
  const canonicalUrl = canonicalizeUrl(ref.ref ?? ref.workflow_run_url ?? '')
  const commentOrArtifactId = String(ref.comment_id ?? ref.artifact_id ?? '')
  const bodyOrArtifactDigest = String(ref.digest ?? ref.body_digest ?? ref.artifact_digest ?? '')

  return [kind, canonicalUrl, commentOrArtifactId, bodyOrArtifactDigest].join('\x00')
}

/**
 * Deduplicate evidence refs.
 * Returns an array where duplicates have `duplicate_of` set to the first occurrence's dedupeKey,
 * and all refs have `used_by_sections` field.
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
      })
    } else {
      seen.set(key, keyDigest)
      result.push({
        ...ref,
        used_by_sections: ref.used_by_sections ?? [],
        duplicate_of: null,
      })
    }
  }

  return result
}
