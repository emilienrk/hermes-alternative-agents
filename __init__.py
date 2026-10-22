"""Talk to Antigravity (agy), Claude Code or opencode (DeepSeek) from Telegram: alternatives to Hermes' LLM.

What reaches an agent (the configured chat only), with /agy… for agy, /cc… for Claude Code and /oc…
for opencode:
  a reply            to one of the agent's messages
  everything         while its mode is on (/agy_on, /cc_on … /agy_off, /cc_off, shown by a pinned
                     banner; one mode at a time); slash commands and messages starting with
                     "Hermes" still go to Hermes, /stop and /new act on the agent
  /agy <message>     the explicit form (/cc <message>)
  /<preset>          a new conversation whose first message is a saved prompt (presets.py), then that
                     agent's mode; /preset lists, saves (in reply to a message) and removes presets
Photos, files and voice notes ride along; voice notes are transcribed locally first when the
agent can't hear them.

Routed messages are taken in the pre_gateway_dispatch hook, before Hermes spends a token.
engine.py runs the turns (typing indicator, live status in verbose mode, queue for messages
sent meanwhile), backends.py knows each CLI, workspace.py gives the agents what Hermes knows
(context, skills, MCP servers) and hermes_mcp.py serves the Hermes tools they lack.

The agents run as the gateway's user without permission prompts: whoever can write in the
configured chat can run anything that user can. Without a chat_id setting, nothing is routed.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from functools import partial
from itertools import zip_longest
from pathlib import Path

from . import backends, config, engine, presets, workspace

logger = logging.getLogger(__name__)

INLINE_DOC_MAX_BYTES = 100 * 1024
_SLASH = re.compile(r"^/([A-Za-z0-9_-]+)(?:@\w+)?(?=\s|$)")
_HERMES_WORD = re.compile(r"^\s*herm[eè]s(?=$|[\s,:;.!?])", re.I)

HANDOFF_THREAD_MESSAGES = 30
HANDBACK_TURNS = 8

COMMANDS = (  # (suffix, description, args hint); {label} is the agent's
    ("", "{label}: status, or a message for it", "[message]"),
    ("on", "{label} mode: everything goes to {label}", ""),
    ("off", "End {label} mode, back to Hermes", ""),
    ("new", "New {label} conversation", "[message]"),
    ("stop", "Stop {label} and drop its queue", ""),
    ("model", "Choose {label}'s model", "[number or name]"),
    ("effort", "{label}'s reasoning effort", "[low|medium|high…]"),
    ("verbose", "Show/hide {label}'s actions", "[on|off]"),
    ("voice", "Voice notes in, and voice replies", "[direct|local|reply off|mirror|always]"),
    ("conv", "Resume a {label} conversation", "[number]"),
    ("retry", "Send my last message to {label} again", ""),
    ("btw", "Side question, answered now, next to the main conversation", "<question>"),
    ("handoff", "Hand the Hermes conversation over to {label}", "[message]"),
    ("back", "Hand the {label} conversation back to Hermes", "[message]"),
    ("task", "Tasks {label} runs on a schedule", "[add <when> | <prompt> · rm n · run n]"),
    ("usage", "{product} quota left, and {label} turns and tokens", ""),
    ("doctor", "Check the {label} integration", ""),
)


def register(ctx):
    configure(ctx.get_config)
    for agent in engine.AGENTS.values():
        for suffix, description, hint in COMMANDS:
            name = f"{agent.backend.prefix}-{suffix}" if suffix else agent.backend.prefix
            handler = globals()[f"_cmd_{suffix or 'status'}"]
            ctx.register_command(name, handler=_target_chat_only(partial(handler, agent)), args_hint=hint,
                                 description=description.format(label=agent.label, product=agent.backend.product))
    own = _own_commands()
    for name in presets.names():
        problem = presets.name_problem(name, own)
        if problem:
            logger.warning("alternative-agents: preset /%s not registered: %s", name, problem)
            continue
        setting = presets.settings(name)
        agent = engine.AGENTS.get(setting["backend"])
        # Hermes looks commands up with "-" for "_", as Telegram's menu shows them.
        ctx.register_command(name.replace("_", "-"), handler=_target_chat_only(partial(_cmd_run_preset, name)),
                             description=setting["description"] or f"New {agent.label if agent else '?'} "
                                                                   f"conversation opened by the /{name} preset")
    ctx.register_command("preset", handler=_target_chat_only(_cmd_preset), args_hint="[name | rm name]",
                         description="Saved prompts opening a conversation (/preset <name> in reply to one)")
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_telegram_handler(_on_telegram_connect)


def _target_chat(source):
    return bool(engine.TARGET_CHAT and source is not None and getattr(source.platform, "value", None) == "telegram"
                and str(source.chat_id) == engine.TARGET_CHAT)


def _target_chat_only(handler):
    """A command handler acting from the configured chat only, as the routing hook does: Hermes runs plugin
    commands for any authorized sender, on any platform (Discord, email…)."""
    async def command(raw_args="", source=None, **_context):
        if not _target_chat(source):
            return "⛔ alternative-agents takes commands from the Telegram chat set in its chat_id only."
        return await handler(raw_args)
    return command


def configure(get):
    """Apply the plugin settings (plugins.entries.alternative-agents.settings in config.yaml)."""
    cfg = config.load(get)
    if not cfg["chat_id"]:
        logger.warning("alternative-agents: no chat_id setting, nothing will be routed "
                       "(hermes config set plugins.entries.alternative-agents.settings.chat_id <telegram chat id>)")
    workspace.configure()
    engine.configure(backends.make(cfg["backends"]))


def _on_telegram_connect(application, adapter):
    # Called by the Telegram adapter at connect(): this is the gateway, and the live bot.
    engine.bind_adapter(adapter)
    engine.schedule_startup()
    from telegram.ext import CallbackQueryHandler
    # Group -1: ahead of the core's catch-all callback handler, which would take the button otherwise.
    application.add_handler(CallbackQueryHandler(_on_banner_button, pattern=f"^{engine.BANNER_LEAVE}"), group=-1)


async def _on_banner_button(update, _context):
    """The banner's ↩️ button: the agent's mode off, as with /<prefix>_off."""
    from telegram.ext import ApplicationHandlerStop
    query = update.callback_query
    agent = engine.AGENTS.get(query.data.removeprefix(engine.BANNER_LEAVE))
    chat = getattr(getattr(query.message, "chat", None), "id", None)
    try:
        await query.answer()
    except Exception:
        logger.debug("alternative-agents: callback answer failed", exc_info=True)
    if agent is not None and str(chat) == engine.TARGET_CHAT:
        if agent.store.load().get("mode_pin") == query.message.message_id:
            await _say(await _cmd_off(agent, ""))
        else:  # a banner the mode no longer tracks (it went off meanwhile): it just goes
            try:
                await query.message.delete()
            except Exception:
                logger.debug("alternative-agents: stale banner not deleted", exc_info=True)
    raise ApplicationHandlerStop


# ── routing ──

async def _on_dispatch(event=None, gateway=None, **_kwargs):
    """Take the agents' messages before auth and dispatch; the configured chat only, since they run
    unsandboxed."""
    try:
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False) or not getattr(event, "allow_gateway_control", True):
            return None
        if not engine.AGENTS or not _target_chat(source):
            return None
        if gateway is not None:
            engine.bind_gateway(gateway)
        decision = route(event, {name: agent.store.load() for name, agent in engine.AGENTS.items()})
        if decision is None:
            return None
        return await _act(decision, event) or {"action": "skip", "reason": "alternative-agents"}
    except Exception:
        logger.exception("alternative-agents: dispatch failed")
        return None


def route(event, states):
    """(kind, agent, payload) — kind: job, btw (payload: Job), handoff, back (payload: text), stop,
    new — or None (the message stays Hermes'). ``states``: each agent's state, by backend name."""
    agents = engine.AGENTS
    media = [str(m) for m in (getattr(event, "media_urls", None) or [])]
    caption = caption_of(event.text or "", media)
    reply_to = getattr(event, "reply_to_message_id", None)
    sticky = next((agents[name] for name, state in states.items() if state.get("sticky") and name in agents), None)
    owner = next((agents[name] for name, state in states.items()
                  if name in agents and agents[name].owns(state, reply_to)), None)
    btw_owner = next((agents[name] for name, state in states.items()
                      if name in agents and agents[name].owns(state, reply_to, side=True)), None)

    command = _SLASH.match(caption)
    if command:
        name = command.group(1).lower().replace("_", "-")
        rest = caption[command.end():]
        for agent in agents.values():
            prefix = agent.backend.prefix
            if name in (prefix, f"{prefix}-new") and (rest.strip() or media):
                return "job", agent, _job(event, rest, media, owner is agent, new=name != prefix)
            if name == f"{prefix}-btw" and (rest.strip() or media):
                return "btw", agent, _job(event, rest, media, True, new=True, side=True)
            if name in (f"{prefix}-handoff", f"{prefix}-back"):
                return name[len(prefix) + 1:], agent, rest.strip()
        if name == "preset":
            return "presets", None, (rest.strip(), getattr(event, "reply_to_text", None) or "")
        preset = presets.find(name)
        if preset:
            return "preset", None, preset
        if sticky and name == "stop" and sticky.busy():
            return "stop", sticky, None
        if sticky and name == "new":
            return "new", sticky, None
        return None
    if sticky and _HERMES_WORD.match(caption):
        return None
    if btw_owner:
        return "btw", btw_owner, _job(event, caption, media, True, side=True)
    if owner or sticky:
        agent = owner or sticky
        return "job", agent, _job(event, caption, media, owner is agent)
    return None


def caption_of(text, media):
    """What the user typed: inlined text documents and the adapter's file notes removed."""
    for path in media:
        body = _small_text(path)
        if not body or body not in text:
            continue
        end = text.index(body) + len(body)
        start = text.rfind("[Content of ", 0, end - len(body))
        text = text[:start if start != -1 else end - len(body)] + text[end:]
    kept = [line for line in text.splitlines()
            if not (line.strip().startswith("[") and any(m in line for m in media))]
    return "\n".join(kept).strip()


def _small_text(path):
    try:
        p = Path(path)
        if p.stat().st_size > INLINE_DOC_MAX_BYTES:
            return ""
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _job(event, text, media, to_agent, new=False, side=False):
    kind = getattr(getattr(event, "message_type", None), "value", "")
    voice = media[:1] if kind == "voice" else []
    types = list(getattr(event, "media_types", None) or [])
    files = [(m, t or "") for m, t in zip_longest(media, types) if m and m not in voice]
    quoted = "" if to_agent else (getattr(event, "reply_to_text", None) or "")
    return engine.Job(text=text.strip(), files=files, voice=voice, message_id=getattr(event, "message_id", None),
                      quoted=quoted, new_conversation=new, side=side)


async def _act(decision, event):
    """Run a routing decision; a dict is the hook's answer instead of the default skip."""
    kind, agent, payload = decision
    if kind == "handoff":
        await _handoff(agent, payload, event.message_id)
    elif kind == "back":
        return {"action": "rewrite", "text": await _hand_back(agent, payload)}
    elif kind == "btw":
        payload.model, payload.effort = await asyncio.to_thread(agent.backend.side_settings, agent.store.load())
        agent.side(payload)
    elif kind == "job":
        await agent.submit(payload)
    elif kind == "stop":
        await agent.send(_stop_text(agent), reply_to=event.message_id)
    elif kind == "new":
        agent.store.update(conversation_id=None)
        await agent.end_preset()
        await agent.send(f"🆕 New {agent.label} conversation from the next message.", reply_to=event.message_id)
    elif kind == "preset":
        await _say(await _run_preset(payload, event.message_id), event.message_id)
    elif kind == "presets":
        args, reply_text = payload
        await _say(await asyncio.to_thread(_manage_presets, args, reply_text), event.message_id)


async def _say(text, reply_to=None):
    """A plugin message that is no agent's (a reply to it goes to Hermes)."""
    if not text:
        return
    ad = engine.adapter()
    if ad is not None:
        try:
            if (await ad.send(engine.TARGET_CHAT, text, reply_to=reply_to)).success:
                return
        except Exception:
            logger.exception("alternative-agents: adapter send raised")
    await asyncio.to_thread(engine._send_standalone, text)


# ── commands (each takes the agent first: register() binds it) ──

async def _cmd_status(agent, raw_args: str):
    if raw_args.strip():  # normally taken by the dispatch hook; this is the fallback
        await agent.submit(engine.Job(text=raw_args.strip()))
        return None
    return _status_card(agent)


def _status_card(agent):
    store, label, cmd = agent.store, agent.label, agent.cmd
    state, snap = store.load(), agent.snapshot()
    lines = [f"{agent.mark} {label} ({agent.backend.product})".strip()]
    other = next((a for a in engine.AGENTS.values() if a is not agent and a.store.load().get("sticky")), None)
    if state.get("sticky"):
        lines.append(f"{label} mode: your messages go to {label} (\"Hermes …\" and / for Hermes) · {cmd('off')}")
    elif other:
        lines.append(f"{other.label} mode is on: {label} when you reply to {label}, or with {cmd()} <message>")
    else:
        lines.append(f"Hermes mode: {label} when you reply to {label}, with {cmd()} <message>, or {cmd('on')}")
    if not engine.TARGET_CHAT:
        lines.append(f"⚠️ No chat_id setting: nothing is routed to {label} (see {cmd('doctor')})")
    effort = store.effort(state)
    lines.append(f"Model: {store.model(state)}" + (f" · effort {effort}" if effort else ""))
    conv = state.get("conversation_id")
    lines.append(f"Conversation: {conv[:8] + '…' if conv else 'new from the next message'}")
    lines.append(f"Display: {'detailed' if store.verbose(state) else 'discreet'} ({cmd('verbose')})")
    if agent.backend.can_hear and store.voice_mode(state) == "direct":
        heard = f"straight to {label} ({agent.backend.product})"
    else:
        heard = "local transcription (Whisper)"
    lines.append(f"Voice notes: {heard} · voice replies: {store.voice_reply(state)} ({cmd('voice')})")
    if state.get("preset"):
        lines.append(f"Conversation opened by the /{state['preset']} preset")
    if snap["busy"]:
        busy = f"Running for {snap['elapsed']} · {snap['actions']} actions"
        if snap["current"]:
            busy += f" · ▶ {snap['current']}"
        if snap["queued"]:
            busy += f" · {snap['queued']} queued"
        lines.append(busy)
    lines.append("\n" + " · ".join(cmd(suffix) for suffix, _d, _h in COMMANDS if suffix))
    return "\n".join(lines)


async def _cmd_on(agent, _raw_args: str):
    already = agent.store.load().get("sticky")
    await _modes_off(but=agent)
    agent.store.update(sticky=True)
    if await agent.set_mode_banner(True):
        return f"{agent.mark} {agent.label} mode already on.".strip() if already else None
    return agent.mode_banner()


def _mode_off(agent):
    """The agent's mode off; a preset's conversation ends with it (x_conv resumes it). Returns the preset."""
    preset = agent.store.load().get("preset")
    agent.store.update(sticky=None, preset=None, **({"conversation_id": None} if preset else {}))
    return preset


async def _modes_off(but=None):
    """One mode at a time: turn off the other agents' modes."""
    for other in engine.AGENTS.values():
        if other is not but and other.store.load().get("sticky"):
            _mode_off(other)
            await other.set_mode_banner(False)


async def _cmd_off(agent, _raw_args: str):
    preset = _mode_off(agent)
    await agent.set_mode_banner(False)
    ended = f" The /{preset} conversation is over ({agent.cmd('conv')} resumes it)." if preset else ""
    return (f"↩️ Back to Hermes.{ended} {agent.label} stays reachable with {agent.cmd()} <message> or by "
            "replying to its messages.")


async def _cmd_new(agent, raw_args: str):
    agent.store.update(conversation_id=None)
    await agent.end_preset()
    if raw_args.strip():
        await agent.submit(engine.Job(text=raw_args.strip(), new_conversation=True))
        return None
    return f"🆕 Next message to {agent.label} = new conversation."


async def _cmd_stop(agent, _raw_args: str):
    return _stop_text(agent)


def _stop_text(agent):
    running, dropped = agent.stop()
    if not running and not dropped:
        return "Nothing running."
    text = "🛑 Stop requested." if running else "🛑"
    if dropped:
        text += f" {dropped} queued message(s) dropped."
    return text


async def _cmd_model(agent, raw_args: str):
    backend, label = agent.backend, agent.label
    choice = raw_args.strip()
    current = agent.store.model(agent.store.load())
    models = await asyncio.to_thread(backend.models)
    if not choice:
        if not models:
            return f"Current model: {current}\n(list unavailable)"
        sent = await _picker(agent, f"{label}'s model:", [
            {"value": name, "label": backend.short_model(desc or name), "is_current": name == current}
            for name, desc in models], partial(_pick_model, agent))
        if sent:
            return None
        rows = [f"{'▶' if name == current else ' '} {i}. {name} — {desc}" for i, (name, desc) in enumerate(models, 1)]
        return f"{label} models:\n" + "\n".join(rows) + f"\n\n{agent.cmd('model')} <number> or <name> to switch."
    if choice.isdigit() and models:
        index = int(choice) - 1
        if not 0 <= index < len(models):
            return f"Invalid number: 1 to {len(models)}."
        choice, note = models[index][0], ""
    else:
        choice, note = await asyncio.to_thread(backend.check_model, choice)
        if choice is None:
            return f"{note}\n{agent.cmd('model')} to see the list."
    return await _pick_model(agent, None, choice) + (f"\n{note}" if note else "")


async def _pick_model(agent, _chat_id, value):
    agent.store.update(model=value)
    return f"{agent.label} model → {value}"


async def _cmd_effort(agent, raw_args: str):
    choice = raw_args.strip().lower()
    state = agent.store.load()
    current, levels = await asyncio.to_thread(agent.backend.efforts, state)
    if not levels:
        return f"{agent.store.model(state)} has no effort variants ({agent.cmd('model')} to pick another model)."
    pick = partial(_pick_effort, agent, levels)
    if not choice:
        if await _picker(agent, f"{agent.label}'s reasoning effort:", [
                {"value": level, "label": level, "is_current": level == current} for level in levels], pick):
            return None
        return f"Effort: {current}\n{agent.cmd('effort')} {' | '.join(levels)}"
    if choice not in levels:
        return f"No {choice} effort for this model: {', '.join(levels)}."
    return await pick(None, choice)


async def _pick_effort(agent, levels, _chat_id, value):
    changes = levels[value]
    agent.store.update(**changes)
    if "model" in changes:
        return f"{agent.label} model → {changes['model']}"
    return f"{agent.label} effort → {value}"


async def _cmd_verbose(agent, raw_args: str):
    arg = raw_args.strip().lower()
    store = agent.store
    if arg in ("on", "yes", "1"):
        verbose = True
    elif arg in ("off", "no", "0"):
        verbose = False
    else:
        verbose = not store.verbose(store.load())
    store.update(verbose=verbose)
    if verbose:
        return f"Detailed display: live status (actions, what {agent.label} writes) then a summary of the actions."
    return f"Discreet display: \"typing…\" while {agent.label} works, then its answer only."


async def _cmd_voice(agent, raw_args: str):
    arg = raw_args.strip().lower()
    if arg.split()[:1] == ["reply"]:
        return _voice_reply(agent, arg.split()[1:])
    store, backend = agent.store, agent.backend
    if not backend.can_hear:
        if arg in ("", "local", "whisper"):
            return (f"🎙 Voice notes → local mode (Whisper transcription on this machine): {backend.product} "
                    "takes no audio.")
        return f"{backend.product} takes no audio: voice notes are always transcribed locally (Whisper)."
    current = store.voice_mode(store.load())
    if arg in ("direct", "google", "agy"):
        mode = "direct"
    elif arg in ("local", "whisper"):
        mode = "local"
    elif not arg:
        mode = "local" if current == "direct" else "direct"
    else:
        return f"Invalid voice mode: `{arg}`. Use `direct` ({backend.product}) or `local` (Whisper)."
    store.update(voice=mode)
    if mode == "direct":
        return f"🎙 Voice notes → direct mode: the raw audio file goes to {agent.label} ({backend.product})."
    return "🎙 Voice notes → local mode (Whisper transcription on this machine)."


def _voice_reply(agent, args):
    store = agent.store
    if not args:
        return (f"Voice replies: {store.voice_reply(store.load())}.\n"
                f"{agent.cmd('voice')} reply off · mirror (when you sent a voice note) · always")
    if args[0] not in config.VOICE_REPLY_MODES:
        return f"Invalid mode: `{args[0]}`. Use off, mirror or always."
    store.update(voice_reply=args[0])
    return {"off": "🔇 Voice replies off.",
            "mirror": f"🔊 {agent.label} answers your voice notes with a voice note too.",
            "always": f"🔊 {agent.label} also answers every message with a voice note."}[args[0]]


# ── handoff ──

async def _cmd_handoff(agent, raw_args: str):
    await _handoff(agent, raw_args.strip())  # normally taken by the dispatch hook; this is the fallback
    return None


async def _handoff(agent, message, reply_to=None):
    """New conversation seeded with the Hermes thread, and the agent's mode on."""
    thread = await asyncio.to_thread(workspace.hermes_thread, HANDOFF_THREAD_MESSAGES)
    user = workspace.USER
    text = (f"Hermes hands this conversation with {user} over to you. Recent thread between {user} and "
            f"Hermes:\n{thread or '(empty)'}\n\n"
            + (message or f"Pick up where Hermes left off: in a few lines, say where things stand and what "
                          f"you will do next, then wait for {user}."))
    await _modes_off(but=agent)
    await agent.end_preset()
    agent.store.update(sticky=True)
    await agent.set_mode_banner(True)
    await agent.submit(engine.Job(text=text, message_id=reply_to, new_conversation=True))


async def _cmd_back(agent, raw_args: str):
    # Normally taken by the dispatch hook, which hands the recap to Hermes as the message itself.
    await _hand_back(agent, "")
    return "↩️ Back to Hermes."


async def _hand_back(agent, message):
    """The agent's mode off; returns the message Hermes gets: the conversation's recap, then ``message``."""
    state = agent.store.load()
    _mode_off(agent)
    await agent.set_mode_banner(False)
    user = workspace.USER
    recap = await asyncio.to_thread(agent.store.recap, state.get("conversation_id"), HANDBACK_TURNS, user,
                                    agent.label, 1200)
    head = (f"[Handover from {agent.label} ({agent.backend.product}): {user} worked with {agent.label} and comes "
            f"back to you. Their last exchanges:\n{recap or '(none)'}]\n\n")
    return head + (message or "Take over from here: in a few lines, say where things stand and what's next.")


# ── tasks ──

async def _cmd_task(agent, raw_args: str):
    from . import tasks
    label, cmd, backend = agent.label, agent.cmd("task"), agent.backend.name
    verb, _, rest = raw_args.strip().partition(" ")
    verb = verb.lower()
    try:
        if verb == "add":
            when, sep, prompt = rest.partition("|")
            if not sep or not when.strip() or not prompt.strip():
                return (f"Usage: {cmd} add <when> | <prompt>\n"
                        "when: every day at 8am · every monday 9am · 0 7 * * 1-5 · in 2h · every 6h")
            task = await asyncio.to_thread(tasks.create, when, prompt, agent.backend.task_settings())
            return (f"⏰ {label} task \"{task['name']}\" scheduled ({task['schedule']}"
                    f"{', next ' + _when(task['next_run_at']) if task.get('next_run_at') else ''}).")
        listed = await asyncio.to_thread(tasks.list_tasks, backend)
        if verb in ("rm", "run"):
            if not rest.strip().isdigit() or not 1 <= int(rest) <= len(listed):
                return f"Invalid number: 1 to {len(listed)}." if listed else f"No {label} task."
            task = listed[int(rest) - 1]
            if verb == "rm":
                await asyncio.to_thread(tasks.remove, task["job_id"])
                return f"🗑 {label} task \"{task['name']}\" removed."
            await asyncio.to_thread(tasks.run_now, task["job_id"])
            return f"▶ {label} task \"{task['name']}\" started: its answer will arrive here."
    except Exception as e:
        logger.exception("alternative-agents: %s failed", cmd)
        return f"❌ {label} task: {e}"
    if not listed:
        return (f"No {label} task. A task runs a prompt on {label} on a schedule, free of Hermes' LLM:\n"
                f"{cmd} add every day at 8am | summarize my unread mails")
    rows = [f"{i}. {t['name']} · {t['schedule']}"
            + (f" · next {_when(t['next_run_at'])}" if t.get("next_run_at") else "")
            + (f" · {t['state']}" if t.get("state") not in (None, "scheduled", "active") else "")
            for i, t in enumerate(listed, 1)]
    return (f"{label} tasks:\n" + "\n".join(rows)
            + f"\n\n{cmd} rm <n> · {cmd} run <n> · {cmd} add <when> | <prompt>")


def _when(iso):
    return str(iso or "")[:16].replace("T", " ")


# ── usage ──

async def _cmd_usage(agent, _raw_args: str):
    quota, turns = await asyncio.gather(asyncio.to_thread(agent.backend.quota),
                                        asyncio.to_thread(agent.store.recent_turns))
    head = f"{agent.mark} {agent.label} usage".strip()
    return f"{head}\n{quota}\n\n" + (_turn_stats(turns) if turns else f"No {agent.label} turn recorded.")


def _turn_stats(turns):
    today = datetime.now().date()
    rows = []
    for label, days in (("Today", 1), ("7 days", 7), ("30 days", 30)):
        since = (today - timedelta(days=days - 1)).isoformat()
        window = [t for t in turns if (t.get("at") or "") >= since]
        if not window:
            rows.append(f"{label}: nothing")
            continue
        tokens = sum(int(t.get("tokens") or 0) for t in window)
        minutes = sum(int(t.get("seconds") or 0) for t in window) // 60
        failed = sum(t.get("status") not in ("SUCCESS", "STOPPED") for t in window)
        tasks = sum(bool(t.get("task")) for t in window)
        cost = sum(float(t.get("cost_usd") or 0) for t in window)
        rows.append(f"{label}: {len(window)} turns" + (f" ({tasks} tasks)" if tasks else "")
                    + f" · {tokens // 1000}k tokens" + (f" (${cost:.2f} at API prices)" if cost else "")
                    + f" · {minutes} min" + (f" · {failed} failed" if failed else ""))
    since = (today - timedelta(days=29)).isoformat()
    models = {}
    for t in turns:
        if (t.get("at") or "") >= since:
            model = t.get("model") or "?"
            models[model] = models.get(model, 0) + int(t.get("tokens") or 0)
    by_model = " · ".join(f"{m} {n // 1000}k" for m, n in sorted(models.items(), key=lambda kv: -kv[1]))
    return "Turns through this plugin:\n" + "\n".join(rows) + (f"\n30 days by model: {by_model}" if by_model else "")


async def _cmd_conv(agent, raw_args: str):
    convs = await asyncio.to_thread(agent.store.conversations)
    current = agent.store.load().get("conversation_id")
    if not convs:
        return f"No {agent.label} conversation recorded."
    arg = raw_args.strip()
    pick = partial(_pick_conv, agent)
    if arg:
        if not arg.isdigit() or not 1 <= int(arg) <= len(convs):
            return f"Invalid number: 1 to {len(convs)}."
        return await pick(None, convs[int(arg) - 1]["id"])
    choices = [{"value": c["id"], "label": f"{c['last'][5:10]} · {_cut(_conv_title(c), 28)}",
                "is_current": c["id"] == current} for c in convs]
    if await _picker(agent, f"Resume a {agent.label} conversation:", choices, pick):
        return None
    rows = [f"{'▶' if c['id'] == current else ' '} {i}. {c['last'][:16].replace('T', ' ')} · {c['turns']} turns · "
            f"{_cut(_conv_title(c), 60)}" for i, c in enumerate(convs, 1)]
    return f"{agent.label} conversations:\n" + "\n".join(rows) + f"\n\n{agent.cmd('conv')} <number> to resume."


def _conv_title(conv):
    """A conversation as listed: its first message, or the preset that opened it and what followed."""
    if conv["preset"]:
        return f"/{conv['preset']} · {conv['topic']}" if conv["topic"] else f"/{conv['preset']}"
    return conv["first"]


async def _pick_conv(agent, _chat_id, value):
    """Resume a conversation in the agent's mode, under its preset's banner if a preset opened it."""
    conv = next((c for c in agent.store.conversations(50) if c["id"] == value), None)
    preset = (conv or {}).get("preset") or None
    before = agent.store.load()
    await _modes_off(but=agent)
    bare = bool(preset) and presets.settings(preset)["bare"]
    agent.store.update(conversation_id=value, sticky=True, preset=preset, **({"bare": value} if bare else {}))
    if before.get("sticky") and before.get("preset") != preset:
        await agent.set_mode_banner(False)  # the banner changes with the preset
    await agent.set_mode_banner(True)
    title = f": {_cut(_conv_title(conv), 60)}" if conv else ""
    return (f"{agent.mark} {agent.label} conversation resumed{title}\n".lstrip()
            + f"Your messages go to {agent.label}, {agent.cmd('off')} to go back to Hermes.")


async def _picker(agent, title, choices, on_selected):
    """Inline-keyboard picker through the adapter; False when unavailable."""
    ad = engine.adapter()
    picker = getattr(ad, "send_choice_picker", None)
    if picker is None:
        return False
    try:
        result = await picker(engine.TARGET_CHAT, title, choices,
                              f"alternative-agents:{agent.backend.name}:{engine.TARGET_CHAT}", on_selected)
    except Exception:
        logger.exception("alternative-agents: picker failed")
        return False
    return bool(getattr(result, "success", False))


async def _cmd_btw(agent, raw_args: str):
    if not raw_args.strip():
        return (f"{agent.cmd('btw')} <question>: a side question, answered right away even while {agent.label} "
                "works, without touching the main conversation. Reply to its answer to keep that side "
                "conversation going.")
    # Normally taken by the dispatch hook; this is the fallback.
    model, effort = await asyncio.to_thread(agent.backend.side_settings, agent.store.load())
    agent.side(engine.Job(text=raw_args.strip(), new_conversation=True, side=True, model=model, effort=effort))
    return None


async def _cmd_retry(agent, _raw_args: str):
    last = agent.store.load().get("last_turn")
    if last and last.get("preset"):  # the preset's prompt again, in a new conversation, as it first went
        queued = await agent.submit(engine.Job(preset=last["preset"], new_conversation=True))
        return f"🔁 /{last['preset']} sent to {agent.label} again" + (" (after the current answer)." if queued else ".")
    if not last or not last.get("text"):
        return "Nothing to send again."
    queued = await agent.submit(engine.Job(text=last["text"]))
    return f"🔁 Sent to {agent.label} again" + (" (after the current answer)." if queued else ".")


# ── presets ──

def _own_commands():
    return {f"{a.backend.prefix}_{suffix}" if suffix else a.backend.prefix
            for a in engine.AGENTS.values() for suffix, _d, _h in COMMANDS}


async def _run_preset(name, reply_to=None):
    """/<name>: the preset's agent in its mode, and a new conversation opened by the prompt, sent in the
    background. Returns what to tell the user ("" when the agent's answer says it all)."""
    setting = presets.settings(name)
    agent = engine.AGENTS.get(setting["backend"])
    if agent is None:
        return f"⚠️ /{name} needs {setting['backend']}, which is not in the plugin's backends setting."
    if not await asyncio.to_thread(presets.exists, name):
        return f"⚠️ /{name} has no prompt any more: /preset {name} in reply to it."
    reason = agent.backend.unavailable()
    if reason:
        return f"⚠️ /{name}: {reason}"
    await _modes_off(but=agent)
    agent.store.update(sticky=True, preset=name, conversation_id=None)
    await agent.set_mode_banner(False)  # the mode's banner, if any, gives way to the preset's
    await agent.set_mode_banner(True)
    await agent.submit(engine.Job(preset=name, new_conversation=True, message_id=reply_to))
    icon = (setting["banner"].split() or [agent.mark])[0]
    return f"{icon} …"


def _manage_presets(args, reply_text):
    """/preset: list; /preset <name> in reply to a message: save it as the prompt; /preset rm <name>."""
    words = args.split()
    if not words:
        listed = presets.names()
        if not listed:
            return "No preset yet. /preset <name> in reply to a message saves it as a prompt."
        rows = []
        for name in listed:
            agent = engine.AGENTS.get(presets.settings(name)["backend"])
            rows.append(f"/{name} · {agent.label if agent else presets.settings(name)['backend'] + ' (off)'} · "
                        f"{len(presets.read(name))} chars")
        return ("Presets:\n" + "\n".join(rows) + "\n\n/preset <name> in reply to a message: save it as the prompt · "
                "/preset rm <name>")
    if words[0] == "rm":
        name = words[1].lower() if len(words) > 1 else ""
        if not name or name not in presets.names():
            return f"No /{name} preset: /preset lists them." if name else "Usage: /preset rm <name>"
        presets.remove(name)
        return f"🗑 /{name} removed." + (" Its settings stay in config.yaml." if name in config.cfg["presets"] else "")
    name = words[0].lower()
    if not reply_text.strip():
        return f"Reply to the prompt's message with /preset {name}: it becomes /{name}'s prompt."
    new = name not in presets.names()
    problem = presets.name_problem(name, _own_commands()) if new else ""
    if problem:
        return f"/{name} can't be a preset: {problem}."
    presets.save(name, reply_text)
    agent = engine.AGENTS.get(presets.settings(name)["backend"])
    return (f"✅ /{name}: prompt saved ({len(reply_text.strip())} chars) for "
            f"{agent.label if agent else presets.settings(name)['backend']}."
            + (f" New preset: type /{name} (in the / menu after a gateway restart)." if new else ""))


async def _cmd_preset(raw_args: str):
    # Normally taken by the dispatch hook, which also sees the message replied to.
    return await asyncio.to_thread(_manage_presets, raw_args, "")


async def _cmd_run_preset(name, _raw_args: str):
    return f"/{name} opens its conversation from the Telegram chat set in the plugin's chat_id."


async def _cmd_doctor(agent, _raw_args: str):
    from . import doctor
    return await doctor.report(agent)


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
