#!/usr/bin/env python3
"""Stand-in for the Claude Code CLI: emits Claude Code's stream-json events, steered by mode.txt next to it."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent


def emit(event):
    print(json.dumps(event), flush=True)


def opt(args, name):
    return args[args.index(name) + 1] if name in args else None


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9 (Claude Code)")
        return
    if args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}))
        return
    mode = (HERE / "mode.txt").read_text().strip() if (HERE / "mode.txt").exists() else "ok"
    prompt = sys.stdin.read()
    resume = opt(args, "--resume")
    fork = "--fork-session" in args
    env = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_DISABLE_AUTO_MEMORY")}
    with (HERE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"agent": "claude", "args": args, "prompt": prompt, "conversation": resume, "fork": fork,
                            "env": env}) + "\n")
    if mode == "missing" and resume:
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
              "errors": [f"No conversation found with session ID: {resume}"]})
        sys.exit(1)
    sid = str(uuid.uuid4()) if fork or not resume else resume
    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": str(Path.cwd()),
          "model": opt(args, "--model")})
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.42, "resetsAt": 1893456000},
                                                "seven_day": {"utilization": 0.32, "resetsAt": 1893800000}}}})
    if mode == "crash":
        print("panic: something broke", flush=True)
        sys.exit(2)
    if mode == "quota":
        emit({"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
              "result": "You've hit your limit · resets 3pm"})
        return
    emit({"type": "stream_event", "event": {"type": "message_start"}})
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "Je regarde.\n"}}})
    command = f"ls {Path.home()}/projects"
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Je regarde."},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command, "description": "List"}}]}})
    time.sleep({"slow": 60, "verbose": 9}.get(mode, 0.2))
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]}})
    response = f"ECHO:{prompt[-200:]}"
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": 0.25,
          "result": f"Voir [main.py](file://{Path.home()}/main.py).\n{response}",
          "usage": {"input_tokens": 4, "output_tokens": 300, "cache_creation_input_tokens": 700,
                    "cache_read_input_tokens": 50000}})


if __name__ == "__main__":
    main()
