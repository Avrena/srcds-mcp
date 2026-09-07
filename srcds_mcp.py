#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
srcds-mcp : zero-dependency stdio MCP server for driving live Garry's Mod /
srcds servers hosted under Pterodactyl, from Claude Code or any MCP client.

Transport: SSH -> host-side python driver -> pty.fork(docker attach) console
injection. Console output is read back from `-condebug` console.log where
available; Lua output goes through a volume file and works on every server.

No third-party packages. Stdlib only. Speaks MCP over newline-delimited JSON-RPC
on stdin/stdout.

Tools: srcds_status, srcds_fetch, srcds_console, srcds_lua, srcds_deploy,
srcds_grep, srcds_diff, srcds_nodeinfo, srcds_clientlua, srcds_power,
srcds_monitor, srcds_db_query, srcds_db_schema, srcds_mongo_query,
srcds_mongo_schema. Server names come from config.json (`servers[]`).

Safety: structured reads are allowed; arbitrary code and state-changing/local-
write operations require explicit confirmation. Audit logs redact payload text
and rotate beside the selected config file.
"""

import sys, os, json, base64, subprocess, socket, struct, re, time, traceback, hashlib

_HERE = os.path.dirname(os.path.abspath(__file__))
MCP_VERSION = "2.0.0"
import uuid as _uuid
_CLIENT_INSTANCE = _uuid.uuid4().hex
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOLS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)

CLIENTLUA_MAX_BYTES = 64 * 1024
CLIENTLUA_ACK_TIMEOUT = 15
CLIENTLUA_MAX_RECIPIENTS = 128
DIFF_FILE_MAX_BYTES = 16 * 1024 * 1024
DIFF_BATCH_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEPLOY_FILE_MAX_BYTES = 64 * 1024 * 1024
DEPLOY_BATCH_MAX_INPUT_BYTES = 256 * 1024 * 1024
GREP_MAX_PATTERNS = 20
GREP_MAX_GLOBS = 50
GREP_MAX_PATHS = 25
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_ROTATE_KEEP = 3

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Everything deployment- or machine-specific lives in config.json (copy
# config.example.json -> config.json and fill it in). NOTHING secret is baked
# into this file: the SSH private key stays on each developer's own machine and
# is referenced only by path. Resolution order (later overrides earlier):
#   1. the DEFAULTS below
#   2. config.json next to this script (or the path in $SRCDS_MCP_CONFIG)
#   3. individual SRCDS_MCP_* environment variables (handy for CI / one-offs)
#
# Almost everything below is a HOST-side fact shared by the whole team (volume
# paths, wings endpoint, server topology). The one thing each developer MUST set
# for themselves is `ssh.key` — the path to their own copy of the SSH key.
# ----------------------------------------------------------------------------
DEFAULTS = {
    "ssh": {
        "bin": "ssh",                       # "ssh" on PATH, or a full path to ssh.exe
        "key": "",                          # REQUIRED: path to YOUR SSH private key
        "known_hosts": "",                  # blank -> ~/.ssh/known_hosts
        "host": "",                         # REQUIRED: e.g. "root@your-node-ip" (ask your team)
        "port": "22",
    },
    "public_ip": "",                        # public game IP, for A2S live player counts
    "volroot": "/var/lib/pterodactyl/volumes",
    "backups_root": "/var/lib/pterodactyl/srcds_mcp_backups",
    "wings": {
        "api": "http://127.0.0.1:8081",
        "config": "/etc/pterodactyl/config.yml",
    },
    "owner_uid": 999,                       # pterodactyl:pterodactyl on the node
    "owner_gid": 987,
    "panel_url": "",                        # your Pterodactyl panel URL (shown in power errors)
    # Player count at/above which a server is "LIVE" (destructive actions warn louder).
    "live_thresholds": {"game": 1},
    # Server topology: a marker dir under garrysmod/ -> logical name. First match wins.
    # Point these at whatever uniquely identifies each of YOUR gamemodes/servers.
    "servers": [{"logical": "game", "marker": "gamemodes/example"}],
    # DB tool convenience aliases: game name -> its MariaDB schema (optional; raw
    # schema names always work too). e.g. {"game": "game_schema"}
    "db_aliases": {},
    # MongoDB (optional — leave container blank to auto-detect any container whose
    # name contains "mongo"; the mongo tools simply report "not found" if absent).
    "mongo": {
        "container": "",                    # exact docker container name, or "" to auto-detect
        "auth_db": "admin",                 # --authenticationDatabase
        "note": "",                         # shown in the tool description (e.g. host port mapping)
    },
    # Mongo convenience aliases: short name -> real mongo database name.
    "mongo_aliases": {},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_config():
    cfg = json.loads(json.dumps(DEFAULTS))     # deep copy of the defaults
    path = os.environ.get("SRCDS_MCP_CONFIG") or os.path.join(_HERE, "config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:
            sys.stderr.write("srcds-mcp: failed to read config %s: %s\n" % (path, e))
    # Flat SRCDS_MCP_* env overrides (CI / quick one-offs).
    for env_key, dst in (("SRCDS_MCP_SSH_KEY",     ("ssh", "key")),
                         ("SRCDS_MCP_SSH_BIN",     ("ssh", "bin")),
                         ("SRCDS_MCP_SSH_HOST",    ("ssh", "host")),
                         ("SRCDS_MCP_SSH_PORT",    ("ssh", "port")),
                         ("SRCDS_MCP_KNOWN_HOSTS", ("ssh", "known_hosts")),
                         ("SRCDS_MCP_PUBLIC_IP",   ("public_ip",))):
        val = os.environ.get(env_key)
        if val:
            d = cfg
            for p in dst[:-1]:
                d = d.setdefault(p, {})
            d[dst[-1]] = val
    # Drop documentation-only top-level keys (e.g. "_comment") from the example file.
    for k in [k for k in list(cfg) if k.startswith("_")]:
        cfg.pop(k, None)
    return cfg, path


CFG, CFG_PATH = _load_config()

_ssh      = CFG["ssh"]
SSH_BIN   = _ssh.get("bin") or "ssh"
SSH_KEY   = os.path.expanduser(_ssh.get("key") or "")
KNOWN_HST = os.path.expanduser(_ssh.get("known_hosts") or os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts"))
SSH_HOST  = _ssh.get("host") or ""
SSH_PORT  = str(_ssh.get("port") or "22")
PUBLIC_IP = CFG.get("public_ip") or ""
VOLROOT   = CFG.get("volroot")

# Live-traffic thresholds: player count at/above which a server is "LIVE" and
# destructive actions get a louder warning.
LIVE_THRESHOLD = CFG.get("live_thresholds") or {}

# Valid logical server names, derived from the configured topology.
SERVER_NAMES = tuple(s["logical"] for s in CFG.get("servers", []) if s.get("logical"))

# One log per config: a second registration (multi-node via SRCDS_MCP_CONFIG)
# logs beside its own config file instead of interleaving with the default's.
LOG_PATH = ((CFG_PATH + ".log") if os.environ.get("SRCDS_MCP_CONFIG")
            else os.path.join(_HERE, "srcds_mcp.log"))

SSH_BASE = [
    SSH_BIN,
    "-o", "ControlMaster=no", "-o", "ControlPath=none",
    "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
    "-o", "BatchMode=yes",
    "-o", "UserKnownHostsFile=" + KNOWN_HST,
    "-i", SSH_KEY,
    "-p", SSH_PORT, SSH_HOST,
]


def config_error():
    """Return a human-readable config problem (or None if the SSH config is usable)."""
    if not SSH_HOST:
        return "ssh.host is not set — edit config.json (copy config.example.json first)."
    if not SSH_KEY:
        return ("ssh.key is not set — point it at your SSH private key in config.json "
                "(copy config.example.json first). The key is NOT bundled; get it from the team.")
    if not os.path.isfile(SSH_KEY):
        return "ssh.key does not exist: %s — fix the path in config.json." % SSH_KEY
    return None

# ----------------------------------------------------------------------------
# Host-side driver (runs as python3 on the node). Receives one urlsafe-base64
# JSON arg. Sidesteps every layer of shell quoting.
# ----------------------------------------------------------------------------
HOST_DRIVER = r'''
import os, sys, json, base64, subprocess, pty, time, select, re, shutil

VOLROOT = @VOLROOT_JSON@
BAKROOT = @BAKROOT_JSON@                               # deploy backups, OUT of every game tree
WINGS_API = @WINGS_API_JSON@
WINGS_CONFIG = @WINGS_CONFIG_JSON@
OWNER_UID = @OWNER_UID@                               # pterodactyl:pterodactyl on the node
OWNER_GID = @OWNER_GID@
SERVERS = @SERVERS_JSON@                              # [{"logical","marker"}], first marker match wins
DIFF_FILE_MAX_BYTES = @DIFF_FILE_MAX_BYTES@
DIFF_BATCH_MAX_INPUT_BYTES = @DIFF_BATCH_MAX_INPUT_BYTES@
DEPLOY_FILE_MAX_BYTES = @DEPLOY_FILE_MAX_BYTES@
DEPLOY_BATCH_MAX_INPUT_BYTES = @DEPLOY_BATCH_MAX_INPUT_BYTES@
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")             # SGR/color escapes (console.log noise)

def jout(o):
    sys.stdout.write(json.dumps(o))
    sys.stdout.flush()

def docker_ps():
    try:
        out = subprocess.run(["docker","ps","--format","{{.ID}}|{{.Names}}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=15).stdout.decode("utf-8","replace")
    except Exception:
        return {}
    m = {}
    for line in out.splitlines():
        if "|" in line:
            i, n = line.split("|", 1)
            m[n.strip()] = i.strip()
    return m

def env_port(name):
    try:
        out = subprocess.run(
            ["docker","inspect","--format","{{range .Config.Env}}{{println .}}{{end}}", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        ).stdout.decode("utf-8","replace")
        for l in out.splitlines():
            if l.startswith("SERVER_PORT="):
                return int(l.split("=",1)[1].strip())
    except Exception:
        pass
    return None

def read_hostname(gm):
    for cfg in (gm + "/cfg/server.cfg", gm + "/cfg/gmodserver.cfg"):
        try:
            with open(cfg, "r", encoding="utf-8", errors="replace") as f:
                for l in f:
                    s = l.strip()
                    if s.lower().startswith("hostname"):
                        rest = s[len("hostname"):].strip()
                        if rest.startswith('"'):
                            end = rest.find('"', 1)
                            if end != -1:
                                return rest[1:end]
                        return rest.strip('"').strip()
        except Exception:
            pass
    return ""

def discover():
    ps = docker_ps()
    res = []
    try:
        vols = sorted(os.listdir(VOLROOT))
    except Exception as e:
        return {"error": "volroot: %s" % e}
    for u in vols:
        gm = os.path.join(VOLROOT, u, "garrysmod")
        if not os.path.isdir(gm):
            continue
        logical = None
        for entry in SERVERS:
            if os.path.isdir(gm + "/" + entry["marker"]):
                logical = entry["logical"]
                break
        if logical is None:
            continue
        log = gm + "/console.log"
        condebug = os.path.isfile(log)
        res.append({
            "logical": logical, "uuid": u, "running": (u in ps),
            "docker_id": ps.get(u), "port": (env_port(u) if u in ps else None),
            "condebug": condebug,
            "log_mtime": (os.path.getmtime(log) if condebug else None),
            "hostname": read_hostname(gm),
        })
    return {"servers": res}

def inject(cid, cmd, lead=1.0, trail=2.0, capture=False):
    # With capture=True, everything the attached pty prints during the injection
    # window is collected and returned — the no-`-condebug` output channel.
    buf = []
    buflen = [0]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("docker", ["docker","attach","--sig-proxy=false","--detach-keys=ctrl-_", cid])
    else:
        def pump(duration, until_quiet=False):
            end = time.time() + duration
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.3)
                if not r:
                    if until_quiet:
                        return
                    continue
                try:
                    d = os.read(fd, 8192)
                except OSError:
                    return
                if not d:
                    return
                if capture and buflen[0] < 400000:
                    buf.append(d)
                    buflen[0] += len(d)
        pump(lead)
        try:
            os.write(fd, (cmd + "\n").encode("utf-8"))
        except OSError:
            pass
        pump(trail)
        try:
            os.write(fd, b"\x1f")  # ctrl-_ detach
        except OSError:
            pass
        pump(3.0, until_quiet=True)   # drain what's left, stop on first quiet gap
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    return b"".join(buf).decode("utf-8", "replace") if capture else ""

def file_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return None

def read_delta(p, before, maxbytes=200000):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            # Bound the read itself. Slicing after f.read() still lets one noisy
            # command allocate the entire console delta in the root driver.
            start = min(end, max(0, before))
            if end - start > maxbytes:
                start = end - maxbytes
            f.seek(start)
            data = f.read(maxbytes).decode("utf-8", "replace")
    except OSError:
        return ""
    return data

def op_console(req):
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"; log = gm + "/console.log"
    ps = docker_ps()
    if u not in ps:
        return {"ok": False, "running": False, "error": "server not running"}
    cid = ps[u]
    condebug = os.path.isfile(log)
    before = file_size(log) if condebug else None
    # Without -condebug there is no console.log to diff, so capture the reply
    # straight off the attached pty instead (works on every server).
    cap = inject(cid, req["cmd"], req.get("lead", 1.0), req.get("trail", 2.5),
                 capture=not condebug)
    if condebug and before is not None:
        out = read_delta(log, before)
    else:
        out = cap.replace("\r\n", "\n").replace("\r", "\n")
        # drop the docker-attach detach notice, not server output
        out = "\n".join(l for l in out.splitlines() if l.strip() != "read escape sequence")
    pat = req.get("grep")
    if pat:
        out = "\n".join(l for l in out.splitlines() if pat in l)
    # Strip ANSI color noise FIRST (on chatty servers it can be a third of the
    # bytes), then byte-cap what the client actually has to read. Keep the
    # most-recent (end) slice, same policy as op_fetch.
    out = _ANSI_RE.sub("", out)
    maxb = max(1, min(int(req.get("maxbytes", 24000)), 200000))
    orig = len(out)
    truncated = orig > maxb
    if truncated:
        out = out[orig - maxb:]
        nl = out.find("\n")            # drop the leading partial line for cleanliness
        if 0 <= nl < 240:
            out = out[nl + 1:]
        out = "...[truncated: last %d of %d chars]...\n%s" % (len(out), orig, out)
    return {"ok": True, "running": True, "condebug": condebug, "output": out, "truncated": truncated,
            "note": ("" if condebug else
                     "no -condebug: reply captured live from the attached console pty; "
                     "only output inside the ~%.0fs injection window is included" % (req.get("trail", 2.5) + 3))}

def op_lua(req):
    body = req.get("body"); runner = req.get("runner"); tok = req.get("token")
    if body is None or runner is None or not tok:
        return {"ok": False, "error": "malformed lua request (missing body/runner/token) "
                "- likely a driver<->tool VERSION SKEW; restart the MCP client so both match."}
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"; log = gm + "/console.log"
    ps = docker_ps()
    if u not in ps:
        return {"ok": False, "running": False, "error": "server not running"}
    cid = ps[u]
    want_async = bool(req.get("async"))
    try:
        os.makedirs(gm + "/lua/_mcp", exist_ok=True)
    except OSError:
        pass
    body_rel = "_mcp/%s_body.lua" % tok
    run_rel  = "_mcp/%s_run.lua" % tok
    body_path = gm + "/lua/" + body_rel
    run_path  = gm + "/lua/" + run_rel
    try:
        with open(body_path, "w", encoding="utf-8") as f:
            f.write(body)            # user code, VERBATIM (1:1 line numbers)
        with open(run_path, "w", encoding="utf-8") as f:
            f.write(runner)          # rendered runner that include()s the body
        for p in (body_path, run_path):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass
    except OSError as e:
        for p in (body_path, run_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return {"ok": False, "error": "write lua: %s" % e}

    condebug = os.path.isfile(log)
    out_path = gm + "/data/_mcp/" + tok + ".txt"   # the runner file.Write's its framed output here
    try:
        os.remove(out_path)                        # clear any stale file
    except OSError:
        pass
    inject(cid, "lua_openscript " + run_rel, req.get("lead", 1.0), req.get("trail", 1.5))

    # Capture grammar:  __MCP~|~<tok>~|~KIND~|~<base64payload>   KIND in BEG/END/RET/ERR/SUM/FAIL/NOTE/DON
    MARK = "__MCP"; DELIM = "~|~"

    def field_line(line):
        i = line.find(MARK)
        if i < 0 or DELIM not in line:
            return None
        parts = line[i:].split(DELIM)
        if len(parts) >= 3 and parts[0] == MARK and parts[1] == tok:
            return (parts[2], parts[3] if len(parts) > 3 else "")
        return None

    def b64d(s):
        try:
            return base64.b64decode(s + "=" * (-len(s) % 4), validate=True).decode("utf-8", "replace")
        except Exception:
            return ""

    def parse(raw):
        started = ended = False
        out = []; fails = []
        acc = {"RET": "", "ERR": "", "SUM": ""}    # chunked channels: concat base64, decode at end
        for ln in raw.splitlines():
            fl = field_line(ln)
            if fl is None:
                continue                            # unframed line = other players' console noise; drop
            kind, pay = fl
            if kind == "BEG":
                started = True; out = []; fails = []
                acc = {"RET": "", "ERR": "", "SUM": ""}
                continue
            if not started:
                continue
            if kind in ("END", "DON"):
                ended = True; break
            if kind in acc:
                acc[kind] += pay
            elif kind == "FAIL":
                fails.append(b64d(pay))
            elif kind == "OUT":
                out.append(b64d(pay))
            elif kind == "NOTE":
                out.append("[note] " + b64d(pay))
        return {"started": started, "ended": ended, "out": "\n".join(out),
                "ret": (b64d(acc["RET"]) if acc["RET"] else None),
                "err": (b64d(acc["ERR"]) if acc["ERR"] else None),
                "sum": (b64d(acc["SUM"]) if acc["SUM"] else None),
                "fails": fails}

    res = {"started": False, "ended": False, "out": "", "ret": None, "err": None, "sum": None, "fails": []}
    deadline_s = req.get("async_timeout", 20) if want_async else req.get("capture_timeout", 8)
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if os.path.isfile(out_path):
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except OSError:
                raw = ""
            res = parse(raw)
            if res["ended"]:
                break
        time.sleep(0.25)
    if res["started"] and not res["ended"]:
        res["note"] = (("async suite did not signal MCP_DONE() within %ds; output may be partial" % deadline_s)
                       if want_async else "END marker not seen (timeout/runaway?) - output may be partial")
    elif not res["started"]:
        res["note"] = "no output file produced (server crashed mid-run, or file.Write blocked)"

    for p in (body_path, run_path, out_path):
        try:
            os.remove(p)
        except OSError:
            pass
    return {"ok": True, "running": True, "condebug": condebug, "result": res, "note": ""}

def op_fetch(req):
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"
    what = req.get("what", "console")
    lines = max(1, min(int(req.get("lines", 200)), 2000))

    if what == "history":
        return _deployment_history(req)

    if what == "dir":
        base = _safe_under(gm, req.get("path", ""))
        if not base:
            return {"ok": False, "error": "path escapes volume"}
        if not os.path.isdir(base):
            return {"ok": False, "error": "no such directory: %s" % req.get("path", "")}
        try:
            names = sorted(os.listdir(base))
        except OSError as e:
            return {"ok": False, "error": str(e)}
        ents = []
        for n in names[:500]:
            fp = os.path.join(base, n)
            try:
                st = os.stat(fp)
                ents.append({"name": n, "dir": os.path.isdir(fp),
                             "size": st.st_size, "mtime": int(st.st_mtime)})
            except OSError:
                ents.append({"name": n, "dir": False, "size": None, "mtime": None})
        return {"ok": True, "entries": ents, "total": len(names),
                "truncated": len(names) > 500}

    if what == "hash":
        import hashlib, fnmatch
        base = _safe_under(gm, req.get("path", ""))
        if not base:
            return {"ok": False, "error": "path escapes volume"}
        glob = req.get("glob") or "*"
        def sha256_of(fp):
            h = hashlib.sha256()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        files = {}
        if os.path.isfile(base):
            try:
                files[os.path.basename(base)] = [sha256_of(base), os.path.getsize(base)]
            except OSError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "files": files, "count": 1, "truncated": False}
        if not os.path.lexists(base):
            return {"ok": True, "files": {os.path.basename(base): ["missing", 0]}, "count": 1, "truncated": False}
        if not os.path.isdir(base):
            return {"ok": False, "error": "not a regular file or directory"}
        n = 0; capped = False; skipped_escaped = 0
        for root, dirs, fnames in os.walk(base):
            dirs.sort()
            for fn in sorted(fnames):
                if not fnmatch.fnmatch(fn, glob):
                    continue
                n += 1
                if n > 2000:
                    capped = True
                    break
                fp = os.path.join(root, fn)
                rel = fp[len(base):].lstrip("/")
                safe_fp = _safe_under(gm, os.path.relpath(fp, gm))
                if not safe_fp:
                    # A file symlink can escape even though os.walk's starting
                    # directory was confined. Do not hash/stat its outside target.
                    files[rel] = [None, None]
                    skipped_escaped += 1
                    continue
                try:
                    files[rel] = [sha256_of(safe_fp), os.path.getsize(safe_fp)]
                except OSError:
                    files[rel] = [None, None]
            if capped:
                break
        return {"ok": True, "files": files, "count": len(files), "truncated": capped,
                "skipped_escaped": skipped_escaped}

    if what == "backups":
        out = []
        versions = os.path.join(_guard_root(u), "versions")
        roots = ([(version, os.path.join(versions, version))
                  for version in sorted(os.listdir(versions), reverse=True)]
                 if os.path.isdir(versions) else [])
        roots.append(("legacy", os.path.join(BAKROOT, u)))
        for version, broot in roots:
            for root, dirs, fnames in os.walk(broot):
                dirs.sort()
                for fn in sorted(fnames):
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, broot).replace(os.sep, "/")
                    if not rel.startswith(req.get("path") or ""):
                        continue
                    if len(out) >= 500:
                        return {"ok": True, "backups": out, "truncated": True}
                    st = os.stat(fp)
                    out.append({"path": rel, "backup_id": version, "size": st.st_size, "mtime": int(st.st_mtime)})
        return {"ok": True, "backups": out, "truncated": False}

    if what == "docker":
        # Console output history via the docker log driver — works WITHOUT
        # -condebug and even while the server is down (covers the current
        # container's lifetime; wings recreates the container on install/boot).
        n = max(1, min(lines, 2000))
        try:
            r = subprocess.run(["docker", "logs", "--tail", str(n), u],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as e:
            return {"ok": False, "error": "docker logs failed: %s" % e}
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        if r.returncode != 0:
            return {"ok": False, "error": "docker logs rc=%d: %s" % (r.returncode, out[-300:])}
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        pat = req.get("grep")
        if pat:
            out = "\n".join(l for l in out.splitlines() if pat in l)
        out = _ANSI_RE.sub("", out)
        maxb = max(1, min(int(req.get("maxbytes", 48000)), 200000))
        orig = len(out)
        truncated = orig > maxb
        if truncated:
            out = "...[truncated: last %d of %d chars]...\n%s" % (maxb, orig, out[orig - maxb:])
        return {"ok": True, "path": "docker logs %s (tail %d)" % (u[:8], n),
                "content": out, "truncated": truncated}

    if what == "console":
        p = gm + "/console.log"
    elif what == "file":
        rel = req.get("path", "")
        p = _safe_under(gm, rel)
        if not p:
            return {"ok": False, "error": "path escapes volume"}
    else:
        return {"ok": False, "error": "unknown what: %s" % what}
    if not os.path.isfile(p):
        return {"ok": False, "exists": False, "error": "no such file: %s" % p}
    if what == "file" and req.get("b64"):
        # binary-safe download: raw bytes as base64 (the client saves them locally;
        # the payload never reaches the model). Hard size cap.
        try:
            size = os.path.getsize(p)
            cap = max(1, min(int(req.get("b64_max", 8000000)), 8000000))
            if size > cap:
                return {"ok": False, "error": "file is %d bytes (> %d download cap)" % (size, cap)}
            with open(p, "rb") as f:
                data = f.read(cap + 1)
            if len(data) > cap:
                return {"ok": False, "error": "file grew beyond %d-byte download cap while reading" % cap}
        except OSError as e:
            return {"ok": False, "error": str(e)}
        import hashlib
        return {"ok": True, "path": p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "content_b64": base64.b64encode(data).decode()}
    source_sha256 = None
    try:
        with open(p, "rb") as f:
            if what == "file":
                import hashlib
                h = hashlib.sha256()
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
                source_sha256 = h.hexdigest()
            f.seek(0, 2)
            size = f.tell()
            block = min(size, lines * 400 + 8192)
            f.seek(max(0, size - block))
            data = f.read().decode("utf-8", "replace")
        tail = "\n".join(data.splitlines()[-lines:])
    except OSError as e:
        return {"ok": False, "error": str(e)}
    pat = req.get("grep")
    if pat:
        tail = "\n".join(l for l in tail.splitlines() if pat in l)
    # Strip ANSI color/SGR escapes: console.log is littered with truecolor codes
    # (\x1b[38;2;r;g;bm ...) that spend tokens with zero semantic value.
    tail = _ANSI_RE.sub("", tail)
    # Byte-cap the payload. The line cap alone does NOT bound bytes: when the tail
    # window holds <= `lines` newlines (long / minified / JSON lines, or a run of
    # long log lines) the whole block comes back (tens of KB) instead of ~N short
    # lines -> the occasional over-return. Keep the most-recent (end) slice.
    maxb = max(1, min(int(req.get("maxbytes", 48000)), 200000))
    orig = len(tail)
    truncated = orig > maxb
    if truncated:
        tail = tail[orig - maxb:]
        nl = tail.find("\n")            # drop the leading partial line for cleanliness
        if 0 <= nl < 240:
            tail = tail[nl + 1:]
        tail = "...[truncated: last %d of %d chars]...\n%s" % (len(tail), orig, tail)
    return {"ok": True, "path": p, "content": tail, "size": size,
            "truncated": truncated, "bytes": len(tail), "sha256": source_sha256}

def _safe_under(gm, rel):
    """Resolve a path beneath root without prefix-collision or symlink escapes."""
    try:
        root = os.path.realpath(gm)
        p = os.path.realpath(os.path.join(root, str(rel or "").lstrip("/")))
        if os.path.commonpath((root, p)) == root:
            return p
    except (OSError, TypeError, ValueError):
        pass
    return None

def _atomic_path(path):
    return "%s.srcds_mcp_tmp_%d_%d" % (path, os.getpid(), time.time_ns())

def _finish_owned_file(tmp, path):
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    try:
        os.chown(tmp, OWNER_UID, OWNER_GID)
    except (OSError, AttributeError):
        pass
    os.replace(tmp, path)
    _sync_parent(path)

def _atomic_copy(src, dst, owned=False):
    d = os.path.dirname(dst)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = _atomic_path(dst)
    try:
        shutil.copyfile(src, tmp)
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
        if owned:
            _finish_owned_file(tmp, dst)
        else:
            os.replace(tmp, dst)
            _sync_parent(dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def _atomic_write(path, data):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = _atomic_path(path)
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _finish_owned_file(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def _sha256_file(path):
    import hashlib
    if not os.path.lexists(path):
        return "missing"
    if not os.path.isfile(path):
        raise ValueError("destination is not a regular file")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _valid_expected(value):
    return isinstance(value, str) and (value == "missing" or re.fullmatch(r"[0-9a-f]{64}", value) is not None)

def _deploy_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("destination must be a non-empty garrysmod-relative path using '/' separators")
    if value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("destination cannot contain absolute, empty, '.' or '..' components")
    return value

def _guard_root(u):
    if not isinstance(u, str) or re.fullmatch(r"[A-Za-z0-9_-]+", u) is None:
        raise ValueError("invalid volume identifier")
    root = _safe_under(BAKROOT, "_guard_v2/" + u)
    if not root:
        raise ValueError("guard root escapes backup root")
    return root

class _DeployLock:
    """One advisory lock per volume, shared by all v2 driver processes."""
    def __init__(self, root):
        self.root = root
        self.f = None

    def __enter__(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.f = open(os.path.join(self.root, "deploy.lock"), "a+b")
        if os.name == "nt":
            import msvcrt
            self.f.seek(0, 2)
            if not self.f.tell():
                self.f.write(b"0")
                self.f.flush()
        else:
            import fcntl
        deadline = time.monotonic() + 15
        while True:
            try:
                if os.name == "nt":
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.f.close()
                    self.f = None
                    raise TimeoutError("deployment lock busy; no files written")
                time.sleep(0.05)

    def __exit__(self, *unused):
        if self.f is not None:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            self.f.close()

def _sync_parent(path):
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

def _audit_record(root, deploy_id, phase, record):
    """Immutable durable metadata; never serialize a request or file contents."""
    history = os.path.join(root, "history")
    os.makedirs(history, mode=0o700, exist_ok=True)
    path = os.path.join(history, deploy_id + "." + phase + ".json")
    data = json.dumps(record, ensure_ascii=True, sort_keys=True).encode("utf-8")
    tmp = _atomic_path(path)
    try:
        with open(tmp, "xb") as f:
            os.chmod(tmp, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # The UUID suffix makes collisions negligible; never replace a receipt.
        if os.path.exists(path):
            raise FileExistsError("deployment receipt already exists")
        os.replace(tmp, path)
        _sync_parent(path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def _deployment_target(req):
    """Fresh marker resolution without Docker state or the client's discovery cache."""
    logical = req.get("server")
    if logical not in [s.get("logical") for s in SERVERS]:
        raise ValueError("DEPLOY_TARGET_REQUIRED: upgraded client must supply a configured logical server")
    matches = []
    for u in sorted(os.listdir(VOLROOT)):
        gm = os.path.join(VOLROOT, u, "garrysmod")
        if not os.path.isdir(gm):
            continue
        # Preserve the configured first-marker rule used by read-only discovery.
        matched = next((s.get("logical") for s in SERVERS
                        if os.path.isdir(os.path.join(gm, s["marker"]))), None)
        if matched == logical:
            matches.append(u)
    if len(matches) != 1 or matches[0] != req.get("uuid"):
        raise ValueError("DEPLOY_TARGET_CHANGED_OR_AMBIGUOUS: fresh logical-to-volume mapping is not unique or has changed")
    gm = _safe_under(VOLROOT, matches[0] + "/garrysmod")
    if not gm:
        raise ValueError("volume escapes configured volume root")
    return gm

