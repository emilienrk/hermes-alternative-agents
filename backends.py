"""The agents behind the chat: how to run each CLI, read its stream-json events and explain its
failures. engine.py runs turns the same way whichever agent answers."""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import workspace
from .config import EFFORTS, cfg
from .state import Store

logger = logging.getLogger(__name__)

MAX_ARG_PROMPT = 100_000  # Linux caps one argv string at 128 KiB
_PARAM_KEYS = ("CommandLine", "AbsolutePath", "TargetFile", "Url", "Query", "SearchPath",
               "DirectoryPath", "Pattern", "Path")
_OC_PARAM_KEYS = ("command", "filePath", "url", "pattern", "path", "name", "description", "prompt")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CLAUDE_PARAM_KEYS = ("command", "file_path", "url", "query", "pattern", "path", "skill", "description", "prompt")


class Backend:
    name = prefix = label = product = ""
    default_model = ""
    can_hear = True  # takes voice notes as audio
    can_fork = False  # a btw can start from a copy of the main conversation
    restart_notice = ""
    default_mark = ""

    def __init__(self):
        self.store = Store(self.name, self.default_model)
        self.bin = self.name
        self.mark = self.default_mark
        self.workspace = None

    def configure(self):
        self.store.default_voice = cfg["voice_input"] if self.can_hear else "local"
        self.store.default_voice_reply = cfg["voice_reply"]
        self.store.default_verbose = bool(cfg["verbose"])

    def _find_bin(self, setting, fallback):
        return os.path.expanduser(setting) if setting else (shutil.which(self.name) or str(fallback))

    def task_settings(self):
        """What a scheduled task needs besides the plugin settings (task_run.py reads those)."""
        return {"backend": self.name, "chat_id": cfg["chat_id"], "mark": self.mark, "label": self.label,
                "prefix": self.prefix}

    def check_model(self, choice):
        """(model id, warning) or (None, error) for a /x_model argument."""
        names = [name for name, _ in self.models()]
        if not names:
            return choice, "⚠️ List unavailable: name not checked."
        return (choice, "") if choice in names else (None, f"Unknown model: {choice}")

    def finished(self, run):
        """Called after the turn, before its answer is sent."""

    def unavailable(self):
        """Why no turn can start (e.g. a missing key), or ""; checked before each run."""
        return ""

    def _with_context(self, run, message, conv):
        """The message after the context block; blocking (reads Hermes' state)."""
        state = self.store.load()
        seen = state.get("context") if conv and (state.get("context") or {}).get("conversation_id") == conv else None
        block, run.context = self.workspace.turn_context(seen)
        return f"{block}\n\n{message}"

    def _context_changes(self, run):
        """The state change recording what the context block told this conversation."""
        if run.context is not None and run.conversation_id and not run.side and not run.failed():
            return {"context": {**run.context, "conversation_id": run.conversation_id}}
        return {}


