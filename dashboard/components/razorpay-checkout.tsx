"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  DemoApiError,
  getRecoveryBootstrap,
  sendCheckoutEvent,
} from "@/lib/demo-api";
import type {
  CheckoutEvent,
  CheckoutEventType,
  DemoSession,
  RazorpayFailure,
  RazorpayOptions,
  RecoveryBootstrap,
} from "@/lib/demo-types";

const SDK_URL = "https://checkout.razorpay.com/v1/checkout.js";
const SDK_ID = "razorpay-checkout-sdk";
const SESSION_STORAGE_KEY = "leakproof:active-demo-session";
const TELEMETRY_PREFIX = "leakproof:checkout-events:";
let sdkPromise: Promise<void> | undefined;

type CheckoutState = "idle" | "preparing" | "open" | "failed" | "completed";
type PublicCheckout = Pick<
  DemoSession,
  | "session_id"
  | "razorpay_key_id"
  | "razorpay_order_id"
  | "amount_paise"
  | "currency"
  | "expires_at"
>;

function eventId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `evt-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function money(amountPaise: number, currency: string) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(amountPaise / 100);
}

function loadCheckoutSdk(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(SDK_ID) as HTMLScriptElement | null;
    const script = existing ?? document.createElement("script");
    const loaded = () => (window.Razorpay ? resolve() : reject(new Error("Checkout SDK unavailable")));
    const failed = () => {
      sdkPromise = undefined;
      reject(new Error("Could not load Razorpay Checkout"));
    };
    script.addEventListener("load", loaded, { once: true });
    script.addEventListener("error", failed, { once: true });
    if (!existing) {
      script.id = SDK_ID;
      script.src = SDK_URL;
      script.async = true;
      document.head.appendChild(script);
    }
  });
  return sdkPromise;
}

function telemetryKey(sessionId: string) {
  return `${TELEMETRY_PREFIX}${sessionId}`;
}

function readQueued(sessionId: string): CheckoutEvent[] {
  try {
    return JSON.parse(localStorage.getItem(telemetryKey(sessionId)) ?? "[]") as CheckoutEvent[];
  } catch {
    return [];
  }
}

function persistQueued(sessionId: string, events: CheckoutEvent[]) {
  if (events.length) localStorage.setItem(telemetryKey(sessionId), JSON.stringify(events));
  else localStorage.removeItem(telemetryKey(sessionId));
}

async function deliverTelemetry(session: DemoSession, event: CheckoutEvent) {
  // Persist first: a retry after navigation reuses the same client_event_id.
  const queued = readQueued(session.session_id);
  if (!queued.some((item) => item.client_event_id === event.client_event_id)) {
    persistQueued(session.session_id, [...queued, event]);
  }
  try {
    await sendCheckoutEvent(session, event);
    persistQueued(
      session.session_id,
      readQueued(session.session_id).filter(
        (item) => item.client_event_id !== event.client_event_id,
      ),
    );
    return true;
  } catch {
    // The server endpoint is idempotent. The exact persisted event is retried on resume.
    return false;
  }
}

async function flushTelemetry(session: DemoSession) {
  for (const event of readQueued(session.session_id)) {
    await deliverTelemetry(session, event);
  }
}

function newTelemetry(
  eventType: CheckoutEventType,
  metadata: CheckoutEvent["metadata"] = {},
): CheckoutEvent {
  return {
    client_event_id: eventId(),
    event_type: eventType,
    occurred_at: new Date().toISOString(),
    metadata,
  };
}

function errorMessage(error: unknown) {
  if (error instanceof DemoApiError || error instanceof Error) return error.message;
  return "Checkout could not be prepared. Please try again.";
}

function failureMessage(response: RazorpayFailure) {
  return (
    response.error?.description ??
    response.error?.reason?.replaceAll("_", " ") ??
    "The payment was not completed. No charge was made."
  );
}

function openCheckout({
  checkout,
  session,
  recovery,
  setState,
  setMessage,
}: {
  checkout: PublicCheckout;
  session?: DemoSession;
  recovery: boolean;
  setState: (state: CheckoutState) => void;
  setMessage: (message: string) => void;
}) {
  if (!window.Razorpay) throw new Error("Razorpay Checkout is not available.");
  let completed = false;
  let attemptId = eventId();
  const track = (event: CheckoutEvent) => {
    if (session) void deliverTelemetry(session, event);
  };
  const options: RazorpayOptions = {
    key: checkout.razorpay_key_id,
    order_id: checkout.razorpay_order_id,
    amount: checkout.amount_paise,
    currency: checkout.currency,
    name: "Leakproof test store",
    description: recovery ? "Recover your original test order" : "Razorpay test payment",
    notes: { leakproof_session_id: checkout.session_id },
    retry: { enabled: true },
    theme: { color: "#3157d5", backdrop_color: "#14213d" },
    handler: (response) => {
      if (response.razorpay_order_id !== checkout.razorpay_order_id) {
        setState("failed");
        setMessage("Checkout returned a different order. Nothing was accepted.");
        return;
      }
      completed = true;
      setState("completed");
      setMessage(
        "Payment submitted. Razorpay’s signed webhook will verify it before Leakproof marks the order recovered.",
      );
      const completion = newTelemetry("checkout_completed", { attempt_id: attemptId });
      if (session) {
        void deliverTelemetry(session, completion).then((delivered) => {
          if (delivered) sessionStorage.removeItem(SESSION_STORAGE_KEY);
        });
      }
    },
    modal: {
      confirm_close: true,
      escape: true,
      ondismiss: () => {
        if (completed) return;
        setState("idle");
        setMessage("Checkout closed. We’ll re-check the original order before opening a recovery case.");
        track(newTelemetry("checkout_dismissed", { dismissed_by: "customer" }));
      },
    },
  };
  const instance = new window.Razorpay(options);
  instance.on("payment.submit", () => {
    attemptId = eventId();
    setMessage("Payment attempt started. Razorpay’s webhook remains the source of truth.");
    track(newTelemetry("payment_attempt_started", { attempt_id: attemptId }));
  });
  instance.on("payment.failed", (response) => {
    setState("failed");
    setMessage(failureMessage(response));
  });
  instance.open();
  setState("open");
  setMessage(recovery ? "Recovery Checkout opened for the original order." : "Secure Checkout opened.");
  track(newTelemetry("checkout_opened", { sdk_version: "v1" }));
}

function CheckoutStatus({ state, message }: { state: CheckoutState; message: string }) {
  return (
    <div className={`checkout-status checkout-status-${state}`} role="status" aria-live="polite">
      <span className="checkout-status-dot" />
      <div>
        <strong>{state === "completed" ? "Payment submitted" : state === "failed" ? "Needs attention" : state === "open" ? "Checkout open" : state === "preparing" ? "Preparing order" : "Ready when you are"}</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}

function OrderReceipt({ checkout, recovered }: { checkout: PublicCheckout; recovered?: boolean }) {
  return (
    <div className="checkout-receipt">
      <div><span>Amount</span><strong>{money(checkout.amount_paise, checkout.currency)}</strong></div>
      <div><span>Order</span><strong title={checkout.razorpay_order_id}>…{checkout.razorpay_order_id.slice(-10)}</strong></div>
      <div><span>Mode</span><strong>{recovered ? "Original order" : "Razorpay test"}</strong></div>
    </div>
  );
}

export function DemoCheckout() {
  const [recipient, setRecipient] = useState("");
  const [session, setSession] = useState<DemoSession>();
  const [state, setState] = useState<CheckoutState>("idle");
  const [message, setMessage] = useState("A fixed ₹500 test order will be created on the server.");
  const preparing = useRef(false);

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (!stored) return;
      const active = JSON.parse(stored) as DemoSession;
      if (new Date(active.expires_at).getTime() > Date.now()) {
        setSession(active);
        setMessage("Your unexpired test order is ready to resume.");
        void flushTelemetry(active);
      } else {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
        persistQueued(active.session_id, []);
      }
    } catch {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    }
  }, []);

  const start = useCallback(async () => {
    if (preparing.current) return;
    preparing.current = true;
    setState("preparing");
    setMessage(session ? "Resuming your existing order…" : "Creating one fixed test order…");
    try {
      const active =
        session && new Date(session.expires_at).getTime() > Date.now()
          ? session
          : await createSession(recipient.trim() || undefined);
      if (!session || active.session_id !== session.session_id) {
        sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(active));
        setSession(active);
      }
      await Promise.all([loadCheckoutSdk(), flushTelemetry(active)]);
      openCheckout({ checkout: active, session: active, recovery: false, setState, setMessage });
    } catch (error) {
      setState("failed");
      setMessage(errorMessage(error));
    } finally {
      preparing.current = false;
    }
  }, [recipient, session]);

  return (
    <div className="checkout-card">
      <div className="checkout-card-copy">
        <span className="checkout-step">01 · Test the leak</span>
        <h2>Open a real Razorpay test Checkout.</h2>
        <p>Dismiss it, trigger a test failure, or complete it. Leakproof records bounded browser signals while Razorpay webhooks decide payment truth.</p>
      </div>
      <label className="checkout-field">
        <span>Recovery email <small>optional</small></span>
        <input
          type="email"
          value={recipient}
          onChange={(event) => setRecipient(event.target.value)}
          placeholder="reviewer@example.com"
          autoComplete="email"
          disabled={Boolean(session)}
        />
        <small>Only allowlisted addresses receive mail. Others stay preview-only.</small>
      </label>
      {session && <OrderReceipt checkout={session} />}
      <button className="checkout-primary" type="button" onClick={start} disabled={state === "preparing" || state === "open"}>
        {state === "preparing" ? "Preparing secure Checkout…" : session ? "Resume Checkout" : "Create test order & open Checkout"}
        <span aria-hidden="true">→</span>
      </button>
      <CheckoutStatus state={state} message={message} />
      <p className="checkout-fineprint">Test mode only · Amount and currency are fixed server-side · No automatic charge</p>
    </div>
  );
}

export function RecoveryCheckout({ token }: { token: string }) {
  const [bootstrap, setBootstrap] = useState<RecoveryBootstrap>();
  const [state, setState] = useState<CheckoutState>("preparing");
  const [message, setMessage] = useState("Verifying the signed recovery link and checking Razorpay payment state…");
  const loading = useRef(false);

  const prepare = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    setState("preparing");
    try {
      const [recovery] = await Promise.all([getRecoveryBootstrap(token), loadCheckoutSdk()]);
      setBootstrap(recovery);
      setState("idle");
      setMessage("Verified and unpaid. Continue with the exact order that was originally created.");
    } catch (error) {
      setState("failed");
      setMessage(errorMessage(error));
    } finally {
      loading.current = false;
    }
  }, [token]);

  useEffect(() => {
    void prepare();
  }, [prepare]);

  const reopen = () => {
    if (!bootstrap) return;
    try {
      openCheckout({ checkout: bootstrap, recovery: true, setState, setMessage });
    } catch (error) {
      setState("failed");
      setMessage(errorMessage(error));
    }
  };

  return (
    <div className="checkout-card recovery-card">
      <div className="checkout-card-copy">
        <span className="checkout-step">Signed recovery · Original order</span>
        <h2>Pick up exactly where you left off.</h2>
        <p>This link is bound to one session, merchant, order, amount, and currency. We check Razorpay again before Checkout can reopen.</p>
      </div>
      {bootstrap && <OrderReceipt checkout={bootstrap} recovered />}
      <button className="checkout-primary" type="button" onClick={reopen} disabled={!bootstrap || state === "preparing" || state === "open"}>
        {state === "preparing" ? "Verifying original order…" : "Continue original order"}
        <span aria-hidden="true">→</span>
      </button>
      {state === "failed" && !bootstrap && (
        <button className="checkout-secondary" type="button" onClick={prepare}>Check the link again</button>
      )}
      <CheckoutStatus state={state} message={message} />
      <p className="checkout-fineprint">The recovery route cannot change the amount or substitute another order.</p>
    </div>
  );
}
