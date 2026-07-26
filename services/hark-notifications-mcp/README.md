# Hark Notifications MCP

This Cloudflare Worker exposes:

- one authenticated MCP action, `send_notification`, at `/mcp`;
- one JSON notification relay at `/notify` for Cloudflare service bindings.

Both routes validate the Hark payload and forward it to the webhook stored in
the `HARK_WEBHOOK_URL` Worker secret. The relay preserves a caller-supplied
`Idempotency-Key` header or generates a unique key when it is omitted.

The public source never contains a webhook URL. The production custom domain is
protected by Cloudflare Access with managed OAuth, and `workers.dev` access is
disabled.

## No-secret calls from Cloudflare services

Add a service binding to each consuming Worker or Pages Function:

```jsonc
{
  "services": [
    {
      "binding": "HARK_NOTIFICATIONS",
      "service": "hark-notifications-mcp"
    }
  ]
}
```

Then call the internal route:

```ts
const response = await env.HARK_NOTIFICATIONS.fetch(
  "https://hark.internal/notify",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": "deploy-42"
    },
    body: JSON.stringify({
      body: "Deploy finished ✅",
      url: "https://example.com/build/42"
    })
  }
);
```

The consumer stores no webhook URL and no authentication secret. Cloudflare
service bindings call the Worker directly; public traffic to the custom domain
still passes through Access.

## Develop

```bash
npm install
npm test
npm run check
npm run build
```

## Deploy

Set the secret without writing it to a file:

```bash
printf '%s' "$HARK_WEBHOOK_URL" | npx wrangler secret put HARK_WEBHOOK_URL
npm run deploy
```

Then protect `hark-mcp.oeis.ntindle.com` with a self-hosted Cloudflare Access
application, allow only the intended identity, and enable managed OAuth.
