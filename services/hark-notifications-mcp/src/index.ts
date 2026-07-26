import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { z } from "zod";

import {
  notificationInputSchema,
  notificationInputShape,
  sendHarkNotification,
} from "./hark";

interface Env {
  HARK_WEBHOOK_URL: string;
}

function createServer(env: Env): McpServer {
  const server = new McpServer(
    {
      name: "hark-notifications",
      version: "0.1.0",
    },
    {
      instructions:
        "Send a Hark notification only when the user explicitly asks or an automation explicitly directs it. Every call creates an external side effect. Use body for the message. Omit deviceIds to notify every active device; never invent device IDs.",
    },
  );

  server.registerTool(
    "send_notification",
    {
      title: "Send Hark notification",
      description:
        "Send a push notification through Hark. Use body for the message. title, imageUrl, and url override service defaults. Omit deviceIds to notify all active devices.",
      inputSchema: notificationInputShape,
      outputSchema: {
        delivered: z.boolean(),
        status: z.number().int().min(100).max(599),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async (input) => {
      const delivery = await sendHarkNotification(env.HARK_WEBHOOK_URL, input);
      return {
        structuredContent: delivery,
        content: [
          {
            type: "text",
            text: "Notification sent through Hark.",
          },
        ],
      };
    },
  );

  return server;
}

type Fetcher = typeof fetch;

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}

export async function handleNotifyRequest(
  request: Request,
  env: Env,
  fetcher: Fetcher = fetch,
): Promise<Response> {
  if (request.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  const contentType = request.headers.get("Content-Type") ?? "";
  if (contentType.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    return jsonResponse({ error: "Content-Type must be application/json" }, 415);
  }

  let candidate: unknown;
  try {
    candidate = await request.json();
  } catch {
    return jsonResponse({ error: "Request body must be valid JSON" }, 400);
  }

  const parsed = notificationInputSchema.safeParse(candidate);
  if (!parsed.success) {
    return jsonResponse(
      {
        error: "Invalid notification payload",
        issues: parsed.error.issues.map((issue) => ({
          path: issue.path.join("."),
          message: issue.message,
        })),
      },
      400,
    );
  }

  try {
    const delivery = await sendHarkNotification(
      env.HARK_WEBHOOK_URL,
      parsed.data,
      {
        fetcher,
        idempotencyKey:
          request.headers.get("Idempotency-Key") ?? undefined,
      },
    );
    return jsonResponse(delivery, 200);
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    if (message.startsWith("Idempotency-Key")) {
      return jsonResponse({ error: message }, 400);
    }
    if (message.startsWith("HARK_WEBHOOK_URL")) {
      return jsonResponse({ error: "Notification relay is not configured" }, 500);
    }
    return jsonResponse({ error: "Notification delivery failed" }, 502);
  }
}

export default {
  async fetch(
    request: Request,
    env: Env,
  ): Promise<Response> {
    const url = new URL(request.url);
    if (url.search) {
      return new Response("Not found", { status: 404 });
    }

    if (url.pathname === "/notify") {
      return handleNotifyRequest(request, env);
    }

    if (url.pathname !== "/mcp") {
      return new Response("Not found", { status: 404 });
    }

    const server = createServer(env);
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    await server.connect(transport);
    return transport.handleRequest(request);
  },
} satisfies ExportedHandler<Env>;
