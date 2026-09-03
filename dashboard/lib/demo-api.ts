import type { LeakType, ScenarioCapability } from "./resource-types";
import type {
  ApiErrorPayload,
  CheckoutEvent,
  CheckoutPaymentVerificationReceipt,
  DemoSession,
  DemoSessionProjection,
  RazorpaySuccess,
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

export async function createSession(recipient?: string, scenario_type: LeakType = "PAYMENT_FAILURE"): Promise<DemoSession> {
  const response = await fetch("/api/demo/sessions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...(recipient ? { recipient } : {}), scenario_type }),
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

export async function verifyCheckoutPayment(
  sessionId: string,
  proof: RazorpaySuccess,
  authorization: { sessionToken?: string; recoveryToken?: string },
): Promise<CheckoutPaymentVerificationReceipt> {
  const headers = new Headers({ "content-type": "application/json" });
  if (authorization.sessionToken) {
    headers.set("x-leakproof-session-token", authorization.sessionToken);
  }
  if (authorization.recoveryToken) {
    headers.set("x-leakproof-recovery-token", authorization.recoveryToken);
  }
  const response = await fetch(
    `/api/demo/sessions/${encodeURIComponent(sessionId)}/payments/verify`,
    {
      method: "POST",
      headers,
      body: JSON.stringify(proof),
    },
  );
  return readResponse<CheckoutPaymentVerificationReceipt>(response);
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


export async function getScenarios(): Promise<ScenarioCapability[]> {
  return readResponse<ScenarioCapability[]>(await fetch("/api/demo/scenarios", { cache: "no-store" }));
}

export async function downloadAcceptance(session: DemoSession): Promise<boolean> {
  const response = await fetch(`/api/demo/sessions/${encodeURIComponent(session.session_id)}/acceptance.json`, {
    cache: "no-store",
    headers: { "x-leakproof-session-token": session.session_token },
  });
  const artifact = await readResponse<{ passed: boolean }>(response);
  const url = URL.createObjectURL(new Blob([JSON.stringify(artifact, null, 2) + "\n"], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `checkout-acceptance-${new Date().toISOString().replaceAll(":", "-")}.json`;
  link.click();
  URL.revokeObjectURL(url);
  return artifact.passed;
}
