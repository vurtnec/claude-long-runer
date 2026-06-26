import tempfile
import unittest
from pathlib import Path
import asyncio

from agent_protocol import EventType, create_agent_client
from opencode_agent import (
    OpenCodeAgentClient,
    _normalize_session_entry,
    _should_skip_permissions,
)
from scheduler import daemon as daemon_module
from scheduler import feishu_bot as feishu_bot_module
from scheduler.daemon import _resolve_model_for_backend as resolve_daemon_model
from scheduler.feishu_bot import (
    FeishuBotServer,
    _is_auto_model,
    _is_claude_like_model,
    _model_display_for_backend,
    _resolve_schedule_model as resolve_feishu_schedule_model,
)


class OpenCodeAgentFactoryTests(unittest.TestCase):
    def test_factory_creates_opencode_client(self):
        client = create_agent_client(
            "opencode",
            project_dir="/tmp",
            model="anthropic/claude-sonnet-4-5",
            permission_mode="bypassPermissions",
            resume="ses_existing",
            effort="xhigh",
        )

        self.assertIsInstance(client, OpenCodeAgentClient)
        self.assertEqual(client.backend_name, "opencode")
        self.assertEqual(client.session_id, "ses_existing")

    def test_build_command_maps_session_model_effort_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = OpenCodeAgentClient(
                project_dir=tmp,
                model="anthropic/claude-sonnet-4-5",
                permission_mode="never",
                resume_session_id="ses_existing",
                effort="xhigh",
            )
            client._opencode_bin = "opencode"

            cmd = client._build_command("hello")

        self.assertEqual(cmd[:4], ["opencode", "run", "--format", "json"])
        self.assertIn("--dir", cmd)
        self.assertIn("--session", cmd)
        self.assertIn("ses_existing", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("anthropic/claude-sonnet-4-5", cmd)
        self.assertIn("--variant", cmd)
        self.assertIn("max", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(cmd[-2:], ["--", "hello"])

    def test_permission_aliases_that_skip_permissions(self):
        self.assertTrue(_should_skip_permissions("bypass"))
        self.assertTrue(_should_skip_permissions("bypassPermissions"))
        self.assertTrue(_should_skip_permissions("never"))
        self.assertFalse(_should_skip_permissions("auto"))


class OpenCodeJsonMappingTests(unittest.TestCase):
    def setUp(self):
        self.client = OpenCodeAgentClient(project_dir="/tmp")

    def test_text_event_updates_session_id_and_emits_text(self):
        event = self.client._map_json_event(
            {
                "type": "text",
                "sessionID": "ses_123",
                "part": {"type": "text", "text": "hello"},
            }
        )

        self.assertEqual(self.client.session_id, "ses_123")
        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.TEXT)
        self.assertEqual(event.text, "hello")

    def test_tool_use_event(self):
        event = self.client._map_json_event(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"cmd": "ls"}},
                },
            }
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.TOOL_USE)
        self.assertEqual(event.tool_name, "bash")
        self.assertEqual(event.tool_input, {"cmd": "ls"})

    def test_tool_result_event(self):
        event = self.client._map_json_event(
            {
                "type": "tool",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "output": "done"},
                },
            }
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.TOOL_RESULT)
        self.assertEqual(event.tool_name, "bash")
        self.assertEqual(event.result_content, "done")
        self.assertFalse(event.is_error)

    def test_tool_error_event(self):
        event = self.client._map_json_event(
            {
                "type": "tool",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "failed", "error": "bad command"},
                },
            }
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.TOOL_RESULT)
        self.assertTrue(event.is_error)
        self.assertEqual(event.result_content, "bad command")

    def test_step_finish_and_unknown_events_are_skipped(self):
        self.assertIsNone(
            self.client._map_json_event(
                {"type": "step_finish", "part": {"type": "step-finish"}}
            )
        )
        self.assertIsNone(self.client._map_json_event({"type": "unknown"}))

    def test_error_event(self):
        event = self.client._map_json_event(
            {"type": "error", "message": "model failed"}
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.type, EventType.ERROR)
        self.assertEqual(event.metadata["error"], "model failed")


class OpenCodeSessionListTests(unittest.TestCase):
    def test_normalize_session_entry_filters_other_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            with tempfile.TemporaryDirectory() as other:
                entry = {"id": "ses_1", "title": "Task", "cwd": other}

                self.assertIsNone(_normalize_session_entry(entry, tmp))

    def test_normalize_session_entry_maps_common_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "id": "ses_1",
                "title": "Task title",
                "cwd": str(Path(tmp).resolve()),
                "createdAt": 1_700_000_000_000,
                "updatedAt": "2026-06-26T12:00:00",
                "model": {
                    "providerID": "anthropic",
                    "modelID": "claude-sonnet-4-5",
                },
            }

            normalized = _normalize_session_entry(entry, tmp)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["session_id"], "ses_1")
        self.assertEqual(normalized["summary"], "Task title")
        self.assertEqual(normalized["backend"], "opencode")
        self.assertEqual(normalized["model"], "anthropic/claude-sonnet-4-5")


