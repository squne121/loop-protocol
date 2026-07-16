#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <stdio.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define LEDGER "subagent-launch-ledger.json"
#define LOCK "subagent-launch-ledger.json.lock"
#define TEMP "subagent-launch-ledger.json.tmp"
#define MAX_LEDGER (1024 * 1024)

static int cleanup_parent = -1;
static int lock_owned = 0;
static int temp_owned = 0;

static void cleanup_owned_paths(void) {
  if (cleanup_parent < 0) return;
  if (temp_owned) {
    (void)unlinkat(cleanup_parent, TEMP, 0);
    temp_owned = 0;
  }
  if (lock_owned) {
    (void)unlinkat(cleanup_parent, LOCK, 0);
    lock_owned = 0;
  }
}

static void fail(const char *reason) {
  cleanup_owned_paths();
  fprintf(stderr, "%s\n", reason);
  exit(2);
}

static int open_directory_at(int parent, const char *name) {
  struct open_how how = {.flags = O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW, .resolve = RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH};
  int fd = (int)syscall(SYS_openat2, parent, name, &how, sizeof(how));
  if (fd >= 0 || errno != ENOSYS) return fd;
  return openat(parent, name, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
}

static int open_parent(const char *repo) {
  int root = open(repo, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (root < 0) fail("ledger_repo_root_unsafe");
  if (mkdirat(root, "artifacts", 0700) < 0 && errno != EEXIST) fail("ledger_parent_create_failed");
  int artifacts = open_directory_at(root, "artifacts"); close(root);
  if (artifacts < 0) fail("ledger_parent_unsafe");
  if (mkdirat(artifacts, "codex", 0700) < 0 && errno != EEXIST) fail("ledger_parent_create_failed");
  int parent = open_directory_at(artifacts, "codex"); close(artifacts);
  if (parent < 0) fail("ledger_parent_unsafe");
  return parent;
}

static void acquire_lock(int parent) {
  for (int i = 0; i < 80; i++) {
    int fd = openat(parent, LOCK, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd >= 0) {
      close(fd);
      cleanup_parent = parent;
      lock_owned = 1;
      return;
    }
    if (errno != EEXIST) fail("ledger_lock_create_failed");
    struct stat st;
    if (fstatat(parent, LOCK, &st, AT_SYMLINK_NOFOLLOW) < 0) continue;
    if (!S_ISREG(st.st_mode)) fail("ledger_lock_path_unsafe");
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 25000000}; nanosleep(&pause, NULL);
  }
  fail("ledger_lock_timeout");
}

static void release_lock(int parent) {
  if (!lock_owned || cleanup_parent != parent) fail("ledger_lock_release_failed");
  if (unlinkat(parent, LOCK, 0) < 0) fail("ledger_lock_release_failed");
  lock_owned = 0;
}

static const char *skip_space_const(const char *p) { while (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t') p++; return p; }

/* This is deliberately a small JSON parser, not a substring check.  The
 * writer must never rewrite a ledger unless the persisted document has the
 * canonical common schema accepted by check_subagent_launch_ledger.py. */
static const char *skip_json_string(const char *p) {
  if (*p++ != '"') return NULL;
  for (; *p; p++) {
    unsigned char c = (unsigned char)*p;
    if (c < 0x20) return NULL;
    if (c == '"') return p + 1;
    if (c != '\\') continue;
    c = (unsigned char)*++p;
    if (!c || !strchr("\"\\/bfnrt", c)) {
      if (c != 'u') return NULL;
      for (int i = 0; i < 4; i++) {
        c = (unsigned char)*++p;
        if (!isxdigit(c)) return NULL;
      }
    }
  }
  return NULL;
}

static const char *skip_json_value(const char *p);

static const char *skip_json_compound(const char *p, char close) {
  p = skip_space_const(p + 1);
  if (*p == close) return p + 1;
  for (;;) {
    if (close == '}') {
      p = skip_json_string(p);
      if (!p) return NULL;
      p = skip_space_const(p);
      if (*p++ != ':') return NULL;
    }
    p = skip_json_value(skip_space_const(p));
    if (!p) return NULL;
    p = skip_space_const(p);
    if (*p == close) return p + 1;
    if (*p++ != ',') return NULL;
    p = skip_space_const(p);
  }
}

static const char *skip_json_value(const char *p) {
  p = skip_space_const(p);
  if (*p == '"') return skip_json_string(p);
  if (*p == '{') return skip_json_compound(p, '}');
  if (*p == '[') return skip_json_compound(p, ']');
  if (!strncmp(p, "true", 4)) return p + 4;
  if (!strncmp(p, "false", 5)) return p + 5;
  if (!strncmp(p, "null", 4)) return p + 4;
  const char *start = p;
  if (*p == '-') p++;
  if (*p == '0') p++;
  else if (*p >= '1' && *p <= '9') while (*++p >= '0' && *p <= '9') {}
  else return NULL;
  if (*p == '.') { if (*++p < '0' || *p > '9') return NULL; while (*++p >= '0' && *p <= '9') {} }
  if (*p == 'e' || *p == 'E') { p++; if (*p == '+' || *p == '-') p++; if (*p < '0' || *p > '9') return NULL; while (*++p >= '0' && *p <= '9') {} }
  return p == start ? NULL : p;
}

static int json_string_equals(const char *start, const char *end, const char *expected) {
  size_t length = strlen(expected);
  return end - start == (ptrdiff_t)length + 2 && !memcmp(start + 1, expected, length);
}

static int json_value_is(const char *start, const char *end, const char *expected) {
  return end - start == (ptrdiff_t)strlen(expected) && !memcmp(start, expected, strlen(expected));
}

static int json_string_contains(const char *start, const char *end, const char *needle) {
  size_t length = strlen(needle);
  if (end - start < (ptrdiff_t)length + 2) return 0;
  for (const char *p = start + 1; p + length < end; p++) {
    if (!memcmp(p, needle, length)) return 1;
  }
  return 0;
}

static int json_string_nonempty(const char *start, const char *end) {
  return *start == '"' && end - start > 2;
}

static int json_boolean(const char *start, const char *end) {
  return json_value_is(start, end, "true") || json_value_is(start, end, "false");
}

static int declared_runtime_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end || !json_string_nonempty(value, value_end)) return 0;
    if (json_string_equals(key, key_end, "model")) {
      if (fields & 1) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "reasoning_effort")) {
      if (fields & 2) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "default_permissions")) {
      if (fields & 4) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "agent_definition_sha256")) {
      if (fields & 8) return 0;
      fields |= 8;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return (fields == 7 || fields == 15) && p + 1 == end;
}

