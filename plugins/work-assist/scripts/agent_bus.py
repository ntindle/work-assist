#!/usr/bin/env python3
"""Small, non-secret SQLite message bus for Work Assist agents.

The bus coordinates agents that can see the shared ``/workspace`` volume.  It
does not read transcripts, inspect process environments, or carry credentials.
Messages are deliberately bounded and common credential shapes are rejected.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from typing import Callable, Iterable, Iterator, Sequence


DEFAULT_DB_PATH = Path("/workspace/.work-assist/v1/agent-bus.sqlite3")
SCHEMA_VERSION = 1
MAX_BODY_BYTES = 2_048
MAX_TOPIC_BYTES = 64
MAX_AGENT_TYPE_BYTES = 64
MAX_SESSION_ID_BYTES = 512
MAX_TTL_SECONDS = 86_400
MIN_TTL_SECONDS = 1
DEFAULT_TTL_SECONDS = 3_600
MAX_FANOUT = 32
MAX_CLAIM = 32
DEFAULT_AGENT_LEASE_SECONDS = 30 * 60
DEFAULT_CHAT_LEASE_SECONDS = 2 * 60 * 60
DEFAULT_VISIBILITY_SECONDS = 5 * 60
ACK_RETENTION_SECONDS = 60 * 60
STALE_CHAT_RETENTION_SECONDS = 7 * 24 * 60 * 60

TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,63}$")
ADDRESS_RE = re.compile(r"^agent_[a-z2-7]{20}$")
CHAT_RE = re.compile(r"^chat_[a-z2-7]{24}$")
INSTANCE_RE = re.compile(r"^inst_[a-f0-9]{24}$")
SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bCF-Access-Client-Secret\b", re.I),
    re.compile(r"\bAuthorization\s*:\s*(?:Bearer|Basic)\s+\S+", re.I),
    re.compile(
        r"\b(?:password|passwd|api[_-]?key|client[_-]?secret|"
        r"access[_-]?token)\s*[:=]\s*\S+",
        re.I,
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
)


class BusError(ValueError):
    """Expected validation or state error."""


@dataclass(frozen=True)
class Identity:
    chat_id: str
    instance_id: str
    agent_id: str
    role: str
    agent_type: str


@dataclass(frozen=True)
class Delivery:
    message_id: int
    sender_agent_id: str
    kind: str
    target: str
    body: str
    created_at: float
    expires_at: float
    claim_token: str | None = None


def _opaque(prefix: str, source: bytes, characters: int) -> str:
    digest = hashlib.sha256(source).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return prefix + encoded[:characters]


def chat_id_for_session(session_id: str) -> str:
    """Derive a stable opaque chat address without retaining the session id."""

    if not isinstance(session_id, str):
        raise BusError("session id must be a string")
    encoded = session_id.encode("utf-8")
    if not encoded or len(encoded) > MAX_SESSION_ID_BYTES or "\x00" in session_id:
        raise BusError("session id is invalid")
    return _opaque("chat_", b"work-assist-chat-v1\0" + encoded, 24)


def agent_id_for(chat_id: str, instance_id: str, native_id: str) -> str:
    _validate_id(chat_id, CHAT_RE, "chat id")
    _validate_id(instance_id, INSTANCE_RE, "instance id")
    if not isinstance(native_id, str) or not native_id or "\x00" in native_id:
        raise BusError("native agent id is invalid")
    if len(native_id.encode("utf-8")) > MAX_SESSION_ID_BYTES:
        raise BusError("native agent id is too long")
    source = (
        b"work-assist-agent-v1\0"
        + chat_id.encode("ascii")
        + b"\0"
        + instance_id.encode("ascii")
        + b"\0"
        + native_id.encode("utf-8")
    )
    return _opaque("agent_", source, 20)


def relay_session_id(chat_id: str) -> str:
    """Map the local chat address to Work Assist's non-secret 128-bit ID."""

    _validate_id(chat_id, CHAT_RE, "chat id")
    return hashlib.sha256(
        b"work-assist-relay-session-v2\0" + chat_id.encode("ascii")
    ).hexdigest()[:32]


