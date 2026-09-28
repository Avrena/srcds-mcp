# srcds-mcp

A zero-dependency stdio MCP server for operating Garry's Mod/SRCDS instances
hosted by Pterodactyl. Server names, volume markers, SSH transport, database
aliases, and live-traffic thresholds come from `config.json`; no deployment
identifiers or credentials are embedded in the source or documentation.

The control path is:

```text
MCP client -> stdio JSON-RPC -> local srcds_mcp.py
           -> SSH stdin -> versioned host-side Python driver
           -> docker attach / bind-mounted volume / database container
```

Lua results use a framed volume-file channel, so they do not depend on
`-condebug`. Requests cross SSH as URL-safe base64 JSON over stdin, avoiding
shell interpolation and command-line length limits.

## Quick start

Use Python 3.10+ and an SSH client locally, and Python 3 on the Linux node.
The node must host the game volumes and have Docker available.

1. Copy `config.example.json` to `config.json`. Set `ssh.host`, `ssh.key`,
   `public_ip`, and the marker directory for each logical server. Obtain the
   endpoint and private key from the server operator; keep the key outside this
   repository. On Windows, use forward slashes or escaped backslashes in JSON.
2. Check configuration with
   `python -c "import srcds_mcp as m; print(m.config_error() or 'config OK')"`.
3. Keep `srcds_diagnostics.lua` beside `srcds_mcp.py`. Register the script using the absolute Python/script paths in
   [`mcp.json.example`](mcp.json.example). It includes JSON and Codex TOML forms.
4. Restart the MCP connection, then run `srcds_status` to check discovery.

Settings load from built-in defaults, then `config.json` (or the file named by
`SRCDS_MCP_CONFIG`), then individual `SRCDS_MCP_*` environment overrides.
For multiple nodes, register the script separately for each node and set
`SRCDS_MCP_CONFIG` to that registration's local configuration file. Keep all
actual node configurations outside Git.

## Tools and safety gates

| Tool | Gate | Purpose |
| --- | --- | --- |
| `srcds_status` | Always allowed | Up/down, A2S player count, threshold state, port, capture capability. Hostnames and container IDs are omitted. |
| `srcds_diagnostics` | Fixed reads automatic; profile, collector start/stop, and refresh confirm | Optional HolyLib capabilities, profiling, network snapshots, and stock Lua-error collection. |
| `srcds_fetch` | Remote reads allowed; `save_to` requires confirm | Console/container log tails, numbered file reads with paging and whole-file grep, directory listings, hashes, deploy backups, and deployment history. |
| `srcds_console` | Allowlisted reads automatic; otherwise confirm | One console command. Multiline/compound commands always require confirmation. |
| `srcds_lua` | Always confirm | Server Lua and asynchronous verification suites, inline or from a local file. Arbitrary Lua is not statically classified as safe. |
| `srcds_deploy` | Always confirm | Single or batch writes/restores with backups and resource limits. |
| `srcds_grep` | Always allowed | One-call bounded grep with multiple patterns, globs, exclusions, and roots; matches grouped by file. |
| `srcds_diff` | Server-to-server automatic; local comparisons confirm | Single or batch diffs with per-file and aggregate input limits. |
| `srcds_nodeinfo` | Always allowed | Host load, memory, disk, and per-container stats, plus optional wings-log and dmesg tails. |
| `srcds_clientlua` | Confirm; broadcasts also require `broadcast` and `force` | Bounded client Lua delivery with transfer/compile/synchronous-runtime acknowledgements. |
| `srcds_power` | Confirm; disruptive actions force on players or unknown population | Wings-backed start/stop/restart/kill plus boot watcher. |
| `srcds_monitor` | Always allowed | Private-state background console/state watcher with PID identity validation on stop; checks return only unseen matches. |
| `srcds_db_query` | Classified reads automatic; writes/ambiguous SQL confirm | MariaDB query; automatic reads run inside a read-only transaction. |
| `srcds_db_schema` | Always allowed | Structured MariaDB schema inspection. |
| `srcds_mongo_query` | Always confirm | Arbitrary mongosh JavaScript. |
| `srcds_mongo_schema` | Always allowed | Structured MongoDB schema inspection. |

General local operation logs redact command/code/query/target/path text, store
aggregate lengths/modes, and rotate at 5 MiB with three retained generations.
Deployment protocol v2 additionally keeps durable host-side per-file metadata:
destination, source working-copy path, expected/before/after SHA-256, client
instance and PID, available task ID, optional Git revision, and deployment ID.
File contents, credentials, commands, and player data are not included in that
deployment history. Existing historical logs are not rewritten.

## Deployment protocol v2 (breaking change)