def _restore_path(root, u, to, backup_id):
    if backup_id == "legacy":
        path = _safe_under(os.path.join(BAKROOT, u), to)
    elif isinstance(backup_id, str) and re.fullmatch(r"[0-9]{20}-[0-9a-f]{16}", backup_id):
        path = _safe_under(root, "versions/" + backup_id + "/" + to)
        with open(os.path.join(root, "history", backup_id + ".prepared.json"), encoding="utf-8") as f:
            record = json.load(f)
        entry = next((e for e in record.get("files", []) if e.get("to") == to), None)
        if not entry or entry.get("backup_id") != backup_id or _sha256_file(path) != entry.get("before_sha256"):
            raise ValueError("backup is missing or does not match its recorded SHA-256")
    else:
        raise ValueError("restore requires an explicit backup_id from fetch history/backups, or 'legacy'")
    if not path or not os.path.isfile(path):
        raise ValueError("requested backup does not exist")
    return path

def _deployment_history(req):
    root = _guard_root(req["uuid"])
    history = os.path.join(root, "history")
    prefix = req.get("path") or ""
    limit = max(1, min(int(req.get("lines", 20)), 100))
    before = req.get("before") or "~"
    records = []
    try:
        names = sorted(os.listdir(history), reverse=True)
    except FileNotFoundError:
        names = []
    used = 0
    cursor = None
    for name in names:
        if not name.endswith(".json") or name >= before:
            continue
        with open(os.path.join(history, name), encoding="utf-8") as f:
            record = json.load(f)
        if prefix:
            record["files"] = [e for e in record.get("files", []) if e.get("to", "").startswith(prefix)]
            if not record["files"]:
                continue
        size = len(json.dumps(record))
        if records and (len(records) >= limit or used + size > 180000):
            break
        records.append(record)
        used += size
        cursor = name
    return {"ok": True, "history": records, "before": cursor,
            "note": "prepared without a result means interrupted or uncertain; inspect live hashes before retrying"}

def op_deploy(req):
    import hashlib, uuid
    deploy_id = "%020d-%s" % (time.time_ns(), uuid.uuid4().hex[:16])
    batch = req.get("files") is not None
    files = req.get("files") if batch else [req]
    if not isinstance(files, list) or not files or len(files) > 400:
        return {"ok": False, "error": "files must contain 1-400 entries"}
    if req.get("backup", True) is not True:
        return {"ok": False, "error": "versioned backups are mandatory; backup=false is no longer supported"}
    restore = req.get("restore") is True
    plans, metadata, seen = [], [], set()
    total = 0
    try:
        root = _guard_root(req.get("uuid"))
        for f in files:
            if not isinstance(f, dict):
                raise ValueError("every files item must be an object")
            to = _deploy_relative(f.get("to"))
            expected = f.get("expected_sha256")
            if not _valid_expected(expected):
                raise ValueError("STALE_BASE_REQUIRED: %s needs expected_sha256 of its original remote base, or 'missing' for creation" % to)
            meta = {"to": to, "expected_sha256": expected,
                    "source_path": str(f.get("source_path") or "inline")[:2048]}
            plan = {"to": to, "expected": expected, "meta": meta}
            if restore:
                plan["restore_id"] = f.get("backup_id")
                meta["restore_from"] = f.get("backup_id")
            else:
                data = base64.b64decode(f["content_b64"], validate=True)
                total += len(data)
                if len(data) > DEPLOY_FILE_MAX_BYTES or total > DEPLOY_BATCH_MAX_INPUT_BYTES:
                    raise ValueError("deploy file or aggregate payload cap exceeded")
                plan["data"] = data
                meta["after_sha256"] = hashlib.sha256(data).hexdigest()
                meta["bytes"] = len(data)
            plans.append(plan)
            metadata.append(meta)
        record = {"protocol": 2, "deployment_id": deploy_id, "time_ns": time.time_ns(),
                  "server": req.get("server"), "uuid": req.get("uuid"),
                  "operation": "restore" if restore else "deploy", "files": metadata,
                  "origin": {k: str((req.get("origin") or {}).get(k) or "")[:256]
                             for k in ("client_instance", "pid", "tool_version", "thread_id", "source_revision")}}
        with _DeployLock(root):
            try:
                gm = _deployment_target(req)
                conflicts = []
                for p in plans:
                    path = _safe_under(gm, p["to"])
                    if not path or path == os.path.realpath(gm):
                        raise ValueError("path escapes volume")
                    if path in seen:
                        raise ValueError("duplicate canonical destination: %s" % p["to"])
                    seen.add(path)
                    p["path"] = path
                    current = _sha256_file(path)
                    p["meta"]["before_sha256"] = current
                    if current != p["expected"]:
                        conflicts.append({"to": p["to"], "expected_sha256": p["expected"], "actual_sha256": current})
                    if restore:
                        p["restore_path"] = _restore_path(root, req["uuid"], p["to"], p["restore_id"])
                        p["meta"]["after_sha256"] = _sha256_file(p["restore_path"])
                        p["meta"]["bytes"] = os.path.getsize(p["restore_path"])
                    p["noop"] = current == p["meta"]["after_sha256"]
                    if current != "missing" and not p["noop"]:
                        p["backup"] = _safe_under(root, "versions/" + deploy_id + "/" + p["to"])
                        if not p["backup"]:
                            raise ValueError("backup path escapes root")
                        p["meta"]["backup_id"] = deploy_id
                if conflicts:
                    record.update({"phase": "rejected", "conflicts": conflicts})
                    _audit_record(root, deploy_id, "rejected", record)
                    return {"ok": False, "deployment_id": deploy_id, "conflicts": conflicts,
                            "error": "STALE_BASE: entire batch rejected; re-fetch and reconcile changes, then use the reconciled base hash. Do not attach a fresh hash to stale content."}
                record["phase"] = "prepared"
                _audit_record(root, deploy_id, "prepared", record)
                # Preserve ALL previous versions before changing any target.
                for p in plans:
                    if p.get("backup"):
                        _atomic_copy(p["path"], p["backup"])
                        if _sha256_file(p["backup"]) != p["expected"]:
                            raise ValueError("source changed during backup; no target files written")
            except Exception as e:
                record.update({"phase": "rejected", "error": str(e)})
                _audit_record(root, deploy_id, "rejected", record)
                return {"ok": False, "deployment_id": deploy_id, "error": str(e)}
            results = []
            try:
                for p in plans:
                    # Detect a non-MCP writer before each replace; such writers do not honor our lock.
                    if _safe_under(gm, p["to"]) != p["path"] or _sha256_file(p["path"]) != p["expected"]:
                        raise ValueError("destination changed outside the deployment lock")
                    if not p["noop"]:
                        if restore:
                            if _sha256_file(p["restore_path"]) != p["meta"]["after_sha256"]:
                                raise ValueError("restore source changed after preflight")
                            _atomic_copy(p["restore_path"], p["path"], owned=True)
                        else:
                            _atomic_write(p["path"], p["data"])
                    actual = _sha256_file(p["path"])
                    if actual != p["meta"]["after_sha256"]:
                        raise ValueError("post-write verification failed")
                    results.append(dict(p["meta"], ok=True, noop=p["noop"], backup=p.get("backup"),
                                        overwrote=p["expected"] != "missing", restored=restore))
                outcome = "complete"
                error = None
            except Exception as e:
                outcome = "partial_or_uncertain"
                error = str(e)
                for p in plans[len(results):]:
                    results.append(dict(p["meta"], ok=False, error="not confirmed: " + error))
            result = dict(record, phase="result", outcome=outcome, files=results,
                          ok=outcome == "complete", error=error, batch=batch, results=results,
                          n_ok=sum(1 for r in results if r["ok"]),
                          n_fail=sum(1 for r in results if not r["ok"]),
                          bytes=sum(r.get("bytes", 0) for r in results if r["ok"] and not r.get("noop")))
            try:
                _audit_record(root, deploy_id, "result", {k: v for k, v in result.items() if k != "results"})
            except Exception as e:
                result.update(ok=False, outcome="partial_or_uncertain",
                              error="receipt failed after possible writes; inspect history and hashes before retrying: %s" % e)
            if not batch and results:
                for k, v in results[0].items():
                    if k not in result:
                        result[k] = v
            return result
    except Exception as e:
        return {"ok": False, "deployment_id": deploy_id, "error": str(e)}


