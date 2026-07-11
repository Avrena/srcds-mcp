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
srcds_db_query, srcds_db_schema. Server names come from config.json (`servers[]`).

Safety: reads are always allowed; writes that look destructive/mutating require
confirm=true. Every call is logged to srcds_mcp.log next to this file.
"""

import sys, os, json, base64, subprocess, socket, struct, re, time, traceback, hashlib

_HERE = os.path.dirname(os.path.abspath(__file__))

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
    "live_thresholds": {"scprp": 50, "drp": 20, "zcity": 3},
    # Server topology: a marker dir under garrysmod/ -> logical name. First match wins.
    # Point these at whatever uniquely identifies each of YOUR gamemodes/servers.
    "servers": [
        {"logical": "scprp", "marker": "addons/example-scp-addon"},
        {"logical": "zcity", "marker": "addons/example-city-addon"},
        {"logical": "drp",   "marker": "gamemodes/darkrp"},
    ],
    # DB tool convenience aliases: game name -> its MariaDB schema (optional; raw
    # schema names always work too). e.g. {"scprp": "my_scprp_schema"}
    "db_aliases": {},
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
import os, sys, json, base64, subprocess, pty, time, select, re

VOLROOT = "@VOLROOT@"
BAKROOT = "@BAKROOT@"                                 # deploy backups, OUT of every game tree
WINGS_API = "@WINGS_API@"
WINGS_CONFIG = "@WINGS_CONFIG@"
OWNER_UID = @OWNER_UID@                               # pterodactyl:pterodactyl on the node
OWNER_GID = @OWNER_GID@
SERVERS = @SERVERS_JSON@                              # [{"logical","marker"}], first marker match wins
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
            f.seek(before)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    if len(data) > maxbytes:
        data = data[-maxbytes:]
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
    maxb = int(req.get("maxbytes", 24000))
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
            return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
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
    what = req.get("what", "console"); lines = int(req.get("lines", 200))

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
        def sha1_of(fp):
            h = hashlib.sha1()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()[:12]
        files = {}
        if os.path.isfile(base):
            try:
                files[os.path.basename(base)] = [sha1_of(base), os.path.getsize(base)]
            except OSError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "files": files, "count": 1, "truncated": False}
        if not os.path.isdir(base):
            return {"ok": False, "error": "no such path: %s" % req.get("path", "")}
        n = 0; capped = False
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
                try:
                    files[rel] = [sha1_of(fp), os.path.getsize(fp)]
                except OSError:
                    files[rel] = [None, None]
            if capped:
                break
        return {"ok": True, "files": files, "count": len(files), "truncated": capped}

    if what == "backups":
        broot = BAKROOT + "/" + u
        out = []
        capped = False
        for root, dirs, fnames in os.walk(broot):
            dirs.sort()
            for fn in sorted(fnames):
                fp = os.path.join(root, fn)
                rel = fp[len(broot):].lstrip("/")
                try:
                    st = os.stat(fp)
                    out.append({"path": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
                except OSError:
                    pass
                if len(out) >= 500:
                    capped = True
                    break
            if capped:
                break
        return {"ok": True, "backups": out, "truncated": capped}

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
        maxb = int(req.get("maxbytes", 48000))
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
        p = os.path.normpath(gm + "/" + rel.lstrip("/"))
        if not p.startswith(os.path.normpath(gm)):
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
            cap = int(req.get("b64_max", 8000000))
            if size > cap:
                return {"ok": False, "error": "file is %d bytes (> %d download cap)" % (size, cap)}
            with open(p, "rb") as f:
                data = f.read()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": p, "size": size,
                "content_b64": base64.b64encode(data).decode()}
    try:
        with open(p, "rb") as f:
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
    maxb = int(req.get("maxbytes", 48000))
    orig = len(tail)
    truncated = orig > maxb
    if truncated:
        tail = tail[orig - maxb:]
        nl = tail.find("\n")            # drop the leading partial line for cleanliness
        if 0 <= nl < 240:
            tail = tail[nl + 1:]
        tail = "...[truncated: last %d of %d chars]...\n%s" % (len(tail), orig, tail)
    return {"ok": True, "path": p, "content": tail, "size": size,
            "truncated": truncated, "bytes": len(tail)}

def _safe_under(gm, rel):
    p = os.path.normpath(gm + "/" + rel.lstrip("/"))
    root = os.path.normpath(gm)
    if p == root or p.startswith(root + os.sep):
        return p
    return None

def op_deploy(req):
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"
    p = _safe_under(gm, req["to"])
    if not p:
        return {"ok": False, "error": "path escapes volume"}
    if req.get("restore"):
        # Roll back to the last deploy backup. Deliberately does NOT re-backup the
        # current (bad) file first — that would overwrite the good backup and make
        # a second restore impossible. The backup is kept as-is.
        bak = BAKROOT + "/" + u + "/" + req["to"].lstrip("/")
        if not os.path.isfile(bak):
            return {"ok": False, "error": "no deploy backup recorded for %s" % req["to"]}
        try:
            with open(bak, "rb") as f:
                data = f.read()
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass
            try:
                os.chown(p, OWNER_UID, OWNER_GID)
            except (OSError, AttributeError):
                pass
        except OSError as e:
            return {"ok": False, "error": "restore failed: %s" % e}
        return {"ok": True, "restored": True, "path": p, "bytes": len(data), "backup": bak}
    try:
        data = base64.b64decode(req["content_b64"])
    except Exception as e:
        return {"ok": False, "error": "bad content: %s" % e}
    existed = os.path.isfile(p)
    bak = None
    if existed and req.get("backup", True):
        # mirror the path under a dedicated backups root so we NEVER drop .mcpbak files
        # into addon/source/git trees. One latest backup per (server, path), overwritten.
        bak = BAKROOT + "/" + u + "/" + req["to"].lstrip("/")
        try:
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            with open(p, "rb") as f:
                old = f.read()
            with open(bak, "wb") as f:
                f.write(old)
        except OSError as e:
            return {"ok": False, "error": "backup failed: %s" % e}
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        try:
            os.chmod(p, 0o644)
        except OSError:
            pass
        try:
            os.chown(p, OWNER_UID, OWNER_GID)   # pterodactyl:pterodactyl
        except (OSError, AttributeError):
            pass
    except OSError as e:
        return {"ok": False, "error": "write failed: %s" % e}
    return {"ok": True, "path": p, "bytes": len(data), "overwrote": existed, "backup": bak}

def op_grep(req):
    u = req["uuid"]; gm = VOLROOT + "/" + u + "/garrysmod"
    base = _safe_under(gm, req.get("path", ""))
    if not base:
        return {"ok": False, "error": "path escapes volume"}
    if not os.path.exists(base):
        return {"ok": False, "error": "no such path: %s" % base}
    glob = req.get("glob") or "*.lua"
    mx = int(req.get("max", 200))
    cmd = ["grep", "-rnI", "--include", glob, "-e", req["pattern"], base]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=30).stdout.decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "grep failed: %s" % e}
    lines = out.splitlines()
    total = len(lines)
    pref = os.path.normpath(gm) + "/"
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
    r = {"ok": True, "matches": shown, "total": total, "shown": len(shown)}
    if capped:
        r["note"] = "byte-capped; narrow with path/glob or a stricter pattern"
    return r

def op_diff(req):
    import difflib, hashlib
    def readside(u, rel):
        gm = VOLROOT + "/" + u + "/garrysmod"
        p = _safe_under(gm, rel)
        if not p:
            return None, "path escapes volume"
        if not os.path.isfile(p):
            return None, "no such file: %s" % rel
        try:
            with open(p, "rb") as f:
                return f.read(), None
        except OSError as e:
            return None, str(e)
    a, err = readside(req["uuid_a"], req["path_a"])
    if err:
        return {"ok": False, "error": "A(%s): %s" % (req.get("label_a", "a"), err)}
    if req.get("content_b64") is not None:
        try:
            b = base64.b64decode(req["content_b64"])
        except Exception as e:
            return {"ok": False, "error": "bad local content: %s" % e}
    else:
        b, err = readside(req["uuid_b"], req["path_b"])
        if err:
            return {"ok": False, "error": "B(%s): %s" % (req.get("label_b", "b"), err)}
    meta = {"ok": True, "equal": a == b, "size_a": len(a), "size_b": len(b),
            "sha_a": hashlib.sha1(a).hexdigest()[:12], "sha_b": hashlib.sha1(b).hexdigest()[:12]}
    if meta["equal"]:
        return meta
    if b"\x00" in a[:8192] or b"\x00" in b[:8192]:
        meta["binary"] = True
        return meta
    d = "".join(difflib.unified_diff(
        a.decode("utf-8", "replace").splitlines(True),
        b.decode("utf-8", "replace").splitlines(True),
        fromfile=req.get("label_a", "a"), tofile=req.get("label_b", "b"),
        n=int(req.get("context", 3))))
    if len(d) > 40000:
        meta["truncated"] = True
        d = d[:40000] + "\n...[diff truncated at 40KB]"
    meta["diff"] = d
    return meta

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
    full = (("USE `%s`;\n" % db) if db else "") + sql
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
        elif op == "db":
            jout(op_db(req))
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
            .replace("@VOLROOT@", CFG["volroot"])
            .replace("@BAKROOT@", CFG["backups_root"])
            .replace("@WINGS_API@", CFG["wings"]["api"])
            .replace("@WINGS_CONFIG@", CFG["wings"]["config"])
            .replace("@OWNER_UID@", str(int(CFG["owner_uid"])))
            .replace("@OWNER_GID@", str(int(CFG["owner_gid"])))
            .replace("@SERVERS_JSON@", json.dumps(CFG["servers"])))


HOST_DRIVER = _render_driver(HOST_DRIVER)

# Version-namespace the host driver by a content hash so that DIFFERENT versions of
# this MCP server (e.g. a stale Claude Code instance + a freshly-edited one) NEVER
# clobber each other's /tmp driver — the cause of intermittent `driver exception:
# 'body'` (a v1 tool sending to a v2 driver, or vice versa, over a shared file).
_DRIVER_HASH = hashlib.sha1(HOST_DRIVER.encode("utf-8")).hexdigest()[:12]
_DRIVER_REMOTE = "/tmp/srcds_host_driver_%s.py" % _DRIVER_HASH
_driver_ready = False


def log_event(rec):
    try:
        rec["t"] = time.time()
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


def discover(force=False):
    if (not force) and _disc_cache["data"] is not None and (time.time() - _disc_cache["t"] < 20):
        return _disc_cache["data"]
    d = run_driver({"op": "discover"}, timeout=40)
    servers = d.get("servers") if isinstance(d, dict) else None
    if servers is not None:
        _disc_cache["t"] = time.time()
        _disc_cache["data"] = servers
        return servers
    # keep stale on failure
    return _disc_cache["data"] if _disc_cache["data"] is not None else []


def resolve(server):
    """logical name -> server dict (or None)."""
    for s in discover():
        if s.get("logical") == server:
            return s
    return None


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
    """Return (players, maxplayers, is_live) for a resolved server dict."""
    if not srv or not srv.get("running") or not srv.get("port"):
        return (None, None, False)
    a = a2s_info(PUBLIC_IP, srv["port"])
    if not a:
        return (None, None, False)
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


def _strip_lua(code):
    """Remove comments and string literals so the classifier sees only executable tokens."""
    s = re.sub(r"--\[(=*)\[.*?\]\1\]", " ", code, flags=re.S)   # block comments
    s = re.sub(r"--[^\n]*", " ", s)                              # line comments
    s = re.sub(r"\[(=*)\[.*?\]\1\]", " ", s, flags=re.S)         # long-bracket strings
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)                    # double-quoted strings
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)                    # single-quoted strings
    return s


def classify_console(cmd):
    m = DESTRUCTIVE_CMD.search(cmd)
    if m:
        return ("destructive console verb '%s'" % m.group(1))
    if cmd.strip().lower().startswith("lua_run"):
        m = LUA_MUTATE.search(_strip_lua(cmd))
        if m:
            return ("mutating lua in lua_run (%s)" % m.group(0))
    return None


def classify_lua(code):
    """Return (reason, band) where band in {None, 'mutate', 'opaque'}."""
    s = _strip_lua(code)
    m = LUA_MUTATE.search(s)
    if m:
        return ("mutating call '%s'" % m.group(0), "mutate")
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
            lines.append("  %-6s  DOWN    uuid=%s  (%s)" % (tag, s["uuid"][:8], s.get("hostname") or "?"))
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
        if s.get("hostname"):
            lines.append("          %s" % s["hostname"])
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

local _outn = 0
local function _LOGLINE(s)
  s = tostring(s)
  if #s > 1400 then s = s:sub(1, 1400) .. "...<+>" end
  _outn = _outn + 1
  if _outn <= 400 then _emit("OUT", s)
  elseif _outn == 401 then _emit("NOTE", "output capped at 400 lines") end
end

local _P, _F = 0, 0
local _section = ""
local _HNAMES = { "SECTION","CHECK","EQ","NEQ","NEAR","TRUE","FALSE","OK","THROWS","DUMP","LOG","MCP_DONE" }
local _saved = {}
for _, k in ipairs(_HNAMES) do _saved[k] = rawget(_G, k) end

local function _tag(m) if _section ~= "" then return "[" .. _section .. "] " .. tostring(m or "?") end return tostring(m or "?") end
local function _record(pass, failtext)
  if pass then _P = _P + 1
  else
    _F = _F + 1
    local ft = (tostring(failtext):gsub("[\r\n]+", " / "))
    if #ft > 1400 then ft = ft:sub(1, 1400) .. "...<+>" end
    _emit("FAIL", ft)
  end
  return pass
end

SECTION = function(name) _section = tostring(name or "") end
CHECK   = function(c, m) return _record(c and true or false, _tag(m)) end
EQ      = function(a, b, m) return _record(a == b, _tag(m) .. "  got=" .. _ser(a) .. " want=" .. _ser(b)) end
NEQ     = function(a, b, m) return _record(a ~= b, _tag(m) .. "  both=" .. _ser(a)) end
NEAR    = function(a, b, eps, m)
  eps = eps or 1e-6
  local ok = (type(a) == "number" and type(b) == "number" and math.abs(a - b) <= eps)
  return _record(ok, _tag(m) .. "  got=" .. _ser(a) .. " want~=" .. _ser(b) .. " eps=" .. _ser(eps))
end
TRUE    = function(v, m) return _record(v == true,  _tag(m) .. "  got=" .. _ser(v)) end
FALSE   = function(v, m) return _record(v == false, _tag(m) .. "  got=" .. _ser(v)) end
OK      = function(v, m) return _record(v ~= nil and v ~= false, _tag(m) .. "  got=" .. _ser(v)) end
THROWS  = function(fn, m) local ok, e = pcall(fn); _record(not ok, _tag(m) .. "  did not throw"); return e end
DUMP    = function(v) return _ser(v) end
LOG     = function(...)
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
  for _, k in ipairs(_HNAMES) do _G[k] = _saved[k] end
end
MCP_DONE = function() _finalize("DON") end

-- sandbox env: body global READS fall through to _G; body global WRITES go to a
-- scratch table => zero _G pollution from the body's own globals (no cleanup needed).
local _scratch = {}
-- capture body print/Msg/MsgN as framed OUT lines (clean separation from other
-- players' live console spam, which the driver drops as unframed noise).
_scratch.print = function(...) LOG(...) end
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

local _ins = 0
debug.sethook(function()
  _ins = _ins + 1
  if _ins > 200 then debug.sethook(); error("[mcp] instruction budget exceeded (runaway loop?)", 2) end
end, "", 100000)
local _ok, _cnt, _vals = _pack(xpcall(_chunk, function(e)
  return tostring(e) .. "\n" .. debug.traceback("", 2)
end))
debug.sethook()

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

if _ASYNC and not _finalized then
  -- async: wait for the body's MCP_DONE() callback (driver waits async_timeout for DON)
else
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
    if reason:
        if band == "opaque":
            reason += " (cannot statically prove read-only)"
        blocked = _confirm_gate(server, srv, reason, args)
        if blocked:
            log_event({"ev": "lua_blocked", "server": server, "reason": reason, "band": band, "code": code[:200]})
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
        for fl in fails:
            parts.append("  [FAIL] " + fl)
    if err is not None:
        parts.append("--- ERROR ---\n" + err)
    if out:
        parts.append("--- output ---\n" + out)
    if ret is not None:
        parts.append("--- return ---\n" + ret)
    if not any([summ, err, out, ret]):
        parts.append("(no output / suite produced nothing)")

    is_error = (err is not None) or (nf > 0) or bool(started and not ended)
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

    if what in ("dir", "hash", "backups"):
        req = {"op": "fetch", "uuid": srv["uuid"], "what": what, "path": args.get("path", "")}
        if what == "hash" and args.get("glob"):
            req["glob"] = args["glob"]
        res = run_driver(req, timeout=90)
        log_event({"ev": "fetch", "server": server, "what": what, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("fetch failed: %s" % res.get("error"), True)
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
            out = ["[%s] sha1 garrysmod/%s (glob=%s) — %d file(s)%s" % (
                server.upper(), args.get("path", ""), args.get("glob") or "*",
                res.get("count", len(files)),
                "  [capped at 2000 — narrow path/glob]" if res.get("truncated") else "")]
            for rel in sorted(files):
                h, sz = files[rel]
                out.append("  %s %9s  %s" % (h or "?" * 12, sz, rel))
            return ("\n".join(out), False)
        baks = res.get("backups", [])
        out = ["[%s] deploy backups (latest per path; roll back via srcds_deploy restore:true) — %d%s" % (
            server.upper(), len(baks), " (capped at 500)" if res.get("truncated") else "")]
        for b in baks:
            out.append("  %-60s %9s  %s" % (b["path"], b["size"], _fmt_mtime(b["mtime"])))
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
            data = base64.b64decode(res.get("content_b64") or "")
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
        return ("[%s] downloaded garrysmod/%s -> %s (%d bytes, sha1=%s, binary-safe)"
                % (server.upper(), args.get("path", ""), save_to, len(data),
                   hashlib.sha1(data).hexdigest()[:12]), False)
    hdr = "[%s] %s (%s)" % (server.upper(), what, res.get("path", ""))
    if res.get("truncated"):
        hdr += "  [byte-capped -> showing most recent; raise maxbytes or narrow via grep/lines for more]"
    return ("%s\n%s" % (hdr, res.get("content", "")), False)


PANEL_URL = CFG.get("panel_url") or ""


def tool_deploy(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    to = (args.get("to") or "").strip()
    if not to or to.startswith("/") or ":" in to or ".." in to.replace("\\", "/").split("/"):
        return ("invalid 'to': give a path relative to garrysmod/ with no '..' or drive/absolute prefix.", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if args.get("restore") is True:
        if args.get("confirm") is not True:
            return ("BLOCKED: restore the last deploy backup over %s:garrysmod/%s. "
                    "Re-call with confirm=true. Nothing was written." % (server, to), True)
        res = run_driver({"op": "deploy", "uuid": srv["uuid"], "to": to, "restore": True}, timeout=45)
        log_event({"ev": "deploy_restore", "server": server, "to": to, "ok": res.get("ok")})
        if not res.get("ok"):
            return ("restore failed: %s" % res.get("error"), True)
        msg = ("[%s] RESTORED garrysmod/%s from its deploy backup (%d bytes). "
               "The backup is kept, so restoring again stays possible." % (server.upper(), to, res.get("bytes")))
        if to.endswith(".lua") and srv.get("running"):
            msg += "\n(.lua — autorefresh reloads it in ~2s; verify with srcds_lua)"
        return (msg, False)
    if args.get("local"):
        try:
            with open(args["local"], "rb") as f:
                data = f.read()
        except OSError as e:
            return ("could not read local file: %s" % e, True)
    elif "content" in args:
        data = (args["content"] or "").encode("utf-8")
    else:
        return ("provide either 'local' (a local file path) or 'content' (inline string).", True)
    if args.get("confirm") is not True:
        players, maxpl, is_live = live_info(srv)
        ln = ("  (%d/%s players%s)" % (players, maxpl, " — LIVE" if is_live else "")) if players is not None else ""
        return ("BLOCKED: deploy %d bytes -> %s:garrysmod/%s%s. Re-call with confirm=true. Nothing was written."
                % (len(data), server, to, ln), True)
    res = run_driver({"op": "deploy", "uuid": srv["uuid"], "to": to,
                      "content_b64": base64.b64encode(data).decode(),
                      "backup": args.get("backup", True)}, timeout=45)
    log_event({"ev": "deploy", "server": server, "to": to, "bytes": len(data), "ok": res.get("ok")})
    if not res.get("ok"):
        return ("deploy failed: %s" % res.get("error"), True)
    msg = "[%s] deployed %d bytes -> garrysmod/%s" % (server.upper(), res.get("bytes"), to)
    if res.get("backup"):
        msg += "  (overwrote; backup at %s)" % res["backup"]
    elif res.get("overwrote"):
        msg += "  (overwrote, no backup)"
    else:
        msg += "  (new file)"
    if not srv.get("running"):
        msg += "\n(server is DOWN — loads on next boot)"
    elif to.endswith(".lua"):
        msg += "\n(.lua — autorefresh reloads it in ~2s; verify with srcds_lua)"
    return (msg, False)


def tool_grep(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    pattern = args.get("pattern") or ""
    if not pattern.strip():
        return ("pattern is empty", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    glob = args.get("glob") or "*.lua"
    path = args.get("path") or ""
    res = run_driver({"op": "grep", "uuid": srv["uuid"], "pattern": pattern,
                      "path": path, "glob": glob, "max": int(args.get("max", 200))}, timeout=40)
    log_event({"ev": "grep", "server": server, "pattern": pattern[:120], "ok": res.get("ok")})
    if not res.get("ok"):
        return ("grep failed: %s" % res.get("error"), True)
    total, shown = res.get("total", 0), res.get("shown", 0)
    head = "[%s] grep '%s' in %s/%s — %d match(es)%s" % (
        server.upper(), pattern, (path or "."), glob, total,
        ("" if total <= shown else " (showing first %d)" % shown))
    if res.get("note"):
        head += "  [%s]" % res["note"]
    matches = res.get("matches", [])
    return (head + ("\n" + "\n".join(matches) if matches else ""), False)


def tool_diff(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    path = (args.get("path") or "").strip()
    if not path:
        return ("path is empty", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    server_b = args.get("server_b")
    local = args.get("local")
    if bool(server_b) == bool(local):
        return ("give exactly ONE of: server_b (compare against another server) or local (a local file path).", True)
    req = {"op": "diff", "uuid_a": srv["uuid"], "path_a": path,
           "label_a": "%s:%s" % (server, path), "context": int(args.get("context", 3))}
    if local:
        try:
            with open(local, "rb") as f:
                req["content_b64"] = base64.b64encode(f.read()).decode()
        except OSError as e:
            return ("could not read local file: %s" % e, True)
        req["label_b"] = "local:%s" % local
    else:
        if server_b not in SERVER_NAMES:
            return ("server_b must be one of: %s" % ", ".join(SERVER_NAMES), True)
        srv_b = resolve(server_b)
        if not srv_b:
            return ("could not resolve server '%s' (host unreachable?)" % server_b, True)
        path_b = (args.get("path_b") or path).strip()
        req["uuid_b"] = srv_b["uuid"]
        req["path_b"] = path_b
        req["label_b"] = "%s:%s" % (server_b, path_b)
    res = run_driver(req, timeout=60)
    log_event({"ev": "diff", "server": server, "path": path,
               "vs": (server_b or "local"), "ok": res.get("ok"), "equal": res.get("equal")})
    if not res.get("ok"):
        return ("diff failed: %s" % res.get("error"), True)
    head = "[diff] %s  vs  %s" % (req["label_a"], req["label_b"])
    if res.get("equal"):
        return (head + " — IDENTICAL (%s bytes, sha1 %s)" % (res.get("size_a"), res.get("sha_a")), False)
    if res.get("binary"):
        return (head + " — BINARY files DIFFER: %s vs %s bytes (sha1 %s vs %s)" % (
            res.get("size_a"), res.get("size_b"), res.get("sha_a"), res.get("sha_b")), False)
    cap = "  [truncated at 40KB]" if res.get("truncated") else ""
    return (head + " — DIFFER (%s vs %s bytes)%s\n%s" % (
        res.get("size_a"), res.get("size_b"), cap, res.get("diff", "")), False)


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


CLIENTLUA_BODY = r'''
local _b64 = "@B64@"
local _tgt = "@TARGET@"
local CH = 900
local targets = {}
if _tgt == "all" then
  targets = player.GetAll()
else
  for _, p in ipairs(player.GetAll()) do
    if p:SteamID() == _tgt or tostring(p:SteamID64()) == _tgt or p:Nick() == _tgt then
      targets[#targets + 1] = p
    end
  end
end
local n = 0
for _, ply in ipairs(targets) do
  if IsValid(ply) then
    ply:SendLua("__mcpcl=''")
    for i = 1, #_b64, CH do ply:SendLua("__mcpcl=__mcpcl..'" .. _b64:sub(i, i + CH - 1) .. "'") end
    ply:SendLua("RunString(util.Base64Decode(__mcpcl),'mcp_clientlua') __mcpcl=nil")
    n = n + 1
  end
end
LOG("sent clientside code (" .. #_b64 .. " b64 bytes) to " .. n .. " client(s)")
return n
'''


def tool_clientlua(args):
    server = args.get("server")
    if server not in SERVER_NAMES:
        return ("server must be one of: %s" % ", ".join(SERVER_NAMES), True)
    code = args.get("code", "")
    if not code.strip():
        return ("code is empty", True)
    srv = resolve(server)
    if not srv:
        return ("could not resolve server '%s' (host unreachable?)" % server, True)
    if not srv["running"]:
        return ("%s is DOWN — no clients connected." % server.upper(), True)
    if args.get("confirm") is not True:
        players, maxpl, is_live = live_info(srv)
        ln = ("  (%d/%s players%s)" % (players, maxpl, " — LIVE" if is_live else "")) if players is not None else ""
        return ("BLOCKED: runs clientside Lua on %s clients%s. Re-call with confirm=true." % (server, ln), True)
    target = str(args.get("target", "all")).replace('"', "").replace("'", "")
    cb64 = base64.b64encode(code.encode("utf-8")).decode()
    body = CLIENTLUA_BODY.replace("@B64@", cb64).replace("@TARGET@", target)
    tok = os.urandom(8).hex()
    runner = render_runner(tok, "_mcp/%s_body.lua" % tok, False)
    res = run_driver({"op": "lua", "uuid": srv["uuid"], "token": tok, "body": body,
                      "runner": runner, "async": False, "capture_timeout": 8}, timeout=55)
    log_event({"ev": "clientlua", "server": server, "target": target, "bytes": len(code), "ok": res.get("ok")})
    if not res.get("ok"):
        return ("clientlua failed: %s" % res.get("error"), True)
    r = res.get("result") or {}
    if r.get("err"):
        return ("clientlua error: " + r["err"], True)
    out = r.get("out", "") or ("sent to %s client(s)" % (r.get("ret") if r.get("ret") is not None else "?"))
    return ("[%s] clientlua → target=%s\n%s" % (server.upper(), target, out), False)


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
    if action in ("stop", "restart", "kill") and is_live and args.get("force") is not True:
        return ("REFUSED: %s is LIVE (%d players) — %s would disrupt them. Re-call with force=true to override."
                % (server.upper(), players or 0, action), True)
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


# ----------------------------------------------------------------------------
# Database (MariaDB) tools — query via `docker exec` into the mariadb container,
# root password read from the container env ($MYSQL_ROOT_PASSWORD), never extracted.
# ----------------------------------------------------------------------------
# Convenience aliases: a game name -> its main schema. Any real schema name also works.
DB_ALIAS = CFG.get("db_aliases") or {}
DB_READ_FIRST = {"select", "show", "describe", "desc", "explain", "with", "use", "help", "checksum"}


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
    """None if every statement is read-only (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH/USE); else a reason."""
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
                      "format": args.get("format", "tsv")}, timeout=60)
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
    res = run_driver({"op": "db", "sql": sql, "format": fmt}, timeout=30)
    log_event({"ev": "db_schema", "database": database, "table": table, "ok": res.get("ok")})
    if not res.get("ok"):
        return ("db schema failed: %s" % (res.get("error_out") or res.get("error")), True)
    what = ("databases" if not database else ("tables in %s" % database if not table else "%s.%s" % (database, table)))
    return ("[db schema: %s]\n%s" % (what, res.get("output", "") or "(empty)"), False)


# ----------------------------------------------------------------------------
# Tool registry / JSON schemas
# ----------------------------------------------------------------------------
SERVER_ENUM = {"type": "string", "enum": list(SERVER_NAMES),
               "description": "Which server. One of: %s." % ", ".join(SERVER_NAMES)}

# Descriptions are built from the configured topology so they stay truthful for
# any deployment (and after servers are added) — never hardcode server names here.
_NAMES_TXT = ", ".join(SERVER_NAMES) or "(none configured — set servers[] in config.json)"
_ALIAS_TXT = ("" if not DB_ALIAS else
              " Configured aliases: " + ", ".join("%s=%s" % (k, v) for k, v in sorted(DB_ALIAS.items())) + ".")

TOOLS = [
    {
        "name": "srcds_status",
        "description": "List the configured game servers (%s): up/down, live player count (via A2S), LIVE flag vs per-server thresholds, port, and whether console output capture (-condebug) is available. Read-only, always allowed." % _NAMES_TXT,
        "inputSchema": {
            "type": "object",
            "properties": {"server": {"type": "string", "enum": list(SERVER_NAMES),
                                       "description": "Optional: only show this one."}},
        },
    },
    {
        "name": "srcds_fetch",
        "description": "Read-only volume/console access, always allowed. what='console': tail console.log (-condebug servers only). what='docker': tail the container's docker log — console history WITHOUT -condebug, works even while the server is DOWN (crash forensics; covers the current container lifetime). what='file': read a file; add save_to=<local path> for a binary-safe download (crash dumps, .db). what='dir': list a directory (sizes+mtimes). what='hash': sha1 every file under a path — compare two servers' listings to spot divergence, then srcds_diff the files that differ. what='backups': list deploy backups (restore via srcds_deploy restore:true).",
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
            },
            "required": ["server"],
        },
    },
    {
        "name": "srcds_console",
        "description": "Inject a server console command via the pty/docker-attach path (works on -norcon servers). The reply is captured on EVERY server: from the console.log delta where -condebug is on, otherwise live off the attached pty (ANSI-stripped and byte-capped either way). Destructive commands (kick/ban/changelevel/map/password/restart/...) require confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "command": {"type": "string", "description": "The console command, e.g. 'status' or 'ulx adduser ...'."},
                "grep": {"type": "string", "description": "Optional substring filter on captured output."},
                "maxbytes": {"type": "integer", "default": 24000, "description": "Byte cap on the returned console.log delta (keeps the most-recent slice)."},
                "confirm": {"type": "boolean", "default": False, "description": "Set true to authorize a destructive command."},
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
            "async=true and call MCP_DONE() when finished. Mutating/obfuscated Lua (Set*/Kill/Remove/Kick/"
            "Give/file.Write/RunConsoleCommand/_G[]/loadstring/...) requires confirm=true."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "code": {"type": "string", "description": "Server Lua / verification suite. e.g. 'return player.GetCount()' or a multi-line CHECK/EQ assertion suite. Use `return <expr>` or LOG(...) to get values back."},
                "confirm": {"type": "boolean", "default": False, "description": "Set true to authorize mutating or obfuscated Lua."},
                "async": {"type": "boolean", "default": False, "description": "True for suites using timers/coroutines/http; then call MCP_DONE() from the final callback."},
                "async_timeout": {"type": "integer", "default": 20, "description": "Seconds to wait for MCP_DONE() when async=true (max ~30)."},
            },
            "required": ["server", "code"],
        },
    },
    {
        "name": "srcds_deploy",
        "description": "Write a file to a server's volume (deploy an addon .lua, cfg, etc.). Provide 'local' (a local file path to copy) OR 'content' (inline string). Path 'to' is relative to garrysmod/ (no '..'/absolute). UTF-8/CJK-safe (driver write, not scp). Backs up an overwritten file to an out-of-tree backups root by default (never drops backup files into source/addon/git trees). restore:true ROLLS BACK 'to' to its last deploy backup (list them via srcds_fetch what='backups'). .lua files hot-reload via autorefresh. Works even if the server is DOWN (loads on boot). Requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "to": {"type": "string", "description": "Destination path relative to garrysmod/, e.g. 'addons/rals/lua/autorun/server/x.lua' or 'cfg/foo.cfg'."},
                "local": {"type": "string", "description": "A local file path to read and copy (preferred for real files)."},
                "content": {"type": "string", "description": "Inline file content (use instead of 'local' for small/generated files)."},
                "restore": {"type": "boolean", "default": False, "description": "Roll back 'to' to its last deploy backup instead of writing new content (local/content ignored; the backup is kept)."},
                "backup": {"type": "boolean", "default": True, "description": "Back up an overwritten file to the out-of-tree backups root, not next to the file."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true to actually write."},
            },
            "required": ["server", "to"],
        },
    },
    {
        "name": "srcds_grep",
        "description": "Recursively grep a server's live volume source (the ACTUALLY-deployed code, which can diverge from your local copy). Read-only, always allowed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "pattern": {"type": "string", "description": "grep -e pattern (basic regex)."},
                "path": {"type": "string", "description": "Subdir under garrysmod/ to search (default whole volume), e.g. 'addons/rals'."},
                "glob": {"type": "string", "default": "*.lua", "description": "Filename include glob (default *.lua)."},
                "max": {"type": "integer", "default": 200, "description": "Max matches to return."},
            },
            "required": ["server", "pattern"],
        },
    },
    {
        "name": "srcds_diff",
        "description": "Unified diff of a DEPLOYED file: compare the same (or another) path across two servers, or a deployed file against a LOCAL file — the fast way to check local<->live and server<->server divergence before deploying. Reports IDENTICAL / DIFFER (+diff, 40KB cap) / binary mismatch with sizes+sha1. For whole trees: compare srcds_fetch what='hash' listings first, then diff the files whose hashes differ. Read-only, always allowed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "path": {"type": "string", "description": "Side A: path relative to garrysmod/ on `server`."},
                "server_b": {"type": "string", "enum": list(SERVER_NAMES), "description": "Side B server (same path unless path_b). Give this OR local."},
                "path_b": {"type": "string", "description": "Optional different path on server_b."},
                "local": {"type": "string", "description": "Side B = this LOCAL file path. Give this OR server_b."},
                "context": {"type": "integer", "default": 3, "description": "Diff context lines."},
            },
            "required": ["server", "path"],
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
        "description": "Run CLIENTSIDE Lua on connected players (srcds_lua is serverside only). Pushed via chunked base64 SendLua + RunString — good for UI/PAC3/clientside hot-reload without a reconnect. target='all' or a SteamID/SteamID64/nick. Fire-and-forget (returns how many clients it sent to; no per-client result). Best off-peak / few clients. Requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "code": {"type": "string", "description": "Clientside Lua to run on the target players."},
                "target": {"type": "string", "default": "all", "description": "'all', or a SteamID / SteamID64 / exact nick."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true (executes code on clients)."},
            },
            "required": ["server", "code"],
        },
    },
    {
        "name": "srcds_power",
        "description": "Power-control a server via the Pterodactyl wings API (start/stop/restart/kill) — graceful, like the panel button: a stop/restart is a NORMAL quit (exit 0, NOT flagged as a crash) and the server reliably comes back up; wings `start` even recreates a removed container. Requires confirm=true; stopping/restarting/killing a LIVE server additionally needs force=true. start/restart also arm a host-side BOOT WATCHER keyed to Pterodactyl's own boot marker (wings state starting->running); then action='watch' with wait=50 (read-only, no confirm) long-polls and returns as soon as the boot completes — call it after every start/restart instead of polling srcds_status. (Falls back to raw docker only if wings is unreachable.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": SERVER_ENUM,
                "action": {"type": "string", "enum": ["start", "stop", "restart", "kill", "watch"],
                           "description": "'watch' = check/await boot completion after a start/restart (read-only)."},
                "confirm": {"type": "boolean", "default": False, "description": "Required true to perform start/stop/restart/kill (not needed for watch)."},
                "force": {"type": "boolean", "default": False, "description": "Required to stop/restart/kill a LIVE server."},
                "watch": {"type": "boolean", "default": True, "description": "Arm the boot watcher after start/restart."},
                "wait": {"type": "integer", "default": 0, "description": "action='watch': long-poll up to this many seconds (max 55) for BOOT COMPLETE before returning."},
            },
            "required": ["server", "action"],
        },
    },
    {
        "name": "srcds_db_query",
        "description": ("Run SQL against the game MariaDB (via docker exec; root password stays inside the "
                        "container). SELECT/SHOW/DESCRIBE/EXPLAIN run automatically; INSERT/UPDATE/DELETE/DDL "
                        "require confirm=true (they change LIVE player data). `database` accepts a raw schema "
                        "name OR any alias defined in db_aliases in config.json.%s Output is TSV by default "
                        "(token-lean; tabs/newlines in values are escaped); format='table' for a bordered "
                        "human-readable table. Capped ~40KB — add LIMIT for big tables." % _ALIAS_TXT),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Raw schema name, or an alias from db_aliases in config.json. Omit for a server-agnostic query (e.g. information_schema)."},
                "sql": {"type": "string", "description": "The SQL. e.g. \"SELECT * FROM ninv_items WHERE owner='STEAM_0:..' LIMIT 20\"."},
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
]

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
    "srcds_db_query": tool_db_query,
    "srcds_db_schema": tool_db_schema,
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
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "srcds-mcp", "version": "1.4.0"},
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
