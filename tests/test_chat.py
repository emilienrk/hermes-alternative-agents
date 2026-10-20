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
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
presets = sys.modules["alt_agents_under_test.presets"]
CHAT = "424242"
SETTINGS = {"chat_id": CHAT, "user_name": "Alex", "backends": ["agy", "claude", "opencode"]}
FAKES = {"agy": str(TESTS_DIR / "fake_agy.py"), "claude": str(TESTS_DIR / "fake_claude.py"),
         "opencode": str(TESTS_DIR / "fake_opencode.py")}


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
            (workspace, "HERMES_HOME"), (presets, "DIR"))}
        presets.DIR = self.tmp / "presets"
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
        self.agy, self.claude, self.oc = engine.AGENTS["agy"], engine.AGENTS["claude"], engine.AGENTS["opencode"]
        self.oc.backend.env_file = self.tmp / "alternative-agents.env"
        self.oc.backend.env_file.write_text("# dedicated key\nDEEPSEEK_API_KEY='sk-test'\n")
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
        self.oc = engine.AGENTS.get("opencode")


class RoutingTests(Base):
    def route(self, ev, agy=None, claude=None):
        return plugin.route(ev, {"agy": agy or {}, "claude": claude or {}})

    def test_commands(self):
        kind, agent, job = self.route(event("/agy list the files"))
        self.assertEqual((kind, agent, job.text), ("job", self.agy, "list the files"))
        self.assertEqual(self.route(event("/agy@HermesBot list"))[2].text, "list")
        self.assertTrue(self.route(event("/agy_new on repart"))[2].new_conversation)
        self.assertEqual(self.route(event("/cc list"))[1], self.claude)
        self.assertTrue(self.route(event("/cc_new on repart"))[2].new_conversation)
        self.assertIsNone(self.route(event("/agy")))  # status card: the command handler answers
        self.assertIsNone(self.route(event("/cc")))
        self.assertIsNone(self.route(event("/agy_model")))
        self.assertIsNone(self.route(event("/new")))

    def test_sticky_mode(self):
        sticky = {"sticky": True}
        self.assertEqual(self.route(event("hello"), agy=sticky)[2].text, "hello")
        self.assertEqual(self.route(event("hello"), claude=sticky)[1], self.claude)
        self.assertIsNone(self.route(event("Hermes, give me a status"), agy=sticky))
        self.assertIsNone(self.route(event("Hermès : ça va ?"), claude=sticky))
        self.assertIsNone(self.route(event("/status"), agy=sticky))
        self.assertEqual(self.route(event("/new"), claude=sticky), ("new", self.claude, None))
        self.assertIsNone(self.route(event("/stop"), agy=sticky))  # nothing running: Hermes' /stop
        self.agy.run = object()
        self.assertEqual(self.route(event("/stop"), agy=sticky), ("stop", self.agy, None))

    def test_reply_to_an_agent(self):
        state = {"msgs": ["55"]}
        job = self.route(event("et maintenant ?", reply_to="55", reply_text="agy's answer"), agy=state)[2]
        self.assertEqual((job.text, job.quoted), ("et maintenant ?", ""))
        self.assertIsNone(self.route(event("et maintenant ?", reply_to="56"), agy=state))
        quoted = self.route(event("/agy do this", reply_to="56", reply_text="le plan de Hermes"), agy=state)[2]
        self.assertEqual(quoted.quoted, "le plan de Hermes")
        # In agy mode, a reply to Claude's message still goes to Claude.
        kind, agent, _job = self.route(event("et toi ?", reply_to="77"), agy={"sticky": True}, claude={"msgs": ["77"]})
        self.assertEqual((kind, agent), ("job", self.claude))
        self.assertEqual(self.route(event("seen", reply_to="9"), agy={"agy_msgs": ["9"]})[1], self.agy)  # older state

    def test_media_voice_and_inlined_documents(self):
        doc = self.tmp / "notes.txt"
        doc.write_text("line 1\nline 2")
        text = f"[Content of notes.txt]:\nline 1\nline 2\n\n/agy summarize\n[document 'notes.txt' saved at: {doc}]"
        job = self.route(event(text, media=[doc], types=["text/plain"], mtype="document"))[2]
        self.assertEqual((job.text, job.files), ("summarize", [(str(doc), "text/plain")]))
        voice = self.route(event("", media=["/c/v.ogg"], types=["audio/ogg"], mtype="voice"), agy={"sticky": True})[2]
        self.assertEqual((voice.voice, voice.files), (["/c/v.ogg"], []))
        self.assertIsNone(self.route(event("", media=["/c/v.ogg"], mtype="voice")))
        photo = self.route(event("/agy", media=["/c/p.jpg"], types=["image/jpeg"], mtype="photo"))[2]
        self.assertEqual(photo.files, [("/c/p.jpg", "image/jpeg")])

    async def test_dispatch_ignores_other_chats(self):
        self.assertIsNone(await self.dispatch(event("/agy hello", chat="42")))
        self.assertIsNone(self.agy.worker)

    async def test_nothing_routed_without_chat_id(self):
        engine.TARGET_CHAT = ""
        try:
            self.assertIsNone(await self.dispatch(event("/agy hello", chat="")))
            self.assertIsNone(await self.dispatch(event("/agy hello")))
        finally:
            engine.TARGET_CHAT = CHAT
        self.assertIsNone(self.agy.worker)

    async def test_disabled_backend_is_not_routed(self):
        self.set(backends=["agy"])
        engine.AGENTS.pop("claude")
        engine.AGENTS.pop("opencode")
        self.assertIsNone(plugin.route(event("/cc hello"), {"agy": {}}))


