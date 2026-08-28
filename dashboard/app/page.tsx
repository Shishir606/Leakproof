import Link from "next/link";
import { ArrowIcon, CoinsIcon, PulseIcon, ShieldIcon } from "@/components/icons";
import { EmptyState, Shell } from "@/components/shell";
import { ApiError, getExceptionReport, getLatestEvals, getLatestScoreboard } from "@/lib/api";
import { duration, label, money, percent, shortId } from "@/lib/format";

export const dynamic = "force-dynamic";

const LEAK_COLORS = ["cobalt", "orange", "mint", "violet", "sand"];

export default async function ScoreboardPage() {
  let scoreboard;
  try {
    scoreboard = await getLatestScoreboard();
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : "The latest recovery run is unavailable.";
    return <Shell active="overview"><EmptyState title="No scoreboard yet" detail={detail} /></Shell>;
  }
  const evaluations = await getLatestEvals().catch(() => null);
  const exceptions = await getExceptionReport(scoreboard.run_id).catch(() => null);
  const leakEntries = Object.entries(scoreboard.cases_by_leak_type).sort((a, b) => b[1] - a[1]);
  const largestLeak = Math.max(...leakEntries.map(([, count]) => count), 1);
  const costs = scoreboard.intervention_cost_paise + scoreboard.llm_cost_paise;

  return (
    <Shell active="overview">
      <div className="page-wrap">
        <header className="page-header">
          <div>
            <p className="eyebrow">Recovery command</p>
            <h1>Revenue, recovered <em>honestly.</em></h1>
            <p className="header-copy">Treatment-versus-holdout measurement across one bounded recovery spine.</p>
          </div>
          <div className="run-meta">
            <span className="data-badge">{scoreboard.synthetic ? "Synthetic dataset" : "Live dataset"}</span>
            <small>Batch {shortId(scoreboard.run_id)}</small>
            <strong>{scoreboard.cases_processed.toLocaleString("en-IN")} cases · {duration(scoreboard.duration_seconds)}</strong>
          </div>
        </header>

        <section className="hero-grid">
          <article className="net-value-card">
            <div className="card-label"><CoinsIcon /> Net value created</div>
            <div className="hero-value">{money(scoreboard.net_value_created_paise)}</div>
            <p>Incremental recovery after every intervention and model cost.</p>
            <div className="value-waterfall">
              <div><span>Incremental</span><strong>+{money(scoreboard.incremental_recovered_paise)}</strong></div>
              <i />
              <div><span>Total cost</span><strong>−{money(costs)}</strong></div>
            </div>
          </article>
          <article className="lift-card">
            <div className="card-label"><PulseIcon /> Measured recovery lift</div>
            <div className="lift-value">+{scoreboard.lift_percentage_points.toFixed(1)}<span>pp</span></div>
            <div className="arm-row">
              <span>Treatment</span>
              <div className="bar-track"><i className="bar treatment" style={{ width: `${Math.max(scoreboard.treatment.recovery_rate * 100, 2)}%` }} /></div>
              <strong>{percent(scoreboard.treatment.recovery_rate)}</strong>
            </div>
            <div className="arm-row">
              <span>Holdout</span>
              <div className="bar-track"><i className="bar holdout" style={{ width: `${Math.max(scoreboard.holdout.recovery_rate * 100, 2)}%` }} /></div>
              <strong>{percent(scoreboard.holdout.recovery_rate)}</strong>
            </div>
            <small>{scoreboard.estimator.replaceAll("_", " ")}</small>
          </article>
        </section>

        <section className="metric-strip" aria-label="Recovery metrics">
          <div><span>Gross recovered</span><strong>{money(scoreboard.gross_recovered_paise)}</strong><small>{scoreboard.treatment.recovered_cases} treated recoveries</small></div>
          <div><span>Organic holdout</span><strong>{money(scoreboard.organic_holdout_paise)}</strong><small>{scoreboard.holdout.recovered_cases} control recoveries</small></div>
          <div><span>Throughput</span><strong>{scoreboard.throughput_cases_per_minute.toFixed(1)}<sup>/min</sup></strong><small>{scoreboard.contacts} customer contacts</small></div>
          <div><span>Recovery cost</span><strong>{money(costs)}</strong><small>{money(scoreboard.llm_cost_paise)} model cost</small></div>
        </section>

        <section className="dashboard-grid">
          <article className="panel leak-mix">
            <div className="panel-heading"><div><p className="eyebrow">Portfolio</p><h2>Cases by leak surface</h2></div><span>{scoreboard.cases_processed} total</span></div>
            <div className="leak-list">
              {leakEntries.map(([leak, count], index) => (
                <div className="leak-row" key={leak}>
                  <div className={`leak-symbol ${LEAK_COLORS[index % LEAK_COLORS.length]}`}>{String(index + 1).padStart(2, "0")}</div>
                  <span>{label(leak)}</span>
                  <div className="leak-bar"><i className={LEAK_COLORS[index % LEAK_COLORS.length]} style={{ width: `${(count / largestLeak) * 100}%` }} /></div>
                  <strong>{count}</strong>
                </div>
              ))}
            </div>
          </article>

          <article className="panel safety-panel">
            <div className="panel-heading"><div><p className="eyebrow">Bounded by design</p><h2>Safety &amp; exceptions</h2></div><ShieldIcon /></div>
            <div className="safety-grid">
              <div className={scoreboard.false_chase_count === 0 ? "safe" : "warn"}><strong>{scoreboard.false_chase_count}</strong><span>False chases</span><small>target: zero</small></div>
              <div><strong>{scoreboard.suppressed_by_circuit_breaker}</strong><span>Suppressed</span><small>circuit breaker</small></div>
              <div><strong>{scoreboard.declined_ev_non_positive}</strong><span>Declined</span><small>EV ≤ 0</small></div>
              <div><strong>{scoreboard.escalated_to_human}</strong><span>Escalated</span><small>human review</small></div>
              <div><strong>{scoreboard.unresolved_exceptions}</strong><span>Unresolved</span><small>open exceptions</small></div>
              <div><strong>{percent(scoreboard.opt_out_rate)}</strong><span>Opt-out rate</span><small>contacted users</small></div>
            </div>
          </article>
        </section>

        <section className="panel exception-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">Nothing hidden</p><h2>Exception list</h2></div>
            <span>{exceptions?.total_cases ?? 0} non-recovered cases</span>
          </div>
          {exceptions?.groups.length ? (
            <div className="exception-table-wrap">
              <table className="exception-table">
                <thead><tr><th>Reason</th><th>Why it remains here</th><th>Cases</th><th>At risk</th></tr></thead>
                <tbody>
                  {exceptions.groups.map((group) => (
                    <tr key={group.reason}>
                      <td><strong>{label(group.reason)}</strong></td>
                      <td>{group.detail}</td>
                      <td>{group.cases.toLocaleString("en-IN")}</td>
                      <td>{money(group.amount_at_risk_paise)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="exception-empty">No unresolved or non-recovered cases in this batch.</p>}
          <p className="exception-footnote">The API retains every underlying case ID; this table groups them only for review.</p>
        </section>

        <section className="bottom-grid">
          <article className="eval-card">
            <div>
              <span className={evaluations?.overall_passed ? "status-pass" : "status-muted"}>{evaluations?.overall_passed ? "All gates passed" : "No retained evaluation"}</span>
              <h2>Adversarial checks stay in the loop.</h2>
              <p>{evaluations ? `${evaluations.runs.length} suites checked against the latest committed corpora.` : "Run make evals to attach the latest quality report."}</p>
            </div>
            <div className="eval-rings"><span>{evaluations?.runs.filter((run) => run.passed).length ?? 0}<small>passing<br/>suites</small></span></div>
          </article>
          <Link className="timeline-cta" href={`/cases?batch_run_id=${encodeURIComponent(scoreboard.run_id)}`}>
            <span>Follow the evidence</span>
            <h2>Open the case timeline</h2>
            <p>Every assignment, decision, gate verdict, action and outcome—in sequence.</p>
            <ArrowIcon />
          </Link>
        </section>
      </div>
    </Shell>
  );
}
