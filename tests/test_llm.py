"""Offline checks for the Groq client wrapper (fake clients, no network)."""

import logging
import types
import unittest

import llm


class FakeError(Exception):
    def __init__(self, message, code=None, headers=None):
        super().__init__(message)
        self.status_code = code
        self.response = types.SimpleNamespace(status_code=code, headers=headers or {})


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        message = types.SimpleNamespace(content=f"{item}:{kwargs['model']}")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def setUpModule():
    logging.disable(logging.WARNING)  # the client logs every simulated failure


def tearDownModule():
    logging.disable(logging.NOTSET)


def make(scripts, **kwargs):
    clients = iter([FakeClient(s) for s in scripts])
    built = []

    def factory(_key):
        client = next(clients)
        built.append(client)
        return client

    clock = Clock()
    groq = llm.RotatingGroq([f"k{i}" for i in range(len(scripts))], client_factory=factory, sleep=clock.sleep, clock=clock, **kwargs)
    return groq, built


class RotatingGroqTests(unittest.TestCase):
    def test_rate_limited_key_is_skipped(self):
        groq, clients = make([[FakeError("rate_limit_exceeded, try again in 7.5s", 429)], []])
        out = llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10))
        self.assertEqual(out, "ok:llama-3.3-70b-versatile")
        self.assertEqual([len(c.calls) for c in clients], [1, 1])

    def test_retired_model_falls_through_and_is_remembered(self):
        groq, clients = make([[FakeError("The model `llama-3.3-70b-versatile` has been decommissioned", 400)]])
        first = llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10))
        self.assertEqual(first, "ok:openai/gpt-oss-120b")
        self.assertEqual(groq.current_model(), "openai/gpt-oss-120b")
        groq.call_with_retry(messages=[], max_tokens=10)
        self.assertEqual(len(clients[0].calls), 3)  # the retired model is not tried again
        # reasoning models get a bigger token budget and a low reasoning effort
        self.assertEqual(clients[0].calls[-1]["extra_body"], {"reasoning_effort": "low"})
        self.assertEqual(clients[0].calls[-1]["max_tokens"], 410)

    def test_rate_limits_are_per_model_so_the_next_model_still_works(self):
        groq, _ = make([[FakeError("rate limit", 429, {"retry-after": "30"})], [FakeError("rate limit", 429)]])
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")

    def test_a_preferred_model_that_was_only_rate_limited_is_used_again_once_it_recovers(self):
        groq, clients = make([[FakeError("rate limit", 429, {"retry-after": "30"})]])
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")
        self.assertEqual(groq.current_model(), "llama-3.3-70b-versatile")  # nothing was retired: it is only paused
        groq._clock.now += 60
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:llama-3.3-70b-versatile")
        self.assertEqual([c["model"] for c in clients[0].calls][-1], "llama-3.3-70b-versatile")

    def test_a_model_that_keeps_failing_with_server_errors_hands_over_to_the_next_one(self):
        groq, clients = make([[FakeError("overloaded", 503)] * 3])
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")
        self.assertEqual([c["model"] for c in clients[0].calls][:3], ["llama-3.3-70b-versatile"] * 3)
        # it was an outage, not a retirement: the preferred model is first in line again for the next request
        self.assertEqual(groq.current_model(), "llama-3.3-70b-versatile")

    def test_a_model_blocked_for_this_account_does_not_take_the_key_down(self):
        blocked = FakeError("model_permission_blocked_org: the model is blocked at the organization level", 403)
        groq, clients = make([[blocked]])
        self.assertEqual(llm.classify_error(blocked), "model_blocked")
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")
        self.assertEqual(sum(c["model"] == "llama-3.3-70b-versatile" for c in clients[0].calls), 1)  # not asked again
        groq._clock.now += llm.MODEL_BLOCKED_SECONDS + 1
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:llama-3.3-70b-versatile")

    def test_an_unexplained_403_pauses_one_model_not_the_whole_key(self):
        forbidden = FakeError("Forbidden", 403)
        groq, clients = make([[forbidden]])
        self.assertEqual(llm.classify_error(forbidden), "forbidden")
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:openai/gpt-oss-120b")
        groq._clock.now += llm.FORBIDDEN_BLOCK + 1
        self.assertEqual(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)), "ok:llama-3.3-70b-versatile")

    def test_error_classes(self):
        for error, kind in (
            (FakeError("x", 401), "bad_key"), (FakeError("Invalid API Key", 400), "bad_key"), (FakeError("x", 403), "forbidden"),
            (FakeError("x", 404), "model_gone"), (FakeError("x", 429), "rate_limit"), (FakeError("x", 413), "too_large"),
            (FakeError("x", 500), "transient"), (FakeError("x", 400), "fatal"),
            (FakeError("You must accept the terms: model_terms_required", 400), "model_blocked"),
        ):
            with self.subTest(code=error.status_code, message=str(error)):
                self.assertEqual(llm.classify_error(error), kind)

    def test_unsupported_penalties_are_dropped(self):
        groq, clients = make([[FakeError("`frequency_penalty` is not supported", 400)]])
        groq.call_with_retry(messages=[], max_tokens=10, frequency_penalty=0.7, presence_penalty=0.3)
        self.assertNotIn("frequency_penalty", clients[0].calls[-1])

    def test_oversized_requests_are_raised_for_the_caller_to_shrink(self):
        groq, _ = make([[FakeError("Request too large for model", 413)]])
        with self.assertRaises(FakeError) as caught:
            groq.call_with_retry(messages=[], max_tokens=10)
        self.assertTrue(llm.is_request_too_large(caught.exception))

    def test_transient_errors_are_retried(self):
        groq, _ = make([[FakeError("boom", 503)]])
        self.assertTrue(llm.extract_text(groq.call_with_retry(messages=[], max_tokens=10)).startswith("ok"))

    def test_bad_keys_everywhere_raise(self):
        groq, _ = make([[FakeError("Invalid API Key", 401)]])
        with self.assertRaises(FakeError):
            groq.call_with_retry(messages=[], max_tokens=10)

    def test_a_hanging_provider_is_abandoned_after_the_overall_deadline(self):
        clock = Clock()

        class Hanging(FakeClient):
            def create(self, **kwargs):
                clock.now += 60  # every attempt "takes" a minute, then times out
                raise FakeError("timeout", 503)

        client = Hanging([])
        groq = llm.RotatingGroq(["k"], client_factory=lambda _key: client, sleep=clock.sleep, clock=clock)
        with self.assertRaises(FakeError):
            groq.call_with_retry(messages=[], max_tokens=10)
        self.assertLessEqual(len(client.calls), 3)  # not 4 models x 3 attempts

    def test_vision_uses_its_own_model_chain(self):
        groq, clients = make([[]])
        groq.call_with_retry(vision=True, messages=[], max_tokens=10)
        self.assertIn("llama-4", clients[0].calls[-1]["model"])

    def test_helpers(self):
        self.assertEqual(llm.parse_model_list("a, b ,a", ("x",), first="z"), ("z", "a", "b"))
        self.assertEqual(llm.parse_model_list("", ("x", "y")), ("x", "y"))
        self.assertAlmostEqual(llm.retry_after_seconds(FakeError("Please try again in 1m23.5s", 429)), 83.5)
        self.assertEqual(llm.retry_after_seconds(FakeError("x", 429, {"retry-after": "12"})), 12.0)
        self.assertEqual(llm.extract_text(types.SimpleNamespace(choices=[])), "")
        self.assertEqual(llm.extract_text(types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=None))])), "")


if __name__ == "__main__":
    unittest.main()
