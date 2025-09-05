import time
import re
from datetime import datetime, timezone
import hmac
from typing import Optional


# ----------------------------
# Helpers
# ----------------------------
def now_ts() -> float:
    return time.time()


def to_iso(ts: float | None) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def is_valid_email(email: Optional[str]) -> bool:
    if not email:
        return False
    email = email.strip()
    # simple but effective email check
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def ct_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
