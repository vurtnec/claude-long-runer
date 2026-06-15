import asyncio
import unittest

from scheduler.notifiers import teams_reply_notifier
from scheduler.notifiers.teams_reply_notifier import TeamsReplyNotifier


class FakeTeamsClient:
    def __init__(self):
        self.sent = []

    def send_chat_message(self, chat_id, text, content_type="text"):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "content_type": content_type,
            }
        )
        return {"id": "message-1"}


class TeamsReplyNotifierTests(unittest.TestCase):
    def setUp(self):
        self.fake_client = FakeTeamsClient()
        self.original_get_teams_client = teams_reply_notifier.get_teams_client
        teams_reply_notifier.get_teams_client = lambda: self.fake_client

    def tearDown(self):
        teams_reply_notifier.get_teams_client = self.original_get_teams_client

    def test_allow_any_sender_sends_without_whitelist(self):
        notifier = TeamsReplyNotifier({})

        sent = asyncio.run(
            notifier.send(
                {
                    "allow_any_sender": True,
                    "title": "PR Review",
                    "body": "{{last_response}}",
                },
                {
                    "chat_id": "chat-1",
                    "sender_name": "Unlisted Sender",
                    "sender_id": "sender-1",
                    "last_response": "Looks good.",
                },
            )
        )

        self.assertTrue(sent)
        self.assertEqual(len(self.fake_client.sent), 1)
        self.assertEqual(self.fake_client.sent[0]["chat_id"], "chat-1")
        self.assertEqual(self.fake_client.sent[0]["text"], "PR Review\n\nLooks good.")

    def test_target_chat_id_overrides_trigger_chat(self):
        notifier = TeamsReplyNotifier({})

        sent = asyncio.run(
            notifier.send(
                {
                    "allow_any_sender": True,
                    "target_chat_id": "review-chat",
                    "body": "{{last_response}}",
                },
                {
                    "chat_id": "source-chat",
                    "sender_name": "Unlisted Sender",
                    "sender_id": "sender-1",
                    "last_response": "Looks good.",
                },
            )
        )

        self.assertTrue(sent)
        self.assertEqual(len(self.fake_client.sent), 1)
        self.assertEqual(self.fake_client.sent[0]["chat_id"], "review-chat")

    def test_empty_whitelist_still_skips_by_default(self):
        notifier = TeamsReplyNotifier({})

        sent = asyncio.run(
            notifier.send(
                {"body": "{{last_response}}"},
                {
                    "chat_id": "chat-1",
                    "sender_name": "Unlisted Sender",
                    "sender_id": "sender-1",
                    "last_response": "Looks good.",
                },
            )
        )

        self.assertFalse(sent)
        self.assertEqual(self.fake_client.sent, [])

    def test_whitelist_still_sends_for_matching_sender(self):
        notifier = TeamsReplyNotifier({})

        sent = asyncio.run(
            notifier.send(
                {
                    "whitelist": ["Listed Sender"],
                    "body": "{{last_response}}",
                },
                {
                    "chat_id": "chat-1",
                    "sender_name": "Listed Sender",
                    "sender_id": "sender-1",
                    "last_response": "Looks good.",
                },
            )
        )

        self.assertTrue(sent)
        self.assertEqual(len(self.fake_client.sent), 1)
        self.assertEqual(self.fake_client.sent[0]["text"], "Looks good.")

    def test_configured_max_chars_truncates_reply(self):
        notifier = TeamsReplyNotifier({})

        sent = asyncio.run(
            notifier.send(
                {
                    "allow_any_sender": True,
                    "max_chars": 100,
                    "body": "{{last_response}}",
                },
                {
                    "chat_id": "chat-1",
                    "sender_name": "Unlisted Sender",
                    "sender_id": "sender-1",
                    "last_response": "x" * 200,
                },
            )
        )

        self.assertTrue(sent)
        self.assertEqual(len(self.fake_client.sent), 1)
        self.assertEqual(len(self.fake_client.sent[0]["text"]), 100)
        self.assertTrue(self.fake_client.sent[0]["text"].endswith("…(truncated)"))


if __name__ == "__main__":
    unittest.main()
