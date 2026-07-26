# Work Assist

Work Assist is a Codex and ChatGPT Work plugin for coordinating isolated agent
sandboxes. It contributes lifecycle hooks, a reusable skill, and a small local
message bus for agent-to-agent coordination.

This repository contains the generic plugin distribution. It does not contain
Cloudflare deployment configuration, credentials, browser profiles, chat
transcripts, or project-specific application code.

## Hark notifications

The marketplace also includes **Hark Notifications**, a separate plugin that
adds one authenticated `send_notification` MCP action and a no-secret internal
relay for other Cloudflare services. It accepts:

- `body` (required);
- optional `title`, `imageUrl`, and destination `url`;
- optional `deviceIds` for Hark Pro routing (omit it to notify every active
  registered device).

The MCP service runs on Cloudflare Workers behind Cloudflare Access managed
OAuth. Its Hark webhook is stored only as the `HARK_WEBHOOK_URL` Worker secret;
the endpoint is not committed to this public repository. See
[`services/hark-notifications-mcp`](services/hark-notifications-mcp) for the
implementation, deployment checks, and service-binding example.

## What it provides

- Opaque identities for top-level chats, activations, roots, and subagents.
- Direct messages between live agents.
- Bounded topic subscriptions and fan-out.
- Lifecycle delivery on session, prompt, and subagent events.
- A low-overhead `PostToolUse` inbox probe that performs one indexed,
  read-only SQLite query and emits a short notice only when mail is waiting.
- Collision-resistant identifiers for independent Work Assist terminal and
  desktop surfaces.

The shared message bus lives at
`/workspace/.work-assist/v1/agent-bus.sqlite3`. It carries only small,
non-secret coordination messages. It does not read chat transcripts or grant
an agent additional authority.

## Install

Add this repository as a Codex plugin marketplace:

```bash
codex plugin marketplace add ntindle/work-assist
```

Restart Codex Desktop, open **Plugins**, select the **Work Assist** marketplace,
and install **Work Assist**. Review and trust the bundled hooks, then start a
new chat so the plugin loads.

The plugin can also be inspected without installing it:

```text
.agents/plugins/marketplace.json
plugins/work-assist/.codex-plugin/plugin.json
plugins/work-assist/hooks/hooks.json
plugins/work-assist/skills/work-assist/SKILL.md
```

## Agent bus commands

The hook context supplies each agent's opaque address. Agents can use:

```bash
python3 plugins/work-assist/scripts/agent_bus.py agents
python3 plugins/work-assist/scripts/agent_bus.py subscribe <agent-id> <topic>
printf '%s\n' 'non-secret coordination note' |
  python3 plugins/work-assist/scripts/agent_bus.py send-direct \
    <sender-agent-id> <recipient-agent-id>
printf '%s\n' 'non-secret topic update' |
  python3 plugins/work-assist/scripts/agent_bus.py publish \
    <sender-agent-id> <topic>
```

Run `python3 plugins/work-assist/scripts/agent_bus.py --help` for the complete
command list.

## Runtime boundaries

- Different top-level chats may share `/workspace`, but they do not gain access
  to one another's transcripts, localhost services, PTYs, VNC servers, or
  browser profiles.
- Hooks cannot wake a dormant chat.
- Hosted tools may not emit `PostToolUse`.
- Codex currently documents `PostToolUse` with the parent session identifier
  but not the individual subagent identifier. The probe therefore checks the
  root inbox unless the runtime supplies an `agent_id`; subagent mail remains
  queued for its next lifecycle event.
- Same-thread collaboration tools remain the immediate path between a root and
  its active subagents.

## Security

Never send credentials, cookies, one-time codes, API keys, private keys,
Cloudflare Access assertions, personal data, or transcript contents through
the agent bus. The implementation bounds message size, fan-out, batch size,
and lifetime and rejects common credential shapes.

Plugin hooks are advisory. Review their source before trusting them:

- `SessionStart`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, and
  `Stop` maintain leases and deliver bounded inbox batches.
- `PostToolUse` performs only a read-only pending-message probe. It does not
  read message bodies, claim messages, acknowledge delivery, refresh leases,
  or create the database.

## Test

The plugin uses only the Python standard library:

```bash
python3 -m unittest \
  plugins/work-assist/test_context_hook.py \
  plugins/work-assist/test_agent_bus.py
```
