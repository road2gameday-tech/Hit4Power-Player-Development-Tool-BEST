import os
import re
import hmac
import hashlib
import random
import string
from typing import Optional

# -------- Flash helpers (session-based) --------
def set_flash(request, message: str):
    """Store a single flash message in the session."""
    request.session["flash"] = message

def pop_flash(request):
    """Retrieve & remove the flash message from the session."""
    return request.session.pop("flash", None)


# -------- Code generation & normalization --------

def normalize_code(s: str) -> str:
    """
    Uppercase and strip non-alphanumerics so lookups are consistent.
    Example: ' P-Qd5 tiv ' -> 'PQD5TIV'
    """
    if s is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.strip().upper())


def generate_code(prefix: str = "", length: int = 6, pretty: bool = True) -> str:
    """
    Generate a random alphanumeric code, optionally with a prefix.
    If pretty=True and prefix is a single letter, add a hyphen like 'P-XXXXXX'.
    """
    core = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
    if pretty and prefix and len(prefix) == 1:
        return f"{prefix}-{core}"
    return f"{prefix}{core}"


# -------- Optional hashing helpers (for storing codes securely) --------

def _secret_salt() -> bytes:
    # Provide a stable app secret in env (e.g., RENDER env var)
    return os.getenv("CODE_HASH_SALT", "hit4power-default-salt").encode("utf-8")


def hash_code(raw_code: str) -> str:
    """
    Hash a code with HMAC-SHA256.
    Store this in the DB instead of the raw code if you want to avoid plaintext.
    """
    norm = normalize_code(raw_code)
    digest = hmac.new(_secret_salt(), norm.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def verify_code(stored_hash: str, provided_code: str) -> bool:
    """
    Constant-time compare between stored hash and the HMAC of the provided code.
    """
    calc = hash_code(provided_code)
    return hmac.compare_digest(stored_hash or "", calc)


# -------- Age buckets & math helpers --------

def age_bucket(age: int) -> str:
    try:
        a = int(age)
    except Exception:
        return "Unknown"
    if 7 <= a <= 9:
        return "7-9"
    if 10 <= a <= 12:
        return "10-12"
    if 13 <= a <= 15:
        return "13-15"
    if 16 <= a <= 18:
        return "16-18"
    if a >= 19:
        return "18+"
    return "Unknown"


def percent_delta(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    """
    Returns percent difference vs reference (e.g., 0.10 == +10%).
    None if reference is 0/None or value is None.
    """
    if value is None or reference in (None, 0):
        return None
    return (value - reference) / reference
