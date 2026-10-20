"""Plugin settings (plugins.entries.alternative-agents.settings in config.yaml), with their defaults."""

BACKENDS = ("agy", "claude", "opencode")
BRIDGE_TOOLS = ("memory", "session_search", "skill_manage", "cronjob_manage", "text_to_speech",
                "telegram_send", "hermes_thread", "tasks")

DEFAULTS = {
    "chat_id": "",
    "user_name": "",
    "backends": ["agy"],
    "machine_notes": "",
    "skip_skills": [],
    "timeout_minutes": 30,
    # agy
    "agy_path": "",
    "workspace": "~/agy-chat",
    "default_model": "",
    "mark": "🛸",
    # Claude Code
    "claude_path": "",
    "claude_workspace": "~/claude-chat",
    "claude_model": "",
    "claude_effort": "",
    "claude_mark": "✳️",
    # opencode (DeepSeek)
    "opencode_path": "",
    "opencode_workspace": "~/opencode-chat",
    "opencode_model": "",
    "opencode_effort": "",
    "opencode_mark": "🐋",
    "opencode_env_file": "~/.hermes/alternative-agents/.env",
    # /<preset>: {name: {backend, banner, description}}, the prompts being files (presets.py)
    "presets": {},
    # display
    "react_working": "👀",
    "react_queued": "✍",
    "footer": True,
    "verbose": False,
    "leave_button": "Back to Hermes",
    # voice
    "voice_input": "direct",
    "voice_reply": "off",
    "tts_provider": "",
    "tts_voice": "",
    # what the agents get from Hermes
    "share_soul": True,
    "share_memory": True,
    "share_thread": True,
    "share_skills": True,
    "share_mcp_servers": True,
    "mcp_on_demand": ["opencode"],
    "bridge_tools": list(BRIDGE_TOOLS),
}

VOICE_REPLY_MODES = ("off", "mirror", "always")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

cfg = dict(DEFAULTS)


def load(get):
    """Read every setting through ``get(key, default)`` (the plugin context's get_config)."""
    cfg.clear()
    for key, default in DEFAULTS.items():
        value = get(key, default)
        cfg[key] = default if value is None else value
    cfg["chat_id"] = str(cfg["chat_id"] or "").strip()
    try:
        cfg["timeout_minutes"] = max(1, int(cfg["timeout_minutes"]))
    except (TypeError, ValueError):
        cfg["timeout_minutes"] = DEFAULTS["timeout_minutes"]
    if cfg["voice_reply"] not in VOICE_REPLY_MODES:
        cfg["voice_reply"] = "off"
    if cfg["voice_input"] not in ("direct", "local"):
        cfg["voice_input"] = "direct"
    if cfg["claude_effort"] not in EFFORTS:
        cfg["claude_effort"] = ""
    cfg["opencode_effort"] = str(cfg["opencode_effort"] or "").strip().lower()
    if not isinstance(cfg["presets"], dict):
        cfg["presets"] = {}
    backends = [cfg["backends"]] if isinstance(cfg["backends"], str) else cfg["backends"] or []
    cfg["backends"] = [b for b in BACKENDS if b in backends]
    on_demand = cfg["mcp_on_demand"]
    on_demand = [on_demand] if isinstance(on_demand, str) else on_demand or []
    cfg["mcp_on_demand"] = [b for b in BACKENDS if b in on_demand]
    # "agy_task" was the tasks tool's name before Claude Code joined.
    tools = ["tasks" if t == "agy_task" else t for t in (cfg["bridge_tools"] or [])]
    cfg["bridge_tools"] = [t for t in BRIDGE_TOOLS if t in tools]
    return cfg
