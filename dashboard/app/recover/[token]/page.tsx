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
          <h1>Continue your payment.<br /><em>Review the current balance.</em></h1>
          <p className="checkout-lede">Your secure link checks the current payment state with Razorpay. Continue on the original payment page when payment is available, or contact the merchant for help.</p>
          <div className="recovery-bindings">
            <div><span>Bound</span><strong>Session + merchant</strong></div>
            <div><span>Locked</span><strong>Original payment</strong></div>
            <div><span>Checked</span><strong>Unpaid at open</strong></div>
          </div>
        </section>
        <RecoveryCheckout token={token} />
      </div>
    </main>
  );
}
