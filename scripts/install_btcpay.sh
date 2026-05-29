#!/usr/bin/env bash
# ============================================================
# BTCPay Server — Docker Install Script
# Run after setup_vps.sh
#
# Docs: https://docs.btcpayserver.org/Docker/
# ============================================================
set -euo pipefail

BTCPAY_HOST="btcpay.yourdomain.com"    # ← change this
REVERSEPROXY="nginx"
NBITCOIN_NETWORK="mainnet"             # or "testnet" for testing
LIGHTNING="lnd"                        # or "clightning", or "" to disable Lightning

# ─── Install Docker ──────────────────────────────────────────
echo "Installing Docker..."
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker
echo "✅ Docker installed."

# ─── Install BTCPay ──────────────────────────────────────────
echo "Installing BTCPay Server..."
mkdir -p /opt/btcpayserver
cd /opt/btcpayserver

export BTCPAY_HOST="$BTCPAY_HOST"
export NBITCOIN_NETWORK="$NBITCOIN_NETWORK"
export BTCPAYGEN_CRYPTO1="btc"
export BTCPAYGEN_REVERSEPROXY="$REVERSEPROXY"
export BTCPAYGEN_LIGHTNING="$LIGHTNING"
export BTCPAYGEN_ADDITIONAL_FRAGMENTS="opt-save-storage"
export BTCPAY_ENABLE_SSH=true

. <(curl -sL https://raw.githubusercontent.com/btcpayserver/btcpayserver-docker/master/btcpay-setup.sh)

echo ""
echo "✅ BTCPay Server installed!"
echo ""
echo "Next steps:"
echo "  1. Open https://$BTCPAY_HOST and create admin account"
echo "  2. Create a Store and get the Store ID"
echo "  3. Create an API key (Account → API Keys) with permissions:"
echo "     - btcpay.store.canviewinvoices"
echo "     - btcpay.store.cancreateinvoice"
echo "     - btcpay.store.webhooks.canmodifywebhooks"
echo "  4. Install Boltz plugin: Server Settings → Plugins → Boltz"
echo "  5. Add to your .env:"
echo "     BTCPAY_URL=https://$BTCPAY_HOST"
echo "     BTCPAY_STORE_ID=<your store id>"
echo "     BTCPAY_API_KEY=<your api key>"
echo "  6. Set webhook in BTCPay:"
echo "     Store → Settings → Webhooks → Add Webhook"
echo "     URL: https://checkout.yourdomain.com/webhooks/btcpay"
echo "     Events: InvoiceSettled, InvoiceExpired, InvoiceInvalid"
echo "     Copy the secret → BTCPAY_WEBHOOK_SECRET in .env"
