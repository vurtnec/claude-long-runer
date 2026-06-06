from types import SimpleNamespace
import unittest

from agent_protocol import EventType
from codex_agent import CodexAgentClient


def _client() -> CodexAgentClient:
    client = object.__new__(CodexAgentClient)
    client._streamed_agent_text = ""
    client._streamed_agent_text_by_item = {}
    client._current_streamed_agent_item_id = None
    return client


class CodexAgentTextDedupTests(unittest.TestCase):
    def test_completed_agent_message_is_emitted_without_delta(self):
        client = _client()
        payload = SimpleNamespace(
            item=SimpleNamespace(type="agentMessage", id="msg-1", text="abcd")
        )

        event = client._on_item_completed("item/completed", payload)

        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.TEXT)
        self.assertEqual(event.text, "abcd")

    def test_completed_agent_message_is_skipped_after_identical_delta(self):
        client = _client()
        delta_payload = SimpleNamespace(item_id="msg-1", delta="abcd")
        completed_payload = SimpleNamespace(
            item=SimpleNamespace(type="agentMessage", id="msg-1", text="abcd")
        )

        delta_event = client._on_text_delta("item/agentMessage/delta", delta_payload)
        completed_event = client._on_item_completed(
            "item/completed", completed_payload
        )

        self.assertEqual(delta_event.text, "abcd")
        self.assertIsNone(completed_event)

    def test_completed_agent_message_only_emits_missing_suffix(self):
        client = _client()
        delta_payload = SimpleNamespace(item_id="msg-1", delta="abc")
        completed_payload = SimpleNamespace(
            item=SimpleNamespace(type="agentMessage", id="msg-1", text="abcd")
        )

        client._on_text_delta("item/agentMessage/delta", delta_payload)
        completed_event = client._on_item_completed(
            "item/completed", completed_payload
        )

        self.assertIsNotNone(completed_event)
        self.assertEqual(completed_event.type, EventType.TEXT)
        self.assertEqual(completed_event.text, "d")

    def test_completed_agent_message_without_delta_is_not_skipped_after_reset(self):
        client = _client()

        client._on_text_delta(
            "item/agentMessage/delta",
            SimpleNamespace(delta="foobar"),
        )
        duplicate_completed = client._on_item_completed(
            "item/completed",
            SimpleNamespace(item=SimpleNamespace(type="agentMessage", text="foobar")),
        )
        next_completed = client._on_item_completed(
            "item/completed",
            SimpleNamespace(item=SimpleNamespace(type="agentMessage", text="bar")),
        )

        self.assertIsNone(duplicate_completed)
        self.assertIsNotNone(next_completed)
        self.assertEqual(next_completed.text, "bar")


if __name__ == "__main__":
    unittest.main()
