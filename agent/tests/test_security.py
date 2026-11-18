from resta_agent.security import sign, signed_headers, verify

SECRET = "x" * 40


def test_roundtrip():
    body = b'{"op":"suspend"}'
    h = signed_headers(SECRET, body, now=1_700_000_000)
    assert verify(SECRET, h["X-Resta-Timestamp"], h["X-Resta-Signature"], body, now=1_700_000_010)


def test_tampered_body_rejected():
    h = signed_headers(SECRET, b"a", now=1_700_000_000)
    assert not verify(SECRET, h["X-Resta-Timestamp"], h["X-Resta-Signature"], b"b", now=1_700_000_000)


def test_wrong_secret_rejected():
    h = signed_headers(SECRET, b"a", now=1_700_000_000)
    assert not verify("y" * 40, h["X-Resta-Timestamp"], h["X-Resta-Signature"], b"a", now=1_700_000_000)


def test_old_timestamp_rejected():
    h = signed_headers(SECRET, b"a", now=1_700_000_000)
    assert not verify(SECRET, h["X-Resta-Timestamp"], h["X-Resta-Signature"], b"a", now=1_700_001_000)


def test_missing_values_rejected():
    assert not verify(SECRET, None, None, b"a")
    assert not verify(SECRET, "abc", sign(SECRET, "abc", b"a"), b"a")


def test_known_vector():
    # نفس القيمة لازم تطلع في منصة أودو (اختبار موديول resta_platform)
    assert sign("k" * 32, "1700000000", b"{}") == "0b3b35525c10b3757ffa78df69037021fc3168d8390a51a4fcb4a8d56a25b557"
