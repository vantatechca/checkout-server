# Shopify Processor rail — architecture & WP bridge contract

Status: **not live.** `SHOPIFY_PROCESSOR_ENABLED` is unset/false everywhere.
Staging has `SHOPIFY_PROCESSOR_WP_URL` + `SHOPIFY_PROCESSOR_SHARED_SECRET`
already set; prod has nothing.

---

## What this rail is (and isn't)

**Is:** WooCommerce/our DB stays the order of record. The customer is
redirected to **Shopify's own hosted checkout** to enter card details, which
settles through that store's Shopify Payments account. Shopify's order is a
payment artifact — archived, never zeroed out (chargeback evidence).

**Is NOT:** card fields on a WordPress page charged via Shopify Payments.
That is impossible, not merely hard. Verified against Storefront API
`2026-07`: the complete cart mutation set is `cartCreate`,
`cartLinesAdd/Update/Remove`, `cartDiscountCodesUpdate`,
`cartDeliveryAddresses*`, `cartSelectedDeliveryOptionsUpdate`,
`cartNoteUpdate`, `cartGiftCardCodes*`. There is **no payment-submit and no
checkout-complete mutation**. Shopify's own docs: *"Use the checkoutUrl field
to direct buyers to Shopify's web checkout to complete their purchase."*

Multipass does not change this — it is SSO only, is now flagged **legacy**,
and requires Plus. Payments extensions don't either: they let a PSP offer
itself *inside* Shopify checkout (approved Partners + Plus only), which is
the opposite direction from what we'd need.

Contrast with our existing `wpay_2d` rail, where the card form genuinely does
render on WordPress — that works because WPay is a normal PSP exposing a
"charge this token" API. Shopify Payments has no such endpoint for merchants.

---

## Flow

```
1. Customer picks "Shopify Processor" on our checkout page
2. POST /api/checkout/shopifyprocessor        (routes/checkout.py)
     → kill-switch check → _validate_cart() → _create_base_order()
     → our order exists as `pending`, id ORD-XXXXXXXX  ← ORDER OF RECORD
3. Server-to-server POST {WP}/wp-json/spb/v1/checkout
     header: X-SPB-Secret
4. WP calls Storefront API cartCreate, returns checkoutUrl
5. We store payment_ref = "shopify:{cartId}", return redirectUrl
6. Customer redirected to Shopify hosted checkout, pays there
7. Shopify fires orders/paid → POST /webhooks/shopify-paid
     → HMAC verified per-store → parse ref:ORD-XXXX from note_attributes
     → mark paid → finalize_paid_order(create_shopify=False)
8. Shopify's post-purchase redirect returns them to OUR confirmation page
```

Card data never touches our servers or WordPress. WP is a server-side broker
holding the Storefront token; it renders no card form.

---

## WP bridge contract

### Request — we send

`POST {SHOPIFY_PROCESSOR_WP_URL}/wp-json/spb/v1/checkout`
Header: `X-SPB-Secret: <SHOPIFY_PROCESSOR_SHARED_SECRET>`

```json
{
  "external_order_id": "ORD-K3M9P2QA",
  "customer": { "email": "", "first_name": "", "last_name": "", "phone": "" },
  "shipping_address": {
    "address1": "", "address2": "", "city": "",
    "province": "", "postal_code": "", "country": ""
  },
  "items": [
    { "product_id": "", "title": "", "variant": "", "quantity": 1, "price": 0.0 }
  ],
  "subtotal": 0.0,
  "total": 0.0,
  "currency": "CAD",
  "discount_code": "",
  "source_domain": "",
  "store_name": "",
  "cart_attributes": [
    { "key": "_src", "value": "somestore.com | ref:ORD-K3M9P2QA" }
  ]
}
```

### Response — WP must return

```json
{ "checkoutUrl": "https://...", "cartId": "gid://shopify/Cart/..." }
```

We also accept `checkout_url` / `redirectUrl` / `redirect_url`, and
`cart_id`. Non-2xx should carry `message` or `error`; it's surfaced to the
customer and written to `order.payment_notes`.

### ⚠️ The one requirement that must not be missed

**`cart_attributes` MUST be passed into `cartCreate`'s `attributes` input.**

Cart attributes surface on the resulting Shopify order as `note_attributes`,
and that is the *only* link back to our order — the customer pays on
Shopify's domain, so nothing else correlates. `/webhooks/shopify-paid`
already parses `_src` for `ref:(ORD-[A-Z0-9]+)` (routes/webhooks.py).

Without it the webhook silently degrades to matching on
`email + total + pending + card`, which mismatches whenever a customer
retries, orders twice, or two carts share a total.

The `_src` key and `"domain | ref:ORD-..."` format are **not arbitrary** —
they match what the existing bridge-store workers emit, so one handler serves
both paths. Don't rename either side without updating the other.

### Catalog mapping — decide this

Either map our `items` to **real Shopify variants** (supports the "genuine
mirror of the same catalog" posture, and is what makes discounts work
cleanly), or push **custom line items** at our prices (simpler, weaker
mirror). This decision drives the discount work below.

### Totals must match

Shopify recomputes from its own product prices. If its total differs from
`order.total`, the customer is charged something other than what our page
quoted. Our discounts live server-side (`crypto_discount`,
`interac_discount`, …) and must map to real Shopify discount objects via
`cartDiscountCodesUpdate`. Shopify documents cart `cost` as *"subject to
change and changes will be reflected at checkout"* — so verify, don't assume.

