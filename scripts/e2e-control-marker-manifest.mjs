export const e2eControlMarkers = Object.freeze([
  Object.freeze({ name: '__LOOP_E2E__', productionForbidden: true, requiredInE2E: true }),
  Object.freeze({ name: '__LOOP_E2E_BOOTSTRAP__', productionForbidden: true, requiredInE2E: true }),
  Object.freeze({ name: '__LOOP_VISUAL_SCENARIO__', productionForbidden: true, requiredInE2E: true }),
  Object.freeze({ name: '__LOOP_STORAGE_KEY__', productionForbidden: true, requiredInE2E: true }),
  Object.freeze({ name: '__E2E_SHORT_SORTIE__', productionForbidden: true, requiredInE2E: true }),
  Object.freeze({ name: '__E2E_PLAYER_HP_OVERRIDE__', productionForbidden: true, requiredInE2E: true }),
])

export function validateE2EControlMarkerManifest(manifest) {
  if (!Array.isArray(manifest) || manifest.length === 0) {
    throw new Error('E2E control marker manifest must be a non-empty array')
  }

  const names = new Set()
  for (const entry of manifest) {
    if (!entry || typeof entry.name !== 'string' || !entry.name.trim()) {
      throw new Error('E2E control marker manifest entries require a non-empty name')
    }
    if (names.has(entry.name)) {
      throw new Error(`E2E control marker manifest contains duplicate marker: ${entry.name}`)
    }
    if (entry.productionForbidden !== true || entry.requiredInE2E !== true) {
      throw new Error(`E2E control marker manifest requires both classifications: ${entry.name}`)
    }
    names.add(entry.name)
  }

  return manifest
}
