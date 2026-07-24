import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent
SCRIPT = PLUGIN_ROOT / "scripts" / "context_hook.py"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from agent_bus import AgentBus  # noqa: E402


class ContextHookTest(unittest.TestCase):
    def run_process(
        self,
        payload: dict,
        *,
        database: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if database is not None:
            environment["WORK_ASSIST_AGENT_BUS_PATH"] = str(database)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )

    def run_payload(
        self,
        payload: dict,
        *,
        database: Path | None = None,
    ) -> dict:
        completed = self.run_process(payload, database=database)
        return json.loads(completed.stdout)

    def run_hook(self, event_name: str) -> dict:
        return self.run_payload({"hook_event_name": event_name})

    def test_session_context(self) -> None:
        output = self.run_hook("SessionStart")
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "SessionStart")
        self.assertIn("Work Assist plugin is installed", specific["additionalContext"])

    def test_turn_context_has_safety_boundary(self) -> None:
        output = self.run_hook("UserPromptSubmit")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bounded, awaited", context)
        self.assertIn("Never expose passwords", context)

    def test_post_tool_use_without_session_or_mail_is_silent(self) -> None:
        no_session = self.run_process({"hook_event_name": "PostToolUse"})
        self.assertEqual(no_session.stdout, "")
        self.assertEqual(no_session.stderr, "")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "quiet-session",
                    "source": "startup",
                },
                database=database,
            )
            no_mail = self.run_process(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "quiet-session",
                    "turn_id": "turn",
                    "tool_name": "Bash",
                },
                database=database,
            )
            self.assertEqual(no_mail.stdout, "")
            self.assertEqual(no_mail.stderr, "")

    def test_post_tool_notice_is_bounded_and_does_not_consume_mail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            session_id = "post-tool-session"
            self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "source": "startup",
                },
                database=database,
            )
            bus = AgentBus(database)
            root = bus.root_identity(session_id)
            peer = bus.register_subagent(session_id, "peer")
            body = "Review the terminal reconnect result."
            message_id = bus.send_direct(peer.agent_id, root.agent_id, body)

            completed = self.run_process(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "turn_id": "turn",
                    "tool_name": "Bash",
                },
                database=database,
            )
            output = json.loads(completed.stdout)
            self.assertEqual(set(output), {"systemMessage"})
            self.assertLessEqual(len(output["systemMessage"]), 320)
            self.assertIn(root.agent_id, output["systemMessage"])
            self.assertNotIn(body, output["systemMessage"])
            self.assertEqual(completed.stderr, "")

            delivered = bus.take(root.agent_id)
            self.assertEqual([item.message_id for item in delivered], [message_id])
            self.assertEqual(delivered[0].body, body)

    def test_post_tool_routes_to_subagent_only_when_agent_id_is_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            session_id = "subagent-post-tool-session"
            self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "source": "startup",
                },
                database=database,
            )
            bus = AgentBus(database)
            child = bus.register_subagent(session_id, "native-child")
            peer = bus.register_subagent(session_id, "peer")
            body = "Child-only coordination note."
            bus.send_direct(peer.agent_id, child.agent_id, body)

            root_event = self.run_process(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "turn_id": "turn",
                    "tool_name": "apply_patch",
                },
                database=database,
            )
            self.assertEqual(root_event.stdout, "")

            child_event = self.run_process(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "agent_id": "native-child",
                    "turn_id": "turn",
                    "tool_name": "apply_patch",
                },
                database=database,
            )
            notice = json.loads(child_event.stdout)["systemMessage"]
            self.assertIn(child.agent_id, notice)
            self.assertNotIn(body, notice)
            self.assertEqual([item.body for item in bus.take(child.agent_id)], [body])

    def test_post_tool_database_failure_fails_open_silently(self) -> None:
        completed = self.run_process(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "unavailable-session",
                "turn_id": "turn",
                "tool_name": "Bash",
            },
            database=Path("/dev/null"),
        )
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_session_and_subagent_lifecycle_use_one_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            session_id = "hook-session"
            session = self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "source": "startup",
                    "transcript_path": "/path/that/must/not/be/read",
                },
                database=database,
            )
            context = session["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Work Assist agent identity:", context)
            self.assertRegex(
                context,
                r"surface-id=root-[a-z2-7]{12}",
            )
            self.assertNotIn(session_id, context)

            started = self.run_payload(
                {
                    "hook_event_name": "SubagentStart",
                    "session_id": session_id,
                    "agent_id": "native-child-id",
                    "agent_type": "researcher",
                },
                database=database,
            )
            self.assertEqual(
                started["hookSpecificOutput"]["hookEventName"],
                "SubagentStart",
            )
            self.assertRegex(
                started["hookSpecificOutput"]["additionalContext"],
                r"surface-id=agent-[a-z2-7]{12}",
            )
            bus = AgentBus(database)
            child = bus.agent_identity(session_id, "native-child-id")
            self.assertIsNotNone(child)

            stopped = self.run_payload(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": session_id,
                    "agent_id": "native-child-id",
                    "agent_type": "researcher",
                },
                database=database,
            )
            self.assertEqual(stopped, {"continue": True})
            agents = bus.list_agents(include_stopped=True)
            child_row = next(
                row for row in agents if row["agent_id"] == child.agent_id
            )
            self.assertEqual(child_row["state"], "stopped")

            root_stopped = self.run_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": "turn-one",
                },
                database=database,
            )
            self.assertEqual(root_stopped, {"continue": True})
            live_root = next(row for row in bus.list_agents() if row["role"] == "root")
            self.assertEqual(live_root["state"], "idle")

    def test_user_prompt_atomically_injects_inbox_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            session_id = "inbox-session"
            self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "source": "startup",
                },
                database=database,
            )
            bus = AgentBus(database)
            root = bus.root_identity(session_id)
            peer = bus.register_subagent(session_id, "peer")
            bus.send_direct(
                peer.agent_id,
                root.agent_id,
                "Review the relay lease test.",
            )

            first = self.run_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": "turn-one",
                    "prompt": "continue",
                },
                database=database,
            )
            first_context = first["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Review the relay lease test.", first_context)
            self.assertIn(peer.agent_id, first_context)

            second = self.run_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": "turn-two",
                    "prompt": "continue",
                },
                database=database,
            )
            self.assertNotIn(
                "Review the relay lease test.",
                second["hookSpecificOutput"]["additionalContext"],
            )

    def test_hook_inbox_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bus.sqlite3"
            session_id = "bounded-session"
            self.run_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": session_id,
                    "source": "startup",
                },
                database=database,
            )
            bus = AgentBus(database)
            root = bus.root_identity(session_id)
            peer = bus.register_subagent(session_id, "peer")
            for number in range(6):
                bus.send_direct(
                    peer.agent_id,
                    root.agent_id,
                    f"{number}:" + "x" * 1_900,
                )
            output = self.run_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": "turn",
                    "prompt": "continue",
                },
                database=database,
            )
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertLessEqual(len(context), 8_000)
            self.assertIn("Agent inbox:", context)

    def test_bus_failure_is_advisory_and_returns_valid_hook_json(self) -> None:
        output = self.run_payload(
            {
                "hook_event_name": "SessionStart",
                "session_id": "unavailable-session",
                "source": "startup",
            },
            database=Path("/dev/null/agent-bus.sqlite3"),
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("agent bus was unavailable", context)

    def test_hook_file_declares_all_agent_lifecycle_events(self) -> None:
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        self.assertEqual(
            set(hooks["hooks"]),
            {
                "SessionStart",
                "PostToolUse",
                "UserPromptSubmit",
                "SubagentStart",
                "SubagentStop",
                "Stop",
            },
        )
        post_tool = hooks["hooks"]["PostToolUse"][0]
        self.assertEqual(post_tool["matcher"], "*")
        self.assertEqual(post_tool["hooks"][0]["timeout"], 1)
        self.assertIn("python3 -S ", post_tool["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