def relay_instance_id(instance_id: str) -> str:
    """Map one activation to Work Assist's non-secret 128-bit ID."""

    _validate_id(instance_id, INSTANCE_RE, "instance id")
    return hashlib.sha256(
        b"work-assist-relay-instance-v2\0" + instance_id.encode("ascii")
    ).hexdigest()[:32]


def relay_surface_id(identity: Identity) -> str:
    """Allocate a collision-resistant default surface for one live agent."""

    _validate_id(identity.agent_id, ADDRESS_RE, "agent id")
    if identity.role not in {"root", "subagent"}:
        raise BusError("agent role is invalid")
    prefix = "root" if identity.role == "root" else "agent"
    return f"{prefix}-{identity.agent_id[-12:]}"


def _validate_id(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BusError(f"{name} is invalid")
    return value


def validate_topic(topic: str) -> str:
    if (
        not isinstance(topic, str)
        or len(topic.encode("utf-8")) > MAX_TOPIC_BYTES
        or not TOPIC_RE.fullmatch(topic)
    ):
        raise BusError("topic is invalid")
    return topic


def validate_body(body: str) -> str:
    if not isinstance(body, str):
        raise BusError("message body must be text")
    encoded = body.encode("utf-8")
    if not encoded or len(encoded) > MAX_BODY_BYTES:
        raise BusError(f"message body must be 1-{MAX_BODY_BYTES} UTF-8 bytes")
    if any(ord(character) < 0x20 and character not in "\n\t" for character in body):
        raise BusError("message body contains a control character")
    if any(pattern.search(body) for pattern in SENSITIVE_PATTERNS):
        raise BusError("message appears to contain a credential or secret")
    return body


def validate_ttl(ttl_seconds: int) -> int:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        raise BusError(
            f"TTL must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds"
        )
    return ttl_seconds


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    activation_started_at REAL NOT NULL,
    lease_until REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (length(chat_id) = 29),
    CHECK (length(instance_id) = 29)
) STRICT;

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL,
    native_id_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('root', 'subagent')),
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'idle', 'stopped', 'expired')),
    registered_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    lease_until REAL NOT NULL,
    UNIQUE (chat_id, instance_id, native_id_hash)
) STRICT;

CREATE INDEX IF NOT EXISTS agents_chat_instance
ON agents(chat_id, instance_id, state, lease_until);

CREATE TABLE IF NOT EXISTS subscriptions (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (agent_id, topic)
) WITHOUT ROWID, STRICT;

CREATE INDEX IF NOT EXISTS subscriptions_topic
ON subscriptions(topic, agent_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    kind TEXT NOT NULL CHECK (kind IN ('direct', 'topic')),
    target TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    fanout_count INTEGER NOT NULL,
    CHECK (fanout_count >= 0 AND fanout_count <= 32)
) STRICT;

CREATE INDEX IF NOT EXISTS messages_expiry ON messages(expires_at);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    recipient_agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'acked')),
    claim_token TEXT,
    claimed_until REAL,
    acked_at REAL,
    PRIMARY KEY (message_id, recipient_agent_id)
) WITHOUT ROWID, STRICT;

CREATE INDEX IF NOT EXISTS deliveries_inbox
ON deliveries(recipient_agent_id, state, message_id);
"""

PENDING_AGENT_QUERY = """
SELECT a.agent_id
  FROM chats AS c
  JOIN agents AS a
    ON a.chat_id = c.chat_id
   AND a.instance_id = c.instance_id
   AND a.native_id_hash = ?
 WHERE c.chat_id = ?
   AND c.lease_until > ?
   AND a.state IN ('active', 'idle')
   AND a.lease_until > ?
   AND EXISTS (
       SELECT 1
         FROM deliveries AS d INDEXED BY deliveries_inbox
         JOIN messages AS m ON m.message_id = d.message_id
        WHERE d.recipient_agent_id = a.agent_id
          AND d.state = 'pending'
          AND m.expires_at > ?
   )
 LIMIT 1
