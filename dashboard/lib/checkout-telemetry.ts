import { DemoApiError, sendCheckoutEvent } from "./demo-api";
import type { CheckoutEvent, DemoSession } from "./demo-types";

const inflight = new Map<string, Promise<void>>();
const key = (id: string) => `leakproof:checkout-events:${id}`;
function queued(id: string): CheckoutEvent[] {
  try { return JSON.parse(localStorage.getItem(key(id)) ?? "[]") as CheckoutEvent[]; }
  catch { return []; }
}
function save(id: string, events: CheckoutEvent[]) {
  if (events.length) localStorage.setItem(key(id), JSON.stringify(events));
  else localStorage.removeItem(key(id));
}

export function flushTelemetry(session: DemoSession): Promise<void> {
  const pending = inflight.get(session.session_id);
  if (pending) return pending;
  const run = async () => {
    while (queued(session.session_id).length) {
      const event = queued(session.session_id)[0];
      if (Date.parse(session.expires_at) <= Date.now()) {
        save(session.session_id, []);
        return;
      }
      try { await sendCheckoutEvent(session, event); }
      catch (error) {
        if (error instanceof DemoApiError && ["session_expired", "invalid_session_token"].includes(error.code)) save(session.session_id, []);
        return; // Preserve receipt order and the exact IDs for the next retry/refresh.
      }
      save(session.session_id, queued(session.session_id).filter(item => item.client_event_id !== event.client_event_id));
    }
  };
  const promise = run().finally(() => inflight.delete(session.session_id));
  inflight.set(session.session_id, promise);
  return promise;
}

export function deliverTelemetry(session: DemoSession, event: CheckoutEvent): Promise<void> {
  const events = queued(session.session_id);
  if (!events.some(item => item.client_event_id === event.client_event_id)) save(session.session_id, [...events, event]);
  return flushTelemetry(session);
}