class OpenCodeSchedulerRoutingTests(unittest.TestCase):
    def test_daemon_omits_opencode_model_when_default_is_codex_model(self):
        self.assertIsNone(
            resolve_daemon_model(
                backend="opencode",
                schedule_model=None,
                default_model="gpt-5.5",
            )
        )

    def test_daemon_accepts_opencode_provider_model(self):
        self.assertEqual(
            resolve_daemon_model(
                backend="opencode",
                schedule_model=None,
                default_model="anthropic/claude-sonnet-4-5",
            ),
            "anthropic/claude-sonnet-4-5",
        )

    def test_schedule_model_override_wins_for_opencode(self):
        self.assertEqual(
            resolve_feishu_schedule_model(
                backend="opencode",
                schedule_model="github-copilot/gpt-5.4",
                default_model="gpt-5.5",
            ),
            "github-copilot/gpt-5.4",
        )

    def test_daemon_claude_uses_cc_switch_opus_when_default_is_codex_model(self):
        original = daemon_module._claude_settings_default_model
        daemon_module._claude_settings_default_model = lambda: "glm-5.2[1M]"
        try:
            self.assertEqual(
                resolve_daemon_model(
                    backend="claude",
                    schedule_model=None,
                    default_model="gpt-5.5",
                ),
                "glm-5.2[1M]",
            )
        finally:
            daemon_module._claude_settings_default_model = original

    def test_daemon_claude_auto_falls_back_to_opus_without_cc_switch(self):
        original = daemon_module._claude_settings_default_model
        daemon_module._claude_settings_default_model = lambda: None
        try:
            self.assertEqual(
                resolve_daemon_model(
                    backend="claude",
                    schedule_model="auto",
                    default_model="gpt-5.5",
                ),
                "claude-opus-4-8",
            )
        finally:
            daemon_module._claude_settings_default_model = original

    def test_feishu_schedule_claude_uses_cc_switch_opus(self):
        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: "glm-5.2[1M]"
        try:
            self.assertEqual(
                resolve_feishu_schedule_model(
                    backend="claude",
                    schedule_model=None,
                    default_model="gpt-5.5",
                ),
                "glm-5.2[1M]",
            )
        finally:
            feishu_bot_module._claude_settings_default_model = original


