/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Build-time commit SHA injected by the deploy workflow via `git rev-parse HEAD`.
   * When set, must be a 40-character lowercase hex string.
   * Absent (undefined) in local dev builds unless explicitly set.
   */
  readonly VITE_LOOP_COMMIT_SHA?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
