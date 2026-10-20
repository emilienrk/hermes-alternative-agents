"""Turns: the queue, the agent's process, its stream-json events and the Telegram output.

One Agent per enabled backend (agy, Claude Code, opencode), each with its queue, its turn and its btw;
backends.py knows how to run each CLI and read its events, this module does the rest the same
way for both.

Everything runs on the gateway's event loop; blocking work (transcription, file copies,
workspace refresh) goes to threads, since a blocked loop trips the gateway's liveness watchdog.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import workspace
from .config import cfg
from .state import is_message

logger = logging.getLogger(__name__)

# Set by configure() from the plugin settings.
TARGET_CHAT = ""
TARGET = ""
HARD_TIMEOUT_S = 35 * 60  # HARD_TIMEOUT_S, REACT_*: set by configure()
TYPING_EVERY_S = 4.5
STATUS_EVERY_S = 4.0
STATUS_REFRESH_S = 30.0
STREAM_LIMIT = 64 * 1024 * 1024  # stream-json lines carry whole responses
MAX_REPLY_CHARS = 12000
SUMMARY_MAX_LINES = 12
STATUS_ACTIONS = 5
INBOX_KEEP_DAYS = 30
RECAP_TURNS = 6
BTW_RECAP_TURNS = 4
BTW_HEAD = ("Side question (\"btw\"): {user} asks this alongside the main conversation, which another {agent} "
            "may be working on right now: don't touch its work in progress. Recent exchanges of the main "
            "conversation:\n{recap}\n\nAnswer briefly.\n\n")
BTW_FORK_HEAD = ("Side question (\"btw\"): {user} asks this in a copy of the conversation, while the main one may "
                 "still be working: don't touch its work in progress. Answer briefly.\n\n")
REACT_WORKING = "👀"
REACT_QUEUED = "✍"
MAX_SPOKEN_CHARS = 4000

_FILE_LINK = re.compile(r"\[([^\]]+)\]\(file://[^)]*\)")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac"}


@dataclass
class Job:
    """One message from the user for an agent."""
    text: str = ""
    files: list = field(default_factory=list)  # [(path, mime)] in the gateway cache
    voice: list = field(default_factory=list)  # voice notes to transcribe
    message_id: str | None = None
    quoted: str = ""  # the message it replies to, when that one is not the agent's
    new_conversation: bool = False
    side: bool = False  # a btw message, for the side conversation
    model: str = ""  # overrides the state's model
    effort: str = ""  # overrides the state's effort


class Run:
    """The live turn."""

    def __init__(self, jobs, model, effort="", side=False):
        self.jobs, self.model, self.effort, self.side = jobs, model, effort, side
        self.conversation_id = None
        self.started = time.time()
        self.proc = None
        self.context = None  # what the backend told the agent of Hermes this turn
        self.reset()
        self.stopped = False
        self.status_msg = None

    def reset(self):
        self.actions = []
        self.current = None
        self.text = {}  # step -> text streamed so far
        self.last_step = None
        self.step = 0
        self.tools = {}  # tool call id -> description, while it runs
        self.result = None
        self.noise = []
        self.timed_out = False
        self.changed = True
        self.tokens = 0
        self.cost = 0.0
        self.rate_limit = None

    def error_text(self):
        if self.result is not None:
            return str(self.result.get("error") or self.result.get("response") or "")
        return "\n".join(self.noise)

    def failed(self):
        return not self.stopped and (self.result is None or self.result.get("status") != "SUCCESS")


_rt = {"gateway": None, "adapter": None, "loop": None, "notice_sent": False}
AGENTS = {}  # backend name -> Agent, set by configure()


def configure(backends):
    """Apply config.cfg (loaded by the plugin's register()) and build the agents."""
    global TARGET_CHAT, TARGET, HARD_TIMEOUT_S, REACT_WORKING, REACT_QUEUED
    TARGET_CHAT = cfg["chat_id"]
    TARGET = f"telegram:{TARGET_CHAT}"
    HARD_TIMEOUT_S = (cfg["timeout_minutes"] + 5) * 60
    REACT_WORKING, REACT_QUEUED = str(cfg["react_working"] or ""), str(cfg["react_queued"] or "")
    AGENTS.clear()
    AGENTS.update({name: Agent(backend) for name, backend in backends.items()})


# ── binding to the gateway ──

def bind_adapter(adapter):
    _rt["adapter"] = adapter
    try:
        _rt["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass


def bind_gateway(gateway):
    _rt["gateway"] = gateway
    _rt["loop"] = asyncio.get_running_loop()


def adapter():
    if _rt["adapter"] is not None:
        return _rt["adapter"]
    gateway = _rt["gateway"]
    if gateway is None:
        return None
    try:
        from gateway.config import Platform
        return (getattr(gateway, "adapters", None) or {}).get(Platform.TELEGRAM)
    except Exception:
        return None


def schedule_startup(delay=20):
    """Once per gateway start, when the bot is up: the restart notices and the mode banner."""
    loop = _rt["loop"]
    if loop is None or _rt["notice_sent"]:
        return
    _rt["notice_sent"] = True
    loop.call_later(delay, lambda: loop.create_task(_startup()))


async def _startup():
    for agent in AGENTS.values():
        await agent.restart_notice()
        await agent.set_mode_banner(bool(agent.store.load().get("sticky")))


class Agent:
    """One backend's turns in the chat."""

    def __init__(self, backend):
        self.backend = backend
        self.store = backend.store
        self.queue = []
        self.worker = None
        self.run = None
        self.side_run = None
        self.side_lock = None

    @property
    def mark(self):
        return self.backend.mark

    @property
    def label(self):
        return self.backend.label

    def cmd(self, suffix=""):
        return f"/{self.backend.prefix}_{suffix}" if suffix else f"/{self.backend.prefix}"

    def owns(self, state, message_id, side=False):
        return is_message(state, message_id, "btw_msgs" if side else "msgs")

    def mode_banner(self):
        return (f"{self.mark} {self.label} mode on · {self.cmd('off')} to go back to Hermes\n".lstrip()
                + f"Your messages, photos and voice notes go to {self.label}. For Hermes: \"Hermes …\" or a / "
                f"command. Here /stop and /new act on {self.label}.")

    async def restart_notice(self):
        inflight = self.store.load().get("inflight")
        if not inflight or self.run is not None:
            return
        self.store.update(inflight=None)
        started = inflight.get("at", "")[11:16]
        message = _cut(inflight.get("message", ""), 120)
        await self.send(f"⚠️ Hermes restarted while {self.label} was answering \"{message}\" (started at {started}): "
                        f"the answer is lost. {self.cmd('retry')} to run it again.")

    # ── queue ──

    def busy(self):
        return self.run is not None

    def snapshot(self):
        run = self.run
        return {"busy": run is not None, "queued": len(self.queue),
                "elapsed": _elapsed(run.started) if run else "", "actions": len(run.actions) if run else 0,
                "current": run.current if run else None}

    async def submit(self, job):
        """Queue a job and start the worker when idle; returns True when it waits behind a turn."""
        self.queue.append(job)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self._drain(), name=f"alternative-agents-{self.backend.name}")
            return False
        if job.message_id and REACT_QUEUED:
            await react(job.message_id, REACT_QUEUED)
        return True

    def stop(self):
        """Kill the running turn and drop the queue; returns (was_running, dropped)."""
        dropped = len(self.queue)
        for job in self.queue:
            if job.message_id:
                asyncio.get_running_loop().create_task(react(job.message_id, None))
        self.queue.clear()
        running = False
        for run in (self.run, self.side_run):
            if run is None:
                continue
            running = True
            run.stopped = True  # also covers a turn still transcribing, before the agent starts
            if run.proc is not None and run.proc.returncode is None:
                _kill(run.proc)
        return running, dropped

    async def _drain(self):
        while self.queue:
            jobs = self.queue[:]
            self.queue.clear()
            try:
                await self._turn(jobs)
            except Exception as e:  # the worker must survive and always answer
                logger.exception("alternative-agents: %s turn failed", self.backend.name)
                await self.send(f"❌ alternative-agents: {e}\n{self.cmd('retry')} to run it again.")
            finally:
                self.run = None
                for job in jobs:
                    if job.message_id:
                        await react(job.message_id, None)

    # ── turn ──

    async def _turn(self, jobs):
        backend, store = self.backend, self.store
        if any(job.new_conversation for job in jobs):
            store.update(conversation_id=None)
        state = store.load()
        conv = state.get("conversation_id")
        verbose = store.verbose(state)
        anchor = next((job.message_id for job in reversed(jobs) if job.message_id), None)
        run = self.run = Run(jobs, store.model(state), store.effort(state))
        for job in jobs:
            if job.message_id and REACT_WORKING:
                await react(job.message_id, REACT_WORKING)

        message, files = await self._compose(jobs, verbose)
        await asyncio.to_thread(backend.workspace.prepare)
        store.update(inflight={"message": message[:500], "at": datetime.now().isoformat(timespec="seconds")},
                     last_turn={"text": message, "files": files})
        prompt = await asyncio.to_thread(backend.prompt, run, message, conv)

        loop = asyncio.get_running_loop()
        side_tasks = [loop.create_task(_typing_loop())]
        if verbose:
            side_tasks.append(loop.create_task(self._status_loop(run, anchor)))
        notice = ""
        try:
            await self._exec(run, prompt, conv)
            if run.failed() and conv and backend.restart_fresh(run.error_text()):
                notice = f"♻️ {backend.restart_notice}: new conversation, seeded with the recent thread."
                store.update(conversation_id=None)
                recap = store.recap(conv, RECAP_TURNS, workspace.USER, self.label)
                seeded = f"Last exchanges of the previous conversation:\n{recap}\n\n{message}" if recap else message
                run.reset()
                await self._exec(run, await asyncio.to_thread(backend.prompt, run, seeded, None), None)
            elif run.failed() and backend.transient(run.error_text()):
                await asyncio.sleep(5)
                run.reset()
                await self._exec(run, prompt, store.load().get("conversation_id"))
        finally:
            for task in side_tasks:
                task.cancel()
            store.update(inflight=None)

        await asyncio.to_thread(backend.finished, run)
        body, attachments = self._reply(run, verbose, notice)
        if verbose and run.status_msg:
            await _edit(run.status_msg, self._summary(run))
        await self.deliver(body, attachments, anchor)
        if self._speaks(state, jobs) and not run.failed() and run.result:
            await self._speak(_spoken_text(run.result.get("response") or ""), anchor)
        await asyncio.to_thread(store.log_turn, {
            "model": run.model, **({"effort": run.effort} if run.effort else {}),
            "conversation_id": store.load().get("conversation_id"),
            "message": message[:500], "response": ((run.result or {}).get("response") or "")[:2000],
            "status": "STOPPED" if run.stopped else (run.result or {}).get("status", "NO_RESULT"),
            "seconds": int(time.time() - run.started), "files": [p for p, _ in files],
            "actions": run.actions, "tokens": run.tokens, **({"cost_usd": run.cost} if run.cost else {})})

    async def _compose(self, jobs, verbose):
        """The message the agent receives, and the attachments copied to its inbox."""
        parts, files = [], []
        vmode = self.store.voice_mode(self.store.load()) if self.backend.can_hear else "local"
        user = workspace.USER
        for job in jobs:
            text = job.text.strip()
            for path in job.voice:
                if vmode == "direct":
                    files += await asyncio.to_thread(self._stash, [(path, "audio/ogg")])
                    voice_note = (f"Voice message from {user} (open the attached audio file with view_file to hear "
                                  "the request and answer it).")
                    text = f"{text}\n{voice_note}".strip()
                    if verbose:
                        await self.send(f"🎙 Voice note passed straight to {self.label} ({self.backend.product})",
                                        reply_to=job.message_id)
                else:
                    transcript = await asyncio.to_thread(_transcribe, path)
                    if transcript:
                        text = f"{text}\n{transcript}".strip()
                        if verbose:
                            await self.send(f"🎙 \"{transcript}\"", reply_to=job.message_id)
                    else:
                        files += await asyncio.to_thread(self._stash, [(path, "audio/ogg")])
                        text = f"{text}\n(voice message, not transcribed: the audio file is attached)".strip()
            files += await asyncio.to_thread(self._stash, job.files)
            if job.quoted:
                text = f"[Replying to: \"{_cut(job.quoted, 1500)}\"]\n{text}"
            parts.append(text or "Look at the attached file(s).")
        if len(parts) == 1:
            message = parts[0]
        else:
            message = f"Several messages from {user}, in order:\n\n" + "\n\n".join(
                f"{i}. {part}" for i, part in enumerate(parts, 1))
        if files:
            listing = "\n".join(f"- {path}" + (f" ({kind})" if kind else "") for path, kind in files)
            message += f"\n\nFiles attached by {user} (open them with your tools):\n{listing}"
        return message, files

    def _stash(self, files):
        """Copy attachments out of the gateway cache (pruned on its own schedule) into the agent's inbox."""
        inbox = self.backend.workspace.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - INBOX_KEEP_DAYS * 86400
        for old in inbox.iterdir():
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = []
        for i, (src, kind) in enumerate(files):
            dest = inbox / f"{stamp}-{i}-{Path(src).name}"
            try:
                shutil.copy2(src, dest)
            except OSError:
                logger.warning("alternative-agents: cannot copy attachment %s", src)
                continue
            out.append((str(dest), kind or ""))
        return out

    async def _exec(self, run, prompt, conv, fork=False):
        if run.stopped:
            return
        backend = self.backend
        reason = backend.unavailable()
        if reason:
            run.result = {"status": "ERROR", "response": "", "error": reason}
            return
        cmd, stdin = backend.command(run, prompt, conv, fork)
        root = backend.workspace.root
        root.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(root), env=backend.env(_base_env()),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=STREAM_LIMIT,
            start_new_session=True)  # own process group: stop/timeout also reach the agent's children
        run.proc = proc
        if stdin is not None:
            try:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the process died early: its output says why
            finally:
                proc.stdin.close()
        try:
            await asyncio.wait_for(self._read_events(run, proc), max(5.0, HARD_TIMEOUT_S - (time.time() - run.started)))
        except (asyncio.TimeoutError, ValueError):
            run.timed_out = True
            _kill(proc)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            _kill(proc, signal.SIGKILL)
            await proc.wait()

    async def _read_events(self, run, proc):
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                run.noise = (run.noise + [line])[-40:]
                continue
            known = run.conversation_id
            self.backend.on_event(run, event)
            if run.conversation_id != known and not run.side:
                self.store.update(conversation_id=run.conversation_id)

    # ── Telegram output ──

    async def _status_loop(self, run, anchor):
        shown_at = 0.0
        while True:
            await asyncio.sleep(STATUS_EVERY_S)
            if not run.changed and time.time() - shown_at < STATUS_REFRESH_S:
                continue
            run.changed, shown_at = False, time.time()
            text = self._status_text(run)
            if run.status_msg is None:
                ids = await self.send(text, reply_to=anchor)
                run.status_msg = ids[-1] if ids else None
            else:
                await _edit(run.status_msg, text, final=False)

    def _status_text(self, run):
        lines = [f"{self.mark} {self.label} working · {_elapsed(run.started)} · {len(run.actions)} actions".strip()]
        if run.current:
            lines.append(f"▶ {run.current}")
        lines += run.actions[-STATUS_ACTIONS:][::-1]
        said = run.text.get(run.last_step, "").strip().splitlines()
        if said:
            lines.append(f"💬 {_cut(said[-1], 160)}")
        return "\n".join(lines)

    def _summary(self, run):
        errors = sum(a.startswith("✗") for a in run.actions)
        head = f"🔧 {len(run.actions)} actions" + (f" ({errors} failed)" if errors else "")
        head += f" · {_elapsed(run.started)}" + (f" · {run.tokens // 1000}k tokens" if run.tokens else "")
        shown = run.actions[-SUMMARY_MAX_LINES:]
        more = [f"… +{len(run.actions) - len(shown)} earlier"] if len(run.actions) > len(shown) else []
        return "\n".join([head, *more, *shown])

    def _reply(self, run, verbose, notice="", label=""):
        """(text, attachments) for the final answer."""
        from gateway.platforms.base import BasePlatformAdapter
        result = run.result or {}
        attachments = []
        if run.stopped:
            text = "🛑 Stopped."
        elif run.result is None:
            text = (f"⌛ No answer from {self.label} in {HARD_TIMEOUT_S // 60} min." if run.timed_out else
                    f"❌ {self.label} exited without an answer (code {run.proc.returncode if run.proc else '?'}):\n"
                    f"{_cut(run.error_text(), 800) or 'no output'}") + self._hint(run)
        elif result.get("status") != "SUCCESS":
            text = f"❌ {self.label} {result.get('status')}: {_cut(run.error_text(), 800) or '?'}" + self._hint(run)
        else:
            attachments, text = BasePlatformAdapter.extract_media(clean(result.get("response") or ""))
            attachments = [path for path, _voice in attachments]
            text = text.strip() or ("" if attachments else "(empty answer)")
            if len(text) > MAX_REPLY_CHARS:
                outbox = self.backend.workspace.outbox
                full = outbox / f"answer-{datetime.now():%Y%m%d-%H%M%S}.md"
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(text)
                text = text[:MAX_REPLY_CHARS] + "\n\n[…continued in the attached file]"
                attachments.append(str(full))
        if not verbose and run.actions and not run.stopped and (run.result or {}).get("status") != "SUCCESS":
            text += "\n\nLast actions:\n" + "\n".join(run.actions[-5:])
        footer = ""
        if cfg["footer"]:
            model = run.model + (f" · {run.effort}" if run.effort else "")
            parts = [f"{self.mark} {self.label}".strip(), *([label] if label else []), *([model] if verbose else []),
                     _elapsed(run.started)]
            footer = "— " + " · ".join(parts)
        if verbose and not run.status_msg and run.actions:  # turn too short for the live status message
            text += "\n\n" + self._summary(run)
        if notice:
            text = f"{notice}\n\n{text}"
        return f"{text}\n\n{footer}".strip(), attachments

    def _hint(self, run):
        return "\n\n" + (self.backend.hint(run.error_text()) or f"{self.cmd('retry')} to run it again.")

    async def deliver(self, text, attachments=(), reply_to=None, side=False):
        """Send the agent's answer and its files; every message sent is remembered as the agent's
        (or its btw's)."""
        from gateway.platforms.base import BasePlatformAdapter
        ids = await self.send(text, reply_to=reply_to, side=side) if text.strip() else []
        ad = adapter()
        dropped = []  # same policy as Hermes' own sends: no secrets (.env, ssh keys…), no missing files
        safe = await asyncio.to_thread(BasePlatformAdapter.filter_media_delivery_paths,
                                       [(path, False) for path in attachments], "", dropped)
        for drop in dropped:
            ids += await self.send(f"⚠️ File not sent ({drop['reason']}): {_home(drop['path'])}", reply_to=reply_to,
                                   side=side)
        for path, _voice in safe:
            p = Path(path)
            if ad is None:
                ids += await self.send(f"MEDIA:{p}", side=side)
                continue
            ext = p.suffix.lower()
            try:
                if ext in _IMAGE_EXTS:
                    res = await ad.send_image_file(TARGET_CHAT, str(p), reply_to=reply_to)
                elif ext in _VIDEO_EXTS:
                    res = await ad.send_video(TARGET_CHAT, str(p), reply_to=reply_to)
                elif ext in _AUDIO_EXTS:
                    res = await ad.send_voice(TARGET_CHAT, str(p), reply_to=reply_to)
                else:
                    res = await ad.send_document(TARGET_CHAT, str(p), reply_to=reply_to)
            except Exception as e:
                logger.exception("alternative-agents: cannot send %s", p)
                res = None
                error = str(e)
            else:
                error = getattr(res, "error", "")
            if res is not None and res.success:
                ids.append(res.message_id)
            else:
                ids += await self.send(f"⚠️ Cannot send {p.name}: {error or '?'}", reply_to=reply_to, side=side)
        self._remember(ids, side)
        return ids

    def _remember(self, ids, side=False):
        self.store.remember_messages(ids, key="btw_msgs" if side else "msgs")

    async def send(self, text, reply_to=None, side=False):
        """Send a text message as the agent; returns the Telegram message ids."""
        ad = adapter()
        if ad is not None:
            try:
                res = await ad.send(TARGET_CHAT, text, reply_to=reply_to)
                if res.success:
                    ids = [*getattr(res, "continuation_message_ids", ()), res.message_id]
                    self._remember(ids, side)
                    return [i for i in ids if i]
                logger.warning("alternative-agents: adapter send failed: %s", res.error)
            except Exception:
                logger.exception("alternative-agents: adapter send raised")
        # No live adapter (or it failed): Hermes' standalone sender. It also mirrors the text into
        # Hermes' session, which the adapter path avoids.
        out = await asyncio.to_thread(_send_standalone, text)
        return [out] if out else []

    async def set_mode_banner(self, sticky):
        """The mode's indicator: a banner pinned at the top of the chat while the mode is on.
        False when the bot can't be reached (the caller then answers in plain text)."""
        bot = getattr(adapter(), "_bot", None)  # the adapter has no pin API
        if bot is None:
            return False
        chat, pinned = int(TARGET_CHAT), self.store.load().get("mode_pin")
        try:
            if sticky and not pinned:
                from telegram import ReplyKeyboardRemove
                # ReplyKeyboardRemove clears the input keyboard an earlier version used as the indicator.
                msg = await bot.send_message(chat_id=chat, text=self.mode_banner(), reply_markup=ReplyKeyboardRemove())
                await bot.pin_chat_message(chat_id=chat, message_id=msg.message_id, disable_notification=True)
                self.store.update(mode_pin=msg.message_id)
            elif not sticky and pinned:
                self.store.update(mode_pin=None)
                for call in (bot.unpin_chat_message, bot.delete_message):
                    try:  # the user may have unpinned or deleted it already
                        await call(chat_id=chat, message_id=pinned)
                    except Exception:
                        logger.debug("alternative-agents: %s failed", call.__name__, exc_info=True)
        except Exception:
            logger.exception("alternative-agents: mode banner failed")
            return False
        return True

    # ── btw: a side conversation alongside the main one ──

    def side(self, job):
        """Run a btw message now, next to the main turn and outside its queue (one btw at a time)."""
        asyncio.get_running_loop().create_task(self._side_turn(job), name="alternative-agents-btw")

    async def _side_turn(self, job):
        self.side_lock = self.side_lock or asyncio.Lock()
        async with self.side_lock:
            try:
                await self._btw(job)
            except Exception as e:
                logger.exception("alternative-agents: btw failed")
                await self.send(f"❌ alternative-agents btw: {e}", reply_to=job.message_id, side=True)
            finally:
                self.side_run = None
                if job.message_id:
                    await react(job.message_id, None)

    async def _btw(self, job):
        backend, store = self.backend, self.store
        state = store.load()
        conv = None if job.new_conversation else state.get("btw_conversation_id")
        run = self.side_run = Run([job], job.model or store.model(state), job.effort or store.effort(state), side=True)
        if job.message_id and REACT_WORKING:
            await react(job.message_id, REACT_WORKING)
        message, files = await self._compose([job], False)
        fork = False
        if not conv:
            main = state.get("conversation_id")
            if backend.can_fork and main:
                conv, fork = main, True
                message = BTW_FORK_HEAD.format(user=workspace.USER) + message
            else:
                recap = await asyncio.to_thread(store.recap, main, BTW_RECAP_TURNS, workspace.USER, self.label)
                message = BTW_HEAD.format(user=workspace.USER, agent=self.label, recap=recap or "(none)") + message
        prompt = await asyncio.to_thread(backend.prompt, run, message, conv)
        typing = asyncio.get_running_loop().create_task(_typing_loop())
        try:
            await self._exec(run, prompt, conv, fork)
        finally:
            typing.cancel()
        if run.conversation_id:
            store.update(btw_conversation_id=run.conversation_id)
        await asyncio.to_thread(backend.finished, run)
        body, attachments = self._reply(run, False, label="btw")
        await self.deliver(body, attachments, job.message_id, side=True)
        await asyncio.to_thread(store.log_turn, {
            "btw": True, "model": run.model, "conversation_id": run.conversation_id, "message": message[-500:],
            "response": ((run.result or {}).get("response") or "")[:2000],
            "status": "STOPPED" if run.stopped else (run.result or {}).get("status", "NO_RESULT"),
            "seconds": int(time.time() - run.started), "files": [p for p, _ in files], "tokens": run.tokens,
            **({"cost_usd": run.cost} if run.cost else {})})

    # ── voice replies ──

    def _speaks(self, state, jobs):
        mode = self.store.voice_reply(state)
        return mode == "always" or (mode == "mirror" and any(job.voice for job in jobs))

    async def _speak(self, text, reply_to=None):
        """Read the answer aloud and send it as a voice note."""
        if not text:
            return
        try:
            path = await (_edge_voice(text, self.backend.workspace.outbox) if cfg["tts_provider"] == "edge"
                          else asyncio.to_thread(_hermes_voice, text))
        except Exception:
            logger.exception("alternative-agents: TTS failed")
            return
        ad = adapter()
        if not path or ad is None:
            return
        try:
            res = await ad.send_voice(TARGET_CHAT, path, reply_to=reply_to)
        except Exception:
            logger.exception("alternative-agents: cannot send the voice reply")
            return
        if res is not None and res.success:
            self._remember([res.message_id])