static int observed_dispatch_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end || !json_string_nonempty(value, value_end)) return 0;
    if (json_string_equals(key, key_end, "model")) {
      if (fields & 1) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "session_id")) {
      if (fields & 2) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "turn_id")) {
      if (fields & 4) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "agent_id")) {
      if (fields & 8) return 0;
      fields |= 8;
    } else if (json_string_equals(key, key_end, "observed_at")) {
      if (fields & 16) return 0;
      fields |= 16;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 31 && p + 1 == end;
}

static int correlation_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end) return 0;
    if (json_string_equals(key, key_end, "evidence_run_id") || json_string_equals(key, key_end, "repo_head_sha")) {
      unsigned bit = json_string_equals(key, key_end, "evidence_run_id") ? 1 : 2;
      if ((fields & bit) || !json_string_nonempty(value, value_end)) return 0;
      fields |= bit;
    } else if (json_string_equals(key, key_end, "worktree_dirty")) {
      if ((fields & 4) || !json_boolean(value, value_end)) return 0;
      fields |= 4;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 7 && p + 1 == end;
}

static int launch_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end) return 0;
    if (json_string_equals(key, key_end, "agent_name")) {
      if ((fields & 1) || !json_string_nonempty(value, value_end)) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "event_type")) {
      if ((fields & 2) || !json_string_equals(value, value_end, "SubagentStart")) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "evidence_source")) {
      if ((fields & 4) || !json_string_equals(value, value_end, "event_derived")) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "event_fingerprint")) {
      if ((fields & 8) || !json_string_nonempty(value, value_end)) return 0;
      fields |= 8;
    } else if (json_string_equals(key, key_end, "declared_runtime")) {
      if ((fields & 16) || !declared_runtime_valid(value, value_end)) return 0;
      fields |= 16;
    } else if (json_string_equals(key, key_end, "observed_dispatch")) {
      if ((fields & 32) || !observed_dispatch_valid(value, value_end)) return 0;
      fields |= 32;
    } else if (json_string_equals(key, key_end, "correlation")) {
      if ((fields & 64) || !correlation_valid(value, value_end)) return 0;
      fields |= 64;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 127 && p + 1 == end;
}

