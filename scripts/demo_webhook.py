"""Send one SIMULATED_END_TO_END fixture; this is not provider-verified recovery evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request

from leakproof.config import get_settings

payload = {
    "event": "payment.failed",
    "created_at": 1787625000,
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_demo_aug25",
                "order_id": "order_demo_aug25",
                "customer_id": "customer_demo_aug25",
                "amount": 125000,
                "currency": "INR",
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "gateway_technical_error",
            }
        }
    },
}
body = json.dumps(payload, separators=(",", ":")).encode()
secret = get_settings().razorpay_webhook_secret
signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
request = urllib.request.Request(
    "http://localhost:8000/webhooks/razorpay",
    data=body,
    method="POST",
    headers={
        "content-type": "application/json",
        "x-razorpay-signature": signature,
        "x-razorpay-event-id": "rzp_evt_demo_aug25",
        "x-leakproof-merchant-id": "merchant_demo",
    },
)
with urllib.request.urlopen(request) as response:
    print(response.read().decode())
