import Link from "next/link";
import { DemoCheckout } from "@/components/razorpay-checkout";
import { Logo } from "@/components/shell";

export const metadata = {
  title: "Live Razorpay demo · Leakproof",
  description: "Create a Razorpay test order and see revenue recovery telemetry in action.",
};

export default function DemoPage() {
  return (
    <main className="checkout-page">
      <header className="checkout-nav">
        <Logo />
        <nav><Link href="/">Recovery scoreboard</Link><Link href="/cases">Case timeline</Link></nav>
      </header>
      <div className="checkout-layout">
        <section className="checkout-hero">
          <p className="eyebrow">Live recovery lab</p>
          <h1>Let one checkout <em>slip.</em><br />Watch the recovery spine catch it.</h1>
          <p className="checkout-lede">A controlled, ₹500 Razorpay test flow. The browser reports intent; the payment provider supplies the truth.</p>
          <div className="checkout-flow" aria-label="Demo flow">
            <div><span>1</span><strong>Create</strong><small>Server-fixed order</small></div>
            <i />
            <div><span>2</span><strong>Attempt</strong><small>Razorpay Checkout</small></div>
            <i />
            <div><span>3</span><strong>Recover</strong><small>Same original order</small></div>
          </div>
          <aside className="checkout-proof">
            <span className="proof-mark">LP</span>
            <p><strong>No browser-selected money.</strong> The API creates the order and returns only the public Checkout material.</p>
          </aside>
        </section>
        <DemoCheckout />
      </div>
    </main>
  );
}
