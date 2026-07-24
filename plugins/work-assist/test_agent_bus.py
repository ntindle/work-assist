import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


PLUGIN_ROOT = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import agent_bus  # noqa: E402
from agent_bus import AgentBus, BusError  # noqa: E402


class Clock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AgentBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "v1" / "agent-bus.sqlite3"
        self.clock = Clock()
        self.bus = AgentBus(self.path, clock=self.clock)
        self.root = self.bus.activate("session-alpha")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def subagent(self, native_id: str = "subagent-one"):
        return self.bus.register_subagent(
            "session-alpha", native_id, agent_type="worker"
        )

    def test_chat_id_is_deterministic_opaque_and_bounded(self) -> None:
        first = agent_bus.chat_id_for_session("session-alpha")
        second = agent_bus.chat_id_for_session("session-alpha")
        other = agent_bus.chat_id_for_session("session-beta")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^chat_[a-z2-7]{24}$")
        self.assertNotIn("session", first)
        with self.assertRaises(BusError):
            agent_bus.chat_id_for_session("")

    def test_default_surface_is_safe_and_unique_per_live_agent(self) -> None:
        child = self.subagent()
        root_surface = agent_bus.relay_surface_id(self.root)
        child_surface = agent_bus.relay_surface_id(child)
        self.assertRegex(root_surface, r"^root-[a-z2-7]{12}$")
        self.assertRegex(child_surface, r"^agent-[a-z2-7]{12}$")
        self.assertNotEqual(root_surface, child_surface)

    def test_activation_persists_through_compact_and_rotates_on_resume(self) -> None:
        compact = self.bus.activate("session-alpha", source="compact")
        self.assertEqual(compact.instance_id, self.root.instance_id)
        self.assertEqual(compact.agent_id, self.root.agent_id)

        resumed = self.bus.activate("session-alpha", source="resume")
        self.assertEqual(resumed.chat_id, self.root.chat_id)
        self.assertNotEqual(resumed.instance_id, self.root.instance_id)
        self.assertNotEqual(resumed.agent_id, self.root.agent_id)
        agents = self.bus.list_agents(include_stopped=True)
        old = next(row for row in agents if row["agent_id"] == self.root.agent_id)
        self.assertEqual(old["state"], "stopped")

    def test_root_inbox_migrates_to_the_new_activation(self) -> None:
        child = self.subagent()
        message_id = self.bus.send_direct(
            child.agent_id,
            self.root.agent_id,
            "Carry this note across sandbox resume.",
        )
        resumed = self.bus.activate("session-alpha", source="resume")
        delivered = self.bus.take(resumed.agent_id)
        self.assertEqual([item.message_id for item in delivered], [message_id])
        self.assertEqual(delivered[0].target, resumed.agent_id)

    def test_registers_root_and_subagent_with_leases(self) -> None:
        child = self.subagent()
        agents = self.bus.list_agents()
        self.assertEqual({row["agent_id"] for row in agents}, {
            self.root.agent_id,
            child.agent_id,
        })
        self.assertEqual(
            next(row for row in agents if row["agent_id"] == child.agent_id)["role"],
            "subagent",
        )
        self.assertEqual(
            self.bus.agent_identity("session-alpha", "subagent-one"),
            child,
        )

    def test_direct_claim_and_ack_are_transactional(self) -> None:
        child = self.subagent()
        message_id = self.bus.send_direct(
            self.root.agent_id, child.agent_id, "Please check the test boundary."
        )
        claimed = self.bus.claim(child.agent_id)
        self.assertEqual([item.message_id for item in claimed], [message_id])
        self.assertIsNotNone(claimed[0].claim_token)
        self.assertEqual(
            self.bus.ack(
                child.agent_id,
                ((message_id, claimed[0].claim_token or ""),),
            ),
            1,
        )
        self.assertEqual(self.bus.claim(child.agent_id), [])

    def test_multi_ack_rolls_back_when_any_claim_is_invalid(self) -> None:
        child = self.subagent()
        self.bus.send_direct(self.root.agent_id, child.agent_id, "first note")
        self.bus.send_direct(self.root.agent_id, child.agent_id, "second note")
        claimed = self.bus.claim(child.agent_id, limit=2)
        self.assertEqual(len(claimed), 2)
        with self.assertRaises(BusError):
            self.bus.ack(
                child.agent_id,
                (
                    (claimed[0].message_id, claimed[0].claim_token or ""),
                    (claimed[1].message_id, "claim_" + "0" * 32),
                ),
            )
        self.assertEqual(
            self.bus.ack(
                child.agent_id,
                tuple(
                    (item.message_id, item.claim_token or "") for item in claimed
                ),
            ),
            2,
        )

    def test_expired_claim_is_requeued_with_a_new_token(self) -> None:
        child = self.subagent()
        self.bus.send_direct(self.root.agent_id, child.agent_id, "lease check")
        first = self.bus.claim(child.agent_id, visibility_seconds=5)
        self.clock.advance(6)
        second = self.bus.claim(child.agent_id, visibility_seconds=5)
        self.assertEqual(first[0].message_id, second[0].message_id)
        self.assertNotEqual(first[0].claim_token, second[0].claim_token)

    def test_take_atomically_acknowledges_a_bounded_inbox(self) -> None:
        child = self.subagent()
        for body in ("aaaa", "bbbb", "cccc"):
            self.bus.send_direct(self.root.agent_id, child.agent_id, body)
        first = self.bus.take(child.agent_id, limit=3, max_body_bytes=8)
        self.assertEqual([item.body for item in first], ["aaaa", "bbbb"])
        second = self.bus.take(child.agent_id)
        self.assertEqual([item.body for item in second], ["cccc"])
        self.assertEqual(self.bus.take(child.agent_id), [])

    def test_pending_probe_is_read_only_indexed_and_leaves_mail_deliverable(
        self,
    ) -> None:
        child = self.subagent()
        message_id = self.bus.send_direct(
            self.root.agent_id,
            child.agent_id,
            "Do not consume this from the hook probe.",
        )

        self.assertIsNone(
            self.bus.pending_agent_id("session-alpha", native_agent_id="root")
        )
        self.assertEqual(
            self.bus.pending_agent_id(
                "session-alpha",
                native_agent_id="subagent-one",
            ),
            child.agent_id,
        )

        delivered = self.bus.take(child.agent_id)
        self.assertEqual([item.message_id for item in delivered], [message_id])
        self.assertIsNone(
            self.bus.pending_agent_id(
                "session-alpha",
                native_agent_id="subagent-one",
            )
        )
        self.assertIn("INDEXED BY deliveries_inbox", agent_bus.PENDING_AGENT_QUERY)

    def test_pending_probe_does_not_create_an_unavailable_database(self) -> None:
        missing = Path(self.temporary.name) / "missing" / "bus.sqlite3"
        probe = AgentBus(missing, clock=self.clock)
        self.assertIsNone(probe.pending_agent_id("session-alpha"))
        self.assertFalse(missing.exists())

    def test_topic_delivery_is_fanout_and_excludes_sender(self) -> None:
        first = self.subagent("first")
        second = self.subagent("second")
        for agent in (self.root, first, second):
            self.bus.subscribe(agent.agent_id, "geometry.arm")
        message_id, fanout = self.bus.publish(
            self.root.agent_id, "geometry.arm", "Coordinate the joint envelope."
        )
        self.assertEqual(fanout, 2)
        self.assertEqual(self.bus.take(self.root.agent_id), [])
        for child in (first, second):
            delivered = self.bus.take(child.agent_id)
            self.assertEqual([item.message_id for item in delivered], [message_id])
            self.assertEqual(delivered[0].target, "geometry.arm")

    def test_direct_and_topic_delivery_cross_top_level_chats(self) -> None:
        other_root = self.bus.activate("session-beta")
        self.bus.subscribe(self.root.agent_id, "work-assist.desktop")
        self.bus.subscribe(other_root.agent_id, "work-assist.desktop")

        message_id, fanout = self.bus.publish(
            self.root.agent_id,
            "work-assist.desktop",
            "Desktop surface beta is ready for review.",
        )
        self.assertEqual(fanout, 1)
        topic_delivery = self.bus.take(other_root.agent_id)
        self.assertEqual(
            [delivery.message_id for delivery in topic_delivery],
            [message_id],
        )
        self.assertEqual(topic_delivery[0].kind, "topic")

        direct_id = self.bus.send_direct(
            other_root.agent_id,
            self.root.agent_id,
            "Review complete; keep terminal alpha open.",
        )
        direct_delivery = self.bus.take(self.root.agent_id)
        self.assertEqual(
            [delivery.message_id for delivery in direct_delivery],
            [direct_id],
        )
        self.assertEqual(direct_delivery[0].kind, "direct")

    def test_topic_fanout_bound_fails_without_partial_delivery(self) -> None:
        first = self.subagent("first")
        second = self.subagent("second")
        self.bus.subscribe(first.agent_id, "busy.topic")
        self.bus.subscribe(second.agent_id, "busy.topic")
        with mock.patch.object(agent_bus, "MAX_FANOUT", 1):
            with self.assertRaises(BusError):
                self.bus.publish(
                    self.root.agent_id, "busy.topic", "bounded fanout check"
                )
        self.assertEqual(self.bus.take(first.agent_id), [])
        self.assertEqual(self.bus.take(second.agent_id), [])

    def test_expired_message_is_not_delivered(self) -> None:
        child = self.subagent()
        self.bus.send_direct(
            self.root.agent_id, child.agent_id, "short lived", ttl_seconds=1
        )
        self.clock.advance(2)
        self.assertEqual(self.bus.take(child.agent_id), [])

    def test_body_topic_ttl_and_secret_bounds(self) -> None:
        child = self.subagent()
        with self.assertRaises(BusError):
            self.bus.send_direct(self.root.agent_id, child.agent_id, "")
        with self.assertRaises(BusError):
            self.bus.send_direct(
                self.root.agent_id,
                child.agent_id,
                "x" * (agent_bus.MAX_BODY_BYTES + 1),
            )
        with self.assertRaises(BusError):
            self.bus.send_direct(
                self.root.agent_id,
                child.agent_id,
                "Authorization: Bearer this-is-not-allowed",
            )
        with self.assertRaises(BusError):
            self.bus.publish(self.root.agent_id, "Invalid Topic", "hello")
        with self.assertRaises(BusError):
            self.bus.send_direct(
                self.root.agent_id, child.agent_id, "hello", ttl_seconds=0
            )

    def test_stop_and_lease_expiry_remove_agent_from_live_listing(self) -> None:
        child = self.subagent()
        self.bus.stop(child.agent_id)
        self.assertNotIn(
            child.agent_id,
            {row["agent_id"] for row in self.bus.list_agents()},
        )
        self.clock.advance(agent_bus.DEFAULT_AGENT_LEASE_SECONDS + 1)
        self.assertEqual(self.bus.list_agents(), [])

    def test_concurrent_claim_has_one_winner(self) -> None:
        child = self.subagent()
        self.bus.send_direct(self.root.agent_id, child.agent_id, "claim once")
        barrier = threading.Barrier(2)
        result: list[int] = []
        errors: list[Exception] = []

        def claim() -> None:
            try:
                local = AgentBus(self.path, clock=self.clock)
                barrier.wait()
                result.append(len(local.claim(child.agent_id, limit=1)))
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result), [0, 1])

    def test_database_and_parent_are_private(self) -> None:
        self.bus.list_agents()
        self.assertEqual(
            stat.S_IMODE(os.stat(self.path).st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.path.parent).st_mode),
            0o700,
        )

    def test_cli_sends_from_stdin_and_takes_json_delivery(self) -> None:
        realtime_bus = AgentBus(self.path)
        root = realtime_bus.activate("cli-session")
        child = realtime_bus.register_subagent("cli-session", "cli-child")
        script = PLUGIN_ROOT / "scripts" / "agent_bus.py"
        sent = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(self.path),
                "send-direct",
                "--from-agent",
                root.agent_id,
                "--to-agent",
                child.agent_id,
            ],
            input="CLI coordination note",
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(sent.stdout)["fanout"], 1)
        received = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(self.path),
                "receive",
                "--agent",
                child.agent_id,
                "--take",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        messages = json.loads(received.stdout)["messages"]
        self.assertEqual([message["body"] for message in messages], [
            "CLI coordination note"
        ])


if __name__ == "__main__":
    unittest.main()