static int launches_valid(const char *start, const char *end) {
  const char *p = skip_space_const(start);
  if (*p++ != '[') return 0;
  p = skip_space_const(p);
  while (*p != ']') {
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end || !launch_valid(value, value_end)) return 0;
    p = skip_space_const(value_end);
    if (*p == ']') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return p + 1 == end;
}

static int root_thread_action_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end) return 0;
    if (json_string_equals(key, key_end, "kind")) {
      /* The writer enforces the structural schema only.  Whether a root action
       * is policy-permitted is decided by the canonical Python audit. */
      if ((fields & 1) || !json_string_nonempty(value, value_end)) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "command")) {
      if ((fields & 2) || !json_string_nonempty(value, value_end)) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "tool_name")) {
      if ((fields & 4) || !(json_string_equals(value, value_end, "Bash") || json_string_equals(value, value_end, "apply_patch") || json_string_equals(value, value_end, "Edit") || json_string_equals(value, value_end, "Write"))) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "coverage_source")) {
      if ((fields & 8) || !json_string_equals(value, value_end, "supported_pretooluse_path")) return 0;
      fields |= 8;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 15 && p + 1 == end;
}

static int root_thread_actions_valid(const char *start, const char *end) {
  const char *p = skip_space_const(start);
  if (*p++ != '[') return 0;
  p = skip_space_const(p);
  while (*p != ']') {
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end || !root_thread_action_valid(value, value_end)) return 0;
    p = skip_space_const(value_end);
    if (*p == ']') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return p + 1 == end;
}

static int coverage_scope_valid(const char *start, const char *end) {
  unsigned fields = 0;
  const char *p = skip_space_const(start);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end) return 0;
    if (json_string_equals(key, key_end, "subagent_start_event_recorded")) {
      if (fields & 1 || !json_value_is(value, value_end, "true")) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "unsupported_paths_fail_closed")) {
      if (fields & 2 || !json_value_is(value, value_end, "true")) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "scope_note")) {
      if (fields & 4 || *value != '"' || !json_string_contains(value, value_end, "supported PreToolUse paths")) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "supported_pretooluse_paths")) {
      unsigned supported = 0;
      const char *item = skip_space_const(value);
      if (*item++ != '[') return 0;
      item = skip_space_const(item);
      while (*item != ']') {
        const char *item_end = skip_json_string(item);
        if (!item_end) return 0;
        if (json_string_equals(item, item_end, "Bash")) supported |= 1;
        else if (json_string_equals(item, item_end, "apply_patch")) supported |= 2;
        else if (json_string_equals(item, item_end, "Edit")) supported |= 4;
        else if (json_string_equals(item, item_end, "Write")) supported |= 8;
        else return 0;
        item = skip_space_const(item_end);
        if (*item == ']') break;
        if (*item++ != ',') return 0;
        item = skip_space_const(item);
      }
      if (fields & 8 || supported != 15 || item + 1 != value_end) return 0;
      fields |= 8;
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 15 && p + 1 == end;
}

