#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <stdio.h>
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

static void fail(const char *reason) { fprintf(stderr, "%s\n", reason); exit(2); }

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
    if (fd >= 0) { close(fd); return; }
    if (errno != EEXIST) fail("ledger_lock_create_failed");
    struct stat st;
    if (fstatat(parent, LOCK, &st, AT_SYMLINK_NOFOLLOW) < 0) continue;
    if (!S_ISREG(st.st_mode)) fail("ledger_lock_path_unsafe");
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 25000000}; nanosleep(&pause, NULL);
  }
  fail("ledger_lock_timeout");
}

static void release_lock(int parent) { if (unlinkat(parent, LOCK, 0) < 0) fail("ledger_lock_release_failed"); }

static int balanced_json(const char *text) {
  int depth = 0, quote = 0, escaped = 0;
  for (const char *p = text; *p; p++) {
    if (quote) { if (escaped) escaped = 0; else if (*p == '\\') escaped = 1; else if (*p == '"') quote = 0; continue; }
    if (*p == '"') quote = 1;
    else if (*p == '{' || *p == '[') depth++;
    else if (*p == '}' || *p == ']') { if (--depth < 0) return 0; }
  }
  return depth == 0 && !quote && !escaped;
}

static char *read_ledger(int parent) {
  int fd = openat(parent, LEDGER, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0 && errno == ENOENT) {
    const char *empty = "{\"ledger_schema\":\"SUBAGENT_LAUNCH_LEDGER_V1\",\"generated_by\":\"codex_hook_pipeline\",\"generated_at\":\"writer-generated\",\"ledger_path\":\"artifacts/codex/subagent-launch-ledger.json\",\"codex_binary_status\":\"available\",\"coverage_scope\":{\"subagent_start_event_recorded\":true,\"supported_pretooluse_paths\":[\"Bash\",\"apply_patch\",\"Edit\",\"Write\"],\"unsupported_paths_fail_closed\":true,\"scope_note\":\"This ledger records event-derived SubagentStart launches and supported PreToolUse paths only.\"},\"launches\":[],\"root_thread_actions\":[]}";
    return strdup(empty);
  }
  if (fd < 0) fail("ledger_target_unsafe");
  struct stat st; if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_size < 2 || st.st_size > MAX_LEDGER) fail("ledger_target_unsafe");
  char *buf = calloc((size_t)st.st_size + 1, 1); if (!buf) fail("ledger_memory_failed");
  ssize_t got = read(fd, buf, (size_t)st.st_size); close(fd);
  if (got != st.st_size || !balanced_json(buf) || !strstr(buf, "SUBAGENT_LAUNCH_LEDGER_V1") || !strstr(buf, "\"launches\"") || !strstr(buf, "\"root_thread_actions\"")) fail("ledger_parse_or_schema_invalid");
  return buf;
}

static char *skip_space(char *p) { while (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t') p++; return p; }
static char *array_end(char *start) {
  int depth = 0, quote = 0, escaped = 0;
  for (char *p = start; *p; p++) { if (quote) { if (escaped) escaped = 0; else if (*p == '\\') escaped = 1; else if (*p == '"') quote = 0; continue; } if (*p == '"') quote = 1; else if (*p == '[') depth++; else if (*p == ']' && --depth == 0) return p; }
  return NULL;
}

static char *append_entry(char *ledger, const char *kind, const char *entry, const char *identity) {
  if (!balanced_json(entry) || entry[0] != '{' || !identity || !*identity) fail("ledger_entry_invalid");
  if (strstr(ledger, identity)) return ledger;
  char key[64]; snprintf(key, sizeof(key), "\"%s\"", kind);
  char *field = strstr(ledger, key); if (!field) fail("ledger_schema_invalid");
  char *open = strchr(field + strlen(key), '['); if (!open) fail("ledger_schema_invalid");
  char *close = array_end(open); if (!close) fail("ledger_schema_invalid");
  char *content = skip_space(open + 1); size_t left = (size_t)(close - ledger); size_t entry_len = strlen(entry);
  char *out = malloc(strlen(ledger) + entry_len + 3); if (!out) fail("ledger_memory_failed");
  memcpy(out, ledger, left); size_t pos = left;
  if (content != close) out[pos++] = ',';
  memcpy(out + pos, entry, entry_len); pos += entry_len;
  strcpy(out + pos, close); free(ledger); return out;
}

static void write_replace(int parent, const char *ledger) {
  struct stat residue; if (fstatat(parent, TEMP, &residue, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT) fail("ledger_temp_preexisting");
  int fd = openat(parent, TEMP, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600); if (fd < 0) fail("ledger_temp_create_failed");
  size_t length = strlen(ledger); if (write(fd, ledger, length) != (ssize_t)length || fsync(fd) < 0) { close(fd); unlinkat(parent, TEMP, 0); fail("ledger_write_failed"); }
  if (close(fd) < 0 || renameat(parent, TEMP, parent, LEDGER) < 0) { unlinkat(parent, TEMP, 0); fail("ledger_atomic_replace_failed"); }
}

int main(int argc, char **argv) {
  const char *repo = NULL, *kind = NULL, *entry = NULL, *identity = NULL;
  for (int i = 1; i + 1 < argc; i += 2) { if (!strcmp(argv[i], "--repo")) repo = argv[i+1]; else if (!strcmp(argv[i], "--kind")) kind = argv[i+1]; else if (!strcmp(argv[i], "--entry")) entry = argv[i+1]; else if (!strcmp(argv[i], "--identity")) identity = argv[i+1]; else fail("ledger_usage_invalid"); }
  if (!repo || !entry || !identity || (!kind || (strcmp(kind, "launches") && strcmp(kind, "root_thread_actions")))) fail("ledger_usage_invalid");
  int parent = open_parent(repo); acquire_lock(parent); char *ledger = read_ledger(parent); ledger = append_entry(ledger, kind, entry, identity); write_replace(parent, ledger); free(ledger); release_lock(parent); close(parent); return 0;
}