Every deployment entry, including restore, requires `expected_sha256`: the full
SHA-256 of the **original remote bytes used as the edit base**. Downloads, file
reads, hash listings, and diffs (`sha256_a` for each differing file) expose full
SHA-256. Save that hash
alongside the working copy before editing. Use literal `"missing"` only when
creating a path that does not exist. Do not fetch a fresh hash and attach it to
an old candidate; first reconcile the remote changes into the candidate.

Both the client and host resolve the target afresh. Ambiguous targets and failed
discovery fail closed. The host locks the volume across all base checks, backups,
writes, verification, and history. One stale file rejects the entire batch
before any target or backup changes. This prevents one cooperating MCP writer
from silently replacing another writer's newer file.

Backups are mandatory and versioned under
`backups_root/_guard_v2/<volume>/versions/<deployment_id>/<relative-path>`.
They are outside game trees and retained until an operator explicitly removes
them. Identical writes are recorded as no-ops and create no redundant backup.
Read `srcds_fetch what="backups"` or `what="history"`, optionally filtering by
`path`. History lists one line per deployment, newest first, and accepts `lines`
(page size, max 100) and the printed `before` cursor. `deployment_id` lists one
deployment's files with full hashes, paged with `offset` and `lines`. A
deployment shown as `uncertain` was prepared without a final result.

Restore requires an explicit `backup_id` from those records, plus the expected
hash of the current file, and no local/content payload. It also backs up the
displaced current version, so restoration can be undone. To select an older
pre-v2 backup, explicitly use `backup_id="legacy"`.

Every file replacement is atomic. A whole batch is **not** an atomic filesystem
transaction: an I/O failure or an unrelated writer after preflight can produce
a partial result. Durable intent, all original backups, and the final receipt
make that visible. Inspect history and live hashes after timeouts, partial
results, or interrupted calls before retrying. There is no automatic rollback
or force bypass. Direct SSH/file-manager/Lua writes do not honor this lock.

After installing v2, reconnect existing MCP sessions to load the new code and
schema. Previously launched Python processes retain old code; they must be
retired or otherwise blocked before considering the cutover complete. Retire
only idle MCP helpers and fence known cached legacy host drivers against writes.
Other machines using separate MCP installations also need upgrading; this is
a concurrency guard for the deployment route, not an access-control boundary
against clients with arbitrary root SSH or server-Lua privileges.

## Batch-first file operations

When comparing or deploying more than one file, use a single batch call. The
server advertises this rule during MCP initialization.

```text
srcds_diff {
  server: "game",
  confirm: true,
  files: [
    {path: "addons/x/lua/a.lua", local: "C:/work/x/lua/a.lua"},
    {path: "addons/x/lua/b.lua", local: "C:/work/x/lua/b.lua"}
  ]
}

srcds_diff {
  server: "game-a",
  server_b: "game-b",
  files: [{path: "addons/x/lua/a.lua"}, {path: "addons/x/lua/b.lua"}]
}

srcds_deploy {
  server: "game",
  confirm: true,
  files: [
    {to: "addons/x/lua/a.lua", local: "C:/work/x/lua/a.lua", expected_sha256: "<saved original SHA-256>"},
    {to: "addons/x/lua/b.lua", local: "C:/work/x/lua/b.lua", expected_sha256: "<saved original SHA-256>"}
  ]
}
```

Local diff contents cross the SSH boundary to the comparison driver, so local
comparisons require `confirm:true`. Server-to-server comparisons remain
read-only and do not require confirmation. Diff limits are 16 MiB per side and
64 MiB aggregate per batch. Deploy limits are 64 MiB per file and 256 MiB per
batch.

For trees, call `srcds_fetch` with `what:"hash"`, compare the listings, then
batch-diff only mismatches. A batch diff prints each differing or failed file
and only counts identical ones. A deploy result lists only files whose hash
changed; an unchanged file keeps the `expected_sha256` that was sent.

## Multi-filter grep

Singular fields remain compatible. Array fields combine filters in one bounded
host invocation:

```text
srcds_grep {
  server: "game",
  patterns: ["RegisterNetReceiver", "net.Receive"],
  globs: ["*.lua", "*.txt"],
  exclude_globs: ["*_test.lua", "vendor_*"],
  paths: ["addons/one", "gamemodes/two"],
  max: 300
}
```

`patterns[]` are OR alternatives (`grep -e` semantics). `globs[]` are OR
include filters. `exclude_globs[]` removes filename matches. Output capture is
bounded at the pipe, so a broad match or one minified line cannot allocate an
unbounded subprocess buffer.

