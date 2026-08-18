import random
import string
import time


def generate_order_id() -> str:
    """
    Generate a short, human-readable order ID.
    Format: ORD-XXXXXXXX  (8 uppercase alphanumeric chars)
    Example: ORD-K3M9P2QA

    Uses timestamp base + random suffix to minimize collisions
    while keeping it short enough for customers to type in
    Interac e-Transfer notes.
    """
    # Last 4 chars of current unix timestamp in base36
    ts = int(time.time())
    ts_part = _to_base36(ts % (36 ** 4)).upper().zfill(4)

    # 4 random uppercase alphanumeric chars
    rand_part = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

    return f"ORD-{ts_part}{rand_part}"


def _to_base36(number: int) -> str:
    chars = string.digits + string.ascii_lowercase
    result = []
    while number:
        number, remainder = divmod(number, 36)
        result.append(chars[remainder])
    return "".join(reversed(result)) or "0"
