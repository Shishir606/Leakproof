import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowIcon, ShieldIcon } from "@/components/icons";
import { EmptyState, Shell } from "@/components/shell";
import { ApiError, getCase, getCases } from "@/lib/api";
import { label, money, moment, shortId } from "@/lib/format";
import type { CaseDetail, CaseEvent, CaseListItem } from "@/lib/types";

export const dynamic = "force-dynamic";

type Search = Record<string, string | string[] | undefined>;
const LEAK_TYPES = ["PAYMENT_FAILURE", "INVOICE_OVERDUE", "CHECKOUT_ABANDON", "SUBSCRIPTION_HALT"];
const STATES = ["DETECTED", "DIAGNOSED", "PLANNED", "ACTING", "WAITING", "VERIFYING", "CLOSED", "SUPPRESSED", "STOPPED", "ESCALATED"];

function first(value: string | string[] | undefined) {
  return Array.isArray(value) ? value[0] : value;
}

function hrefFor(item: CaseListItem, filters: { state?: string; leakType?: string; batchRunId?: string }) {
  const query = new URLSearchParams({ case_id: item.id });
  if (filters.state) query.set("state", filters.state);
  if (filters.leakType) query.set("leak_type", filters.leakType);
  if (filters.batchRunId) query.set("batch_run_id", filters.batchRunId);
  return `/cases?${query}`;
}

function EventItem({ event, terminal }: { event: CaseEvent; terminal: boolean }) {
  const summaries: Record<string, string> = {
    DETECTED: "Revenue risk signal opened a recovery case.",
    ASSIGNED: `Experiment arm fixed to ${String(event.payload.arm ?? "unknown").toLowerCase()}.`,
    SIGNAL: "Additional evidence merged into the existing case.",
    DIAGNOSED: `${String(event.payload.failure_class ?? "Failure")} · ${String(event.payload.rule_id ?? "deterministic rule")}`,
    PLANNED: "Bounded recovery ladder selected by expected value.",
    GATE: `${String(event.payload.decision ?? "Verdict")} · ${String(event.payload.reason ?? "all guardrails evaluated")}`,
    ACTED: `${label(String(event.payload.action_type ?? "Action"))} · ${String(event.payload.status ?? "executed")}`,
    VERIFYING: "Payment signal matched and attribution window checked.",
    CLOSED: `Final outcome: ${String(event.payload.outcome ?? "closed")}.`,
    SUPPRESSED: "Circuit breaker stopped matching recovery work.",
    AI_PROPOSED: `AI proposed ${label(String(event.payload.recommended_action ?? "No action"))} from a bounded evidence slice.`,
    AI_PROPOSAL_REJECTED: `Deterministic validation rejected the proposal: ${String(event.payload.reason ?? "unsupported proposal")}.`,
    POLICY_VALIDATED: "Deterministic policy validated the evidence, scope, confidence, and TTL.",
    SUPPRESSION_OPENED: "A scoped circuit breaker opened after deterministic validation.",
    RETRY_DELAYED: "Matching retries were delayed; unrelated cases continue.",
    MERCHANT_ALERTED: "A scoped merchant alert was recorded.",
    NO_ACTION: `No cohort actuator ran: ${String(event.payload.reason ?? "no supported intervention")}.`,
    AI_DEGRADED: "The model was unavailable or invalid; deterministic recovery continued safely.",
    ESCALATED: "Case transferred for human judgement.",
  };
  return (
    <div className={`event ${terminal ? "terminal" : ""}`}>
      <div className="event-rail"><i /><span /></div>
      <div className="event-body">
        <div className="event-title"><strong>{label(event.kind)}</strong><time>{moment(event.occurred_at)}</time></div>
        <p>{summaries[event.kind] ?? "Append-only case event recorded."}</p>
        <div className="event-meta"><span>#{event.seq}</span><span>{event.actor}</span></div>
        <details><summary>Inspect evidence</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
      </div>
    </div>
  );
}

function CaseHeader({ detail }: { detail: CaseDetail }) {
  const item = detail.case;
  return (
    <div className="case-detail-header">
      <div>
        <div className="case-kicker"><span className={`state-dot state-${item.state.toLowerCase()}`} />{label(item.state)} · {item.arm.toLowerCase()}</div>
        <h2>{label(item.leak_type)}</h2>
        <p>{shortId(item.id)} · {item.entity_type} {shortId(item.entity_id)}</p>
      </div>
        <div className="case-amount"><span>Amount at risk</span><strong>{money(item.amount_at_risk)}</strong><small>{item.batch_run_id ? "measured batch" : "continuous recovery"}</small></div>
    </div>
  );
}

