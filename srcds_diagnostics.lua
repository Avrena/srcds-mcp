-- Fixed diagnostic operations, loaded through the existing framed Lua runner.
-- No network listener or autorun installation. Persistent collectors expire.
local STATE = "SRCDS_MCP_DIAGNOSTICS_V1"
local HOOK = "SRCDS_MCP.Diagnostics.Errors"
local PROFILE = "SRCDS_MCP.Diagnostics.Profile"
local MAX_EVENTS, MAX_NODES = 128, 2048

---@param value any
---@param size integer
---@return string
local function text(value, size)
    local kind = type(value)
    local s = kind == "string" and value or (kind == "number" and tostring(value) or "")
    if #s <= size then return s end
    s = string.sub(s, 1, size)
    -- Drop the last possibly incomplete UTF-8 character, not part of the next one.
    while #s > 0 and string.byte(s, -1) >= 128 and string.byte(s, -1) < 192 do
        s = string.sub(s, 1, -2)
    end
    if #s > 0 and string.byte(s, -1) >= 192 then s = string.sub(s, 1, -2) end
    return s .. "..."
end

---@param value any
---@return number|nil
local function number(value)
    if type(value) == "number" and value == value and math.abs(value) < math.huge then return value end
end

---@param object any
---@param name string
---@param ... any
---@return any
local function method(object, name, ...)
    local found, fn = pcall(function() return object and object[name] end)
    if not found or type(fn) ~= "function" then return nil end
    local ok, value = pcall(fn, object, ...)
    if ok then return value end
end

---@param library table|nil
---@param names string[]
---@return boolean
local function functions(library, names)
    if type(library) ~= "table" then return false end
    for _, name in ipairs(names) do
        if type(library[name]) ~= "function" then return false end
    end
    return true
end

---@return table
local function state()
    local s = rawget(_G, STATE)
    if s == nil then
        s = {version = 1}
        rawset(_G, STATE, s)
    end
    assert(type(s) == "table" and s.version == 1, "diagnostic state name is occupied")
    return s
end

---@param collector table
local function stopErrors(collector)
    collector.active = false
    for event, callback in pairs(collector.hooks) do
        local installed = hook.GetTable()[event]
        if installed and installed[HOOK] == callback then hook.Remove(event, HOOK) end
    end
    timer.Remove(HOOK)
end

---@return table|nil
local function errors()
    local s = rawget(_G, STATE)
    local c = type(s) == "table" and s.errors or nil
    if c and c.active and SysTime() >= c.expires then stopErrors(c) end
    return c
end

---@return table
local function capabilities()
    local enabled = GetConVar("holylib_gmoddatapack_luapack_enable")
    local c = errors()
    return {
        holylib = _HOLYLIB == true, version = text(_HOLYLIB_VERSION, 80),
        map = text(game.GetMap(), 128), gmod = text(VERSIONSTR, 80),
        features = {
            profile = functions(vprof, {"Start", "Stop", "IsEnabled", "GetRoot"}) and vprof.NODE_GC_SAFE == true,
            netstats = functions(gameserver, {"GetAll"}),
            http = functions(httpserver, {"Create"}),
            luapack_refresh = functions(gmoddatapack, {"RefreshExistingLuaFile"}),
            luapack_enabled = enabled ~= nil and enabled:GetBool() or false,
        },
        errors = {active = c ~= nil and c.active or false,
            server_observed = c ~= nil and c.server_observed or false,
            client_observed = c ~= nil and c.client_observed or false},
    }
end

