/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Build-time commit SHA injected by the deploy workflow via `git rev-parse HEAD`.
   * When set, must be a 40-character lowercase hex string.
   * Absent (undefined) in local dev builds unless explicitly set.
   */
  readonly VITE_LOOP_COMMIT_SHA?: string
  readonly VITE_LOOP_RUN_ID?: string
  readonly VITE_LOOP_RUN_ATTEMPT?: string
  readonly VITE_LOOP_PAGE_URL?: string
  readonly VITE_LOOP_ARTIFACT_URL?: string
  readonly VITE_LOOP_ARTIFACT_NAMES?: string
  readonly VITE_LOOP_ARTIFACT_DIGEST_OR_ATTESTATION?: string
  readonly VITE_LOOP_RETENTION_DAYS?: string
  readonly VITE_LOOP_SCREENSHOT_PATH?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
