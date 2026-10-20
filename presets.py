"""Presets: /<name> opens a new conversation with an agent whose first message is a saved prompt.

<HERMES_HOME>/alternative-agents/presets/<name>.md is the prompt (directory 700, files 600), written
from Telegram with /preset in reply to a message. A preset's optional settings (agent, banner,
description) are under `presets` in the plugin settings. The prompt is read in code only, at the
moment it is sent: it is never shown, logged or recorded (turns.log keeps "(prompt /<name>)").
"""

import os
import re
from pathlib import Path

from .config import cfg

DIR = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "alternative-agents" / "presets"
DEFAULT_BACKEND = "opencode"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ALIASES = {"oc": "opencode", "cc": "claude"}
# Taken whatever Hermes says: the plugin's own commands (agy, cc_on, oc_new…) are checked apart.
_RESERVED = {"preset", "new", "stop", "start", "help"}


def settings(name):
    """The preset's settings: backend (an agent's name), banner, description."""
    raw = (cfg.get("presets") or {}).get(name)
    raw = raw if isinstance(raw, dict) else {}
    backend = str(raw.get("backend") or DEFAULT_BACKEND).strip().lower()
    return {"backend": _ALIASES.get(backend, backend), "banner": str(raw.get("banner") or "").strip(),
            "description": str(raw.get("description") or "").strip()}


def path(name):
    return DIR / f"{name}.md"


def names():
    """Presets with a prompt file, sorted."""
    try:
        return sorted(p.stem for p in DIR.glob("*.md") if _NAME.match(p.stem))
    except OSError:
        return []


def exists(name):
    """True when the preset has a non-empty prompt file (stat only)."""
    try:
        return _NAME.match(name) is not None and path(name).stat().st_size > 0
    except OSError:
        return False


def read(name):
    """The prompt, for the agent only: never show, log or record it."""
    return path(name).read_text().strip()


def save(name, text):
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target, tmp = path(name), DIR / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text.strip() + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(target)


def remove(name):
    path(name).unlink(missing_ok=True)


def name_problem(name, own_commands):
    """Why ``name`` can't be a new preset ("" when it can): it must not shadow a command."""
    if not _NAME.match(name):
        return "a-z, 0-9 and _ only, starting with a letter, 32 at most"
    if name in _RESERVED or name in own_commands:
        return "the name is a command already"
    try:
        from hermes_cli.commands import resolve_command
        from hermes_cli.plugins import get_plugin_commands
        if resolve_command(name) is not None or name.replace("_", "-") in get_plugin_commands():
            return "the name is a command already"
    except Exception:
        pass
    return ""