---@param request table
---@return table
local function players(request)
    local rows = {}
    local extended = functions(gameserver, {"GetAll"})
    local all = extended and gameserver.GetAll() or player.GetAll()
    for _, client in ipairs(all) do
        if #rows >= 512 then break end
        if method(client, "IsValid") then
            local row = {userid = number(method(client, extended and "GetUserID" or "UserID")),
                name = text(method(client, extended and "GetName" or "Nick"), 96)}
            if extended then
                row.slot = number(method(client, "GetPlayerSlot"))
                row.signon = number(method(client, "GetSignonState"))
                row.connected = method(client, "IsConnected") == true
                if method(client, "HasNetChannel") then
                    for flow, label in pairs({[0] = "out", [1] = "in"}) do
                        local latency = number(method(client, "GetAvgLatency", flow))
                        local loss = number(method(client, "GetAvgLoss", flow))
                        row["latency_" .. label .. "_ms"] = latency and latency * 1000
                        row["loss_" .. label .. "_pct"] = loss and loss * 100
                    end
                    row.choked_packets = number(method(client, "GetChokedPackets"))
                    row.timeout_seconds = number(method(client, "GetTimeout"))
                end
            else
                row.ping_ms = number(method(client, "Ping"))
                row.loss_pct = number(method(client, "PacketLoss"))
            end
            rows[#rows + 1] = row
        end
    end
    table.sort(rows, function(a, b) return (a.userid or -1) < (b.userid or -1) end)
    local shown = {}
    for i = request.offset + 1, math.min(#rows, request.offset + request.limit) do
        shown[#shown + 1] = rows[i]
        if #util.TableToJSON(shown) > (request.maxbytes or 12000) - 1024 then
            shown[#shown] = nil
            break
        end
    end
    return {ok = true, source = extended and "holylib" or "stock", players = shown,
        total = #rows, next_offset = request.offset + #shown,
        more = request.offset + #shown < #rows, scan_limited = #rows >= 512}
end

---@param request table
---@return table
local function startErrors(request)
    local s = state()
    local existing = errors()
    if existing and existing.active then
        return {ok = true, active = true, existing = true, cursor = existing.epoch .. ":" .. existing.seq,
            remaining_seconds = math.max(0, existing.expires - SysTime())}
    end
    for _, event in ipairs({"OnLuaError", "OnClientLuaError", "ShutDown"}) do
        local installed = hook.GetTable()[event]
        assert(not (installed and installed[HOOK]), "diagnostic hook name is occupied")
    end
    assert(not timer.Exists(HOOK), "diagnostic timer name is occupied")
    local c = {active = true, epoch = request.token, seq = 0, dropped = 0, failures = 0,
        expires = SysTime() + request.ttl, events = {}, hooks = {}, busy = false}

    local function record(message, realm, stack, userid, addon)
        if not c.active or c.busy then return end
        if SysTime() >= c.expires then stopErrors(c) return end
        c.busy = true
        local ok = pcall(function()
            local frames = {}
            if type(stack) == "table" then
                for i = 1, math.min(#stack, 6) do
                    local frame = rawget(stack, i)
                    if type(frame) == "table" then
                        frames[#frames + 1] = {
                            source = text(rawget(frame, "File") or rawget(frame, "source"), 192),
                            name = text(rawget(frame, "Function") or rawget(frame, "function") or rawget(frame, "name"), 96),
                            line = number(rawget(frame, "Line") or rawget(frame, "line"))}
                    end
                end
            end
            local entry = {message = text(message, 1024), realm = realm, stack = frames,
                userid = number(userid), addon = text(addon, 128), count = 1, time = SysTime()}
            entry.stack_truncated = type(stack) == "table" and #stack > 6 or false
            entry.message_truncated = type(message) == "string" and #message > 1024 or false
            local key = util.TableToJSON({entry.message, realm, entry.userid or 0, frames})
            c.seq = c.seq + 1
            local previous = c.events[#c.events]
            if previous and previous.key == key and entry.time - previous.time <= 1 then
                previous.count = previous.count + 1
                previous.time, previous.seq = entry.time, c.seq
            else
                entry.key, entry.seq = key, c.seq
                if #c.events >= MAX_EVENTS then
                    c.dropped = c.dropped + table.remove(c.events, 1).count
                end
                c.events[#c.events + 1] = entry
            end
            c[realm .. "_observed"] = true
        end)
        if not ok then c.failures = c.failures + 1 end
        c.busy = false
    end
    c.hooks.OnLuaError = function(message, realm, stack, addon)
        record(message, "server", stack, nil, addon)
    end
    c.hooks.OnClientLuaError = function(message, client, stack, addon)
        -- Copy identity while the reported player is still valid; never retain it.
        record(message, "client", stack, method(client, "UserID"), addon)
    end
    c.hooks.ShutDown = function() stopErrors(c) end
    s.errors = c
    local installed, err = pcall(function()
        for event, callback in pairs(c.hooks) do hook.Add(event, HOOK, callback) end
        timer.Create(HOOK, 1, 0, function()
            if SysTime() >= c.expires then stopErrors(c) end
        end)
    end)
    if not installed then stopErrors(c) error(err) end
    return {ok = true, active = true, cursor = c.epoch .. ":0", remaining_seconds = request.ttl}
end

---@param request table
---@return table
local function readErrors(request)
    local c = errors()
    if not c then
        if request.after then return {ok = false, error = "error cursor unavailable after map change or server restart; start a collector"} end
        return {ok = true, active = false, events = {}, note = "collector has not been started"}
    end
    local after = 0
    if request.after then
        local epoch, seq = string.match(request.after, "^([a-f0-9]+):(%d+)$")
        if epoch ~= c.epoch then
            return {ok = false, error = "error cursor expired after collector restart or map change", cursor = c.epoch .. ":0"}
        end
        after = tonumber(seq)
        assert(after and after <= c.seq, "error cursor is ahead of this collector")
    end
    local rows, cursor, more = {}, after, false
    for _, event in ipairs(c.events) do
        if event.seq > after then
            local entry = {}
            for k, v in pairs(event) do if k ~= "key" then entry[k] = v end end
            rows[#rows + 1] = entry
            local bytes = #util.TableToJSON(rows)
            if #rows > request.limit or bytes > request.maxbytes - 1024 then
                rows[#rows] = nil
                if #rows == 0 then
                    return {ok = false, error = "maxbytes too small for next error; increase the budget",
                        required_bytes = bytes + 1024, cursor = c.epoch .. ":" .. after}
                end
                more = true
                break
            end
            cursor = event.seq
        end
    end
    return {ok = true, active = c.active, events = rows, more = more, cursor = c.epoch .. ":" .. cursor,
        dropped = c.dropped, collector_failures = c.failures,
        gap = #c.events > 0 and after > 0 and after < c.events[1].seq - c.events[1].count,
        server_observed = c.server_observed or false, client_observed = c.client_observed or false,
        remaining_seconds = math.max(0, c.expires - SysTime())}
end

---@param library table
---@return table
local function snapshot(library)
    local pending = {{node = library.GetRoot(), path = "root", label = "", depth = 0}}
    local seen, rows, count = {}, {}, 0
    while #pending > 0 do
        local item = table.remove(pending)
        local node = item.node
        if node and not seen[node] then
            seen[node] = true
            count = count + 1
            assert(count <= MAX_NODES and item.depth < 128, "profile tree exceeds traversal limit")
            local name = text(node:GetName(), 160)
            local label = text(item.label .. "/" .. name, 256)
            rows[item.path] = {name = name, path = label, self_ms = node:GetTotalTimeLessChildren(), calls = node:GetTotalCalls()}
            local sibling, child = node:GetSibling(), node:GetChild()
            if sibling then pending[#pending + 1] = {node = sibling, path = item.path .. "/s", label = item.label, depth = item.depth} end
            if child then pending[#pending + 1] = {node = child, path = item.path .. "/c", label = label, depth = item.depth + 1} end
        end
    end
    return rows
end

---@param request table
---@param reply fun(result: table)
local function profile(request, reply)
    assert(capabilities().features.profile, "HolyLib vprof API or NODE_GC_SAFE capability unavailable; upgrade HolyLib")
    local hibernate = GetConVar("sv_hibernate_think")
    assert(not (hibernate and not hibernate:GetBool() and player.GetCount() == 0),
        "empty server may hibernate; profiling needs active frames")
    local s, library = state(), vprof
    assert(not s.profile and not timer.Exists(PROFILE), "a diagnostic profile is already active")
    local installed = hook.GetTable().ShutDown
    assert(not (installed and installed[PROFILE]), "diagnostic profile hook name is occupied")
    local before, ticks
    ticks = 0
    local p = {owned = not library.IsEnabled(), finished = false}
    s.profile = p
    local started
    local function finish(cancelled, failure)
        if p.finished then return end
        p.finished = true
        timer.Remove(PROFILE)
        local hooks = hook.GetTable().ShutDown
        if hooks and hooks[PROFILE] == p.shutdown then hook.Remove("ShutDown", PROFILE) end
        if s.profile == p then s.profile = nil end
        -- Pair only our own Start; never reset counters or stop an existing session.
        local stopped, stopError = true, nil
        if p.started then stopped, stopError = pcall(library.Stop) end
        if cancelled then reply({ok = false, error = "profile interrupted by server shutdown"}) return end
        if failure then reply({ok = false, error = text(failure, 1024)}) return end
        local ok, result = pcall(function()
            assert(stopped, stopError)
            assert(vprof == library, "vprof module changed during profile")
            local after, rows = snapshot(library), {}
            for path, current in pairs(after) do
                local previous = before[path] or {self_ms = 0, calls = 0}
                assert(not before[path] or (previous.name == current.name and previous.path == current.path), "profile tree changed during sample")
                local elapsed, calls = current.self_ms - previous.self_ms, current.calls - previous.calls
                assert(elapsed >= -0.01 and calls >= 0, "profile counters reset during sample")
                if path ~= "root" and (elapsed > 0 or calls > 0) then
                    rows[#rows + 1] = {name = current.name, path = current.path, self_ms = math.max(0, elapsed), calls = calls}
                end
            end
            table.sort(rows, function(a, b) return a.self_ms > b.self_ms end)
            local total = #rows
            while #rows > request.limit do table.remove(rows) end
            while #rows > 0 and #util.TableToJSON(rows) > (request.maxbytes or 12000) - 1024 do table.remove(rows) end
            return {ok = true, seconds = SysTime() - started, nodes = rows, total = total,
                borrowed = not p.owned, scope = "vprof nodes; gamemode events aggregate hook callbacks"}
        end)
        reply(ok and result or {ok = false, error = text(result, 1024)})
    end
    p.shutdown = function() finish(true) end
    local ok, err = pcall(function()
        if p.owned then library.Start() p.started = true end
        hook.Add("ShutDown", PROFILE, p.shutdown)
        timer.Create(PROFILE, 0, 0, function()
            local sampled, sampleError = pcall(function()
                ticks = ticks + 1
                if not before then
                    -- Discard startup/partial frames without resetting shared counters.
                    if ticks >= 3 then before = snapshot(library) started = SysTime() end
                elseif SysTime() - started >= request.seconds then finish(false) end
            end)
            if not sampled then finish(false, sampleError) end
        end)
    end)
    if not ok then finish(true) error(err) end
end

---@param request table
---@return table
local function refresh(request)
    local caps, rows = capabilities(), {}
    for _, path in ipairs(request.paths) do
        local status, changed = "unsupported", false
        if caps.features.luapack_refresh and caps.features.luapack_enabled then
            local ok, value, detail = pcall(gmoddatapack.RefreshExistingLuaFile, path)
            status = ok and text(detail, 96) or "refresh_error"
            changed = ok and value == true
        elseif caps.features.luapack_refresh then status = "disabled" end
        rows[#rows + 1] = {path = path, status = status, changed = changed}
    end
    return {ok = true, files = rows, client_execution_verified = false}
end

---@param request table
---@param reply fun(result: table)
return function(request, reply)
    local replied = false
    local function once(result)
        if replied then return end
        replied = true
        reply(result)
    end
    local ok, err = pcall(function()
        local action = request.action
        if action == "profile" then profile(request, once) return end
        local result
        if action == "capabilities" then result = {ok = true, capabilities = capabilities()}
        elseif action == "players" then result = players(request)
        elseif action == "errors_start" then result = startErrors(request)
        elseif action == "errors" then result = readErrors(request)
        elseif action == "errors_stop" then
            local c = errors()
            if c then stopErrors(c) end
            result = {ok = true, active = false}
        elseif action == "refresh" then result = refresh(request)
        else error("unknown diagnostic action") end
        once(result)
    end)
    if not ok then once({ok = false, error = text(err, 1024)}) end
end
