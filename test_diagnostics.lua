-- Run with Lua 5.1: lua test_diagnostics.lua
-- In GMod, ADAPTER_SOURCE can supply the source to avoid touching game files.
local source = ADAPTER_SOURCE
if not source then
    local f = assert(io.open("srcds_diagnostics.lua", "rb"))
    source = f:read("*a")
    f:close()
end
local passed = 0
local function test(name, run)
    local ok, err = pcall(run)
    assert(ok, name .. ": " .. tostring(err))
    passed = passed + 1
end

-- Only the offline tests use this encoder; live tests use GMod's real encoder.
local function json(value)
    if type(value) == "string" then return string.format("%q", value):gsub("\n", "\\n") end
    if type(value) ~= "table" then return tostring(value) end
    local parts = {}
    for k, v in pairs(value) do parts[#parts + 1] = json(tostring(k)) .. ":" .. json(v) end
    table.sort(parts)
    return "{" .. table.concat(parts, ",") .. "}"
end

local function fixture()
    local e = setmetatable({}, {__index = _G})
    e._G = e
    e._HOLYLIB, e.vprof, e.gameserver, e.httpserver, e.gmoddatapack = false, false, false, false, false
    e.now, e.hibernating = 0, false
    e.SysTime = function() return e.now end
    e.VERSIONSTR = "test"
    e.game = {GetMap = function() return "test" end}
    e.GetConVar = function(name)
        if name == "sv_hibernate_think" then return {GetBool = function() return not e.hibernating end} end
        if name == "holylib_gmoddatapack_luapack_enable" then return {GetBool = function() return e.luapack == true end} end
    end
    e.player = {GetAll = function() return {} end, GetCount = function() return 0 end}
    e.util = {TableToJSON = util and util.TableToJSON or json}
    local hooks, timers = {}, {}
    e.hook = {
        GetTable = function() return hooks end,
        Add = function(event, id, fn) hooks[event] = hooks[event] or {} hooks[event][id] = fn end,
        Remove = function(event, id) if hooks[event] then hooks[event][id] = nil end end,
    }
    e.timer = {
        Create = function(id, duration, count, fn) timers[id] = fn end,
        Remove = function(id) timers[id] = nil end,
        Exists = function(id) return timers[id] ~= nil end,
    }
    local chunk = CompileString and CompileString(source, "diagnostic-test-adapter", false) or loadstring(source)
    assert(type(chunk) == "function", chunk)
    setfenv(chunk, e)
    local adapter = chunk()
    local f = {env = e, hooks = hooks, timers = timers}
    function f.call(action, options)
        local req = {action = action, token = string.rep("a", 32), ttl = 10, limit = 20,
            offset = 0, seconds = 1, maxbytes = 12000}
        for k, v in pairs(options or {}) do req[k] = v end
        f.result = nil
        adapter(req, function(result) f.result = result end)
        return f.result
    end
    function f.emit(message, stack)
        local fn = hooks.OnLuaError["SRCDS_MCP.Diagnostics.Errors"]
        assert(fn(message, "server", stack or {}, "test") == nil)
    end
    function f.profiler(enabled)
        local node = {elapsed = 20, calls = 2}
        function node:GetName() return "test node" end
        function node:GetTotalTimeLessChildren() return self.elapsed end
        function node:GetTotalCalls() return self.calls end
        function node:GetChild() return nil end
        function node:GetSibling() return nil end
        local root = setmetatable({elapsed = 0, calls = 0}, {__index = node})
        function root:GetName() return "Root" end
        function root:GetChild() return node end
        e.vprof = {starts = 0, stops = 0, NODE_GC_SAFE = true,
            IsEnabled = function() return enabled end, GetRoot = function() return root end,
            Start = function() e.vprof.starts = e.vprof.starts + 1 end,
            Stop = function() e.vprof.stops = e.vprof.stops + 1 end}
        return node
    end
    function f.warmup()
        for i = 1, 3 do f.timers["SRCDS_MCP.Diagnostics.Profile"]() end
    end
    return f
end

test("stock capabilities do not require HolyLib or allocate persistent state", function()
    local f = fixture()
    local result = f.call("capabilities")
    assert(result.ok and not result.capabilities.holylib and not result.capabilities.features.profile)
    assert(rawget(f.env, "SRCDS_MCP_DIAGNOSTICS_V1") == nil)
    assert(f.call("players").source == "stock")
end)

test("stock player ping and loss remain available", function()
    local f = fixture()
    f.env.player.GetAll = function() return {{IsValid = function() return true end,
        UserID = function() return 3 end, Nick = function() return "test" end,
        Ping = function() return 40 end, PacketLoss = function() return 2 end}} end
    local p = f.call("players").players[1]
    assert(p.userid == 3 and p.ping_ms == 40 and p.loss_pct == 2)
end)

test("connecting clients without channels never use channel methods", function()
    local f, touched = fixture(), false
    f.env.gameserver = {GetAll = function() return {{IsValid = function() return true end,
        HasNetChannel = function() return false end, GetSignonState = function() return 2 end,
        GetAvgLoss = function() touched = true end}} end}
    assert(f.call("players").players[1].signon == 2 and not touched)
end)

test("error cursors are independent and duplicate counts remain observable", function()
    local f = fixture()
    local initial = f.call("errors_start").cursor
    f.emit("test", {{File = "a.lua", Function = "f", Line = 7}})
    local first = f.call("errors", {after = initial})
    assert(#first.events == 1 and first.events[1].stack[1].line == 7)
    assert(#f.call("errors", {after = initial}).events == 1)
    assert(#f.call("errors", {after = first.cursor}).events == 0)
    f.emit("test", {{File = "a.lua", Function = "f", Line = 7}})
    assert(f.call("errors", {after = first.cursor}).events[1].count == 2)
    assert(f.call("errors_start").existing)
end)

test("queue overflow is bounded and reports a cursor gap", function()
    local f = fixture()
    f.call("errors_start") f.emit("first")
    local cursor = f.call("errors").cursor
    for i = 1, 150 do f.emit("error " .. i) end
    local result = f.call("errors", {after = cursor, limit = 50})
    assert(result.dropped == 23 and result.gap and result.more and #result.events == 50)
end)

test("stop preserves replacements and expired collectors reject old epochs", function()
    local f = fixture()
    local old = f.call("errors_start").cursor
    local replacement = function() end
    f.hooks.OnLuaError["SRCDS_MCP.Diagnostics.Errors"] = replacement
    f.call("errors_stop")
    assert(f.hooks.OnLuaError["SRCDS_MCP.Diagnostics.Errors"] == replacement)
    f.hooks.OnLuaError["SRCDS_MCP.Diagnostics.Errors"] = nil
    f.call("errors_start", {token = string.rep("b", 32)})
    assert(not f.call("errors", {after = old}).ok)
    f.env.now = 11
    assert(not f.call("errors").active)
    assert(not f.timers["SRCDS_MCP.Diagnostics.Errors"])
end)

test("client errors tolerate missing stacks and malformed identities", function()
    local f = fixture()
    f.call("errors_start")
    local broken = setmetatable({}, {__index = function() error("bad identity") end})
    f.hooks.OnClientLuaError["SRCDS_MCP.Diagnostics.Errors"]("test", broken, nil, "test")
    local result = f.call("errors")
    assert(result.client_observed and #result.events[1].stack == 0 and not result.events[1].userid)
end)

test("collector setup failure removes installed hooks", function()
    local f = fixture()
    f.env.timer.Create = function() error("timer unavailable") end
    assert(not f.call("errors_start").ok)
    assert(not f.call("errors").active)
    assert(not f.hooks.OnLuaError["SRCDS_MCP.Diagnostics.Errors"])
end)

test("a cursor from a previous map cannot silently return empty history", function()
    local f = fixture()
    assert(not f.call("errors", {after = string.rep("a", 32) .. ":1"}).ok)
end)

test("error pages never silently consume an oversized first record", function()
    local f = fixture()
    local cursor = f.call("errors_start").cursor
    f.emit(string.rep("x", 2000))
    local result = f.call("errors", {after = cursor, maxbytes = 1030})
    assert(not result.ok and result.cursor == cursor and result.required_bytes > 1030)
    result = f.call("errors", {after = cursor})
    assert(#result.events == 1 and result.events[1].message_truncated)
end)

test("profile computes deltas and balances only its own start", function()
    for _, enabled in ipairs({false, true}) do
        local f = fixture()
        local node = f.profiler(enabled)
        assert(f.call("profile") == nil)
        assert(not f.call("profile").ok)
        f.warmup()
        node.elapsed, node.calls, f.env.now = 25, 5, 1
        f.timers["SRCDS_MCP.Diagnostics.Profile"]()
        assert(f.result.ok and f.result.nodes[1].self_ms == 5 and f.result.nodes[1].calls == 3)
        assert(f.env.vprof.starts == (enabled and 0 or 1))
        assert(f.env.vprof.stops == f.env.vprof.starts)
        assert(not f.timers["SRCDS_MCP.Diagnostics.Profile"])
    end
end)

test("profiler counter reset and timer setup failure restore owned state", function()
    local f = fixture()
    local node = f.profiler(false)
    f.call("profile") f.warmup() node.elapsed = 0 f.env.now = 1
    f.timers["SRCDS_MCP.Diagnostics.Profile"]()
    assert(not f.result.ok and f.env.vprof.stops == 1)
    f = fixture() f.profiler(false)
    f.env.timer.Create = function() error("timer unavailable") end
    assert(not f.call("profile").ok and f.env.vprof.stops == 1)
end)

test("profile discards startup timing artifacts", function()
    local f = fixture()
    local node = f.profiler(false)
    f.call("profile") node.elapsed = 10000 f.warmup()
    node.elapsed, f.env.now = 10002, 1
    f.timers["SRCDS_MCP.Diagnostics.Profile"]()
    assert(f.result.ok and f.result.nodes[1].self_ms == 2)
end)

test("empty hibernating server cannot leave a pending profile", function()
    local f = fixture()
    f.profiler(false) f.env.hibernating = true
    assert(not f.call("profile").ok and f.env.vprof.starts == 0)
end)

test("unsafe native node ownership blocks profiling", function()
    local f = fixture()
    f.profiler(false) f.env.vprof.NODE_GC_SAFE = nil
    assert(not f.call("profile").ok and f.env.vprof.starts == 0)
end)

test("LuaPack refresh preserves exact paths and never enables the feature", function()
    local f, seen = fixture(), {}
    f.env.gmoddatapack = {RefreshExistingLuaFile = function(path) seen[#seen + 1] = path return true, "refresh_queued" end}
    local paths = {"addons/test/lua/cl.lua"}
    assert(f.call("refresh", {paths = paths}).files[1].status == "disabled" and #seen == 0)
    f.env.luapack = true
    local result = f.call("refresh", {paths = paths})
    assert(seen[1] == paths[1] and result.files[1].status == "refresh_queued" and not result.client_execution_verified)
end)

print("Diagnostic Lua tests: " .. passed .. " passed")
