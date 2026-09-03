"use client";

import Link from "next/link";
import { useSessionProjection } from "@/lib/use-session-projection";
import { AbandonmentStatus } from "@/components/abandonment-status";
import { deliverTelemetry, flushTelemetry } from "@/lib/checkout-telemetry";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  DemoApiError,
  getRecoveryBootstrap,
  verifyCheckoutPayment,
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
let sdkPromise: Promise<void> | undefined;

type CheckoutState = "idle" | "preparing" | "open" | "failed" | "completed";
type CheckoutOutcome = "failed" | "completed";
type VerificationAuthorization = { sessionToken?: string; recoveryToken?: string };
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

async function verifyCapturedPayment(
  sessionId: string,
  response: Parameters<typeof verifyCheckoutPayment>[1],
  authorization: VerificationAuthorization,
) {
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      return await verifyCheckoutPayment(sessionId, response, authorization);
    } catch (error) {
      const waitingForCapture =
        error instanceof DemoApiError &&
        error.code === "payment_not_captured" &&
        error.retryable;
      if (!waitingForCapture || attempt === 5) throw error;
      await new Promise((resolve) => window.setTimeout(resolve, attempt * 500));
    }
  }
  throw new Error("Payment verification did not complete.");
}

function openCheckout({
  checkout,
  session,
  verificationAuthorization,
  recovery,
  setState,
  setMessage,
  onOutcome,
}: {
  checkout: PublicCheckout;
  session?: DemoSession;
  verificationAuthorization: VerificationAuthorization;
  recovery: boolean;
  setState: (state: CheckoutState) => void;
  setMessage: (message: string) => void;
  onOutcome: (outcome: CheckoutOutcome) => void;
}) {
  if (!window.Razorpay) throw new Error("Razorpay Checkout is not available.");
  let completed = false;
  let dismissed = false;
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
    handler: async (response) => {
      if (response.razorpay_order_id !== checkout.razorpay_order_id) {
        setState("failed");
        setMessage("Checkout returned a different order. Nothing was accepted.");
        onOutcome("failed");
        return;
      }
      completed = true;
      setState("preparing");
      setMessage("Payment submitted. Verifying Razorpay’s signature and captured status…");
      try {
        await verifyCapturedPayment(
          checkout.session_id,
          response,
          verificationAuthorization,
        );
        setState("completed");
        setMessage(
          "Payment verified server-side against the original Razorpay test order and captured status.",
        );
        const completion = newTelemetry("checkout_completed", { attempt_id: attemptId });
        if (session) void deliverTelemetry(session, completion);
        onOutcome("completed");
      } catch (error) {
        setState("failed");
        setMessage(
          error instanceof DemoApiError
            ? error.message
            : "Payment was submitted, but server verification did not complete. Nothing was marked recovered.",
        );
      }
    },
    modal: {
      confirm_close: true,
      escape: true,
      ondismiss: () => {
        if (completed || dismissed) return;
        dismissed = true;
        setState("idle");
        setMessage("Checkout closed. We’ll re-check the original order before opening a recovery case.");
        track(newTelemetry("checkout_dismissed", { dismissed_by: "customer", attempt_id: attemptId }));
      },
    },
  };
  const instance = new window.Razorpay(options);
  instance.on("payment.submit", () => {
    attemptId = eventId();
    dismissed = false;
    setMessage("Payment attempt started. Only server-verified Razorpay truth can close the case.");
    track(newTelemetry("payment_attempt_started", { attempt_id: attemptId }));
  });
  instance.on("payment.failed", (response) => {
    setState("failed");
    setMessage(failureMessage(response));
    if (!dismissed) {
      dismissed = true;
      track(newTelemetry("checkout_dismissed", { dismissed_by: "browser", attempt_id: attemptId }));
    }
    onOutcome("failed");
  });
  track(newTelemetry("checkout_opened", { sdk_version: "v1", attempt_id: attemptId }));
  instance.open();
  setState("open");
  setMessage(recovery ? "Recovery Checkout opened for the original order." : "Secure Checkout opened.");
}

