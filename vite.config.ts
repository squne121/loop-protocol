import { defineConfig } from 'vite';

function normalizeBase(raw: string | undefined): string {
  if (!raw || raw.trim() === '') return '/';

  const value = raw.trim();
  if (!value.startsWith('/')) {
    throw new Error(`VITE_BASE_PATH must start with "/": ${value}`);
  }

  return value.endsWith('/') ? value : `${value}/`;
}

export default defineConfig({
  base: normalizeBase(process.env.VITE_BASE_PATH),
});
