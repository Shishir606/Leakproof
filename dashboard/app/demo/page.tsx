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
          <p className="checkout-lede">Choose a Razorpay Test Mode recovery rehearsal.</p>
          <div className="checkout-flow" aria-label="Demo flow">
            <div><span>1</span><strong>Set up</strong></div>
            <i />
            <div><span>2</span><strong>Detect</strong></div>
            <i />
            <div><span>3</span><strong>Recover</strong></div>
          </div>
        </section>
        <DemoCheckout />
      </div>
    </main>
  );
}
