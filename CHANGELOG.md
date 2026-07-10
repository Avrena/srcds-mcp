# Changelog

## 1.3.0 — Divergence tooling, rollback, downloads, node health
Two new tools and two extended ones, aimed at the workflows around the core dev
loop: comparing deployments, undoing a bad push, and crash forensics.

- **`srcds_diff` (new).** Unified diff of a deployed file against another
  server's copy (same or different path) or against a local file. Reports
  IDENTICAL / DIFFER (+diff, 40 KB cap) / binary mismatch with sizes and sha1s.
- **`srcds_nodeinfo` (new).** Read-only host health: loadavg, memory/swap,
  uptime, disk usage, per-container `docker stats`, plus optional wings-log and
  `dmesg` tails (crash-detection lines and OOM-killer traces).
- **`srcds_fetch` modes.** `what:"dir"` (listing with sizes/mtimes),
  `what:"hash"` (recursive sha1 of a subtree — compare two servers' listings,
  then `srcds_diff` the files that differ), `what:"backups"` (list deploy
  backups), and `save_to` (binary-safe download of e.g. crash dumps — bytes go
  straight to a local file, never into the conversation; 8 MB cap,
  `overwrite:true` to replace).
- **`srcds_deploy restore:true`.** Rolls a path back to its last out-of-tree
  deploy backup. The backup is deliberately kept (not overwritten by the bad
  file), so restore stays repeatable.
- 12 tools total.

## 1.2.0 — Boot watcher, token-lean output, config-derived descriptions
Output-size hardening for LLM clients, boot-completion notification, and the
last de-branding pass.

- **Boot watcher.** `srcds_power` `start`/`restart` now arm a detached host-side
  watcher keyed to Pterodactyl's own boot marker (wings flips `starting →
  running` on the egg's startup-done line). New read-only
  `action:"watch"` (`wait` up to 55 s) long-polls and returns the moment the
  boot completes — reporting boot duration, `still booting` progress, or
  `DIED DURING BOOT` with the state history. No more `srcds_status` babysitting.
- **`srcds_console` output is now ANSI-stripped and byte-capped** (default 24 KB,
  new `maxbytes` arg), same policy as `srcds_fetch` — previously a chatty live
  console could return a ~200 KB delta littered with truecolor escapes.
- **`srcds_grep` caps each match line at 300 chars** (a match inside a
  minified/packed line used to return the entire line) **and the total payload
  at 40 KB**, with a note when capped.
- **Tool descriptions are built from your config**: server names come from
  `servers[]` and the DB-alias list from `db_aliases`, instead of hardcoding one
  deployment's names and per-server `-condebug` facts. Status header, module
  docstring, and `serverInfo` (`srcds-mcp`) de-branded to match.
- `srcds_status` no longer emits a blank line for servers without a hostname.
- README: new **"Adding a server"** and **"Multiple nodes"**
  (`SRCDS_MCP_CONFIG` double-registration) sections.
- Multi-node hygiene: `.gitignore` now covers `config*.json` and `*.log`; an
  instance started with `SRCDS_MCP_CONFIG` logs beside its own config file.

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
