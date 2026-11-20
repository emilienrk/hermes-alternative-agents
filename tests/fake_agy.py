#!/usr/bin/env python3
"""Stand-in for the agy CLI: emits agy's stream-json events, steered by mode.txt next to it."""

import json
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    if args == ["models"]:
        print("Fetching available models...")
        print("gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)")
        return
    if args[:1] == ["-p=/usage"]:
        print(json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"window": "weekly", "remaining_fraction": 0.73, "reset_time": "2030-01-02T06:13:07Z"},
                {"window": "5h", "remaining_fraction": 0.85, "reset_time": "2030-01-01T20:47:57Z"}]}]}}}))
        return
    if args == ["--version"]:
        print("9.9.9")
        return
    mode = (HERE / "mode.txt").read_text().strip() if (HERE / "mode.txt").exists() else "ok"
    prompt = next(a[3:] for a in args if a.startswith("-p="))
    conv = args[args.index("--conversation") + 1] if "--conversation" in args else None
    with (HERE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "conversation": conv}) + "\n")
    earlier = [json.loads(line)["prompt"] for line in (HERE / "calls.jsonl").read_text().splitlines()][:-1]
    cid = conv or str(uuid.uuid4())
    emit({"event": "init", "conversation_id": cid, "init": {"model": "x", "cwd": str(Path.cwd())}})
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 0, "state": "DONE",
                                                  "step_type": "user_input"}})
    capacity = "API error (attempt 1): UNAVAILABLE (code 503): No capacity available for model x on the server"
    if prompt == "/compact":
        if mode == "saturated":
            emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR",
                                                "error": "API error (attempt 1): UNAVAILABLE (code 503)"}})
            return
        emit({"event": "step_update", "step_update": {
            "conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "checkpoint",
            "usage": {"input_tokens": 60000, "cache_read_tokens": 0}}})
        emit({"event": "step_update", "step_update": {
            "conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "agent_response",
            "text_delta": "\n", "usage": {"input_tokens": 9000, "cache_read_tokens": 0}}})
        # agy reports a compaction alone as failed with the conversation's last error
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR", "response": "",
                                            "error": capacity}})
        return
    if mode == "capacity" and conv and not earlier:
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR", "error": capacity}})
        return
    if mode == "capacity_stale" and conv:
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR", "response": "OK\n",
                                            "error": capacity}})
        return
    if mode == "saturated_compactable" and conv and "/compact" not in earlier:
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR",
                                            "error": "API error (attempt 1): UNAVAILABLE (code 503)"}})
        return
    if mode == "saturated" and conv:
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR",
                                            "error": "API error (attempt 1): UNAVAILABLE (code 503)"}})
        return
    if mode == "blocked" and conv:
        error = "Your previous response was blocked by content safety filters: x"
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR", "response": "OK\n",
                                            "error": error}})
        return
    if mode == "crash":
        print("panic: something broke", flush=True)
        sys.exit(2)
    if mode in ("thought", "thought_glued") and "internal reasoning" not in prompt:
        # Gemini's reasoning in the answer: its own step, then the answer (thought), or both in one.
        leak = "thought\nLet's analyze the audio.\n"
        steps = [leak, "Voici la réponse."] if mode == "thought" else [leak + "Voici la réponse collée."]
        for i, text in enumerate(steps, 1):
            emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": i, "state": "DONE",
                                                          "step_type": "agent_response", "text_delta": text}})
        emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS",
                                            "response": "".join(steps)}})
        return
    if mode == "thought_glued":
        emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS",
                                            "response": "Voici la réponse seule."}})
        return
    size = 450000 if mode == "big" else 20000
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE",
                                                  "step_type": "agent_response", "text_delta": "Looking.\n",
                                                  "usage": {"input_tokens": size // 3, "cache_read_tokens": size - size // 3}}})
    emit({"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": f"ls {Path.home()}/projects"}}}})
    time.sleep({"slow": 60, "verbose": 9}.get(mode, 0.2))
    emit({"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": f"ls {Path.home()}/projects"}}}})
    response = f"ECHO:{prompt[-200:]}"
    if mode == "media":
        response += "\n" + (HERE / "media.txt").read_text()
    if mode == "long":
        response = "x" * 15000
    if mode == "silent":
        emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "[SILENT]"}})
        return
    emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS",
                                        "response": f"See [main.py](file://{Path.home()}/main.py).\n{response}",
                                        "usage": {"total_tokens": 54321}}})


if __name__ == "__main__":
    main()
