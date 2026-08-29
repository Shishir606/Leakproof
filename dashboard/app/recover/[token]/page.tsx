import Link from "next/link";
import { RecoveryCheckout } from "@/components/razorpay-checkout";
import { Logo } from "@/components/shell";

export const metadata = {
  title: "Resume payment · Leakproof",
  description: "Securely reopen Razorpay Checkout for the original unpaid order.",
};

export default async function RecoveryPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  return (
    <main className="checkout-page recovery-page">
      <header className="checkout-nav">
        <Logo />
        <nav><Link href="/demo">Back to live demo</Link></nav>
      </header>
      <div className="checkout-layout">
        <section className="checkout-hero">
          <p className="eyebrow">Payment recovery</p>
          <h1>Your basket is still here.<br /><em>Your order is unchanged.</em></h1>
          <p className="checkout-lede">Leakproof does not create a payment link or replacement order. This route verifies the signed claims and reopens the original Razorpay Checkout.</p>
          <div className="recovery-bindings">
            <div><span>Bound</span><strong>Session + merchant</strong></div>
            <div><span>Locked</span><strong>Order + amount</strong></div>
            <div><span>Checked</span><strong>Unpaid at open</strong></div>
          </div>
        </section>
        <RecoveryCheckout token={token} />
      </div>
    </main>
  );
}
