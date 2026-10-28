"""Run buttons: when a preset turns them on, each code block in the agent's answer gets a button that
runs the script on this machine and sends its output back to Telegram.

A preset runs bare: the agent has no tools, so it can only write code, not act. These buttons let the
user run what it wrote, from their phone, with a confirmation and nothing to install or SSH into. The
script is the agent's answer, already shown in the chat; it is saved under the plugin's data dir only
between the button and the click, deleted as soon as it runs, and pruned by age otherwise.

The script runs as the gateway's user, like anything else the user could type: whoever can press the
button could already send the agent a message. The confirmation is the guard, not a sandbox.
"""

import asyncio
import logging
import os
import re
import signal
import time
from pathlib import Path
from secrets import token_hex

from . import state
from .config import cfg

logger = logging.getLogger(__name__)

ASK = "alt-agents:run:"   # + <id>: offer to run (asks to confirm)
GO = "alt-agents:run!:"   # + <id>: confirmed, run it
CANCEL = "alt-agents:runx:"  # + <id>: drop it
PREFIX = "alt-agents:run"  # the three share this, told apart by the next char

MAX_BLOCKS = 5
MAX_OUTPUT = 3500
PRUNE_AFTER_S = 3600
_ID = re.compile(r"^[0-9a-f]{12}$")
_FENCE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\n(.*?)```", re.S)
_RUNNERS = {"py": "python3", "python": "python3", "python3": "python3",
            "sh": "bash", "bash": "bash", "shell": "bash", "zsh": "bash"}


def _dir():
    return state.DATA_DIR / "scripts"


def blocks(text):
    """The runnable fenced code blocks of ``text``: (language word, extension, code), at most MAX_BLOCKS."""
    out = []
    for lang, code in _FENCE.findall(text or ""):
        runner = _RUNNERS.get(lang.lower())
        if runner and code.strip():
            out.append((lang.lower(), "py" if runner == "python3" else "sh", code))
        if len(out) >= MAX_BLOCKS:
            break
    return out


def _prune():
    now = time.time()
    for script in _dir().glob("*"):
        try:
            if now - script.stat().st_mtime > PRUNE_AFTER_S:
                script.unlink()
        except OSError:
            pass


def save(code, ext):
    """Write ``code`` to a fresh file, pruning stale ones first; returns its id."""
    _dir().mkdir(parents=True, exist_ok=True)
    _prune()
    sid = token_hex(6)
    path = _dir() / f"{sid}.{ext}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(code if code.endswith("\n") else code + "\n")
    return sid


def _path(sid):
    if not _ID.match(sid or ""):
        return None
    return next((p for p in _dir().glob(f"{sid}.*")), None)


def drop(sid):
    path = _path(sid)
    if path is not None:
        path.unlink(missing_ok=True)


async def run(sid):
    """Run the saved script (then delete it): (ok, output). ``ok`` is False when it's gone or failed."""
    path = _path(sid)
    if path is None:
        return False, "This script is no longer available."
    interp = "python3" if path.suffix == ".py" else "bash"
    timeout = max(1, int(cfg.get("run_timeout_seconds") or 120))
    try:
        proc = await asyncio.create_subprocess_exec(
            interp, str(path), cwd=str(Path.home()), env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        path.unlink(missing_ok=True)
        return False, f"Could not start {interp}: {e}"
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, _ = await proc.communicate()
    finally:
        path.unlink(missing_ok=True)
    text = (out or b"").decode("utf-8", errors="replace").strip()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n…(output truncated)"
    code = proc.returncode
    if timed_out:
        return False, f"⏱ Stopped after {timeout}s.\n{text}".strip()
    head = "✅ Done" if code == 0 else f"❌ Exit code {code}"
    return code == 0, f"{head}\n{text}".strip() if text else head
