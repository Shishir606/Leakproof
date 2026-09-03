"use client";

import { useEffect, useState } from "react";
import { DemoApiError, getSessionProjection } from "./demo-api";
import { flushTelemetry } from "./checkout-telemetry";
import type { DemoSession, DemoSessionProjection } from "./demo-types";

export function useSessionProjection(session: DemoSession | null | undefined) {
  const [projection, setProjection] = useState<DemoSessionProjection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    setProjection(null);
    setError(null);
    setExpired(false);
    if (!session) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        if (Date.parse(session.expires_at) <= Date.now()) throw new DemoApiError("Session expired", "session_expired");
        await flushTelemetry(session);
        const next = await getSessionProjection(session);
        if (stopped) return;
        if (!["LIVE_PROVIDER_VERIFIED", "LIVE_TELEMETRY_PROVIDER_RECONCILED"].includes(next.data_provenance)) {
          throw new DemoApiError(`Live Demo rejected ${next.data_provenance} data. Use Scenario Lab for simulated results.`, "provenance_mismatch");
        }
        setProjection(next);
        setError(null);
        if (next.state === "RECOVERED") return;
      } catch (caught) {
        if (stopped) return;
        setError(caught instanceof Error ? caught.message : "Session status unavailable.");
        if (caught instanceof DemoApiError && ["session_expired", "invalid_session_token"].includes(caught.code)) {
          setExpired(true);
          sessionStorage.removeItem("leakproof:active-demo-session");
          localStorage.removeItem(`leakproof:checkout-events:${session.session_id}`);
          return;
        }
        if (caught instanceof DemoApiError && caught.code === "provenance_mismatch") return;
      }
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [session]);
  return { projection, error, expired };
}
