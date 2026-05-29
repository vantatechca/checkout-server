/**
 * How to embed the checkout from a Shopify store (or any source site).
 *
 * The checkout page reads cart items from the URL query param `?items=<base64>`.
 * Encode your cart items as JSON, base64 it, and redirect/link to the checkout URL.
 *
 * Item format:
 *   [{ id, name, variant, quantity, unitPrice, image }]
 *
 * ─── Option 1: Liquid snippet in Shopify theme ─────────────────────────────
 * Add to your theme's cart.liquid or cart-drawer.liquid:
 *
 *   <script>
 *     window.CHECKOUT_URL = "https://checkout.yourstore.com";
 *   </script>
 *   <button onclick="redirectToCheckout()">Proceed to Checkout</button>
 *
 * Then use this script:
 */

function redirectToCheckout() {
  // Read Shopify cart via AJAX
  fetch('/cart.js')
    .then(r => r.json())
    .then(cart => {
      const items = cart.items.map(item => ({
        id:        String(item.variant_id),
        name:      item.product_title,
        variant:   item.variant_title !== 'Default Title' ? item.variant_title : null,
        quantity:  item.quantity,
        unitPrice: item.price / 100,   // Shopify prices are in cents
        image:     item.image || null,
      }));

      const encoded = btoa(JSON.stringify(items));
      const url = `${window.CHECKOUT_URL}?items=${encoded}`;
      window.location.href = url;
    })
    .catch(err => {
      console.error('Could not load cart:', err);
      alert('There was an error loading your cart. Please try again.');
    });
}


/**
 * ─── Option 2: Direct link with hardcoded items ────────────────────────────
 * Useful for testing or simple product pages.
 */
function buildCheckoutLink(checkoutBaseUrl, items) {
  const encoded = btoa(JSON.stringify(items));
  return `${checkoutBaseUrl}?items=${encoded}`;
}

// Example:
const link = buildCheckoutLink('https://checkout.store1.com', [
  { id: 'var_123', name: 'BPC-157 - 5mg', variant: null, quantity: 2, unitPrice: 39.00, image: null },
  { id: 'var_free', name: 'Reconstitution Solution', variant: null, quantity: 1, unitPrice: 0.00, image: null },
]);
console.log('Checkout link:', link);


/**
 * ─── Option 3: Shopify Buy Button / Custom redirect ──────────────────────────
 * You can override Shopify's checkout URL using theme.liquid by intercepting
 * the checkout form submit:
 *
 *   document.querySelector('form[action="/cart"]')?.addEventListener('submit', function(e) {
 *     e.preventDefault();
 *     redirectToCheckout();
 *   });
 */
