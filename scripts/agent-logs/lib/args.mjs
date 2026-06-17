export class CliError extends Error {
  constructor(code, message, exitCode = 1) {
    super(message)
    this.name = 'CliError'
    this.code = code
    this.exitCode = exitCode
  }
}

export function usageError(code, message) {
  return new CliError(code, message, 2)
}

export function runtimeError(code, message) {
  return new CliError(code, message, 1)
}

export function parseArgs(argv, optionSpec) {
  const options = {}

  for (const spec of Object.values(optionSpec)) {
    if (spec.multiple) {
      options[spec.key] = []
    } else if ('defaultValue' in spec) {
      options[spec.key] = spec.defaultValue
    }
  }

  let positionalMode = false

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--') {
      positionalMode = true
      continue
    }

    const [flag, inlineValue] = positionalMode ? [arg, undefined] : arg.split(/=(.*)/s, 2)
    const spec = optionSpec[flag]
    if (!spec) {
      throw usageError('cli.unknown_option', `unknown option: ${flag}`)
    }

    const value = inlineValue !== undefined ? inlineValue : argv[index + 1]
    if (typeof value !== 'string') {
      throw usageError('cli.missing_value', `missing value for option: ${flag}`)
    }
    if (inlineValue === undefined) {
      index += 1
    }
    if (spec.multiple) {
      options[spec.key].push(value)
    } else {
      options[spec.key] = value
    }
  }

  for (const [flag, spec] of Object.entries(optionSpec)) {
    const value = options[spec.key]
    if (spec.required && (value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0))) {
      throw usageError('cli.required_option', `missing required option: ${flag}`)
    }
  }

  return options
}

export function assertEnum(value, allowedValues, code, message) {
  if (!allowedValues.includes(value)) {
    throw runtimeError(code, message)
  }
  return value
}

export function assertNonEmptyString(value, code, message, { maxLength = null } = {}) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw runtimeError(code, message)
  }
  if (maxLength !== null && value.length > maxLength) {
    throw runtimeError(code, message)
  }
  return value
}

export function assertPositiveIntegerString(value, code, message) {
  if (!/^[1-9][0-9]*$/.test(value)) {
    throw runtimeError(code, message)
  }
  return Number(value)
}

export function assertIsoTimestamp(value, code, message) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime()) || date.toISOString() !== value) {
    throw runtimeError(code, message)
  }
  return value
}

export function assertIntegerString(value, code, message) {
  if (!/^(0|[1-9][0-9]*)$/.test(value)) {
    throw runtimeError(code, message)
  }
  return Number(value)
}

export function printCliError(prefix, error) {
  if (error instanceof CliError) {
    console.error(`${prefix}: ${error.code}: ${error.message}`)
    return error.exitCode
  }
  console.error(`${prefix}: cli.unexpected_error: unexpected runtime failure`)
  return 1
}
