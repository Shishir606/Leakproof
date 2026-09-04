import Link from "next/link";
import { DemoCheckout } from "@/components/razorpay-checkout";
import { Logo } from "@/components/shell";

export const metadata = {
  title: "Recovery Lab · Leakproof",
  description: "Choose an evidence-labelled Razorpay Test Mode recovery rehearsal.",
};

export default function DemoPage() {
  return (
    <main className="checkout-page">
      <header className="checkout-nav">
        <Logo />
        <nav><Link href="/">Live dashboard</Link><Link href="/scenario-lab">Scenario Lab</Link></nav>
      </header>
      <div className="checkout-layout">
        <section className="checkout-hero">
          <p className="eyebrow">Recruiter recovery lab</p>
          <h1>Five leak surfaces.<br /><em>One recovery spine.</em></h1>
          <p className="checkout-lede">Choose a bounded Razorpay Test Mode rehearsal. Each card states what is available, what you need to do, and which evidence can support the result.</p>
          <div className="checkout-flow" aria-label="Demo flow">
            <div><span>1</span><strong>Set up</strong><small>Test resource</small></div>
            <i />
            <div><span>2</span><strong>Detect</strong><small>Browser or provider</small></div>
            <i />
            <div><span>3</span><strong>Recover</strong><small>Original obligation</small></div>
          </div>
          <aside className="checkout-proof">
            <span className="proof-mark">LP</span>
            <p><strong>No fixture is presented as provider proof.</strong> Browser intent, provider state, deterministic decisions, and Luna explanations remain visibly distinct.</p>
          </aside>
        </section>
        <DemoCheckout />
      </div>
    </main>
  );
}
