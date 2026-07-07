# srcds-mcp

> *You gave an AI a terminal into your **live** game server. Bold. Respect.*
> The good news: every button that can ruin your night needs a very deliberate
> `confirm: true` first. The tool is paranoid so you don't have to be.

An [MCP](https://modelcontextprotocol.io) server that lets Claude Code (or Codex, or
any MCP client) drive live **Garry's Mod / srcds** servers running under
[Pterodactyl](https://pterodactyl.io): tail logs, run console commands, execute
server **and** client Lua, run assertion suites, deploy files, control power, and
query the game MariaDB — as first-class tools, no manual SSH dance each time.

Zero dependencies. It's a single stdlib-only Python file. No `pip install`, ever.

---

## If your AI says "the tools aren't working" — READ THIS FIRST

**99% of the time, the MCP was never configured.** This tool ships with *blank*
connection settings on purpose (so nobody's server address ends up on GitHub). Until
**you** create a `config.json`, every tool politely refuses with a message like:

```
config: ssh.key is not set — point it at your SSH private key in config.json
config: ssh.host is not set — edit config.json (copy config.example.json first)
config: ssh.key does not exist: C:/path/to/your/ssh_key.pem — fix the path
```

That is **not a bug**. That is the tool telling you step 3 below hasn't happened yet.

Common failure modes, ranked by how often we've actually seen them:

| Symptom | What's really going on | Fix |
| --- | --- | --- |
| Every tool returns `config: ssh.key is not set …` | No `config.json`, or `ssh.key` is blank. | Do the **Quick start** below. |
| `config: ssh.key does not exist: …` | The path is wrong / the key isn't where you said. | Fix the path in `config.json`. |
| `could not upload host driver over SSH` | Config is fine, but the box is unreachable. | Check VPN/network, `ssh.host`, `ssh.port`, and that the key actually works. |
| Your AI cheerfully announced "all set up!" but nothing works | The model **hallucinated** doing the setup. It cannot invent a private key it doesn't have. | Open `config.json` yourself. If it doesn't exist, the AI lied. Create it. |
| The tools don't appear in the client at all | MCP servers load at **client startup**. | Register per `mcp.json.example`, then **restart** the client. |

> **For the human:** if you delegated setup to an LLM, verify two things with your own
> eyes: (1) a file literally named `config.json` exists next to `srcds_mcp.py`, and
> (2) its `ssh.key` points at a private key file that actually exists on disk. An LLM
> can write config and register the server, but it **cannot** produce the SSH key or
> the node address out of thin air — those come from a human. If it claims otherwise,
> it's confabulating; check the file.

Sanity-check without launching an MCP client at all:
```
python -c "import srcds_mcp as m; print(m.config_error() or 'config OK')"
```
Prints `config OK` when you're good to go, or the exact thing to fix.

---

## Quick start (per developer)

1. **Python 3.8+** on PATH (or note the full path to `python.exe`).
2. **Get the SSH key + node address** from whoever runs the servers. Save the key
   somewhere private (e.g. `C:\Users\<you>\Documents\`). Do **not** put it in this
   folder, and never commit it.
3. **Make your config:**
   ```
   copy config.example.json config.json      # Windows
   cp   config.example.json config.json       # macOS/Linux
   ```
   Open `config.json` and set at least **`ssh.key`**, **`ssh.host`**, and
   **`public_ip`**. (On Windows use `C:/Users/you/...` or escaped `C:\\Users\\you\\...`.)
4. **Register it with your MCP client** — see `mcp.json.example`. For Claude Code,
   merge the `mcpServers` block into your project `.mcp.json` (or `~/.claude.json`)
   with absolute paths to `python.exe` and `srcds_mcp.py`. For Codex, use the TOML form.
5. **Restart the client.** MCP servers load at startup.
6. **Smoke test:** run `srcds_status`. You should see your servers and live player counts.

---

## Configuration (`config.json`)

Resolution order (later overrides earlier):

1. built-in `DEFAULTS` in `srcds_mcp.py` (blank connection settings on purpose)
2. `config.json` next to the script (or the path in `$SRCDS_MCP_CONFIG`)
3. individual `SRCDS_MCP_*` environment variables (handy for CI / one-offs)

| Key | What | Who sets it |
| --- | --- | --- |
| `ssh.key` | Path to **your** SSH private key. **Required.** | you |
| `ssh.host` / `ssh.port` | SSH endpoint of the node, e.g. `root@1.2.3.4` : `22`. **Required.** | you |
| `public_ip` | Public game IP, for A2S player counts. | you |
| `ssh.bin` | `ssh` on PATH, or a full path to `ssh.exe`. | usually default |
| `ssh.known_hosts` | Blank ⇒ `~/.ssh/known_hosts`. | usually blank |
| `volroot` / `backups_root` | Pterodactyl volumes root; out-of-tree deploy backups. | default (standard Ptero) |
| `wings.api` / `wings.config` | Wings power API + config path (for the bearer token). | default (standard Ptero) |
| `owner_uid` / `owner_gid` | `chown` target for deployed files (`pterodactyl:pterodactyl`). | default |
| `panel_url` | Pterodactyl panel URL (shown in power errors). | optional |
| `live_thresholds` | Player count per server that counts as "LIVE". | optional |
| `servers` | Topology: marker dir under `garrysmod/` → logical name (first match wins). | you |
| `db_aliases` | DB-tool aliases: game name → MariaDB schema. Optional (raw names work). | optional |

Env overrides: `SRCDS_MCP_SSH_KEY`, `SRCDS_MCP_SSH_BIN`, `SRCDS_MCP_SSH_HOST`,
`SRCDS_MCP_SSH_PORT`, `SRCDS_MCP_KNOWN_HOSTS`, `SRCDS_MCP_PUBLIC_IP`, and
`SRCDS_MCP_CONFIG` (path to an alternate config file).

**Topology matters.** Each `servers[]` entry maps a marker directory (something that
uniquely exists under that server's `garrysmod/`, e.g. a signature addon or its
gamemode folder) to a logical name. UUIDs, ports, and up/down state are all resolved
**live** (`docker ps` + `SERVER_PORT`), so only the marker→name mapping is config.

The host-side facts (`volroot`, `backups_root`, `wings.*`, `owner_*`, `servers`) are
baked into the uploaded host driver, so the driver is byte-identical across everyone
on the same node (stable content hash); a different deployment yields a different,
correctly-namespaced driver.

---

## Why it's built the way it is

Servers commonly run **`-norcon`** (classic Source RCON off) with stdin owned by the
Pterodactyl wings daemon. The reliable control path is: **SSH → `pty.fork(docker
attach)` injection**. Reading results back differs by channel:

- **`srcds_console`** reads the `-condebug` `console.log`. Only servers launched with
  `-condebug` echo output back; without it a console command runs "blind" (executes,
  no echo).
- **`srcds_lua`** does **not** depend on `-condebug`. The injected runner `file.Write`s
  its framed output to `data/_mcp/<token>.txt` on the volume, which the host driver
  reads directly off the bind-mounted volume. So **Lua/verification output is captured
  on every server**, with no console 4 KB line limit and none of the live console's
  other-player spam.

```
MCP client ──stdio JSON-RPC──> srcds_mcp.py (your machine, zero-dep, stdlib only)
                                   │  ssh -i <your key> <ssh.host>:<ssh.port>
                                   ▼
                           /tmp/srcds_host_driver_<hash>.py (python3 on the node)
                                   │  pty.fork → docker attach → lua_openscript
                                   ▼
                           srcds runner ──file.Write──> data/_mcp/<token>.txt
                                   ▲                           │
                                   └── driver reads volume ────┘  (works w/o -condebug)
```

The request crosses the SSH boundary as **urlsafe-base64 JSON over stdin** (never the
command line), so no shell-quoting layer can mangle Lua/commands **and large payloads
don't hit the Windows ~32 KB command-line limit**.

---

## Tools

| Tool | Gate | What |
| --- | --- | --- |
| `srcds_status` | always allowed | up/down, **live player count (A2S)**, LIVE flag vs thresholds, port, condebug |
| `srcds_fetch` | always allowed | tail `console.log` or read any file under `garrysmod/`; ANSI stripped, output byte-capped (`maxbytes`, default 48 KB) |
| `srcds_console` | confirm if destructive | inject a console command; returns the console.log delta where `-condebug` is on |
| `srcds_lua` | confirm if mutating | run server Lua / **multi-line verification suites**; captures output + `return <expr>` with an assertion harness |
| `srcds_deploy` | confirm | write a local file / inline content to the volume; UTF-8/CJK-safe, backs up overwrites **out-of-tree**, `.lua` hot-reloads, works even if server DOWN |
| `srcds_grep` | always allowed | recursive `grep` across the **deployed** volume source (find symbols / local-vs-remote divergence) |
| `srcds_clientlua` | confirm | run **clientside** Lua on connected players (chunked base64 `SendLua`); UI/PAC3 hot-reload without reconnect |
| `srcds_power` | confirm (+force if LIVE) | wings-API start/stop/restart/kill (graceful, not a crash) |
| `srcds_db_query` | read auto / write confirm | SQL against the game MariaDB; SELECT/SHOW auto, INSERT/UPDATE/DELETE/DDL need confirm |
| `srcds_db_schema` | always allowed | browse databases → tables (row counts) → columns/indexes |

Dev loop: **`srcds_grep`** (find) → edit locally → **`srcds_deploy`** (push,
autorefresh reloads) → **`srcds_lua`** suite (verify) → **`srcds_clientlua`** (push
clientside) → **`srcds_power`** (boot a server that's off).

### Examples
- `srcds_status` → table of all servers.
- `srcds_lua {server:"scprp", code:"return player.GetCount()"}` → `0`.
- `srcds_console {server:"scprp", command:"status", grep:"players"}`.
- `srcds_fetch {server:"scprp", what:"file", path:"cfg/server.cfg"}`.
- `srcds_deploy {server:"scprp", to:"addons/x/lua/autorun/server/y.lua", local:"C:/.../y.lua", confirm:true}`.

*(`scprp` here is just whatever you named a server in `config.json` → `servers`.)*

### Database (MariaDB)
If the node runs a `mariadb` container, the DB tools query it via `docker exec` and
read the root password from the container's own `$MYSQL_ROOT_PASSWORD` — **the
password is never extracted, hardcoded, or logged**. `database` accepts a raw schema
name or any alias you define in `db_aliases`. Reads run automatically; a write/DDL is
blocked until `confirm:true`. Output is a text table (or `format:"tsv"`), capped
~40 KB — add a `LIMIT`.

---

## Verification suites (`srcds_lua`)

`srcds_lua` is built for **multi-line assertion suites**, not just one-liners. Your
code runs **verbatim** (errors report *your* line numbers), with injected globals:

```
SECTION(name)            EQ(a, b, msg)      NEAR(a, b, eps, msg)   THROWS(fn, msg) -> err
CHECK(cond, msg)         NEQ(a, b, msg)     TRUE(v, msg) FALSE OK  DUMP(v) -> string
LOG(...)                 MCP_DONE()  -- async only
```

Each failing check emits a `[FAIL]` line with `got=`/`want=`; a `checks: p=X f=Y`
summary is returned and **any failure makes the call an error**. A top-level `return
<expr>` (numbers/strings/booleans/**tables**) is safely serialized (entities/vectors
tagged; cycles & functions never crash).

```lua
-- srcds_lua {server="scprp", code = [[
SECTION("site state")
local n = player.GetCount()
CHECK(n >= 0 and n <= game.MaxPlayers(), "player count in range")
NEQ(game.GetMap(), "", "map present")
LOG("map=", game.GetMap(), "players=", n)
return { map = game.GetMap(), players = n }
]]}
```

Internals: the body is `CompileString`'d (NOT `include()`, which swallows errors)
inside a `setfenv` sandbox; a `debug.sethook` instruction budget aborts runaway loops
*before* HolyLib's 10 s hang-kill; output is framed `__MCP~|~<token>~|~KIND~|~<b64>`
lines `file.Write`n to `data/_mcp/<token>.txt` (works on every server regardless of
`-condebug`). For timer/coroutine suites pass `async=true` and call `MCP_DONE()`.

---

## Safety model
- `srcds_status` / `srcds_fetch` / `srcds_grep` / `srcds_db_schema` are read-only → always run.
- `srcds_console` / `srcds_lua` run automatically **unless** the command/code matches
  the destructive/mutating denylist (kick, ban, changelevel, map, `*_password`,
  restart, `:SetHealth`, `:Give`, `file.Write`, `RunConsoleCommand`, …) — then they're
  **blocked** until re-called with `confirm:true`. The block message includes the live
  player count. The denylist is a backstop heuristic, not a sandbox.
- `srcds_deploy` / `srcds_clientlua` / `srcds_power` always require `confirm:true`.
  `srcds_deploy` is path-guarded (no `..`/absolute escape out of `garrysmod/`) and
  backs up overwrites. `srcds_power` additionally needs `force:true` to
  stop/restart/kill a **LIVE** server.
- `srcds_db_query` runs SELECT/SHOW/DESCRIBE/EXPLAIN automatically but blocks any
  write/DDL until `confirm:true`. DB names are regex-validated and SQL crosses via
  stdin (no shell injection).
- Every call is appended to `srcds_mcp.log` (next to the script, git-ignored).

---

## Security notes (please read before you `git push`)
- **Never commit `config.json` or your SSH key.** Both are in `.gitignore`. `config.json`
  holds your node address; the key is the keys to the kingdom.
- The private key lives on your machine only and is referenced **by path**. This repo
  contains no keys, no real IPs, and no live endpoints — you supply them locally.
- Tools that mutate are confirm-gated, but the denylist is a *heuristic*, not a jail.
  Treat `confirm:true` on a live server with the respect it deserves.

---

## Troubleshooting
- **`config: …`** → see the big "READ THIS FIRST" section above.
- **"could not upload host driver / SSH failed"** → network/VPN to the node, the key
  path, and that your `ssh.bin` exists.
- **"server DOWN"** → boot it with `srcds_power {server:"<name>", action:"start", confirm:true}`.
- **Lua syntax error** → captured as a framed `ERR` carrying your own body line number;
  the call is marked `isError`.
- **`srcds_power`** drives the **wings API** (same path as the panel buttons), so a
  stop/restart is a *normal quit* (exit 0, not flagged as a crash) and the server
  reliably comes back. Raw `docker` is only a fallback if wings is unreachable.
- Inspect `srcds_mcp.log` for a JSONL trace of every call.

---

See `CHANGELOG.md` for version history. Built for Pterodactyl-hosted GMod servers;
adapt the config to your own fleet.
