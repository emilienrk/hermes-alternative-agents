"""/agy_doctor, /cc_doctor: check every link of the chain without running a turn."""

import asyncio
import json
from pathlib import Path

from . import engine


async def report(agent):
    async def workspace_then_bridge():  # the bridge check reads the MCP config the workspace writes
        lines = await asyncio.to_thread(_workspace, agent)
        return lines + await _bridge(agent)
    checks = await asyncio.gather(
        asyncio.to_thread(_chat, agent), asyncio.to_thread(agent.backend.checks), workspace_then_bridge(),
        asyncio.to_thread(_menu, agent), return_exceptions=True)
    lines = []
    for check in checks:
        if isinstance(check, Exception):
            lines.append(f"✗ {type(check).__name__}: {check}")
        else:
            lines.extend(check)
    lines.append("✓ Telegram adapter bound" if engine.adapter() is not None
                 else "✗ Telegram adapter not bound (answers go through Hermes' standalone sender)")
    ok = all(line.startswith("✓") for line in lines)
    return (f"🩺 {agent.label}: all good" if ok else f"🩺 {agent.label}: needs a look") + "\n" + "\n".join(lines)


def _chat(agent):
    if not engine.TARGET_CHAT:
        return ["✗ no chat_id setting: hermes config set plugins.entries.agy-chat.settings.chat_id <id>, "
                "then restart the gateway"]
    return [f"✓ routed chat: {engine.TARGET_CHAT}"]


def _workspace(agent):
    summary = agent.backend.workspace.prepare()
    lines = [f"✓ Hermes context: {summary['rules_bytes'] // 1024} KB of rules, {summary['skills']} skills"]
    lines.append(f"✓ MCP servers for {agent.label}: {', '.join(summary['mcp_servers']) or 'none'}")
    lines += [f"✗ {error}" for error in summary["errors"]]
    return lines


async def _bridge(agent):
    """Start hermes_mcp.py like the agent would and list its tools."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        return [f"✗ cannot test the MCP bridge: {e}"]
    config = json.loads(agent.backend.workspace.mcp_config.read_text())["mcpServers"].get("hermes")
    if config is None:
        return ["✓ hermes MCP bridge: off (bridge_tools is empty)"]
    params = StdioServerParameters(command=config["command"], args=config["args"],
                                   env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", **config.get("env", {})})

    async def probe():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [tool.name for tool in (await session.list_tools()).tools]
    try:
        names = await asyncio.wait_for(probe(), 30)
    except Exception as e:
        return [f"✗ hermes MCP bridge: {type(e).__name__}: {e}"]
    return [f"✓ hermes MCP bridge: {', '.join(names)}"]


def _menu(agent):
    from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands

    from . import COMMANDS
    prefix = agent.backend.prefix
    # Hermes keeps commands with a required argument (<question>) out of the menu: a tap would send none.
    wanted = [f"{prefix}_{suffix}" if suffix else prefix for suffix, _d, hint in COMMANDS if not hint.startswith("<")]
    menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
    missing = [c for c in wanted if c not in {name for name, _ in menu}]
    if missing:
        return [f"✗ missing from Telegram's / menu: {', '.join('/' + c for c in missing)} "
                "(platforms.telegram.extra.command_menu.priority in config.yaml)"]
    return [f"✓ {agent.label} commands in Telegram's / menu"]