"""


class AgentBus:
    """Transactional agent registry and bounded delivery queue."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        configured = os.environ.get("WORK_ASSIST_AGENT_BUS_PATH")
        self.path = Path(path or configured or DEFAULT_DB_PATH)
        if not self.path.is_absolute():
            raise BusError("agent bus path must be absolute")
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        previous_umask = os.umask(0o077)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            connection = sqlite3.connect(
                self.path,
                timeout=1.5,
                isolation_level=None,
            )
        finally:
            os.umask(previous_umask)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 1500")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA trusted_schema = OFF")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            version = SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            connection.close()
            raise BusError("unsupported agent bus schema")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

    def pending_agent_id(
        self,
        session_id: str,
        native_agent_id: str = "root",
    ) -> str | None:
        """Return the addressed agent only when it has live pending mail.

        This is the hook hot path: it never creates or migrates the database,
        acquires a write transaction, refreshes a lease, claims a delivery, or
        reads a message body.  The inbox probe is a single read-only query that
        is forced through ``deliveries_inbox``.
        """

        chat_id = chat_id_for_session(session_id)
        if (
            not isinstance(native_agent_id, str)
            or not native_agent_id
            or "\x00" in native_agent_id
            or len(native_agent_id.encode("utf-8")) > MAX_SESSION_ID_BYTES
        ):
            raise BusError("native agent id is invalid")
        if not self.path.exists():
            return None

        now = float(self.clock())
        native_hash = hashlib.sha256(native_agent_id.encode("utf-8")).hexdigest()
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=ro",
            uri=True,
            timeout=0.05,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA busy_timeout = 50")
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            row = connection.execute(
                PENDING_AGENT_QUERY,
                (native_hash, chat_id, now, now, now),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[tuple[sqlite3.Connection, float]]:
        connection = self._connect()
        now = float(self.clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, now)
            yield connection, now
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _cleanup(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE deliveries
               SET state = 'pending', claim_token = NULL, claimed_until = NULL
             WHERE state = 'claimed' AND claimed_until <= ?
            """,
            (now,),
        )
        connection.execute(
            """
            UPDATE agents
               SET state = 'expired'
             WHERE state IN ('active', 'idle') AND lease_until <= ?
            """,
            (now,),
        )
        connection.execute("DELETE FROM messages WHERE expires_at <= ?", (now,))
        connection.execute(
            """
            DELETE FROM messages
             WHERE message_id IN (
                 SELECT m.message_id
                   FROM messages AS m
                  WHERE NOT EXISTS (
                      SELECT 1 FROM deliveries AS d
                       WHERE d.message_id = m.message_id
                         AND d.state != 'acked'
                  )
                    AND m.created_at <= ?
             )
            """,
            (now - ACK_RETENTION_SECONDS,),
        )
        stale_before = now - STALE_CHAT_RETENTION_SECONDS
        connection.execute(
            """
            DELETE FROM chats
             WHERE lease_until <= ?
               AND updated_at <= ?
               AND NOT EXISTS (
                   SELECT 1 FROM messages AS m
                   JOIN agents AS a ON a.agent_id = m.sender_agent_id
                    WHERE a.chat_id = chats.chat_id
               )
            """,
            (now, stale_before),
        )

    def activate(
        self,
        session_id: str,
        *,
        source: str = "startup",
        agent_type: str = "root",
    ) -> Identity:
        """Start or resume one activation and register its root agent."""

        chat_id = chat_id_for_session(session_id)
        source = source if source in {"startup", "resume", "clear", "compact"} else "startup"
        agent_type = self._validate_agent_type(agent_type)
        with self._transaction() as (connection, now):
            row = connection.execute(
                "SELECT instance_id FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            rotate = row is None or source != "compact"
            old_root_agent_id: str | None = None
            if rotate:
                old_root = connection.execute(
                    """
                    SELECT agent_id FROM agents
                     WHERE chat_id = ? AND role = 'root'
                     ORDER BY registered_at DESC LIMIT 1
                    """,
                    (chat_id,),
                ).fetchone()
                if old_root is not None:
                    old_root_agent_id = old_root["agent_id"]
                instance_id = "inst_" + secrets.token_hex(12)
                connection.execute(
                    "UPDATE agents SET state = 'stopped', lease_until = ? WHERE chat_id = ?",
                    (now, chat_id),
                )
                connection.execute(
                    """
                    INSERT INTO chats(
                        chat_id, instance_id, activation_started_at,
                        lease_until, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        instance_id = excluded.instance_id,
                        activation_started_at = excluded.activation_started_at,
                        lease_until = excluded.lease_until,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        instance_id,
                        now,
                        now + DEFAULT_CHAT_LEASE_SECONDS,
                        now,
                    ),
                )
            else:
                instance_id = row["instance_id"]
                connection.execute(
                    """
                    UPDATE chats SET lease_until = ?, updated_at = ?
                     WHERE chat_id = ?
                    """,
                    (now + DEFAULT_CHAT_LEASE_SECONDS, now, chat_id),
                )
            identity = self._register(
                connection,
                now,
                chat_id=chat_id,
                instance_id=instance_id,
                native_id="root",
                role="root",
                agent_type=agent_type,
                topics=(f"chat/{chat_id}",),
            )
            if old_root_agent_id is not None:
                # A top-level chat's inbox survives sandbox reactivation even
                # though its live agent address is fenced per activation.
                connection.execute(
                    """
                    UPDATE messages SET target = ?
                     WHERE kind = 'direct' AND target = ?
                       AND expires_at > ?
                       AND EXISTS (
                           SELECT 1 FROM deliveries AS d
                            WHERE d.message_id = messages.message_id
                              AND d.recipient_agent_id = ?
                              AND d.state IN ('pending', 'claimed')
                       )
                    """,
                    (
                        identity.agent_id,
                        old_root_agent_id,
                        now,
                        old_root_agent_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE deliveries
                       SET recipient_agent_id = ?, state = 'pending',
                           claim_token = NULL, claimed_until = NULL
                     WHERE recipient_agent_id = ?
                       AND state IN ('pending', 'claimed')
                    """,
                    (identity.agent_id, old_root_agent_id),
                )
            return identity

    def ensure_root(self, session_id: str, *, agent_type: str = "root") -> Identity:
        """Get the current activation, creating one if SessionStart was missed."""

        chat_id = chat_id_for_session(session_id)
        agent_type = self._validate_agent_type(agent_type)
        with self._transaction() as (connection, now):
            row = connection.execute(
                "SELECT instance_id FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                instance_id = "inst_" + secrets.token_hex(12)
                connection.execute(
                    """
                    INSERT INTO chats(
                        chat_id, instance_id, activation_started_at,
                        lease_until, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        instance_id,
                        now,
                        now + DEFAULT_CHAT_LEASE_SECONDS,
                        now,
                    ),
                )
            else:
                instance_id = row["instance_id"]
                connection.execute(
                    "UPDATE chats SET lease_until = ?, updated_at = ? WHERE chat_id = ?",
                    (now + DEFAULT_CHAT_LEASE_SECONDS, now, chat_id),
                )
            return self._register(
                connection,
                now,
                chat_id=chat_id,
                instance_id=instance_id,
                native_id="root",
                role="root",
                agent_type=agent_type,
                topics=(f"chat/{chat_id}",),
            )

    def register_subagent(
        self,
        session_id: str,
        native_agent_id: str,
        *,
        agent_type: str = "subagent",
    ) -> Identity:
        root = self.ensure_root(session_id)
        with self._transaction() as (connection, now):
            return self._register(
                connection,
                now,
                chat_id=root.chat_id,
                instance_id=root.instance_id,
                native_id=native_agent_id,
                role="subagent",
                agent_type=self._validate_agent_type(agent_type),
                topics=(f"chat/{root.chat_id}",),
            )

    @staticmethod
    def _validate_agent_type(agent_type: str) -> str:
        if not isinstance(agent_type, str):
            raise BusError("agent type must be text")
        encoded = agent_type.encode("utf-8")
        if not encoded or len(encoded) > MAX_AGENT_TYPE_BYTES or "\x00" in agent_type:
            raise BusError("agent type is invalid")
        return agent_type

    def _register(
        self,
        connection: sqlite3.Connection,
        now: float,
        *,
        chat_id: str,
        instance_id: str,
        native_id: str,
        role: str,
        agent_type: str,
        topics: Iterable[str],
    ) -> Identity:
        address = agent_id_for(chat_id, instance_id, native_id)
        native_hash = hashlib.sha256(native_id.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO agents(
                agent_id, chat_id, instance_id, native_id_hash, role,
                agent_type, state, registered_at, last_seen_at, lease_until
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                agent_type = excluded.agent_type,
                state = 'active',
                last_seen_at = excluded.last_seen_at,
                lease_until = excluded.lease_until
            """,
            (
                address,
                chat_id,
                instance_id,
                native_hash,
                role,
                agent_type,
                now,
                now,
                now + DEFAULT_AGENT_LEASE_SECONDS,
            ),
        )
        for topic in topics:
            connection.execute(
                """
                INSERT OR IGNORE INTO subscriptions(agent_id, topic, created_at)
                VALUES(?, ?, ?)
                """,
                (address, validate_topic(topic), now),
            )
        return Identity(chat_id, instance_id, address, role, agent_type)

    def root_identity(self, session_id: str) -> Identity:
        root = self.ensure_root(session_id)
        return root

    def agent_identity(self, session_id: str, native_agent_id: str) -> Identity | None:
        root = self.ensure_root(session_id)
        address = agent_id_for(root.chat_id, root.instance_id, native_agent_id)
        with self._transaction() as (connection, _):
            row = connection.execute(
                """
                SELECT chat_id, instance_id, agent_id, role, agent_type
                  FROM agents WHERE agent_id = ?
                """,
                (address,),
            ).fetchone()
            return None if row is None else Identity(**dict(row))

    def subscribe(self, agent_id: str, topic: str) -> None:
        _validate_id(agent_id, ADDRESS_RE, "agent id")
        topic = validate_topic(topic)
        with self._transaction() as (connection, now):
            self._require_agent(connection, agent_id, now)
            connection.execute(
                """
                INSERT OR IGNORE INTO subscriptions(agent_id, topic, created_at)
                VALUES(?, ?, ?)
                """,
                (agent_id, topic, now),
            )

    def unsubscribe(self, agent_id: str, topic: str) -> None:
        _validate_id(agent_id, ADDRESS_RE, "agent id")
        topic = validate_topic(topic)
        with self._transaction() as (connection, now):
            self._require_agent(connection, agent_id, now)
            connection.execute(
                "DELETE FROM subscriptions WHERE agent_id = ? AND topic = ?",
                (agent_id, topic),
            )

    @staticmethod
    def _require_agent(
        connection: sqlite3.Connection,
        agent_id: str,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM agents
             WHERE agent_id = ?
               AND state IN ('active', 'idle')
               AND lease_until > ?
            """,
            (agent_id, now),
        ).fetchone()
        if row is None:
            raise BusError("agent is unknown or its lease expired")
        return row

    def send_direct(
        self,
        sender_agent_id: str,
        recipient_agent_id: str,
        body: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> int:
        _validate_id(sender_agent_id, ADDRESS_RE, "sender agent id")
        _validate_id(recipient_agent_id, ADDRESS_RE, "recipient agent id")
        body = validate_body(body)
        ttl_seconds = validate_ttl(ttl_seconds)
        with self._transaction() as (connection, now):
            self._require_agent(connection, sender_agent_id, now)
            self._require_agent(connection, recipient_agent_id, now)
            cursor = connection.execute(
                """
                INSERT INTO messages(
                    sender_agent_id, kind, target, body,
                    created_at, expires_at, fanout_count
                ) VALUES(?, 'direct', ?, ?, ?, ?, 1)
                """,
                (
                    sender_agent_id,
                    recipient_agent_id,
                    body,
                    now,
                    now + ttl_seconds,
                ),
            )
            message_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO deliveries(message_id, recipient_agent_id, state)
                VALUES(?, ?, 'pending')
                """,
                (message_id, recipient_agent_id),
            )
            return message_id

    def publish(
        self,
        sender_agent_id: str,
        topic: str,
        body: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> tuple[int, int]:
        _validate_id(sender_agent_id, ADDRESS_RE, "sender agent id")
        topic = validate_topic(topic)
        body = validate_body(body)
        ttl_seconds = validate_ttl(ttl_seconds)
        with self._transaction() as (connection, now):
            self._require_agent(connection, sender_agent_id, now)
            recipients = connection.execute(
                """
                SELECT s.agent_id
                  FROM subscriptions AS s
                  JOIN agents AS a ON a.agent_id = s.agent_id
                 WHERE s.topic = ?
                   AND s.agent_id != ?
                   AND a.state IN ('active', 'idle')
                   AND a.lease_until > ?
                 ORDER BY s.agent_id
                 LIMIT ?
                """,
                (topic, sender_agent_id, now, MAX_FANOUT + 1),
            ).fetchall()
            if len(recipients) > MAX_FANOUT:
                raise BusError(f"topic fanout exceeds {MAX_FANOUT} recipients")
            cursor = connection.execute(
                """
                INSERT INTO messages(
                    sender_agent_id, kind, target, body,
                    created_at, expires_at, fanout_count
                ) VALUES(?, 'topic', ?, ?, ?, ?, ?)
                """,
                (
                    sender_agent_id,
                    topic,
                    body,
                    now,
                    now + ttl_seconds,
                    len(recipients),
                ),
            )
            message_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO deliveries(message_id, recipient_agent_id, state)
                VALUES(?, ?, 'pending')
                """,
                ((message_id, row["agent_id"]) for row in recipients),
            )
            return message_id, len(recipients)

    @staticmethod
    def _delivery_from_row(
        row: sqlite3.Row,
        *,
        claim_token: str | None = None,
    ) -> Delivery:
        return Delivery(
            message_id=row["message_id"],
            sender_agent_id=row["sender_agent_id"],
            kind=row["kind"],
            target=row["target"],
            body=row["body"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            claim_token=claim_token,
        )

    @staticmethod
    def _pending_rows(
        connection: sqlite3.Connection,
        recipient_agent_id: str,
        now: float,
        limit: int,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT m.*
              FROM deliveries AS d
              JOIN messages AS m ON m.message_id = d.message_id
             WHERE d.recipient_agent_id = ?
               AND d.state = 'pending'
               AND m.expires_at > ?
             ORDER BY m.message_id
             LIMIT ?
            """,
            (recipient_agent_id, now, limit),
        ).fetchall()

    def claim(
        self,
        recipient_agent_id: str,
        *,
        limit: int = 8,
        visibility_seconds: int = DEFAULT_VISIBILITY_SECONDS,
    ) -> list[Delivery]:
        _validate_id(recipient_agent_id, ADDRESS_RE, "recipient agent id")
        if not 1 <= limit <= MAX_CLAIM:
            raise BusError(f"claim limit must be 1-{MAX_CLAIM}")
        if not 1 <= visibility_seconds <= MAX_TTL_SECONDS:
            raise BusError("visibility timeout is invalid")
        with self._transaction() as (connection, now):
            self._require_agent(connection, recipient_agent_id, now)
            rows = self._pending_rows(connection, recipient_agent_id, now, limit)
            deliveries: list[Delivery] = []
            for row in rows:
                token = "claim_" + secrets.token_hex(16)
                updated = connection.execute(
                    """
                    UPDATE deliveries
                       SET state = 'claimed', claim_token = ?, claimed_until = ?
                     WHERE message_id = ? AND recipient_agent_id = ?
                       AND state = 'pending'
                    """,
                    (
                        token,
                        now + visibility_seconds,
                        row["message_id"],
                        recipient_agent_id,
                    ),
                )
                if updated.rowcount == 1:
                    deliveries.append(
                        self._delivery_from_row(row, claim_token=token)
                    )
            return deliveries

    def ack(
        self,
        recipient_agent_id: str,
        claims: Sequence[tuple[int, str]],
    ) -> int:
        _validate_id(recipient_agent_id, ADDRESS_RE, "recipient agent id")
        if not claims or len(claims) > MAX_CLAIM:
            raise BusError(f"ack requires 1-{MAX_CLAIM} claims")
        with self._transaction() as (connection, now):
            self._require_agent(connection, recipient_agent_id, now)
            for message_id, token in claims:
                if (
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id < 1
                    or not isinstance(token, str)
                    or not re.fullmatch(r"claim_[a-f0-9]{32}", token)
                ):
                    raise BusError("claim is invalid")
                updated = connection.execute(
                    """
                    UPDATE deliveries
                       SET state = 'acked', acked_at = ?,
                           claim_token = NULL, claimed_until = NULL
                     WHERE message_id = ? AND recipient_agent_id = ?
                       AND state = 'claimed' AND claim_token = ?
                    """,
                    (now, message_id, recipient_agent_id, token),
                )
                if updated.rowcount != 1:
                    raise BusError("claim is stale or belongs to another agent")
            return len(claims)

    def take(
        self,
        recipient_agent_id: str,
        *,
        limit: int = 6,
        max_body_bytes: int = 6_144,
    ) -> list[Delivery]:
        """Atomically fetch and acknowledge a bounded hook inbox."""

        _validate_id(recipient_agent_id, ADDRESS_RE, "recipient agent id")
        if not 1 <= limit <= MAX_CLAIM:
            raise BusError(f"take limit must be 1-{MAX_CLAIM}")
        if not 1 <= max_body_bytes <= MAX_BODY_BYTES * MAX_CLAIM:
            raise BusError("take byte bound is invalid")
        with self._transaction() as (connection, now):
            self._require_agent(connection, recipient_agent_id, now)
            rows = self._pending_rows(connection, recipient_agent_id, now, limit)
            deliveries: list[Delivery] = []
            used = 0
            for row in rows:
                size = len(row["body"].encode("utf-8"))
                if deliveries and used + size > max_body_bytes:
                    break
                if size > max_body_bytes:
                    continue
                updated = connection.execute(
                    """
                    UPDATE deliveries
                       SET state = 'acked', acked_at = ?
                     WHERE message_id = ? AND recipient_agent_id = ?
                       AND state = 'pending'
                    """,
                    (now, row["message_id"], recipient_agent_id),
                )
                if updated.rowcount == 1:
                    used += size
                    deliveries.append(self._delivery_from_row(row))
            return deliveries

    def mark_idle(self, agent_id: str) -> None:
        _validate_id(agent_id, ADDRESS_RE, "agent id")
        with self._transaction() as (connection, now):
            row = self._require_agent(connection, agent_id, now)
            connection.execute(
                """
                UPDATE agents
                   SET state = 'idle', last_seen_at = ?, lease_until = ?
                 WHERE agent_id = ?
                """,
                (now, now + DEFAULT_AGENT_LEASE_SECONDS, row["agent_id"]),
            )

    def stop(self, agent_id: str) -> None:
        _validate_id(agent_id, ADDRESS_RE, "agent id")
        with self._transaction() as (connection, now):
            connection.execute(
                """
                UPDATE agents
                   SET state = 'stopped', last_seen_at = ?, lease_until = ?
                 WHERE agent_id = ?
                """,
                (now, now, agent_id),
            )

    def list_agents(self, *, include_stopped: bool = False) -> list[dict[str, object]]:
        with self._transaction() as (connection, now):
            condition = "" if include_stopped else "WHERE state IN ('active', 'idle') AND lease_until > ?"
            parameters: tuple[object, ...] = () if include_stopped else (now,)
            rows = connection.execute(
                f"""
                SELECT agent_id, chat_id, instance_id, role, agent_type,
                       state, last_seen_at, lease_until
                  FROM agents {condition}
                 ORDER BY chat_id, role, agent_id
                """,
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]


def _read_body() -> str:
    raw = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise BusError(f"message body exceeds {MAX_BODY_BYTES} bytes")
    try:
        return validate_body(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise BusError("message body is not valid UTF-8") from error


def _print_json(value: object) -> None:
    json.dump(value, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded non-secret Work Assist agent message bus"
    )
    parser.add_argument("--db", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    agents = commands.add_parser("agents", help="list live agent addresses")
    agents.add_argument("--include-stopped", action="store_true")

    subscribe = commands.add_parser("subscribe", help="subscribe an agent to a topic")
    subscribe.add_argument("--agent", required=True)
    subscribe.add_argument("--topic", required=True)

    unsubscribe = commands.add_parser(
        "unsubscribe", help="remove an agent topic subscription"
    )
    unsubscribe.add_argument("--agent", required=True)
    unsubscribe.add_argument("--topic", required=True)

    direct = commands.add_parser(
        "send-direct",
        help="read a non-secret body from stdin and send it to one agent",
    )
    direct.add_argument("--from-agent", required=True)
    direct.add_argument("--to-agent", required=True)
    direct.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)

    publish = commands.add_parser(
        "publish",
        help="read a non-secret body from stdin and publish it to a topic",
    )
    publish.add_argument("--from-agent", required=True)
    publish.add_argument("--topic", required=True)
    publish.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)

    receive = commands.add_parser("receive", help="claim pending messages")
    receive.add_argument("--agent", required=True)
    receive.add_argument("--limit", type=int, default=8)
    receive.add_argument(
        "--visibility-seconds", type=int, default=DEFAULT_VISIBILITY_SECONDS
    )
    receive.add_argument(
        "--take",
        action="store_true",
        help="atomically acknowledge returned messages (used by hooks)",
    )

    ack = commands.add_parser("ack", help="acknowledge one claimed message")
    ack.add_argument("--agent", required=True)
    ack.add_argument("--message-id", required=True, type=int)
    ack.add_argument("--claim-token", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        bus = AgentBus(arguments.db)
        if arguments.command == "agents":
            _print_json(
                {"agents": bus.list_agents(include_stopped=arguments.include_stopped)}
            )
        elif arguments.command == "subscribe":
            bus.subscribe(arguments.agent, arguments.topic)
            _print_json({"subscribed": True})
        elif arguments.command == "unsubscribe":
            bus.unsubscribe(arguments.agent, arguments.topic)
            _print_json({"unsubscribed": True})
        elif arguments.command == "send-direct":
            message_id = bus.send_direct(
                arguments.from_agent,
                arguments.to_agent,
                _read_body(),
                ttl_seconds=arguments.ttl_seconds,
            )
            _print_json({"message_id": message_id, "fanout": 1})
        elif arguments.command == "publish":
            message_id, fanout = bus.publish(
                arguments.from_agent,
                arguments.topic,
                _read_body(),
                ttl_seconds=arguments.ttl_seconds,
            )
            _print_json({"message_id": message_id, "fanout": fanout})
        elif arguments.command == "receive":
            if arguments.take:
                deliveries = bus.take(arguments.agent, limit=arguments.limit)
            else:
                deliveries = bus.claim(
                    arguments.agent,
                    limit=arguments.limit,
                    visibility_seconds=arguments.visibility_seconds,
                )
            _print_json({"messages": [asdict(item) for item in deliveries]})
        elif arguments.command == "ack":
            count = bus.ack(
                arguments.agent,
                ((arguments.message_id, arguments.claim_token),),
            )
            _print_json({"acked": count})
        else:
            parser.error("unknown command")
        return 0
    except (BusError, sqlite3.Error, OSError) as error:
        print(f"agent bus error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