# ── shared helpers ──

def _base_env():
    # The gateway process holds every API key in os.environ; don't hand them to the agent.
    try:
        from tools.environments.local import build_subprocess_env
    except ImportError:  # outside Hermes (scheduled tasks run in Hermes' venv, so rarely)
        return dict(os.environ)
    return build_subprocess_env()


def _transcribe(path):
    try:
        from tools.transcription_tools import transcribe_audio
        result = transcribe_audio(path, None, "gateway")
    except Exception:
        logger.exception("alternative-agents: transcription failed")
        return ""
    return (result.get("transcript") or "").strip() if result.get("success") else ""


def _kill(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _typing_loop():
    while True:
        ad = adapter()
        if ad is not None:
            try:
                await ad.send_typing(TARGET_CHAT)
            except Exception:
                logger.debug("alternative-agents: typing failed", exc_info=True)
        await asyncio.sleep(TYPING_EVERY_S)


def clean(text):
    # file:// links are dead ends on a phone: keep the label. MEDIA: lines keep their absolute path.
    lines = [line if line.lstrip().startswith("MEDIA:") else _home(_FILE_LINK.sub(r"\1", line))
             for line in text.splitlines()]
    return "\n".join(lines).strip()


def _send_standalone(text):
    try:
        from tools.send_message_tool import send_message_tool
        out = json.loads(send_message_tool({"action": "send", "target": TARGET, "message": text}))
        if out.get("success"):
            return out.get("message_id")
        logger.warning("alternative-agents: send_message failed: %s", str(out)[:500])
    except Exception:
        logger.exception("alternative-agents: send_message raised")
    return None


async def _edit(message_id, text, final=True):
    ad = adapter()
    if ad is None or message_id is None:
        return
    try:
        await ad.edit_message(TARGET_CHAT, message_id, text, finalize=final)
    except Exception:
        logger.debug("alternative-agents: edit failed", exc_info=True)


async def react(message_id, emoji):
    """Set (None: clear) the bot's reaction on one of the user's messages."""
    ad = adapter()
    # The Telegram adapter has no public reaction API; _set_reaction is what its own
    # processing reactions use, and it swallows its errors.
    setter = getattr(ad, "_set_reaction", None)
    if setter is None or message_id is None:
        return
    try:
        await setter(TARGET_CHAT, message_id, emoji)
    except Exception:
        logger.debug("alternative-agents: reaction failed", exc_info=True)


def _spoken_text(response):
    """What is worth reading aloud: no MEDIA lines, links reduced to their label."""
    lines = [line for line in response.splitlines() if not line.lstrip().startswith("MEDIA:")]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", "\n".join(lines)).strip()
    return text[:MAX_SPOKEN_CHARS]


def _hermes_voice(text):
    """Hermes' TTS, with its configured provider (it ignores per-call provider overrides)."""
    from tools.tts_tool import text_to_speech_tool
    out = json.loads(text_to_speech_tool(text=text))
    if not out.get("success"):
        logger.warning("alternative-agents: Hermes TTS failed: %s", str(out.get("error") or out)[:300])
    return out.get("file_path") if out.get("success") else None


async def _edge_voice(text, outbox):
    """Edge TTS (free) straight from the edge_tts package Hermes ships, as an OGG/Opus voice note."""
    import edge_tts
    try:
        from tools.tts_text_normalize import prepare_spoken_text
        text = prepare_spoken_text(text, max_chars=None)
    except Exception:
        pass
    outbox.mkdir(parents=True, exist_ok=True)
    base = outbox / f"voice-{datetime.now():%Y%m%d-%H%M%S}"
    mp3, ogg = base.with_suffix(".mp3"), base.with_suffix(".ogg")
    await edge_tts.Communicate(text, _edge_voice_name()).save(str(mp3))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k", str(ogg),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _out, err = await proc.communicate()
    if proc.returncode != 0:  # no ffmpeg/opus: Telegram still plays the MP3, as an audio file
        logger.warning("alternative-agents: ffmpeg failed: %s", err.decode(errors="replace")[:300])
        return str(mp3)
    mp3.unlink(missing_ok=True)
    return str(ogg)


def _edge_voice_name():
    if cfg["tts_voice"]:
        return cfg["tts_voice"]
    try:
        import yaml
        tts = (yaml.safe_load((workspace.HERMES_HOME / "config.yaml").read_text()) or {}).get("tts") or {}
        return (tts.get("edge") or {}).get("voice") or "en-US-AriaNeural"
    except Exception:
        return "en-US-AriaNeural"


def _home(text):
    return text.replace(str(Path.home()), "~")


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _elapsed(started):
    secs = int(time.time() - started)
    return f"{secs // 60} min {secs % 60:02d}" if secs >= 60 else f"{secs}s"
