import asyncio
from types import SimpleNamespace
import unittest

from agent_protocol import EventType, create_agent_client
from codex_agent import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    CodexAgentClient,
    _normalize_approval_policy,
)


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


class CodexAgentDefaultsTests(unittest.TestCase):
    def test_factory_uses_sol_high_by_default(self):
        client = create_agent_client("codex", project_dir="/tmp")

        self.assertEqual(client._model, DEFAULT_CODEX_MODEL)
        self.assertEqual(client._model, "gpt-5.6-sol")
        self.assertEqual(client._effort, DEFAULT_CODEX_EFFORT)
        self.assertEqual(client._effort, "high")

    def test_published_sdk_approval_modes_are_normalized(self):
        self.assertEqual(_normalize_approval_policy("auto"), "auto_review")
        self.assertEqual(_normalize_approval_policy("on-request"), "auto_review")
        self.assertEqual(_normalize_approval_policy("bypass"), "deny_all")
        self.assertEqual(
            _normalize_approval_policy("bypassPermissions"), "deny_all"
        )

    def test_raw_newer_cli_notifications_are_mapped_and_terminate(self):
        client = _client()
        client._session_id = "thread-1"

        class RawPayload:
            def __init__(self, params):
                self.params = params

        class FakeTurnHandle:
            async def stream(self):
                yield SimpleNamespace(
                    method="item/agentMessage/delta",
                    payload=RawPayload(
                        {"turnId": "turn-1", "itemId": "msg-1", "delta": "OK"}
                    ),
                )
                yield SimpleNamespace(
                    method="turn/completed",
                    payload=RawPayload(
                        {
                            "turn": {
                                "id": "turn-1",
                                "status": "completed",
                                "error": None,
                            }
                        }
                    ),
                )
                raise AssertionError("receive_events must stop at turn/completed")

        client._turn_handle = FakeTurnHandle()

        async def collect():
            return [event async for event in client.receive_events()]

        events = asyncio.run(collect())

        self.assertEqual(events[0].type, EventType.TEXT)
        self.assertEqual(events[0].text, "OK")
        self.assertEqual(events[-1].type, EventType.RESULT)


if __name__ == "__main__":
    unittest.main()
