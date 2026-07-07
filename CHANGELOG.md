# Changelog

## 1.1.0 — Public release packaging
First shareable, open build. Config-driven and sanitized: the repo ships with
**blank connection settings and no real endpoints/keys** — you supply the node
address and SSH key locally via `config.json` (git-ignored). A prominent README
section covers the "the AI didn't actually configure the MCP" failure mode.

- **Config-driven.** All machine- and deployment-specific values moved out of the
  source into `config.json` (with `SRCDS_MCP_*` env overrides and built-in
  defaults). Copy `config.example.json` → `config.json` and set `ssh.key`.
- **No secrets in the bundle.** The SSH private key is referenced by path only and
  is obtained out-of-band; `config.json`, `*.pem`, and `srcds_mcp.log` are
  git-ignored.
- **Portable host driver.** The host-side facts (volume root, backups root, wings
  API/config, owner uid/gid, and the server topology) are templated into the
  uploaded driver from config. The driver stays byte-identical across developers on
  the same node (stable content hash); a different deployment yields a different,
  correctly namespaced driver.
- **Topology-derived everywhere.** Valid server names, the tool input enums,
  live-threshold display, and DB aliases are all derived from `config.servers` /
  `config.live_thresholds` / `config.db_aliases`.
- **Clear config errors.** Tools return a friendly `config: …` message when
  `ssh.key`/`ssh.host` is unset or the key path doesn't exist, instead of an opaque
  SSH failure.
- Added `README.md` (dev quick-start + reference), `config.example.json`,
  `.gitignore`, `mcp.json.example`.
- Behaviour of all 10 tools is unchanged from 1.0.0.

## 1.0.0 — Internal single-developer tool
- 10 tools: `srcds_status`, `srcds_fetch`, `srcds_console`, `srcds_lua`,
  `srcds_deploy`, `srcds_grep`, `srcds_clientlua`, `srcds_power`, `srcds_db_query`,
  `srcds_db_schema`.
- SSH → `pty.fork(docker attach)` injection transport; file-based Lua output bridge
  works on all servers regardless of `-condebug`; wings-API power control; MariaDB
  query/schema tools; confirm-gated safety model.