def op_grep(req):
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"
    patterns = req.get("patterns") or ([req.get("pattern")] if req.get("pattern") else [])
    globs = req.get("globs") or ([req.get("glob")] if req.get("glob") else ["*.lua"])
    exclude_globs = req.get("exclude_globs") or []
    rel_paths = req.get("paths") or [req.get("path", "")]
    if not patterns or any(not isinstance(x, str) or not x for x in patterns):
        return {"ok": False, "error": "at least one non-empty grep pattern is required"}
    if any(not isinstance(x, str) or not x for x in globs + exclude_globs):
        return {"ok": False, "error": "grep globs must be non-empty strings"}
    bases = []
    for rel in rel_paths:
        base = _safe_under(gm, rel)
        if not base:
            return {"ok": False, "error": "path escapes volume: %s" % rel}
        if not os.path.exists(base):
            return {"ok": False, "error": "no such path: %s" % rel}
        bases.append(base)
    mx = max(1, min(int(req.get("max", 200)), 2000))
    cmd = ["grep", "-rnI"]
    for glob in globs:
        cmd += ["--include", glob]
    for glob in exclude_globs:
        cmd += ["--exclude", glob]
    for pattern in patterns:
        cmd += ["-e", pattern]
    cmd += bases
    # Cap grep at the pipe, not after communicate(): a broad/minified match must
    # never allocate an unbounded stdout buffer in the root host driver.
    capture_cap = 50000
    g = h = None
    try:
        g = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        h = subprocess.Popen(["head", "-c", str(capture_cap + 1)], stdin=g.stdout,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        g.stdout.close()
        raw, _ = h.communicate(timeout=30)
        byte_capped = len(raw) > capture_cap
        raw = raw[:capture_cap]
        if byte_capped and g.poll() is None:
            g.terminate()
        try:
            g.wait(timeout=3)
        except subprocess.TimeoutExpired:
            g.kill(); g.wait()
        err = g.stderr.read().decode("utf-8", "replace")[-500:]
        if not byte_capped and g.returncode not in (0, 1):
            return {"ok": False, "error": "grep rc=%s: %s" % (g.returncode, err)}
        out = raw.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        for p in (h, g):
            if p is not None and p.poll() is None:
                p.kill()
        return {"ok": False, "error": "grep timed out after 30s"}
    except Exception as e:
        for p in (h, g):
            if p is not None and p.poll() is None:
                p.kill()
        return {"ok": False, "error": "grep failed: %s" % e}
    lines = out.splitlines()
    total = len(lines)
    pref = os.path.realpath(gm) + os.sep
    # Cap each line (a match inside a minified/packed line would otherwise return
    # the WHOLE line) and the total payload, so one grep can't flood the client.
    shown, used, capped = [], 0, False
    for l in lines[:mx]:
        l = l.replace(pref, "")
        if len(l) > 300:
            l = l[:300] + "...<+%d chars>" % (len(l) - 300)
        if used + len(l) + 1 > 40000:
            capped = True
            break
        used += len(l) + 1
        shown.append(l)
    capped = capped or byte_capped or total > mx
    r = {"ok": True, "matches": shown, "total": total, "shown": len(shown),
         "total_exact": not byte_capped, "truncated": capped}
    if capped:
        r["note"] = "capture-capped; narrow paths/globs/patterns for a complete result"
    return r

def _diff_one(req, diff_cap=40000, batch=False):
    import difflib, hashlib
    file_cap = max(1, min(int(req.get("file_max_bytes", DIFF_FILE_MAX_BYTES)), DIFF_FILE_MAX_BYTES))
    def readside(u, rel):
        gm = VOLROOT + "/" + u + "/garrysmod"
        p = _safe_under(gm, rel)
        if not p:
            return None, "path escapes volume"
        if not os.path.isfile(p):
            return None, "no such file: %s" % rel
        try:
            size = os.path.getsize(p)
            if size > file_cap:
                return None, "file is %d bytes (> %d diff cap)" % (size, file_cap)
            with open(p, "rb") as f:
                return f.read(), None
        except OSError as e:
            return None, str(e)
    a, err = readside(req["uuid_a"], req["path_a"])
    if err:
        return {"ok": False, "error": "A(%s): %s" % (req.get("label_a", "a"), err)}
    if req.get("content_b64") is not None:
        try:
            b = base64.b64decode(req["content_b64"], validate=True)
        except Exception as e:
            return {"ok": False, "error": "bad local content: %s" % e}
        if len(b) > file_cap:
            return {"ok": False, "error": "local file is %d bytes (> %d diff cap)" % (len(b), file_cap)}
    else:
        b, err = readside(req["uuid_b"], req["path_b"])
        if err:
            return {"ok": False, "error": "B(%s): %s" % (req.get("label_b", "b"), err)}
    meta = {"ok": True, "equal": a == b, "size_a": len(a), "size_b": len(b),
            "sha_a": hashlib.sha1(a).hexdigest()[:12], "sha_b": hashlib.sha1(b).hexdigest()[:12],
            "sha256_a": hashlib.sha256(a).hexdigest(), "sha256_b": hashlib.sha256(b).hexdigest()}
    if meta["equal"]:
        return meta
    if b"\x00" in a[:8192] or b"\x00" in b[:8192]:
        meta["binary"] = True
        return meta
    diff_cap = max(0, int(diff_cap))
    suffix = ("\n...[diff truncated for batch output budget]" if batch
              else "\n...[diff truncated at 40KB]")
    parts = []
    used = 0
    for piece in difflib.unified_diff(
            a.decode("utf-8", "replace").splitlines(True),
            b.decode("utf-8", "replace").splitlines(True),
            fromfile=req.get("label_a", "a"), tofile=req.get("label_b", "b"),
            n=int(req.get("context", 3))):
        if used + len(piece) > diff_cap:
            meta["truncated"] = True
            room = max(0, diff_cap - used)
            if room:
                parts.append(piece[:room])
            break
        parts.append(piece)
        used += len(piece)
    d = "".join(parts)
    if meta.get("truncated"):
        if diff_cap <= len(suffix):
            d = suffix[:diff_cap]
        else:
            d = d[:diff_cap - len(suffix)] + suffix
    meta["diff"] = d
    return meta

def op_diff(req):
    files = req.get("files")
    if files is None:
        return _diff_one(req, 40000)
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "files must be a non-empty array"}
    if len(files) > 200:
        return {"ok": False, "error": "batch has %d files (max 200)" % len(files)}
    try:
        maxbytes = max(0, min(int(req.get("maxbytes", 48000)), 200000))
    except (TypeError, ValueError):
        return {"ok": False, "error": "maxbytes must be an integer"}
    # Preflight every side before reading/diffing so a 200-file request cannot turn
    # the output cap into an unbounded input/CPU budget.
    input_bytes = 0
    for f in files:
        if not isinstance(f, dict):
            return {"ok": False, "error": "every files item must be an object"}
        for side in ("a", "b"):
            if side == "b" and f.get("content_b64") is not None:
                raw = f.get("content_b64") or ""
                size = max(0, (len(raw.rstrip("=")) * 3) // 4)
            else:
                uk, pk = ("uuid_a", "path_a") if side == "a" else ("uuid_b", "path_b")
                if not f.get(uk) or f.get(pk) is None:
                    return {"ok": False, "error": "missing %s side" % side.upper()}
                root = VOLROOT + "/" + f[uk] + "/garrysmod"
                p = _safe_under(root, f[pk])
                if not p or not os.path.isfile(p):
                    return {"ok": False, "error": "%s side missing/escaped: %s" % (side.upper(), f.get(pk))}
                try:
                    size = os.path.getsize(p)
                except OSError as e:
                    return {"ok": False, "error": "%s side stat: %s" % (side.upper(), e)}
            if size > DIFF_FILE_MAX_BYTES:
                return {"ok": False, "error": "%s side is %d bytes (> %d per-file diff cap)" %
                        (side.upper(), size, DIFF_FILE_MAX_BYTES)}
            input_bytes += size
            if input_bytes > DIFF_BATCH_MAX_INPUT_BYTES:
                return {"ok": False, "error": "batch input exceeds %d-byte diff cap" %
                        DIFF_BATCH_MAX_INPUT_BYTES}
    # Divide the aggregate diff budget fairly so one large file cannot hide all
    # later comparisons. Metadata for every item is always returned.
    per_file_cap = min(40000, maxbytes // len(files)) if maxbytes else 0
    results = []
    for f in files:
        try:
            results.append(_diff_one(f, per_file_cap, batch=True))
        except Exception as e:
            results.append({"ok": False, "error": "diff exception: %s" % e})
    n_fail = sum(1 for r in results if not r.get("ok"))
    n_equal = sum(1 for r in results if r.get("ok") and r.get("equal"))
    n_differ = len(results) - n_fail - n_equal
    return {"ok": True, "batch": True, "results": results,
            "n_equal": n_equal, "n_differ": n_differ, "n_fail": n_fail,
            "n_binary": sum(1 for r in results if r.get("ok") and r.get("binary")),
            "n_truncated": sum(1 for r in results if r.get("ok") and r.get("truncated")),
            "maxbytes": maxbytes, "input_bytes": input_bytes}

def op_nodeinfo(req):
    info = {}
    try:
        with open("/proc/loadavg") as f:
            info["loadavg"] = f.read().split()[:3]
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for l in f:
                k = l.split(":")[0]
                if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    mem[k] = int(l.split()[1]) // 1024
        info["mem_mb"] = mem
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            info["uptime_h"] = round(float(f.read().split()[0]) / 3600, 1)
    except Exception:
        pass
    try:
        r = subprocess.run(["df", "-hP", "/", VOLROOT], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15)
        info["disk"] = r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        info["disk"] = "df failed: %s" % e
    try:
        r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                            "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        info["docker"] = r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        info["docker"] = "docker stats failed: %s" % e
    wl = int(req.get("wings_log_lines", 0))
    if wl > 0:
        try:
            r = subprocess.run(["sh", "-c", "tail -n %d /var/log/pterodactyl/wings.log" % min(wl, 200)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            info["wings_log"] = r.stdout.decode("utf-8", "replace")[-20000:]
        except Exception as e:
            info["wings_log"] = "tail failed: %s" % e
    dl = int(req.get("dmesg_lines", 0))
    if dl > 0:
        try:
            r = subprocess.run(["sh", "-c", "dmesg -T | tail -n %d" % min(dl, 100)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            info["dmesg"] = r.stdout.decode("utf-8", "replace")[-15000:]
        except Exception as e:
            info["dmesg"] = "dmesg failed: %s" % e
    return {"ok": True, "info": info}

def _wings_token():
    # The wings API bearer token lives at top-level `token:` in the wings config.
    # Read here, used only for the localhost API call, NEVER returned/logged.
    try:
        with open(WINGS_CONFIG) as f:
            for line in f:
                if line.startswith("token:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None

def op_power(req):
    u = req["uuid"]; action = req["action"]
    if action not in ("start", "stop", "restart", "kill"):
        return {"ok": False, "error": "bad action"}
    # Route power through the wings API (same as the panel buttons) rather than
    # `docker`. A wings-initiated stop/restart is a NORMAL shutdown: wings sets its
    # stopping flag, srcds receives a graceful `quit` (exit 0), and crash detection
    # is suppressed. Calling `docker stop/kill` bypasses wings, so it sees an
    # unexpected container death -> "detected server as entering a crashed state",
    # and with detect_clean_exit_as_crash=true + the crash-loop rate-limit it then
    # "did not restart server after crash; occurred too soon" -> stays DOWN. wings
    # `start` also recreates a removed container, which raw `docker start` cannot.
    token = _wings_token()
    if token:
        import urllib.request, urllib.error
        data = json.dumps({"action": action}).encode("utf-8")
        rq = urllib.request.Request(
            WINGS_API + ("/api/servers/%s/power" % u),
            data=data, method="POST",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            resp = urllib.request.urlopen(rq, timeout=90)
            code = resp.getcode()
            return {"ok": code in (200, 202, 204), "rc": code, "via": "wings",
                    "out": "wings %s accepted (HTTP %d) - graceful, panel state stays in sync." % (action, code)}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            return {"ok": False, "rc": e.code, "via": "wings",
                    "error": "wings power %s -> HTTP %d %s" % (action, e.code, detail)}
        except Exception as e:
            wings_err = "wings API unreachable: %s" % e
    else:
        wings_err = "wings token not found in /etc/pterodactyl/config.yml"
    # Fallback: raw docker (bypasses wings crash accounting -> last resort only).
    try:
        r = subprocess.run(["docker", action, u], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=90)
    except Exception as e:
        return {"ok": False, "error": "%s; docker %s fallback also failed: %s" % (wings_err, action, e)}
    msg = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).strip()
    return {"ok": r.returncode == 0, "rc": r.returncode, "via": "docker",
            "out": "docker %s fallback (%s): %s" % (action, wings_err, msg[-300:])}

# --- boot watcher -------------------------------------------------------------
# Boot-complete detection is done at the PTERODACTYL end: wings flips the server
# state "starting" -> "running" when the egg's startup-done marker appears in the
# console. The watcher is a tiny detached process that polls wings' per-server
# state and records the transition to /tmp, so an MCP client can (long-)poll for
# "BOOT COMPLETE" without babysitting the console itself.
WATCH_SRC = """
import sys, json, time, re, urllib.request
u, api, cfgpath, out, action = sys.argv[1:6]
def token():
    try:
        for line in open(cfgpath):
            if line.startswith("token:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None
def state():
    tok = token()
    if not tok:
        return None
    try:
        rq = urllib.request.Request(api + "/api/servers/" + u,
                                    headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        b = urllib.request.urlopen(rq, timeout=8).read().decode("utf-8", "replace")
        m = re.search(r'"state"\\s*:\\s*"([a-z]+)"', b)
        return m.group(1) if m else None
    except Exception:
        return None
def write(d):
    try:
        with open(out, "w") as f:
            json.dump(d, f)
    except Exception:
        pass
t0 = time.time(); last = None; seen_start = False
d = {"armed": t0, "action": action, "phase": "watching", "state": None, "history": []}
write(d)
while time.time() < t0 + 900:
    s = state()
    if s != last:
        last = s
        d["state"] = s
        d["history"].append([round(time.time() - t0, 1), s])
        if s in ("starting", "running"):
            seen_start = True
        if s == "running":
            d["phase"] = "booted"; d["t_boot"] = round(time.time() - t0, 1)
            write(d); sys.exit(0)
        if s == "offline" and seen_start:
            d["phase"] = "died_during_boot"; write(d); sys.exit(0)
        write(d)
    time.sleep(2)
d["phase"] = "timeout"
write(d)
"""

def _watch_path(u):
    return "/tmp/srcds_bootwatch_%s.json" % u

def op_bootwatch(req):
    u = req["uuid"]; mode = req.get("mode", "poll")
    out = _watch_path(u)
    if mode == "arm":
        try:
            os.remove(out)                       # clear a stale verdict from a previous boot
        except OSError:
            pass
        try:
            subprocess.Popen(["python3", "-c", WATCH_SRC, u, WINGS_API, WINGS_CONFIG, out,
                              req.get("action", "?")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return {"ok": False, "error": "arm watcher: %s" % e}
        return {"ok": True, "armed": True}
    # poll: optionally long-poll (server-side) until the watcher reaches a verdict.
    deadline = time.time() + min(float(req.get("wait", 0)), 55)
    d = None
    while True:
        try:
            with open(out) as f:
                d = json.load(f)
        except Exception:
            d = None
        if d and d.get("phase") != "watching":
            break
        if time.time() >= deadline:
            break
        time.sleep(2)
    if d and "armed" in d:
        d["elapsed"] = round(time.time() - d["armed"], 1)   # node-side clock, no skew
    # live wings state too, so a dead/never-armed watcher still yields an answer
    live = None; live_err = None
    token = _wings_token()
    if not token:
        live_err = "wings token not found"
    else:
        import urllib.request
        rq = urllib.request.Request(WINGS_API + "/api/servers/" + u,
                                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            body = urllib.request.urlopen(rq, timeout=10).read().decode("utf-8", "replace")
            m = re.search(r'"state"\s*:\s*"([a-z]+)"', body)
            live = m.group(1) if m else None
        except Exception as e:
            live_err = "wings state: %s" % e
    return {"ok": True, "watch": d, "state": live, "state_err": live_err}

# --- generic monitor ----------------------------------------------------------
# srcds_monitor generalizes the boot watcher: a tiny detached process either
# follows the container console (docker logs -f, works on every server with no
# -condebug) for a regex, or polls wings state for an up/down transition (down
# also captures the last console lines at death - crash forensics). Verdict goes
# to /tmp/srcds_monitor_<uuid>_<id>.json; MCP has no push channel, so the
# returning check call IS the notification.
MONITOR_SRC = """
import sys, json, time, re, os, subprocess, urllib.request
u, mode, pattern, out, timeout_s, api, cfgpath, nonce = sys.argv[1:9]
timeout_s = float(timeout_s)
ANSI = re.compile("\\x1b\\\\[[0-9;]*[A-Za-z]")
def proc_start(pid):
    try:
        raw=open("/proc/%d/stat" % pid).read()
        return raw.rsplit(")",1)[1].split()[19]
    except Exception:
        return None
d = {"pid": os.getpid(), "pid_start": proc_start(os.getpid()), "nonce": nonce,
     "mode": mode, "pattern": pattern, "armed": time.time(),
     "phase": "watching", "match_count": 0, "matches": [], "history": [], "note": ""}
def write():
    try:
        with open(out, "w") as f:
            json.dump(d, f)
        os.chmod(out, 0o600)
    except Exception:
        pass
def token():
    try:
        for line in open(cfgpath):
            if line.startswith("token:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None
def wstate():
    tok = token()
    if not tok:
        return None
    try:
        rq = urllib.request.Request(api + "/api/servers/" + u,
                                    headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        b = urllib.request.urlopen(rq, timeout=8).read().decode("utf-8", "replace")
        m = re.search('"state"\\\\s*:\\\\s*"([a-z]+)"', b)
        return m.group(1) if m else None
    except Exception:
        return None
def tail_lines(n):
    try:
        r = subprocess.run(["docker", "logs", "--tail", str(n), u],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        ls = ANSI.sub("", r.stdout.decode("utf-8", "replace")).splitlines()
        return [l.rstrip()[:300] for l in ls[-n:]]
    except Exception as e:
        return ["<tail failed: %s>" % e]
t0 = time.time(); write()
if mode == "pattern":
    rx = re.compile(pattern)
    lastwrite = t0
    while time.time() < t0 + timeout_s:
        try:
            p = subprocess.Popen(["docker", "logs", "-f", "--tail", "0", u],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            d["note"] = "docker logs: %s" % e; write(); time.sleep(5); continue
        for raw in p.stdout:
            if time.time() > t0 + timeout_s:
                break
            line = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
            if rx.search(line):
                d["match_count"] += 1
                d["matches"].append([round(time.time() - t0, 1), line[:300]])
                d["matches"] = d["matches"][-50:]
                if d["phase"] == "watching":
                    d["phase"] = "triggered"
                write(); lastwrite = time.time()
            elif time.time() - lastwrite > 10:
                write(); lastwrite = time.time()      # heartbeat
        try:
            p.kill()
        except Exception:
            pass
        if time.time() >= t0 + timeout_s:
            break
        d["note"] = "log stream ended (container stop/restart?) - re-attaching"
        write(); time.sleep(4)
else:  # mode "down" / "up" - wings state transition (one-shot)
    last = None
    lastwrite = t0
    while time.time() < t0 + timeout_s:
        s = wstate()
        if s != last:
            d["history"].append([round(time.time() - t0, 1), s])
            d["history"] = d["history"][-20:]
            trig = (mode == "down" and s == "offline" and last not in (None, "offline")) or \\
                   (mode == "up" and s == "running" and last is not None and last != "running")
            last = s
            if trig:
                d["phase"] = "triggered"; d["t_event"] = round(time.time() - t0, 1)
                if mode == "down":
                    d["context"] = tail_lines(40)
                write(); sys.exit(0)
            write(); lastwrite = time.time()
        elif time.time() - lastwrite > 15:
            write(); lastwrite = time.time()          # heartbeat
        time.sleep(4)
if d["phase"] == "watching":
    d["phase"] = "expired"
else:
    d["phase"] = "done"
write()
"""

MONITOR_ROOT = "/tmp/srcds_mcp_monitors"

def _ensure_monitor_root():
    try:
        os.makedirs(MONITOR_ROOT, mode=0o700, exist_ok=True)
        os.chmod(MONITOR_ROOT, 0o700)
        return True
    except OSError:
        return False

def _mon_path(u, mid):
    return MONITOR_ROOT + "/%s_%s.json" % (u, mid)

def _proc_start(pid):
    try:
        raw = open("/proc/%d/stat" % pid).read()
        return raw.rsplit(")", 1)[1].split()[19]
    except Exception:
        return None

def op_monitor(req):
    import glob as _glob
    u = req["uuid"]; act = req.get("act", "list")
    if not _ensure_monitor_root():
        return {"ok": False, "error": "could not create private monitor state directory"}
    for f in _glob.glob(MONITOR_ROOT + "/%s_*.json" % u):   # GC day-old verdicts
        try:
            if time.time() - os.path.getmtime(f) > 86400:
                os.remove(f)
        except OSError:
            pass
    if act == "arm":
        mid = req["id"]
        out = _mon_path(u, mid)
        nonce = os.urandom(16).hex()
        try:
            subprocess.Popen(["python3", "-c", MONITOR_SRC, u, req["mode"],
                              req.get("pattern") or "", out,
                              str(float(req.get("timeout_s", 1800))), WINGS_API, WINGS_CONFIG, nonce],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return {"ok": False, "error": "arm monitor: %s" % e}
        for _ in range(10):          # confirm the watcher actually came up
            if os.path.isfile(out):
                break
            time.sleep(0.2)
        return {"ok": True, "armed": True, "id": mid, "statefile": os.path.isfile(out)}
    if act == "list":
        mons = []
        for f in sorted(_glob.glob(MONITOR_ROOT + "/%s_*.json" % u)):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            mons.append({"id": f.rsplit("_", 1)[1][:-5], "mode": d.get("mode"),
                         "pattern": (d.get("pattern") or "")[:60], "phase": d.get("phase"),
                         "matches": d.get("match_count", 0),
                         "age_s": round(time.time() - d.get("armed", time.time()), 1)})
        return {"ok": True, "monitors": mons}
    mid = req.get("id") or ""
    if not re.match(r"^[a-f0-9]{4,16}$", mid):
        return {"ok": False, "error": "bad/missing monitor id"}
    out = _mon_path(u, mid)
    def read():
        try:
            with open(out) as f:
                return json.load(f)
        except Exception:
            return None
    if act == "stop":
        d = read()
        if not d:
            return {"ok": False, "error": "no such monitor %s" % mid}
        # The watcher runs start_new_session=True, so it leads its own process
        # group; kill the GROUP so a pattern-mode `docker logs -f` child dies
        # with it instead of following the console as an orphan forever.
        killed = False
        if d.get("phase") in ("watching", "triggered"):
            try:
                pid = int(d.get("pid", 0))
                nonce = str(d.get("nonce") or "")
                current_start = _proc_start(pid)
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    cmdline = f.read().decode("utf-8", "replace")
                identity_ok = (pid > 1 and current_start and current_start == str(d.get("pid_start"))
                               and nonce and nonce in cmdline and os.getpgid(pid) == pid)
                if not identity_ok:
                    return {"ok": False, "error": "monitor process identity no longer matches; refusing to signal PID"}
                try:
                    os.killpg(pid, 15)
                except (OSError, AttributeError):
                    os.kill(pid, 15)
                killed = True
                d["phase"] = "stopped"
            except Exception as e:
                return {"ok": False, "error": "could not safely stop monitor: %s" % e}
        try:
            with open(out, "w") as f:
                json.dump(d, f)
        except OSError:
            pass
        return {"ok": True, "watch": d, "killed": killed}
    # act "check": long-poll until a hit / final phase / deadline
    deadline = time.time() + min(float(req.get("wait", 0)), 55)
    after = int(req.get("after", 0))
    while True:
        d = read()
        if d:
            ph = d.get("phase")
            if ph in ("expired", "stopped", "done"):
                break
            if d.get("mode") == "pattern":
                if d.get("match_count", 0) > after:
                    break
            elif ph != "watching":
                break
        if time.time() >= deadline:
            break
        time.sleep(2)
    if not d:
        return {"ok": False, "error": "no such monitor %s (verdicts GC after 24h)" % mid}
    d["elapsed"] = round(time.time() - d.get("armed", time.time()), 1)
    return {"ok": True, "watch": d, "id": mid}

def _mariadb_cid():
    for name, cid in docker_ps().items():
        if "maria" in name.lower():
            return cid
    return None

def op_db(req):
    sql = req.get("sql")
    if not sql:
        return {"ok": False, "error": "empty sql"}
    db = req.get("database")
    if db is not None and not all(c.isalnum() or c == "_" for c in db):
        return {"ok": False, "error": "invalid database name"}
    cid = _mariadb_cid()
    if not cid:
        return {"ok": False, "error": "mariadb container not found"}
    fmt = req.get("format", "table")
    opt = "--batch" if fmt == "tsv" else "-t"
    prefix = (("USE `%s`;\n" % db) if db else "")
    if req.get("read_only"):
        full = prefix + "START TRANSACTION READ ONLY;\n" + sql + "\nROLLBACK;"
    else:
        full = prefix + sql
    # MYSQL_PWD keeps the password OUT of any command line; it stays inside the container.
    inner = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb -uroot --default-character-set=utf8mb4 -A %s' % opt
    try:
        r = subprocess.run(["docker", "exec", "-i", cid, "sh", "-c", inner],
                           input=full.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    except Exception as e:
        return {"ok": False, "error": "db exec failed: %s" % e}
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    maxb = int(req.get("maxbytes", 40000))
    truncated = len(out) > maxb
    if truncated:
        out = out[:maxb]
    return {"ok": r.returncode == 0, "rc": r.returncode, "output": out,
            "error_out": err.strip(), "truncated": truncated}

def _mongo_cid(name=None):
    ps = docker_ps()
    if name and ps.get(name):
        return ps[name]
    for n, cid in ps.items():
        if "mongo" in n.lower():
            return cid
    return None

def op_mongo(req):
    script = req.get("script")
    if not script:
        return {"ok": False, "error": "empty script"}
    db = req.get("database") or ""
    if db and not all(c.isalnum() or c in "_-" for c in db):
        return {"ok": False, "error": "invalid database name"}
    cid = _mongo_cid(req.get("container"))
    if not cid:
        return {"ok": False, "error": "mongo container not found"}
    authdb = req.get("auth_db") or "admin"
    if not all(c.isalnum() or c in "_-" for c in authdb):
        return {"ok": False, "error": "invalid auth db"}
    jsonflag = "--json=relaxed " if req.get("format") == "json" else ""
    # Credentials come from the container's own env ($MONGO_INITDB_ROOT_*) and are
    # never read out or passed from the host; if the deployment runs without auth
    # the -u/-p pair is simply omitted. The SCRIPT travels as an env var (not argv)
    # so quoting is never an issue and it survives arbitrary length.
    inner = ('set -- ; '
             'if [ -n "$MONGO_INITDB_ROOT_USERNAME" ]; then '
             'set -- -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
             '--authenticationDatabase %s; fi; '
             'exec mongosh --quiet %s"$@" %s --eval "$SRCDS_MONGO_SCRIPT"'
             % (authdb, jsonflag, (("'%s'" % db) if db else "")))
    try:
        r = subprocess.run(["docker", "exec", "-i", "-e", "SRCDS_MONGO_SCRIPT=" + script,
                            cid, "sh", "-c", inner],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except Exception as e:
        return {"ok": False, "error": "mongo exec failed: %s" % e}
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    maxb = int(req.get("maxbytes", 40000))
    truncated = len(out) > maxb
    if truncated:
        out = out[:maxb]
    # mongosh prints script errors (TypeError/MongoServerError) on stdout with rc=1.
    return {"ok": r.returncode == 0, "rc": r.returncode, "output": out,
            "error_out": err.strip(), "truncated": truncated}

def main():
    try:
        # request arrives on STDIN (urlsafe-base64 JSON) so large payloads (deploy
        # file content, big Lua bodies) never hit the OS command-line length limit.
        raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
        req = json.loads(base64.urlsafe_b64decode(raw.strip().encode()).decode("utf-8"))
    except Exception as e:
        jout({"ok": False, "error": "bad request: %s" % e}); return
    op = req.get("op")
    try:
        if op == "discover":
            jout(discover())
        elif op == "console":
            jout(op_console(req))
        elif op == "lua":
            jout(op_lua(req))
        elif op == "fetch":
            jout(op_fetch(req))
        elif op == "deploy":
            jout(op_deploy(req))
        elif op == "grep":
            jout(op_grep(req))
        elif op == "diff":
            jout(op_diff(req))
        elif op == "nodeinfo":
            jout(op_nodeinfo(req))
        elif op == "power":
            jout(op_power(req))
        elif op == "bootwatch":
            jout(op_bootwatch(req))
        elif op == "monitor":
            jout(op_monitor(req))
        elif op == "db":
            jout(op_db(req))
        elif op == "mongo":
            jout(op_mongo(req))
        else:
            jout({"ok": False, "error": "unknown op: %s" % op})
    except Exception as e:
        jout({"ok": False, "error": "driver exception: %s" % e})

main()
'''

# ----------------------------------------------------------------------------
# Local helpers
# ----------------------------------------------------------------------------
# Bake the host-side config (volume paths, wings endpoint, owner uid/gid, server
# topology) into the driver source. These are HOST facts, identical for everyone on
# the same node, so the rendered driver — and therefore its hash below — is stable
# across developers; a different deployment's config yields a different driver+hash.
def _render_driver(tmpl):
    return (tmpl
            .replace("@VOLROOT_JSON@", json.dumps(CFG["volroot"]))
            .replace("@BAKROOT_JSON@", json.dumps(CFG["backups_root"]))
            .replace("@WINGS_API_JSON@", json.dumps(CFG["wings"]["api"]))
            .replace("@WINGS_CONFIG_JSON@", json.dumps(CFG["wings"]["config"]))
             .replace("@OWNER_UID@", str(int(CFG["owner_uid"])))
             .replace("@OWNER_GID@", str(int(CFG["owner_gid"])))
             .replace("@DIFF_FILE_MAX_BYTES@", str(DIFF_FILE_MAX_BYTES))
             .replace("@DIFF_BATCH_MAX_INPUT_BYTES@", str(DIFF_BATCH_MAX_INPUT_BYTES))
             .replace("@DEPLOY_FILE_MAX_BYTES@", str(DEPLOY_FILE_MAX_BYTES))
             .replace("@DEPLOY_BATCH_MAX_INPUT_BYTES@", str(DEPLOY_BATCH_MAX_INPUT_BYTES))
             .replace("@SERVERS_JSON@", json.dumps(CFG["servers"])))


HOST_DRIVER = _render_driver(HOST_DRIVER)

# Version-namespace the host driver by a content hash so that DIFFERENT versions of
# this MCP server (e.g. a stale Claude Code instance + a freshly-edited one) NEVER
# clobber each other's /tmp driver — the cause of intermittent `driver exception:
# 'body'` (a v1 tool sending to a v2 driver, or vice versa, over a shared file).
_DRIVER_HASH = hashlib.sha1(HOST_DRIVER.encode("utf-8")).hexdigest()[:12]
_DRIVER_REMOTE = "/tmp/srcds_host_driver_%s.py" % _DRIVER_HASH
_driver_ready = False


_LOG_TEXT_KEYS = {"cmd", "code", "sql", "script", "pattern", "target",
                  "to", "path", "local", "save_to", "backup"}


def _redact_log_record(rec):
    out = dict(rec)
    for key in list(out):
        if key not in _LOG_TEXT_KEYS or not isinstance(out.get(key), str):
            continue
        value = out.pop(key)
        if key == "target":
            out["target_mode"] = "all" if value.lower() == "all" else "specific"
        out[key + "_chars"] = len(value)
        out[key + "_bytes"] = len(value.encode("utf-8", "replace"))
    return out


def _rotate_log_if_needed():
    try:
        if os.path.getsize(LOG_PATH) < LOG_ROTATE_BYTES:
            return
    except OSError:
        return
    try:
        oldest = LOG_PATH + ".%d" % LOG_ROTATE_KEEP
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_ROTATE_KEEP - 1, 0, -1):
            src, dst = LOG_PATH + ".%d" % i, LOG_PATH + ".%d" % (i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def log_event(rec):
    try:
        rec = _redact_log_record(rec)
        rec["t"] = time.time()
        _rotate_log_if_needed()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ensure_driver():
    global _driver_ready
    if _driver_ready:
        return True
    if config_error():
        return False
    try:
        # Atomic upload to the version-specific path: write to a unique temp then mv,
        # so a concurrent reader never sees a half-written driver.
        tmp = _DRIVER_REMOTE + ".tmp." + os.urandom(4).hex()
        remote = "cat > %s && mv -f %s %s" % (tmp, tmp, _DRIVER_REMOTE)
        r = subprocess.run(
            SSH_BASE + [remote],
            input=HOST_DRIVER.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        _driver_ready = (r.returncode == 0)
        if not _driver_ready:
            log_event({"ev": "driver_upload_fail", "rc": r.returncode,
                       "err": r.stderr.decode("utf-8", "replace")[-300:]})
        return _driver_ready
    except Exception as e:
        log_event({"ev": "driver_upload_exc", "err": str(e)})
        return False


def run_driver(req, timeout=45, _retried=False):
    ce = config_error()
    if ce:
        return {"ok": False, "error": "config: " + ce}
    if not ensure_driver():
        return {"ok": False, "error": "could not upload host driver over SSH (check network/VPN, ssh.key, ssh.host)."}
    b64 = base64.urlsafe_b64encode(json.dumps(req).encode("utf-8")).decode()
    try:
        # feed the (possibly large) request via STDIN, NOT the command line, to avoid
        # the Windows ~32KB command-line limit (WinError 206) on big deploys/Lua bodies.
        r = subprocess.run(
            SSH_BASE + ["python3 " + _DRIVER_REMOTE],
            input=b64.encode("ascii"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "driver timed out after %ss" % timeout}
    except Exception as e:
        return {"ok": False, "error": "ssh exec failed: %s" % e}
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace")
        # The host's /tmp can be wiped (host reboot, tmpfiles cleanup) while this
        # process still believes the driver is uploaded (_driver_ready caches per
        # process). On the ENOENT signature, re-upload once and retry.
        if (not _retried) and "can't open file" in err:
            global _driver_ready
            _driver_ready = False
            log_event({"ev": "driver_missing_reupload"})
            return run_driver(req, timeout=timeout, _retried=True)
        return {"ok": False, "error": "ssh rc=%d: %s" % (r.returncode, err[-400:])}
    out = r.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(out)
    except Exception as e:
        return {"ok": False, "error": "bad driver output: %s | %.400s" % (e, out)}


# discover cache (process-lifetime, short TTL)
_disc_cache = {"t": 0, "data": None}


def discover(force=False, strict=False):
    if (not force) and _disc_cache["data"] is not None and (time.time() - _disc_cache["t"] < 20):
        return _disc_cache["data"]
    d = run_driver({"op": "discover"}, timeout=40)
    servers = d.get("servers") if isinstance(d, dict) else None
    if servers is not None:
        _disc_cache["t"] = time.time()
        _disc_cache["data"] = servers
        return servers
    if strict:
        return []
    # Read-only callers may use the last snapshot on failure.
    return _disc_cache["data"] if _disc_cache["data"] is not None else []


def resolve(server, fresh=False):
    """Resolve exactly one target; writes require successful fresh discovery."""
    matches = [s for s in discover(force=fresh, strict=fresh) if s.get("logical") == server]
    return matches[0] if len(matches) == 1 else None


# ----------------------------------------------------------------------------
# A2S_INFO (live player count, read-only, external UDP)
# ----------------------------------------------------------------------------
def a2s_info(ip, port, timeout=2.0):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        req = b"\xFF\xFF\xFF\xFF\x54Source Engine Query\x00"
        s.sendto(req, (ip, port))
        data, _ = s.recvfrom(4096)
        if data[4:5] == b"\x41":  # challenge
            s.sendto(req + data[5:9], (ip, port))
            data, _ = s.recvfrom(4096)
        if data[4:5] != b"\x49":
            return None

        def cstr(d, i):
            j = d.index(b"\x00", i)
            return d[i:j].decode("utf-8", "replace"), j + 1

        i = 6                      # 4x0xFF, 0x49 header, 1 protocol byte
        name, i = cstr(data, i)    # server name
        mapn, i = cstr(data, i)    # map
        folder, i = cstr(data, i)  # game folder
        game, i = cstr(data, i)    # game description
        i += 2                     # AppID (short)
        players = data[i]; maxpl = data[i + 1]; bots = data[i + 2]
        return {"name": name, "map": mapn, "players": players,
                "maxplayers": maxpl, "bots": bots}
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def live_info(srv):
    """Return (players, maxplayers, live-state); live-state is tri-state."""
    if not srv or not srv.get("running"):
        return (None, None, False)
    if not srv.get("port"):
        return (None, None, None)
    a = a2s_info(PUBLIC_IP, srv["port"])
    if not a:
        return (None, None, None)
    thr = LIVE_THRESHOLD.get(srv["logical"], 9999)
    return (a["players"], a["maxplayers"], a["players"] >= thr)


# ----------------------------------------------------------------------------
# Safety classifier
# ----------------------------------------------------------------------------
DESTRUCTIVE_CMD = re.compile(
    r"(?i)\b(quit|exit|_restart|restart|killserver|sv_shutdown|changelevel|map|gamemode|"
    r"kick|kickid|ban|banid|banip|addip|removeid|removeip|writeid|rcon_password|sv_password|"
    r"sv_cheats|host_writeconfig|heartbeat|crash|meta\s+reload|sv_kickban)\b")

LUA_MUTATE = re.compile(
    r"(?i)(:SetHealth|:SetMaxHealth|:SetArmor|:Kill\b|:Remove\b|:Kick\b|:Ban\b|:StripWeapons|"
    r":Give\b|:SetPos|:SetTeam|:SetModel|:SetVelocity|:God\b|:Freeze\b|:Spawn\b|:Disconnect|"
    r":ConCommand|:SendLua|:SetPData|:Fire\b|:Input\b|:EmitSound|:Ignite|:TakeDamage|:SetMoveType|"
    r":Set(NW|NW2)(Int|String|Float|Bool|Entity|Vector|Angle)?|RunConsoleCommand|game\.ConsoleCommand|"
    r"game\.CleanUpMap|game\.KickID|engine\.CloseServer|BroadcastLua|player\.GetByID|"
    r"file\.(Write|Append|Delete|CreateDir|Rename)|sql\.(Query|Begin|Commit)|RunString|RunStringEx|CompileString|"
    r"util\.RemoveAll|ents\.Create|hook\.Remove|timer\.(Remove|Destroy)|concommand\.Run|\bos\.|\bio\.)")

# Indirection / obfuscation: cannot statically prove read-only -> confirm with louder note.
LUA_OPAQUE = re.compile(
    r"(?i)(_G\s*\[|getfenv\b|setfenv\b|\bloadstring\b|\bload\s*\(|string\.char\b|string\.byte\b|"
    r"\[\s*[\"'][A-Za-z_]\w*[\"']\s*\]\s*\()")

LUA_MUTATE_DOT = re.compile(
    r"(?i)\.\s*(SetHealth|SetMaxHealth|SetArmor|Kill|Remove|Kick|Ban|StripWeapons|Give|"
    r"SetPos|SetTeam|SetModel|SetVelocity|God|Freeze|Spawn|Disconnect|ConCommand|SendLua|"
    r"SetPData|Fire|Input|EmitSound|Ignite|TakeDamage|SetMoveType|SetNW\w*|Write|Append|Delete|"
    r"CreateDir|Rename|Create|Run|Destroy)\s*\(")

CONSOLE_READ_ONLY = (
    re.compile(r"(?i)\s*(status|stats|version|uptime)\s*"),
    re.compile(r"(?i)\s*(cvarlist|find|help|maps)(?:\s+[^\r\n;]*)?\s*"),
    re.compile(r"(?i)\s*meta\s+(list|version)\s*"),
)


def _strip_lua(code):
    """Remove comments and string literals so the classifier sees only executable tokens."""
    s = re.sub(r"--\[(=*)\[.*?\]\1\]", " ", code, flags=re.S)   # block comments
    s = re.sub(r"--[^\n]*", " ", s)                              # line comments
    s = re.sub(r"\[(=*)\[.*?\]\1\]", " ", s, flags=re.S)         # long-bracket strings
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)                    # double-quoted strings
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)                    # single-quoted strings
    return s


def classify_console(cmd):
    if any(c in cmd for c in ("\r", "\n", ";")):
        return "multiple console commands are not on the read-only allowlist"
    m = DESTRUCTIVE_CMD.search(cmd)
    if m:
        return ("destructive console verb '%s'" % m.group(1))
    if any(p.fullmatch(cmd) for p in CONSOLE_READ_ONLY):
        return None
    return "command is not on the explicit read-only console allowlist"


def classify_lua(code):
    """Return (reason, band) where band in {None, 'mutate', 'opaque'}."""
    s = _strip_lua(code)
    m = LUA_MUTATE.search(s)
    if m:
        return ("mutating call '%s'" % m.group(0), "mutate")
    m = LUA_MUTATE_DOT.search(s)
    if m:
        return ("mutating dot-call '%s'" % m.group(1), "mutate")
    m = LUA_OPAQUE.search(s)
    if m:
        return ("opaque/indirect call '%s'" % m.group(0).strip(), "opaque")
    return (None, None)


# ----------------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------------
def tool_status(args):
    want = args.get("server")
    servers = discover(force=True)
    if not servers:
        return ("Could not reach the host (SSH/driver failed). Check VPN / key / host.", True)
    lines = ["Game servers (resolved live):", ""]
    for s in sorted(servers, key=lambda x: x["logical"]):
        if want and s["logical"] != want:
            continue
        tag = s["logical"].upper()
        if not s["running"]:
            lines.append("  %-6s  DOWN" % tag)
            continue
        players, maxpl, is_live = live_info(s)
        thr = LIVE_THRESHOLD.get(s["logical"], "?")
        if players is None:
            pc = "players=?? (A2S unreachable)"
            live_s = "?"
        else:
            pc = "players=%d/%s" % (players, maxpl)
            live_s = ("LIVE" if is_live else "quiet") + (" (>=%s=live)" % thr)
        cap = "condebug" if s["condebug"] else "NO-condebug(blind)"
        lines.append("  %-6s  UP  %-22s  %-22s  port=%s  %s" % (
            tag, pc, live_s, s.get("port"), cap))
    lines.append("")
    if LIVE_THRESHOLD:
        lines.append("Live thresholds: " + ", ".join(
            "%s>=%s" % (k.upper(), v) for k, v in sorted(LIVE_THRESHOLD.items())) + " players.")
    return ("\n".join(lines), False)


def _confirm_gate(server, srv, reason, args):
    """Return an error-text string if blocked, else None."""
    if args.get("confirm") is True:
        return None
    players, maxpl, is_live = live_info(srv)
    live_note = ""
    if players is not None:
        live_note = "  Currently %d/%s players%s." % (
            players, maxpl, " — SERVER IS LIVE" if is_live else "")
    return ("BLOCKED (safety gate): this looks destructive — %s.%s\n"
            "Re-call with confirm=true to proceed. Nothing was executed." % (reason, live_note))


def tool_console(args):
    server = args.get("server")
    command = args.get("command", "")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    if not command.strip():
        return ("command is empty", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if not srv["running"]:
        return ("%s is DOWN — cannot inject console commands (files would load on boot, but the process isn't running)." % server.upper(), True)
    reason = classify_console(command)
    if reason:
        blocked = _confirm_gate(server, srv, reason, args)
        if blocked:
            log_event({"ev": "console_blocked", "server": server, "cmd": command, "reason": reason})
            return (blocked, True)
    res = run_driver({"op": "console", "uuid": srv["uuid"], "cmd": command,
                      "grep": args.get("grep"),
                      "maxbytes": int(args.get("maxbytes", 24000))}, timeout=45)
    log_event({"ev": "console", "server": server, "cmd": command,
               "confirm": bool(args.get("confirm")), "ok": res.get("ok")})
    if not res.get("ok"):
        return ("console failed: %s" % res.get("error"), True)
    out = res.get("output", "") or "(no console.log output captured)"
    note = res.get("note", "")
    head = "[%s] injected: %s" % (server.upper(), command)
    if res.get("truncated"):
        head += "  [byte-capped -> most recent; raise maxbytes or narrow with grep]"
    if note:
        head += "\n(note: %s)" % note
    src = "console.log delta" if res.get("condebug") else "pty capture"
    return (head + "\n--- %s ---\n" % src + out, False)


# Safe value -> single-line JSON-ish text. Cycle/entity/depth/fanout/length-safe.
# NEVER throws, NEVER hangs (util.TableToJSON does both). Defines `local _ser`.
SERIALIZER_LUA = r'''
local _ser
do
  local MAXDEPTH, MAXKEYS, MAXLEN, MAXBUF = 6, 256, 32768, 4000
  local function q(s)
    return (string.format("%q", tostring(s)):gsub("\\\n", "\\n"))
  end
  local function scalar(v)
    local t = type(v)
    if t == "string" then return q(v) end
    if t == "number" then
      if v ~= v then return "\"nan\"" end
      if v == math.huge then return "\"inf\"" end
      if v == -math.huge then return "\"-inf\"" end
      return tostring(v)
    end
    if t == "boolean" or t == "nil" then return tostring(v) end
    return nil
  end
  local function tagged(v)
    local t = type(v)
    if t == "Vector" then return "\"<Vector " .. tostring(v) .. ">\"" end
    if t == "Angle"  then return "\"<Angle " .. tostring(v) .. ">\"" end
    if IsColor and IsColor(v) then
      return string.format("\"<Color %s,%s,%s,%s>\"", tostring(v.r), tostring(v.g), tostring(v.b), tostring(v.a))
    end
    if t == "Entity" or t == "Player" or t == "NPC" or t == "Vehicle" or t == "Weapon" or t == "NextBot" then
      if not IsValid(v) then
        if v.EntIndex and v:EntIndex() == 0 then return "\"<worldspawn>\"" end
        return "\"<" .. t .. ":NULL>\""
      end
      local idx = tostring((v.EntIndex and v:EntIndex()) or "?")
      if v.IsPlayer and v:IsPlayer() then
        return "\"<Player:" .. idx .. " " .. (tostring(v:Nick()):gsub('["\\]', "")) .. ">\""
      end
      local cls = (v.GetClass and v:GetClass()) or "?"
      return "\"<" .. t .. ":" .. idx .. " " .. tostring(cls) .. ">\""
    end
    if t == "function" then return "\"<function>\"" end
    if t == "userdata" then return "\"<userdata>\"" end
    if t == "thread"   then return "\"<thread>\"" end
    return nil
  end
  local buf
  local function walk(v, seen, depth)
    if #buf > MAXBUF then return end
    local s = scalar(v); if s ~= nil then buf[#buf + 1] = s; return end
    local tg = tagged(v); if tg ~= nil then buf[#buf + 1] = tg; return end
    if type(v) ~= "table" then buf[#buf + 1] = "\"<" .. type(v) .. ">\""; return end
    if seen[v] then buf[#buf + 1] = "\"<cycle>\""; return end
    if depth >= MAXDEPTH then buf[#buf + 1] = "\"<maxdepth>\""; return end
    seen[v] = true
    local n, isarr = 0, true
    for k in pairs(v) do n = n + 1; if type(k) ~= "number" then isarr = false end end
    if isarr and n == #v then
      buf[#buf + 1] = "["
      for i = 1, #v do
        if i > MAXKEYS then buf[#buf + 1] = ",\"<...more>\""; break end
        if i > 1 then buf[#buf + 1] = "," end
        walk(v[i], seen, depth + 1)
      end
      buf[#buf + 1] = "]"
    else
      buf[#buf + 1] = "{"
      local c = 0
      for k, val in pairs(v) do
        c = c + 1
        if c > MAXKEYS then buf[#buf + 1] = ",\"<...more>\""; break end
        if c > 1 then buf[#buf + 1] = "," end
        local kk = (type(k) == "string" or type(k) == "number") and tostring(k) or ("__key:" .. tostring(k))
        buf[#buf + 1] = q(kk) .. ":"
        walk(val, seen, depth + 1)
      end
      buf[#buf + 1] = "}"
    end
    seen[v] = nil
  end
  _ser = function(v)
    buf = {}
    local ok = pcall(walk, v, {}, 0)
    if not ok then return "\"<serialize-error:" .. tostring(type(v)) .. ">\"" end
    local out = table.concat(buf)
    if #out > MAXLEN then out = out:sub(1, MAXLEN) .. "...\"<truncated>\"" end
    return out
  end
end
'''

# Runner that is lua_openscript'd. include()s the @BODY@ file (user code, verbatim)
# so tracebacks read _mcp/<tok>_body.lua:<USER_LINE>. Placeholders: @TOK@ @BODY@ @ASYNC@ @SER@.
RUNNER_TEMPLATE = r'''
local _T     = "@TOK@"
local _BODY  = "@BODY@"
local _ASYNC = @ASYNC@
local _D     = "~|~"

local function _b64(s)
  local ok, r = pcall(util.Base64Encode, tostring(s), true)
  if not ok or r == nil then ok, r = pcall(util.Base64Encode, tostring(s)) end
  if not ok or r == nil then r = "" end
  return (tostring(r):gsub("%s+", ""))
end
-- Buffer framed lines; the whole buffer is file.Write'n to data/_mcp/<tok>.txt at
-- finalize and read directly off the volume by the host driver. This is the OUTPUT
-- channel for ALL servers: works without -condebug, no console 4KB line limit, and
-- none of the live console's other-player spam.
local _BUF = {}
local function _emit(kind, payload)
  local b = (payload ~= nil and payload ~= "") and _b64(payload) or ""
  _BUF[#_BUF + 1] = "__MCP" .. _D .. _T .. _D .. kind .. _D .. b
end

@SER@

-- OUT is budgeted by lines AND bytes: 400 short lines is fine, but 400 x 1400-char
-- lines would be ~560KB of tokens. First cap crossed wins; a NOTE marks the cut.
local _outn, _outb, _outcap = 0, 0, false
local function _LOGLINE(s)
  s = tostring(s)
  if #s > 1400 then s = s:sub(1, 1400) .. "...<+>" end
  if _outcap then return end
  _outn = _outn + 1
  _outb = _outb + #s
  if _outn > 400 or _outb > 32768 then
    _outcap = true
    _emit("NOTE", "output capped (>400 lines or >32KB) - further lines dropped")
    return
  end
  _emit("OUT", s)
end

local _P, _F = 0, 0
local _fb, _fcap = 0, false
local _section = ""
local _scratch = {}

local function _tag(m) if _section ~= "" then return "[" .. _section .. "] " .. tostring(m or "?") end return tostring(m or "?") end
local function _record(pass, failtext)
  if pass then _P = _P + 1
  else
    -- FAIL detail is budgeted (a failing check inside a loop would otherwise emit
    -- one line per iteration - megabytes). _F keeps counting so the p=/f= summary
    -- stays exact even when detail is suppressed.
    _F = _F + 1
    local ft = (tostring(failtext):gsub("[\r\n]+", " / "))
    if #ft > 1400 then ft = ft:sub(1, 1400) .. "...<+>" end
    if not _fcap then
      _fb = _fb + #ft
      if _F > 60 or _fb > 24576 then
        _fcap = true
        _emit("NOTE", "FAIL details capped (>60 fails or >24KB) - the p=/f= summary still counts ALL checks")
      else
        _emit("FAIL", ft)
      end
    end
  end
  return pass
end

_scratch.SECTION = function(name) _section = tostring(name or "") end
_scratch.CHECK   = function(c, m) return _record(c and true or false, _tag(m)) end
_scratch.EQ      = function(a, b, m) return _record(a == b, _tag(m) .. "  got=" .. _ser(a) .. " want=" .. _ser(b)) end
_scratch.NEQ     = function(a, b, m) return _record(a ~= b, _tag(m) .. "  both=" .. _ser(a)) end
_scratch.NEAR    = function(a, b, eps, m)
  eps = eps or 1e-6
  local ok = (type(a) == "number" and type(b) == "number" and math.abs(a - b) <= eps)
  return _record(ok, _tag(m) .. "  got=" .. _ser(a) .. " want~=" .. _ser(b) .. " eps=" .. _ser(eps))
end
_scratch.TRUE    = function(v, m) return _record(v == true,  _tag(m) .. "  got=" .. _ser(v)) end
_scratch.FALSE   = function(v, m) return _record(v == false, _tag(m) .. "  got=" .. _ser(v)) end
_scratch.OK      = function(v, m) return _record(v ~= nil and v ~= false, _tag(m) .. "  got=" .. _ser(v)) end
_scratch.THROWS  = function(fn, m) local ok, e = pcall(fn); _record(not ok, _tag(m) .. "  did not throw"); return e end
_scratch.DUMP    = function(v) return _ser(v) end
_scratch.LOG     = function(...)
  local nn = select("#", ...); local t = {}
  for i = 1, nn do local x = select(i, ...); t[i] = (type(x) == "string") and x or _ser(x) end
  _LOGLINE(table.concat(t, "\t"))
end

local _finalized = false
local function _finalize(kind)
  if _finalized then return end
  _finalized = true
  if (_P + _F) > 0 then _emit("SUM", "p=" .. _P .. " f=" .. _F) end
  _emit(kind)
  pcall(file.CreateDir, "_mcp")
  pcall(file.Write, "_mcp/" .. _T .. ".txt", table.concat(_BUF, "\n"))
end
_scratch.MCP_DONE = function() _finalize("DON") end

-- sandbox env: body global READS fall through to _G; body global WRITES go to a
-- scratch table => zero _G pollution from the body's own globals (no cleanup needed).
-- capture body print/Msg/MsgN as framed OUT lines (clean separation from other
-- players' live console spam, which the driver drops as unframed noise).
_scratch.print = function(...) _scratch.LOG(...) end
_scratch.MsgN  = function(...)
  local nn = select("#", ...); local t = {}
  for i = 1, nn do t[i] = tostring((select(i, ...))) end
  _LOGLINE(table.concat(t))
end
_scratch.Msg = _scratch.MsgN
local _env = setmetatable({}, {
  __index = function(_, k) local s = _scratch[k]; if s ~= nil then return s end return _G[k] end,
  __newindex = function(_, k, v) _scratch[k] = v end,
})
local function _pack(ok, ...) return ok, select("#", ...), { ... } end

_emit("BEG")
-- Load the body via CompileString (NOT include(): GMod include() swallows errors
-- internally and returns nothing, so xpcall never sees them). CompileString returns
-- the chunk OR an error string (handleError=false), carrying _BODY:<line> => 1:1 lines.
local _src = file.Read(_BODY, "LUA") or file.Read("lua/" .. _BODY, "GAME")
if _src == nil then
  _emit("ERR", "could not read body file (" .. _BODY .. ")")
  _finalize("END")
  return
end
local _chunk = CompileString(_src, _BODY, false)
if type(_chunk) ~= "function" then
  _emit("ERR", tostring(_chunk))     -- compile/syntax error (already _BODY:line: ...)
  _finalize("END")
  return
end
if setfenv then setfenv(_chunk, _env) end

local _oldhook, _oldmask, _oldcount = debug.gethook()
local function _restorehook()
  if _oldhook then debug.sethook(_oldhook, _oldmask, _oldcount) else debug.sethook() end
end
local _ins = 0
debug.sethook(function()
  _ins = _ins + 1
  if _ins > 200 then _restorehook(); error("[mcp] instruction budget exceeded (runaway loop?)", 2) end
end, "", 100000)
local _ok, _cnt, _vals = _pack(xpcall(_chunk, function(e)
  return tostring(e) .. "\n" .. debug.traceback("", 2)
end))
_restorehook()

if _ok then
  if _cnt == 1 then
    _emit("RET", _ser(_vals[1]))
  elseif _cnt > 1 then
    local parts = {}
    for i = 1, _cnt do parts[i] = _ser(_vals[i]) end
    _emit("RET", "[" .. table.concat(parts, ",") .. "]")
  end
else
  _emit("ERR", tostring(_vals[1]))
end

if _ASYNC and _ok and not _finalized then
  -- async: wait for the body's MCP_DONE() callback (driver waits async_timeout for DON)
else
  -- Synchronous compile/runtime/setup failures cannot ever call MCP_DONE().
  -- Finalize them immediately even when the caller requested async capture.
  _finalize("END")
end
'''


def render_runner(tok, body_rel, want_async):
    return (RUNNER_TEMPLATE
            .replace("@SER@", SERIALIZER_LUA)
            .replace("@TOK@", tok)
            .replace("@BODY@", body_rel)
            .replace("@ASYNC@", "true" if want_async else "false"))


def tool_lua(args):
    server = args.get("server")
    code = args.get("code", "")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    if not code.strip():
        return ("code is empty", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if not srv["running"]:
        return ("%s is DOWN — cannot run Lua (the server process isn't running)." % server.upper(), True)
    if len(code) > 64 * 1024:
        return ("code too large (>64KB)", True)
    if "~|~" in code or "__MCP" in code:
        return ("code may not contain the reserved markers '~|~' or '__MCP'", True)
    reason, band = classify_lua(code)
    gate_reason = reason or "arbitrary server-side Lua cannot be statically proven read-only"
    if band == "opaque":
        gate_reason += " (indirect/obfuscated call)"
    blocked = _confirm_gate(server, srv, gate_reason, args)
    if blocked:
        log_event({"ev": "lua_blocked", "server": server, "reason": gate_reason,
                   "band": band or "arbitrary", "code": code})
        return (blocked, True)
    want_async = bool(args.get("async"))
    atimeout = int(args.get("async_timeout", 20))
    tok = os.urandom(8).hex()
    runner = render_runner(tok, "_mcp/%s_body.lua" % tok, want_async)
    res = run_driver({"op": "lua", "uuid": srv["uuid"], "token": tok,
                      "body": code, "runner": runner, "async": want_async,
                      "capture_timeout": 7, "async_timeout": atimeout},
                     timeout=(atimeout + 30 if want_async else 55))
    log_event({"ev": "lua", "server": server, "confirm": bool(args.get("confirm")),
               "async": want_async, "ok": res.get("ok"), "code": code[:200]})
    if not res.get("ok"):
        return ("lua failed: %s" % res.get("error"), True)

    r = res.get("result") or {}
    note = res.get("note") or r.get("note") or ""
    out, ret, err, summ = r.get("out", ""), r.get("ret"), r.get("err"), r.get("sum")
    fails = r.get("fails") or []
    started, ended = r.get("started"), r.get("ended")

    nf = 0
    if summ:
        mm = re.search(r"f=(\d+)", summ)
        if mm:
            nf = int(mm.group(1))

    parts = ["[%s] lua%s" % (server.upper(), " (async)" if want_async else "")]
    if note:
        parts.append("(note: %s)" % note)
    if summ:
        parts.append("checks: %s%s" % (summ, "" if nf == 0 else "   <-- FAILURES"))
        for fl in fails[:60]:
            parts.append("  [FAIL] " + fl)
        if len(fails) > 60:
            parts.append("  ...[%d more FAIL lines suppressed — the f= count above is exact]" % (len(fails) - 60))
    if err is not None:
        parts.append("--- ERROR ---\n" + err)
    if out:
        # backstop only — the runner already budgets OUT (400 lines / 32KB)
        if len(out) > 60000:
            out = "...[capped: last 60000 of %d chars]...\n%s" % (len(out), out[-60000:])
        parts.append("--- output ---\n" + out)
    if ret is not None:
        parts.append("--- return ---\n" + ret)
    if not any([summ, err, out, ret]):
        parts.append("(no output / suite produced nothing)")

    is_error = (err is not None) or (nf > 0) or (not bool(started)) or (not bool(ended))
    return ("\n".join(parts), is_error)


def _fmt_mtime(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "?"


def tool_fetch(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    what = args.get("what", "console")
    save_to = args.get("save_to")
    if save_to:
        if what != "file":
            return ("save_to is only valid with what='file'.", True)
        if not os.path.isabs(save_to):
            return ("save_to must be an absolute local path.", True)
        if args.get("confirm") is not True:
            return ("BLOCKED: save the remote file into the requested LOCAL path. Re-call with "
                    "confirm=true. No remote read or local write was performed.", True)

    if what in ("dir", "hash", "backups", "history"):
        req = {"op": "fetch", "uuid": srv["uuid"], "what": what, "path": args.get("path", "")}
        if what == "history":
            req.update(lines=args.get("lines", 20), before=args.get("before"))
        if what == "hash" and args.get("glob"):
            req["glob"] = args["glob"]
        res = run_driver(req, timeout=90)
        log_event({"ev": "fetch", "server": server, "what": what, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("fetch failed: %s" % res.get("error"), True)
        if what == "history":
            return (json.dumps(res, ensure_ascii=False, indent=2), False)
        if what == "dir":
            ents = res.get("entries", [])
            out = ["[%s] dir garrysmod/%s — %d entries%s" % (
                server.upper(), args.get("path", ""), res.get("total", len(ents)),
                " (showing first 500)" if res.get("truncated") else "")]
            for e in ents:
                if e.get("dir"):
                    out.append("  %-44s     <dir>" % (e["name"] + "/"))
                else:
                    out.append("  %-44s %9s  %s" % (e["name"], e.get("size"), _fmt_mtime(e.get("mtime"))))
            return ("\n".join(out), False)
        if what == "hash":
            files = res.get("files", {})
            out = ["[%s] sha256 garrysmod/%s (glob=%s) — %d file(s)%s" % (
                server.upper(), args.get("path", ""), args.get("glob") or "*",
                res.get("count", len(files)),
                "  [capped at 2000 — narrow path/glob]" if res.get("truncated") else "")]
            # byte-budget the listing (2000 entries would be ~150KB of tokens)
            used, omitted = 0, 0
            for rel in sorted(files):
                h, sz = files[rel]
                line = "  %s %9s  %s" % (h or "?" * 12, sz, rel)
                if used + len(line) > 48000:
                    omitted += 1
                    continue
                used += len(line) + 1
                out.append(line)
            if omitted:
                out.append("  ...[%d of %d entries omitted at 48KB — narrow path/glob for a complete compare]"
                           % (omitted, len(files)))
            if res.get("skipped_escaped"):
                out.append("  [%d symlink target(s) escaped garrysmod/ and were not read]" %
                           res["skipped_escaped"])
            out.append("TIP: when several hashes differ, compare them in ONE srcds_diff files=[...] batch, not separate calls.")
            return ("\n".join(out), False)
        baks = res.get("backups", [])
        out = ["[%s] deploy backups (restore requires expected_sha256 and explicit backup_id) — %d%s" % (
            server.upper(), len(baks), " (capped at 500)" if res.get("truncated") else "")]
        for b in baks:
            out.append("  %s  %s  %9s  %s" % (b.get("backup_id", "legacy"), b["path"], b["size"], _fmt_mtime(b["mtime"])))
        return ("\n".join(out), False)

    req = {"op": "fetch", "uuid": srv["uuid"], "what": what,
           "lines": int(args.get("lines", 200))}
    if args.get("maxbytes") is not None:
        req["maxbytes"] = int(args["maxbytes"])
    if what == "file":
        req["path"] = args.get("path", "")
        if save_to:
            req["b64"] = True
    if args.get("grep"):
        req["grep"] = args["grep"]
    res = run_driver(req, timeout=(150 if save_to else 40))
    log_event({"ev": "fetch", "server": server, "what": what,
               "truncated": res.get("truncated"), "ok": res.get("ok")})
    if not res.get("ok"):
        if res.get("exists") is False and what == "console":
            return ("%s has no console.log (no -condebug). Use the v2 Lua bridge for live output, or fetch a specific file with what='file'." % server.upper(), True)
        return ("fetch failed: %s" % res.get("error"), True)
    if save_to and what == "file":
        try:
            data = base64.b64decode(res.get("content_b64") or "", validate=True)
        except Exception as e:
            return ("bad download transfer: %s" % e, True)
        if os.path.exists(save_to) and args.get("overwrite") is not True:
            return ("local file exists: %s — pass overwrite:true to replace it. Nothing was written." % save_to, True)
        try:
            d = os.path.dirname(save_to)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(save_to, "wb") as f:
                f.write(data)
        except OSError as e:
            return ("could not write %s: %s" % (save_to, e), True)
        return ("[%s] downloaded garrysmod/%s -> %s (%d bytes, sha256=%s, binary-safe)"
                % (server.upper(), args.get("path", ""), save_to, len(data),
                   hashlib.sha256(data).hexdigest()), False)
    hdr = "[%s] %s (%s)" % (server.upper(), what, res.get("path", ""))
    if res.get("sha256"):
        hdr += "\nfull_file_sha256=" + res["sha256"] + " (text below may be filtered or tailed)"
    if res.get("truncated"):
        hdr += "  [byte-capped -> showing most recent; raise maxbytes or narrow via grep/lines for more]"
    return ("%s\n%s" % (hdr, res.get("content", "")), False)


PANEL_URL = CFG.get("panel_url") or ""


DEPLOY_BATCH_MAX = 400          # sanity cap; a whole addon fits comfortably
DIFF_BATCH_MAX = 200
DIFF_BATCH_DEFAULT_MAXBYTES = 48000
DIFF_BATCH_MAXBYTES = 200000

# Anti-trickle nudge: an LLM that uploads N files as N single-file calls burns a
# confirm + an SSH round-trip per file. Count DISTINCT paths single-deployed per
# server in a sliding window and, past the threshold, tell it to batch. Advisory
# only — never blocks (re-deploying the SAME file repeatedly is a legit dev loop
# and doesn't trip this, since distinct paths are what's counted).
SINGLE_TRICKLE_WINDOW = 240.0   # seconds
SINGLE_TRICKLE_AT = 3           # distinct files before the nudge fires
_recent_singles = {}            # server -> {to: last_deploy_time}
_recent_diff_singles = {}       # (server, comparison kind) -> {path: last_diff_time}


def _trickle_note(server, to, record):
    now = time.time()
    h = _recent_singles.setdefault(server, {})
    for k in [k for k, t in h.items() if now - t >= SINGLE_TRICKLE_WINDOW]:
        del h[k]
    if record:
        h[to] = now
    n = len(h) + (0 if (record or to in h) else 1)
    if n >= SINGLE_TRICKLE_AT:
        return ("\nTIP: %d different files deployed one-by-one to %s in the last %d min — send multiple files "
                "as ONE call: files:[{to, local|content}, ...] (one confirm, one SSH round-trip)."
                % (n, server, int(SINGLE_TRICKLE_WINDOW // 60)))
    return ""


def _diff_trickle_note(server, against, path, record):
    now = time.time()
    key = (server, against)
    h = _recent_diff_singles.setdefault(key, {})
    for k in [k for k, t in h.items() if now - t >= SINGLE_TRICKLE_WINDOW]:
        del h[k]
    if record:
        h[path] = now
    n = len(h) + (0 if (record or path in h) else 1)
    if n >= SINGLE_TRICKLE_AT:
        return ("\nTIP: %d different files diffed one-by-one in the last %d min — send them as ONE "
                "srcds_diff call with files:[{path, local|path_b}, ...] (one SSH round-trip)."
                % (n, int(SINGLE_TRICKLE_WINDOW // 60)))
    return ""


def _bad_deploy_to(to):
    return (not to) or to.startswith("/") or ":" in to or ".." in to.replace("\\", "/").split("/")


def _read_local_capped(path, cap):
    """Read at most cap+1 bytes so a changing/huge local file stays bounded."""
    try:
        size = os.path.getsize(path)
        if size > cap:
            return None, "local file is %d bytes (> %d cap)" % (size, cap)
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
    except (OSError, TypeError, ValueError) as e:
        return None, "could not read local file: %s" % e
    if len(data) > cap:
        return None, "local file grew beyond the %d-byte cap while being read" % cap
    return data, None


def tool_deploy(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    batch = args.get("files") is not None
    if batch and any(k in args for k in ("to", "local", "content", "expected_sha256", "backup_id")):
        return ("give files OR top-level to/local/content/expected_sha256/backup_id, not both", True)
    files = args.get("files") if batch else [args]
    if not isinstance(files, list) or not files or len(files) > DEPLOY_BATCH_MAX:
        return ("files must contain 1-%d entries" % DEPLOY_BATCH_MAX, True)
    if args.get("backup", True) is not True:
        return ("versioned backups are mandatory; backup=false is no longer supported", True)
    if args.get("confirm") is not True:
        return ("BLOCKED: deployment/restore requires confirm=true. Nothing was written.", True)
    restore = args.get("restore") is True
    revision = args.get("source_revision") or ""
    if revision and (not isinstance(revision, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) is None):
        return ("source_revision must be a full 40- or 64-character Git object hash", True)
    entries, seen, total = [], set(), 0
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return ("files[%d] must be an object" % i, True)
        to = f.get("to")
        if (not isinstance(to, str) or _bad_deploy_to(to) or "\\" in to or "\x00" in to
                or any(p in ("", ".", "..") for p in to.split("/"))):
            return ("files[%d]: invalid garrysmod-relative destination; use '/' separators and no dot components" % i, True)
        if to in seen:
            return ("duplicate destination: " + to, True)
        seen.add(to)
        expected = f.get("expected_sha256")
        if not isinstance(expected, str) or (expected != "missing" and re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            return ("STALE_BASE_REQUIRED: %s needs expected_sha256 from the original remote file used as the edit base, or 'missing' for creation. Re-fetch and reconcile stale edits; never attach a fresh hash to old content." % to, True)
        entry = {"to": to, "expected_sha256": expected}
        if restore:
            if "content" in f or "local" in f:
                return ("restore accepts no local/content payload", True)
            backup_id = f.get("backup_id")
            if not isinstance(backup_id, str) or (backup_id != "legacy" and re.fullmatch(r"[0-9]{20}-[0-9a-f]{16}", backup_id) is None):
                return ("restore requires explicit backup_id from fetch history/backups, or 'legacy'", True)
            entry.update(backup_id=backup_id, source_path="backup:" + backup_id)
        else:
            if "backup_id" in f:
                return ("backup_id is only valid for restore", True)
            has_local, has_content = bool(f.get("local")), "content" in f
            if has_local == has_content:
                return ("%s: provide exactly one of local or content" % to, True)
            if has_local:
                data, err = _read_local_capped(f["local"], DEPLOY_FILE_MAX_BYTES)
                if err:
                    return ("%s: %s; nothing deployed" % (to, err), True)
                entry["source_path"] = os.path.abspath(f["local"])
            else:
                if not isinstance(f["content"], str):
                    return ("content must be a string", True)
                data = f["content"].encode("utf-8")
                entry["source_path"] = "inline"
            total += len(data)
            if len(data) > DEPLOY_FILE_MAX_BYTES or total > DEPLOY_BATCH_MAX_INPUT_BYTES:
                return ("deploy file or aggregate payload cap exceeded; nothing deployed", True)
            entry["content_b64"] = base64.b64encode(data).decode()
        entries.append(entry)
    # Never select a stale cached or first-of-many target for a write.
    srv = resolve(server, fresh=True)
    if not srv:
        return ("DEPLOY_TARGET_CHANGED_OR_AMBIGUOUS: fresh discovery failed or did not resolve exactly one server; nothing deployed", True)
    req = {"op": "deploy", "server": server, "uuid": srv["uuid"], "restore": restore,
           "origin": {"client_instance": _CLIENT_INSTANCE, "pid": os.getpid(), "tool_version": MCP_VERSION,
                      "thread_id": os.environ.get("CODEX_THREAD_ID", ""), "source_revision": revision}}
    if batch:
        req["files"] = entries
    else:
        req.update(entries[0])
    res = run_driver(req, timeout=min(240, 60 + 2 * len(entries)))
    log_event({"ev": "deploy_v2", "server": server, "deployment_id": res.get("deployment_id"),
               "files": len(entries), "bytes": total, "restore": restore, "ok": res.get("ok"),
               "n_fail": res.get("n_fail"), "outcome": res.get("outcome")})
    deploy_id = res.get("deployment_id") or "unavailable"
    if not res.get("ok"):
        msg = "deployment %s failed: %s" % (deploy_id, res.get("error"))
        for c in res.get("conflicts", []):
            msg += "\n%s: expected=%s actual=%s" % (c["to"], c["expected_sha256"], c["actual_sha256"])
        if res.get("outcome") == "partial_or_uncertain" or not res.get("deployment_id"):
            msg += "\nInspect fetch what='history' and live hashes before retrying; files may already have changed."
        return (msg, True)
    results = res.get("results") or [res]
    msg = "[%s] %s %d file(s), %d changed bytes; deployment_id=%s" % (
        server.upper(), "restored" if restore else "deployed", len(results), res.get("bytes", 0), deploy_id)
    for r in results:
        msg += "\n%s: sha256=%s%s%s" % (r.get("to"), r.get("after_sha256"),
               " (unchanged)" if r.get("noop") else "",
               " backup_id=" + r["backup_id"] if r.get("backup_id") else "")
    if any(e["to"].endswith(".lua") for e in entries):
        msg += "\nSource bytes verified; runtime reload and client behavior still require verification."
    return (msg, False)



def tool_grep(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    patterns = []
    if args.get("pattern") is not None:
        patterns.append(args.get("pattern"))
    if args.get("patterns") is not None:
        if not isinstance(args["patterns"], list):
            return ("patterns must be an array of regex strings.", True)
        patterns.extend(args["patterns"])
    if not patterns or any(not isinstance(p, str) or not p.strip() for p in patterns):
        return ("provide at least one non-empty pattern or patterns[] entry.", True)
    if len(patterns) > GREP_MAX_PATTERNS:
        return ("too many grep patterns (%d; max %d)." % (len(patterns), GREP_MAX_PATTERNS), True)
    globs = []
    if args.get("glob") is not None:
        globs.append(args.get("glob"))
    if args.get("globs") is not None:
        if not isinstance(args["globs"], list):
            return ("globs must be an array of filename globs.", True)
        globs.extend(args["globs"])
    if not globs:
        globs = ["*.lua"]
    exclude_globs = args.get("exclude_globs") or []
    if not isinstance(exclude_globs, list):
        return ("exclude_globs must be an array.", True)
    if any(not isinstance(g, str) or not g for g in globs + exclude_globs):
        return ("all include/exclude globs must be non-empty strings.", True)
    if len(globs) + len(exclude_globs) > GREP_MAX_GLOBS:
        return ("too many grep globs (%d; max %d total)." %
                (len(globs) + len(exclude_globs), GREP_MAX_GLOBS), True)
    paths = []
    if args.get("path") is not None:
        paths.append(args.get("path"))
    if args.get("paths") is not None:
        if not isinstance(args["paths"], list):
            return ("paths must be an array of paths relative to garrysmod/.", True)
        paths.extend(args["paths"])
    if not paths:
        paths = [""]
    if any(not isinstance(p, str) for p in paths) or len(paths) > GREP_MAX_PATHS:
        return ("invalid paths[] (max %d string roots)." % GREP_MAX_PATHS, True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    res = run_driver({"op": "grep", "uuid": srv["uuid"], "patterns": patterns,
                      "paths": paths, "globs": globs, "exclude_globs": exclude_globs,
                      "max": int(args.get("max", 200))}, timeout=40)
    log_event({"ev": "grep", "server": server, "pattern": "\n".join(patterns),
               "paths": len(paths), "globs": len(globs), "ok": res.get("ok")})
    if not res.get("ok"):
        return ("grep failed: %s" % res.get("error"), True)
    total, shown = res.get("total", 0), res.get("shown", 0)
    total_text = str(total) if res.get("total_exact", True) else (">=%d" % total)
    head = "[%s] grep — %s captured match(es), %d pattern(s), %d include glob(s), %d root(s)%s" % (
        server.upper(), total_text, len(patterns), len(globs), len(paths),
        ("" if total <= shown and not res.get("truncated") else " (showing %d)" % shown))
    if res.get("note"):
        head += "  [%s]" % res["note"]
    matches = res.get("matches", [])
    return (head + ("\n" + "\n".join(matches) if matches else ""), False)


def _format_diff_result(req, res):
    head = "[diff] %s  vs  %s" % (req["label_a"], req["label_b"])
    head += "\nsha256_a=%s sha256_b=%s\n" % (res.get("sha256_a"), res.get("sha256_b"))
    if res.get("equal"):
        return head + " — IDENTICAL (%s bytes, sha1 %s)" % (res.get("size_a"), res.get("sha_a"))
    if res.get("binary"):
        return head + " — BINARY files DIFFER: %s vs %s bytes (sha1 %s vs %s)" % (
            res.get("size_a"), res.get("size_b"), res.get("sha_a"), res.get("sha_b"))
    cap = "  [truncated by output budget]" if res.get("truncated") else ""
    diff = res.get("diff", "")
    if not diff:
        diff = "[no textual diff rendered; sha1 %s vs %s]" % (res.get("sha_a"), res.get("sha_b"))
    return head + " — DIFFER (%s vs %s bytes)%s\n%s" % (
        res.get("size_a"), res.get("size_b"), cap, diff)


def _diff_local_entry(server, srv, path, local, context):
    data, err = _read_local_capped(local, DIFF_FILE_MAX_BYTES)
    if err:
        return None, err.replace(" cap", " diff cap")
    return ({"uuid_a": srv["uuid"], "path_a": path,
             "label_a": "%s:%s" % (server, path),
             "content_b64": base64.b64encode(data).decode(),
             "label_b": "local:%s" % os.path.basename(local), "context": context,
             "_local_bytes": len(data)}, None)


def _diff_server_entry(server, srv, path, server_b, srv_b, path_b, context):
    return {"uuid_a": srv["uuid"], "path_a": path,
            "label_a": "%s:%s" % (server, path),
            "uuid_b": srv_b["uuid"], "path_b": path_b,
            "label_b": "%s:%s" % (server_b, path_b), "context": context}


def _diff_batch(server, srv, args, files):
    if args.get("path") or args.get("path_b") or args.get("local"):
        return ("give EITHER 'files' (batch) OR top-level path/path_b/local (single), not both.", True)
    if not isinstance(files, list) or not files:
        return ("'files' must be a non-empty array of {path, local|path_b} objects.", True)
    if len(files) > DIFF_BATCH_MAX:
        return ("batch too large: %d files (max %d). Split into several calls."
                % (len(files), DIFF_BATCH_MAX), True)
    context = max(0, min(int(args.get("context", 3)), 100))
    server_b = args.get("server_b")
    srv_b = None
    if server_b:
        if server_b not in SERVER_NAMES:
            return ("server_b must be one of: %s" % ", ".join(SERVER_NAMES), True)
        srv_b = resolve(server_b)
        if not srv_b:
            return ("could not resolve server '%s' (host unreachable?)" % server_b, True)
    elif args.get("confirm") is not True:
        return ("BLOCKED: local batch diff reads local files and transmits their contents to the remote "
                "comparison driver. Re-call with confirm=true to authorize that data transfer. Nothing was read.", True)
    entries = []
    local_input_bytes = 0
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return ("files[%d] is not an object." % i, True)
        path = (f.get("path") or "").strip()
        if _bad_deploy_to(path):
            return ("files[%d]: invalid 'path' (%r): give a path relative to garrysmod/."
                    % (i, f.get("path")), True)
        local = f.get("local")
        path_b = (f.get("path_b") or path).strip()
        if server_b:
            if local:
                return ("files[%d] (%s): omit 'local' when top-level server_b is set." % (i, path), True)
            if _bad_deploy_to(path_b):
                return ("files[%d]: invalid 'path_b' (%r): give a path relative to garrysmod/."
                        % (i, f.get("path_b")), True)
            entries.append(_diff_server_entry(server, srv, path, server_b, srv_b, path_b, context))
        else:
            if f.get("path_b"):
                return ("files[%d] (%s): path_b requires top-level server_b." % (i, path), True)
            if not local:
                return ("files[%d] (%s): provide 'local', or set top-level server_b for server-to-server batch mode."
                        % (i, path), True)
            entry, err = _diff_local_entry(server, srv, path, local, context)
            if err:
                return ("files[%d] (%s): %s. No remote diff was run." % (i, path, err), True)
            local_input_bytes += entry.pop("_local_bytes", 0)
            if local_input_bytes > DIFF_BATCH_MAX_INPUT_BYTES:
                return ("local batch input exceeds the %d-byte diff cap. No remote diff was run."
                        % DIFF_BATCH_MAX_INPUT_BYTES, True)
            entries.append(entry)
    maxbytes = max(0, min(int(args.get("maxbytes", DIFF_BATCH_DEFAULT_MAXBYTES)), DIFF_BATCH_MAXBYTES))
    res = run_driver({"op": "diff", "files": entries, "maxbytes": maxbytes},
                     timeout=min(240, 60 + len(entries)))
    log_event({"ev": "diff_batch", "server": server, "vs": (server_b or "local"),
               "files": len(entries), "ok": res.get("ok"), "n_equal": res.get("n_equal"),
               "n_differ": res.get("n_differ"), "n_fail": res.get("n_fail")})
    if not res.get("ok"):
        return ("batch diff failed: %s" % res.get("error"), True)
    results = res.get("results") or []
    n_equal = res.get("n_equal", 0)
    n_differ = res.get("n_differ", 0)
    n_fail = res.get("n_fail", 0)
    msg = ("[batch diff] %s vs %s — %d files: %d IDENTICAL, %d DIFFER, %d FAILED; "
           "one SSH round-trip" % (server, server_b or "local", len(entries), n_equal, n_differ, n_fail))
    details = []
    for i, entry in enumerate(entries):
        if i >= len(results):
            details.append("FAILED  %s  vs  %s — missing driver result" %
                           (entry["label_a"], entry["label_b"]))
            n_fail += 1
            continue
        item = results[i]
        if not item.get("ok"):
            details.append("FAILED  %s  vs  %s — %s" %
                           (entry["label_a"], entry["label_b"], item.get("error")))
        else:
            details.append(_format_diff_result(entry, item))
    if details:
        msg += "\n\n" + "\n\n".join(details)
    if res.get("n_truncated"):
        msg += "\n%d differing diff(s) were truncated to the %d-byte aggregate output budget." % (
            res["n_truncated"], res.get("maxbytes", maxbytes))
    _recent_diff_singles.pop((server, server_b or "local"), None)
    return (msg, n_fail > 0)


def tool_diff(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if args.get("files") is not None:
        return _diff_batch(server, srv, args, args.get("files"))
    path = (args.get("path") or "").strip()
    if _bad_deploy_to(path):
        return ("invalid 'path': give a path relative to garrysmod/ with no '..' or drive/absolute prefix.", True)
    server_b = args.get("server_b")
    local = args.get("local")
    if bool(server_b) == bool(local):
        return ("give exactly ONE of: server_b (compare against another server) or local (a local file path).", True)
    context = max(0, min(int(args.get("context", 3)), 100))
    if local:
        if args.get("confirm") is not True:
            return ("BLOCKED: local diff reads a local file and transmits its contents to the remote comparison "
                    "driver. Re-call with confirm=true to authorize that data transfer. Nothing was read.", True)
        req, err = _diff_local_entry(server, srv, path, local, context)
        if err:
            return (err, True)
        req.pop("_local_bytes", None)
    else:
        if server_b not in SERVER_NAMES:
            return ("server_b must be one of: %s" % ", ".join(SERVER_NAMES), True)
        srv_b = resolve(server_b)
        if not srv_b:
            return ("could not resolve server '%s' (host unreachable?)" % server_b, True)
        path_b = (args.get("path_b") or path).strip()
        if _bad_deploy_to(path_b):
            return ("invalid 'path_b': give a path relative to garrysmod/ with no '..' or drive/absolute prefix.", True)
        req = _diff_server_entry(server, srv, path, server_b, srv_b, path_b, context)
    res = run_driver(dict(req, op="diff"), timeout=60)
    log_event({"ev": "diff", "server": server, "path": path,
               "vs": (server_b or "local"), "ok": res.get("ok"), "equal": res.get("equal")})
    if not res.get("ok"):
        return ("diff failed: %s" % res.get("error"), True)
    msg = _format_diff_result(req, res)
    msg += _diff_trickle_note(server, server_b or "local", path, record=True)
    return (msg, False)


def tool_nodeinfo(args):
    res = run_driver({"op": "nodeinfo",
                      "wings_log_lines": int(args.get("wings_log_lines", 0)),
                      "dmesg_lines": int(args.get("dmesg_lines", 0))}, timeout=60)
    log_event({"ev": "nodeinfo", "ok": res.get("ok")})
    if not res.get("ok"):
        return ("nodeinfo failed: %s" % res.get("error"), True)
    i = res.get("info", {})
    out = ["Node health:"]
    if i.get("loadavg"):
        out.append("  load (1/5/15m): %s" % " ".join(i["loadavg"]))
    m = i.get("mem_mb") or {}
    if m:
        out.append("  mem: %s MB available of %s MB  (swap free %s of %s MB)" % (
            m.get("MemAvailable"), m.get("MemTotal"), m.get("SwapFree"), m.get("SwapTotal")))
    if i.get("uptime_h") is not None:
        out.append("  uptime: %s h" % i["uptime_h"])
    if i.get("disk"):
        out.append("  disk:\n    " + i["disk"].replace("\n", "\n    "))
    if i.get("docker"):
        out.append("  docker stats (name|cpu|mem):\n    " + i["docker"].replace("\n", "\n    "))
    if i.get("wings_log"):
        out.append("--- wings.log tail ---\n" + i["wings_log"].rstrip())
    if i.get("dmesg"):
        out.append("--- dmesg tail ---\n" + i["dmesg"].rstrip())
    return ("\n".join(out), False)


CLIENTLUA_BOOTSTRAP = r'''
local M="srcds_mcp_client_v1"
local READY="@READY_TOKEN@"
_G.__SRCDS_MCP_CLIENT_V1=_G.__SRCDS_MCP_CLIENT_V1 or {parts={}}
local S=_G.__SRCDS_MCP_CLIENT_V1
net.Receive(M,function(bits)
  if bits>524288 then return end
  if net.ReadString()~="payload" then return end
  local tok=net.ReadString()
  local idx,total=net.ReadUInt(16),net.ReadUInt(16)
  local compressed=net.ReadBool()
  local rawlen=net.ReadUInt(32)
  local crc=net.ReadString()
  local n=net.ReadUInt(16)
  if #tok~=16 or not tok:match("^[a-f0-9]+$") or rawlen>65536 or
     #crc>10 or not crc:match("^%d+$") or idx<1 or total<1 or total>2 or idx>total or n>48000 then return end
  local part=net.ReadData(n)
  if not part or #part~=n then return end
  local q=S.parts[tok]
  if not q then
    q={chunks={},got=0,total=total,compressed=compressed,rawlen=rawlen,crc=crc}
    S.parts[tok]=q
    timer.Create("srcds_mcp_client_gc_"..tok,20,1,function() S.parts[tok]=nil end)
  end
  if q.total~=total or q.compressed~=compressed or q.rawlen~=rawlen or q.crc~=crc then return end
  if not q.chunks[idx] then q.chunks[idx]=part q.got=q.got+1 end
  if q.got<q.total then return end
  timer.Remove("srcds_mcp_client_gc_"..tok)
  S.parts[tok]=nil
  local packed=table.concat(q.chunks)
  local src=q.compressed and util.Decompress(packed) or packed
  local status,detail="ok",""
  if not src or #src~=q.rawlen or util.CRC(src)~=q.crc then
    status,detail="transfer_error","payload length/checksum mismatch"
  else
    local fn=CompileString(src,"mcp_clientlua_"..tok,false)
    if type(fn)~="function" then
      status,detail="compile_error",tostring(fn)
    else
      local oldhook,oldmask,oldcount=debug.gethook()
      local function restorehook()
        if oldhook then debug.sethook(oldhook,oldmask,oldcount) else debug.sethook() end
      end
      local ins=0
      debug.sethook(function()
        ins=ins+1
        if ins>200 then restorehook() error("[mcp client] instruction budget exceeded",2) end
      end,"",100000)
      local ok,err=xpcall(fn,function(e) return tostring(e).."\n"..debug.traceback("",2) end)
      restorehook()
      if not ok then status,detail="runtime_error",tostring(err) end
    end
  end
  detail=tostring(detail or ""):gsub("[\r\n]+"," / "):sub(1,600)
  net.Start(M)
  net.WriteString("ack")
  net.WriteString(tok)
  net.WriteString(status)
  net.WriteString(detail)
  net.SendToServer()
end)
local function ready(attempt)
  local ok=pcall(function()
    net.Start(M)
    net.WriteString("ready")
    net.WriteString(READY)
    net.SendToServer()
  end)
  if not ok and attempt<12 then timer.Simple(0.25,function() ready(attempt+1) end) end
end
ready(1)
'''

if len(CLIENTLUA_BOOTSTRAP.encode("utf-8")) > 5800:
    raise RuntimeError("clientlua bootstrap exceeds Player:SendLua's 6000-byte limit")


CLIENTLUA_BODY = r'''
local _tok = "@TOKEN@"
local _tgt = @TARGET_LUA@
local _code = util.Base64Decode("@B64@")
local _boot = util.Base64Decode("@BOOT_B64@")
local MSG = "srcds_mcp_client_v1"
local CHUNK = 48000
local ACK_TIMEOUT = @ACK_TIMEOUT@
local MAX_RECIPIENTS = @MAX_RECIPIENTS@

if not isstring(_code) or not isstring(_boot) then error("clientlua payload decode failed", 0) end

local targets = {}
for _, p in ipairs(player.GetAll()) do
  if IsValid(p) and p:IsPlayer() and not p:IsBot() and p:IsFullyAuthenticated() then
    if _tgt == "all" or p:SteamID() == _tgt or tostring(p:SteamID64()) == _tgt then
      targets[#targets + 1] = p
    end
  end
end
if #targets == 0 then error("zero fully authenticated human clients matched target", 0) end
if #targets > MAX_RECIPIENTS then
  error("recipient count " .. #targets .. " exceeds safety cap " .. MAX_RECIPIENTS, 0)
end

local msg_id = util.AddNetworkString(MSG)
if not msg_id or msg_id == 0 then error("could not pool clientlua network message", 0) end
_G.__SRCDS_MCP_CLIENT_SERVER_V1 = _G.__SRCDS_MCP_CLIENT_SERVER_V1 or { pending = {} }
local STATE = _G.__SRCDS_MCP_CLIENT_SERVER_V1

-- One generic receiver serves concurrent tokenized requests. It never trusts a
-- token alone: the sender must be one of that request's exact player entities.
net.Receive(MSG, function(bits, ply)
  if bits > 32768 then return end
  local kind = net.ReadString()
  local tok = net.ReadString()
  local state = rawget(_G, "__SRCDS_MCP_CLIENT_SERVER_V1")
  local pending = state and state.pending and state.pending[tok]
  if not pending or not pending.want[ply] then return end
  if kind == "ready" then
    if pending.ready[ply] then return end
    pending.ready[ply] = true
    pending.ready_count = pending.ready_count + 1
    pending.send(ply)
    return
  end
  if kind ~= "ack" or not pending.ready[ply] or pending.seen[ply] then return end
  local status = net.ReadString()
  local detail = net.ReadString()
  if status ~= "ok" and status ~= "compile_error" and status ~= "runtime_error" and status ~= "transfer_error" then
    status = "invalid_ack"
  end
  pending.seen[ply] = true
  pending.acked = pending.acked + 1
  pending.counts[status] = (pending.counts[status] or 0) + 1
  if status ~= "ok" and #pending.errors < 10 then
    pending.errors[#pending.errors + 1] = "client#" .. pending.want[ply] .. " " .. status .. " " ..
      tostring(detail or ""):gsub("[\r\n]+", " / "):sub(1, 600)
  end
  if pending.acked >= pending.expected then pending.finish() end
end)

local pending = {
  want = {}, seen = {}, ready = {}, sent = {}, counts = {}, errors = {},
  ready_count = 0, acked = 0,
  expected = #targets, done = false,
}
for i, ply in ipairs(targets) do pending.want[ply] = i end
STATE.pending[_tok] = pending

pending.finish = function()
  if pending.done then return end
  pending.done = true
  timer.Remove("srcds_mcp_client_timeout_" .. _tok)
  STATE.pending[_tok] = nil
  local okn = pending.counts.ok or 0
  local timeoutn = pending.expected - pending.acked
  local compile_n = pending.counts.compile_error or 0
  local runtime_n = pending.counts.runtime_error or 0
  local transfer_n = (pending.counts.transfer_error or 0) + (pending.counts.invalid_ack or 0)
  LOG("clientlua ack: sent=" .. pending.expected .. " ready=" .. pending.ready_count ..
      " acked=" .. pending.acked .. " ok=" .. okn ..
      " compile_error=" .. compile_n .. " runtime_error=" .. runtime_n ..
      " transfer_error=" .. transfer_n .. " timeout=" .. timeoutn)
  for _, line in ipairs(pending.errors) do LOG(line) end
  CHECK(okn == pending.expected,
        "client execution acknowledgements incomplete/failed: ok=" .. okn .. "/" .. pending.expected)
  MCP_DONE()
end

local packed = util.Compress(_code)
local compressed = isstring(packed) and #packed < #_code
if not compressed then packed = _code end
local total = math.ceil(#packed / CHUNK)
local crc = util.CRC(_code)

pending.send = function(ply)
  if pending.done or pending.sent[ply] or not IsValid(ply) then return end
  pending.sent[ply] = true
  for i = 1, total do
    local part = packed:sub((i - 1) * CHUNK + 1, i * CHUNK)
    net.Start(MSG)
    net.WriteString("payload")
    net.WriteString(_tok)
    net.WriteUInt(i, 16)
    net.WriteUInt(total, 16)
    net.WriteBool(compressed)
    net.WriteUInt(#_code, 32)
    net.WriteString(crc)
    net.WriteUInt(#part, 16)
    net.WriteData(part, #part)
    net.Send(ply)
  end
end

timer.Create("srcds_mcp_client_timeout_" .. _tok, ACK_TIMEOUT, 1, pending.finish)

-- The fixed net name may have been pooled for the first time above. Give its
-- string-table update time to reach clients, then let each bootstrap explicitly
-- announce receiver readiness before pending.send transmits any payload.
timer.Simple(0.5, function()
  if pending.done then return end
  for _, ply in ipairs(targets) do
    local ok, err = pcall(ply.SendLua, ply, _boot)
    if not ok and not pending.seen[ply] then
      pending.seen[ply] = true
      pending.acked = pending.acked + 1
      pending.counts.transfer_error = (pending.counts.transfer_error or 0) + 1
      if #pending.errors < 10 then
        pending.errors[#pending.errors + 1] = "client#" .. pending.want[ply] ..
          " transfer_error bootstrap dispatch failed: " .. tostring(err):gsub("[\r\n]+", " / "):sub(1, 500)
      end
    end
  end
  if pending.acked >= pending.expected then timer.Simple(0, pending.finish) end
end)

return #targets
'''


def tool_clientlua(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    code = args.get("code", "")
    if not code.strip():
        return ("code is empty", True)
    code_bytes = code.encode("utf-8")
    if len(code_bytes) > CLIENTLUA_MAX_BYTES:
        return ("clientlua code too large: %d UTF-8 bytes (max %d)" %
                (len(code_bytes), CLIENTLUA_MAX_BYTES), True)
    if "target" not in args or not isinstance(args.get("target"), str) or not args["target"].strip():
        return ("target is required: give an exact SteamID/SteamID64, or explicit target='all'.", True)
    target = args["target"].strip()
    if target != "all" and not (re.fullmatch(r"STEAM_[0-5]:[01]:\d+", target, re.I) or
                                re.fullmatch(r"\d{17}", target)):
        return ("target must be 'all', an exact SteamID, or a 17-digit SteamID64; nicknames are not accepted.", True)
    if target.lower().startswith("steam_"):
        target = target.upper()
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if not srv["running"]:
        return ("%s is DOWN — no clients connected." % server.upper(), True)
    if args.get("confirm") is not True:
        players, maxpl, is_live = live_info(srv)
        ln = ("  (%d/%s players%s)" % (players, maxpl, " — LIVE" if is_live else "")) if players is not None else ""
        return ("BLOCKED: runs clientside Lua on %s clients%s. Re-call with confirm=true." % (server, ln), True)
    if target == "all" and args.get("broadcast") is not True:
        return ("REFUSED: target='all' additionally requires broadcast=true to make the fan-out explicit.", True)
    if target == "all" and args.get("force") is not True:
        return ("REFUSED: broadcasting executable client Lua additionally requires force=true.", True)
    tok = os.urandom(8).hex()
    cb64 = base64.b64encode(code_bytes).decode()
    bootstrap = CLIENTLUA_BOOTSTRAP.replace("@READY_TOKEN@", tok)
    boot64 = base64.b64encode(bootstrap.encode("utf-8")).decode()
    body = (CLIENTLUA_BODY
            .replace("@TOKEN@", tok)
            .replace("@TARGET_LUA@", json.dumps(target))
            .replace("@B64@", cb64)
            .replace("@BOOT_B64@", boot64)
            .replace("@ACK_TIMEOUT@", str(CLIENTLUA_ACK_TIMEOUT))
            .replace("@MAX_RECIPIENTS@", str(CLIENTLUA_MAX_RECIPIENTS)))
    runner = render_runner(tok, "_mcp/%s_body.lua" % tok, True)
    res = run_driver({"op": "lua", "uuid": srv["uuid"], "token": tok, "body": body,
                      "runner": runner, "async": True,
                      "async_timeout": CLIENTLUA_ACK_TIMEOUT + 3}, timeout=60)
    log_event({"ev": "clientlua", "server": server, "target": target,
               "bytes": len(code_bytes), "broadcast": target == "all", "ok": res.get("ok")})
    if not res.get("ok"):
        return ("clientlua failed: %s" % res.get("error"), True)
    r = res.get("result") or {}
    note = r.get("note") or res.get("note") or ""
    if not r.get("started"):
        return ("clientlua failed before the server runner started%s" %
                ((": " + note) if note else ""), True)
    if not r.get("ended"):
        return ("clientlua acknowledgement window did not complete%s" %
                ((": " + note) if note else ""), True)
    if r.get("err"):
        return ("clientlua error: " + r["err"], True)
    try:
        sent = int(r.get("ret"))
    except (TypeError, ValueError):
        return ("clientlua failed: server did not return a recipient count", True)
    if sent <= 0:
        return ("clientlua failed: zero eligible clients were targeted", True)
    summ = r.get("sum") or ""
    sm = re.fullmatch(r"p=(\d+) f=(\d+)", summ.strip())
    if not sm or (int(sm.group(1)) + int(sm.group(2))) != 1:
        return ("clientlua failed: missing or malformed execution acknowledgement summary", True)
    nfail = int(sm.group(2))
    ack = re.search(
        r"clientlua ack: sent=(\d+) ready=(\d+) acked=(\d+) ok=(\d+) compile_error=(\d+) "
        r"runtime_error=(\d+) transfer_error=(\d+) timeout=(\d+)",
        r.get("out") or "",
    )
    if not ack:
        return ("clientlua failed: acknowledgement counters were not returned", True)
    sent_reported, ready_n, acked, okn, compile_n, runtime_n, transfer_n, timeout_n = (
        int(v) for v in ack.groups()
    )
    counters_valid = (
        sent_reported == sent
        and 0 <= ready_n <= sent
        and 0 <= acked <= sent
        and okn + compile_n + runtime_n <= ready_n
        and okn + compile_n + runtime_n + transfer_n == acked
        and timeout_n == sent - acked
        and ((nfail == 0) == (okn == sent and ready_n == sent))
    )
    if not counters_valid:
        return ("clientlua failed: inconsistent execution acknowledgement counters", True)
    lines = ["[%s] clientlua synchronous execution ACK → target=%s, recipients=%d" %
             (server.upper(), "all" if target == "all" else "specific", sent)]
    if r.get("out"):
        lines.append(r["out"])
    for failure in (r.get("fails") or [])[:10]:
        lines.append("[FAIL] " + failure)
    if nfail:
        lines.append("Client-reported synchronous execution failed or timed out; no visual/player acceptance is implied.")
    else:
        lines.append("All targeted clients acknowledged synchronous execution; visual/player acceptance remains separate.")
    return ("\n".join(lines), nfail > 0)


def tool_power(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    action = args.get("action", "")
    if action not in ("start", "stop", "restart", "kill", "watch"):
        return ("action must be one of: start, stop, restart, kill, watch", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if action == "watch":
        # Read-only boot progress check (no confirm). The Pterodactyl end owns the
        # boot marker: wings turns state "starting"->"running" on the egg's
        # startup-done line; the armed watcher records that transition.
        wait = max(0, min(int(args.get("wait", 0)), 55))
        res = run_driver({"op": "bootwatch", "uuid": srv["uuid"], "mode": "poll",
                          "wait": wait}, timeout=wait + 35)
        log_event({"ev": "power_watch", "server": server, "wait": wait, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("boot watch failed: %s" % res.get("error"), True)
        d = res.get("watch") or {}
        live = res.get("state") or ("? (%s)" % res.get("state_err"))
        phase = d.get("phase")
        head = "[%s] boot watch — wings state: %s" % (server.upper(), live)
        if phase == "booted":
            return (head + "\nBOOT COMPLETE: Pterodactyl marked it running %ss after the power action. "
                           "Verify with srcds_status / srcds_lua." % d.get("t_boot"), False)
        if phase == "died_during_boot":
            return (head + "\nDIED DURING BOOT: went offline again before reaching running "
                           "(history: %s). Check srcds_fetch console." % json.dumps(d.get("history")), True)
        if phase == "timeout":
            return (head + "\nWatcher gave up after 15min without seeing running.", True)
        if phase == "watching":
            return (head + "\nStill booting (%ss elapsed, history: %s). Call action='watch' with "
                           "wait=50 again — it returns early on BOOT COMPLETE."
                    % (d.get("elapsed"), json.dumps(d.get("history"))), False)
        return (head + "\nNo watcher armed for the last power action; the live wings state above "
                       "is all we know. (start/restart arm one automatically.)", False)
    players, maxpl, is_live = live_info(srv)
    if args.get("confirm") is not True:
        ln = ("  Currently %d/%s players%s." % (players, maxpl, " — LIVE!" if is_live else "")) if players is not None else ""
        return ("BLOCKED: power %s on %s.%s Re-call with confirm=true." % (action.upper(), server, ln), True)
    if action in ("stop", "restart", "kill") and args.get("force") is not True:
        if players is None:
            return ("REFUSED: %s player population is UNKNOWN (A2S unavailable) — %s could disrupt connected "
                    "players. Re-call with force=true to override." % (server.upper(), action), True)
        if players > 0:
            return ("REFUSED: %s has %d connected player(s) — %s would disrupt them. Re-call with "
                    "force=true to override." % (server.upper(), players, action), True)
    res = run_driver({"op": "power", "uuid": srv["uuid"], "action": action}, timeout=100)
    log_event({"ev": "power", "server": server, "action": action, "confirm": True,
               "force": bool(args.get("force")), "via": res.get("via"), "ok": res.get("ok")})
    via = res.get("via", "?")
    if not res.get("ok"):
        return ("power %s failed (rc=%s, via=%s): %s\n(Routed through the wings API like the panel; "
                "if wings is down, use the Pterodactyl panel at %s.)"
                % (action, res.get("rc"), via, res.get("error") or res.get("out"), PANEL_URL), True)
    note = "  Graceful (normal quit, not a crash)." if via == "wings" else "  (docker fallback - wings was unreachable.)"
    tail = "\nWatch it with srcds_status."
    if action in ("start", "restart") and args.get("watch", True):
        arm = run_driver({"op": "bootwatch", "uuid": srv["uuid"], "mode": "arm",
                          "action": action}, timeout=25)
        if arm.get("ok"):
            tail = ("\nBoot watcher armed (tracks Pterodactyl's starting->running marker). "
                    "Poll srcds_power {action:'watch', wait:50} — it returns when the boot completes.")
        else:
            tail = "\n(boot watcher failed to arm: %s — fall back to srcds_status polling.)" % arm.get("error")
    return ("[%s] %s OK via %s. %s%s%s"
            % (server.upper(), action.upper(), via, res.get("out", ""), note, tail), False)


def tool_monitor(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    action = args.get("action")
    if not action:
        # ergonomic inference: pattern/watch given -> arm; id given -> check; else list
        action = "arm" if (args.get("pattern") or args.get("watch")) else ("check" if args.get("id") else "list")
    if action == "arm":
        watch = args.get("watch") or ("pattern" if args.get("pattern") else None)
        if watch not in ("pattern", "down", "up"):
            return ("watch must be 'pattern' (console regex), 'down' or 'up' (state transition).", True)
        pattern = (args.get("pattern") or "").strip()
        if watch == "pattern":
            if not pattern:
                return ("give 'pattern' (python regex, e.g. '(?i)lua error').", True)
            try:
                re.compile(pattern)
            except re.error as e:
                return ("bad regex: %s" % e, True)
            if not srv.get("running"):
                return ("%s is DOWN — no console to follow. Use watch:'up' to be told when it boots." % server.upper(), True)
        timeout_min = max(1, min(int(args.get("timeout_min", 30)), 240))
        mid = os.urandom(4).hex()
        res = run_driver({"op": "monitor", "uuid": srv["uuid"], "act": "arm", "id": mid,
                          "mode": watch, "pattern": pattern, "timeout_s": timeout_min * 60}, timeout=30)
        log_event({"ev": "monitor_arm", "server": server, "watch": watch,
                   "pattern": pattern[:120], "id": mid, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("arm failed: %s" % res.get("error"), True)
        if not res.get("statefile"):
            return ("arm failed: watcher process did not come up (python3/docker missing on host?)", True)
        what = ("console regex /%s/" % pattern) if watch == "pattern" else ("server going %s" % watch.upper())
        return ("[%s] monitor ARMED — id=%s, watching %s for %d min.\n"
                "Poll: srcds_monitor {server:'%s', id:'%s', wait:50} — returns early on a hit. "
                "Disarm: action:'stop'." % (server.upper(), mid, what, timeout_min, server, mid), False)
    if action == "list":
        res = run_driver({"op": "monitor", "uuid": srv["uuid"], "act": "list"}, timeout=30)
        log_event({"ev": "monitor_list", "server": server, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("list failed: %s" % res.get("error"), True)
        mons = res.get("monitors") or []
        if not mons:
            return ("[%s] no monitors (arm one with watch:'pattern'+pattern, or watch:'down'/'up')." % server.upper(), False)
        out = ["[%s] monitors:" % server.upper()]
        for m in mons:
            out.append("  %s  %-9s %-8s matches=%-3s age=%dm  %s" % (
                m.get("id"), m.get("phase"), m.get("mode"), m.get("matches"),
                int((m.get("age_s") or 0) / 60), m.get("pattern") or ""))
        return ("\n".join(out), False)
    mid = (args.get("id") or "").strip()
    if action == "stop":
        res = run_driver({"op": "monitor", "uuid": srv["uuid"], "act": "stop", "id": mid}, timeout=30)
        log_event({"ev": "monitor_stop", "server": server, "id": mid, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("stop failed: %s" % res.get("error"), True)
        d = res.get("watch") or {}
        return ("[%s] monitor %s stopped (%s, %d matches recorded)." % (
            server.upper(), mid, d.get("phase"), d.get("match_count", 0)), False)
    if action != "check":
        return ("action must be one of: arm, check, stop, list", True)
    wait = max(0, min(int(args.get("wait", 0)), 55))
    res = run_driver({"op": "monitor", "uuid": srv["uuid"], "act": "check", "id": mid,
                      "wait": wait, "after": int(args.get("after", 0))}, timeout=wait + 30)
    log_event({"ev": "monitor_check", "server": server, "id": mid, "wait": wait, "ok": res.get("ok")})
    if not res.get("ok"):
        return ("check failed: %s" % res.get("error"), True)
    d = res.get("watch") or {}
    ph = (d.get("phase") or "?").upper()
    mode = d.get("mode")
    lines = ["[%s] monitor %s — %s (%s, %.1f min in)" % (server.upper(), mid, ph, mode,
                                                         (d.get("elapsed") or 0) / 60.0)]
    if mode == "pattern":
        mc = d.get("match_count", 0)
        matches = d.get("matches") or []
        if mc:
            lines[0] += "  matches=%d" % mc
            if mc > 15:
                lines.append("  (showing last 15 of %d — pass after:%d to await the next)" % (mc, mc))
            for t, l in matches[-15:]:
                lines.append("  [+%ss] %s" % (t, l))
    else:
        hist = d.get("history") or []
        if hist:
            lines.append("  state: " + " -> ".join("%s@+%ss" % (s, t) for t, s in hist[-8:]))
        ctx = d.get("context") or []
        if ctx:
            lines.append("--- last %d console lines at the event ---" % len(ctx))
            lines += ["  " + l for l in ctx[-25:]]
    if d.get("note"):
        lines.append("  (note: %s)" % d["note"])
    if d.get("phase") == "watching":
        lines.append("  still watching — re-check with wait:50, or action:'stop' to disarm.")
    return ("\n".join(lines), False)


# ----------------------------------------------------------------------------
# Database (MariaDB) tools — query via `docker exec` into the mariadb container,
# root password read from the container env ($MYSQL_ROOT_PASSWORD), never extracted.
# ----------------------------------------------------------------------------
# Convenience aliases: a game name -> its main schema. Any real schema name also works.
DB_ALIAS = CFG.get("db_aliases") or {}
DB_READ_FIRST = {"select", "show", "describe", "desc", "explain", "use", "help", "checksum"}


def _resolve_db(database):
    if not database:
        return None
    d = database.strip()
    return DB_ALIAS.get(d.lower(), d)


def _db_name_ok(d):
    return bool(d) and all(c.isalnum() or c == "_" for c in d)


def _strip_sql(sql):
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)     # /* */ comments
    s = re.sub(r"--[^\n]*", " ", s)                     # -- comments
    s = re.sub(r"#[^\n]*", " ", s)                      # # comments
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)           # single-quoted strings
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)           # double-quoted strings
    return s


def classify_db(sql):
    """None only for statements permitted inside the read-only transaction path."""
    if re.search(r"(?is)/\*\s*(?:!|M!)", sql):
        return "MariaDB/MySQL executable comment"
    s = _strip_sql(sql)
    # a SELECT ... INTO OUTFILE/DUMPFILE writes a file despite the read-looking leading keyword
    if re.search(r"(?i)\binto\s+(outfile|dumpfile)\b", s):
        return "SELECT ... INTO OUTFILE/DUMPFILE (writes a file)"
    for st in (x.strip() for x in s.split(";") if x.strip()):
        m = re.match(r"(?i)\s*([a-z_]+)", st)
        kw = m.group(1).lower() if m else ""
        if kw not in DB_READ_FIRST:
            return "non-read statement '%s'" % (kw or "?")
    return None


def tool_db_query(args):
    database = _resolve_db(args.get("database"))
    if database and not _db_name_ok(database):
        return ("invalid database name (letters/digits/underscore only)", True)
    sql = args.get("sql", "")
    if not sql.strip():
        return ("sql is empty", True)
    reason = classify_db(sql)
    if reason and args.get("confirm") is not True:
        log_event({"ev": "db_blocked", "database": database, "reason": reason, "sql": sql[:200]})
        return ("BLOCKED: this SQL is a WRITE/DDL (%s) against the LIVE game DB '%s' — it changes real "
                "player/server data. Re-call with confirm=true to run it. Nothing was executed."
                % (reason, database or "?"), True)
    res = run_driver({"op": "db", "sql": sql, "database": database,
                      "format": args.get("format", "tsv"),
                      "read_only": reason is None}, timeout=60)
    log_event({"ev": "db_query", "database": database, "write": bool(reason),
               "confirm": bool(args.get("confirm")), "sql": sql[:200], "ok": res.get("ok")})
    if not res.get("ok"):
        return ("db query failed: %s" % (res.get("error_out") or res.get("error")), True)
    out = res.get("output", "") or "(no rows / empty result set)"
    if res.get("error_out"):
        out += "\n[mysql] " + res["error_out"]
    if res.get("truncated"):
        out += "\n... (output truncated — add a LIMIT or narrow the query)"
    return ("[db:%s]\n%s" % (database or "(server default)", out), False)


def tool_db_schema(args):
    database = _resolve_db(args.get("database"))
    table = args.get("table")
    if database and not _db_name_ok(database):
        return ("invalid database name", True)
    fmt = args.get("format", "tsv")
    if not database:
        sql = "SHOW DATABASES;"
    elif not table:
        sql = ("SELECT table_name AS tbl, table_rows AS approx_rows, "
               "ROUND(data_length/1024) AS data_kb, ROUND(index_length/1024) AS idx_kb "
               "FROM information_schema.tables WHERE table_schema='%s' ORDER BY table_name;" % database)
    else:
        if not _db_name_ok(table):
            return ("invalid table name", True)
        sql = "DESCRIBE `%s`.`%s`; SHOW INDEX FROM `%s`.`%s`;" % (database, table, database, table)
    res = run_driver({"op": "db", "sql": sql, "format": fmt,
                      "read_only": True}, timeout=30)
    log_event({"ev": "db_schema", "database": database, "table": table, "ok": res.get("ok")})
    if not res.get("ok"):
        return ("db schema failed: %s" % (res.get("error_out") or res.get("error")), True)
    what = ("databases" if not database else ("tables in %s" % database if not table else "%s.%s" % (database, table)))
    return ("[db schema: %s]\n%s" % (what, res.get("output", "") or "(empty)"), False)


# ----------------------------------------------------------------------------
# Database (MongoDB) tools — mongosh via `docker exec` into the mongo container;
# root credentials are read from the container env ($MONGO_INITDB_ROOT_USERNAME /
# $MONGO_INITDB_ROOT_PASSWORD) inside the container and never leave it.
# ----------------------------------------------------------------------------
MONGO_CFG = CFG.get("mongo") or {}
MONGO_ALIAS = CFG.get("mongo_aliases") or {}

# Explicitly-mutating methods: named so the block message can say WHAT it caught.
MONGO_WRITE_METHODS = {
    "insert", "insertone", "insertmany", "update", "updateone", "updatemany",
    "replaceone", "delete", "deleteone", "deletemany", "remove", "save",
    "findandmodify", "findoneandupdate", "findoneanddelete", "findoneandreplace",
    "bulkwrite", "drop", "dropdatabase", "dropindex", "dropindexes",
    "createindex", "createindexes", "createcollection", "createview",
    "renamecollection", "converttocapped", "compact", "reindex", "validate",
    "mapreduce", "runcommand", "admincommand", "eval",
    "createuser", "dropuser", "updateuser", "changeuserpassword",
    "grantrolestouser", "revokerolesfromuser", "createrole", "droprole",
    "shutdownserver", "killop", "setparameter", "fsynclock", "fsyncunlock",
    "setprofilinglevel", "cleanuporphaned", "watch",
}

# Everything a read-only query legitimately needs: collection/db read methods plus
# the JS/cursor helpers that show up in real queries. Anything NOT here is treated
# as a write (fail closed) and needs confirm=true.
MONGO_READ_METHODS = {
    # collection / db reads
    "find", "findone", "aggregate", "count", "countdocuments",
    "estimateddocumentcount", "distinct", "getindexes", "getindexkeys",
    "stats", "datasize", "storagesize", "totalsize", "totalindexsize",
    "getcollectionnames", "getcollectioninfos", "getcollection", "getsiblingdb",
    "getdb", "getname", "getmongo", "exists", "iscapped", "explain", "itcount",
    "listcollections", "listcommands", "version", "hello", "serverstatus",
    "hostinfo", "buildinfo", "getprofilinglevel", "currentop",
    # cursor shaping
    "limit", "skip", "sort", "project", "hint", "batchsize", "maxtimems",
    "collation", "toarray", "hasnext", "next", "pretty", "objsleftinbatch",
    "readpref", "allowdiskuse", "tojson", "toobject",
    # JS / formatting helpers
    "map", "filter", "reduce", "foreach", "some", "every", "flat", "flatmap",
    "slice", "splice", "concat", "join", "split", "push", "pop", "shift",
    "unshift", "reverse", "sortdoc", "keys", "values", "entries", "fromentries",
    "assign", "stringify", "parse", "print", "printjson", "tostring",
    "tofixed", "toprecision", "tolocalestring", "toisostring", "tolowercase",
    "touppercase", "trim", "trimstart", "trimend", "padstart", "padend",
    "repeat", "includes", "indexof", "lastindexof", "startswith", "endswith",
    "replace", "replaceall", "match", "matchall", "test", "exec", "search",
    "isarray", "isnan", "isinteger", "from", "of", "round", "floor", "ceil",
    "abs", "min", "max", "pow", "sqrt", "random", "gettime", "getfullyear",
    "getmonth", "getdate", "gethours", "getminutes", "getseconds", "now",
    "localecompare", "charat", "charcodeat", "substring", "substr", "sort",
    "number", "string", "boolean", "date", "objectid", "isodate", "long",
    "call", "apply", "bind", "hasownproperty", "getownpropertynames",
    "defineproperty", "add", "has", "get", "set",   # Map/Set helpers
}

_MONGO_METHOD_RE = re.compile(r"\.\s*([A-Za-z_$][\w$]*)\s*\(")


def _strip_js(js):
    s = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)         # /* */ comments
    s = re.sub(r"//[^\n]*", " ", s)                        # // comments
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)              # '...'
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)              # "..."
    s = re.sub(r"`(?:\\.|[^`\\])*`", "``", s)              # `...`
    return s


def classify_mongo(js):
    """None if the script only calls read-only methods; else a human reason."""
    # $out / $merge write a collection from inside an otherwise read-looking
    # aggregate. Match them only in KEY position ({$out: ..} / {"$merge": ..}) and
    # on the raw text, so a quoted stage name still trips it but the same token
    # appearing in a filter VALUE (e.g. {subject:"a $out b"}) does not.
    if re.search(r"""["']?\$(out|merge)["']?\s*:""", js):
        return "aggregate stage $out/$merge (writes a collection)"
    s = _strip_js(js)
    for name in _MONGO_METHOD_RE.findall(s):
        low = name.lower()
        if low in MONGO_WRITE_METHODS:
            return "write/admin method .%s()" % name
        if low not in MONGO_READ_METHODS:
            return "method .%s() is not on the read-only allowlist" % name
    return None


def _resolve_mongo_db(database):
    if not database:
        return None
    d = database.strip()
    return MONGO_ALIAS.get(d.lower(), d)


def _mongo_db_ok(d):
    return bool(d) and all(c.isalnum() or c in "_-" for c in d)


def _run_mongo(script, database=None, fmt="shell", timeout=75):
    return run_driver({"op": "mongo", "script": script, "database": database,
                       "format": fmt, "container": MONGO_CFG.get("container") or "",
                       "auth_db": MONGO_CFG.get("auth_db") or "admin"}, timeout=timeout)


def _mongo_body(res):
    """Whatever mongosh actually said (stdout + stderr), or "" if it said nothing."""
    out = res.get("output", "") or ""
    if res.get("error_out"):
        out += ("\n" if out else "") + "[mongosh] " + res["error_out"]
    if res.get("truncated"):
        out += "\n... (output truncated — add .limit()/a projection, or narrow the query)"
    return out


def _mongo_out(res):
    return _mongo_body(res) or "(no output — mongosh prints the last expression's value; use print() if you see nothing)"


def _mongo_err(res):
    """Failure text: mongosh's own message if it produced one, else the driver's
    (e.g. 'mongo container not found' on a node with no mongo deployed)."""
    return _mongo_body(res) or res.get("error") or "unknown error"


def tool_mongo_query(args):
    database = _resolve_mongo_db(args.get("database"))
    if database and not _mongo_db_ok(database):
        return ("invalid database name (letters/digits/underscore/hyphen only)", True)
    script = args.get("script", "")
    if not script.strip():
        return ("script is empty", True)
    reason = classify_mongo(script)
    gate_reason = reason or "arbitrary mongosh JavaScript cannot be statically proven read-only"
    if args.get("confirm") is not True:
        log_event({"ev": "mongo_blocked", "database": database,
                   "reason": gate_reason, "script": script})
        return ("BLOCKED: %s against the LIVE game Mongo DB '%s'. Arbitrary mongosh scripts require "
                "confirm=true; use srcds_mongo_schema for unconfirmed structured inspection. Nothing was executed."
                % (gate_reason, database or "?"), True)
    res = _run_mongo(script, database, args.get("format", "shell"))
    log_event({"ev": "mongo_query", "database": database, "write": bool(reason),
               "confirm": bool(args.get("confirm")), "script": script[:200], "ok": res.get("ok")})
    if not res.get("ok"):
        return ("mongo query failed: %s" % _mongo_err(res), True)
    return ("[mongo:%s]\n%s" % (database or "(no db selected)", _mongo_out(res)), False)


_MONGO_LIST_DBS = (
    "var d=db.adminCommand({listDatabases:1}).databases||[];"
    "print('database\\tsize_mb\\tempty');"
    "print(d.map(function(x){return x.name+'\\t'+Math.round((Number(x.sizeOnDisk)||0)/104857.6)/10+'\\t'+(x.empty?'yes':'')}).join('\\n'))"
)

_MONGO_LIST_COLLS = (
    "var rows=db.getCollectionNames().sort().map(function(c){"
    "var s={};try{s=db.runCommand({collStats:c})}catch(e){}"
    "return [c,db.getCollection(c).countDocuments(),Math.round((s.size||0)/1024),Math.round((s.totalIndexSize||0)/1024)].join('\\t')});"
    "print('collection\\tdocs\\tdata_kb\\tidx_kb');print(rows.join('\\n'))"
)


def _mongo_describe_js(coll, sample):
    return (
        "var C=%s,N=%d;var col=db.getCollection(C),docs=col.find().limit(N).toArray();"
        "function ty(v){if(v===null)return'null';if(v===undefined)return'undefined';"
        "if(Array.isArray(v))return'array';if(v instanceof Date)return'date';"
        "var c=v&&v.constructor&&v.constructor.name;"
        "if(c&&['ObjectId','Long','Int32','Double','Decimal128','Binary','Timestamp','UUID','Code','MinKey','MaxKey'].indexOf(c)>=0)return c;"
        "if(typeof v==='object')return'object';return typeof v}"
        "var f={};docs.forEach(function(d){Object.keys(d).forEach(function(k){"
        "f[k]=f[k]||{n:0,t:{}};f[k].n++;var t=ty(d[k]);f[k].t[t]=(f[k].t[t]||0)+1})});"
        "print('-- fields (inferred from '+docs.length+' sampled docs of '+col.countDocuments()+') --');"
        "print('field\\ttypes\\tpresent');"
        "print(Object.keys(f).map(function(k){return k+'\\t'+Object.keys(f[k].t).map(function(t){return t+':'+f[k].t[t]}).join(',')+'\\t'+f[k].n+'/'+docs.length}).join('\\n'));"
        "print('-- indexes --');"
        "print(col.getIndexes().map(function(i){return i.name+'\\t'+EJSON.stringify(i.key)+(i.unique?'\\tUNIQUE':'')}).join('\\n'));"
        "print('-- sample doc --');"
        "print(docs.length?EJSON.stringify(docs[0],null,1):'(empty collection)')"
        % (json.dumps(coll), sample)
    )


def tool_mongo_schema(args):
    database = _resolve_mongo_db(args.get("database"))
    coll = (args.get("collection") or "").strip()
    if database and not _mongo_db_ok(database):
        return ("invalid database name", True)
    if not database:
        script, what = _MONGO_LIST_DBS, "databases"
    elif not coll:
        script, what = _MONGO_LIST_COLLS, "collections in %s" % database
    else:
        sample = max(1, min(int(args.get("sample", 25)), 200))
        script, what = _mongo_describe_js(coll, sample), "%s.%s" % (database, coll)
    res = _run_mongo(script, database, "shell", timeout=60)
    log_event({"ev": "mongo_schema", "database": database, "collection": coll, "ok": res.get("ok")})
    if not res.get("ok"):
        return ("mongo schema failed: %s" % _mongo_err(res), True)
    return ("[mongo schema: %s]\n%s" % (what, _mongo_out(res)), False)


# ----------------------------------------------------------------------------
# Tool registry / JSON schemas
# ----------------------------------------------------------------------------
SERVER_ENUM = {"type": "string", "enum": list(SERVER_NAMES),
               "description": "Which server. One of: %s." % ", ".join(SERVER_NAMES)}

MCP_INSTRUCTIONS = (
    "Deploy protocol v2: every file needs expected_sha256 of the original remote bytes used as its edit base "
    "(full SHA-256 from fetch/download/diff sha256_a), or literal 'missing' for creation. Keep that base hash "
    "with the working copy. A fresh hash is NOT permission to upload an older working copy: reconcile the "
    "current remote changes into the candidate first. A stale hash rejects the entire batch. Restore also "
    "needs expected_sha256 and explicit backup_id from fetch what='history' or 'backups'. Versioned backups "
    "are mandatory. After a timeout or uncertain result, inspect history and hashes before retrying. "
    "Batch file operations: whenever more than one file must be compared, call srcds_diff once with "
    "files=[...]; never loop single-file diffs. For local comparisons each item is {path,local}; for "
    "server comparisons set server_b and use {path,path_b?}. Likewise, deploy more than one file in "
    "one srcds_deploy files=[...] call. For trees, use srcds_fetch what='hash' first, then batch-diff "
    "only the mismatches. Grep multiple regexes/globs/roots in one srcds_grep call using patterns/globs/paths. "
    "Arbitrary server Lua and mongosh scripts always require confirm=true. srcds_clientlua requires an explicit "
    "immutable target; target='all' additionally requires broadcast=true and force=true. A clientlua ACK proves "
    "client-reported synchronous execution only, never later timer/callback behavior or visual/player acceptance."
)

# Descriptions are built from the configured topology so they stay truthful for
# any deployment (and after servers are added) — never hardcode server names here.
_NAMES_TXT = ", ".join(SERVER_NAMES) or "(none configured — set servers[] in config.json)"
_ALIAS_TXT = ("" if not DB_ALIAS else
              " Configured aliases: " + ", ".join("%s=%s" % (k, v) for k, v in sorted(DB_ALIAS.items())) + ".")
_MONGO_ALIAS_TXT = ("" if not MONGO_ALIAS else
                    " Configured aliases: " + ", ".join("%s=%s" % (k, v) for k, v in sorted(MONGO_ALIAS.items())) + ".")
_MONGO_NOTE_TXT = (" " + MONGO_CFG["note"].strip()) if (MONGO_CFG.get("note") or "").strip() else ""

TOOLS = [
    {
        "name": "srcds_status",
        "description": "List the configured game servers (%s): up/down, live player count (via A2S), LIVE flag vs per-server thresholds, port, and whether console output capture (-condebug) is available. Hostnames and container identifiers are intentionally omitted. Read-only, always allowed." % _NAMES_TXT,
        "inputSchema": {
            "type": "object",
            "properties": {"server": {"type": "string", "enum": list(SERVER_NAMES),
                                       "description": "Optional: only show this one."}},
        },
    },
    {
        "name": "srcds_fetch",
        "description": "Read-only remote volume/console access. what='console': tail console.log; what='docker': tail the container log; what='file': read a file; what='dir': list a directory; what='hash': hash a tree, then send ALL differing files together in one srcds_diff files=[...] batch; what='backups': list deploy backups. save_to writes into the LOCAL filesystem and therefore requires confirm=true (plus overwrite=true for an existing file). Remote paths are realpath-confined beneath garrysmod/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "what": {"type": "string", "enum": ["console", "file", "dir", "hash", "backups", "docker"], "default": "console"},
                "lines": {"type": "integer", "default": 200, "description": "console/docker: tail this many lines (docker max 2000)."},
                "path": {"type": "string", "description": "For file/dir/hash: path relative to garrysmod/ (e.g. cfg/server.cfg, addons/x/lua)."},
                "glob": {"type": "string", "description": "For what='hash': filename glob filter (default *)."},
                "grep": {"type": "string", "description": "Optional substring filter (console/file)."},
                "maxbytes": {"type": "integer", "default": 48000, "description": "Byte cap on returned content (keeps the most-recent slice). ANSI color codes are always stripped."},
                "save_to": {"type": "string", "description": "For what='file': save the raw bytes to this LOCAL path instead of returning text (binary-safe, up to 8MB; content never enters the conversation)."},
                "overwrite": {"type": "boolean", "default": False, "description": "Allow save_to to replace an existing local file."},
                "confirm": {"type": "boolean", "default": False, "description": "Required when save_to is used because that writes to the local filesystem."},
            },
            "required": ["server"],
        },
    },
    {
        "name": "srcds_console",
        "description": "Inject one server console command via the pty/docker-attach path. A small explicit read-only allowlist (status/stats/version/uptime, cvarlist/find/help/maps, meta list/version) runs automatically; every other or multi-command input requires confirm=true. Output is ANSI-stripped and byte-capped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "command": {"type": "string", "description": "The console command, e.g. 'status' or 'ulx adduser ...'."},
                "grep": {"type": "string", "description": "Optional substring filter on captured output."},
                "maxbytes": {"type": "integer", "default": 24000, "description": "Byte cap on the returned console.log delta (keeps the most-recent slice)."},
                "confirm": {"type": "boolean", "default": False, "description": "Required for every command not on the explicit read-only allowlist."},
            },
            "required": ["server", "command"],
        },
    },
    {
        "name": "srcds_lua",
        "description": (
            "Run server-side Lua, including multi-line VERIFICATION SUITES. Your code runs VERBATIM "
            "(runtime/syntax errors report YOUR line numbers). Injected global helpers: "
            "SECTION(name), CHECK(cond,msg), EQ(a,b,msg), NEQ, NEAR(a,b,eps,msg), TRUE(v,msg), FALSE, "
            "OK(v,msg), THROWS(fn,msg)->err, DUMP(v)->str, LOG(...). A PASS/FAIL summary is returned and "
            "ANY failed check marks the call as an error. A top-level `return <expr>` (numbers/strings/"
            "booleans/tables) is captured and safely serialized (entities/vectors tagged; cycles & "
            "functions won't crash). Globals you set do NOT pollute _G; a runaway loop is auto-aborted. "
            "Output is captured on ALL servers via a volume file (works without -condebug). For timer/coroutine suites set "
            "async=true and call MCP_DONE() when finished. Arbitrary server Lua cannot be proven read-only and "
            "therefore ALWAYS requires confirm=true."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "code": {"type": "string", "description": "Server Lua / verification suite. e.g. 'return player.GetCount()' or a multi-line CHECK/EQ assertion suite. Use `return <expr>` or LOG(...) to get values back."},
                "confirm": {"type": "boolean", "default": False, "description": "Required for every srcds_lua call."},
                "async": {"type": "boolean", "default": False, "description": "True for suites using timers/coroutines/http; then call MCP_DONE() from the final callback."},
                "async_timeout": {"type": "integer", "default": 20, "description": "Seconds to wait for MCP_DONE() when async=true (max ~30)."},
            },
            "required": ["server", "code"],
        },
    },
    {
        "name": "srcds_deploy",
        "description": "Write files to a server's realpath-confined volume. SINGLE: to + local|content. BATCH: files=[{to,local|content}, ...] pushes many files in ONE call (use whenever deploying >1). Per-file cap 64MB; aggregate batch cap 256MB. Overwrites receive out-of-tree rollback backups by default. restore:true rolls back. Requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "to": {"type": "string", "description": "SINGLE mode: destination path relative to garrysmod/, e.g. 'addons/rals/lua/autorun/server/x.lua' or 'cfg/foo.cfg'."},
                "local": {"type": "string", "description": "SINGLE mode: a local file path to read and copy (preferred for real files)."},
                "content": {"type": "string", "description": "SINGLE mode: inline file content (use instead of 'local' for small/generated files)."},
                "files": {"type": "array", "description": "BATCH mode: array of {to, local|content} objects (same semantics as the top-level params; each 'to' unique, max 400 per call). With restore:true items need only 'to'. Mutually exclusive with top-level to/local/content.",
                          "items": {"type": "object",
                                    "properties": {"to": {"type": "string"}, "local": {"type": "string"}, "content": {"type": "string"}},
                                    "required": ["to"]}},
                "restore": {"type": "boolean", "default": False, "description": "Roll back 'to' (or every files[].to) to its last deploy backup instead of writing new content (local/content ignored; the backup is kept)."},
                "backup": {"type": "boolean", "default": True, "description": "Back up overwritten files to the out-of-tree backups root, not next to the file."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true to actually write."},
            },
            "required": ["server"],
        },
    },
    {
        "name": "srcds_grep",
        "description": "Recursively grep deployed source in one bounded host call. Use pattern or patterns[] (multiple regexes are OR alternatives), glob or globs[] (multiple include filters), exclude_globs[], and path or paths[]. Singular fields remain compatible. Read-only, always allowed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "pattern": {"type": "string", "description": "grep -e pattern (basic regex)."},
                "patterns": {"type": "array", "minItems": 1, "maxItems": 20,
                             "items": {"type": "string"}, "description": "Multiple grep -e basic regexes; matches ANY pattern."},
                "path": {"type": "string", "description": "Subdir under garrysmod/ to search (default whole volume), e.g. 'addons/rals'."},
                "paths": {"type": "array", "minItems": 1, "maxItems": 25,
                          "items": {"type": "string"}, "description": "Search several garrysmod-relative roots in the same call."},
                "glob": {"type": "string", "default": "*.lua", "description": "Filename include glob (default *.lua)."},
                "globs": {"type": "array", "minItems": 1, "maxItems": 50,
                          "items": {"type": "string"}, "description": "Multiple filename include globs (OR)."},
                "exclude_globs": {"type": "array", "maxItems": 50,
                                  "items": {"type": "string"}, "description": "Filename globs to exclude."},
                "max": {"type": "integer", "default": 200, "description": "Max matches to return."},
            },
            "required": ["server"],
            "anyOf": [{"required": ["pattern"]}, {"required": ["patterns"]}],
        },
    },
    {
        "name": "srcds_diff",
        "description": "Unified diff of deployed files against LOCAL files or another server. SINGLE: path + exactly one of local/server_b. BATCH: files=[...] compares many files in ONE call/SSH round-trip; never loop single diffs. Per-side cap 16MB and aggregate batch input cap 64MB. Server-to-server is read-only. Local comparisons transmit local contents to the remote comparison driver and require confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "path": {"type": "string", "description": "SINGLE mode side A: path relative to garrysmod/ on `server`."},
                "server_b": {"type": "string", "enum": list(SERVER_NAMES), "description": "Side B server for SINGLE or BATCH server-to-server mode. Same path unless path_b."},
                "path_b": {"type": "string", "description": "SINGLE mode: optional different path on server_b."},
                "local": {"type": "string", "description": "SINGLE mode side B: this LOCAL file path. Give this OR server_b."},
                "files": {"type": "array", "minItems": 1, "maxItems": 200, "description": "BATCH mode (use whenever comparing >1 file; max 200). Local mode: [{path,local}, ...]. Server mode: set top-level server_b and use [{path,path_b?}, ...]. Mutually exclusive with top-level path/path_b/local.",
                          "items": {"type": "object",
                                    "properties": {"path": {"type": "string"}, "path_b": {"type": "string"}, "local": {"type": "string"}},
                                    "required": ["path"]}},
                "context": {"type": "integer", "default": 3, "description": "Diff context lines for every comparison (0-100)."},
                "maxbytes": {"type": "integer", "default": 48000, "description": "BATCH mode aggregate unified-diff output budget in bytes (max 200000); every file is still accounted for in the status summary."},
                "confirm": {"type": "boolean", "default": False, "description": "Required for local-file comparisons because local contents cross the SSH boundary; not needed server-to-server."},
            },
            "required": ["server"],
        },
    },
    {
        "name": "srcds_nodeinfo",
        "description": "Host-node health (read-only, always allowed): loadavg, memory/swap, uptime, disk usage (/ + volumes root), per-container docker stats. Optional forensics tails: wings_log_lines (Pterodactyl wings log — crash-detection lines live here) and dmesg_lines (kernel log — OOM-killer traces).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wings_log_lines": {"type": "integer", "default": 0, "description": "Also tail this many lines of the wings log (max 200)."},
                "dmesg_lines": {"type": "integer", "default": 0, "description": "Also tail this many lines of dmesg -T (max 100)."},
            },
        },
    },
    {
        "name": "srcds_clientlua",
        "description": "Run up to 64KiB of CLIENTSIDE Lua on explicitly targeted, fully Steam-authenticated human clients. A short SendLua bootstrap installs a fixed receiver and returns a ready signal; only then do tokenized compressed net chunks carry the code. Every client reports transfer/compile/synchronous-runtime status. target is REQUIRED and accepts only SteamID/SteamID64 or 'all' (no nickname matching). target='all' additionally requires broadcast=true and force=true. confirm=true is always required. ACK covers the synchronous top-level chunk only; later timer/callback behavior and visual/player acceptance remain separate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "code": {"type": "string", "description": "Clientside Lua to run on the target players."},
                "target": {"type": "string", "description": "Required: 'all', exact SteamID, or exact 17-digit SteamID64. Nicknames are intentionally rejected."},
                "broadcast": {"type": "boolean", "default": False, "description": "Required true when target='all'."},
                "force": {"type": "boolean", "default": False, "description": "Required true when target='all', even if A2S reports quiet/unknown."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true (executes code on clients)."},
            },
            "required": ["server", "code", "target"],
        },
    },
    {
        "name": "srcds_power",
        "description": "Power-control through the Pterodactyl wings API. Requires confirm=true. stop/restart/kill additionally require force=true whenever any players are connected OR A2S population is unknown (fail closed). start/restart arm a boot watcher; action='watch' polls it read-only. Falls back to raw docker only if wings is unreachable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "action": {"type": "string", "enum": ["start", "stop", "restart", "kill", "watch"],
                           "description": "'watch' = check/await boot completion after a start/restart (read-only)."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true to perform start/stop/restart/kill (not needed for watch)."},
                "force": {"type": "boolean", "default": False, "description": "Required to stop/restart/kill when players are connected or population is unknown."},
                "watch": {"type": "boolean", "default": True, "description": "Arm the boot watcher after start/restart."},
                "wait": {"type": "integer", "default": 0, "description": "action='watch': long-poll up to this many seconds (max 55) for BOOT COMPLETE before returning."},
            },
            "required": ["server", "action"],
        },
    },
    {
        "name": "srcds_monitor",
        "description": ("Arm a background watcher on the node and poll it — the easy way to be told about a "
                        "console event or an up/down transition without tailing logs in a loop. Read-only, always "
                        "allowed. ARM (one call): pattern:'<python regex>' (e.g. '(?i)lua error') follows the live "
                        "container console on ANY server (no -condebug needed); or watch:'down'/'up' fires on the "
                        "wings state transition — 'down' also captures the last 40 console lines at death (crash "
                        "forensics). Returns an id. CHECK: id + wait<=55 long-polls and returns early on a hit; "
                        "pass after:<seen count> to await only NEW matches. action:'stop'+id disarms; no args "
                        "lists this server's monitors. Watchers auto-expire after timeout_min. The action field "
                        "can usually be omitted — it is inferred (pattern/watch=arm, id=check, neither=list)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "action": {"type": "string", "enum": ["arm", "check", "stop", "list"],
                           "description": "Optional — inferred from the other args if omitted."},
                "watch": {"type": "string", "enum": ["pattern", "down", "up"],
                          "description": "arm: 'pattern'=console regex (default when pattern given); 'down'/'up'=wings state transition."},
                "pattern": {"type": "string", "description": "arm: python regex searched against each ANSI-stripped console line."},
                "timeout_min": {"type": "integer", "default": 30, "description": "arm: watcher auto-expires after this many minutes (1-240)."},
                "id": {"type": "string", "description": "check/stop: the monitor id returned by arm."},
                "wait": {"type": "integer", "default": 0, "description": "check: long-poll up to this many seconds (<=55), returning early on a hit."},
                "after": {"type": "integer", "default": 0, "description": "check (pattern): only return early when match_count EXCEEDS this — pass the count you already saw."},
            },
            "required": ["server"],
        },
    },
    {
        "name": "srcds_db_query",
        "description": ("Run SQL against the game MariaDB (credentials stay inside the container). Classified "
                        "SELECT/SHOW/DESCRIBE/EXPLAIN reads run inside a READ ONLY transaction; writes, WITH, "
                        "and MariaDB/MySQL executable comments require confirm=true. `database` accepts a raw schema "
                        "name OR any alias defined in db_aliases in config.json.%s Output is TSV by default "
                        "(token-lean; tabs/newlines in values are escaped); format='table' for a bordered "
                        "human-readable table. Capped ~40KB — add LIMIT for big tables." % _ALIAS_TXT),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Raw schema name, or an alias from db_aliases in config.json. Omit for a server-agnostic query (e.g. information_schema)."},
                "sql": {"type": "string", "description": "The SQL. e.g. \"SELECT * FROM inventory_items WHERE owner='STEAM_0:..' LIMIT 20\"."},
                "format": {"type": "string", "enum": ["tsv", "table"], "default": "tsv",
                           "description": "tsv (default, token-lean) or table (bordered, for humans)."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true for any write/DDL statement."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "srcds_db_schema",
        "description": "Browse the MariaDB schema (read-only, always allowed): no args → list databases; database only → its tables with approx row counts + sizes; database+table → DESCRIBE columns + indexes. `database` accepts the same aliases as srcds_db_query. TSV output by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Schema name or server alias. Omit to list all databases."},
                "table": {"type": "string", "description": "Table name to describe (columns + indexes)."},
                "format": {"type": "string", "enum": ["tsv", "table"], "default": "tsv"},
            },
        },
    },
    {
        "name": "srcds_mongo_query",
        "description": ("Run a mongosh script against the game MongoDB (credentials stay inside the container).%s "
                        "Because arbitrary JavaScript cannot be proven read-only, every srcds_mongo_query call "
                        "requires confirm=true; use srcds_mongo_schema for unconfirmed structured inspection. "
                        "The script is evaluated like a mongosh "
                        "REPL line, so the last expression's value is printed: `db.mail.find({to:'765..'})"
                        ".limit(5)` works as-is; use print()/EJSON.stringify() for custom output. `database` "
                        "accepts a raw db name OR an alias from mongo_aliases in config.json.%s Output capped "
                        "~40KB — always .limit() big collections." % (_MONGO_NOTE_TXT, _MONGO_ALIAS_TXT)),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Mongo database name, or an alias from mongo_aliases. Omit only for admin-level scripts that pick their own db."},
                "script": {"type": "string", "description": "mongosh JavaScript. e.g. \"db.delivery_orders.find({status:7}).sort({created_at:-1}).limit(10).toArray()\"."},
                "format": {"type": "string", "enum": ["shell", "json"], "default": "shell",
                           "description": "shell (default, mongosh's compact human/BSON-typed rendering) or json (--json=relaxed, strict Extended JSON)."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true for every arbitrary mongosh script."},
            },
            "required": ["script"],
        },
    },
    {
        "name": "srcds_mongo_schema",
        "description": ("Browse the MongoDB schema (read-only, always allowed): no args → list databases with "
                        "sizes; database only → its collections with doc counts + data/index KB; "
                        "database+collection → field names with inferred BSON types and presence counts "
                        "(sampled), indexes, and one sample document. `database` accepts the same aliases as "
                        "srcds_mongo_query.%s" % _MONGO_ALIAS_TXT),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Mongo database name or alias. Omit to list all databases."},
                "collection": {"type": "string", "description": "Collection to describe (fields + indexes + sample doc)."},
                "sample": {"type": "integer", "default": 25, "description": "How many docs to sample for field inference (1-200). Mongo is schemaless — a bigger sample finds rarer fields."},
            },
        },
    },
]


_EXPECTED_SCHEMA = {"type": "string", "pattern": "^(?:[0-9a-f]{64}|missing)$",
                    "description": "SHA-256 of the original remote bytes used as the edit base. Use 'missing' only for a new path. Never attach a fresh hash to stale content."}
_BACKUP_ID_SCHEMA = {"type": "string", "pattern": "^(?:[0-9]{20}-[0-9a-f]{16}|legacy)$",
                     "description": "Restore source version from fetch history/backups; required for restore."}
for _schema_tool in TOOLS:
    _props = _schema_tool["inputSchema"]["properties"]
    if _schema_tool["name"] == "srcds_deploy":
        _schema_tool["description"] = ("Guarded file deployment. SINGLE: to + expected_sha256 + exactly one of local/content. "
            "BATCH: files=[{to,expected_sha256,local|content},...] in one call. Fresh target resolution, per-volume lock, "
            "whole-batch stale-base preflight, immutable backups and durable per-file history. Restore requires "
            "expected_sha256 and backup_id and preserves the displaced file. Requires confirm=true. "
            "64MiB/file, 256MiB/batch. Runtime/client acceptance is separate from byte verification.")
        _props["expected_sha256"] = dict(_EXPECTED_SCHEMA)
        _props["backup_id"] = dict(_BACKUP_ID_SCHEMA)
        _props["source_revision"] = {"type": "string", "pattern": "^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
                                     "description": "Optional full Git commit hash for audit provenance."}
        _props["files"]["description"] = "Batch entries; expected_sha256 required per file. Restore entries also need backup_id. Mutually exclusive with single-file fields."
        _props["files"].update(minItems=1, maxItems=400)
        _props["files"]["items"]["properties"].update(expected_sha256=dict(_EXPECTED_SCHEMA), backup_id=dict(_BACKUP_ID_SCHEMA))
        _props["files"]["items"]["required"] = ["to", "expected_sha256"]
        _props["restore"]["description"] = "Restore an explicit backup_id; requires current-file expected_sha256 and no local/content. The displaced current version is backed up."
        _props["backup"].update(const=True, description="Versioned backups are mandatory. False is rejected.")
        _schema_tool["inputSchema"]["oneOf"] = [
            {"required": ["files"], "not": {"anyOf": [{"required": [k]} for k in ("to", "local", "content", "expected_sha256", "backup_id")]}},
            {"required": ["to", "expected_sha256"], "not": {"required": ["files"]}}]
    elif _schema_tool["name"] == "srcds_fetch":
        _props["what"]["enum"].append("history")
        _props["before"] = {"type": "string", "description": "For history: opaque 'before' cursor returned by the previous page."}
        _props["path"]["description"] += " For history/backups: optional relative path prefix filter."
        _schema_tool["description"] += " Hashes/downloads include full SHA-256. what='history' returns durable per-file deployment metadata (lines=page size, max 100; before=cursor). Backups list explicit backup_id values."

DISPATCH = {
    "srcds_status": tool_status,
    "srcds_fetch": tool_fetch,
    "srcds_console": tool_console,
    "srcds_lua": tool_lua,
    "srcds_deploy": tool_deploy,
    "srcds_grep": tool_grep,
    "srcds_diff": tool_diff,
    "srcds_nodeinfo": tool_nodeinfo,
    "srcds_clientlua": tool_clientlua,
    "srcds_power": tool_power,
    "srcds_monitor": tool_monitor,
    "srcds_db_query": tool_db_query,
    "srcds_db_schema": tool_db_schema,
    "srcds_mongo_query": tool_mongo_query,
    "srcds_mongo_schema": tool_mongo_schema,
}


# ----------------------------------------------------------------------------
# JSON-RPC / MCP stdio loop
# ----------------------------------------------------------------------------
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in MCP_SUPPORTED_PROTOCOLS else MCP_PROTOCOL_VERSION
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "srcds-mcp", "version": MCP_VERSION},
            "instructions": MCP_INSTRUCTIONS,
        }})
        return
    if method == "notifications/initialized" or method == "initialized":
        return  # notification, no reply
    if method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if not fn:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool: %s" % name}})
            return
        # Central config gate: EVERY tool needs SSH, so an unconfigured install
        # always answers with the actionable "config: ..." message (the README
        # promises this) instead of a generic "host unreachable".
        ce = config_error()
        if ce:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "config: " + ce}],
                "isError": True,
            }})
            return
        try:
            text, is_error = fn(args)
        except Exception as e:
            log_event({"ev": "tool_exc", "tool": name, "err": str(e), "tb": traceback.format_exc()[-800:]})
            text, is_error = ("internal error in %s: %s" % (name, e), True)
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        }})
        return

    # Unknown request -> error; unknown notification -> ignore
    if mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log_event({"ev": "boot", "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            handle(msg)
        except Exception as e:
            log_event({"ev": "loop_exc", "err": str(e), "tb": traceback.format_exc()[-800:]})


if __name__ == "__main__":
    main()
