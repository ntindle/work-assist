import { describe, expect, it } from "vitest";

import {
  notificationInputSchema,
  resolveIdempotencyKey,
  sendHarkNotification,
  validateWebhookUrl,
} from "../src/hark";
import { handleNotifyRequest } from "../src/index";

const WEBHOOK = "https://hark.ryan.ceo/hooks/whk_test-token";

describe("notificationInputSchema", () => {
  it("accepts the minimal payload and rejects extra fields", () => {
    expect(notificationInputSchema.parse({ body: "Deploy finished ✅" })).toEqual({
      body: "Deploy finished ✅",
    });
    expect(() =>
      notificationInputSchema.parse({ body: "hello", extra: true }),
    ).toThrow();
  });

  it("enforces lengths, HTTPS images, and unique device IDs", () => {
    expect(() => notificationInputSchema.parse({ body: "" })).toThrow();
    expect(() =>
      notificationInputSchema.parse({
        body: "hello",
        imageUrl: "http://example.com/avatar.png",
      }),
    ).toThrow();
    expect(() =>
      notificationInputSchema.parse({
        body: "hello",
        deviceIds: ["phone", "phone"],
      }),
    ).toThrow();
  });
});

describe("validateWebhookUrl", () => {
  it("accepts only a Hark hook URL without query or fragment", () => {
    expect(validateWebhookUrl(WEBHOOK).href).toBe(WEBHOOK);
    expect(() =>
      validateWebhookUrl("https://example.com/hooks/whk_test-token"),
    ).toThrow();
    expect(() => validateWebhookUrl(`${WEBHOOK}?leak=true`)).toThrow();
  });
});

describe("sendHarkNotification", () => {
  it("posts the exact payload with JSON and an idempotency key", async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetcher: typeof fetch = async (target, init) => {
      calls.push([target, init]);
      return new Response(null, { status: 202 });
    };
    const payload = {
      body: "Deploy finished ✅",
      title: "Work Assist",
      imageUrl: "https://example.com/avatar.png",
      url: "https://example.com/build/42",
      deviceIds: ["phone", "tablet"],
    };

    await expect(
      sendHarkNotification(WEBHOOK, payload, { fetcher }),
    ).resolves.toEqual({
      delivered: true,
      status: 202,
    });

    expect(calls).toHaveLength(1);
    const [target, init] = calls[0];
    expect(String(target)).toBe(WEBHOOK);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
    });
    expect(
      (init?.headers as Record<string, string>)["Idempotency-Key"],
    ).toMatch(/^[0-9a-f-]{36}$/);
    expect(JSON.parse(String(init?.body))).toEqual(payload);
  });

  it("does not leak the webhook when Hark rejects a request", async () => {
    const fetcher: typeof fetch = async () =>
      new Response("no", { status: 429 });
    const error = await sendHarkNotification(
      WEBHOOK,
      { body: "hello" },
      { fetcher },
    ).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe(
      "Hark rejected the notification (HTTP 429)",
    );
    expect((error as Error).message).not.toContain(WEBHOOK);
  });
});

describe("resolveIdempotencyKey", () => {
  it("preserves a caller key and rejects unsafe header values", () => {
    expect(resolveIdempotencyKey("deploy-42")).toBe("deploy-42");
    expect(() => resolveIdempotencyKey("bad\nkey")).toThrow();
    expect(resolveIdempotencyKey()).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("handleNotifyRequest", () => {
  it("forwards a valid service-binding request without a credential", async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetcher: typeof fetch = async (target, init) => {
      calls.push([target, init]);
      return new Response(null, { status: 200 });
    };
    const request = new Request("https://hark.internal/notify", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "deploy-42",
      },
      body: JSON.stringify({
        body: "Deploy finished ✅",
        url: "https://example.com/build/42",
      }),
    });

    const response = await handleNotifyRequest(
      request,
      { HARK_WEBHOOK_URL: WEBHOOK },
      fetcher,
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      delivered: true,
      status: 200,
    });
    expect(calls).toHaveLength(1);
    expect(
      (calls[0][1]?.headers as Record<string, string>)["Idempotency-Key"],
    ).toBe("deploy-42");
  });

  it("rejects an invalid payload before calling Hark", async () => {
    let called = false;
    const fetcher: typeof fetch = async () => {
      called = true;
      return new Response(null, { status: 200 });
    };
    const request = new Request("https://hark.internal/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: "", unexpected: true }),
    });

    const response = await handleNotifyRequest(
      request,
      { HARK_WEBHOOK_URL: WEBHOOK },
      fetcher,
    );

    expect(response.status).toBe(400);
    expect(called).toBe(false);
  });
});