class TurnTests(Base):
    async def test_basic_turn(self):
        store = self.agy.store
        result = await self.dispatch(event("/agy list my projects", msg_id="7"))
        self.assertEqual(result, {"action": "skip", "reason": "alternative-agents"})
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("ECHO:", reply[1])
        self.assertIn("list my projects", reply[1])
        self.assertIn("See main.py.", reply[1])  # file:// link reduced to its label
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

        await self.dispatch(event("/agy what next?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], state["conversation_id"])
        self.assertNotIn("Context:", second["prompt"])

    async def test_reply_threading_reaches_agy(self):
        await self.dispatch(event("/agy hello", msg_id="7"))
        await self.finish()
        agy_id = str(self.adapter.next_id)
        result = await self.dispatch(event("be precise", msg_id="9", reply_to=agy_id))
        self.assertEqual(result["action"], "skip")
        await self.finish()
        self.assertTrue(self.calls()[-1]["prompt"].endswith("be precise"))

    async def test_queue_merges_messages(self):
        self.mode("verbose")
        await self.dispatch(event("/agy long task", msg_id="1"))
        await asyncio.sleep(0.5)
        await self.dispatch(event("/agy and this too", msg_id="2"))
        await self.dispatch(event("/agy and this", msg_id="3"))
        self.assertIn(("react", "2", "✍"), self.adapter.calls)
        self.mode("ok")
        await self.finish(40)
        prompts = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(prompts), 2)
        self.assertIn("Several messages from Alex", prompts[1])
        self.assertIn("1. and this too", prompts[1])
        self.assertIn("2. and this", prompts[1])
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
        await self.dispatch(event("/agy hello", msg_id="7"))
        await self.finish()
        self.assertIn("❌ alternative-agents", self.adapter.sent()[-1][1])
        self.assertNotIn("inflight", self.agy.store.load())
        self.assertEqual(self.adapter.calls[-1], ("react", "7", None))
        self.assertIsNone(self.agy.run)

    async def test_saturated_conversation_restarts_fresh(self):
        store = self.agy.store
        store.update(conversation_id="old-conv")
        store.log_turn({"conversation_id": "old-conv", "message": "before", "response": "earlier answer"})
        self.mode("saturated")
        await self.dispatch(event("/agy continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["old-conv", None])
        self.assertIn("earlier answer", calls[1]["prompt"])
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
        self.assertIn("💬 Looking.", status[1])
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
        await self.dispatch(event("/agy what is it?", media=[photo], types=["image/jpeg"], mtype="photo"))
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
        await self.dispatch(event("/cc list my projects", msg_id="7"))
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("ECHO:", reply[1])
        self.assertIn("See main.py.", reply[1])
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
        self.assertTrue(prompt.endswith("list my projects"))
        state = store.load()
        conv = state["conversation_id"]
        self.assertEqual(state["context"]["conversation_id"], conv)
        self.assertTrue(self.claude.owns(state, int(self.adapter.next_id)))
        self.assertFalse(self.agy.owns(self.agy.store.load(), int(self.adapter.next_id)))
        log = json.loads(store.turns_log.read_text().splitlines()[-1])
        self.assertEqual((log["status"], log["tokens"], log["cost_usd"]), ("SUCCESS", 51004, 0.25))

        await self.dispatch(event("/cc what next?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], conv)
        self.assertFalse(second["fork"])
        self.assertNotIn("likes tea", second["prompt"])  # memory unchanged: not sent again
        (workspace.HERMES_HOME / "memories" / "MEMORY.md").write_text("likes coffee now")
        await self.dispatch(event("/cc and now?", msg_id="9"))
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
        await self.dispatch(event("/cc hello", msg_id="7"))
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
        self.assertIn("💬 Looking.", status[1])
        final_edit = [c for c in self.adapter.calls if c[0] == "edit" and c[3]][-1]
        self.assertIn("🔧 1 actions", final_edit[2])
        self.assertIn("✓ Bash ls ~/projects", final_edit[2])

    async def test_lost_conversation_restarts_fresh(self):
        store = self.claude.store
        store.update(conversation_id="gone")
        store.log_turn({"conversation_id": "gone", "message": "before", "response": "earlier answer"})
        self.mode("missing")
        await self.dispatch(event("/cc continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["gone", None])
        self.assertIn("earlier answer", calls[1]["prompt"])
        self.assertIn("♻️ Conversation lost or too long", self.adapter.sent()[-1][1])
        self.assertNotEqual(store.load()["conversation_id"], "gone")

    async def test_quota_hint(self):
        self.mode("quota")
        await self.dispatch(event("/cc hello", msg_id="1"))
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
        self.assertTrue(self.adapter.sent("bot_send")[-1][1].startswith("✳️ Claude mode on · for Hermes"))
        self.assertIn("Claude mode is on", await plugin._cmd_status(self.agy, ""))
        result = await self.dispatch(event("hello", msg_id="4"))
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


class OpenCodeTests(Base):
    async def finish(self, timeout=20, agent=None):
        await super().finish(timeout, agent or self.oc)

    async def test_turn_and_resume(self):
        store = self.oc.store
        await self.dispatch(event("/oc list my projects", msg_id="7"))
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("See main.py.\n\nECHO:", reply[1])  # the last step's text parts
        self.assertNotIn("Looking", reply[1])
        self.assertIn("— 🐋 opencode", reply[1])
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(args[:3], ["run", "--format", "json"])
        self.assertEqual(args[args.index("--model") + 1], "deepseek/deepseek-flash")
        self.assertIn("--auto", args)
        self.assertIn("--title", args)  # no title request to the model
        self.assertNotIn("--variant", args)
        self.assertNotIn("list my projects", " ".join(args))  # the message goes through stdin
        self.assertEqual(call["env"], {"DEEPSEEK_API_KEY": "sk-test", "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                                       "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                                       "OPENCODE_CONFIG_DIR": str(self.oc.backend.workspace.config_dir)})
        self.assertIsNone(call["conversation"])
        self.assertTrue(call["prompt"].startswith("[Context from the plugin]\nNow: "))
        self.assertIn("likes tea", call["prompt"])
        self.assertTrue(call["prompt"].endswith("list my projects"))
        state = store.load()
        conv = state["conversation_id"]
        self.assertTrue(conv.startswith("ses_"))
        self.assertEqual(state["context"]["conversation_id"], conv)
        log = json.loads(store.turns_log.read_text().splitlines()[-1])
        self.assertEqual((log["status"], log["tokens"], log["actions"]), ("SUCCESS", 2580, ["✓ bash ls ~/projects"]))
        self.assertAlmostEqual(log["cost_usd"], 0.0005)

        await self.dispatch(event("/oc what next?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual((second["conversation"], second["fork"]), (conv, False))
        self.assertNotIn("--title", second["args"])
        self.assertNotIn("likes tea", second["prompt"])

    async def test_no_key_no_run(self):
        self.oc.backend.env_file.unlink()
        await self.dispatch(event("/oc hello", msg_id="7"))
        await self.finish()
        self.assertEqual(self.calls(), [])
        reply = self.adapter.sent()[-1][1]
        self.assertIn("No DeepSeek key", reply)
        self.assertIn("alternative-agents.env is missing", reply)
        self.oc.backend.env_file.write_text("OTHER=1\n")
        self.assertIn("DEEPSEEK_API_KEY is missing", self.oc.backend.unavailable())
        self.assertIn("✗ No DeepSeek key", "\n".join(self.oc.backend.checks()))

    async def test_hermes_keys_never_reach_opencode(self):
        env = self.oc.backend.env({"PATH": "/bin", "DEEPSEEK_API_KEY": "sk-hermes", "OPENCODE_CONFIG": "/x"})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-test")
        self.assertNotIn("OPENCODE_CONFIG", env)
        self.oc.backend.env_file.unlink()
        self.assertNotIn("DEEPSEEK_API_KEY", self.oc.backend.env({"DEEPSEEK_API_KEY": "sk-hermes"}))

    async def test_effort_model_and_usage(self):
        self.assertIsNone(await plugin._cmd_effort(self.oc, ""))  # picker of the model's variants
        self.assertEqual([c["label"] for c in self.adapter.sent("picker")[0][2]], ["low", "high", "max"])
        self.assertEqual(await plugin._cmd_effort(self.oc, "high"), "opencode effort → high")
        self.assertIn("Unknown model", await plugin._cmd_model(self.oc, "gpt-9"))
        self.assertEqual(await plugin._cmd_model(self.oc, "deepseek-v4-pro"),
                         "opencode model → deepseek/deepseek-v4-pro")
        self.oc.backend.balance = lambda: {"USD": "1.98"}
        await self.dispatch(event("/oc hello", msg_id="7"))
        await self.finish()
        args = self.calls()[0]["args"]
        self.assertEqual(args[args.index("--model") + 1], "deepseek/deepseek-v4-pro")
        self.assertEqual(args[args.index("--variant") + 1], "high")
        usage = await plugin._cmd_usage(self.oc, "")
        self.assertIn("DeepSeek balance: 1.98 USD", usage)
        self.assertIn("1 turns · 2k tokens ($0.00 at API prices)", usage)

    async def test_verbose_status_shows_tools(self):
        self.oc.store.update(verbose=True)
        self.mode("verbose")
        await self.dispatch(event("/oc build", msg_id="5"))
        await self.finish(30)
        status = self.adapter.sent()[0]
        self.assertTrue(status[1].startswith("🐋 opencode working"))
        self.assertIn("💬 Looking.", status[1])
        final_edit = [c for c in self.adapter.calls if c[0] == "edit" and c[3]][-1]
        self.assertIn("✓ bash ls ~/projects", final_edit[2])

    async def test_lost_session_restarts_fresh(self):
        store = self.oc.store
        store.update(conversation_id="ses_gone")
        store.log_turn({"conversation_id": "ses_gone", "message": "before", "response": "earlier answer"})
        self.mode("missing")
        await self.dispatch(event("/oc continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["ses_gone", None])
        self.assertIn("earlier answer", calls[1]["prompt"])
        self.assertIn("♻️ Conversation lost", self.adapter.sent()[-1][1])

    async def test_balance_hint(self):
        self.mode("balance")
        await self.dispatch(event("/oc hello", msg_id="1"))
        await self.finish()
        reply = self.adapter.sent()[-1][1]
        self.assertIn("❌ opencode ERROR: Insufficient Balance (HTTP 402)", reply)
        self.assertIn("/oc_usage", reply)
        self.assertEqual(len(self.calls()), 1)  # not retried

    async def test_btw_forks_the_main_conversation(self):
        await self.dispatch(event("/oc travail", msg_id="1"))
        await self.finish()
        main = self.oc.store.load()["conversation_id"]
        await self.dispatch(event("/oc_btw vite ?", msg_id="2"))
        await BtwTests.finish_btw(self)
        btw = self.calls()[-1]
        self.assertEqual((btw["conversation"], btw["fork"]), (main, True))
        self.assertEqual(btw["args"][btw["args"].index("--variant") + 1], "low")
        self.assertEqual(self.oc.store.load()["conversation_id"], main)

    async def test_doctor(self):
        self.oc.backend.balance = lambda: {"USD": "0.00"}
        ws = self.oc.backend.workspace
        ws.config_dir.mkdir(parents=True, exist_ok=True)
        ws.config_file.write_text(json.dumps({"mcp": {}}))
        report = await plugin._cmd_doctor(self.oc, "")
        self.assertIn("✓ opencode 9.9.9", report)
        self.assertIn("✓ DeepSeek key in ", report)
        self.assertIn("✗ DeepSeek balance used up: 0.00 USD", report)
        self.assertIn("✓ opencode config valid", report)


class PresetTests(Base):
    SECRET = "SECRET-PROMPT-1234 you are a mouse"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.mode("quiet")  # the fake answers "OK" instead of echoing the prompt

    async def finish(self, timeout=20, agent=None):
        await super().finish(timeout, agent or self.oc)

    async def preset_cmd(self, text, reply_text=None):
        result = await self.dispatch(event(text, msg_id="50", reply_text=reply_text))
        self.assertEqual(result, {"action": "skip", "reason": "alternative-agents"})
        return self.adapter.sent()[-1][1]

    def assertNoSecret(self):
        for call in self.adapter.calls:
            self.assertNotIn("SECRET-PROMPT", repr(call))
        for log in self.tmp.glob("*turns.log"):
            self.assertNotIn("SECRET-PROMPT", log.read_text())
        for state in self.tmp.glob("*state.json"):
            self.assertNotIn("SECRET-PROMPT", state.read_text())

    async def test_manage(self):
        self.assertIn("No preset yet", await self.preset_cmd("/preset"))
        self.assertIn("Reply to the prompt", await self.preset_cmd("/preset mouse"))
        saved = await self.preset_cmd("/preset mouse", reply_text=self.SECRET)
        self.assertIn(f"✅ /mouse: prompt saved ({len(self.SECRET)} chars) for opencode. New preset", saved)
        self.assertEqual(oct(presets.DIR.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(presets.path("mouse").stat().st_mode & 0o777), "0o600")
        self.assertEqual(presets.read("mouse"), self.SECRET)
        self.assertIn(f"/mouse · opencode · {len(self.SECRET)} chars", await self.preset_cmd("/preset"))
        self.assertNotIn("New preset", await self.preset_cmd("/preset mouse", reply_text=self.SECRET + "!"))
        for taken in ("oc_new", "new", "agy", "preset", "Bad-Name"):
            self.assertIn("can't be a preset", await self.preset_cmd(f"/preset {taken}", reply_text="x"))
        self.assertIn("🗑 /mouse removed.", await self.preset_cmd("/preset rm mouse"))
        self.assertFalse(presets.path("mouse").exists())
        self.assertNoSecret()

    async def test_preset_opens_a_conversation(self):
        config.cfg["presets"] = {"mouse": {"backend": "oc", "banner": "🐭 Mouse mode · /oc_off to leave"}}
        presets.save("mouse", self.SECRET)
        self.agy.store.update(sticky=True)
        self.oc.store.update(conversation_id="ses_old")
        result = await self.dispatch(event("/mouse", msg_id="10"))
        self.assertEqual(result["action"], "skip")
        await self.finish()
        call = self.calls()[0]
        self.assertEqual(call["prompt"], self.SECRET)  # as is: no context block
        self.assertIsNone(call["conversation"])
        self.assertNotIn(self.SECRET, " ".join(call["args"]))
        state = self.oc.store.load()
        self.assertEqual((state["sticky"], state["preset"]), (True, "mouse"))
        self.assertNotIn("sticky", self.agy.store.load())
        self.assertTrue(self.adapter.sent("bot_send")[-1][1].startswith("🐭 Mouse mode"))
        self.assertIn(("send", "🐭 …", "10"), self.adapter.calls)
        self.assertIn("See main.py.\n\nOK", self.adapter.sent()[-1][1])
        log = json.loads(self.oc.store.turns_log.read_text().splitlines()[-1])
        self.assertEqual((log["message"], log["preset"]), ("(prompt /mouse)", "mouse"))
        self.assertIn("/mouse preset", await plugin._cmd_status(self.oc, ""))

        await self.dispatch(event("what next?", msg_id="11"))  # the mode goes on in that conversation
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], state["conversation_id"])
        self.assertTrue(second["prompt"].endswith("what next?"))
        self.assertEqual(self.oc.store.conversations()[0]["first"], "(prompt /mouse)")
        self.assertNoSecret()

        pin = self.oc.store.load()["mode_pin"]
        self.assertIn("Next message", await plugin._cmd_new(self.oc, ""))
        self.assertNotIn("preset", self.oc.store.load())
        self.assertTrue(self.oc.store.load()["sticky"])
        self.assertIn(("unpin", pin), self.adapter.calls)
        self.assertTrue(self.adapter.sent("bot_send")[-1][1].startswith("🐋 opencode mode on"))
        await plugin._cmd_off(self.oc, "")
        self.assertNotIn("mode_pin", self.oc.store.load())

    async def test_bare_preset_conversation(self):
        presets.save("mouse", self.SECRET)
        await self.dispatch(event("/mouse", msg_id="10"))
        await self.finish()
        first = self.calls()[0]
        bare_dir = self.oc.backend.workspace.bare_dir
        self.assertEqual(first["env"]["OPENCODE_CONFIG_DIR"], str(bare_dir))
        self.assertIn("--agent bare", " ".join(first["args"]))
        conv = self.oc.store.load()["conversation_id"]

        await self.dispatch(event("what next?", msg_id="11"))
        await self.finish()
        await plugin._cmd_off(self.oc, "")
        await self.dispatch(event("/oc and then?", msg_id="12"))  # out of the mode, the same conversation
        await self.finish()
        self.assertEqual([(c["prompt"], c["conversation"]) for c in self.calls()[1:]],
                         [("what next?", conv), ("and then?", conv)])  # no context block
        self.assertTrue(all(c["env"]["OPENCODE_CONFIG_DIR"] == str(bare_dir) for c in self.calls()))

        await plugin._cmd_new(self.oc, "")
        await self.dispatch(event("/oc hello", msg_id="13"))
        await self.finish()
        normal = self.calls()[-1]
        self.assertNotEqual(normal["env"]["OPENCODE_CONFIG_DIR"], str(bare_dir))
        self.assertNotIn("--agent", normal["args"])
        self.assertTrue(normal["prompt"].endswith("hello") and normal["prompt"] != "hello")

        await plugin._pick_conv(self.oc, CHAT, conv)  # resumed: bare again
        await self.dispatch(event("back", msg_id="14"))
        await self.finish()
        self.assertEqual((self.calls()[-1]["prompt"], self.calls()[-1]["conversation"]), ("back", conv))
        self.assertNoSecret()

    async def test_preset_with_its_agent_setup(self):
        config.cfg["presets"] = {"mouse": {"bare": False}}
        presets.save("mouse", self.SECRET)
        await self.dispatch(event("/mouse", msg_id="10"))
        await self.finish()
        await self.dispatch(event("what next?", msg_id="11"))
        await self.finish()
        first, second = self.calls()
        self.assertEqual(first["prompt"], self.SECRET)
        self.assertEqual(first["env"]["OPENCODE_CONFIG_DIR"], str(self.oc.backend.workspace.config_dir))
        self.assertNotIn("--agent", first["args"])
        self.assertTrue(second["prompt"].endswith("what next?") and second["prompt"] != "what next?")

    async def test_retry_replays_the_preset(self):
        presets.save("mouse", self.SECRET)
        self.mode("balance")
        await self.dispatch(event("/mouse", msg_id="10"))
        await self.finish()
        self.assertIn("Insufficient Balance", self.adapter.sent()[-1][1])
        self.mode("quiet")
        self.assertEqual(await plugin._cmd_retry(self.oc, ""), "🔁 /mouse sent to opencode again.")
        await self.finish()
        self.assertEqual([c["prompt"] for c in self.calls()], [self.SECRET, self.SECRET])
        self.assertIsNone(self.calls()[1]["conversation"])
        self.assertNoSecret()

    async def test_messages_queued_behind_the_preset_come_next(self):
        presets.save("mouse", self.SECRET)
        self.mode("verbose")
        await self.dispatch(event("/oc before", msg_id="9"))
        await asyncio.sleep(0.5)
        await self.dispatch(event("/mouse", msg_id="10"))
        await self.dispatch(event("/oc after", msg_id="11"))
        self.mode("quiet")
        await self.finish(40)
        prompts = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[1], self.SECRET)
        self.assertTrue(prompts[2].endswith("after"))
        self.assertEqual(self.calls()[2]["conversation"], self.oc.store.load()["conversation_id"])

    async def test_missing_or_empty_prompt(self):
        config.cfg["presets"] = {"mouse": {}}
        self.assertIn("/mouse has no prompt", await self.preset_cmd("/mouse"))
        presets.DIR.mkdir(parents=True)
        presets.path("mouse").write_text("")
        self.assertIn("/mouse has no prompt", await self.preset_cmd("/mouse"))
        presets.save("mouse", self.SECRET)
        self.oc.backend.env_file.unlink()
        self.assertIn("No DeepSeek key", await self.preset_cmd("/mouse"))
        self.assertIsNone(self.oc.worker)
        self.assertEqual(self.calls(), [])

    def test_preset_commands_registered(self):
        presets.save("mouse", self.SECRET)
        names = []
        ctx = SimpleNamespace(get_config=lambda key, default=None: SETTINGS.get(key, default),
                              register_command=lambda name, **kw: names.append(name),
                              register_hook=lambda *a: None, register_telegram_handler=lambda *a: None)
        plugin.register(ctx)
        self.assertIn("mouse", names)
        self.assertIn("preset", names)


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

    async def test_banner_button_leaves_the_mode(self):
        from telegram.ext import ApplicationHandlerStop
        await plugin._cmd_on(self.oc, "")
        _kind, _banner, markup = self.adapter.sent("bot_send")[-1]
        button = markup.inline_keyboard[0][0]
        self.assertEqual((button.text, button.callback_data), ("Back to Hermes", "alt-agents:leave:opencode"))
        pin = self.oc.store.load()["mode_pin"]
        deleted = []

        def press(data, chat=CHAT, message_id=pin):
            async def answer():
                pass

            async def delete():
                deleted.append(message_id)
            message = SimpleNamespace(chat=SimpleNamespace(id=int(chat)), message_id=message_id, delete=delete)
            update = SimpleNamespace(callback_query=SimpleNamespace(data=data, message=message, answer=answer))
            return plugin._on_banner_button(update, None)

        with self.assertRaises(ApplicationHandlerStop):
            await press(button.callback_data, chat="999")  # another chat: nothing
        self.assertTrue(self.oc.store.load()["sticky"])
        with self.assertRaises(ApplicationHandlerStop):
            await press(button.callback_data)
        self.assertNotIn("sticky", self.oc.store.load())
        self.assertIn(("unpin", pin), self.adapter.calls)
        self.assertIn("Back to Hermes", self.adapter.sent()[-1][1])
        self.assertEqual(deleted, [])  # the mode's own banner: deleted with the mode
        with self.assertRaises(ApplicationHandlerStop):
            await press(button.callback_data, message_id=pin)  # pressed again on a leftover banner
        self.assertEqual(deleted, [pin])

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
        store.log_turn({"conversation_id": "c1", "message": "first topic"})
        store.log_turn({"conversation_id": "c2", "message": "second topic"})
        self.assertIsNone(await plugin._cmd_conv(self.agy, ""))
        choices = self.adapter.sent("picker")[0][2]
        self.assertEqual([c["value"] for c in choices], ["c2", "c1"])
        self.assertIn("first topic", await plugin._cmd_conv(self.agy, "2"))
        self.assertEqual(store.load()["conversation_id"], "c1")

    async def test_resume_turns_the_mode_on_with_the_preset_banner(self):
        config.cfg["presets"] = {"mouse": {"backend": "oc", "banner": "🐭 Mouse mode"}}
        store = self.oc.store
        store.log_turn({"conversation_id": "plain", "message": "a topic"})
        store.log_turn({"conversation_id": "rat", "message": "(prompt /mouse)", "preset": "mouse"})
        store.log_turn({"conversation_id": "rat", "message": "what next?"})
        self.agy.store.update(sticky=True)
        await plugin._cmd_on(self.agy, "")
        agy_pin = self.agy.store.load()["mode_pin"]

        self.assertIsNone(await plugin._cmd_conv(self.oc, ""))
        self.assertTrue(self.adapter.sent("picker")[-1][2][0]["label"].endswith("/mouse · what next?"))
        reply = await plugin._cmd_conv(self.oc, "1")
        self.assertIn("/mouse · what next?", reply)
        state = store.load()
        self.assertEqual((state["conversation_id"], state["sticky"], state["preset"]), ("rat", True, "mouse"))
        self.assertNotIn("sticky", self.agy.store.load())  # one mode at a time
        self.assertIn(("unpin", agy_pin), self.adapter.calls)
        self.assertEqual(self.adapter.sent("bot_send")[-1][1], "🐭 Mouse mode")
        rat_pin = state["mode_pin"]

        await plugin._cmd_conv(self.oc, "1")  # the same again: the banner stays
        self.assertEqual(store.load()["mode_pin"], rat_pin)

        self.assertIn("a topic", await plugin._cmd_conv(self.oc, "2"))
        state = store.load()
        self.assertEqual((state["conversation_id"], state["sticky"], state.get("preset")), ("plain", True, None))
        self.assertIn(("unpin", rat_pin), self.adapter.calls)
        self.assertTrue(self.adapter.sent("bot_send")[-1][1].startswith("🐋 opencode mode on"))

        await self.dispatch(event("next one", msg_id="20"))  # goes to opencode, in that conversation
        await self.finish(agent=self.oc)
        self.assertEqual(self.calls()[-1]["conversation"], "plain")

    def test_commands_registered_per_backend(self):
        names = []
        ctx = SimpleNamespace(get_config=lambda key, default=None: SETTINGS.get(key, default),
                              register_command=lambda name, **kw: names.append(name),
                              register_hook=lambda *a: None, register_telegram_handler=lambda *a: None)
        plugin.register(ctx)
        self.assertIn("agy-btw", names)
        self.assertIn("cc", names)
        self.assertIn("cc-handoff", names)
        self.assertIn("oc-usage", names)
        self.assertEqual(len(names), 3 * len(plugin.COMMANDS) + 1)  # + /preset


class SettingsTests(Base):
    async def test_no_reactions_no_footer(self):
        self.set(react_working="", footer=False, mark="")
        await self.dispatch(event("/agy hello", msg_id="7"))
        await self.finish()
        self.assertNotIn(("react", "7", "👀"), self.adapter.calls)
        self.assertNotIn("— ", self.adapter.sent()[-1][1])

    async def test_custom_mark_and_reaction(self):
        self.set(react_working="🔥")
        self.agy.backend.mark = "🤖"
        await self.dispatch(event("/agy hello", msg_id="7"))
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
        await self.dispatch(event("write", msg_id="1"))
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
        self.assertEqual(engine._spoken_text("See [doc](https://x.y)\nMEDIA:/tmp/a.png\nend"), "See doc\nend")

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
            added = await plugin._cmd_task(self.agy, "add every day at 8am | summarize my mails")
            self.assertIn("scheduled", added)
            create = calls[0]
            self.assertTrue(create["no_agent"])
            self.assertEqual(create["deliver"], f"telegram:{CHAT}")
            self.assertEqual(create["name"], "agy · summarize my mails")
            script = self.tmp / "scripts" / create["script"]
            self.assertIn("task_run.py", script.read_text())
            self.assertIn("/agy_task", script.read_text())
            self.assertIn("1. summarize my mails · every day at 8am", await plugin._cmd_task(self.agy, ""))
            await plugin._cmd_task(self.claude, "add weekdays at 7am | point du jour")
            self.assertIn('"backend": "claude"', (self.tmp / "scripts" / calls[-1]["script"]).read_text())
            self.assertIn("1. point du jour", await plugin._cmd_task(self.claude, ""))
            self.assertNotIn("point du jour", await plugin._cmd_task(self.agy, ""))
            self.assertIn("removed", await plugin._cmd_task(self.agy, "rm 1"))
            self.assertFalse(script.exists())
            self.assertIn("No agy task", await plugin._cmd_task(self.agy, ""))
        finally:
            tasks._cron, tasks.HERMES_HOME, tasks._registry_path = saved

    def run_task(self, task):
        import subprocess
        hermes = self.tmp / "hermes"
        (hermes / "config.yaml").write_text(json.dumps({"plugins": {"entries": {"alternative-agents": {"settings": {
            "user_name": "Alex", "claude_path": FAKES["claude"], "claude_workspace": str(self.tmp / "cw"),
            "opencode_path": FAKES["opencode"], "opencode_workspace": str(self.tmp / "ow"),
            "opencode_env_file": str(self.oc.backend.env_file)}}}}}))
        env = {**os.environ, "HERMES_HOME": str(hermes)}
        return subprocess.run([sys.executable, str(PLUGIN_DIR / "task_run.py"), json.dumps(task)],
                              capture_output=True, text=True, env=env, timeout=60)

    async def test_agy_task_run(self):
        task = {"name": "digest", "prompt": "summarize", "model": "m1", "settings": {
            "agy_path": FAKES["agy"], "workspace": str(self.tmp), "timeout_minutes": 1, "mark": "🛸"}}
        out = self.run_task(task)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("🛸 agy · digest\n\n"), out.stdout)
        self.assertIn("ECHO:", out.stdout)
        self.assertIn("See main.py.", out.stdout)
        self.assertIn("unattended", self.calls()[0]["prompt"])
        self.mode("crash")
        out = self.run_task(task)
        self.assertEqual(out.returncode, 1)
        self.assertIn("❌", out.stdout)

    async def test_task_context_and_silence(self):
        task = {"name": "health", "prompt": "check it",
                "settings": {"agy_path": FAKES["agy"], "workspace": str(self.tmp)},
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
        out = self.run_task({"backend": "claude", "name": "point", "prompt": "give me a status", "model": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("✳️ Claude · point\n\n"), out.stdout)
        call = self.calls()[0]
        self.assertIn("unattended", call["prompt"])
        self.assertIn("[Context from the plugin]", call["prompt"])
        self.assertIsNone(call["conversation"])

    async def test_opencode_task_run(self):
        out = self.run_task({"backend": "opencode", "name": "point", "prompt": "give me a status", "model": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("🐋 opencode · point\n\n"), out.stdout)
        call = self.calls()[0]
        self.assertIn("unattended", call["prompt"])
        self.assertEqual(call["env"]["DEEPSEEK_API_KEY"], "sk-test")


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

    async def test_opencode_workspace(self):
        home = workspace.HERMES_HOME
        skill = home / "skills" / "devops" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: deploy things\n---\nbody")
        (home / "config.yaml").write_text(json.dumps({"mcp_servers": {
            "stats": {"url": "https://x.y/mcp", "headers": {"Authorization": "Bearer t"}}}}))
        ws = workspace.OpenCodeWorkspace(self.oc.backend, self.tmp / "ow")
        summary = ws.prepare()
        self.assertEqual(summary["errors"], [])
        oc_config = json.loads(ws.config_file.read_text())
        self.assertEqual(sorted(oc_config["mcp"]), ["hermes"])  # Hermes' MCP servers through the bridge
        self.assertEqual(oc_config["mcp"]["hermes"]["type"], "local")
        env = oc_config["mcp"]["hermes"]["environment"]
        self.assertEqual((env["ALT_AGENTS_MCP_ON_DEMAND"], env["ALT_AGENTS_MCP_CATALOG"]),
                         ("1", str(ws.mcp_catalog())))
        self.assertIn("`mcp_tool_info`", ws.rules_file.read_text())
        self.assertNotIn("sk-test", ws.config_file.read_text())
        self.assertEqual(oct(ws.config_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(ws.bridge_config()["args"], [str(workspace.BRIDGE)])
        rules = ws.rules_file.read_text()
        for text in ("You are opencode", "`oc_task`", "`skill` tool", "[Context from the plugin]"):
            self.assertIn(text, rules)
        self.assertEqual((ws.skills_dir / "deploy").resolve(), skill.resolve())
        bare = json.loads((ws.bare_dir / "opencode.json").read_text())  # bare conversations: nothing but messages
        self.assertEqual(sorted(bare), ["$schema", "agent", "autoupdate", "share"])
        self.assertEqual(bare["agent"]["bare"]["tools"], {"*": False})
        self.assertIn("output.system.length = 0", (ws.bare_dir / "plugin" / "bare.js").read_text())
        self.assertEqual(sorted(p.name for p in ws.bare_dir.iterdir()), ["opencode.json", "plugin"])

        config.cfg.update(mcp_on_demand=[])
        try:
            ws.prepare()
        finally:
            config.cfg.update(mcp_on_demand=["opencode"])
        oc_config = json.loads(ws.config_file.read_text())
        self.assertEqual(oc_config["mcp"]["stats"], {"type": "remote", "url": "https://x.y/mcp",
                                                  "headers": {"Authorization": "Bearer t"}})
        self.assertNotIn("ALT_AGENTS_MCP_ON_DEMAND", oc_config["mcp"]["hermes"]["environment"])

    async def test_bridge_mcp_on_demand(self):
        schemas = {"mcp__stats__get_steps": {"description": "Steps per day. " + "x" * 200,
                                             "parameters": {"type": "object", "properties": {"day": {}}}},
                   "mcp__money__overview": {"description": "Portfolio overview.", "parameters": {}}}
        calls = []
        registry = types.SimpleNamespace(
            get_entry=lambda name: types.SimpleNamespace(schema=schemas[name]) if name in schemas else None,
            dispatch=lambda name, args: calls.append((name, args)) or {"ok": name})
        fakes = {"tools": types.ModuleType("tools"),
                 "tools.mcp_tool_discovery": types.SimpleNamespace(discover_mcp_tools=lambda: list(schemas)),
                 "tools.registry": types.SimpleNamespace(registry=registry)}
        catalog = self.tmp / "catalog.json"
        env = {"ALT_AGENTS_MCP_ON_DEMAND": "1", "ALT_AGENTS_MCP_CATALOG": str(catalog), "ALT_AGENTS_TOOLS": "none"}
        stdout = sys.stdout
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, fakes):
            spec = importlib.util.spec_from_file_location("bridge_under_test", PLUGIN_DIR / "hermes_mcp.py")
            bridge = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(bridge)
            finally:
                sys.stdout = stdout  # the bridge hands stdout to MCP
            tools = {t["name"]: t for t in bridge.list_tools()}
            self.assertEqual(sorted(tools), ["mcp_tool_call", "mcp_tool_info"])
            listing = tools["mcp_tool_info"]["description"]
            self.assertIn("money.overview: Portfolio overview.", listing)
            self.assertIn("stats.get_steps: Steps per day.", listing)
            self.assertLess(len(listing), 400)  # summaries, not the schemas
            self.assertTrue(catalog.exists())
            info = json.loads(bridge.call_tool("mcp_tool_info", {"names": ["stats.get_steps", "nope.x"]}))
            self.assertEqual(info["stats.get_steps"]["parameters"]["properties"], {"day": {}})
            self.assertEqual(info["nope.x"], {"error": "unknown tool"})
            self.assertEqual(json.loads(bridge.call_tool("mcp_tool_call", {"name": "money.overview"})),
                             {"ok": "mcp__money__overview"})
            self.assertIn("Unknown MCP tool", bridge.call_tool("mcp_tool_call", {"name": "money.nope"}))
            self.assertEqual(calls, [("mcp__money__overview", {})])


def datetime_now():
    from datetime import datetime
    return datetime.now().astimezone()


if __name__ == "__main__":
    unittest.main(verbosity=1)
