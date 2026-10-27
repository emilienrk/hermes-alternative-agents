#!/usr/bin/env python3
"""Stand-in for the opencode CLI: emits `opencode run --format json` events (as opencode 1.18.35 does),
steered by mode.txt next to it."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent
MODELS = {"deepseek/deepseek-flash": {"name": "DeepSeek V4.1 Flash", "variants": {"low": {}, "high": {}, "max": {}}},
          "deepseek/deepseek-v4-pro": {"name": "DeepSeek V4 Pro", "variants": {"low": {}, "high": {}, "max": {}}}}


def opt(args, name):
    return args[args.index(name) + 1] if name in args else None


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9")
        return
    if args[:2] == ["models", "deepseek"]:
        for model, info in MODELS.items():
            print(model)
            if "--verbose" in args:
                print(json.dumps({"id": model.split("/")[1], **info}, indent=2))
        return
    if args[:2] == ["session", "delete"]:
        with (HERE / "deleted.txt").open("a") as f:
            f.write(args[2] + "\n")
        return
    if args[:2] == ["debug", "config"]:
        config = Path(os.environ["OPENCODE_CONFIG_DIR"]) / "opencode.json"
        print(config.read_text() if config.exists() else "{}")
        return
    mode = (HERE / "mode.txt").read_text().strip() if (HERE / "mode.txt").exists() else "ok"
    prompt = sys.stdin.read()
    session = opt(args, "--session")
    fork = "--fork" in args
    env = {k: os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "OPENCODE_CONFIG_DIR", "OPENCODE_DISABLE_CLAUDE_CODE",
                                          "OPENCODE_DISABLE_PROJECT_CONFIG")}
    with (HERE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"agent": "opencode", "args": args, "prompt": prompt, "conversation": session, "fork": fork,
                            "env": env}) + "\n")
    if mode == "missing" and session:
        print("\x1b[91m\x1b[1mError: \x1b[0mSession not found", flush=True)
        sys.exit(1)
    sid = f"ses_{uuid.uuid4().hex[:24]}" if fork or not session else session

    def emit(kind, part=None, **extra):
        print(json.dumps({"type": kind, "timestamp": int(time.time() * 1000), "sessionID": sid,
                          **({"part": part} if part is not None else {}), **extra}), flush=True)

    if mode == "crash":
        print("panic: something broke", flush=True)
        sys.exit(2)
    if mode == "balance":
        emit("error", error={"name": "APIError", "data": {"message": "Insufficient Balance", "statusCode": 402,
                                                          "isRetryable": False}})
        sys.exit(1)
    tokens = {"total": 1290, "input": 1234, "output": 56, "reasoning": 0, "cache": {"write": 0, "read": 1000}}
    if mode == "runaway":  # one action and $0.01 per step, for a long while
        for i in range(100):
            emit("step_start", {"type": "step-start"})
            emit("tool_use", {"type": "tool", "tool": "bash", "callID": f"call_{i}", "state": {
                "status": "completed", "input": {"command": f"echo {i}"}, "output": f"{i}\n", "title": "echo"}})
            emit("step_finish", {"type": "step-finish", "reason": "tool-calls", "tokens": tokens, "cost": 0.01})
            time.sleep(0.2)
        return
    emit("step_start", {"type": "step-start"})
    emit("text", {"type": "text", "text": "Looking."})
    time.sleep({"slow": 60, "verbose": 9}.get(mode, 0.2))
    emit("tool_use", {"type": "tool", "tool": "bash", "callID": "call_1", "state": {
        "status": "completed", "input": {"command": f"ls {Path.home()}/projects", "description": "List"},
        "output": "x\n", "title": "ls"}})
    emit("step_finish", {"type": "step-finish", "reason": "tool-calls", "tokens": tokens, "cost": 0.0002})
    emit("step_start", {"type": "step-start"})
    emit("text", {"type": "text", "text": f"See [main.py](file://{Path.home()}/main.py)."})
    emit("text", {"type": "text", "text": "OK" if mode == "quiet" else f"ECHO:{prompt[-200:]}"})
    emit("step_finish", {"type": "step-finish", "reason": "stop", "tokens": tokens, "cost": 0.0003})


if __name__ == "__main__":
    main()
