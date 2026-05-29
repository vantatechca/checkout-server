"""
Interac e-Transfer matching — MANUAL MODE

Automatic Gmail polling is disabled for now.
Payments are matched manually via the admin panel:

  GET  /admin/interac/unmatched  -> list unmatched payments
  POST /admin/interac/match      -> manually link payment to order

How the manual flow works:
  1. Customer places order -> sees reference code (ORD-XXXXXXXX)
  2. Customer sends Interac e-Transfer to CheckoutCaster@proton.me
     with the reference code in the message field
  3. Admin receives email notification from Interac
  4. Admin calls POST /admin/orders/{id}/mark-paid
  5. Order status updates to paid

To enable automatic Gmail matching later:
  - Set INTERAC_AUTO_MATCH=true in .env
  - Run: python services/interac_watcher.py --setup
  - Uncomment the Celery beat schedule in tasks/celery_app.py
"""

INTERAC_AUTO_MATCH_ENABLED = False


def poll_interac_emails() -> list:
    """Gmail polling disabled in manual mode. Returns empty list."""
    if not INTERAC_AUTO_MATCH_ENABLED:
        return []
    return []