class Agy(Backend):
    name, prefix, label, product = "agy", "agy", "agy", "Antigravity"
    default_model = "gemini-3.8-flash-high"
    default_mark = "🛸"
    restart_notice = "Conversation saturated (503)"
    MODELS_TTL_S = 3600
    TOKEN_FILE = Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token"
    SETTINGS = Path.home() / ".gemini/antigravity-cli/settings.json"
    # agy encodes the reasoning effort in the model id (gemini-3.8-flash-high); its --effort flag
    # conflicts with those ids, so the effort switches to a sibling variant instead.
    _EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
    # A long conversation ends in a permanent 503 on every model, while a fresh one answers
    # (measured 2026-10-01).
    _SATURATED = re.compile(r"UNAVAILABLE|code 503")
    # Two agy processes sharing the OAuth token (e.g. another account on the machine): a
    # simultaneous refresh fails one of them once.
    _TRANSIENT = re.compile(r"not logged into Antigravity|INTERNAL \(code 500\)", re.I)
    _QUOTA = re.compile(r"RESOURCE_EXHAUSTED|quota|rate.?limit|code 429", re.I)
    _WINDOWS = {"weekly": "week", "5h": "5 h"}

    def __init__(self):
        super().__init__()
        self._models_cache = {"at": 0.0, "models": []}

    def configure(self):
        super().configure()
        self.bin = self._find_bin(cfg["agy_path"], Path.home() / ".local/bin/agy")
        self.mark = str(cfg["mark"] or "")
        self.store.default_model = cfg["default_model"] or self.default_model
        self.workspace = workspace.AgyWorkspace(self, cfg["workspace"] or "~/agy-chat")

    # ── a turn ──

    def prompt(self, run, message, conv):
        return message if conv else f"{self.workspace.first_turn_preamble()}\n\n{message}"

    def command(self, run, prompt, conv, fork=False):
        if len(prompt) > MAX_ARG_PROMPT:
            path = self.workspace.inbox / f"{datetime.now():%Y%m%d-%H%M%S}-message.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompt)
            prompt = f"{workspace.USER}'s message is too long for the command line: read {path} and answer it."
        cmd = [self.bin, "--dangerously-skip-permissions", "--model", run.model,
               "--print-timeout", f"{cfg['timeout_minutes']}m", f"-p={prompt}", "--output-format", "stream-json"]
        if conv:
            cmd[1:1] = ["--conversation", conv]
        return cmd, None

    def env(self, base):
        return base

    def on_event(self, run, event):
        kind = event.get("event")
        if kind == "init" and event.get("conversation_id"):
            run.conversation_id = event["conversation_id"]
        elif kind == "step_update":
            self._track_step(run, event.get("step_update") or {})
        elif kind == "result":
            run.result = event.get("result") or {}
            run.tokens = int(((run.result.get("usage") or {}).get("total_tokens")) or 0)

    def _track_step(self, run, step):
        kind, status = step.get("step_type"), step.get("state")
        if kind == "tool":
            info = step.get("tool_info") or {}
            desc = _describe(info.get("name") or step.get("tool_name"), info.get("parameters") or {}, _PARAM_KEYS)
            if status == "ACTIVE":
                run.current = desc
            elif status in ("DONE", "ERROR"):
                run.actions.append(f"{'✓' if status == 'DONE' else '✗'} {desc}")
                run.current = None
            run.changed = True
        elif kind == "agent_response" and step.get("text_delta"):
            index = step.get("step_index")
            run.text[index] = run.text.get(index, "") + step["text_delta"]
            run.last_step = index
            run.changed = True

    def restart_fresh(self, error):
        return bool(self._SATURATED.search(error))

    def transient(self, error):
        return bool(self._TRANSIENT.search(error))

    def hint(self, error):
        if self._QUOTA.search(error):
            return "Quota reached on this model? /agy_model to switch."
        if self._SATURATED.search(error):
            return "Conversation too heavy? /agy_new then /agy_retry."
        if self._TRANSIENT.search(error):
            return "Antigravity session to reconnect? Run `agy` on the machine (e.g. over SSH)."
        return ""

    # ── models ──

    def models(self):
        """[(id, label)] from `agy models`, cached for an hour."""
        cache = self._models_cache
        if time.time() - cache["at"] < self.MODELS_TTL_S and cache["models"]:
            return cache["models"]
        try:
            out = subprocess.run([self.bin, "models"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return cache["models"]
        models = [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]
        if models:
            cache.update(at=time.time(), models=models)
        return models

    def short_model(self, label):
        return label.replace("Gemini ", "").replace(" (Thinking)", "").replace("Claude ", "")

    def efforts(self, state):
        """(current effort, {effort: state change}) — {} when the model has no effort variants."""
        model = self.store.model(state)
        if not self._EFFORT_SUFFIX.search(model):
            return "", {}
        family = self._EFFORT_SUFFIX.sub("", model)
        variants = {m[len(family) + 1:]: {"model": m} for m, _ in self.models()
                    if self._EFFORT_SUFFIX.sub("", m) == family and m != family}
        return self._EFFORT_SUFFIX.search(model).group(1), variants

    def side_settings(self, state):
        """(model, effort) for a btw: the current model's low-effort variant, a side question wants speed."""
        _current, variants = self.efforts(state)
        return (variants.get("low") or {}).get("model") or self.store.model(state), ""

    # ── usage and checks ──

    def quota(self):
        """The subscription's remaining quota, as `agy -p /usage` reports it (costs nothing)."""
        try:
            self.workspace.root.mkdir(parents=True, exist_ok=True)
            out = subprocess.run([self.bin, "-p=/usage", "--output-format", "json"], capture_output=True,
                                 text=True, timeout=30, cwd=str(self.workspace.root), stdin=subprocess.DEVNULL)
            groups = json.loads(out.stdout)["command"]["data"]["groups"]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            logger.warning("alternative-agents: agy /usage failed", exc_info=True)
            return "Antigravity quota: unavailable (`agy -p /usage` failed)"
        lines = []
        for group in groups:
            buckets = [f"{self._WINDOWS.get(b.get('window'), b.get('window'))} "
                       f"{round(100 * float(b.get('remaining_fraction', 0)))}% (reset {_local(b.get('reset_time'))})"
                       for b in group.get("buckets", [])]
            lines.append(f"{group.get('name', '?')}: " + " · ".join(buckets))
        return "Antigravity quota left:\n" + "\n".join(lines)

    def checks(self):
        try:
            out = subprocess.run([self.bin, "--version"], capture_output=True, text=True, timeout=20)
            lines = [f"✓ agy {out.stdout.strip() or '?'}"]
        except (OSError, subprocess.TimeoutExpired) as e:
            lines = [f"✗ agy missing or silent ({self.bin}): {e}"]
        try:
            age_h = (time.time() - self.TOKEN_FILE.stat().st_mtime) / 3600
            lines.append(f"✓ Antigravity token refreshed {age_h:.0f} h ago")
        except OSError:
            lines.append("✗ no Antigravity token: run `agy` once on the machine to log in")
        try:
            trusted = json.loads(self.SETTINGS.read_text()).get("trustedWorkspaces", [])
        except (OSError, ValueError):
            return lines + [f"✗ cannot read agy's {self.SETTINGS}"]
        root = self.workspace.root
        lines.append(f"✓ {root} is a trusted workspace for agy" if str(root) in trusted
                     else f"✗ {root} missing from agy's trustedWorkspaces ({self.SETTINGS})")
        return lines


class Claude(Backend):
    name, prefix, label, product = "claude", "cc", "Claude", "Claude Code"
    default_model = "opus"
    default_mark = "✳️"
    can_hear = False
    can_fork = True
    restart_notice = "Conversation lost or too long"
    MODELS = (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"))
    # Interactive or session-bound tools: nobody answers mid-turn, and the process ends with the turn.
    DISALLOWED = ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "CronCreate", "CronDelete", "CronList",
                  "ScheduleWakeup", "RemoteTrigger", "PushNotification")
    # The user's own settings are not loaded (--setting-sources ""): commits stay theirs.
    SETTINGS = {"attribution": {"commit": "", "pr": ""}}
    _RESTART = re.compile(r"No conversation found with session ID|prompt is too long", re.I)
    _TRANSIENT = re.compile(r"overloaded|\b529\b|API Error: 5\d\d|ECONNRESET|socket hang up|fetch failed", re.I)
    _QUOTA = re.compile(r"usage limit|limit reached|hit your limit|rate.?limit|\b429\b", re.I)
    _AUTH = re.compile(r"/login|not logged in|invalid api key|OAuth|authentication|\b401\b", re.I)
    _WINDOWS = {"five_hour": "5 h", "seven_day": "week"}

    def configure(self):
        super().configure()
        self.bin = self._find_bin(cfg["claude_path"], Path.home() / ".local/bin/claude")
        self.mark = str(cfg["claude_mark"] or "")
        self.store.default_model = cfg["claude_model"] or self.default_model
        self.store.default_effort = cfg["claude_effort"]
        self.workspace = workspace.ClaudeWorkspace(self, cfg["claude_workspace"] or "~/claude-chat")

    # ── a turn ──

    def prompt(self, run, message, conv):
        return self._with_context(run, message, conv)

    def command(self, run, prompt, conv, fork=False):
        ws = self.workspace
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", run.model, "--permission-mode", "bypassPermissions",
               "--setting-sources", "", "--settings", json.dumps(self.SETTINGS),
               "--append-system-prompt-file", str(ws.system_prompt), "--plugin-dir", str(ws.plugin),
               "--strict-mcp-config", "--mcp-config", str(ws.mcp_config),
               "--disallowedTools", ",".join(self.DISALLOWED)]
        if run.effort:
            cmd += ["--effort", run.effort]
        if conv:
            cmd += ["--resume", conv] + (["--fork-session"] if fork else [])
        return cmd, prompt

    def env(self, base):
        # The subscription pays, never an API key; Hermes' memory is the shared one.
        env = {k: v for k, v in base.items() if not k.startswith("ANTHROPIC_") and k != "CLAUDECODE"}
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        return env

    def on_event(self, run, event):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
            run.conversation_id = event["session_id"]
        elif kind == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "message_start":
                run.step += 1
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and not event.get("parent_tool_use_id"):
                run.text[run.step] = run.text.get(run.step, "") + delta.get("text", "")
                run.last_step = run.step
                run.changed = True
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or ():
                if block.get("type") == "tool_use":
                    desc = _describe(block.get("name"), block.get("input") or {}, _CLAUDE_PARAM_KEYS)
                    run.tools[block.get("id")] = desc
                    run.current = desc
                    run.changed = True
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else ():
                if block.get("type") == "tool_result" and block.get("tool_use_id") in run.tools:
                    desc = run.tools.pop(block["tool_use_id"])
                    run.actions.append(f"{'✗' if block.get('is_error') else '✓'} {desc}")
                    run.current = None
                    run.changed = True
        elif kind == "rate_limit_event":
            run.rate_limit = event.get("rate_limit_info") or {}
        elif kind == "result":
            failed = bool(event.get("is_error")) or event.get("subtype") != "success"
            response = event.get("result") or ""
            errors = "; ".join(str(e) for e in event.get("errors") or ())
            usage = event.get("usage") or {}
            run.tokens = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            run.cost = float(event.get("total_cost_usd") or 0)
            run.result = {"status": "ERROR" if failed else "SUCCESS", "response": "" if failed else response,
                          "error": errors or response or event.get("subtype") or "", "usage": usage}

    def finished(self, run):
        changes = self._context_changes(run)
        if run.rate_limit:
            changes["rate_limit"] = {**run.rate_limit, "seen_at": time.time()}
        if changes:
            self.store.update(**changes)

    def restart_fresh(self, error):
        return bool(self._RESTART.search(error))

    def transient(self, error):
        return bool(self._TRANSIENT.search(error)) and not self._QUOTA.search(error)

    def hint(self, error):
        if self._QUOTA.search(error):
            return "Claude subscription limit reached? /cc_usage shows when it resets."
        if self._AUTH.search(error):
            return "Claude Code session to reconnect? Run `claude` on the machine (e.g. over SSH), then /login."
        if self._TRANSIENT.search(error):
            return "Anthropic's API is overloaded: /cc_retry in a moment."
        return ""

    # ── models ──

    def models(self):
        return list(self.MODELS)

    def short_model(self, label):
        return label

    def check_model(self, choice):
        # Aliases follow the latest model of each family; a full id pins one.
        if choice in dict(self.MODELS) or choice.startswith("claude-"):
            return choice, ""
        return None, f"Unknown model: {choice} (an alias, or a full id such as claude-opus-5-5)"

    def efforts(self, state):
        return self.store.effort(state) or "default", {level: {"effort": level} for level in EFFORTS}

    def side_settings(self, state):
        return self.store.model(state), "low"

    # ── usage and checks ──

    def quota(self):
        """The subscription's usage, as the last turn's rate-limit event reported it."""
        info = self.store.load().get("rate_limit")
        if not info:
            return "Claude subscription usage: unknown until the next turn."
        windows = info.get("unifiedWindows") or {}
        parts = [f"{self._WINDOWS.get(name, name)} {round(100 * (1 - float(w.get('utilization') or 0)))}% "
                 f"(reset {_local_ts(w.get('resetsAt'))})" for name, w in windows.items()]
        status = "" if info.get("status") in (None, "allowed") else f" · {info['status']}"
        seen = datetime.fromtimestamp(float(info.get("seen_at") or 0), workspace.local_tz())
        return (f"Claude subscription left (as of {seen:%H:%M}{status}):\n"
                + (" · ".join(parts) or "no window reported"))

    def checks(self):
        try:
            out = subprocess.run([self.bin, "--version"], capture_output=True, text=True, timeout=20)
            lines = [f"✓ Claude Code {out.stdout.strip() or '?'}"]
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"✗ claude missing or silent ({self.bin}): {e}"]
        try:
            out = subprocess.run([self.bin, "auth", "status"], capture_output=True, text=True, timeout=20,
                                 env=self.env(dict(os.environ)))
            auth = json.loads(out.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return lines + ["✗ `claude auth status` failed"]
        if not auth.get("loggedIn"):
            return lines + ["✗ Claude Code not logged in: run `claude` once on the machine, then /login"]
        lines.append(f"✓ Claude Code logged in ({auth.get('authMethod')}, {auth.get('subscriptionType') or '?'} plan)")
        return lines


class OpenCode(Backend):
    """opencode on DeepSeek, with a key of its own (never Hermes'): the env file's DEEPSEEK_API_KEY
    reaches opencode through its environment only."""
    name, prefix, label, product = "opencode", "oc", "opencode", "DeepSeek"
    default_model = "deepseek/deepseek-flash"
    default_mark = "🐋"
    can_hear = False
    can_fork = True
    restart_notice = "Conversation lost"
    PROVIDER = "deepseek"
    BALANCE_URL = "https://api.deepseek.com/user/balance"
    MODELS_TTL_S = 3600
    # Kept away from the user's own opencode and Claude Code setup (config, AGENTS.md, CLAUDE.md, skills).
    ISOLATION = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                 "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                 "OPENCODE_DISABLE_SHARE": "1"}
    _RESTART = re.compile(r"Session not found", re.I)
    _TRANSIENT = re.compile(r"\b50[0-4]\b|overloaded|ECONNRESET|socket hang up|fetch failed|timed? ?out", re.I)
    _BALANCE = re.compile(r"Insufficient Balance|\b402\b", re.I)
    _AUTH = re.compile(r"\b401\b|Authentication|invalid api key|API key", re.I)
    _QUOTA = re.compile(r"rate.?limit|\b429\b", re.I)

    def __init__(self):
        super().__init__()
        self._models_cache = {"at": 0.0, "models": [], "variants": {}}
        self.env_file = Path()

    def configure(self):
        super().configure()
        self.bin = self._find_bin(cfg["opencode_path"], Path.home() / ".local/bin/opencode")
        self.mark = str(cfg["opencode_mark"] or "")
        self.env_file = Path(os.path.expanduser(str(cfg["opencode_env_file"] or "")))
        self.store.default_model = self._full_id(cfg["opencode_model"] or self.default_model)
        self.store.default_effort = cfg["opencode_effort"]
        self.workspace = workspace.OpenCodeWorkspace(self, cfg["opencode_workspace"] or "~/opencode-chat")

    def api_key(self):
        """DEEPSEEK_API_KEY from the plugin's env file ("" when missing)."""
        try:
            lines = self.env_file.read_text().splitlines()
        except OSError:
            return ""
        for line in lines:
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip("'\"")
        return ""

    def unavailable(self):
        if not self.env_file.is_file():
            return f"No DeepSeek key: {_home(str(self.env_file))} is missing (DEEPSEEK_API_KEY=…, mode 600)."
        if not self.api_key():
            return f"No DeepSeek key: DEEPSEEK_API_KEY is missing from {_home(str(self.env_file))}."
        return ""

    # ── a turn ──

    def prompt(self, run, message, conv):
        return self._with_context(run, message, conv)

    def command(self, run, prompt, conv, fork=False):
        cmd = [self.bin, "run", "--format", "json", "--dir", str(self.workspace.root), "--model", run.model, "--auto"]
        if run.effort:
            cmd += ["--variant", run.effort]
        if conv:
            cmd += ["--session", conv] + (["--fork"] if fork else [])
        else:
            # A title of its own: opencode would otherwise ask the model for one, from the message.
            cmd += ["--title", f"Telegram {datetime.now():%Y-%m-%d %H:%M}"]
        return cmd, prompt  # through stdin: no argv size limit, nothing in `ps`

    def env(self, base):
        env = {k: v for k, v in base.items() if not k.startswith(("DEEPSEEK_", "OPENCODE_"))}
        env.update(self.ISOLATION, OPENCODE_CONFIG_DIR=str(self.workspace.config_dir))
        key = self.api_key()
        if key:
            env["DEEPSEEK_API_KEY"] = key
        return env

    def on_event(self, run, event):
        kind, part = event.get("type"), event.get("part") or {}
        if event.get("sessionID") and not run.conversation_id:
            run.conversation_id = event["sessionID"]
        if kind == "step_start":
            run.step += 1
        elif kind == "text" and part.get("text"):
            # A text part arrives whole once written; a step may hold several.
            said = run.text.get(run.step, "")
            run.text[run.step] = f"{said}\n\n{part['text']}" if said else part["text"]
            run.last_step = run.step
            run.changed = True
        elif kind == "tool_use":
            state = part.get("state") or {}
            desc = _describe(part.get("tool"), state.get("input") or {}, _OC_PARAM_KEYS)
            run.actions.append(f"{'✗' if state.get('status') == 'error' else '✓'} {desc}")
            run.changed = True
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            run.tokens += int(tokens.get("total") or 0)
            run.cost += float(part.get("cost") or 0)
            if part.get("reason") != "tool-calls":
                run.result = {"status": "SUCCESS", "response": run.text.get(run.step, "").strip(), "error": "",
                              "usage": tokens}
        elif kind == "error":
            error = event.get("error") or {}
            data = error.get("data") or {}
            text = str(data.get("message") or error.get("name") or "error")
            if data.get("statusCode"):
                text += f" (HTTP {data['statusCode']})"
            run.result = {"status": "ERROR", "response": "", "error": text}

    def finished(self, run):
        changes = self._context_changes(run)
        if changes:
            self.store.update(**changes)

    def restart_fresh(self, error):
        return bool(self._RESTART.search(error))

    def transient(self, error):
        return bool(self._TRANSIENT.search(error)) and not self._BALANCE.search(error)

    def hint(self, error):
        if self._BALANCE.search(error):
            return "DeepSeek balance used up? /oc_usage shows it; top it up on platform.deepseek.com."
        if self._AUTH.search(error):
            return f"DeepSeek key refused: check DEEPSEEK_API_KEY in {_home(str(self.env_file))}."
        if self._QUOTA.search(error):
            return "DeepSeek rate limit: /oc_retry in a moment."
        if self._TRANSIENT.search(error):
            return "DeepSeek's API is struggling: /oc_retry in a moment."
        return ""

    # ── models ──

    def _full_id(self, model):
        return model if "/" in model else f"{self.PROVIDER}/{model}"

    def _catalog(self):
        """(models, {model: [variants]}) from `opencode models deepseek --verbose`, cached for an hour."""
        cache = self._models_cache
        if time.time() - cache["at"] < self.MODELS_TTL_S and cache["models"]:
            return cache["models"], cache["variants"]
        try:
            out = subprocess.run([self.bin, "models", self.PROVIDER, "--verbose"], capture_output=True, text=True,
                                 timeout=60, stdin=subprocess.DEVNULL, env=self.env(dict(os.environ))).stdout
        except (OSError, subprocess.TimeoutExpired):
            return cache["models"], cache["variants"]
        models, variants, decoder, pos = [], {}, json.JSONDecoder(), 0
        while True:  # "<provider>/<model>" lines, each followed by its JSON description
            line_end = out.find("\n", pos)
            if line_end == -1:
                break
            model, pos = out[pos:line_end].strip(), line_end + 1
            if not model:
                continue
            try:
                info, pos = decoder.raw_decode(out, pos)
            except ValueError:
                break
            models.append((model, str(info.get("name") or model)))
            variants[model] = list(info.get("variants") or {})
        if models:
            cache.update(at=time.time(), models=models, variants=variants)
        return models, variants

    def models(self):
        return self._catalog()[0]

    def short_model(self, label):
        return label.replace("DeepSeek ", "")

    def check_model(self, choice):
        return super().check_model(self._full_id(choice))

    def efforts(self, state):
        levels = self._catalog()[1].get(self.store.model(state)) or []
        return self.store.effort(state) or "default", {level: {"effort": level} for level in levels}

    def side_settings(self, state):
        _current, levels = self.efforts(state)
        return self.store.model(state), "low" if "low" in levels else ""

    # ── usage and checks ──

    def balance(self):
        """{currency: total} left on the DeepSeek account; raises on failure."""
        import urllib.request
        request = urllib.request.Request(self.BALANCE_URL, headers={"Authorization": f"Bearer {self.api_key()}",
                                                                    "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
        return {b.get("currency", "?"): b.get("total_balance", "?") for b in data.get("balance_infos") or ()}

    def quota(self):
        reason = self.unavailable()
        if reason:
            return f"DeepSeek balance: unknown. {reason}"
        try:
            left = self.balance()
        except Exception as e:
            return f"DeepSeek balance: unavailable ({type(e).__name__})"
        return "DeepSeek balance: " + (" · ".join(f"{total} {currency}" for currency, total in left.items()) or "?")

    def checks(self):
        try:
            out = subprocess.run([self.bin, "--version"], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL)
            lines = [f"✓ opencode {out.stdout.strip() or '?'}"]
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"✗ opencode missing or silent ({self.bin}): {e}"]
        reason = self.unavailable()
        if reason:
            return lines + [f"✗ {reason}"]
        try:
            mode = self.env_file.stat().st_mode & 0o777
        except OSError:
            mode = 0
        lines.append(f"✓ DeepSeek key in {_home(str(self.env_file))}"
                     + ("" if mode & 0o077 == 0 else f" (⚠️ mode {oct(mode)[2:]}: chmod 600 it)"))
        try:
            left = self.balance()
            empty = not any(_positive(total) for total in left.values())
            lines.append(("✗ DeepSeek balance used up: " if empty else "✓ DeepSeek balance: ")
                         + " · ".join(f"{total} {currency}" for currency, total in left.items()))
        except Exception as e:
            lines.append(f"✗ DeepSeek balance unavailable ({type(e).__name__}: {e})")
        self.workspace.root.mkdir(parents=True, exist_ok=True)
        try:
            out = subprocess.run([self.bin, "debug", "config"], capture_output=True, text=True, timeout=60,
                                 cwd=str(self.workspace.root), stdin=subprocess.DEVNULL, env=self.env(dict(os.environ)))
        except (OSError, subprocess.TimeoutExpired) as e:
            return lines + [f"✗ `opencode debug config` failed: {e}"]
        try:
            json.loads(out.stdout)
        except ValueError:
            return lines + [f"✗ opencode rejects its config: {_cut(_ANSI.sub('', out.stderr or out.stdout), 300)}"]
        return lines + [f"✓ opencode config valid ({_home(str(self.workspace.config_file))})"]


def make(names):
    """{name: backend} for the enabled backends, configured."""
    out = {}
    for name in names:
        backend = {"agy": Agy, "claude": Claude, "opencode": OpenCode}[name]()
        backend.configure()
        out[name] = backend
    return out


def _describe(name, params, keys):
    arg = next((params[k] for k in keys if isinstance(params.get(k), str)), None)
    if arg is None:
        arg = next((v for v in params.values() if isinstance(v, str)), "")
    return f"{name or '?'} {_cut(_home(arg), 80)}".rstrip()


def _local(iso):
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    return _short_time(when)


def _local_ts(ts):
    try:
        return _short_time(datetime.fromtimestamp(float(ts), timezone.utc))
    except (TypeError, ValueError, OSError):
        return "?"


def _short_time(when):
    when = when.astimezone(workspace.local_tz())
    return f"{when:%H:%M}" if when.date() == workspace.local_now().date() else f"{when:%a %H:%M}"


def _positive(amount):
    try:
        return float(amount) > 0
    except (TypeError, ValueError):
        return False


def _home(text):
    return text.replace(str(Path.home()), "~")


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
