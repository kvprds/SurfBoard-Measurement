import json, hmac, hashlib, time, asyncio
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
import fakes
from fastapi.testclient import TestClient
import db as dbx, payments
from app import app

SCHEMA = open(os.path.join(os.path.dirname(_HERE), "schema.sql")).read()
WHSEC = "whsec_test"
PASS = FAIL = 0
def check(l, c, x=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  ok   {l}")
    else: FAIL += 1; print(f"  FAIL {l} {x}")

env = fakes.FakeEnv(SCHEMA)
c = TestClient(fakes.with_env(app, env), base_url="https://test.local")
loop = asyncio.new_event_loop()
D = dbx.Database(env.DB)
EMAIL = "buyer@example.com"

def event(evt_id, bundle="coach", email=EMAIL, paid="paid", amount=3500, etype="checkout.session.completed"):
    return json.dumps({
        "id": evt_id, "type": etype,
        "data": {"object": {
            "id": f"cs_{evt_id}", "payment_status": paid, "amount_total": amount,
            "currency": "usd", "payment_intent": f"pi_{evt_id}",
            "client_reference_id": email, "metadata": {"bundle": bundle, "email": email}}}})

def post(body, secret=WHSEC, ts=None):
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return c.post("/webhooks/stripe", content=body,
                  headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})

def bundles(col="coach_bundles"):
    return loop.run_until_complete(dbx.get_inventory(D, EMAIL))[col]

print("\n=== webhook authentication ===")
r = c.post("/webhooks/stripe", content=event("evt_x"), headers={"Content-Type": "application/json"})
check("unsigned webhook rejected (400)", r.status_code == 400, r.status_code)
check("  ...and granted nothing", bundles() == 0)

r = post(event("evt_x"), secret="whsec_attacker")
check("forged signature rejected (400)", r.status_code == 400, r.status_code)
check("  ...and granted nothing", bundles() == 0)

r = post(event("evt_x"), ts=int(time.time()) - 7200)
check("replayed old signature rejected", r.status_code == 400, r.status_code)

print("\n=== a real payment ===")
r = post(event("evt_1"))
check("valid webhook accepted (200)", r.status_code == 200, r.text[:150])
check("  ...credited exactly 1 coach bundle", bundles() == 1, bundles())
check("  ...status reports the credit", "credited 1 coach" in r.json()["status"], r.json())

print("\n=== idempotency: Stripe retries the same event ===")
for i in range(4):
    r = post(event("evt_1"))
check("duplicate deliveries still return 200", r.status_code == 200)
check("  ...duplicate is reported as such", "duplicate" in r.json()["status"], r.json())
check("  ...balance is STILL 1, not 5", bundles() == 1, bundles())
rows = loop.run_until_complete(D.query("SELECT * FROM purchase"))
check("  ...exactly one purchase row recorded", len(rows) == 1, len(rows))

print("\n=== unpaid / irrelevant events ===")
r = post(event("evt_2", paid="unpaid"))
check("unpaid session grants nothing", bundles() == 1 and r.status_code == 200, r.json())
r = post(event("evt_3", etype="payment_intent.created"))
check("unrelated event type acknowledged, grants nothing", bundles() == 1, r.json())
r = post(event("evt_4", bundle="free_stuff"))
check("unknown bundle key grants nothing", bundles() == 1, r.json())

print("\n=== a second, genuine purchase ===")
r = post(event("evt_5", bundle="ai", amount=1000))
check("different event credits again", bundles("ai_bundles") == 2, bundles("ai_bundles"))
rows = loop.run_until_complete(D.query("SELECT bundle, amount_total_cents FROM purchase ORDER BY id"))
check("ledger has both sales", len(rows) == 2, rows)
total = sum(r["amount_total_cents"] for r in rows)
check(f"  ...revenue recorded = ${total/100:.2f}", total == 4500, total)

print("\n=== catalogue matches the shop page ===")
check("ai=$10 coach=$35 zoom=$99",
      [payments.BUNDLES[k]["amount_cents"] for k in ("ai","coach","zoom")] == [1000,3500,9900])

print(f"\n{'='*46}\n  {PASS} passed, {FAIL} failed\n{'='*46}")
sys.exit(1 if FAIL else 0)
