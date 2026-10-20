"""Run one scheduled task unattended and print the agent's answer (Hermes' cron delivers stdout as is).

Usage: python task_run.py '<task json>'  (written by tasks.py into the task's cron script)

The plugin is loaded by path and configured from the settings Hermes keeps in config.yaml, so a
task runs its agent exactly as a Telegram turn would, minus the chat. A task's context_command runs
first and its output opens the prompt, like a Hermes cron job's script. An answer that is a silence
marker ([SILENT]) prints nothing: Hermes' cron delivers no empty output.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
# Tasks created before Claude Code joined carried these settings themselves.
_LEGACY_KEYS = {"agy_bin": "agy_path", "workspace": "workspace", "timeout_minutes": "timeout_minutes",
                "user": "user_name", "mark": "mark"}
CONTEXT_TIMEOUT_S = 30 * 60
CONTEXT_MAX_CHARS = 40_000


def _load_plugin():
    src = HERMES_HOME / "hermes-agent"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    spec = importlib.util.spec_from_file_location("alternative_agents_task", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings(task):
    import yaml
    try:
        config = yaml.safe_load((HERMES_HOME / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        config = {}
    settings = dict((((config.get("plugins") or {}).get("entries") or {}).get("alternative-agents") or {}).get("settings") or {})
    settings.update({new: task[old] for old, new in _LEGACY_KEYS.items() if task.get(old)})
    settings.update(task.get("settings") or {})
    settings["backends"] = [task.get("backend") or "agy"]  # this task's agent, whatever the chat enables
    return settings


def _context(command):
    """The context command's output, for the prompt (its failure is context too)."""
    try:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                              timeout=CONTEXT_TIMEOUT_S, cwd=str(Path.home()))
        output, code = (proc.stdout + proc.stderr).strip(), proc.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        output, code = str(e), "?"
    return (f"Output of `{' '.join(command)}` (exit {code}), gathered just before this task:\n"
            f"{output[-CONTEXT_MAX_CHARS:] or '(none)'}")


def _silent(response):
    try:
        from gateway.response_filters import is_autonomous_silence_response
        return is_autonomous_silence_response(response)
    except Exception:
        return response.strip() == "[SILENT]"


def main():
    task = json.loads(sys.argv[1])
    plugin = _load_plugin()
    settings = _settings(task)
    plugin.configure(lambda key, default=None: settings.get(key, default))
    engine = plugin.engine
    agent = next(iter(engine.AGENTS.values()))
    backend, store = agent.backend, agent.store
    state = store.load()
    # side: the task's conversation must not become the chat's current one.
    run = engine.Run([], task.get("model") or store.model(state), store.effort(state), side=True)
    backend.workspace.prepare()
    context = f"{_context(task['context_command'])}\n\n" if task.get("context_command") else ""
    message = (f"(Scheduled task \"{task['name']}\": you run unattended, nobody can answer a question. "
               f"Your final answer is sent to {plugin.workspace.USER} on Telegram as is: make it the deliverable, "
               f"not an account of your work.)\n\n{context}{task['prompt']}")
    prompt = backend.prompt(run, message, None)
    error = ""
    try:
        asyncio.run(agent._exec(run, prompt, None))
    except OSError as e:
        error = str(e)
    backend.finished(run)
    result = run.result or {}
    response = result.get("response") or ""
    store.log_turn({"task": task["name"], "model": run.model, "conversation_id": run.conversation_id,
                    "message": task["prompt"][:500], "response": response[:2000],
                    "status": result.get("status", "NO_RESULT"), "seconds": int(time.time() - run.started),
                    "tokens": run.tokens, **({"cost_usd": run.cost} if run.cost else {})})
    head = f"{agent.mark} {agent.label} · {task['name']}".strip()
    if run.failed():
        print(f"{head}\n\n❌ {error or run.error_text() or 'no output'}"[:3500])
        return 1
    if not _silent(response):
        print(f"{head}\n\n{engine.clean(response)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
