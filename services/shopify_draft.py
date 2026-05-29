"""
Shopify Draft Orders service
Creates a pre-filled draft order and returns the invoice URL,
OR forwards a Stripe embedded checkout client_secret if the bridge
routed the payment to Stripe instead of Shopify.
"""
import logging
import httpx
from config import settings
import json

logger = logging.getLogger(__name__)


class ShopifyError(Exception):
    pass


async def create_draft_order(
    order_id: str,
    email: str,
    first_name: str,
    last_name: str,
    address1: str,
    address2: str,
    city: str,
    province: str,
    postal_code: str,
    country: str,
    items: list,
    currency: str = "CAD",
    source_domain: str = "",
    billing: dict = None,
    store_country: str = "CA",
    discount_code: str = None,
    discount_amount: float = 0.0,
    payment_method_discount: float = 0.0,
) -> dict:
    """
    Calls the bridge worker to create a checkout session.

    Returns a dict with one of two shapes:

    Shopify mode (traditional draft order):
        {"type": "shopify", "invoice_url": "https://..."}

    Stripe embedded mode (modal payment on portal):
        {
          "type": "stripe_embedded",
          "client_secret": "cs_test_xxx_secret_yyy",
          "session_id": "cs_test_xxx",
          "publishable_key": "pk_test_..."
        }
    """

    shipping_address = {
        "first_name":    first_name or "",
        "last_name":     last_name  or "",
        "address1":      address1   or "",
        "address2":      address2   or "",
        "city":          city       or "",
        "province":      province   or "",
        "province_code": province   or "",
        "zip":           postal_code or "",
        "country":       country    or "CA",
        "country_code":  country    or "CA",
    }

    if billing:
        billing_address = {
            "first_name":    billing.get("first_name")  or first_name or "",
            "last_name":     billing.get("last_name")   or last_name  or "",
            "address1":      billing.get("address1")    or "",
            "address2":      billing.get("address2")    or "",
            "city":          billing.get("city")        or "",
            "province":      billing.get("province")    or "",
            "province_code": billing.get("province")    or "",
            "zip":           billing.get("postal_code") or "",
            "country":       billing.get("country")     or "CA",
            "country_code":  billing.get("country")     or "CA",
        }
    else:
        billing_address = shipping_address

    lines = [
        {
            "title":    str(item.title),
            "variant":  str(item.variant) if item.variant else None,
            "price":    f"{float(item.price):.2f}",
            "quantity": int(item.qty),
        }
        for item in items
    ]

    src = f"{source_domain} | ref:{order_id}" if source_domain else f"ref:{order_id}"

    bridge_payload = {
        "lines":                    lines,
        "currency":                 currency,
        "email":                    email,
        "shipping_address":         shipping_address,
        "billing_address":          billing_address,
        "source_store":             src,
        "discount_code":            discount_code,
        "discount":                 discount_code,    # for Stripe coupon lookup
        "order_id":                 order_id,         # for Stripe metadata
        "discount_amount":          round(float(discount_amount), 2) if discount_amount else 0.0,
        "payment_method_discount":  round(float(payment_method_discount), 2) if payment_method_discount else 0.0,
    }

    # Pick bridge based on the *store's* country (not customer shipping country)
    if (store_country or "CA").upper() == "US":
        bridge_url    = getattr(settings, "BRIDGE_URL_US", "")
        bridge_secret = getattr(settings, "BRIDGE_SECRET_US", "")
        if not bridge_url:
            raise ShopifyError("BRIDGE_URL_US not configured — cannot route US order")
    else:
        bridge_url = getattr(
            settings, "BRIDGE_URL",
            "https://bridge-7.flystarcafe7.workers.dev/s2s",
        )
        bridge_secret = getattr(settings, "BRIDGE_SECRET", "")

    origin = getattr(settings, "BASE_URL", "https://pepscheckoutportal.com")

    headers = {
        "Content-Type":    "application/json",
        "Accept":          "application/json",
        "Origin":          origin,
        "Referer":         f"{origin}/?source={source_domain}" if source_domain else origin,
        "X-Bridge-Secret": bridge_secret,
        "X-Requested-With": "XMLHttpRequest",
    }

    logger.info(f"Routing draft {order_id} via {(store_country or 'CA').upper()} bridge → {bridge_url}")
    logger.info(f"Bridge payload: {json.dumps(bridge_payload, indent=2)}")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        resp = await client.post(bridge_url, json=bridge_payload, headers=headers)

    # ── Bridge returned 302 redirect → Shopify draft order invoice URL ─────
    if resp.status_code == 302:
        invoice_url = resp.headers.get("Location", "")
        if not invoice_url:
            raise ShopifyError("Bridge returned 302 with no Location header")
        logger.info(f"✅ Bridge routed draft for {order_id} → Shopify: {invoice_url}")
        return {"type": "shopify", "invoice_url": invoice_url}

    # ── Bridge returned 200 with JSON body ─────────────────────────────────
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            raise ShopifyError(f"Bridge returned 200 with non-JSON body: {resp.text[:300]}")

        # Stripe Elements (custom card form) response
        if data.get("stripe_elements") is True:
            client_secret = data.get("client_secret")
            intent_id     = data.get("intent_id")
            pub_key       = data.get("publishable_key", "")
            if not client_secret:
                raise ShopifyError("Stripe response missing client_secret")
            logger.info(f"✅ Bridge routed draft for {order_id} → Stripe Elements: {intent_id}")
            return {
                "type":            "stripe_elements",
                "client_secret":   client_secret,
                "intent_id":       intent_id,
                "amount":          data.get("amount"),
                "currency":        data.get("currency"),
                "discount_amount": data.get("discount_amount", 0),
                "discount_code":   data.get("discount_code", ""),
                "publishable_key": pub_key,
            }

        # Whop hosted checkout — redirect customer to Whop's hosted page.
        # Worker sends back purchase_url (already includes ?session=ch_xxx for
        # the price override + metadata). Frontend redirects browser there.
        if data.get("whop_hosted") is True:
            purchase_url = data.get("purchase_url")
            if not purchase_url:
                raise ShopifyError("Whop response missing purchase_url")
            session_id = data.get("session_id", "")
            plan_id    = data.get("plan_id", "")
            logger.info(f"✅ Bridge routed draft for {order_id} → Whop hosted: {session_id} → {purchase_url}")
            return {
                "type":            "whop_hosted",
                "purchase_url":    purchase_url,
                "session_id":      session_id,
                "plan_id":         plan_id,
                "amount":          data.get("amount"),
                "currency":        data.get("currency"),
                "discount_amount": data.get("discount_amount", 0),
                "discount_code":   data.get("discount_code", ""),
                "sandbox":         data.get("sandbox", False),
                "whop_account_id": data.get("whop_account_id", "whop_1"),
                "whop_worker_url": data.get("whop_worker_url", ""),
            }

        # Stripe embedded checkout response (legacy, for backwards compat)
        if data.get("stripe_embedded") is True:
            client_secret = data.get("client_secret")
            session_id    = data.get("session_id")
            pub_key       = data.get("publishable_key", "")
            if not client_secret:
                raise ShopifyError("Stripe response missing client_secret")
            logger.info(f"✅ Bridge routed draft for {order_id} → Stripe embedded: {session_id}")
            return {
                "type":            "stripe_embedded",
                "client_secret":   client_secret,
                "session_id":      session_id,
                "publishable_key": pub_key,
            }

        # Shopify JSON response (acceptsJson path)
        invoice_url = data.get("invoice_url")
        if invoice_url:
            logger.info(f"✅ Bridge routed draft for {order_id} → Shopify (JSON): {invoice_url}")
            return {"type": "shopify", "invoice_url": invoice_url}


        # Helcim Pay embedded checkout response
        if data.get("helcim_pay") is True:
            checkout_token = data.get("checkout_token")
            pending_id = data.get("pending_id", "")
            if not checkout_token:
                raise ShopifyError("Helcim response missing checkout_token")
            logger.info(f"✅ Bridge routed draft for {order_id} → Helcim Pay: {checkout_token}")
            return {
                "type":             "helcim_pay",
                "checkout_token":   checkout_token,
                "pending_id":       pending_id,
                "amount":           data.get("amount"),
                "currency":         data.get("currency"),
                "discount_amount":  data.get("discount_amount", 0),
                "discount_code":    data.get("discount_code", ""),
                "helcim_account_id": data.get("helcim_account_id", "helcim_1"),
            }

        # Unknown 200 response
        raise ShopifyError(f"Bridge returned 200 but unknown JSON shape: {json.dumps(data)[:300]}")

    if resp.status_code == 403:
        raise ShopifyError(
            f"Bridge rejected Origin '{origin}'. "
            f"Add it to ALLOWED_ORIGINS on the bridge worker."
        )

    raise ShopifyError(f"Bridge call failed: {resp.status_code} — {resp.text[:300]}")