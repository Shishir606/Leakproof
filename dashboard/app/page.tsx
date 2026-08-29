import { LiveDemoDashboard } from "@/components/live-demo-dashboard";
import { Shell } from "@/components/shell";

export const metadata = {
  title: "Live Demo · Leakproof",
  description: "Live Razorpay recovery state, provider receipts, and operational metrics.",
};

export default function LiveDemoPage() {
  return (
    <Shell active="live">
      <LiveDemoDashboard />
    </Shell>
  );
}
