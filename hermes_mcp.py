"""MCP server giving the agents (agy, Claude Code, opencode) the Hermes tools they lack (stdio, launched by
the agent itself).

They already have a terminal, files, a browser and web search. What they
lack is what makes Hermes Hermes: the shared memory, past conversations, skills it can
write, the scheduler, voice notes and a way to message the user mid-task. The registry
tools keep Hermes' own schema and handler, so Hermes and the agent read and write the same state.

Run with Hermes' venv python: alternative-agents' workspace.py writes the launch line into the agent's
MCP config.
"""

import asyncio
import importlib
import io
import json
import os
import sys
from pathlib import Path

# MCP owns stdout; Hermes modules may print while importing or running, so every print
# goes to stderr and the protocol gets the original stream.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
HERMES_SRC = HERMES_HOME / "hermes-agent"
if str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

# Written into the agent's MCP config by workspace.py, from the plugin settings.
TARGET_CHAT = os.environ.get("ALT_AGENTS_CHAT_ID", "")
USER = os.environ.get("ALT_AGENTS_USER") or "the user"
TARGET = f"telegram:{TARGET_CHAT}"
SESSION_KEY = f"agent:main:telegram:dm:{TARGET_CHAT}"
ENABLED = set(filter(None, os.environ.get("ALT_AGENTS_TOOLS", "").split(","))) or None  # None: all
TASK_SETTINGS = json.loads(os.environ.get("ALT_AGENTS_TASK") or "{}")
AGENT = TASK_SETTINGS.get("label") or "agy"
TASK_TOOL = f"{TASK_SETTINGS.get('prefix') or 'agy'}_task"
# Hermes' MCP servers behind two tools, their schemas fetched when needed: an agent without deferred
# tool loading would otherwise carry every MCP tool's description in each request.
ON_DEMAND = os.environ.get("ALT_AGENTS_MCP_ON_DEMAND") == "1"
CATALOG = Path(os.environ.get("ALT_AGENTS_MCP_CATALOG")
               or HERMES_HOME / "plugin-data" / "alternative-agents" / "mcp_catalog.json")
SUMMARY_CHARS = 70


# tool -> (module registering it, note appended to Hermes' description for the agent)
REGISTRY_TOOLS = {
    "memory": ("tools.memory_tool", (
        "Memory shared with Hermes: what you write here, Hermes reads on every turn, and vice versa. "
        "Only put durable things in it (preference, stable fact, decision), not a task report.")),
    "session_search": ("tools.session_search_tool", (
        f"Searches {USER}'s past conversations with Hermes (Telegram, CLI, email…).")),
    "skill_manage": ("tools.skill_manager_tool", (
        "Creates or fixes a Hermes skill: both Hermes and you will see it afterwards. "
        "Do it when you found a non-obvious procedure worth reusing.")),
    "cronjob_manage": ("tools.cronjob_tools", (
        f"For {USER}: deliver='{TARGET}'. Reminder or fixed task: no_agent=true + script (free, the script's "
        "output is sent as is). A job with a prompt runs Hermes' LLM, which is paid: suggest it to "
        f"{USER} before creating it.")),
    "text_to_speech": ("tools.tts_tool", (
        "The result holds a path: put MEDIA:<path> on a line of its own in your answer to send it as a "
        "voice note.")),
}

CUSTOM_TOOLS = {
    "telegram_send": {
        "description": (
            f"Sends a Telegram message to {USER} right away: progress update on long work, or a file to "
            "deliver before the end (a MEDIA:/absolute/path line in the message). Your final answer is "
            "sent anyway: don't use this to duplicate it."),
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Text (simple Markdown)."}},
            "required": ["message"],
        },
    },
    TASK_TOOL: {
        "description": (
            f"Schedules a prompt that YOU ({AGENT}) will run unattended, free (no Hermes LLM): your answer is "
            f"sent to {USER} on Telegram. For recurring or later work: daily digest, weekly check, \"in 2h, "
            "remind me…\". The prompt must be self-contained (no access to this conversation). Actions: "
            "list, create (schedule + prompt [+ name]), remove (job_id), run (job_id, now)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "remove", "run"]},
                "schedule": {"type": "string", "description": (
                    "create: 'every day at 8am', 'every monday 9am', 'weekdays at 7am', '0 7 * * 1-5', "
                    "'in 2h' (once), 'every 6h', or an ISO timestamp (once).")},
                "prompt": {"type": "string", "description": "create: what to do, self-contained."},
                "name": {"type": "string", "description": "create: short name (optional)."},
                "job_id": {"type": "string", "description": "remove/run: from list."},
            },
            "required": ["action"],
        },
    },
    "hermes_thread": {
        "description": (
            f"Latest messages between {USER} and Hermes on Telegram (the thread you don't see). Useful "
            f"when {USER} refers to something said to or asked of Hermes."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Number of messages (default 20, max 100)."}},
        },
    },
}

