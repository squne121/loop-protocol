function fail(message, code = 2) {
  const error = new Error(message)
  error.exitCode = code
  throw error
}

export function parseArgs(argv, spec) {
  const result = {}
  const multiValueKeys = new Set(
    Object.entries(spec)
      .filter(([, config]) => config.multiple)
      .map(([key]) => key),
  )

  for (const key of multiValueKeys) {
    result[key] = []
  }

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index]
    if (!token.startsWith('--')) {
      fail(`unknown_argument:${token}`)
    }
    const name = token.slice(2)
    const config = spec[name]
    if (!config) {
      fail(`unknown_option:${token}`)
    }
    if (config.type === 'boolean') {
      result[name] = true
      continue
    }
    const value = argv[index + 1]
    if (value === undefined || value.startsWith('--')) {
      fail(`missing_value:${token}`)
    }
    index += 1
    if (config.multiple) {
      result[name].push(value)
    } else {
      result[name] = value
    }
  }

  for (const [name, config] of Object.entries(spec)) {
    if (config.required && (result[name] === undefined || result[name]?.length === 0)) {
      fail(`missing_required:${name}`)
    }
    if (result[name] === undefined && 'defaultValue' in config) {
      result[name] = config.defaultValue
    }
  }

  return result
}

export function ensureIsoTimestamp(value, fieldName) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)) {
    fail(`invalid_timestamp:${fieldName}`)
  }
  return value
}

export function ensureEnum(value, allowed, fieldName) {
  if (!allowed.includes(value)) {
    fail(`invalid_enum:${fieldName}`)
  }
  return value
}

export function ensureLength(value, maxLength, fieldName) {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength) {
    fail(`invalid_length:${fieldName}`)
  }
  return value
}
