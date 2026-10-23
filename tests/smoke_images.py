"""Regression tests for image prompts, their encoding and retention, thought events, and clipboard reads.

Everything runs against local fake processes (tests/fake_*.py); no real agent, API key or network is used.

    ~/Projects/deck/.venv/bin/python ~/Projects/deck/tests/smoke_images.py
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402,F401  (throwaway state; must come before deck is imported)
sys.path.insert(0, str(ROOT))

from deck import acp, codexserver, core, images  # noqa: E402
from deck.adapters import Ev, parse_claude, parse_opencode  # noqa: E402
from deck.detect import Found  # noqa: E402
from deck.images import Prompt  # noqa: E402

TESTS = Path(__file__).resolve().parent
FAKE_ACP = TESTS / "fake_acp_agent.py"                  # no image capability
FAKE_ACP_IMAGES = TESTS / "fake_imaging_acp_agent.py"   # advertises promptCapabilities.image
FAKE_CODEX = TESTS / "fake_codex_appserver.py"
FAKE_CLAUDE = TESTS / "fake_claude_cli.py"
FAKE_CLIPBOARD = TESTS / "fake_clipboard_tool.py"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
WORK = _env.BASE / "images-work"
WORK.mkdir(parents=True, exist_ok=True)
LOGS = _env.BASE / "images-logs"
LOGS.mkdir(parents=True, exist_ok=True)


def logged(path: Path, kind: str) -> list:
    if not path.exists():
        return []
    return [r["payload"] for r in map(json.loads, path.read_text().splitlines()) if r["kind"] == kind]


def run_async(coro, timeout: float = 30):
    async def bounded():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(bounded())


class FakeHub:
    """Only what acp.drive needs when no permission is requested."""

    def __init__(self):
        self.decisions = []

    async def decide(self, run, tool, inp):
        self.decisions.append((tool, inp))
        return False


# ---- image validation and encoding ------------------------------------------------------------

class ImageTests(unittest.TestCase):
    def test_magic_bytes_pick_the_mime(self):
        for data, mime in ((PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")):
            with self.subTest(mime=mime):
                im = images.Image.from_bytes("x." + mime.split("/")[1], data)
                self.assertEqual(im.mime, mime)
                self.assertEqual(im.data, data)
                self.assertEqual(im.name, "x." + mime.split("/")[1])

    def test_rejects_non_images_and_truncated_headers(self):
        for data in (b"", b"not an image", b"RIFF\x00\x00\x00\x00WAVE", b"\x89PNG\r\n\x1a", b"GIF8"):
            with self.subTest(data=data[:12]):
                with self.assertRaises(ValueError):
                    images.Image.from_bytes("x", data)

    def test_size_limit_is_inclusive_and_refuses_one_byte_more(self):
        with mock.patch.object(images, "MAX_BYTES", 8):
            self.assertEqual(images.Image.from_bytes("p", PNG[:8]).data, PNG[:8])
            with self.assertRaisesRegex(ValueError, "20 MiB"):
                images.Image.from_bytes("p", PNG[:8] + b"x")

    def test_from_path_keeps_the_file_name_and_bounds_the_read(self):
        path = LOGS / "tiny.png"
        path.write_bytes(PNG)
        im = images.Image.from_path(path)
        self.assertEqual((im.name, im.mime, im.data), ("tiny.png", "image/png", PNG))
        big = LOGS / "big.png"
        big.write_bytes(PNG + b"\x00" * 32)
        with mock.patch.object(images, "MAX_BYTES", len(PNG)):
            with self.assertRaisesRegex(ValueError, "20 MiB"):
                images.Image.from_path(big)
        with self.assertRaises(FileNotFoundError):
            images.Image.from_path(LOGS / "missing.png")

    def test_image_is_frozen(self):
        im = images.Image.from_bytes("x.png", PNG)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            im.data = b""

    def test_native_encodings(self):
        im = images.Image.from_bytes("shot.png", PNG)
        b64 = base64.b64encode(PNG).decode("ascii")
        self.assertEqual(im.encoded(), b64)
        self.assertEqual(im.acp(), {"type": "image", "mimeType": "image/png", "data": b64})
        self.assertEqual(im.claude(), {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": b64}})
        self.assertEqual(im.codex(), {"type": "image", "url": f"data:image/png;base64,{b64}"})


class PromptTests(unittest.TestCase):
    def test_prompt_is_a_string_with_images(self):
        a, b = images.Image.from_bytes("a.png", PNG), images.Image.from_bytes("b.png", JPEG)
        p = Prompt("hello", [a, b])
        self.assertIsInstance(p, str)
        self.assertEqual(p, "hello")
        self.assertEqual(p.images, (a, b))
        self.assertEqual(Prompt("plain").images, ())

    def test_join_prompts_keeps_text_order_and_every_image(self):
        a, b, c = (images.Image.from_bytes(n, data) for n, data in
                   (("a.png", PNG), ("b.jpg", JPEG), ("c.gif", GIF)))
        joined = Prompt.join_prompts([Prompt("first", [a]), Prompt("second", [b]), Prompt("third", [c])])
        self.assertIsInstance(joined, Prompt)
        self.assertEqual(joined, "first\n\nsecond\n\nthird")
        self.assertEqual(joined.images, (a, b, c))

    def test_join_prompts_accepts_plain_strings_and_no_prompts(self):
        a = images.Image.from_bytes("a.png", PNG)
        joined = Prompt.join_prompts(["just text", Prompt("with image", [a]), "more text"])
        self.assertEqual(str(joined), "just text\n\nwith image\n\nmore text")
        self.assertEqual(joined.images, (a,))
        empty = Prompt.join_prompts([])
        self.assertEqual((str(empty), empty.images), ("", ()))

    def test_joining_does_not_mutate_inputs(self):
        a = images.Image.from_bytes("a.png", PNG)
        p = Prompt("one", [a])
        Prompt.join_prompts([p, "two"])
        self.assertEqual((str(p), p.images), ("one", (a,)))


# ---- thought events from the parsers and event stream -----------------------------------------

class ParserTests(unittest.TestCase):
    def test_claude_thinking_becomes_a_thought_event(self):
        events = parse_claude({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "thinking", "thinking": ""},
        ]}})
        self.assertEqual([(e.kind, e.text) for e in events], [
            ("thought", "hmm"), ("text", "answer"), ("tool", "Bash: ls")])

    def test_claude_without_thinking_is_unchanged(self):
        events = parse_claude({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        self.assertEqual([(e.kind, e.text) for e in events], [("text", "hi")])

    def test_opencode_reasoning_becomes_a_thought_event(self):
        events = parse_opencode({"type": "reasoning", "sessionID": "ses-1", "part": {"text": "let me think"}})
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].kind, events[0].text, events[0].meta),
                         ("thought", "let me think", {"session": "ses-1"}))

    def test_opencode_text_stays_text(self):
        events = parse_opencode({"type": "text", "sessionID": "ses-1", "part": {"text": "hello"}})
        self.assertEqual([(e.kind, e.text) for e in events], [("text", "hello")])


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.run = core.Run(SimpleNamespace(), "claude")

    def test_switching_between_text_and_thought_flushes_the_right_kind(self):
        self.run.emit_delta("hello", "text")
        self.run.emit_delta("wor", "thought")
        self.assertEqual(self.run.lines, [("text", "hello")])
        self.run.emit_delta("ld\n", "thought")
        self.assertEqual(self.run.lines, [("text", "hello"), ("thought", "world")])
        self.run.emit_delta("back\n", "text")
        self.assertEqual(self.run.lines[-1], ("text", "back"))

    def test_flush_uses_the_kind_of_the_pending_text(self):
        self.run.emit_delta("a thought", "thought")
        self.run.flush()
        self.assertEqual(self.run.lines, [("thought", "a thought")])
        self.run.emit_delta("visible", "text")
        self.run.flush()
        self.assertEqual(self.run.lines[-1], ("text", "visible"))

    def test_switching_after_a_flush_does_not_emit_an_empty_line(self):
        self.run.emit_delta("done\n", "text")
        self.run.flush()
        self.run.emit_delta("thinking", "thought")
        self.assertEqual(self.run.lines, [("text", "done")])
        self.run.flush()
        self.assertEqual(self.run.lines, [("text", "done"), ("thought", "thinking")])

    def test_whitespace_only_tail_is_dropped(self):
        self.run.emit_delta("   ", "text")
        self.run.flush()
        self.assertEqual(self.run.lines, [])


# ---- retention through the queue --------------------------------------------------------------

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.hub = core.Hub.__new__(core.Hub)
        self.hub.cfg = {"auto_checkin": False}
        self.delivered = []

        async def follow_up(run, prompt, shown=None):
            self.delivered.append((run, prompt, shown))

        self.hub.follow_up = follow_up
        self.a = images.Image.from_bytes("a.png", PNG)
        self.b = images.Image.from_bytes("b.jpg", JPEG)

    def _run(self, **kw):
        return core.Run(SimpleNamespace(runs={}), "claude", label="claude", **kw)

    def _after_turn(self, run):
        async def go():
            self.hub._after_turn(run)
            await asyncio.sleep(0)
        run_async(go())

    def test_queued_images_survive_joining(self):
        run = self._run(session="s1", status="done")
        run.queued = [Prompt("first", [self.a]), "second", Prompt("third", [self.b])]
        self._after_turn(run)
        self.assertEqual(run.queued, [])
        self.assertEqual(len(self.delivered), 1)
        _, prompt, shown = self.delivered[0]
        self.assertIsInstance(prompt, Prompt)
        self.assertEqual(str(prompt), "first\n\nsecond\n\nthird")
        self.assertEqual(prompt.images, (self.a, self.b))
        self.assertIn("queued replies", shown)
        self.assertIn("first", shown)

    def test_one_queued_prompt_is_still_an_image_prompt(self):
        run = self._run(session="s1", status="done")
        run.queued = [Prompt("with pic", [self.a])]
        self._after_turn(run)
        self.assertEqual(self.delivered[0][1].images, (self.a,))
        self.assertIn("queued reply", self.delivered[0][2])

    def test_a_killed_run_drops_its_queue(self):
        run = self._run(session="s1", status="killed")
        run.queued = [Prompt("never sent", [self.a])]
        self._after_turn(run)
        self.assertEqual((run.queued, self.delivered), ([], []))

    def test_reply_queues_the_prompt_with_its_images(self):
        run = self._run(session="s1", status="running")
        result = self.hub.reply(run, Prompt("look at this", [self.a]))
        self.assertIn("queued", result)
        self.assertEqual(len(run.queued), 1)
        self.assertEqual(run.queued[0].images, (self.a,))

    def test_an_idle_reply_hands_images_to_follow_up(self):
        run = self._run(session="s1", status="done")

        async def go():
            result = self.hub.reply(run, Prompt("here", [self.b]))
            await asyncio.sleep(0)
            return result

        self.assertEqual(run_async(go()), "sent to claude")
        self.assertEqual(self.delivered[0][1].images, (self.b,))

    def test_no_session_is_reported_not_queued(self):
        run = self._run(session=None, status="done")
        self.assertIn("no session", self.hub.reply(run, Prompt("x", [self.a])))


# ---- the real drivers, against fake processes -------------------------------------------------

class AcpImageTests(unittest.TestCase):
    def _drive(self, agent: Path, log: Path, prompt: str, attachments):
        run = SimpleNamespace(proc=None, status="running")
        events: list[Ev] = []

        async def go():
            with mock.patch.dict(os.environ, {"FAKE_ACP_LOG": str(log)}):
                return await acp.drive(
                    FakeHub(), run, prompt, None, argv=[sys.executable, str(agent)], model="", effort="",
                    plan=False, cwd=str(WORK), env={**os.environ, "FAKE_ACP_LOG": str(log)},
                    apply=events.append, images=attachments)
        return run_async(go()), events

    def test_an_agent_without_image_capability_refuses_before_prompting(self):
        log = LOGS / "acp-no-image.log"
        log.unlink(missing_ok=True)
        im = images.Image.from_bytes("a.png", PNG)
        rc, events = self._drive(FAKE_ACP, log, "look", [im])
        self.assertEqual(rc, 1)
        errors = [e.text for e in events if e.kind == "error"]
        self.assertTrue(any("does not advertise image input support" in e for e in errors), errors)
        self.assertEqual(logged(log, "session/prompt"), [])
        self.assertEqual(logged(log, "session/new"), [])

    def test_images_are_encoded_as_acp_content_blocks(self):
        log = LOGS / "acp-images.log"
        log.unlink(missing_ok=True)
        im = images.Image.from_bytes("a.png", PNG)
        rc, events = self._drive(FAKE_ACP_IMAGES, log, "what is this?", [im])
        self.assertEqual(rc, 0, [e for e in events if e.kind == "error"])
        sent = logged(log, "session/prompt")[-1]["prompt"]
        self.assertEqual(sent[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(sent[1], {"type": "image", "mimeType": "image/png",
                                   "data": base64.b64encode(PNG).decode("ascii")})
        self.assertEqual(len(sent), 2)

    def test_thought_and_text_chunks_keep_their_kinds(self):
        log = LOGS / "acp-thoughts.log"
        log.unlink(missing_ok=True)
        rc, events = self._drive(FAKE_ACP_IMAGES, log, "hi", [])
        self.assertEqual(rc, 0)
        thoughts = [e for e in events if e.kind == "thought"]
        texts = [e for e in events if e.kind == "text"]
        self.assertEqual("".join(e.text for e in thoughts), "pondering the image\n")
        self.assertTrue(all(e.meta.get("delta") for e in thoughts), thoughts)
        self.assertEqual("".join(e.text for e in texts), "I see it.\n")
        self.assertTrue(any(e.meta.get("tokens") == 21 for e in events), events)

    def test_a_prompt_without_images_has_only_the_text_block(self):
        log = LOGS / "acp-text-only.log"
        log.unlink(missing_ok=True)
        rc, _ = self._drive(FAKE_ACP_IMAGES, log, "hello", [])
        self.assertEqual(rc, 0)
        self.assertEqual(logged(log, "session/prompt")[-1]["prompt"],
                         [{"type": "text", "text": "hello"}])


class CodexImageTests(unittest.TestCase):
    def test_turn_input_and_thought_events(self):
        log = LOGS / "codex-images.log"
        log.unlink(missing_ok=True)
        im = images.Image.from_bytes("a.png", PNG)
        events: list[Ev] = []
        run = SimpleNamespace(proc=None, status="running")

        async def go():
            with mock.patch.dict(os.environ, {"FAKE_CODEX_LOG": str(log)}):
                return await codexserver.drive(
                    None, run, "what is this?", None, model="", effort="", plan=False, cwd=str(WORK),
                    env={**os.environ, "FAKE_CODEX_LOG": str(log)}, apply=events.append,
                    exe=str(FAKE_CODEX), images=[im])
        rc = run_async(go())
        self.assertEqual(rc, 0, [e for e in events if e.kind == "error"])
        turn = logged(log, "turn/start")[-1]
        self.assertEqual(turn["input"][0], {"type": "text", "text": "what is this?"})
        self.assertEqual(turn["input"][1], {
            "type": "image", "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")})
        self.assertEqual(len(turn["input"]), 2)
        self.assertIn(("thought", "Looked at the image.\nNothing alarming."), [(e.kind, e.text) for e in events])
        self.assertIn(("tool_output", "a.txt\n"), [(e.kind, e.text) for e in events])
        self.assertIn(("text", "all done\n"), [(e.kind, e.text) for e in events])
        self.assertTrue(any(e.meta.get("tokens") == 4242 for e in events), events)

    def test_text_only_turn_and_session_resume(self):
        log = LOGS / "codex-text.log"
        log.unlink(missing_ok=True)
        events: list[Ev] = []
        run = SimpleNamespace(proc=None, status="running")

        async def go():
            with mock.patch.dict(os.environ, {"FAKE_CODEX_LOG": str(log)}):
                return await codexserver.drive(
                    None, run, "hello", "fake-thread", model="", effort="", plan=False, cwd=str(WORK),
                    env={**os.environ, "FAKE_CODEX_LOG": str(log)}, apply=events.append,
                    exe=str(FAKE_CODEX), images=[])
        rc = run_async(go())
        self.assertEqual(rc, 0)
        self.assertEqual(logged(log, "thread/resume")[-1]["threadId"], "fake-thread")
        self.assertEqual(logged(log, "turn/start")[-1]["input"], [{"type": "text", "text": "hello"}])


class ClaudeImageTests(unittest.TestCase):
    def _hub(self, found: dict):
        with mock.patch.object(core.detect, "scan", return_value={}):
            hub = core.Hub(dict(core.DEFAULT_CONFIG), lambda run: None)
        hub.found = found
        return hub

    def _run(self, hub, agent: str, prompt, session=None):
        task = core.Task(str(prompt), str(WORK))
        run = hub._add_run(task, agent)
        run.cwd = str(WORK)

        async def go():
            await hub._run(run, prompt, False, session)
            return run
        return run_async(go())

    def test_claude_gets_an_image_in_stream_json_input(self):
        log = LOGS / "claude-images.log"
        log.unlink(missing_ok=True)
        im = images.Image.from_bytes("a.png", PNG)
        im2 = images.Image.from_bytes("b.jpg", JPEG)
        hub = self._hub({"claude": Found("claude", str(FAKE_CLAUDE))})
        prompt = Prompt("describe this", [im, im2])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(log)}):
            run = self._run(hub, "claude", prompt)
        self.assertEqual(run.status, "done", run.lines)
        argv = logged(log, "argv")[-1]
        # -p stays (print mode); its prompt argument is removed because the prompt now goes on stdin
        self.assertNotIn("describe this", argv, argv)
        self.assertNotEqual(argv[argv.index("-p") + 1], "describe this", argv)
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        messages = logged(log, "stdin")[-1]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "user")
        content = messages[0]["message"]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe this"})
        self.assertEqual(content[1:], [im.claude(), im2.claude()])
        self.assertIn(("thought", "I should look at the attached image."), run.lines)
        self.assertIn(("text", "I received your picture."), run.lines)
        self.assertEqual(run.session, "fake-session")
        self.assertEqual(run.tokens, 15)
        self.assertIsNone(run.proc)

    def test_claude_without_images_keeps_the_plain_prompt_flag(self):
        log = LOGS / "claude-text.log"
        log.unlink(missing_ok=True)
        hub = self._hub({"claude": Found("claude", str(FAKE_CLAUDE))})
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(log)}):
            run = self._run(hub, "claude", "plain question")
        self.assertEqual(run.status, "done", run.lines)
        argv = logged(log, "argv")[-1]
        self.assertEqual(argv[argv.index("-p") + 1], "plain question")
        self.assertNotIn("--input-format", argv)
        self.assertEqual(logged(log, "stdin")[-1], [])

    def test_a_non_claude_text_adapter_refuses_images_before_spawning(self):
        hub = self._hub({})
        im = images.Image.from_bytes("a.png", PNG)
        run = self._run(hub, "agy", Prompt("look", [im]))
        self.assertEqual(run.status, "failed")
        self.assertTrue(any(k == "error" and "does not support image input" in t for k, t in run.lines), run.lines)
        self.assertIsNone(run.proc)

    def test_codex_gets_images_through_the_hub(self):
        log = LOGS / "codex-hub.log"
        log.unlink(missing_ok=True)
        im = images.Image.from_bytes("a.png", PNG)
        hub = self._hub({"codex": Found("codex", str(FAKE_CODEX))})
        with mock.patch.dict(os.environ, {"FAKE_CODEX_LOG": str(log)}):
            run = self._run(hub, "codex", Prompt("look", [im]))
        self.assertEqual(run.status, "done", run.lines)
        self.assertEqual(logged(log, "turn/start")[-1]["input"][1], im.codex())
        self.assertIn(("thought", "Looked at the image.\nNothing alarming."), run.lines)
        self.assertIn(("tool_output", "a.txt\n"), run.lines)


# ---- clipboard reading ------------------------------------------------------------------------

class ClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin = _env.BASE / "clipbin"
        cls.bin.mkdir(parents=True, exist_ok=True)
        for tool in ("wl-paste", "xclip"):
            script = cls.bin / tool
            script.write_text(f'#!/bin/sh\nexport FAKE_CLIP_TOOL={tool}\nexec "{sys.executable}" '
                              f'"{FAKE_CLIPBOARD}" "$@"\n')
            script.chmod(0o755)

    def _env(self, mode, *, wayland=True, log=None):
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_CLIP_MODE": mode,
               "WAYLAND_DISPLAY": "wayland-0" if wayland else "",
               "DISPLAY": ":99" if not wayland else ""}
        if log:
            env["FAKE_CLIP_LOG"] = str(log)
        return mock.patch.dict(os.environ, env)

    def _clipboard(self, mode, *, wayland=True, log=None):
        with self._env(mode, wayland=wayland, log=log):
            return run_async(images.clipboard())

    def test_wayland_partial_reads_are_joined_until_eof(self):
        im = self._clipboard("png-slow")
        self.assertIsInstance(im, images.Image)
        self.assertEqual(im.mime, "image/png")
        self.assertEqual(im.data, PNG)  # a single read() would return only the first 33 bytes

    def test_x11_partial_reads_are_joined_and_the_requested_type_is_used(self):
        log = LOGS / "clip-x11.log"
        log.unlink(missing_ok=True)
        im = self._clipboard("jpeg", wayland=False, log=log)
        self.assertEqual((im.mime, im.data), ("image/jpeg", JPEG))
        calls = log.read_text().splitlines()
        self.assertTrue(any(c.startswith("xclip jpeg -selection clipboard -o -t TARGETS") for c in calls), calls)
        self.assertTrue(any(c.startswith("xclip jpeg -selection clipboard -o -t image/jpeg") for c in calls), calls)

    def test_text_clipboard_comes_back_as_text(self):
        self.assertEqual(self._clipboard("text"), "hello from the clipboard")

    def test_an_oversized_clipboard_is_refused(self):
        with mock.patch.object(images, "MAX_BYTES", 32):
            with self._env("oversize"):
                with self.assertRaisesRegex(ValueError, "20 MiB"):
                    run_async(images.clipboard())

    def test_a_failing_clipboard_tool_is_reported(self):
        with self.assertRaisesRegex(ValueError, "could not read the clipboard"):
            self._clipboard("fail")

    def test_missing_clipboard_tools_explain_what_to_use_instead(self):
        empty = _env.BASE / "emptybin"
        empty.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"PATH": str(empty), "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":99"}):
            with self.assertRaisesRegex(ValueError, "wl-paste"):
                run_async(images.clipboard())

    def test_both_tools_present_prefers_wayland(self):
        log = LOGS / "clip-wayland.log"
        log.unlink(missing_ok=True)
        im = self._clipboard("png-slow", log=log)
        self.assertEqual(im.data, PNG)
        calls = log.read_text()
        self.assertIn("wl-paste", calls)
        self.assertNotIn("xclip", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