function CheckoutStatus({ state, message }: { state: CheckoutState; message: string }) {
  return (
    <div className={`checkout-status checkout-status-${state}`} role="status" aria-live="polite">
      <span className="checkout-status-dot" />
      <div>
        <strong>{state === "completed" ? "Payment verified" : state === "failed" ? "Needs attention" : state === "open" ? "Checkout open" : state === "preparing" ? "Verifying" : "Ready when you are"}</strong>
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
  const router = useRouter();
  const [recipient, setRecipient] = useState("");
  const [scenario, setScenario] = useState<"CHECKOUT_ABANDON" | "PAYMENT_FAILURE">("CHECKOUT_ABANDON");
  const [session, setSession] = useState<DemoSession>();
  const [state, setState] = useState<CheckoutState>("idle");
  const [message, setMessage] = useState("A fixed ₹500 test order will be created on the server.");
  const preparing = useRef(false);
  const { projection, error: projectionError, expired } = useSessionProjection(session);

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (!stored) return;
      const active = JSON.parse(stored) as DemoSession;
      if (new Date(active.expires_at).getTime() > Date.now()) {
        setSession(active);
        setScenario(active.scenario_type === "CHECKOUT_ABANDON" ? "CHECKOUT_ABANDON" : "PAYMENT_FAILURE");
        setMessage("Your unexpired test order is ready to resume.");
        void flushTelemetry(active);
      } else {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
        localStorage.removeItem(`leakproof:checkout-events:${active.session_id}`);
        setMessage("Your previous session expired. Create a new test order to rehearse again.");
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
        session && !expired && new Date(session.expires_at).getTime() > Date.now()
          ? session
          : await createSession(recipient.trim() || undefined, scenario);
      if (!session || active.session_id !== session.session_id) {
        sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(active));
        setSession(active);
      }
      await Promise.all([loadCheckoutSdk(), flushTelemetry(active)]);
      openCheckout({
        checkout: active,
        session: active,
        verificationAuthorization: { sessionToken: active.session_token },
        recovery: false,
        setState,
        setMessage,
        onOutcome: () => router.replace("/"),
      });
    } catch (error) {
      setState("failed");
      setMessage(errorMessage(error));
    } finally {
      preparing.current = false;
    }
  }, [recipient, router, session, scenario, expired]);

  return (
    <div className="checkout-card">
      <div className="checkout-card-copy">
        <span className="checkout-step">01 · Test the leak</span>
        <h2>Open a real Razorpay test Checkout.</h2>
        <p>Dismiss it, trigger a test failure, or complete it. Leakproof records bounded browser signals while server-verified Razorpay sandbox truth decides payment success.</p>
      </div>
      <fieldset className="checkout-scenarios" disabled={Boolean(session) && !expired}>
        <legend>Choose a rehearsal</legend>
        <label><input type="radio" name="scenario" checked={scenario === "CHECKOUT_ABANDON"} onChange={() => setScenario("CHECKOUT_ABANDON")} /> Checkout abandonment</label>
        <label><input type="radio" name="scenario" checked={scenario === "PAYMENT_FAILURE"} onChange={() => setScenario("PAYMENT_FAILURE")} /> Payment failure</label>
      </fieldset>
      <p>{scenario === "CHECKOUT_ABANDON" ? "Open Checkout, close it without paying, then watch the waiting and provider recheck below. Continue recovery once the original order is confirmed unpaid." : "Use a Razorpay test failure, then follow the recovery case. Provider-confirmed failure takes precedence over dismissal."}</p>
      <label className="checkout-field">
        <span>Recovery email <small>optional</small></span>
        <input
          type="email"
          value={recipient}
          onChange={(event) => setRecipient(event.target.value)}
          placeholder="reviewer@example.com"
          autoComplete="email"
          disabled={Boolean(session) && !expired}
        />
        <small>Only allowlisted addresses receive mail. Others stay preview-only.</small>
      </label>
      {session && <OrderReceipt checkout={session} />}
      <button className="checkout-primary" type="button" onClick={start} disabled={state === "preparing" || (!expired && (state === "open" || projection?.state === "RECOVERED" || Boolean(projection?.recovery_path)))}>
        {state === "preparing" ? "Preparing secure Checkout…" : expired ? "Start a new rehearsal" : session ? "Resume Checkout" : scenario === "CHECKOUT_ABANDON" ? "Start checkout abandonment" : "Create test order & open Checkout"}
        <span aria-hidden="true">→</span>
      </button>
      <CheckoutStatus state={state} message={message} />
      {expired && <p role="status">Session expired. Recovery is disabled; start a new rehearsal.</p>}
      {!expired && projectionError && <p role="status">Status delayed: {projectionError}</p>}
      {!expired && projection && <AbandonmentStatus check={projection.abandonment_check} />}
      {!expired && projection?.recovery_path && <Link className="checkout-primary" href={projection.recovery_path}>Continue recovery →</Link>}
      {session && <Link className="checkout-secondary" href="/">Watch the live dashboard →</Link>}

      <p className="checkout-fineprint">Test mode only · Amount and currency are fixed server-side · No automatic charge</p>
    </div>
  );
}

export function RecoveryCheckout({ token }: { token: string }) {
  const router = useRouter();
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

  const reopen = async () => {
    if (!bootstrap || loading.current) return;
    loading.current = true;
    setState("preparing");
    setMessage("Rechecking the original order with Razorpay…");
    try {
      const fresh = await getRecoveryBootstrap(token);
      setBootstrap(fresh);
      let active: DemoSession | undefined;
      try {
        const stored = JSON.parse(sessionStorage.getItem(SESSION_STORAGE_KEY) ?? "null") as DemoSession | null;
        if (stored?.session_id === fresh.session_id) active = stored;
      } catch { /* A recovery link works without browser session storage. */ }
      if (active) await flushTelemetry(active);
      openCheckout({
        checkout: fresh,
        session: active,
        verificationAuthorization: { recoveryToken: token },
        recovery: true,
        setState,
        setMessage,
        onOutcome: () => router.replace("/"),
      });
    } catch (error) {
      setState("failed");
      setMessage(errorMessage(error));
      setBootstrap(undefined);
    } finally {
      loading.current = false;
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