class OpenCodeFeishuCommandTests(unittest.TestCase):
    def _bot(self) -> FeishuBotServer:
        bot = object.__new__(FeishuBotServer)
        bot._sessions = {}
        bot._chat_backends = {}
        bot._chat_models = {}
        bot._chat_modes = {}
        bot.default_backend = "codex"
        bot.default_model = "gpt-5.5"
        bot.default_mode = "auto"
        bot._loop = None
        bot.default_project_dir = Path("/tmp")
        bot.projects = {}
        bot._chat_project_dirs = {}
        bot._project_models = {}
        bot._project_backends = {}
        bot._project_efforts = {}
        bot._project_restricted = {}
        bot.sent = []
        bot.replies = []
        bot.cards = []
        bot._send_message = lambda chat_id, text: bot.sent.append((chat_id, text))
        bot._reply_text = lambda message_id, text: bot.replies.append((message_id, text))
        bot._reply_card_json = lambda message_id, card_json, fallback_text="": bot.cards.append(
            (message_id, card_json, fallback_text)
        )
        return bot

    def test_backend_opencode_resets_model_to_opencode_default(self):
        bot = self._bot()
        bot._chat_models["chat-1"] = "gpt-5.5"

        bot._handle_backend("opencode", "chat-1", "msg-1")

        self.assertEqual(bot._chat_backends["chat-1"], "opencode")
        self.assertNotIn("chat-1", bot._chat_models)
        self.assertIn("OpenCode config default", bot.sent[0][1])

    def test_provider_model_auto_switches_to_opencode(self):
        bot = self._bot()

        bot._handle_model("anthropic/claude-sonnet-4-5", "chat-1", "msg-1")

        self.assertEqual(bot._chat_backends["chat-1"], "opencode")
        self.assertEqual(
            bot._chat_models["chat-1"], "anthropic/claude-sonnet-4-5"
        )
        self.assertIn("[opencode]", bot.sent[0][1])

    def test_opencode_model_without_provider_is_rejected(self):
        bot = self._bot()
        bot._chat_backends["chat-1"] = "opencode"

        bot._handle_model("sonnet", "chat-1", "msg-1")

        self.assertNotIn("chat-1", bot._chat_models)
        self.assertIn("provider/model", bot.replies[0][1])

    def test_mode_without_active_session_applies_to_next_session(self):
        bot = self._bot()

        bot._handle_mode("bypass", "chat-1", "msg-1")

        self.assertEqual(bot._chat_modes["chat-1"], "bypassPermissions")
        self.assertIn("Will apply to the next new session", bot.replies[0][1])

    def test_opencode_model_display_uses_config_default_when_unset(self):
        self.assertEqual(
            _model_display_for_backend("opencode", None),
            "OpenCode config default",
        )

    def test_claude_accepts_raw_glm_model_from_model_command(self):
        bot = self._bot()
        bot._chat_backends["chat-1"] = "claude"

        bot._handle_model("glm-5.2[1M]", "chat-1", "msg-1")

        self.assertEqual(bot._chat_models["chat-1"], "glm-5.2[1M]")
        self.assertIn("[claude]", bot.sent[0][1])

    def test_claude_rejects_codex_model_during_resolution(self):
        bot = self._bot()

        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: "glm-5.2[1M]"
        try:
            self.assertEqual(
                bot._resolve_model_for_backend("claude", "gpt-5.5"),
                "glm-5.2[1M]",
            )
        finally:
            feishu_bot_module._claude_settings_default_model = original

    def test_glm_is_treated_as_claude_like_model(self):
        self.assertTrue(_is_claude_like_model("glm-5.2[1M]"))

    def test_claude_session_uses_global_glm_default_model(self):
        bot = self._bot()
        with tempfile.TemporaryDirectory() as tmp:
            bot.default_project_dir = Path(tmp)
            bot.default_backend = "claude"
            bot.default_model = "glm-5.2[1M]"
            bot.default_effort = None
            bot._chat_project_dirs = {}
            bot._chat_efforts = {}
            bot._chat_modes = {}
            bot._project_models = {}
            bot._project_backends = {}
            bot._project_efforts = {}
            bot._project_restricted = {}
            bot.projects = {}

            captured = {}

            class FakeClient:
                async def connect(self):
                    return None

                async def disconnect(self):
                    return None

            def fake_create_agent_client(**kwargs):
                captured.update(kwargs)
                return FakeClient()

            original = feishu_bot_module.create_agent_client
            feishu_bot_module.create_agent_client = fake_create_agent_client
            try:
                session = asyncio.run(bot._get_or_create_session("chat-1"))
            finally:
                feishu_bot_module.create_agent_client = original

        self.assertEqual(captured["backend"], "claude")
        self.assertEqual(captured["model"], "glm-5.2[1M]")
        self.assertEqual(session.model, "glm-5.2[1M]")

    def test_claude_model_dropdown_includes_cc_switch_default(self):
        bot = self._bot()
        bot._chat_backends["chat-1"] = "claude"
        bot.default_model = "gpt-5.5"

        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: "glm-5.2[1M]"
        try:
            bot._handle_model(None, "chat-1", "msg-1")
        finally:
            feishu_bot_module._claude_settings_default_model = original

        self.assertEqual(len(bot.cards), 1)
        self.assertIn("glm-5.2[1M]", bot.cards[0][1])
        self.assertIn("glm-5.2[1M]  \u2190 current", bot.cards[0][1])

    def test_backend_claude_message_shows_cc_switch_default(self):
        bot = self._bot()
        bot.default_model = "gpt-5.5"

        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: "glm-5.2[1M]"
        try:
            bot._handle_backend("claude", "chat-1", "msg-1")
        finally:
            feishu_bot_module._claude_settings_default_model = original

        self.assertEqual(bot._chat_backends["chat-1"], "claude")
        self.assertIn("glm-5.2[1M]", bot.sent[0][1])

    def test_claude_default_falls_back_when_cc_switch_is_absent(self):
        bot = self._bot()
        bot.default_model = "gpt-5.5"

        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: None
        try:
            default_model = bot._default_model_for_backend("claude")
            bot._handle_backend("claude", "chat-1", "msg-1")
        finally:
            feishu_bot_module._claude_settings_default_model = original

        self.assertEqual(default_model, "claude-opus-4-8")
        self.assertIn("opus", bot.sent[0][1])

    def test_auto_model_falls_back_to_claude_builtin_without_cc_switch(self):
        bot = self._bot()
        bot.default_model = "auto"

        original = feishu_bot_module._claude_settings_default_model
        feishu_bot_module._claude_settings_default_model = lambda: None
        try:
            default_model = bot._default_model_for_backend("claude")
        finally:
            feishu_bot_module._claude_settings_default_model = original

        self.assertTrue(_is_auto_model("auto"))
        self.assertEqual(default_model, "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
