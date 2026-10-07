"""Minimal Stripe client (Checkout + webhook signature check) over the REST API."""
import hashlib
import hmac
import time

import requests

API = "https://api.stripe.com/v1"


class StripeError(RuntimeError):
    pass


class Stripe:
    def __init__(self, secret_key: str, session: requests.Session | None = None):
        if not secret_key:
            raise StripeError("STRIPE_SECRET_KEY is not set")
        self.key = secret_key
        self.http = session or requests.Session()

    def _req(self, method: str, path: str, **kwargs) -> dict:
        resp = self.http.request(method, f"{API}{path}", auth=(self.key, ""), timeout=30, **kwargs)
        data = resp.json()
        if resp.status_code >= 400:
            raise StripeError(data.get("error", {}).get("message", resp.text))
        return data

    def create_checkout_session(self, price_id: str, success_url: str, cancel_url: str) -> dict:
        return self._req("POST", "/checkout/sessions", data={
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": 1,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": "true",
            "consent_collection[terms_of_service]": "required",
        })

    def retrieve_checkout_session(self, session_id: str) -> dict:
        return self._req("GET", f"/checkout/sessions/{session_id}")

    def create_portal_session(self, customer_id: str, return_url: str) -> dict:
        return self._req("POST", "/billing_portal/sessions", data={"customer": customer_id, "return_url": return_url})


def verify_webhook(payload: bytes, sig_header: str, secret: str, tolerance: int = 300, now: float | None = None) -> bool:
    """Check a `Stripe-Signature` header (https://docs.stripe.com/webhooks#verify-manually)."""
    if not secret or not sig_header:
        return False
    try:
        parts = [p.split("=", 1) for p in sig_header.split(",")]
        timestamp = int(next(v for k, v in parts if k == "t"))
        signatures = [v for k, v in parts if k == "v1"]
    except (StopIteration, ValueError):
        return False
    if abs((now or time.time()) - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)
