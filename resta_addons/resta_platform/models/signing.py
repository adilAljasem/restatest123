"""نفس توقيع الوكيل (agent/resta_agent/security.py) — لا تغيّره في طرف واحد فقط."""
import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def sign(secret, timestamp, body):
    message = timestamp.encode() + b"." + (body or b"")
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret, body, now=None):
    ts = str(int(now if now is not None else time.time()))
    return {
        "X-Resta-Timestamp": ts,
        "X-Resta-Signature": sign(secret, ts, body),
        "Content-Type": "application/json",
    }


def verify(secret, timestamp, signature, body, now=None, max_skew=MAX_SKEW_SECONDS):
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > max_skew:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)
