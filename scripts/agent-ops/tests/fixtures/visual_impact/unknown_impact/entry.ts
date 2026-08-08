// Fixture for AC7: constructs the resolver cannot fully solve MUST be
// reported as unknown_impact, never silently treated as "no impact".

// 1. import.meta.glob (including negative globs).
const modules = import.meta.glob(['./variants/*.ts', '!./variants/excluded.ts'])

// 2. variable / unbounded dynamic import.
async function loadVariant(name: string) {
  return import(`./variants/${name}.ts`)
}

// 3. dynamic new URL() (non-static first argument).
function assetUrl(name: string) {
  return new URL(name, import.meta.url)
}

// 4. virtual/generated module specifier.
import virtualData from 'virtual:fixture-generated-module'

export { modules, loadVariant, assetUrl, virtualData }
