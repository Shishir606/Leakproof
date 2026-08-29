import type {
  ApiErrorPayload,
  CheckoutEvent,
  DemoSession,
  DemoSessionProjection,
  RecoveryBootstrap,
} from "./demo-types";

export class DemoApiError extends Error {
  constructor(
    message: string,
    readonly code = "request_failed",
    readonly retryable = false,
  ) {
    super(message);
  }
}

async function readResponse<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => null)) as ApiErrorPayload | T | null;
  if (!response.ok) {
    const detail = (payload as ApiErrorPayload | null)?.error;
    throw new DemoApiError(
      detail?.message ?? `Request failed (${response.status}).`,
      detail?.code,
      detail?.retryable,
    );
  }
  return payload as T;
}

export async function createSession(recipient?: string): Promise<DemoSession> {
  const response = await fetch("/api/demo/sessions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(recipient ? { recipient } : {}),
  });
  return readResponse<DemoSession>(response);
}

export async function sendCheckoutEvent(
  session: Pick<DemoSession, "session_id" | "session_token">,
  event: CheckoutEvent,
): Promise<void> {
  const response = await fetch(
    `/api/demo/sessions/${encodeURIComponent(session.session_id)}/checkout-events`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-leakproof-session-token": session.session_token,
      },
      body: JSON.stringify(event),
      keepalive:
        event.event_type === "checkout_dismissed" ||
        event.event_type === "checkout_completed",
    },
  );
  await readResponse(response);
}

export async function getSessionProjection(
  session: Pick<DemoSession, "session_id" | "session_token">,
): Promise<DemoSessionProjection> {
  const response = await fetch(
    `/api/demo/sessions/${encodeURIComponent(session.session_id)}`,
    {
      cache: "no-store",
      headers: { "x-leakproof-session-token": session.session_token },
    },
  );
  return readResponse<DemoSessionProjection>(response);
}

export async function getRecoveryBootstrap(token: string): Promise<RecoveryBootstrap> {
  const response = await fetch(`/api/recover/${encodeURIComponent(token)}`, {
    cache: "no-store",
  });
  return readResponse<RecoveryBootstrap>(response);
}
