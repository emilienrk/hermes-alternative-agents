"""alternative-agents tests: routing, and whole turns against fake_agy.py, fake_claude.py and a fake Telegram adapter.

Run with the gateway's interpreter (it imports Hermes' gateway modules):
  <hermes venv python> tests/test_chat.py
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "hermes-agent"))


def _load_plugin():
    spec = importlib.util.spec_from_file_location("alt_agents_under_test", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["alt_agents_under_test"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
engine = sys.modules["alt_agents_under_test.engine"]
workspace = sys.modules["alt_agents_under_test.workspace"]
config = sys.modules["alt_agents_under_test.config"]
backends = sys.modules["alt_agents_under_test.backends"]
CHAT = "424242"
SETTINGS = {"chat_id": CHAT, "user_name": "Alex", "backends": ["agy", "claude"]}
FAKES = {"agy": str(TESTS_DIR / "fake_agy.py"), "claude": str(TESTS_DIR / "fake_claude.py")}


def ok(message_id=None):
    return SimpleNamespace(success=True, message_id=message_id, continuation_message_ids=(), error=None)


class FakeBot:
    def __init__(self, adapter):
        self.adapter = adapter

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.adapter.calls.append(("bot_send", text, reply_markup))
        return SimpleNamespace(message_id=int(self.adapter._new_id()))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        self.adapter.calls.append(("pin", message_id, disable_notification))

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.adapter.calls.append(("unpin", message_id))

    async def delete_message(self, chat_id, message_id):
        self.adapter.calls.append(("delete", message_id))


class FakeAdapter:
    def __init__(self):
        self.calls, self.next_id = [], 1000
        self._bot = FakeBot(self)

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(("send", content, reply_to))
        return ok(self._new_id())

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.calls.append(("edit", message_id, content, finalize))
        return ok(message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.calls.append(("typing",))

    async def _set_reaction(self, chat_id, message_id, emoji):
        self.calls.append(("react", str(message_id), emoji))
        return True

    async def _file(self, kind, path, reply_to):
        self.calls.append((kind, path, reply_to))
        return ok(self._new_id())

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("image", image_path, reply_to)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kw):
        return await self._file("document", file_path, reply_to)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("video", video_path, reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("voice", audio_path, reply_to)

    async def send_choice_picker(self, chat_id, title, choices, session_key, on_choice_selected, metadata=None):
        self.calls.append(("picker", title, choices))
        self.on_choice = on_choice_selected
        return ok(self._new_id())

    def sent(self, kind="send"):
        return [c for c in self.calls if c[0] == kind]


def event(text="", media=(), types=(), mtype="text", msg_id="1", reply_to=None, reply_text=None, chat=CHAT):
    return SimpleNamespace(
        text=text, media_urls=list(media), media_types=list(types), message_type=SimpleNamespace(value=mtype),
        message_id=msg_id, reply_to_message_id=reply_to, reply_to_text=reply_text, internal=False,
        allow_gateway_control=True, source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alternative-agents-test-"))
        self.cfg_saved = dict(config.cfg)
        self.saved = {(module, name): getattr(module, name) for module, name in (
            (engine, "TYPING_EVERY_S"), (engine, "STATUS_EVERY_S"), (engine, "_transcribe"),
            (workspace, "HERMES_HOME"))}
        plugin.configure(lambda key, default=None: SETTINGS.get(key, default))
        hermes = self.tmp / "hermes"
        (hermes / "memories").mkdir(parents=True)
        (hermes / "memories" / "MEMORY.md").write_text("likes tea")
        workspace.HERMES_HOME = hermes
        for name, agent in engine.AGENTS.items():
            backend = agent.backend
            backend.bin = FAKES[name]
            backend.workspace = type(backend.workspace)(backend, self.tmp / name)
            backend.workspace.prepare = lambda: {}
            backend.store.state_file = self.tmp / f"{name}-state.json"
            backend.store.turns_log = self.tmp / f"{name}-turns.log"
        self.agy, self.claude = engine.AGENTS["agy"], engine.AGENTS["claude"]
        engine.TYPING_EVERY_S, engine.STATUS_EVERY_S = 0.2, 0.3
        engine._transcribe = lambda path: "transcription du vocal"
        for f in ("calls.jsonl", "mode.txt", "media.txt"):
            (TESTS_DIR / f).unlink(missing_ok=True)
        self.adapter = FakeAdapter()
        engine._rt.update(adapter=self.adapter, gateway=None, loop=asyncio.get_running_loop(), notice_sent=False)

    async def asyncTearDown(self):
        for agent in engine.AGENTS.values():
            if agent.worker and not agent.worker.done():
                agent.stop()
                await asyncio.wait_for(agent.worker, 15)
        for (module, name), value in self.saved.items():
            setattr(module, name, value)
        config.cfg.clear()
        config.cfg.update(self.cfg_saved)
        for f in ("calls.jsonl", "mode.txt", "media.txt"):
            (TESTS_DIR / f).unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mode(self, mode):
        (TESTS_DIR / "mode.txt").write_text(mode)

    def calls(self):
        path = TESTS_DIR / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def dispatch(self, ev):
        return await plugin._on_dispatch(event=ev)

    async def finish(self, timeout=20, agent=None):
        await asyncio.wait_for((agent or self.agy).worker, timeout)

    def set(self, **values):
        config.cfg.update(values)
        engine.configure({name: agent.backend for name, agent in engine.AGENTS.items()})
        self.agy, self.claude = engine.AGENTS["agy"], engine.AGENTS["claude"]


class RoutingTests(Base):
    def route(self, ev, agy=None, claude=None):
        return plugin.route(ev, {"agy": agy or {}, "claude": claude or {}})

    def test_commands(self):
        kind, agent, job = self.route(event("/agy liste les fichiers"))
        self.assertEqual((kind, agent, job.text), ("job", self.agy, "liste les fichiers"))
        self.assertEqual(self.route(event("/agy@HermesBot liste"))[2].text, "liste")
        self.assertTrue(self.route(event("/agy_new on repart"))[2].new_conversation)
        self.assertEqual(self.route(event("/cc liste"))[1], self.claude)
        self.assertTrue(self.route(event("/cc_new on repart"))[2].new_conversation)
        self.assertIsNone(self.route(event("/agy")))  # status card: the command handler answers
        self.assertIsNone(self.route(event("/cc")))
        self.assertIsNone(self.route(event("/agy_model")))
        self.assertIsNone(self.route(event("/new")))

    def test_sticky_mode(self):
        sticky = {"sticky": True}
        self.assertEqual(self.route(event("salut"), agy=sticky)[2].text, "salut")
        self.assertEqual(self.route(event("salut"), claude=sticky)[1], self.claude)
        self.assertIsNone(self.route(event("Hermes, fais le point"), agy=sticky))
        self.assertIsNone(self.route(event("Hermès : ça va ?"), claude=sticky))
        self.assertIsNone(self.route(event("/status"), agy=sticky))
        self.assertEqual(self.route(event("/new"), claude=sticky), ("new", self.claude, None))
        self.assertIsNone(self.route(event("/stop"), agy=sticky))  # nothing running: Hermes' /stop
        self.agy.run = object()
        self.assertEqual(self.route(event("/stop"), agy=sticky), ("stop", self.agy, None))

    def test_reply_to_an_agent(self):
        state = {"msgs": ["55"]}
        job = self.route(event("et maintenant ?", reply_to="55", reply_text="réponse d'agy"), agy=state)[2]
        self.assertEqual((job.text, job.quoted), ("et maintenant ?", ""))
        self.assertIsNone(self.route(event("et maintenant ?", reply_to="56"), agy=state))
        quoted = self.route(event("/agy fais ça", reply_to="56", reply_text="le plan de Hermes"), agy=state)[2]
        self.assertEqual(quoted.quoted, "le plan de Hermes")
        # In agy mode, a reply to Claude's message still goes to Claude.
        kind, agent, _job = self.route(event("et toi ?", reply_to="77"), agy={"sticky": True}, claude={"msgs": ["77"]})
        self.assertEqual((kind, agent), ("job", self.claude))
        self.assertEqual(self.route(event("vu", reply_to="9"), agy={"agy_msgs": ["9"]})[1], self.agy)  # older state

    def test_media_voice_and_inlined_documents(self):
        doc = self.tmp / "notes.txt"
        doc.write_text("ligne 1\nligne 2")
        text = f"[Content of notes.txt]:\nligne 1\nligne 2\n\n/agy résume\n[document 'notes.txt' saved at: {doc}]"
        job = self.route(event(text, media=[doc], types=["text/plain"], mtype="document"))[2]
        self.assertEqual((job.text, job.files), ("résume", [(str(doc), "text/plain")]))
        voice = self.route(event("", media=["/c/v.ogg"], types=["audio/ogg"], mtype="voice"), agy={"sticky": True})[2]
        self.assertEqual((voice.voice, voice.files), (["/c/v.ogg"], []))
        self.assertIsNone(self.route(event("", media=["/c/v.ogg"], mtype="voice")))
        photo = self.route(event("/agy", media=["/c/p.jpg"], types=["image/jpeg"], mtype="photo"))[2]
        self.assertEqual(photo.files, [("/c/p.jpg", "image/jpeg")])

    async def test_dispatch_ignores_other_chats(self):
        self.assertIsNone(await self.dispatch(event("/agy salut", chat="42")))
        self.assertIsNone(self.agy.worker)

    async def test_nothing_routed_without_chat_id(self):
        engine.TARGET_CHAT = ""
        try:
            self.assertIsNone(await self.dispatch(event("/agy salut", chat="")))
            self.assertIsNone(await self.dispatch(event("/agy salut")))
        finally:
            engine.TARGET_CHAT = CHAT
        self.assertIsNone(self.agy.worker)

    async def test_disabled_backend_is_not_routed(self):
        self.set(backends=["agy"])
        engine.AGENTS.pop("claude")
        self.assertIsNone(plugin.route(event("/cc salut"), {"agy": {}}))


class TurnTests(Base):
    async def test_basic_turn(self):
        store = self.agy.store
        result = await self.dispatch(event("/agy liste mes projets", msg_id="7"))
        self.assertEqual(result, {"action": "skip", "reason": "alternative-agents"})
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("ECHO:", reply[1])
        self.assertIn("liste mes projets", reply[1])
        self.assertIn("Voir main.py.", reply[1])  # file:// link reduced to its label
        self.assertIn("— 🛸 agy", reply[1])
        self.assertNotIn("run_command", reply[1])  # discreet mode hides actions
        self.assertEqual(reply[2], "7")
        self.assertIn(("react", "7", "👀"), self.adapter.calls)
        self.assertEqual(self.adapter.calls[-1], ("react", "7", None))
        self.assertTrue(self.adapter.sent("typing"))
        state = store.load()
        self.assertTrue(state["conversation_id"])
        self.assertNotIn("inflight", state)
        self.assertTrue(self.agy.owns(state, int(self.adapter.next_id)))
        call = self.calls()[0]
        self.assertIsNone(call["conversation"])
        self.assertIn("Context:", call["prompt"])  # first turn carries the rules pointer
        log = [json.loads(line) for line in store.turns_log.read_text().splitlines()]
        self.assertEqual((log[-1]["status"], log[-1]["tokens"]), ("SUCCESS", 54321))

        await self.dispatch(event("/agy et ensuite ?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], state["conversation_id"])
        self.assertNotIn("Context:", second["prompt"])

    async def test_reply_threading_reaches_agy(self):
        await self.dispatch(event("/agy salut", msg_id="7"))
        await self.finish()
        agy_id = str(self.adapter.next_id)
        result = await self.dispatch(event("précise", msg_id="9", reply_to=agy_id))
        self.assertEqual(result["action"], "skip")
        await self.finish()
        self.assertTrue(self.calls()[-1]["prompt"].endswith("précise"))

    async def test_queue_merges_messages(self):
        self.mode("verbose")
        await self.dispatch(event("/agy tâche longue", msg_id="1"))
        await asyncio.sleep(0.5)
        await self.dispatch(event("/agy et aussi ça", msg_id="2"))
        await self.dispatch(event("/agy et ça", msg_id="3"))
        self.assertIn(("react", "2", "✍"), self.adapter.calls)
        self.mode("ok")
        await self.finish(40)
        prompts = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(prompts), 2)
        self.assertIn("Several messages from Alex", prompts[1])
        self.assertIn("1. et aussi ça", prompts[1])
        self.assertIn("2. et ça", prompts[1])
        self.assertEqual(self.adapter.sent()[-1][2], "3")

    async def test_stop_kills_the_turn(self):
        self.mode("slow")
        await self.dispatch(event("/agy attends", msg_id="1"))
        await asyncio.sleep(1.5)
        started = time.time()
        self.assertEqual(await plugin._cmd_stop(self.agy, ""), "🛑 Stop requested.")
        await self.finish()
        self.assertLess(time.time() - started, 8)
        self.assertIn("🛑 Stopped.", self.adapter.sent()[-1][1])
        self.assertEqual(await plugin._cmd_stop(self.agy, ""), "Nothing running.")

    async def test_stop_while_transcribing_never_starts_agy(self):
        self.agy.store.update(sticky=True, voice="local")
        engine._transcribe = lambda path: time.sleep(1.5) or "vocal"
        await self.dispatch(event("", media=[self.tmp / "v.ogg"], types=["audio/ogg"], mtype="voice", msg_id="1"))
        await asyncio.sleep(0.5)
        self.assertEqual(await plugin._cmd_stop(self.agy, ""), "🛑 Stop requested.")
        await self.finish()
        self.assertEqual(self.calls(), [])
        self.assertIn("🛑 Stopped.", self.adapter.sent()[-1][1])

    async def test_launch_failure_cleans_up(self):
        self.agy.backend.bin = str(self.tmp / "missing-agy")
        await self.dispatch(event("/agy salut", msg_id="7"))
        await self.finish()
        self.assertIn("❌ alternative-agents", self.adapter.sent()[-1][1])
        self.assertNotIn("inflight", self.agy.store.load())
        self.assertEqual(self.adapter.calls[-1], ("react", "7", None))
        self.assertIsNone(self.agy.run)

    async def test_saturated_conversation_restarts_fresh(self):
        store = self.agy.store
        store.update(conversation_id="old-conv")
        store.log_turn({"conversation_id": "old-conv", "message": "avant", "response": "réponse d'avant"})
        self.mode("saturated")
        await self.dispatch(event("/agy continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["old-conv", None])
        self.assertIn("réponse d'avant", calls[1]["prompt"])
        reply = self.adapter.sent()[-1][1]
        self.assertIn("♻️", reply)
        self.assertIn("ECHO:", reply)
        self.assertNotEqual(store.load()["conversation_id"], "old-conv")

    async def test_crash_reports_error_and_retry_works(self):
        self.mode("crash")
        await self.dispatch(event("/agy plante", msg_id="1"))
        await self.finish()
        reply = self.adapter.sent()[-1][1]
        self.assertIn("❌", reply)
        self.assertIn("panic: something broke", reply)
        self.assertIn("/agy_retry", reply)
        self.mode("ok")
        self.assertEqual(await plugin._cmd_retry(self.agy, ""), "🔁 Sent to agy again.")
        await self.finish()
        self.assertIn("plante", self.calls()[-1]["prompt"])

    async def test_media_delivery(self):
        image, doc = self.tmp / "out.png", self.tmp / "rapport.pdf"
        image.write_bytes(b"png")
        doc.write_bytes(b"pdf")
        (TESTS_DIR / "media.txt").write_text(
            f"MEDIA:{image}\nMEDIA:{doc}\nMEDIA:{self.tmp}/absent.png\nMEDIA:{Path.home()}/.hermes/.env")
        self.mode("media")
        await self.dispatch(event("/agy envoie", msg_id="4"))
        await self.finish()
        self.assertEqual(self.adapter.sent("image"), [("image", str(image), "4")])
        self.assertEqual(self.adapter.sent("document"), [("document", str(doc), "4")])
        warnings = [c[1] for c in self.adapter.sent() if c[1].startswith("⚠️")]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("absent.png", warnings[0])
        # Hermes never extracts an extension-less path it refuses: the line stays as text, nothing is sent.
        self.assertIn(".hermes/.env", self.adapter.sent()[0][1])
        self.assertNotIn(f"MEDIA:{image}", self.adapter.sent()[0][1])

    async def test_verbose_status_and_summary(self):
        self.agy.store.update(verbose=True)
        self.mode("verbose")
        await self.dispatch(event("/agy build", msg_id="5"))
        await self.finish(30)
        status = self.adapter.sent()[0]
        self.assertTrue(status[1].startswith("🛸 agy working"))
        self.assertIn("▶ run_command ls ~/projects", status[1])
        self.assertIn("💬 Je regarde.", status[1])
        final_edit = [c for c in self.adapter.calls if c[0] == "edit" and c[3]][-1]
        self.assertIn("🔧 1 actions", final_edit[2])
        self.assertIn("54k tokens", final_edit[2])
        self.assertIn("gemini-3.8-flash-high", self.adapter.sent()[-1][1])

    async def test_long_reply_goes_to_file(self):
        self.mode("long")
        await self.dispatch(event("/agy long", msg_id="6"))
        await self.finish()
        text = self.adapter.sent()[-1][1]
        self.assertIn("[…continued in the attached file]", text)
        self.assertLess(len(text), engine.MAX_REPLY_CHARS + 200)
        attached = self.adapter.sent("document")[0][1]
        self.assertGreater(len(Path(attached).read_text()), 15000)

    async def test_voice_note_in_sticky_mode(self):
        self.agy.store.update(sticky=True)
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="3"))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("view_file", prompt)
        self.assertIn("audio/ogg", prompt)

    async def test_voice_note_local_mode(self):
        self.agy.store.update(sticky=True, voice="local")
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="4"))
        await self.finish()
        self.assertIn("transcription du vocal", self.calls()[0]["prompt"])

    async def test_command_voice(self):
        self.assertIn("direct", await plugin._cmd_voice(self.agy, "direct"))
        self.assertEqual(self.agy.store.voice_mode(self.agy.store.load()), "direct")
        self.assertIn("local", await plugin._cmd_voice(self.agy, "local"))
        self.assertEqual(self.agy.store.voice_mode(self.agy.store.load()), "local")

    async def test_attachments_are_copied_to_inbox(self):
        photo = self.tmp / "photo.jpg"
        photo.write_bytes(b"jpg")
        await self.dispatch(event("/agy c'est quoi ?", media=[photo], types=["image/jpeg"], mtype="photo"))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("Files attached by Alex", prompt)
        copied = list(self.agy.backend.workspace.inbox.iterdir())
        self.assertEqual(len(copied), 1)
        self.assertIn(str(copied[0]), prompt)

    async def test_huge_message_goes_through_a_file(self):
        await self.dispatch(event("/agy " + "a" * 150_000))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("too long for the command line", prompt)
        self.assertLess(len(prompt), 1000)

    async def test_restart_notice(self):
        self.agy.store.update(inflight={"message": "relance best-stuff", "at": "2026-10-06T21:40:00"})
        engine.schedule_startup(delay=0.1)
        await asyncio.sleep(0.5)
        notice = self.adapter.sent()[-1][1]
        self.assertIn("relance best-stuff", notice)
        self.assertIn("/agy_retry", notice)
        self.assertNotIn("inflight", self.agy.store.load())


class ClaudeTests(Base):
    async def finish(self, timeout=20, agent=None):
        await super().finish(timeout, agent or self.claude)

    async def test_turn_and_resume(self):
        store = self.claude.store
        await self.dispatch(event("/cc liste mes projets", msg_id="7"))
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("ECHO:", reply[1])
        self.assertIn("Voir main.py.", reply[1])
        self.assertIn("— ✳️ Claude", reply[1])
        self.assertEqual(reply[2], "7")
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(args[args.index("--setting-sources") + 1], "")  # none of the user's own setup
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("AskUserQuestion", args[args.index("--disallowedTools") + 1])
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertNotIn("--effort", args)
        self.assertEqual(call["env"], {"ANTHROPIC_API_KEY": None, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertIsNone(call["conversation"])
        prompt = call["prompt"]  # through stdin
        self.assertTrue(prompt.startswith("[Context from the plugin]\nNow: "))
        self.assertIn("likes tea", prompt)
        self.assertTrue(prompt.endswith("liste mes projets"))
        state = store.load()
        conv = state["conversation_id"]
        self.assertEqual(state["context"]["conversation_id"], conv)
        self.assertTrue(self.claude.owns(state, int(self.adapter.next_id)))
        self.assertFalse(self.agy.owns(self.agy.store.load(), int(self.adapter.next_id)))
        log = json.loads(store.turns_log.read_text().splitlines()[-1])
        self.assertEqual((log["status"], log["tokens"], log["cost_usd"]), ("SUCCESS", 51004, 0.25))

        await self.dispatch(event("/cc et ensuite ?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], conv)
        self.assertFalse(second["fork"])
        self.assertNotIn("likes tea", second["prompt"])  # memory unchanged: not sent again
        (workspace.HERMES_HOME / "memories" / "MEMORY.md").write_text("likes coffee now")
        await self.dispatch(event("/cc et là ?", msg_id="9"))
        await self.finish()
        self.assertIn("likes coffee now", self.calls()[2]["prompt"])

    async def test_effort_model_and_usage(self):
        self.assertIsNone(await plugin._cmd_effort(self.claude, ""))  # picker
        choices = self.adapter.sent("picker")[0][2]
        self.assertEqual([c["label"] for c in choices], list(config.EFFORTS))
        self.assertEqual(await plugin._cmd_effort(self.claude, "low"), "Claude effort → low")
        self.assertIn("Unknown model", await plugin._cmd_model(self.claude, "gpt-9"))
        self.assertEqual(await plugin._cmd_model(self.claude, "sonnet"), "Claude model → sonnet")
        self.assertEqual(await plugin._cmd_model(self.claude, "claude-opus-5-5"), "Claude model → claude-opus-5-5")
        self.assertIn("unknown until the next turn", await plugin._cmd_usage(self.claude, ""))
        await self.dispatch(event("/cc salut", msg_id="7"))
        await self.finish()
        args = self.calls()[0]["args"]
        self.assertEqual(args[args.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(args[args.index("--effort") + 1], "low")
        usage = await plugin._cmd_usage(self.claude, "")
        self.assertIn("5 h 58%", usage)
        self.assertIn("week 68%", usage)
        self.assertIn("1 turns · 51k tokens ($0.25 at API prices)", usage)

    async def test_verbose_status_shows_tools(self):
        self.claude.store.update(verbose=True)
        self.mode("verbose")
        await self.dispatch(event("/cc build", msg_id="5"))
        await self.finish(30)
        status = self.adapter.sent()[0]
        self.assertTrue(status[1].startswith("✳️ Claude working"))
        self.assertIn("▶ Bash ls ~/projects", status[1])
        self.assertIn("💬 Je regarde.", status[1])
        final_edit = [c for c in self.adapter.calls if c[0] == "edit" and c[3]][-1]
        self.assertIn("🔧 1 actions", final_edit[2])
        self.assertIn("✓ Bash ls ~/projects", final_edit[2])

    async def test_lost_conversation_restarts_fresh(self):
        store = self.claude.store
        store.update(conversation_id="gone")
        store.log_turn({"conversation_id": "gone", "message": "avant", "response": "réponse d'avant"})
        self.mode("missing")
        await self.dispatch(event("/cc continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["gone", None])
        self.assertIn("réponse d'avant", calls[1]["prompt"])
        self.assertIn("♻️ Conversation lost or too long", self.adapter.sent()[-1][1])
        self.assertNotEqual(store.load()["conversation_id"], "gone")

    async def test_quota_hint(self):
        self.mode("quota")
        await self.dispatch(event("/cc salut", msg_id="1"))
        await self.finish()
        reply = self.adapter.sent()[-1][1]
        self.assertIn("❌ Claude ERROR: You've hit your limit", reply)
        self.assertIn("/cc_usage", reply)
        self.assertEqual(len(self.calls()), 1)  # not retried

    async def test_voice_is_always_transcribed(self):
        self.claude.store.update(sticky=True)
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="3"))
        await self.finish()
        self.assertIn("transcription du vocal", self.calls()[0]["prompt"])
        self.assertIn("takes no audio", await plugin._cmd_voice(self.claude, "direct"))

    async def test_btw_forks_the_main_conversation(self):
        await self.dispatch(event("/cc travail", msg_id="1"))
        await self.finish()
        main = self.claude.store.load()["conversation_id"]
        await self.dispatch(event("/cc_btw vite ?", msg_id="2"))
        await BtwTests.finish_btw(self)
        btw = self.calls()[-1]
        self.assertEqual((btw["conversation"], btw["fork"]), (main, True))
        args = btw["args"]
        self.assertEqual(args[args.index("--effort") + 1], "low")
        self.assertIn("Side question", btw["prompt"])
        state = self.claude.store.load()
        self.assertEqual(state["conversation_id"], main)
        self.assertNotEqual(state["btw_conversation_id"], main)
        self.assertIn("— ✳️ Claude · btw", self.adapter.sent()[-1][1])
        # A reply to the btw answer continues the side conversation, without forking again.
        await self.dispatch(event("et donc ?", msg_id="3", reply_to=state["btw_msgs"][-1]))
        await BtwTests.finish_btw(self)
        last = self.calls()[-1]
        self.assertEqual((last["conversation"], last["fork"]), (state["btw_conversation_id"], False))

    async def test_one_mode_at_a_time(self):
        self.assertIsNone(await plugin._cmd_on(self.agy, ""))
        agy_pin = self.agy.store.load()["mode_pin"]
        self.assertIsNone(await plugin._cmd_on(self.claude, ""))
        self.assertNotIn("sticky", self.agy.store.load())
        self.assertIn(("unpin", agy_pin), self.adapter.calls)
        self.assertTrue(self.adapter.sent("bot_send")[-1][1].startswith("✳️ Claude mode on · /cc_off"))
        self.assertIn("Claude mode is on", await plugin._cmd_status(self.agy, ""))
        result = await self.dispatch(event("salut", msg_id="4"))
        self.assertEqual(result["action"], "skip")
        await self.finish()
        self.assertEqual(self.calls()[0]["agent"], "claude")

    async def test_handoff_and_back(self):
        await plugin._cmd_on(self.agy, "")
        saved, workspace.hermes_thread = workspace.hermes_thread, lambda limit: "- Alex: plan the trip"
        try:
            await self.dispatch(event("/cc_handoff continue", msg_id="5"))
            await self.finish()
        finally:
            workspace.hermes_thread = saved
        self.assertIn("plan the trip", self.calls()[0]["prompt"])
        self.assertTrue(self.claude.store.load()["sticky"])
        self.assertNotIn("sticky", self.agy.store.load())
        result = await self.dispatch(event("/cc_back and now?", msg_id="6"))
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("[Handover from Claude (Claude Code)", result["text"])
        self.assertIn("ECHO:", result["text"])
        self.assertNotIn("sticky", self.claude.store.load())

    async def test_doctor(self):
        self.claude.backend.workspace.mcp_config.parent.mkdir(parents=True, exist_ok=True)
        self.claude.backend.workspace.mcp_config.write_text(json.dumps({"mcpServers": {}}))
        report = await plugin._cmd_doctor(self.claude, "")
        self.assertIn("✓ Claude Code 9.9.9", report)
        self.assertIn("✓ Claude Code logged in (claude.ai, pro plan)", report)


class CommandTests(Base):
    async def test_status_card_and_modes(self):
        card = await plugin._cmd_status(self.agy, "")
        self.assertIn("Hermes mode", card)
        self.assertIsNone(await plugin._cmd_on(self.agy, ""))
        self.assertTrue(self.agy.store.load()["sticky"])
        _kind, banner, _markup = self.adapter.sent("bot_send")[-1]
        self.assertTrue(banner.startswith("🛸 agy mode on"))
        pin_id = self.agy.store.load()["mode_pin"]
        self.assertEqual(self.adapter.sent("pin"), [("pin", pin_id, True)])
        self.assertEqual(await plugin._cmd_on(self.agy, ""), "🛸 agy mode already on.")
        self.assertEqual(len(self.adapter.sent("pin")), 1)
        self.assertIn("agy mode:", await plugin._cmd_status(self.agy, ""))
        self.assertIn("Back to Hermes", await plugin._cmd_off(self.agy, ""))
        self.assertNotIn("sticky", self.agy.store.load())
        self.assertNotIn("mode_pin", self.agy.store.load())
        self.assertEqual((self.adapter.sent("unpin"), self.adapter.sent("delete")),
                         ([("unpin", pin_id)], [("delete", pin_id)]))

    async def test_banner_follows_state_at_startup(self):
        self.agy.store.update(sticky=True)  # mode on, no banner yet (e.g. state from an older version)
        engine.schedule_startup(delay=0.1)
        await asyncio.sleep(0.5)
        self.assertTrue(self.agy.store.load()["mode_pin"])
        self.assertEqual(len(self.adapter.sent("pin")), 1)

    async def test_model_picker(self):
        self.agy.backend._models_cache.update(at=0.0, models=[])
        self.assertIsNone(await plugin._cmd_model(self.agy, ""))
        title, choices = self.adapter.sent("picker")[0][1:]
        self.assertEqual([c["value"] for c in choices], ["gemini-3.8-flash-high", "claude-opus-4-6-thinking"])
        self.assertEqual(await self.adapter.on_choice(CHAT, "claude-opus-4-6-thinking"),
                         "agy model → claude-opus-4-6-thinking")
        self.assertEqual(self.agy.store.load()["model"], "claude-opus-4-6-thinking")
        self.assertEqual(await plugin._cmd_model(self.agy, "1"), "agy model → gemini-3.8-flash-high")
        self.assertIn("Unknown", await plugin._cmd_model(self.agy, "gpt-9"))

    async def test_verbose(self):
        self.assertIn("Detailed", await plugin._cmd_verbose(self.agy, ""))
        self.assertIn("Discreet", await plugin._cmd_verbose(self.agy, ""))

    async def test_conversations(self):
        store = self.agy.store
        self.assertEqual(await plugin._cmd_conv(self.agy, ""), "No agy conversation recorded.")
        store.log_turn({"conversation_id": "c1", "message": "premier sujet"})
        store.log_turn({"conversation_id": "c2", "message": "second sujet"})
        self.assertIsNone(await plugin._cmd_conv(self.agy, ""))
        choices = self.adapter.sent("picker")[0][2]
        self.assertEqual([c["value"] for c in choices], ["c2", "c1"])
        self.assertIn("premier sujet", await plugin._cmd_conv(self.agy, "2"))
        self.assertEqual(store.load()["conversation_id"], "c1")

    def test_commands_registered_per_backend(self):
        names = []
        ctx = SimpleNamespace(get_config=lambda key, default=None: SETTINGS.get(key, default),
                              register_command=lambda name, **kw: names.append(name),
                              register_hook=lambda *a: None, register_telegram_handler=lambda *a: None)
        plugin.register(ctx)
        self.assertIn("agy-btw", names)
        self.assertIn("cc", names)
        self.assertIn("cc-handoff", names)
        self.assertEqual(len(names), 2 * len(plugin.COMMANDS))


class SettingsTests(Base):
    async def test_no_reactions_no_footer(self):
        self.set(react_working="", footer=False, mark="")
        await self.dispatch(event("/agy salut", msg_id="7"))
        await self.finish()
        self.assertNotIn(("react", "7", "👀"), self.adapter.calls)
        self.assertNotIn("— ", self.adapter.sent()[-1][1])

    async def test_custom_mark_and_reaction(self):
        self.set(react_working="🔥")
        self.agy.backend.mark = "🤖"
        await self.dispatch(event("/agy salut", msg_id="7"))
        await self.finish()
        self.assertIn(("react", "7", "🔥"), self.adapter.calls)
        self.assertIn("— 🤖 agy", self.adapter.sent()[-1][1])

    async def test_verbose_default_from_settings(self):
        store = self.agy.store
        store.default_verbose = True
        self.assertTrue(store.verbose(store.load()))
        self.assertIn("Discreet", await plugin._cmd_verbose(self.agy, ""))
        self.assertFalse(store.verbose(store.load()))

    def test_settings_normalized(self):
        cfg = config.load(lambda key, default=None: {"backends": "claude", "claude_effort": "huge",
                                                     "bridge_tools": ["agy_task", "memory", "nope"]}.get(key, default))
        self.assertEqual((cfg["backends"], cfg["claude_effort"], cfg["bridge_tools"]),
                         (["claude"], "", ["memory", "tasks"]))

    async def test_voice_reply_mirror(self):
        spoken = []

        async def speak(text, reply_to=None):
            spoken.append(text)
        self.agy._speak = speak
        self.assertIn("voice note too", await plugin._cmd_voice(self.agy, "reply mirror"))
        self.agy.store.update(sticky=True)
        await self.dispatch(event("écris", msg_id="1"))
        await self.finish()
        self.assertEqual(spoken, [])
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="2"))
        await self.finish()
        self.assertEqual(len(spoken), 1)
        self.assertIn("ECHO:", spoken[0])
        self.assertNotIn("file://", spoken[0])
        self.assertIn("Invalid", await plugin._cmd_voice(self.agy, "reply loud"))

    async def test_spoken_text(self):
        self.assertEqual(engine._spoken_text("Voir [doc](https://x.y)\nMEDIA:/tmp/a.png\nfin"), "Voir doc\nfin")

    async def test_effort_switches_model_variant(self):
        models = [("gemini-3.8-flash-high", "Flash (High)"), ("gemini-3.8-flash-low", "Flash (Low)"),
                  ("gemini-3.1-pro-high", "Pro (High)"), ("claude-opus-4-6-thinking", "Opus")]
        self.agy.backend._models_cache.update(at=time.time(), models=models)
        self.assertIsNone(await plugin._cmd_effort(self.agy, ""))  # picker
        choices = self.adapter.sent("picker")[0][2]
        self.assertEqual([(c["label"], c["is_current"]) for c in choices], [("high", True), ("low", False)])
        self.assertEqual(await plugin._cmd_effort(self.agy, "low"), "agy model → gemini-3.8-flash-low")
        self.assertIn("No max effort", await plugin._cmd_effort(self.agy, "max"))
        self.agy.store.update(model="claude-opus-4-6-thinking")
        self.assertIn("no effort variants", await plugin._cmd_effort(self.agy, "low"))


class BtwTests(Base):
    async def finish_btw(self, timeout=20):
        for _ in range(int(timeout / 0.1)):
            await asyncio.sleep(0.1)
            tasks = [t for t in asyncio.all_tasks() if t.get_name() == "alternative-agents-btw"]
            if not tasks:
                return
        raise AssertionError("btw still running")

    async def test_btw_runs_alongside_the_main_turn(self):
        store = self.agy.store
        store.update(conversation_id="main-conv")
        store.log_turn({"conversation_id": "main-conv", "message": "refactor the parser", "response": "on it"})
        self.mode("verbose")  # the main turn takes ~9 s
        await self.dispatch(event("/agy long job", msg_id="1"))
        await asyncio.sleep(0.5)
        self.mode("ok")
        started = time.time()
        result = await self.dispatch(event("/agy_btw quick one?", msg_id="2"))
        self.assertEqual(result["action"], "skip")
        await self.finish_btw()
        self.assertLess(time.time() - started, 6)  # did not wait behind the main turn
        self.assertTrue(self.agy.busy())
        btw = [c for c in self.calls() if "Side question" in c["prompt"]][0]
        self.assertIsNone(btw["conversation"])
        self.assertIn("refactor the parser", btw["prompt"])
        reply = [c for c in self.adapter.sent() if "ECHO:" in c[1]][-1]
        self.assertIn("— 🛸 agy · btw", reply[1])
        state = store.load()
        self.assertEqual(state["conversation_id"], "main-conv")
        self.assertTrue(self.agy.owns(state, self.adapter.next_id, side=True))
        self.assertFalse(self.agy.owns(state, self.adapter.next_id))
        await self.finish(40)

        # A reply to the btw answer continues the side conversation, not the main one.
        btw_conv = store.load()["btw_conversation_id"]
        btw_msg = next(i for i in store.load()["btw_msgs"])
        await self.dispatch(event("and then?", msg_id="3", reply_to=btw_msg))
        await self.finish_btw()
        last = self.calls()[-1]
        self.assertEqual(last["conversation"], btw_conv)
        self.assertEqual(store.load()["conversation_id"], "main-conv")
        self.assertEqual([c["id"] for c in store.conversations()], ["main-conv"])

    async def test_btw_command_without_question(self):
        self.assertIn("side question", await plugin._cmd_btw(self.agy, ""))


class AgentTests(Base):
    async def test_handoff_seeds_a_new_conversation(self):
        self.agy.store.update(conversation_id="old")
        saved, workspace.hermes_thread = workspace.hermes_thread, lambda limit: "- Alex: plan the trip"
        try:
            result = await self.dispatch(event("/agy_handoff continue", msg_id="5"))
            await self.finish()
        finally:
            workspace.hermes_thread = saved
        self.assertEqual(result["action"], "skip")
        call = self.calls()[0]
        self.assertIsNone(call["conversation"])
        self.assertIn("plan the trip", call["prompt"])
        self.assertTrue(call["prompt"].rstrip().endswith("continue"))
        self.assertTrue(self.agy.store.load()["sticky"])

    async def test_back_rewrites_for_hermes(self):
        store = self.agy.store
        store.update(conversation_id="c1", sticky=True)
        store.log_turn({"conversation_id": "c1", "message": "fix the build", "response": "build fixed"})
        result = await self.dispatch(event("/agy_back and now?", msg_id="6"))
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("build fixed", result["text"])
        self.assertTrue(result["text"].endswith("and now?"))
        self.assertNotIn("sticky", store.load())
        self.assertIsNone(self.agy.worker)

    async def test_usage(self):
        usage = await plugin._cmd_usage(self.agy, "")
        self.assertIn("Gemini Models: week 73% (reset ", usage)
        self.assertIn("· 5 h 85%", usage)
        self.assertIn("No agy turn recorded.", usage)
        self.agy.store.log_turn({"model": "m1", "status": "SUCCESS", "tokens": 3000, "seconds": 90})
        self.agy.store.log_turn({"model": "m1", "status": "ERROR", "tokens": 1000, "seconds": 30, "task": "digest"})
        usage = await plugin._cmd_usage(self.agy, "")
        self.assertIn("Today: 2 turns (1 tasks) · 4k tokens · 2 min · 1 failed", usage)
        self.assertIn("m1 4k", usage)

    async def test_tasks(self):
        import importlib
        tasks = importlib.import_module("alt_agents_under_test.tasks")
        jobs, calls = {}, []

        def cron(args):
            calls.append(args)
            if args["action"] == "create":
                job_id = f"j{len(jobs) + 1}"
                jobs[job_id] = {"job_id": job_id, "schedule": args["schedule"], "next_run_at": "2026-10-11T08:00:00"}
                return {"success": True, "job": jobs[job_id]}
            if args["action"] == "list":
                return {"success": True, "jobs": list(jobs.values())}
            if args["action"] == "remove":
                jobs.pop(args["job_id"])
            return {"success": True}
        saved = (tasks._cron, tasks.HERMES_HOME, tasks._registry_path)
        tasks._cron, tasks.HERMES_HOME = cron, self.tmp
        tasks._registry_path = lambda: self.tmp / "tasks.json"
        try:
            self.assertIn("Usage", await plugin._cmd_task(self.agy, "add every day"))
            added = await plugin._cmd_task(self.agy, "add every day at 8am | résume mes mails")
            self.assertIn("scheduled", added)
            create = calls[0]
            self.assertTrue(create["no_agent"])
            self.assertEqual(create["deliver"], f"telegram:{CHAT}")
            self.assertEqual(create["name"], "agy · résume mes mails")
            script = self.tmp / "scripts" / create["script"]
            self.assertIn("task_run.py", script.read_text())
            self.assertIn("/agy_task", script.read_text())
            self.assertIn("1. résume mes mails · every day at 8am", await plugin._cmd_task(self.agy, ""))
            await plugin._cmd_task(self.claude, "add weekdays at 7am | point du jour")
            self.assertIn('"backend": "claude"', (self.tmp / "scripts" / calls[-1]["script"]).read_text())
            self.assertIn("1. point du jour", await plugin._cmd_task(self.claude, ""))
            self.assertNotIn("point du jour", await plugin._cmd_task(self.agy, ""))
            self.assertIn("removed", await plugin._cmd_task(self.agy, "rm 1"))
            self.assertFalse(script.exists())
            self.assertIn("No agy task", await plugin._cmd_task(self.agy, ""))
        finally:
            tasks._cron, tasks.HERMES_HOME, tasks._registry_path = saved

    def run_task(self, task, runner="task_run.py"):
        import subprocess
        hermes = self.tmp / "hermes"
        (hermes / "config.yaml").write_text(json.dumps({"plugins": {"entries": {"alternative-agents": {"settings": {
            "user_name": "Alex", "claude_path": FAKES["claude"], "claude_workspace": str(self.tmp / "cw")}}}}}))
        env = {**os.environ, "HERMES_HOME": str(hermes)}
        return subprocess.run([sys.executable, str(PLUGIN_DIR / runner), json.dumps(task)],
                              capture_output=True, text=True, env=env, timeout=60)

    async def test_agy_task_run(self):
        # Tasks created before Claude Code joined: agy_run.py and their own settings.
        task = {"name": "digest", "prompt": "résume", "agy_bin": FAKES["agy"], "workspace": str(self.tmp),
                "model": "m1", "timeout_minutes": 1, "user": "Alex", "mark": "🛸"}
        out = self.run_task(task, "agy_run.py")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("🛸 agy · digest\n\n"), out.stdout)
        self.assertIn("ECHO:", out.stdout)
        self.assertIn("Voir main.py.", out.stdout)
        self.assertIn("unattended", self.calls()[0]["prompt"])
        self.mode("crash")
        out = self.run_task(task, "agy_run.py")
        self.assertEqual(out.returncode, 1)
        self.assertIn("❌", out.stdout)

    async def test_task_context_and_silence(self):
        task = {"name": "health", "prompt": "check it", "agy_bin": FAKES["agy"], "workspace": str(self.tmp),
                "context_command": ["echo", "container Up"]}
        out = self.run_task(task)
        self.assertEqual(out.returncode, 0, out.stderr)
        prompt = self.calls()[0]["prompt"]
        self.assertIn("Output of `echo container Up` (exit 0)", prompt)
        self.assertIn("container Up", prompt)
        self.assertLess(prompt.index("container Up"), prompt.index("check it"))
        self.mode("silent")
        out = self.run_task(task)
        self.assertEqual((out.returncode, out.stdout), (0, ""))  # Hermes' cron delivers nothing

    async def test_claude_task_run(self):
        out = self.run_task({"backend": "claude", "name": "point", "prompt": "fais le point", "model": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("✳️ Claude · point\n\n"), out.stdout)
        call = self.calls()[0]
        self.assertIn("unattended", call["prompt"])
        self.assertIn("[Context from the plugin]", call["prompt"])
        self.assertIsNone(call["conversation"])


class WorkspaceTests(Base):
    async def test_share_toggles(self):
        home = workspace.HERMES_HOME
        (home / "SOUL.md").write_text("be kind")
        ws = self.agy.backend.workspace
        saved = workspace.hermes_thread
        ws.rules_file = self.tmp / "AGENTS.md"
        workspace.hermes_thread = lambda limit: "- Alex: hi"
        try:
            ws._write_rules({}, datetime_now())
            rules = ws.rules_file.read_text()
            for text in ("be kind", "likes tea", "Alex: hi", "`memory`", "`agy_task`", "Hermes' skills"):
                self.assertIn(text, rules)
            config.cfg.update(share_soul=False, share_memory=False, share_thread=False, share_skills=False,
                              share_mcp_servers=False, bridge_tools=["telegram_send"])
            ws._write_rules({}, datetime_now())
            rules = ws.rules_file.read_text()
            for text in ("be kind", "likes tea", "Alex: hi", "`memory`", "`agy_task`", "Hermes' skills"):
                self.assertNotIn(text, rules)
            self.assertIn("`telegram_send`", rules)
        finally:
            workspace.hermes_thread = saved

    async def test_claude_workspace(self):
        home = workspace.HERMES_HOME
        (home / "SOUL.md").write_text("be kind")
        for path, name in (("devops/deploy", "deploy"), ("notes/journal", "journal"), (".hub/hidden", "hidden")):
            skill = home / "skills" / path
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} things\n---\nbody")
        (home / "config.yaml").write_text(json.dumps({"mcp_servers": {
            "stats": {"url": "https://x.y/mcp", "headers": {"Authorization": "Bearer t"}},
            "off": {"url": "https://z/mcp", "enabled": False}}}))
        ws = workspace.ClaudeWorkspace(self.claude.backend, self.tmp / "cw")
        config.cfg.update(skip_skills=["journal"])
        workspace.configure()
        try:
            summary = ws.prepare()
        finally:
            config.cfg.update(skip_skills=[])
            workspace.configure()
        self.assertEqual(summary["errors"], [])
        system = ws.system_prompt.read_text()
        for text in ("You are Claude Code", "be kind", "`cc_task`", "hermes:<name>", "[Context from the plugin]"):
            self.assertIn(text, system)
        self.assertNotIn("likes tea", system)  # memory goes in the message, not the recorded system prompt
        links = sorted(p.name for p in ws.skills_dir.iterdir())
        self.assertEqual(links, ["deploy"])
        self.assertEqual((ws.skills_dir / "deploy").resolve(), (home / "skills/devops/deploy").resolve())
        servers = json.loads(ws.mcp_config.read_text())["mcpServers"]
        self.assertEqual(sorted(servers), ["hermes", "stats"])
        self.assertEqual(servers["stats"]["type"], "http")
        self.assertEqual(json.loads(servers["hermes"]["env"]["ALT_AGENTS_TASK"])["prefix"], "cc")
        self.assertEqual(oct(ws.mcp_config.stat().st_mode & 0o777), "0o600")
        self.assertTrue((ws.plugin / ".claude-plugin" / "plugin.json").exists())


def datetime_now():
    from datetime import datetime
    return datetime.now().astimezone()


if __name__ == "__main__":
    unittest.main(verbosity=1)