---

## Configuration

### This repo's `.env`

```
SHOPIFY_PROCESSOR_ENABLED=true
SHOPIFY_PROCESSOR_WP_URL=https://wp-site.example       # no trailing /
SHOPIFY_PROCESSOR_SHARED_SECRET=<must match WP>

# The Shopify store, for orders/paid HMAC verification.
DEVTEST_CHECKOUT_SHOP=your-store.myshopify.com
DEVTEST_WEBHOOK_SECRET=<from that store's webhook config>
```

All three `SHOPIFY_PROCESSOR_*` must be truthy or `main.py` hides the option
and the endpoint 503s.

### ⚠️ Naming trap

`_verify_shopify_hmac` auto-detects stores by scanning for `*_CHECKOUT_SHOP`
plus a matching `*_WEBHOOK_SECRET`. It must be **`_CHECKOUT_SHOP`**.

Our existing `STORE_1_SHOP` uses `_SHOP` only, so `STORE_1` is invisible to
the scan and its `STORE_1_WEBHOOK_SECRET` is dead config. Name new stores
correctly or every webhook delivery 401s.

### Shopify store setup

1. Custom app → Storefront API token, scope `unauthenticated_write_checkouts`
   (lives on the WP side, never here)
2. Webhook `orders/paid` → `https://<host>/webhooks/shopify-paid`
3. Copy that webhook's signing secret into `<PREFIX>_WEBHOOK_SECRET`
4. Custom checkout domain (optional, hides the hop)
5. Branding matched to the storefront

---

## Dev store testing

A Shopify **dev store** covers everything mechanical: real `cartCreate`, real
`checkoutUrl`, real hosted checkout, real HMAC-signed `orders/paid`, real
`note_attributes`. Test orders via the **Bogus gateway** or the payment
provider's **test mode**.

Two limits, per Shopify's docs:

- **Password page can't be removed** on a dev store. Storefront API calls are
  unaffected (token auth), but the customer-facing redirect hits a password
  prompt. Fine for us; a speed bump for demos.
- **No real transactions, and dev stores can't be converted to production.**
  So a dev store cannot validate Shopify Payments *approval for these
  products*, real rates, settlement, or reserve behaviour — and production
  will be a **separate store** with new token/secret/`_CHECKOUT_SHOP` pair.
  Plan for two sets of config.

---

## Known gaps (as of writing)

1. **`cart_attributes` not yet sent** by `checkout_shopifyprocessor` — so the
   webhook's exact-match path can never fire today.
2. **`/webhooks/shopify-paid` never calls `finalize_paid_order()`** — unlike
   the other six rails. So a Shopify-paid order fires **no affiliate
   webhook** (contradicting the explicit policy in
   `services/order_finalize.py`'s docstring) and **no confirmation email**.
   Fix needs three things together:
   - `create_shopify=False` — the customer paid *on* Shopify, so that order
     already exists; the default `True` would create a duplicate (double
     inventory, two order numbers). This branch is already proven by the
     Shippo mark-paid path.
   - `selectinload(Order.items)` on the lookup — `finalize_paid_order`
     requires eager-loaded items; this handler's query doesn't do it, and
     every other rail adds it.
   - persist `order.shopify_order_id` (currently only `payment_ref`) so
     Shippo fulfillment sync keeps working. Watch
     `_shopify_id_gap_is_meaningful` / `SHOPIFY_ORDER_ID_TRACKING_SINCE` in
     `routes/admin.py` so this rail isn't misread as "Needs Label".
3. **No discount/total parity** — endpoint passes `discount_pct=0.0` while
   forwarding `discount_code`. Needs a server-side guard: if the returned
   cart cost != `order.total`, fail loudly instead of redirecting.
4. **Payment tile only exists in `templates/checkout.html`** — `checkout-ca`,
   `checkout-v2`, `checkout-new-ca` have no `shopify_processor_enabled`
   block, so the rail is invisible on `?v=ca`, `?v=2`, `?v=new-ca`.
5. **`services/order_expiry.py` line ~37 reads `for method in ():`** — an
   empty tuple, so the stale-order sweeper is currently a **no-op for every
   payment method**, not just this rail. Pre-existing bug; abandoned Shopify
   redirects will accumulate as `pending` until it's fixed.

---

## Compliance note

Structure this as **your own store, your own Shopify Payments account, a
genuine catalog mirror** — that's the normal headless pattern.

Routing a third party's orders through someone else's approved SPD account is
payment factoring. The current Shopify Payments ToS appoints Shopify as your
agent for *your* transactions, and grants Shopify security interests and
liens over funds, discretionary **Reserve Accounts** that may hold *"a
certain amount (including the full amount)"* of settlement for a period, and
possible **personal guarantees** from a principal. That's the mechanism
behind terminated-account/funds-held outcomes.

Note also that Shopify's AUP is now **principles-based with no enumerated
category list** (the old prohibited-businesses help page is gone), pushing
product-eligibility judgement onto the merchant: *"in choosing a market to
enter and products to sell, you, the merchant, are making a commitment to
take that market seriously."* So category risk here is an underwriting
question for the real store, not something a published list settles — and not
something a dev store can answer.
