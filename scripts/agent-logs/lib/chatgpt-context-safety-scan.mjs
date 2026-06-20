import { runtimeError } from './args.mjs'

/**
 * Patterns that must not appear in rendered Markdown output.
 * These represent prompt injection attempts.
 */
const INJECTION_PATTERNS = [
  // Markdown-escaped command injection
  /\bignore previous instructions\b/i,
  /\bforget all previous\b/i,
  /\bact as\b.*\bsystem\b/i,
  // HTML-style injections
  /<script[\s>]/i,
  /<iframe[\s>]/i,
  // YAML front matter injection
  /^---\s*$/m,
  // ChatGPT system-level directives
  /\[\[(?:SYSTEM|USER|ASSISTANT)\]\]/i,
  // Null byte
  /\0/,
]

/**
 * Patterns that represent leakage of transcript or local data.
 * These are the same as FORBIDDEN_FIELDS but checked in rendered text.
 */
const RENDERED_LEAKAGE_PATTERNS = [
  /"raw_transcript"\s*:/,
  /"transcript_excerpt"\s*:/,
  /"full_command_output"\s*:/,
  /"stdout"\s*:/,
  /"stderr"\s*:/,
  /"local_path"\s*:/,
]

/**
 * Scan rendered Markdown for injection patterns and leakage.
 * @param {string} renderedMarkdown
 * @throws {CliError} if any injection or leakage is detected
 */
export function scanRenderedMarkdown(renderedMarkdown) {
  const violations = []

  for (const pattern of INJECTION_PATTERNS) {
    if (pattern.test(renderedMarkdown)) {
      violations.push(`injection_pattern: ${pattern.source}`)
    }
  }

  for (const pattern of RENDERED_LEAKAGE_PATTERNS) {
    if (pattern.test(renderedMarkdown)) {
      violations.push(`leakage_pattern: ${pattern.source}`)
    }
  }

  if (violations.length > 0) {
    throw runtimeError(
      'safety.injection_detected',
      `rendered markdown failed safety scan: ${violations.join('; ')}`
    )
  }
}

/**
 * Escape external-origin text for safe inclusion as a DATA block.
 * Wraps content in a fenced code block labeled DATA.
 * @param {string} text
 * @returns {string}
 */
export function wrapAsDataBlock(text) {
  // Ensure no fence sequences inside the content can escape the block
  // by replacing any sequence of 3+ backticks with escaped version
  const escaped = text.replace(/`{3,}/g, (m) => m.replace(/`/g, '&#96;'))
  return `\`\`\`DATA\n${escaped}\n\`\`\``
}

/**
 * Escape external-origin text as a blockquote.
 * @param {string} text
 * @returns {string}
 */
export function wrapAsQuote(text) {
  return text
    .split('\n')
    .map((line) => `> ${line}`)
    .join('\n')
}