Matches are grouped by file: the path once, then `N:text` with indentation
stripped, and `N-text` for context lines. `regex` selects `basic` (default),
`extended`, `fixed`, or `perl` syntax; `ignore_case`, `context` (0-10), and
`output:"files"` (names only) are also available. `max` (default 50) limits
shown matches; the total is still counted.

## Reading files

`srcds_fetch what:"file"` returns numbered lines from line 1. The header gives
the range returned, the file's line count, the next `offset` when more remain,
and the full-file SHA-256 to keep as a deploy base. A negative `offset` counts
back from the end (`-50` reads the last 50 lines). `grep` searches the whole
file and returns numbered matching lines. Lines over 2000 characters are cut
with a marker, and binary files are summarized; use `save_to` for exact bytes.
Matching scans every byte even in very long lines, while retaining only a
bounded display prefix. File-read payload budgets count UTF-8 bytes; a budget
too small for the first displayed line returns an error with the required size.

History receipts marked `uncertain` show planned hashes as unconfirmed. A
prepared receipt does not establish that a write or backup completed; inspect
the current file hashes and available backups before retrying.

## Output budgets

Results stay in an agent's context for the rest of a session, so default
budgets are small: console 8 KB, fetch 12 KB, grep 50 matches, batch diff
16 KB, and DB/Mongo 12 KB. Raise `maxbytes` up to 200000, or grep's `max` up to
2000 matches. Truncated results say how to request more. Guidance that does
not change between calls is printed once per MCP process.

## Server Lua verification

Every `srcds_lua` call requires `confirm:true`. Pass the suite as `code`, or
as `local`, the path of a local UTF-8 file, to rerun it without resending it.
Both forms have the same 64 KiB UTF-8 source limit.
The runner isolates plain global assignments but retains access to shared
tables and engine APIs. Only trusted, explicitly authorized code should run.
The runner supplies:

```text
SECTION(name)  CHECK(cond,msg)  EQ/NEQ/NEAR  TRUE/FALSE/OK
THROWS(fn,msg) DUMP(value)      LOG(...)     MCP_DONE()
```

It captures output and return values, preserves user line numbers, limits
instructions/output/failure detail, and treats missing start/end markers as an
error. Asynchronous suites set `async:true` and call `MCP_DONE()` from the final
callback. Helper functions and finalization state are isolated per runner, so
overlapping async requests cannot finalize or log into one another.

## Optional runtime diagnostics

`srcds_diagnostics` uses fixed Lua operations over the existing framed transport.
Keep `srcds_diagnostics.lua` beside the Python entry point. No native module,
listener, or autorun file is installed by these operations. HolyLib is optional;
basic player ping/loss and the error collector work without it.

```text
srcds_diagnostics {server: "game", action: "capabilities"}
srcds_diagnostics {server: "game", action: "players", limit: 20, offset: 0}
srcds_diagnostics {server: "game", action: "profile", seconds: 1, limit: 10, confirm: true}
srcds_diagnostics {server: "game", action: "errors_start", ttl: 600, confirm: true}
srcds_diagnostics {server: "game", action: "errors", after: "<returned cursor>"}
srcds_diagnostics {server: "game", action: "errors_stop", confirm: true}
srcds_fetch {server: "game", what: "crashes", lines: 20}
srcds_fetch {server: "game", what: "crashes", path: "<listed filename>", lines: 100}
```

- Capabilities are checked afresh, including individual APIs. `srcds_status`
  accepts `diagnostics:true` and `players:true` for the same optional details;
  these add Lua-call latency. Player snapshots report native sign-on, directional
  latency/loss, current choked-packet counts and timeout settings when available.
  Native slots are zero-based; loss fields are percentages and latency is in ms.
  Missing channel data remains absent. Use `next_offset` to page a fresh snapshot.