function CaseInspector({ detail }: { detail: CaseDetail }) {
  return (
    <>
      <CaseHeader detail={detail} />
      <div className={detail.replay.projection_matches ? "integrity-line" : "integrity-line mismatch"}>
        <ShieldIcon />
        <span><strong>Audit projection {detail.replay.projection_matches ? "verified" : "needs review"}</strong>Replayed from {detail.replay.events.length} immutable events</span>
        <a href={`/api/audit/${detail.case.id}`} title="The API endpoint is exposed for export">audit.json</a>
      </div>

      <section className="decision-strip">
        <div><span>Experiment arm</span><strong>{label(detail.case.arm)}</strong></div>
        <div><span>Diagnosis</span><strong>{detail.diagnosis ? label(detail.diagnosis.failure_class) : "Pending"}</strong><small>{detail.diagnosis?.rule_id ?? "—"}</small></div>
        <div><span>Planned actions</span><strong>{detail.actions.length}</strong><small>{detail.actions.filter((action) => action.executed_at).length} executed</small></div>
        <div><span>Attribution</span><strong>{detail.attribution ? (detail.attribution.organic ? "Organic" : "Last touch") : "Pending"}</strong><small>{detail.attribution ? money(detail.attribution.amount_paise) : "—"}</small></div>
      </section>

      {detail.actions.length > 0 && (
        <section className="action-ladder">
          <div className="section-heading"><div><p className="eyebrow">Recovery ladder</p><h3>Bounded action plan</h3></div></div>
          <div className="action-steps">
            {detail.actions.map((action) => (
              <div className="action-step" key={action.id}>
                <span className="step-index">{action.step_index + 1}</span>
                <div><strong>{label(action.action_type)}</strong><small>{moment(action.scheduled_for)}</small></div>
                <div className="action-ev"><span>Expected value</span><strong>{action.ev_estimate == null ? "—" : money(action.ev_estimate)}</strong></div>
                <span className={`action-status status-${(action.status ?? "scheduled").toLowerCase()}`}>{label(action.status ?? "scheduled")}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="timeline-section">
        <div className="section-heading"><div><p className="eyebrow">Append-only record</p><h3>Case timeline</h3></div><span>IST · oldest first</span></div>
        <div className="events">
          {detail.replay.events.map((event, index) => <EventItem event={event} terminal={index === detail.replay.events.length - 1} key={event.seq} />)}
        </div>
      </section>
    </>
  );
}

export default async function CasesPage({ searchParams }: { searchParams: Promise<Search> }) {
  if (process.env.LEAKPROOF_OPERATOR_UI_ENABLED !== "true") notFound();
  const query = await searchParams;
  const filters = {
    state: first(query.state),
    leakType: first(query.leak_type),
    batchRunId: first(query.batch_run_id),
  };
  let cases;
  try {
    cases = await getCases({ ...filters, limit: 40 });
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : "Cases are unavailable.";
    return <Shell active="cases"><EmptyState title="Cannot load cases" detail={detail} /></Shell>;
  }
  const selectedId = first(query.case_id) ?? cases.items[0]?.id;
  const detail = selectedId ? await getCase(selectedId).catch(() => null) : null;

  return (
    <Shell active="cases">
      <div className="cases-page">
        <header className="cases-topbar">
          <div><p className="eyebrow">Evidence explorer</p><h1>Case timeline</h1><p>Follow every decision from signal to verified outcome.</p></div>
          <form className="filters" action="/cases">
            {filters.batchRunId && <input type="hidden" name="batch_run_id" value={filters.batchRunId} />}
            <label><span>Leak type</span><select name="leak_type" defaultValue={filters.leakType ?? ""}><option value="">All surfaces</option>{LEAK_TYPES.map((value) => <option value={value} key={value}>{label(value)}</option>)}</select></label>
            <label><span>Case state</span><select name="state" defaultValue={filters.state ?? ""}><option value="">All states</option>{STATES.map((value) => <option value={value} key={value}>{label(value)}</option>)}</select></label>
            <button type="submit">Apply</button>
          </form>
        </header>

        <div className="case-workspace">
          <aside className="case-list-panel">
            <div className="list-heading"><span>{cases.total} cases</span><small>most recent first</small></div>
            <div className="case-list">
              {cases.items.map((item) => (
                <Link className={item.id === selectedId ? "case-list-item selected" : "case-list-item"} href={hrefFor(item, filters)} key={item.id}>
                  <div className="case-list-top"><span className={`leak-pill leak-${item.leak_type.toLowerCase()}`}>{label(item.leak_type)}</span><time>{moment(item.detected_at)}</time></div>
                  <strong>{money(item.amount_at_risk)} <small>at risk</small></strong>
                  <div className="case-list-bottom"><span><i className={`state-dot state-${item.state.toLowerCase()}`} />{label(item.state)}</span><span>{item.event_count} events</span></div>
                  <ArrowIcon />
                </Link>
              ))}
              {cases.items.length === 0 && <div className="list-empty"><p>No cases match these filters.</p><Link href="/cases">Clear filters</Link></div>}
            </div>
          </aside>
          <article className="case-inspector">
            {detail ? <CaseInspector detail={detail} /> : <div className="inspector-empty"><span>↗</span><h2>Select a case</h2><p>Its immutable audit trail will appear here.</p></div>}
          </article>
        </div>
      </div>
    </Shell>
  );
}
