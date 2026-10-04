"""Unit tests for the KXHIGHNY live L2 recorder's core logic: request
signing (data/kalshi_auth.py) and order-book reconstruction/gap detection
(data/kalshi_orderbook.py). No network calls, no real credentials, and no
order-placement code exists anywhere in this project to test against --
these tests only exercise pure functions. Follows the same ad-hoc check()
pattern as scripts/test_integration.py.
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from data.kalshi_auth import sign_text, build_auth_headers, build_ws_auth_headers, build_rest_auth_headers, WS_PATH, REST_SIGNED_PATH_PREFIX
from data.kalshi_orderbook import OrderBook, OrderBookConsistencyError, SequenceTracker

results = {"passed": [], "failed": []}


def check(name, cond, detail=""):
    if cond:
        results["passed"].append(name)
        print(f"PASS: {name}")
    else:
        results["failed"].append(f"{name} -- {detail}")
        print(f"FAIL: {name} -- {detail}")


# ---- signing: RSA-PSS round-trip against a throwaway test keypair (never the real key) ----
test_rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
sig_b64 = sign_text(test_rsa_key, "1234567890000GET/trade-api/ws/v2")
check("RSA sign_text returns a base64 string", isinstance(sig_b64, str) and len(sig_b64) > 0)

import base64
sig_bytes = base64.b64decode(sig_b64)
try:
    test_rsa_key.public_key().verify(
        sig_bytes, b"1234567890000GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    check("RSA-PSS signature verifies against the public key", True)
except Exception as e:
    check("RSA-PSS signature verifies against the public key", False, str(e))

# ---- signing: Ed25519 round-trip ----
test_ed_key = Ed25519PrivateKey.generate()
sig_ed_b64 = sign_text(test_ed_key, "hello")
try:
    test_ed_key.public_key().verify(base64.b64decode(sig_ed_b64), b"hello")
    check("Ed25519 signature verifies against the public key", True)
except Exception as e:
    check("Ed25519 signature verifies against the public key", False, str(e))

# ---- header construction ----
headers = build_auth_headers("KEY123", test_rsa_key, "GET", "/trade-api/v2/portfolio/orders?limit=5")
check("auth headers contain all 3 required names", set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"})
check("KALSHI-ACCESS-KEY matches the given key id", headers["KALSHI-ACCESS-KEY"] == "KEY123")
check("timestamp header is purely numeric (ms)", headers["KALSHI-ACCESS-TIMESTAMP"].isdigit())

# query string must be stripped before signing -- verify by re-deriving the signed message and checking it verifies
msg = headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/orders"
try:
    test_rsa_key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
    )
    check("query string is stripped from the signed path", True)
except Exception as e:
    check("query string is stripped from the signed path", False, str(e))

rest_headers = build_rest_auth_headers("KEY123", test_rsa_key, "GET", "/portfolio/balance")
rest_msg = rest_headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + REST_SIGNED_PATH_PREFIX + "/portfolio/balance"
try:
    test_rsa_key.public_key().verify(
        base64.b64decode(rest_headers["KALSHI-ACCESS-SIGNATURE"]), rest_msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
    )
    check("build_rest_auth_headers signs the full /trade-api/v2-prefixed path", True)
except Exception as e:
    check("build_rest_auth_headers signs the full /trade-api/v2-prefixed path", False, str(e))

ws_headers = build_ws_auth_headers("KEY123", test_rsa_key)
ws_msg = ws_headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + WS_PATH
try:
    test_rsa_key.public_key().verify(
        base64.b64decode(ws_headers["KALSHI-ACCESS-SIGNATURE"]), ws_msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
    )
    check("WS auth header signs the literal /trade-api/ws/v2 path", True)
except Exception as e:
    check("WS auth header signs the literal /trade-api/ws/v2 path", False, str(e))

# ---- order book: snapshot + deltas = reconstructed book (per-market; seq lives in SequenceTracker, not here) ----
book = OrderBook(market_ticker="KXHIGHNY-TEST-B50")
book.apply_snapshot(
    yes_dollars_fp=[["0.0800", "300.00"], ["0.2200", "333.00"]],
    no_dollars_fp=[["0.5400", "20.00"], ["0.5600", "146.00"]],
)
check("snapshot populates yes levels exactly", book.yes_levels == {Decimal("0.0800"): Decimal("300.00"), Decimal("0.2200"): Decimal("333.00")})
check("snapshot populates no levels exactly", book.no_levels == {Decimal("0.5400"): Decimal("20.00"), Decimal("0.5600"): Decimal("146.00")})
check("snapshot sets has_snapshot/ever_had_snapshot", book.has_snapshot and book.ever_had_snapshot)

book.apply_delta(side="yes", price_dollars="0.0800", delta_fp="50.00")
check("positive delta increases level size", book.yes_levels[Decimal("0.0800")] == Decimal("350.00"))

book.apply_delta(side="yes", price_dollars="0.2200", delta_fp="-333.00")
check("delta to exactly zero removes the level", Decimal("0.2200") not in book.yes_levels)

book.apply_delta(side="no", price_dollars="0.9900", delta_fp="10.00")
check("delta at a new price level creates it", book.no_levels[Decimal("0.9900")] == Decimal("10.00"))
check("n_deltas_applied tracks applied deltas", book.n_deltas_applied == 3)

# delta before any snapshot raises
book_nosnap = OrderBook(market_ticker="KXHIGHNY-TEST-B51")
try:
    book_nosnap.apply_delta(side="yes", price_dollars="0.50", delta_fp="1.00")
    check("delta before any snapshot raises (book cannot be trusted)", False, "did not raise")
except OrderBookConsistencyError as e:
    check("delta before any snapshot raises (book cannot be trusted)", "before any snapshot" in str(e))

# invalidate() forces has_snapshot False (so a stray delta during an unresolved gap raises) while
# ever_had_snapshot stays True (distinguishes "never initialized" from "temporarily invalidated")
book_nosnap.apply_snapshot(yes_dollars_fp=[["0.50", "10.00"]], no_dollars_fp=[])
book_nosnap.invalidate()
check("invalidate() clears has_snapshot", not book_nosnap.has_snapshot)
check("invalidate() does not clear ever_had_snapshot", book_nosnap.ever_had_snapshot)
try:
    book_nosnap.apply_delta(side="yes", price_dollars="0.50", delta_fp="1.00")
    check("delta after invalidate() raises until a fresh snapshot arrives", False, "did not raise")
except OrderBookConsistencyError:
    check("delta after invalidate() raises until a fresh snapshot arrives", True)
seg_before = book_nosnap.segment_id
book_nosnap.apply_snapshot(yes_dollars_fp=[["0.50", "20.00"]], no_dollars_fp=[])
check("a fresh snapshot after invalidate() increments segment_id (new reconstruction segment)", book_nosnap.segment_id == seg_before + 1)
check("a fresh snapshot resets n_deltas_applied", book_nosnap.n_deltas_applied == 0)

# negative-size consistency violation
book3 = OrderBook(market_ticker="KXHIGHNY-TEST-B52")
book3.apply_snapshot(yes_dollars_fp=[["0.50", "5.00"]], no_dollars_fp=[])
try:
    book3.apply_delta(side="yes", price_dollars="0.50", delta_fp="-10.00")  # would go to -5.00
    check("delta driving a level negative raises OrderBookConsistencyError", False, "did not raise")
except OrderBookConsistencyError as e:
    check("delta driving a level negative raises OrderBookConsistencyError", "negative" in str(e))

# ---- SequenceTracker: gap detection is scoped to one subscription (sid), shared across markets ----
tracker = SequenceTracker(sid_label="orderbook_delta:1")
check("first-ever seq is classified FIRST, not an anomaly", tracker.check(1) == "FIRST")
tracker.advance(1)
check("the immediate next seq has no anomaly", tracker.check(2) is None)
tracker.advance(2)
check("a jump ahead is classified GAP", tracker.check(5) == "GAP")
check("a duplicate/old seq is classified DUPLICATE_OR_OLD", tracker.check(2) == "DUPLICATE_OR_OLD")
check("an older seq is also DUPLICATE_OR_OLD", tracker.check(1) == "DUPLICATE_OR_OLD")
tracker.advance(5)  # caller advances past the gap regardless, same as the real recorder does
check("tracker now expects seq 6 after advancing past a gap", tracker.check(6) is None)
tracker.reset()
check("reset() clears last_seq so the next message is FIRST again", tracker.check(1) == "FIRST")

print(f"\n{len(results['passed'])} passed, {len(results['failed'])} failed")
if results["failed"]:
    print("FAILURES:")
    for f in results["failed"]:
        print(" -", f)
    sys.exit(1)
