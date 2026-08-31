"""The local-CLI inference transport: opt-in, batching, and the silent rate-limit shape.

No test here runs the binary. What matters offline is that inference stays *off* until an
installation asks for it, that a rate-limited call is recognised as such rather than read
as a hard failure, and that a run's time budget is shared rather than granted per call.
"""

import os
import time
import unittest
from contextlib import contextmanager

from coreyard import ai


@contextmanager
def environment(**values):
    """Set real environment variables, since config._get reads them ahead of .env."""
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class OptIn(unittest.TestCase):
    """The Shopify pipeline never reaches this module; nothing may switch it on by proxy."""

    def test_inference_is_off_unless_explicitly_enabled(self):
        with environment(COREYARD_AI_ENABLED=None):
            self.assertFalse(ai.enabled())
        with environment(COREYARD_AI_ENABLED="true"):
            self.assertTrue(ai.enabled())

    def test_a_disabled_installation_refuses_before_spawning_anything(self):
        with environment(COREYARD_AI_ENABLED="false"):
            with self.assertRaises(ai.Unavailable):
                ai.ask_json("prompt", {"type": "object"})

    def test_unavailable_is_an_ai_error_so_one_except_clause_covers_both(self):
        self.assertTrue(issubclass(ai.Unavailable, ai.AIError))
        self.assertTrue(issubclass(ai.RateLimited, ai.AIError))

    def test_the_binary_is_configurable_and_defaults_to_a_cli_alias(self):
        with environment(COREYARD_AI_BIN=None, COREYARD_AI_MODEL=None):
            self.assertEqual(ai.binary(), ai.DEFAULT_BIN)
            self.assertEqual(ai.model(), ai.DEFAULT_MODEL)
        with environment(COREYARD_AI_BIN="  other  ", COREYARD_AI_MODEL=" opus "):
            self.assertEqual(ai.binary(), "other")
            self.assertEqual(ai.model(), "opus")

    def test_a_nonsense_timeout_is_reported_rather_than_silently_defaulted(self):
        with environment(COREYARD_AI_TIMEOUT="soon"):
            with self.assertRaises(ai.AIError):
                ai.call_timeout()

    def test_an_empty_timeout_falls_back_to_the_default(self):
        with environment(COREYARD_AI_TIMEOUT=""):
            self.assertEqual(ai.call_timeout(), ai.DEFAULT_TIMEOUT)


class Deadline(unittest.TestCase):
    def test_no_budget_means_unbounded(self):
        with environment(COREYARD_AI_DEADLINE=None):
            self.assertIsNone(ai.deadline_now())
        with environment(COREYARD_AI_DEADLINE="0"):
            self.assertIsNone(ai.deadline_now())

    def test_a_budget_becomes_one_absolute_deadline_for_the_whole_run(self):
        with environment(COREYARD_AI_DEADLINE="60"):
            deadline = ai.deadline_now()
        self.assertIsNotNone(deadline)
        self.assertAlmostEqual(deadline - time.monotonic(), 60, delta=5)

    def test_a_wait_that_would_overrun_the_deadline_is_not_started(self):
        """Dying inside sleep() banks nothing; stopping lets the caller checkpoint."""
        soon = time.monotonic() + 5
        self.assertFalse(ai._may_wait(0, 5, 60, soon))
        self.assertTrue(ai._may_wait(0, 5, 1, soon))

    def test_the_last_attempt_never_waits(self):
        self.assertFalse(ai._may_wait(5, 5, 1, None))

    def test_an_unbounded_run_still_waits(self):
        self.assertTrue(ai._may_wait(0, 5, 600, None))


class RateLimitShape(unittest.TestCase):
    """A limited call fails instantly with no error text; only the usage counters say so."""

    LIMITED = {"is_error": True, "duration_api_ms": 0, "num_turns": 1,
               "usage": {"input_tokens": 0, "output_tokens": 0}}

    def test_the_silent_limited_envelope_is_recognised(self):
        self.assertTrue(ai._looks_rate_limited(self.LIMITED))

    def test_a_genuine_failure_that_spent_tokens_is_not_a_rate_limit(self):
        spent = {**self.LIMITED, "usage": {"input_tokens": 900, "output_tokens": 20}}
        self.assertFalse(ai._looks_rate_limited(spent))

    def test_a_failure_that_took_api_time_is_not_a_rate_limit(self):
        self.assertFalse(ai._looks_rate_limited({**self.LIMITED,
                                                 "duration_api_ms": 1200}))

    def test_a_failure_after_several_turns_is_not_a_rate_limit(self):
        self.assertFalse(ai._looks_rate_limited({**self.LIMITED, "num_turns": 4}))

    def test_a_successful_envelope_is_never_a_rate_limit(self):
        self.assertFalse(ai._looks_rate_limited({**self.LIMITED, "is_error": False}))

    def test_cache_reads_count_as_tokens_spent(self):
        cached = {**self.LIMITED,
                  "usage": {"cache_read_input_tokens": 13_000, "output_tokens": 0}}
        self.assertFalse(ai._looks_rate_limited(cached))


class Batching(unittest.TestCase):
    """One call carries ~13k tokens of overhead, so batch size is a cost decision."""

    def test_every_item_appears_exactly_once(self):
        values = list(range(25))
        batched = list(ai.batches(values, 10))
        self.assertEqual([len(item) for item in batched], [10, 10, 5])
        self.assertEqual([item for batch in batched for item in batch], values)

    def test_an_empty_sequence_yields_no_calls(self):
        self.assertEqual(list(ai.batches([], 10)), [])

    def test_a_zero_batch_size_is_refused_rather_than_looping_forever(self):
        with self.assertRaises(ValueError):
            list(ai.batches([1], 0))

    def test_a_generator_is_consumed_safely(self):
        self.assertEqual(list(ai.batches((n for n in range(5)), 2)),
                         [[0, 1], [2, 3], [4]])


class MissingBinary(unittest.TestCase):
    def test_a_binary_that_is_not_installed_says_which_setting_to_change(self):
        with environment(COREYARD_AI_ENABLED="true",
                         COREYARD_AI_BIN="coreyard-no-such-binary"):
            self.assertFalse(ai.installed())
            with self.assertRaises(ai.Unavailable) as caught:
                ai.ask_json("prompt", {"type": "object"}, retries=0)
        message = str(caught.exception)
        self.assertIn("COREYARD_AI_BIN", message)
        self.assertIn("COREYARD_AI_ENABLED", message)


if __name__ == "__main__":
    unittest.main()
