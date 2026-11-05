"""Offline checks for voice_handler (Fish Audio + gTTS fallback) and character_vision, against local servers."""

import base64
import importlib.util
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

REQUIRED = ("httpx", "ormsgpack", "requests", "dotenv")
MISSING = [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def setUpModule():
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


class _Server:
    """A throw-away HTTP server on localhost; ``handler(path, headers, body) -> (status, bytes, content_type)``."""

    def __init__(self, handler):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.requests.append((self.path, dict(self.headers), body))
                status, payload, ctype = handler(self.path, self.headers, body)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipIf(MISSING, f"dependencies not installed: {MISSING}")
class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import voice_handler

        self.voice = voice_handler

    def serve(self, handler):
        server = _Server(handler)
        self.addCleanup(server.close)
        self.enterContext(patch.object(self.voice, "FISH_API_URL", server.url + "/v1/tts"))
        return server

    async def test_fish_audio_request_and_response(self):
        import ormsgpack

        server = self.serve(lambda path, headers, body: (200, b"FISH-MP3", "audio/mpeg"))
        audio = await self.voice.get_audio_mooded("Well, this is fine.", "secret-key", mood=-8, style="tense")
        self.assertEqual(audio, b"FISH-MP3")
        _, headers, body = server.requests[0]
        self.assertEqual(headers["authorization"], "Bearer secret-key")
        payload = ormsgpack.unpackb(body)
        self.assertEqual(payload["format"], "mp3")
        self.assertEqual(payload["chunk_length"], 120)           # tense + low mood -> short chunks
        self.assertEqual(payload["text"], "Well. this is fine.")  # "tense" style breaks clauses

    async def test_failure_falls_back_to_gtts_without_blocking_the_loop(self):
        self.serve(lambda path, headers, body: (500, b"nope", "text/plain"))
        with patch.object(self.voice, "_gtts_blocking", return_value=b"GTTS") as fallback:
            self.assertEqual(await self.voice.get_audio_mooded("Hello there.", "key"), b"GTTS")
            self.assertEqual(await self.voice.get_audio("Hello there.", "key"), b"GTTS")
            self.assertEqual(await self.voice.get_audio_mooded("Hello there.", ""), b"GTTS")  # no key at all
        self.assertEqual(fallback.call_count, 3)

    async def test_empty_text_is_never_synthesised(self):
        with patch.object(self.voice, "_gtts_blocking", return_value=b"GTTS") as fallback:
            self.assertIsNone(await self.voice.get_audio("   ", "key"))
            self.assertIsNone(await self.voice.get_audio_mooded("", ""))
        fallback.assert_not_called()

    def test_style_changes_do_not_break_numbers(self):
        self.assertEqual(self.voice._style_tts_text("It costs 1,000; fine, then", "tense"), "It costs 1,000. fine. then")
        self.assertEqual(self.voice._style_tts_text("a and b but c", "cutting"), "a. b. c")


@unittest.skipIf(MISSING, f"dependencies not installed: {MISSING}")
class VisionTests(unittest.TestCase):
    def setUp(self):
        import character_vision

        self.vision = character_vision

    def serve(self, handler):
        server = _Server(handler)
        self.addCleanup(server.close)
        self.enterContext(patch.object(self.vision, "XAI_BASE_URL", server.url))
        self.enterContext(patch.object(self.vision, "XAI_API_KEY", "xai-test"))
        return server

    def test_image_request_and_reply(self):
        import json

        server = self.serve(lambda p, h, b: (200, json.dumps({"choices": [{"message": {"content": " A cat. "}}]}).encode(), "application/json"))
        self.assertTrue(self.vision.vision_available())
        reply = self.vision.ask_character_bot("scaramouche", "What is this?", image_bytes=b"\x89PNG", mime_type="image/png", system_prompt="sys")
        self.assertEqual(reply, "A cat.")
        _, headers, body = server.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer xai-test")
        payload = json.loads(body)
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        image_part = payload["messages"][1]["content"][1]["image_url"]["url"]
        self.assertEqual(image_part, "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode())

    def test_malformed_provider_answer_is_a_clear_error(self):
        self.serve(lambda p, h, b: (200, b'{"unexpected": true}', "application/json"))
        with self.assertRaises(RuntimeError):
            self.vision.ask_character_bot("scaramouche", "hi")

    def test_http_errors_are_raised(self):
        import requests

        self.serve(lambda p, h, b: (429, b"{}", "application/json"))
        with self.assertRaises(requests.HTTPError):
            self.vision.ask_character_bot("wanderer", "hi")

    def test_missing_key_and_unknown_bot(self):
        with patch.object(self.vision, "XAI_API_KEY", ""):
            self.assertFalse(self.vision.vision_available())
            with self.assertRaises(RuntimeError):
                self.vision.ask_character_bot("scaramouche", "hi")
        with patch.object(self.vision, "XAI_API_KEY", "k"), self.assertRaises(ValueError):
            self.vision.ask_character_bot("someone-else", "hi")


if __name__ == "__main__":
    unittest.main()
