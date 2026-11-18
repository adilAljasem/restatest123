"""التوقيع الآمن بين المنصة والوكيل (HMAC-SHA256).

كل طلب يحمل:
  X-Resta-Timestamp: وقت الإرسال بالثواني
  X-Resta-Signature: hex(HMAC_SHA256(secret, "<timestamp>." + body))

نفس الدالة مستخدمة في منصة ريستا (resta_platform/models/signing.py) وفي RestaSH لاحقاً.
"""
import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + (body or b"")
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, body: bytes, now: float | None = None) -> dict:
    ts = str(int(now if now is not None else time.time()))
    return {
        "X-Resta-Timestamp": ts,
        "X-Resta-Signature": sign(secret, ts, body),
        "Content-Type": "application/json",
    }


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes,
           now: float | None = None, max_skew: int = MAX_SKEW_SECONDS) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > max_skew:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
