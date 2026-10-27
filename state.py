"""Persistent state and turn log of each agent (agy: state.json and turns.log, Claude Code:
claude-state.json and claude-turns.log, opencode: opencode-…), plus what the agents share (the chat's mode)."""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _data_dir():
    # Outside the install tree, which `hermes plugins update` replaces.
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("alternative-agents")
    except Exception:
        path = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugin-data" / "alternative-agents"
        path.mkdir(parents=True, exist_ok=True)
        return path


DATA_DIR = _data_dir()
MSGS_KEEP = 400
LOG_TAIL_BYTES = 512 * 1024

_lock = threading.Lock()


class Store:
    """One agent's state.json and turns.log."""

    def __init__(self, name, default_model):
        prefix = "" if name == "agy" else f"{name}-"  # agy keeps its pre-Claude file names
        self.state_file = DATA_DIR / f"{prefix}state.json"
        self.turns_log = DATA_DIR / f"{prefix}turns.log"
        self.default_model = default_model
        self.default_voice = "direct"  # default_*: set from the plugin settings at register()
        self.default_voice_reply = "off"
        self.default_verbose = False
        self.default_effort = ""

    def load(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def update(self, **changes):
        """Merge ``changes`` into the state (None deletes a key); returns the new state."""
        with _lock:
            state = self.load()
            for key, value in changes.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
            self._save(state)
            return state

    def _save(self, state):
        tmp = self.state_file.with_suffix(f".{os.getpid()}.tmp")  # task_run.py writes it too
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.replace(self.state_file)

    def model(self, state):
        return state.get("model") or self.default_model

    def effort(self, state):
        return state.get("effort") or self.default_effort

    def voice_mode(self, state):
        return state.get("voice") or self.default_voice

    def voice_reply(self, state):
        return state.get("voice_reply") or self.default_voice_reply

    def verbose(self, state):
        value = state.get("verbose")
        return self.default_verbose if value is None else bool(value)

    def remember_messages(self, ids, key="msgs"):
        """Record Telegram message ids sent by this agent (key: msgs, or btw_msgs for the side
        conversation), so a reply to one of them reaches the same conversation."""
        ids = [str(i) for i in ids if i]
        if not ids:
            return
        with _lock:
            state = self.load()
            known = state.get(key, [])
            state[key] = (known + [i for i in ids if i not in known])[-MSGS_KEEP:]
            self._save(state)

    def log_turn(self, entry):
        entry = {"at": datetime.now().isoformat(timespec="seconds"), **entry}
        try:
            with self.turns_log.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("alternative-agents: cannot write %s", self.turns_log)

    def recent_turns(self):
        """Logged turns, oldest first (only the log's tail is read)."""
        try:
            with self.turns_log.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL_BYTES))
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = raw.splitlines()
        if size > LOG_TAIL_BYTES:
            lines = lines[1:]  # first line is cut
        turns = []
        for line in lines:
            try:
                turns.append(json.loads(line))
            except ValueError:
                continue
        return turns

    def conversations(self, limit=8):
        """Recent conversations, newest first: id, first message, last activity, turn count. Those a
        preset opened are not listed: older logs recorded them, they are deleted once over."""
        convs, opened_by_preset = {}, set()
        for order, turn in enumerate(self.recent_turns()):
            cid = turn.get("conversation_id")
            if not cid or turn.get("btw"):
                continue
            if turn.get("preset"):
                opened_by_preset.add(cid)
            conv = convs.setdefault(cid, {"id": cid, "first": turn.get("message") or "", "turns": 0})
            conv.update(last=turn.get("at") or "", order=order)
            conv["turns"] += 1
        listed = [c for c in convs.values() if c["id"] not in opened_by_preset]
        return sorted(listed, key=lambda c: c["order"], reverse=True)[:limit]

    def recap(self, conversation_id, max_turns, user="User", agent="agy", max_chars=600):
        """The last exchanges of a conversation, to seed a fresh one."""
        turns = [t for t in self.recent_turns() if t.get("conversation_id") == conversation_id and t.get("response")]
        lines = []
        for turn in turns[-max_turns:]:
            lines.append(f"- {user}: {_cut(turn.get('message') or '', max_chars)}")
            lines.append(f"  {agent}: {_cut(turn.get('response') or '', max_chars)}")
        return "\n".join(lines)


def is_message(state, message_id, key="msgs"):
    if message_id is None:
        return False
    # agy_msgs: agy's key before Claude Code joined.
    return str(message_id) in state.get(key, ()) or (key == "msgs" and str(message_id) in state.get("agy_msgs", ()))


def _cut(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