- Profiles last 0.1–5 seconds and return cumulative-counter differences ranked
  by self time after three warm-up frames. The root/idle bucket is excluded.
  Profiles require `vprof.NODE_GC_SAFE = true` from HolyLib's
  [borrowed-node ownership fix](https://github.com/ncgst/gmod-holylib/pull/23);
  older native wrappers can delete engine-owned profile nodes during collection.
  Existing profiling sessions and counters are preserved. Collection
  requires active frames; empty servers with hibernation thinking disabled are
  rejected. VProf gamemode nodes aggregate an event's callbacks; they do not
  identify every `hook.Add` callback separately. Counter resets, tree changes,
  or traversal limits produce an explicit error rather than misleading timings.
- The collector uses stock `OnLuaError` and `OnClientLuaError`. It retains at most
  128 records, bounds messages/stacks, and coalesces consecutive matching records
  within one second. `count` is the record's cumulative occurrence count. A repeat
  advances its cursor so existing readers can see the updated count. Reads do not
  consume records; each reader keeps its own `after` cursor. `more` indicates a
  further page, `dropped` counts evicted occurrences, and `gap` reports overwritten
  unread history. An insufficient byte budget leaves the cursor unchanged.
- Collection expires after `ttl` seconds (10–3600) or stops on map shutdown.
  Starting an active collector reuses it without resetting history or extending
  its expiry. Map changes/restarts lose the buffer and invalidate its cursors.
  Registration does not prove engine delivery: `server_observed` and
  `client_observed` indicate received events. Client reports can be rate limited
  and lack stacks; the buffer is not a complete error history. Hooks observe
  events without suppressing normal output. No persistent log is written.
- Crash logs are listed newest first and can be read while the game is down.
  Reads remain confined to the game volume and reject individual symlink files.

Set `refresh_lua:true` on a confirmed deployment to request LuaPack refreshes
after successful changed Lua writes. Exact `garrysmod`-relative paths are retained
to avoid selecting a shadowing addon. LuaPack must already be enabled and the
file must already be registered. Unsupported, disabled, and ineligible states
are reported; this option never enables LuaPack. `captured` and `refresh_queued`
describe server processing, **not client execution**.

If a refresh fails after files were written, the deployment receipt still reports
the completed write. Retry only the refresh with `srcds_diagnostics action:"refresh",
paths:["addons/example/lua/client.lua"], confirm:true`; reconcile file content
before retrying a deployment. No-op or rejected deployments request no refresh.

## Client Lua transport

`srcds_clientlua` no longer treats `SendLua` queueing as execution:

1. `target` is required and accepts only exact SteamID/SteamID64 or `all`.
2. Raw UTF-8 source is capped at 64 KiB; bots and not-yet-authenticated players
   are excluded, and recipients are capped at 128.
3. A short `SendLua` bootstrap installs a fixed client receiver and returns a
   per-request ready signal.
4. Only after that ready signal, the server sends tokenized, optionally
   compressed net chunks with length and checksum metadata.
5. Each client reports `ok`, `transfer_error`, `compile_error`, or
   `runtime_error` for the synchronous top-level chunk; missing or inconsistent
   acknowledgements mark the tool call as an error.

Broadcast example:

```text
srcds_clientlua {
  server: "game",
  target: "all",
  code: "print('diagnostic')",
  confirm: true,
  broadcast: true,
  force: true
}
```

An acknowledgement is client-reported transfer, compile, and synchronous
top-level execution evidence only. Later timers/callbacks, UI appearance,
rendering, interaction, and player-visible correctness still require a real
client or human acceptance check.

## Database boundaries

MariaDB read classification rejects executable comments and ambiguous leading
forms such as `WITH`. Automatic reads run inside `START TRANSACTION READ ONLY`
and finish with `ROLLBACK`. Writes and ambiguous SQL require `confirm:true`.
`srcds_db_schema` uses generated read-only statements.

Arbitrary mongosh JavaScript cannot be safely proven read-only with a method
regex, so every `srcds_mongo_query` call requires `confirm:true`.
`srcds_mongo_schema` remains an unconfirmed, generated read-only inspection
tool.

Credentials remain inside their containers and are never returned or logged.

## Filesystem and process boundaries

Remote paths use `realpath` plus `commonpath`, rejecting prefix-collision and
symlink escapes. Local `save_to` requires an absolute path, `confirm:true`, and
`overwrite:true` if the destination exists.

Monitor state lives in a root-only directory. Stopping a watcher verifies its
PID start time, process-group leadership, and per-process nonce before sending a
signal, preventing stale-state PID reuse from killing an unrelated process.

## Configuration and reload

Copy `config.example.json` to `config.json`, then set the SSH key path, SSH
endpoint, server marker topology, and optional DB/Mongo aliases. Keep private
endpoints, keys, and identifiers out of committed documentation.

MCP clients load stdio servers at startup. After editing `srcds_mcp.py`, restart
or reconnect every configured MCP registration. Until reconnection, the client
continues exposing the old tool schema and initialization instructions.

## Validation

The regression suite is offline and never connects to a game server:

```text
python -m unittest discover -p "test*.py" -v
```

It covers confirmation bypasses, executable SQL comments, acknowledged
clientlua failure paths, fail-closed power control, local-write confirmation,
multi-filter grep request composition, MCP protocol negotiation, concurrent
deployment conflicts, whole-batch preflight, target resolution, versioned
restore, backup integrity, durable-history failure handling, complete and
paged file reads, monitor deltas, history summaries, grouped grep, and output
budgets. The host tests use temporary directories; they do not require a live
game server.
The symlink test skips on Windows when the process cannot create symlinks.
