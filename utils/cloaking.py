"""
utils/cloaking.py

Title cloaking for cart items before they leave our server.

Two independent cloaking systems:
  - cloak_items_lasso()   → Lasso-specific mapping (peptide → decoy product)
  - cloak_items()         → Universal fallback (single decoy for everything else)

Real product names are always preserved in our DB (OrderItem.title).
Only titles sent to external processors are masked.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any


# ─── Lasso cloak map ──────────────────────────────────────────────────────────
# Maps peptide product titles (lowercase, partial match) → Lasso decoy title.
# Add every peptide/supplement variant you carry.
# Keys are matched case-insensitively against the real product title.
# First match wins — order from most specific to least specific.

LASSO_CLOAK_MAP: dict[str, str] = {
    # # BPC-157
    # "bpc-157":          "Women's Running Shoe - Black",
    # "bpc 157":          "Women's Running Shoe - Black",
    # "bpc157":           "Women's Running Shoe - Black",

    # # TB-500
    # "tb-500":           "Women's Running Shoe - White",
    # "tb 500":           "Women's Running Shoe - White",
    # "tb500":            "Women's Running Shoe - White",
    # "thymosin beta":    "Women's Running Shoe - White",

    # # CJC-1295
    # "cjc-1295":         "Men's Casual Sneaker - Grey",
    # "cjc 1295":         "Men's Casual Sneaker - Grey",
    # "cjc1295":          "Men's Casual Sneaker - Grey",

    # # Ipamorelin
    # "ipamorelin":       "Men's Casual Sneaker - Navy",

    # # GHRP-6
    # "ghrp-6":           "Unisex Slide Sandal - Beige",
    # "ghrp 6":           "Unisex Slide Sandal - Beige",
    # "ghrp6":            "Unisex Slide Sandal - Beige",

    # # GHRP-2
    # "ghrp-2":           "Unisex Slide Sandal - Black",
    # "ghrp 2":           "Unisex Slide Sandal - Black",
    # "ghrp2":            "Unisex Slide Sandal - Black",

    # # Hexarelin
    # "hexarelin":        "Women's Slip-On Loafer - Tan",

    # # Sermorelin
    # "sermorelin":       "Women's Slip-On Loafer - Brown",

    # # AOD-9604
    # "aod-9604":         "Men's Sport Sandal - Olive",
    # "aod 9604":         "Men's Sport Sandal - Olive",
    # "aod9604":          "Men's Sport Sandal - Olive",

    # # Melanotan
    # "melanotan ii":     "Women's Ballet Flat - Nude",
    # "melanotan 2":      "Women's Ballet Flat - Nude",
    # "melanotan":        "Women's Ballet Flat - Nude",
    # "mt-2":             "Women's Ballet Flat - Nude",
    # "mt2":              "Women's Ballet Flat - Nude",

    # # PT-141
    # "pt-141":           "Women's Wedge Sandal - Coral",
    # "pt 141":           "Women's Wedge Sandal - Coral",
    # "pt141":            "Women's Wedge Sandal - Coral",
    # "bremelanotide":    "Women's Wedge Sandal - Coral",

    # # Semax
    # "semax":            "Men's Oxford Shoe - Cognac",

    # # Selank
    # "selank":           "Men's Oxford Shoe - Black",

    # # Epithalon
    # "epithalon":        "Women's Ankle Boot - Camel",
    # "epitalon":         "Women's Ankle Boot - Camel",

    # # GHK-Cu
    # "ghk-cu":           "Women's Ankle Boot - Dark Brown",
    # "ghk cu":           "Women's Ankle Boot - Dark Brown",
    # "copper peptide":   "Women's Ankle Boot - Dark Brown",

    # # Kisspeptin
    # "kisspeptin":       "Men's Chelsea Boot - Chestnut",

    # # LGD-4033
    # "lgd-4033":         "Men's Trail Runner - Blue",
    # "lgd 4033":         "Men's Trail Runner - Blue",
    # "lgd4033":          "Men's Trail Runner - Blue",
    # "ligandrol":        "Men's Trail Runner - Blue",

    # # RAD-140
    # "rad-140":          "Men's Trail Runner - Red",
    # "rad 140":          "Men's Trail Runner - Red",
    # "rad140":           "Men's Trail Runner - Red",
    # "testolone":        "Men's Trail Runner - Red",

    # # MK-677
    # "mk-677":           "Men's Trail Runner - Green",
    # "mk 677":           "Men's Trail Runner - Green",
    # "mk677":            "Men's Trail Runner - Green",
    # "ibutamoren":       "Men's Trail Runner - Green",

    # # Cardarine
    # "cardarine":        "Women's High-Top Sneaker - Pink",
    # "gw-501516":        "Women's High-Top Sneaker - Pink",
    # "gw 501516":        "Women's High-Top Sneaker - Pink",

    # # Ostarine
    # "ostarine":         "Women's High-Top Sneaker - Purple",
    # "mk-2866":          "Women's High-Top Sneaker - Purple",
    # "mk 2866":          "Women's High-Top Sneaker - Purple",

    # # NAD+
    # "nad+":             "Unisex Canvas Shoe - White",
    # "nad ":             "Unisex Canvas Shoe - White",
    # "nicotinamide":     "Unisex Canvas Shoe - White",

    # # Semaglutide
    # "semaglutide":      "Women's Platform Sneaker - Sage",
    # "ozempic":          "Women's Platform Sneaker - Sage",
    # "wegovy":           "Women's Platform Sneaker - Sage",

    # # Tirzepatide
    # "tirzepatide":      "Women's Platform Sneaker - Mauve",
    # "mounjaro":         "Women's Platform Sneaker - Mauve",

    # # Generic fallback (catches unlisted peptides/supplements)
    # "peptide":          "New Women's Valentine '26 Shoes",
    # "research":         "New Women's Valentine '26 Shoes",
}

# Fallback decoy when no key matches at all
LASSO_DEFAULT_DECOY = "New Women's Valentine '26 Shoes"

# ─── Universal fallback decoy (non-Lasso use) ─────────────────────────────────
UNIVERSAL_DECOY_TITLE = "New Women's Valentine '26 Shoes"


# ─── Cloaked item ─────────────────────────────────────────────────────────────

@dataclass
class CloakedItem:
    """Processor-safe cart line. Title is masked; price/qty are real."""
    product_id:     str | None
    title:          str         # masked title
    variant:        str | None
    qty:            int
    price:          float       # real charge amount — only title is swapped
    original_title: str         # preserved internally for fulfillment / DB


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_fields(item: Any) -> tuple[str, str | None, str | None, int, float]:
    """Unpack a CartItem pydantic model or plain dict into primitives."""
    if isinstance(item, dict):
        return (
            item.get("title", ""),
            item.get("product_id"),
            item.get("variant"),
            int(item.get("qty", 1)),
            float(item.get("price", 0)),
        )
    return (
        getattr(item, "title", ""),
        getattr(item, "product_id", None),
        getattr(item, "variant", None),
        int(getattr(item, "qty", 1)),
        float(getattr(item, "price", 0)),
    )


def _lasso_cloak_title(title: str) -> str:
    """
    Look up the Lasso-specific decoy for a given product title.
    Matches case-insensitively. Returns LASSO_DEFAULT_DECOY if no match.
    """
    lower = title.lower().strip()
    for key, decoy in LASSO_CLOAK_MAP.items():
        if key in lower:
            return decoy
    return LASSO_DEFAULT_DECOY


# ─── Public API ───────────────────────────────────────────────────────────────

def cloak_items_lasso(items: list[Any]) -> list[CloakedItem]:
    """
    Lasso-specific cloaking. Each peptide title is mapped to its own
    dedicated decoy product from LASSO_CLOAK_MAP. Different peptides
    → different decoy titles (avoids every order looking identical).
    """
    cloaked = []
    for item in items:
        title, product_id, variant, qty, price = _extract_fields(item)
        masked = _lasso_cloak_title(title)
        cloaked.append(CloakedItem(
            product_id     = product_id,
            title          = masked,
            variant        = variant,
            qty            = qty,
            price          = price,
            original_title = title,
        ))
    return cloaked


def cloak_items(items: list[Any]) -> list[CloakedItem]:
    """
    Universal fallback cloaking — every item becomes the same decoy.
    Used by non-Lasso flows if needed.
    """
    cloaked = []
    for item in items:
        title, product_id, variant, qty, price = _extract_fields(item)
        cloaked.append(CloakedItem(
            product_id     = product_id,
            title          = UNIVERSAL_DECOY_TITLE,
            variant        = variant,
            qty            = qty,
            price          = price,
            original_title = title,
        ))
    return cloaked


def build_lasso_cart(cloaked_items: list[CloakedItem]) -> list[dict]:
    """
    Converts cloaked items into the Shopify cart item format Lasso expects.
    Lasso was built to consume raw Shopify cart payloads — field names must match.
    Price fields are in cents (Shopify standard).
    """
    cart = []
    for item in cloaked_items:
        price_cents = round(item.price * 100)
        line_price  = price_cents * item.qty

        entry: dict = {
            "title":          item.title,           # cloaked title
            "variant_title":  item.variant or "",
            "quantity":       item.qty,
            "price":          price_cents,           # unit price in cents
            "line_price":     line_price,            # total for this line
            "original_price": price_cents,
            "discounted_price": price_cents,
            "total_discount": 0,
        }

        # Include IDs if available — Lasso uses these for inventory lookup
        if item.product_id:
            try:
                entry["product_id"] = int(item.product_id)
                entry["variant_id"] = int(item.product_id)
            except (ValueError, TypeError):
                entry["product_id"] = item.product_id
                entry["variant_id"] = item.product_id

        cart.append(entry)

    return cart