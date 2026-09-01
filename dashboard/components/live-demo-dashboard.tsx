"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { DemoApiError, getSessionProjection } from "@/lib/demo-api";
import type {
  DemoSession,
  DemoSessionProjection,
  ProviderStatus,
  TimelineItem,
} from "@/lib/demo-types";
import { label, money, percent } from "@/lib/format";

const SESSION_STORAGE_KEY = "leakproof:active-demo-session";
const ACTIVE_STATES = new Set(["CREATED", "CHECKOUT_OPEN", "AT_RISK"]);
const SOURCE_LABELS: Record<TimelineItem["source"], string> = {
  browser: "Browser Telemetry",
  razorpay: "Razorpay Webhook",
  openai: "Luna",
  resend: "Resend",
  leakproof: "Leakproof",
};

function time(value: string) {
  return new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function elapsed(seconds: number | null) {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function receipt(status: ProviderStatus) {
  if (!status.request_id) return "No receipt yet";
  return `…${status.request_id.slice(-12)}`;
}

function detail(payload: Record<string, unknown>) {
  const preferred = ["failure_class", "recommended_action", "decision", "status", "outcome", "reason"];
  for (const key of preferred) {
    const value = payload[key];
    if (typeof value === "string" && value) return label(value);
  }
  return "Sanitized audit event";
}

function scopeDetail(payload: Record<string, unknown>) {
  const scope = payload.scope;
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return "No scope proposed";
  const entries = Object.entries(scope as Record<string, unknown>)
    .filter(([, value]) => typeof value === "string")
    .map(([key, value]) => `${label(key)} ${String(value)}`);
  return entries.length ? entries.join(" · ") : "No scope proposed";
}

function EmptyLiveDemo() {
  return (
    <section className="live-empty">
      <span className="live-radar"><i /></span>
      <p className="eyebrow">No active public session</p>
      <h2>Start with one real Razorpay test order.</h2>
      <p>
        Dismiss Checkout or use a Razorpay failure path, then return here to watch the
        diagnosis, Luna explanation, recovery action, and provider receipts update live.
      </p>
      <Link className="live-primary-link" href="/demo">
        Start live checkout <span>→</span>
      </Link>
      <small>Test mode only · fixed ₹500 order · no automatic debit</small>
    </section>
  );
}

export function LiveDemoDashboard() {
  const [session, setSession] = useState<DemoSession | null>(null);
  const [projection, setProjection] = useState<DemoSessionProjection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (!raw) return;
      const stored = JSON.parse(raw) as DemoSession;
      if (new Date(stored.expires_at).getTime() <= Date.now()) {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
        return;
      }
      setSession(stored);
    } catch {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await getSessionProjection(session);
        if (cancelled) return;
        if (next.data_provenance !== "LIVE_PROVIDER_VERIFIED") {
          throw new DemoApiError(
            `Live Demo rejected ${next.data_provenance} data. Use Scenario Lab for simulated results.`,
            "provenance_mismatch",
          );
        }
        setProjection(next);
        setError(null);
        const unexpired = new Date(next.expires_at).getTime() > Date.now();
        if (ACTIVE_STATES.has(next.state) && unexpired) {
          timer = setTimeout(poll, 2_000);
        } else {
          sessionStorage.removeItem(SESSION_STORAGE_KEY);
        }
      } catch (caught) {
        if (cancelled) return;
        setError(
          caught instanceof DemoApiError || caught instanceof Error
            ? caught.message
            : "The live projection is unavailable.",
        );
        if (new Date(session.expires_at).getTime() > Date.now()) {
          timer = setTimeout(poll, 2_000);
        }
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [session]);

  if (loading) return <section className="live-empty"><p>Loading active session…</p></section>;
  if (!session) return <EmptyLiveDemo />;
  if (!projection) {
    return (
      <section className="live-empty">
        <p className="eyebrow">Connecting to recovery spine</p>
        <h2>{error ?? "Reading the live session…"}</h2>
        <Link className="live-primary-link" href="/demo">Return to checkout <span>→</span></Link>
      </section>
    );
  }

  const currentCase = projection.case;
  const latestProviders = new Map<string, ProviderStatus>();
  for (const status of projection.provider_statuses) latestProviders.set(status.provider, status);
  const events = [...projection.timeline].reverse().slice(0, 12);
  const diagnosis = currentCase?.deterministic_diagnosis;
  const cohortProposal = [...projection.timeline]
    .reverse()
    .find((event) => event.kind === "AI_PROPOSED");
  const cohortVerdict = [...projection.timeline]
    .reverse()
    .find((event) =>
      ["POLICY_VALIDATED", "AI_PROPOSAL_REJECTED", "AI_DEGRADED"].includes(event.kind),
    );

  return (
    <div className="live-dashboard">
      {error && <div className="live-warning" role="status">Polling delayed: {error}</div>}
      <header className="live-header">
        <div>
          <p className="eyebrow">Public recovery · live API</p>
          <h1>Watch one payment <em>come back.</em></h1>
          <p>Provider truth and browser intent, kept visibly separate.</p>
        </div>
        <div className="live-session-meta">
          <span className={`live-state state-${projection.state.toLowerCase()}`}>
            <i /> {label(projection.state)}
          </span>
          <small>Session …{projection.session_id.slice(-10)}</small>
          <Link href="/demo">Open Checkout</Link>
        </div>
      </header>

      <section className="source-legend" aria-label="Live data sources">
        {(["browser", "razorpay", "openai", "resend"] as const).map((source) => (
          <span className={`source-badge source-${source}`} key={source}>
            <i /> {SOURCE_LABELS[source]}
          </span>
        ))}
        <small>Polling every 2 seconds while active</small>
      </section>

      <section className="live-metrics" aria-label="Live operational metrics">
        <div><span>Cases detected</span><strong>{projection.metrics.cases_detected}</strong><small>live sessions only</small></div>
        <div><span>Recovered</span><strong>{projection.metrics.recovered_cases}</strong><small>{percent(projection.metrics.recovery_rate)} recovery rate</small></div>
        <div><span>Amount recovered</span><strong>{money(projection.metrics.recovered_amount_paise)}</strong><small>verified provider truth</small></div>
        <div><span>Median recovery</span><strong>{elapsed(projection.metrics.median_recovery_time_seconds)}</strong><small>{projection.metrics.provider_failures} provider failures</small></div>
        <div><span>Luna cost</span><strong>{money(projection.metrics.luna_cost_paise)}</strong><small>recorded API cost</small></div>
      </section>

      <section className="live-main-grid">
        <article className="live-case-card">
          <div className="live-panel-heading">
            <div><p className="eyebrow">Current case</p><h2>{currentCase ? label(currentCase.leak_type) : "Waiting for a risk signal"}</h2></div>
            <span>{currentCase ? label(currentCase.state) : label(projection.state)}</span>
          </div>
          <div className="live-amount-row">
            <div><span>Detected amount</span><strong>{money(projection.amount_paise)}</strong></div>
            <div><span>End-to-end latency</span><strong>{elapsed(projection.end_to_end_latency_seconds)}</strong></div>
          </div>
          <div className="decision-cards">
            <div>
              <span>Deterministic diagnosis</span>
              <strong>{diagnosis ? label(diagnosis.failure_class) : "Pending"}</strong>
              <small>{diagnosis ? `${diagnosis.rule_id ?? "Tier 1"} · ${percent(diagnosis.confidence)}` : "Authority remains deterministic"}</small>
            </div>
            <div>
              <span>Gate decision</span>
              <strong>{projection.gate_verdict ? label(projection.gate_verdict) : "Pre-flight pending"}</strong>
              <small>Applied immediately before outbound email</small>
            </div>
            <div>
              <span>AI cohort proposal</span>
              <strong>{cohortProposal ? label(String(cohortProposal.payload.recommended_action ?? "NO_ACTION")) : "No qualified cohort yet"}</strong>
              <small>{cohortProposal ? scopeDetail(cohortProposal.payload) : "Only observed aggregate attempt data can qualify"}</small>
            </div>
            <div>
              <span>Deterministic cohort verdict</span>
              <strong>{cohortVerdict ? label(cohortVerdict.kind) : "Awaiting proposal"}</strong>
              <small>{cohortVerdict ? detail(cohortVerdict.payload) : "Scope, thresholds, confidence and TTL are rechecked"}</small>
            </div>
          </div>
          <div className={`insight-card insight-${currentCase?.insight_status ?? "pending"}`}>
            <div><span>Luna explanation</span><b>{currentCase ? label(currentCase.insight_status) : "Awaiting case"}</b></div>
            <h3>{currentCase?.insight?.summary ?? "Recovery stays available while the explanation is prepared."}</h3>
            <p>{currentCase?.insight?.recommended_next_step ?? "No model output can approve an action or override the gate."}</p>
          </div>
        </article>

        <article className="live-actions-card">
          <div className="live-panel-heading"><div><p className="eyebrow">Bounded ladder</p><h2>Recovery actions</h2></div><span>{projection.email_mode.replaceAll("_", " ")}</span></div>
          {projection.recovery_actions.length ? (
            <div className="live-action-list">
              {projection.recovery_actions.map((action, index) => (
                <div className="live-action" key={action.action_id ?? action.action_type}>
                  <span className="live-action-index">{index}</span>
                  <div><strong>{label(action.action_type)}</strong><small>{action.action_type === "recovery_link" ? "Customer-authorized original order" : `Due ${time(action.scheduled_for)}`}</small></div>
                  <span className={`action-status status-${action.status}`}>{label(action.status)}</span>
                  <div className="live-action-receipt"><span>Gate</span><strong>{action.gate_verdict ? label(action.gate_verdict) : "Pending"}</strong><small>{action.provider_receipt_id ? `Receipt …${action.provider_receipt_id.slice(-10)}` : "No provider receipt"}</small></div>
                </div>
              ))}
            </div>
          ) : <p className="live-placeholder">The in-app recovery link appears as soon as a verified case is detected.</p>}
        </article>
      </section>

      <section className="live-bottom-grid">
        <article className="live-providers-card">
          <div className="live-panel-heading"><div><p className="eyebrow">Provider receipts</p><h2>Integration health</h2></div></div>
          <div className="provider-list">
            {(["razorpay", "openai", "resend"] as const).map((provider) => {
              const status = latestProviders.get(provider);
              return (
                <div key={provider}>
                  <span className={`source-badge source-${provider}`}><i /> {provider === "openai" ? "Luna" : label(provider)}</span>
                  <strong>{status ? label(status.status) : "Waiting"}</strong>
                  <small>{status ? `${label(status.operation)} · ${status.latency_ms ?? 0}ms · ${receipt(status)}` : "No call recorded"}</small>
                </div>
              );
            })}
          </div>
        </article>

        <article className="live-timeline-card">
          <div className="live-panel-heading"><div><p className="eyebrow">Sanitized audit</p><h2>Live timeline</h2></div><span>{projection.timeline.length} events</span></div>
          {events.length ? (
            <div className="live-event-list">
              {events.map((event, index) => (
                <div className="live-event" key={`${event.occurred_at}-${event.kind}-${index}`}>
                  <span className={`source-dot source-${event.source}`} />
                  <div><strong>{label(event.kind)}</strong><small>{detail(event.payload)}</small></div>
                  <span className={`source-badge compact source-${event.source}`}>{SOURCE_LABELS[event.source]}</span>
                  <time>{time(event.occurred_at)}</time>
                </div>
              ))}
            </div>
          ) : <p className="live-placeholder">Open Checkout to begin the browser telemetry timeline.</p>}
        </article>
      </section>
    </div>
  );
}
