import { z } from "zod";

const HARK_ORIGIN = "https://hark.ryan.ceo";
const HARK_PATH_PREFIX = "/hooks/whk_";
const REQUEST_TIMEOUT_MS = 10_000;

export const notificationInputShape = {
  body: z
    .string()
    .min(1)
    .max(2_000)
    .describe("Notification message body."),
  title: z
    .string()
    .min(1)
    .max(80)
    .optional()
    .describe("Optional sender title. Overrides the service default."),
  imageUrl: z
    .url()
    .max(2_048)
    .refine((value) => value.startsWith("https://"), {
      message: "imageUrl must use HTTPS",
    })
    .optional()
    .describe("Optional HTTPS avatar URL. Overrides the service default."),
  url: z
    .url()
    .max(2_048)
    .optional()
    .describe("Optional destination opened when the notification is tapped."),
  deviceIds: z
    .array(z.string())
    .min(1)
    .max(50)
    .refine((items) => new Set(items).size === items.length, {
      message: "deviceIds must contain unique values",
    })
    .optional()
    .describe(
      "Optional Hark Pro routing targets. Omit to notify every active registered device.",
    ),
};

export const notificationInputSchema = z
  .object(notificationInputShape)
  .strict();

export type HarkNotification = z.infer<typeof notificationInputSchema>;

export interface HarkDelivery {
  [key: string]: unknown;
  delivered: true;
  status: number;
}

type Fetcher = typeof fetch;

interface DeliveryOptions {
  fetcher?: Fetcher;
  idempotencyKey?: string;
}

export function resolveIdempotencyKey(value?: string): string {
  if (value === undefined) {
    return crypto.randomUUID();
  }
  if (value.length < 1 || value.length > 255 || !/^[\x21-\x7e]+$/.test(value)) {
    throw new Error(
      "Idempotency-Key must contain 1 to 255 visible ASCII characters",
    );
  }
  return value;
}

export function validateWebhookUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("HARK_WEBHOOK_URL is not a valid URL");
  }

  if (
    url.origin !== HARK_ORIGIN ||
    !url.pathname.startsWith(HARK_PATH_PREFIX) ||
    url.pathname.length <= HARK_PATH_PREFIX.length ||
    url.search ||
    url.hash
  ) {
    throw new Error("HARK_WEBHOOK_URL must be a Hark HTTPS webhook URL");
  }
  return url;
}

export async function sendHarkNotification(
  webhookUrl: string,
  input: HarkNotification,
  options: DeliveryOptions = {},
): Promise<HarkDelivery> {
  const target = validateWebhookUrl(webhookUrl);
  const payload = notificationInputSchema.parse(input);
  const idempotencyKey = resolveIdempotencyKey(options.idempotencyKey);
  const fetcher = options.fetcher ?? fetch;
  const response = await fetcher(target, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });

  if (!response.ok) {
    throw new Error(`Hark rejected the notification (HTTP ${response.status})`);
  }

  return {
    delivered: true,
    status: response.status,
  };
}
