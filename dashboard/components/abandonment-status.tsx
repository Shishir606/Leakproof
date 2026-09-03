"use client";

import { useEffect, useState } from "react";
import type { AbandonmentCheck } from "@/lib/demo-types";

export function AbandonmentStatus({ check }: { check: AbandonmentCheck }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, []);
  if (!check || check.status === "idle") return null;
  const seconds = Math.max(0, Math.ceil(((check.due_at ? Date.parse(check.due_at) : now) - now) / 1000));
  const copy: Record<AbandonmentCheck["status"], [string, string]> = {
    idle: ["", ""],
    waiting: [seconds ? `Dismissal recorded · recheck in ${seconds}s` : "Waiting for provider recheck", "Browser telemetry recorded the close. Razorpay must confirm the order is still unpaid before abandonment is detected."],
    provider_recheck: ["Waiting for provider recheck", "The server is checking Razorpay. Scheduled retries keep this check pending if delivery is delayed; you can refresh safely."],
    provider_retry: ["Provider recheck delayed · retry scheduled", "Razorpay could not be checked. No new abandonment is confirmed from this attempt; the server will retry."],
    provider_pending: ["Payment pending at Razorpay", "A payment is in progress. Waiting for provider confirmation before offering recovery."],
    confirmed: ["Abandonment confirmed · original order unpaid", "The browser recorded dismissal; the Razorpay API confirmed no payment. Continue recovery with this same order."],
    payment_failure: ["Payment failure takes precedence", "Razorpay confirmed a failed payment. The same case now follows payment-failure recovery; the abandonment timer is cancelled."],
    recovered: ["Payment verified · recovery complete", "The original order is paid. Pending contact has been cancelled."],
  };
  const [title, description] = copy[check.status];
  return <section className="abandonment-status" role="status" aria-live="polite"><strong>{title}</strong><p>{description}</p></section>;
}
