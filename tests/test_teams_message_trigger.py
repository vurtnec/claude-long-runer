import unittest

from scheduler.schedule_loader import parse_task_ref, parse_trigger_config
from scheduler.teams_client import TeamsMessage
from scheduler.triggers.teams_trigger import TeamsMessageTrigger


PR_URL = "https://org.visualstudio.com/Project/_git/repo/pullrequests/123"
PR_PATTERN = (
    r"https://(?P<org>[^.]+)\.visualstudio\.com/"
    r"(?P<project>[^/]+)/_git/(?P<repo>[^/]+)/pullrequests?/(?P<pr_id>\d+)"
)


def message(
    *,
    chat_id: str = "chat-1",
    chat_topic: str = "General",
    sender_name: str = "Someone Else",
    sender_id: str = "sender-1",
    body: str = PR_URL,
) -> TeamsMessage:
    return TeamsMessage(
        id=f"{chat_id}-msg",
        chat_id=chat_id,
        chat_topic=chat_topic,
        chat_type="group",
        created_at="2026-05-21T01:02:03.000Z",
        sender_name=sender_name,
        sender_id=sender_id,
        body_html=body,
        body_text=body,
        raw={},
    )


class FakeTeamsClient:
    def __init__(self, *, chats=None, messages_by_chat=None, watermarks=None):
        self.chats = chats or []
        self.messages_by_chat = messages_by_chat or {}
        self.watermarks = watermarks or {}
        self.fetched_chat_ids = []

    def get_my_identity(self):
        return "me-id", "Me"

    def is_self_chat(self, chat_id):
        return False

    def list_chats(self, top=50):
        return self.chats[:top]

    def get_watermark(self, owner, chat_id):
        return self.watermarks.get((owner, chat_id))

    def set_watermark(self, owner, chat_id, ts):
        self.watermarks[(owner, chat_id)] = ts

    def initialize_watermark_to_now(self, owner, chat_id):
        self.watermarks[(owner, chat_id)] = "2026-05-21T00:00:00.000Z"

    def get_chat_messages(self, chat_id, since=None, top=50):
        self.fetched_chat_ids.append(chat_id)
        return self.messages_by_chat.get(chat_id, [])


class TeamsMessageTriggerAllowlistTests(unittest.TestCase):
    def test_sender_allowlist_blocks_untrusted_pr_links(self):
        trigger = TeamsMessageTrigger(
            {
                "content_pattern": PR_PATTERN,
                "allowed_sender_displaynames": ["Evan Chen"],
            },
            client=FakeTeamsClient(),
        )

        self.assertIsNotNone(
            trigger._matches(message(sender_name="Evan Chen", sender_id="evan-id"))
        )
        self.assertIsNone(
            trigger._matches(message(sender_name="Unrelated User", sender_id="other-id"))
        )

    def test_allowlist_matches_sender_or_chat_topic(self):
        trigger = TeamsMessageTrigger(
            {
                "content_pattern": PR_PATTERN,
                "allowed_sender_displaynames": ["Evan Chen"],
                "allowed_chat_topic_contains": ["PMP PR Review"],
            },
            client=FakeTeamsClient(),
        )

        self.assertIsNotNone(
            trigger._matches(
                message(
                    chat_topic="PMP PR Review Squad",
                    sender_name="Unrelated User",
                    sender_id="other-id",
                )
            )
        )

    def test_chat_allowlist_avoids_fetching_untrusted_chats(self):
        client = FakeTeamsClient(
            chats=[
                {"id": "trusted", "topic": "PMP PR Review"},
                {"id": "large-group", "topic": "Large Unrelated Group"},
            ],
            messages_by_chat={
                "trusted": [
                    message(
                        chat_id="trusted",
                        chat_topic="PMP PR Review",
                        sender_name="Reviewer",
                    )
                ],
                "large-group": [
                    message(
                        chat_id="large-group",
                        chat_topic="Large Unrelated Group",
                        sender_name="Reviewer",
                    )
                ],
            },
            watermarks={
                ("teams_pr_review", "trusted"): "2026-05-21T00:00:00.000Z",
                ("teams_pr_review", "large-group"): "2026-05-21T00:00:00.000Z",
            },
        )
        trigger = TeamsMessageTrigger(
            {
                "content_pattern": PR_PATTERN,
                "allowed_chat_topic_contains": ["PMP PR Review"],
            },
            client=client,
        )
        trigger.owner_name = "teams_pr_review"

        result = trigger.evaluate()

        self.assertTrue(result.fired)
        self.assertEqual(client.fetched_chat_ids, ["trusted"])

    def test_loader_parses_allowlist_fields(self):
        config = parse_trigger_config(
            {
                "type": "teams_message",
                "allowed_sender_displaynames": "Evan Chen",
                "allowed_sender_ids": ["aad-1"],
                "allowed_chat_topic_contains": ["PMP PR Review"],
                "allowed_chat_ids": ["chat-1"],
                "scan_chat_limit": 30,
            }
        )

        self.assertEqual(config.allowed_sender_displaynames, ["Evan Chen"])
        self.assertEqual(config.allowed_sender_ids, ["aad-1"])
        self.assertEqual(config.allowed_chat_topic_contains, ["PMP PR Review"])
        self.assertEqual(config.allowed_chat_ids, ["chat-1"])
        self.assertEqual(config.scan_chat_limit, 30)


class ScheduleTaskRefTests(unittest.TestCase):
    def test_loader_rejects_unknown_task_type(self):
        with self.assertRaisesRegex(ValueError, "Unsupported task.type"):
            parse_task_ref({"task": {"type": "inlineopus-4.8", "prompt": "hi"}})

    def test_loader_rejects_standard_task_without_name(self):
        with self.assertRaisesRegex(ValueError, "Standard tasks require task.name"):
            parse_task_ref({"task": {"type": "standard"}})

    def test_loader_rejects_inline_task_without_prompt(self):
        with self.assertRaisesRegex(ValueError, "Inline tasks require task.prompt"):
            parse_task_ref({"task": {"type": "inline"}})


if __name__ == "__main__":
    unittest.main()