static int ledger_common_schema_valid(const char *text) {
  unsigned fields = 0;
  const char *p = skip_space_const(text);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (*p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *value = p, *value_end = skip_json_value(p);
    if (!value_end) return 0;
    if (json_string_equals(key, key_end, "ledger_schema")) {
      if (fields & 1 || !json_string_equals(value, value_end, "SUBAGENT_LAUNCH_LEDGER_V1")) return 0;
      fields |= 1;
    } else if (json_string_equals(key, key_end, "generated_by")) {
      if (fields & 2 || !json_string_equals(value, value_end, "codex_hook_pipeline")) return 0;
      fields |= 2;
    } else if (json_string_equals(key, key_end, "coverage_scope")) {
      if (fields & 4 || !coverage_scope_valid(value, value_end)) return 0;
      fields |= 4;
    } else if (json_string_equals(key, key_end, "launches")) {
      if (fields & 8 || !launches_valid(value, value_end)) return 0;
      fields |= 8;
    } else if (json_string_equals(key, key_end, "root_thread_actions")) {
      if (fields & 16 || !root_thread_actions_valid(value, value_end)) return 0;
      fields |= 16;
    } else if (json_string_equals(key, key_end, "codex_binary_status") ||
               json_string_equals(key, key_end, "generated_at") ||
               json_string_equals(key, key_end, "ledger_path") ||
               json_string_equals(key, key_end, "tool_path_support")) {
      /* Optional common-schema fields are type-checked by the canonical Python validator. */
    } else return 0;
    p = skip_space_const(value_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return fields == 31 && !*skip_space_const(p + 1);
}

static char *read_ledger(int parent) {
  struct stat before;
  if (fstatat(parent, LEDGER, &before, AT_SYMLINK_NOFOLLOW) < 0) {
    if (errno != ENOENT) fail("ledger_target_unsafe");
    const char *empty = "{\"ledger_schema\":\"SUBAGENT_LAUNCH_LEDGER_V1\",\"generated_by\":\"codex_hook_pipeline\",\"generated_at\":\"writer-generated\",\"ledger_path\":\"artifacts/codex/subagent-launch-ledger.json\",\"codex_binary_status\":\"available\",\"coverage_scope\":{\"subagent_start_event_recorded\":true,\"supported_pretooluse_paths\":[\"Bash\",\"apply_patch\",\"Edit\",\"Write\"],\"unsupported_paths_fail_closed\":true,\"scope_note\":\"This ledger records event-derived SubagentStart launches and supported PreToolUse paths only.\"},\"launches\":[],\"root_thread_actions\":[]}";
    return strdup(empty);
  }
  if (!S_ISREG(before.st_mode) || before.st_size < 2 || before.st_size > MAX_LEDGER) fail("ledger_target_unsafe");
  int fd = openat(parent, LEDGER, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) fail("ledger_target_unsafe");
  struct stat after;
  if (fstat(fd, &after) < 0 || !S_ISREG(after.st_mode) || after.st_dev != before.st_dev || after.st_ino != before.st_ino ||
      after.st_size < 2 || after.st_size > MAX_LEDGER) {
    close(fd);
    fail("ledger_target_unsafe");
  }
  char *buf = calloc((size_t)after.st_size + 1, 1); if (!buf) { close(fd); fail("ledger_memory_failed"); }
  size_t offset = 0;
  while (offset < (size_t)after.st_size) {
    ssize_t got = read(fd, buf + offset, (size_t)after.st_size - offset);
    if (got <= 0) { close(fd); free(buf); fail("ledger_read_failed"); }
    offset += (size_t)got;
  }
  close(fd);
  if (!ledger_common_schema_valid(buf)) { free(buf); fail("ledger_parse_or_schema_invalid"); }
  return buf;
}

static char *skip_space(char *p) { while (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t') p++; return p; }
static char *array_end(char *start) {
  int depth = 0, quote = 0, escaped = 0;
  for (char *p = start; *p; p++) { if (quote) { if (escaped) escaped = 0; else if (*p == '\\') escaped = 1; else if (*p == '"') quote = 0; continue; } if (*p == '"') quote = 1; else if (*p == '[') depth++; else if (*p == ']' && --depth == 0) return p; }
  return NULL;
}

static int object_field_span(const char *object, const char *end, const char *expected,
                             const char **value, const char **value_end) {
  const char *p = skip_space_const(object);
  if (*p++ != '{') return 0;
  p = skip_space_const(p);
  while (p < end && *p != '}') {
    const char *key = p, *key_end = skip_json_string(p);
    if (!key_end) return 0;
    p = skip_space_const(key_end);
    if (*p++ != ':') return 0;
    p = skip_space_const(p);
    const char *candidate = p, *candidate_end = skip_json_value(p);
    if (!candidate_end) return 0;
    if (json_string_equals(key, key_end, expected)) {
      *value = candidate;
      *value_end = candidate_end;
      return 1;
    }
    p = skip_space_const(candidate_end);
    if (*p == '}') break;
    if (*p++ != ',') return 0;
    p = skip_space_const(p);
  }
  return 0;
}

static int entry_duplicate(const char *array, const char *array_close, const char *kind,
                           const char *entry, const char *entry_end) {
  const char *wanted_one = NULL, *wanted_one_end = NULL, *wanted_two = NULL, *wanted_two_end = NULL;
  const char *key_one = !strcmp(kind, "launches") ? "event_fingerprint" : "tool_name";
  const char *key_two = !strcmp(kind, "launches") ? NULL : "command";
  if (!object_field_span(entry, entry_end, key_one, &wanted_one, &wanted_one_end) ||
      (key_two && !object_field_span(entry, entry_end, key_two, &wanted_two, &wanted_two_end))) fail("ledger_entry_invalid");
  const char *p = skip_space_const(array + 1);
  while (p < array_close && *p != ']') {
    const char *existing = p, *existing_end = skip_json_value(p);
    const char *existing_one = NULL, *existing_one_end = NULL, *existing_two = NULL, *existing_two_end = NULL;
    if (!existing_end || !object_field_span(existing, existing_end, key_one, &existing_one, &existing_one_end)) fail("ledger_schema_invalid");
    if (key_two && !object_field_span(existing, existing_end, key_two, &existing_two, &existing_two_end)) fail("ledger_schema_invalid");
    if (wanted_one_end - wanted_one == existing_one_end - existing_one && !memcmp(wanted_one, existing_one, (size_t)(wanted_one_end - wanted_one)) &&
        (!key_two || (wanted_two_end - wanted_two == existing_two_end - existing_two && !memcmp(wanted_two, existing_two, (size_t)(wanted_two_end - wanted_two))))) return 1;
    p = skip_space_const(existing_end);
    if (*p == ']') break;
    if (*p++ != ',') fail("ledger_schema_invalid");
    p = skip_space_const(p);
  }
  return 0;
}

static char *append_entry(char *ledger, const char *kind, const char *entry, const char *identity) {
  const char *entry_end = skip_json_value(entry);
  if (!entry_end || *skip_space_const(entry_end) || entry[0] != '{' || !identity || !*identity ||
      (!strcmp(kind, "launches") ? !launch_valid(entry, entry_end) : !root_thread_action_valid(entry, entry_end))) fail("ledger_entry_invalid");
  char key[64]; snprintf(key, sizeof(key), "\"%s\"", kind);
  char *field = strstr(ledger, key); if (!field) fail("ledger_schema_invalid");
  char *open = strchr(field + strlen(key), '['); if (!open) fail("ledger_schema_invalid");
  char *close = array_end(open); if (!close) fail("ledger_schema_invalid");
  if (entry_duplicate(open, close, kind, entry, entry_end)) return ledger;
  char *content = skip_space(open + 1); size_t left = (size_t)(close - ledger); size_t entry_len = strlen(entry);
  char *out = malloc(strlen(ledger) + entry_len + 3); if (!out) fail("ledger_memory_failed");
  memcpy(out, ledger, left); size_t pos = left;
  if (content != close) out[pos++] = ',';
  memcpy(out + pos, entry, entry_len); pos += entry_len;
  strcpy(out + pos, close);
  if (!ledger_common_schema_valid(out)) { free(out); fail("ledger_append_schema_invalid"); }
  free(ledger);
  return out;
}

static void write_replace(int parent, const char *ledger) {
  struct stat residue; if (fstatat(parent, TEMP, &residue, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) fail("ledger_temp_preexisting");
  int fd = openat(parent, TEMP, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600); if (fd < 0) fail("ledger_temp_create_failed");
  temp_owned = 1;
  size_t length = strlen(ledger); if (write(fd, ledger, length) != (ssize_t)length || fsync(fd) < 0) { close(fd); fail("ledger_write_failed"); }
  if (close(fd) < 0 || renameat(parent, TEMP, parent, LEDGER) < 0) fail("ledger_atomic_replace_failed");
  temp_owned = 0;
}

int main(int argc, char **argv) {
  const char *repo = NULL, *kind = NULL, *entry = NULL, *identity = NULL;
  for (int i = 1; i + 1 < argc; i += 2) { if (!strcmp(argv[i], "--repo")) repo = argv[i+1]; else if (!strcmp(argv[i], "--kind")) kind = argv[i+1]; else if (!strcmp(argv[i], "--entry")) entry = argv[i+1]; else if (!strcmp(argv[i], "--identity")) identity = argv[i+1]; else fail("ledger_usage_invalid"); }
  if (!repo || !entry || !identity || (!kind || (strcmp(kind, "launches") && strcmp(kind, "root_thread_actions")))) fail("ledger_usage_invalid");
  int parent = open_parent(repo); acquire_lock(parent); char *ledger = read_ledger(parent); ledger = append_entry(ledger, kind, entry, identity); write_replace(parent, ledger); free(ledger); release_lock(parent); close(parent); return 0;
}
