from pathlib import Path
import re

path = Path("templates/checkout.html")
text = path.read_text(encoding="utf-8")

# Safety: don't run twice
if 'value="shopifyprocessor"' in text:
    raise SystemExit(
        "ERROR: shopifyprocessor already exists in checkout.html. "
        "No changes made."
    )

# ============================================================
# CHANGE 1
# Add Shopify Processor before the onramp_wp payment option
# ============================================================

payment_anchor = """        {% if onramp_wp_enabled %}"""

payment_block = """        <!-- SHOPIFY PROCESSOR -->
        <label class="pay-method" id="pm-shopifyprocessor">
          <input type="radio" name="paymentMethod" value="shopifyprocessor" onchange="selectMethod('shopifyprocessor')"/>
          <div class="pay-info">
            <div class="pay-header">
              <span class="pay-name">Shopify Processor</span>
              <div class="pay-icons">
                <svg width="38" height="24" viewBox="0 0 38 24" xmlns="http://www.w3.org/2000/svg">
                  <rect width="38" height="24" rx="4" fill="#1A1F71"/>
                  <text x="19" y="17" font-family="Arial,sans-serif" font-size="11" font-weight="900" font-style="italic" fill="white" text-anchor="middle" letter-spacing="1">VISA</text>
                </svg>
                <svg width="38" height="24" viewBox="0 0 38 24" xmlns="http://www.w3.org/2000/svg">
                  <rect width="38" height="24" rx="4" fill="#252525"/>
                  <circle cx="14" cy="12" r="7" fill="#EB001B"/>
                  <circle cx="24" cy="12" r="7" fill="#F79E1B"/>
                  <path d="M19 6.5a7 7 0 0 1 0 11 7 7 0 0 1 0-11z" fill="#FF5F00"/>
                </svg>
              </div>
            </div>

            <div class="pay-desc">
              Visa, Mastercard · Secure payment via Shopify
            </div>

            <div class="pay-body">
              <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:1.4rem;font-size:1.3rem;color:#0c4a6e;line-height:1.55;">
                🔒 <strong>Pay securely with Shopify.</strong>
                After placing your order, you'll be redirected to Shopify's secure checkout to complete your card payment.
              </div>
            </div>
          </div>
        </label>

"""

if payment_anchor not in text:
    raise SystemExit(
        "ERROR: onramp_wp payment anchor not found. No changes made."
    )

text = text.replace(
    payment_anchor,
    payment_block + payment_anchor,
    1
)

# ============================================================
# CHANGE 2
# Route Shopify Processor from handleSubmit()
# ============================================================

endpoint_anchor = """    var endpoint = '/api/checkout/' + currentMethod;"""

submit_block = """

    // === SHOPIFY PROCESSOR ===
    if (currentMethod === 'shopifyprocessor') {
      handleShopifyProcessorPayment(payload, btn);
      return;
    }"""

if endpoint_anchor not in text:
    raise SystemExit(
        "ERROR: checkout endpoint line not found. No changes made."
    )

text = text.replace(
    endpoint_anchor,
    endpoint_anchor + submit_block,
    1
)

# ============================================================
# CHANGE 3
# Add the Shopify Processor HTTP handler immediately
# before the PYMTZ popup provider section
# ============================================================

match = re.search(
    r'(?m)^[ \t]*//[^\r\n]*PROVIDER:\s*PYMTZ POPUP[^\r\n]*$',
    text
)

if not match:
    raise SystemExit(
        "ERROR: PYMTZ provider section not found. No changes made."
    )

shopify_handler = """  // ─── SHOPIFY PROCESSOR ───────────────────────────────────────────────
  function handleShopifyProcessorPayment(payload, btn) {
    window._lastCheckoutPayload = payload;

    fetch('/api/checkout/shopifyprocessor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    .then(function(r) {
      var ct = r.headers.get('content-type') || '';

      if (!ct.includes('application/json')) {
        return {
          ok: false,
          data: {
            error: 'Server error (' + r.status + '). Please try again shortly.'
          }
        };
      }

      return r.json().then(function(d) {
        return {
          ok: r.ok,
          data: d
        };
      });
    })
    .then(function(res) {
      btn.classList.remove('loading');
      btn.disabled = false;

      if (!res.ok) {
        showNotif(
          'error',
          res.data.detail ||
          res.data.error ||
          'Could not initialize Shopify payment.'
        );
        return;
      }

      if (!res.data.redirectUrl) {
        showNotif(
          'error',
          'Shopify checkout URL was not returned.'
        );
        return;
      }

      clearReservedOrderId();
      handleShopifyRedirect(res.data);
    })
    .catch(function(err) {
      btn.classList.remove('loading');
      btn.disabled = false;

      console.error(
        'Shopify Processor payment error:',
        err
      );

      showNotif(
        'error',
        'Network error. Please try again.'
      );
    });
  }

"""

text = (
    text[:match.start()]
    + shopify_handler
    + text[match.start():]
)

# ============================================================
# WRITE ONLY AFTER EVERY CHECK PASSES
# ============================================================

path.write_text(
    text,
    encoding="utf-8",
    newline="\n"
)

print("DONE: Shopify Processor changes applied cleanly.")