ON_DEMAND_TOOLS = {
    "mcp_tool_info": {
        "description": "Parameters and full description of MCP tools listed below (call it before mcp_tool_call).",
        "inputSchema": {
            "type": "object",
            "properties": {"names": {"type": "array", "items": {"type": "string"},
                                     "description": "Tools as listed: server.tool"}},
            "required": ["names"],
        },
    },
    "mcp_tool_call": {
        "description": "Calls one of the MCP tools listed in mcp_tool_info.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "server.tool"},
                           "arguments": {"type": "object", "description": "As described by mcp_tool_info."}},
            "required": ["name"],
        },
    },
}

_loaded = {}
_mcp = {}  # full tool name -> schema, once Hermes' MCP servers are connected


def _boot():
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()  # the Telegram token and tool settings live in Hermes' .env


def _registry_entry(name):
    from tools.registry import registry
    if name not in _loaded:
        importlib.import_module(REGISTRY_TOOLS[name][0])
        _loaded[name] = True
    return registry.get_entry(name)


def _enabled(name):
    return ENABLED is None or ("tasks" if name == TASK_TOOL else name) in ENABLED


def list_tools():
    tools = []
    for name, (_module, note) in REGISTRY_TOOLS.items():
        if not _enabled(name):
            continue
        try:
            entry = _registry_entry(name)
        except Exception as e:  # one broken Hermes module must not hide the other tools
            print(f"hermes-mcp: {name} unavailable: {e}", file=sys.stderr)
            continue
        if entry is None:
            continue
        schema = entry.schema
        tools.append({"name": name, "description": f"{schema.get('description', '')}\n\n{note}".strip(),
                      "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}}})
    tools += [{"name": name, **spec} for name, spec in CUSTOM_TOOLS.items() if _enabled(name)]
    if ON_DEMAND:
        info = ON_DEMAND_TOOLS["mcp_tool_info"]
        tools.append({"name": "mcp_tool_info", "inputSchema": info["inputSchema"],
                      "description": f"{info['description']}\n\n{_listing()}"})
        tools.append({"name": "mcp_tool_call", **ON_DEMAND_TOOLS["mcp_tool_call"]})
    return tools


def _connect_mcp():
    """Connect Hermes' MCP servers through its own client (config, auth) and refresh the catalog."""
    if _mcp:
        return _mcp
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.registry import registry
    for name in discover_mcp_tools():
        entry = registry.get_entry(name)
        if entry is not None:
            _mcp[name] = entry.schema
    catalog = {name: (schema.get("description") or "").strip() for name, schema in _mcp.items()}
    try:
        CATALOG.parent.mkdir(parents=True, exist_ok=True)
        CATALOG.write_text(json.dumps(catalog, ensure_ascii=False))
    except OSError as e:
        print(f"hermes-mcp: cannot write {CATALOG}: {e}", file=sys.stderr)
    return _mcp


def _listing():
    """The MCP tools by server, one line each, from the catalog (connecting once when there is none)."""
    try:
        catalog = json.loads(CATALOG.read_text())
    except (OSError, ValueError):
        catalog = {name: schema.get("description") or "" for name, schema in _connect_mcp().items()}
    by_server = {}
    for name, description in sorted(catalog.items()):
        server, _, tool = name.removeprefix("mcp__").partition("__")
        summary = " ".join(description.split())
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS - 1] + "…"
        by_server.setdefault(server, []).append(f"  {server}.{tool}: {summary}" if summary else f"  {server}.{tool}")
    if not by_server:
        return "No MCP tools available."
    return "MCP tools:\n" + "\n".join(line for lines in by_server.values() for line in lines)


def _mcp_name(name):
    name = str(name or "").strip()
    server, dot, tool = name.partition(".")
    return f"mcp__{server}__{tool}" if dot and not name.startswith("mcp__") else name


def mcp_tool_info(names):
    tools = _connect_mcp()
    out = {}
    for name in names or []:
        schema = tools.get(_mcp_name(name))
        out[name] = ({"description": schema.get("description", ""), "parameters": schema.get("parameters") or {}}
                     if schema else {"error": "unknown tool"})
    return out


def mcp_tool_call(name, arguments):
    full = _mcp_name(name)
    if full not in _connect_mcp():
        return json.dumps({"error": f"Unknown MCP tool: {name}"})
    from tools.registry import registry
    result = registry.dispatch(full, dict(arguments or {}))
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def call_tool(name, args):
    args = dict(args or {})
    if ON_DEMAND and name == "mcp_tool_info":
        return json.dumps(mcp_tool_info(args.get("names")), ensure_ascii=False)
    if ON_DEMAND and name == "mcp_tool_call":
        return mcp_tool_call(args.get("name"), args.get("arguments"))
    if not _enabled(name):
        return json.dumps({"error": f"Tool disabled: {name}"})
    if name == TASK_TOOL:
        return json.dumps(task(args), ensure_ascii=False)
    if name == "telegram_send":
        from tools.send_message_tool import send_message_tool
        return send_message_tool({"action": "send", "target": TARGET, "message": str(args.get("message") or "")})
    if name == "hermes_thread":
        return json.dumps(hermes_thread(args.get("limit") or 20), ensure_ascii=False)
    if name not in REGISTRY_TOOLS:
        return json.dumps({"error": f"Unknown tool: {name}"})
    from tools.registry import registry
    _registry_entry(name)
    result = registry.dispatch(name, args)
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def _load_by_path(module_name, filename):
    """A plugin module, by path: this runs outside the package (and must not shadow Hermes' modules)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).resolve().with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task(args):
    tasks = _load_by_path("alternative_agents_tasks", "tasks.py")
    action = args.get("action")
    if action == "list":
        return {"success": True, "tasks": tasks.list_tasks()}
    if action == "create":
        return {"success": True, "task": tasks.create(str(args.get("schedule") or ""), str(args.get("prompt") or ""),
                                                      TASK_SETTINGS, str(args.get("name") or ""))}
    if action == "remove":
        tasks.remove(str(args.get("job_id")))
        return {"success": True}
    if action == "run":
        tasks.run_now(str(args.get("job_id")))
        return {"success": True, "note": "started; its answer will be sent on Telegram"}
    return {"success": False, "error": f"unknown action: {action}"}


def hermes_thread(limit, max_chars=1500):
    """Recent text messages of the user's Telegram DM session with Hermes, oldest first."""
    from hermes_state_registry import acquire, release_or_close
    limit = max(1, min(int(limit), 100))
    db = acquire()
    try:
        row = next((r for r in db.list_gateway_sessions(active_only=True)
                    if r.get("session_key") == SESSION_KEY), None)
        if row is None:
            return {"messages": [], "note": "no active Telegram session"}
        messages = []
        for msg in db.get_messages(row["id"]):
            content = msg.get("content")
            if msg.get("role") not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": USER if msg["role"] == "user" else "Hermes",
                             "at": _iso(msg.get("timestamp")), "text": content.strip()[:max_chars]})
        return {"session_title": row.get("title") or "", "messages": messages[-limit:]}
    finally:
        release_or_close(db)


def _iso(ts):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return ""


async def _serve():
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(_ctx, _params):
        tools = await asyncio.to_thread(list_tools)
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"], input_schema=t["inputSchema"]) for t in tools])

    async def on_call_tool(_ctx, params):
        try:
            text = await asyncio.to_thread(call_tool, params.name, params.arguments)
            failed = _is_error(text)
        except Exception as e:
            text, failed = json.dumps({"error": f"{type(e).__name__}: {e}"}), True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=failed)

    server = Server("hermes", instructions="Hermes' tools (memory, history, skills, scheduling, Telegram).",
                    on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    stdout = anyio.wrap_file(io.TextIOWrapper(_PROTOCOL_OUT.buffer, encoding="utf-8"))
    async with stdio_server(stdout=stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _is_error(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("success") is False or
                                       (bool(data.get("error")) and not data.get("success")))


if __name__ == "__main__":
    _boot()
    asyncio.run(_serve())
