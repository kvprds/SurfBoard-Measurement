import asyncio
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
import fakes                       # installs the runtime stubs

from fastapi.testclient import TestClient
import auth, db as dbx
from app import app

SCHEMA = open(os.path.join(os.path.dirname(_HERE), "schema.sql")).read()
SECRET = "t" * 64
PASS = FAIL = 0

def check(label, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {label}")
    else:    FAIL += 1; print(f"  FAIL {label} {extra}")

def client(env=None):
    env = env or fakes.FakeEnv(SCHEMA)
    c = TestClient(fakes.with_env(app, env), base_url="https://test.local")
    return c, env

def login(c, email, csrf="csrf-token-fixed"):
    """Forge a signed session cookie, as /authorize would issue."""
    tok = auth.sign_session({"email": email, "name": email, "csrf": csrf}, SECRET)
    c.cookies.set(auth.SESSION_COOKIE, tok)
    return csrf

print("\n=== anonymous routing ===")
c, env = client()
r = c.get("/", follow_redirects=False)
check("GET / returns the landing page", r.status_code == 200 and "Surfing AI" in r.text)
check("security headers present", r.headers.get("x-frame-options") == "DENY")
check("GET /privacy", c.get("/privacy").status_code == 200)
for path in ("/dashboard", "/shop", "/new_analysis", "/upload_video", "/admin"):
    r = c.get(path, follow_redirects=False)
    check(f"{path} redirects when signed out", r.status_code == 303 and r.headers["location"] == "/")
r = c.get("/login", follow_redirects=False)
check("/login redirects to Google", "accounts.google.com" in r.headers.get("location", ""))
check("  ...and carries a state param", "state=" in r.headers["location"])

print("\n=== CSRF ===")
c, env = client()
csrf = login(c, "surfer@example.com")
r = c.post("/send_zoom_message", data={"message": "hi"}, follow_redirects=False)
check("POST without a token is 403", r.status_code == 403, r.status_code)
r = c.post("/send_zoom_message", data={"message": "hi", "_csrf_token": "wrong"}, follow_redirects=False)
check("POST with a wrong token is 403", r.status_code == 403, r.status_code)
r = c.post("/send_zoom_message", data={"message": "hello coach", "_csrf_token": csrf}, follow_redirects=False)
check("POST with the right token succeeds", r.status_code == 303, r.status_code)

print("\n=== dashboard + D1 round trip ===")
r = c.get("/dashboard")
check("dashboard renders", r.status_code == 200 and "My Dashboard" in r.text)
check("new user starts with 1 AI bundle", "1 AI | 0 Pro | 0 Zoom" in r.text)
# The chat panel only renders for users who own a Zoom bundle, so grant one
# and re-fetch to prove the message really landed in D1.
asyncio.get_event_loop().run_until_complete(
    dbx.set_bundles(dbx.Database(env.DB), "surfer@example.com", "zoom", 1))
check("zoom message was stored and displayed", "hello coach" in c.get("/dashboard").text)

print("\n=== stats wizard validation ===")
r = c.post("/new_analysis", data={"unit_sys": "metric", "height_cm": "180", "weight_kg": "75",
                                  "skill": "Intermediate", "_csrf_token": csrf}, follow_redirects=False)
check("valid metric stats accepted", r.status_code == 303 and r.headers["location"] == "/upload_video")
r = c.post("/new_analysis", data={"unit_sys": "metric", "height_cm": "0", "weight_kg": "0",
                                  "_csrf_token": csrf}, follow_redirects=False)
check("zero measurements rejected", r.status_code == 400 and "Check your measurements" in r.text)
r = c.post("/new_analysis", data={"unit_sys": "imperial", "height_ft": "5", "height_in": "11",
                                  "weight_lbs": "165", "skill": "Pro", "_csrf_token": csrf},
           follow_redirects=False)
check("imperial converts and is accepted", r.status_code == 303)
r = c.get("/upload_video")
check("upload page renders after the wizard", r.status_code == 200 and "/api/analysis/start" in r.text)

print("\n=== bundle accounting ===")
r = c.post("/api/analysis/start", json={"tier": "standard", "count": 1},
           headers={"X-CSRF-Token": csrf})
check("start spends the free AI bundle", r.status_code == 200, r.text[:120])
sid = r.json().get("surfer_id")
check("  ...and returns a surfer id", isinstance(sid, int) and sid > 0)

r2 = c.post("/api/analysis/start", json={"tier": "standard", "count": 1},
            headers={"X-CSRF-Token": csrf})
check("second start is refused with no balance (402)", r2.status_code == 402, r2.status_code)

r3 = c.post("/api/analysis/start", json={"tier": "pro", "count": 1}, headers={"X-CSRF-Token": csrf})
check("coach tier refused with 0 coach bundles", r3.status_code == 402, r3.status_code)

r4 = c.post("/api/analysis/start", json={"tier": "standard", "count": 99},
            headers={"X-CSRF-Token": csrf})
check("absurd video count rejected", r4.status_code == 400, r4.status_code)

print("\n=== submit with no uploaded video refunds ===")
r = c.post(f"/api/analysis/{sid}/submit", headers={"X-CSRF-Token": csrf})
check("submit with no videos is a 400", r.status_code == 400, r.status_code)
check("nothing was left queued", asyncio.get_event_loop().run_until_complete(
    dbx.Database(env.DB).query("SELECT * FROM surfer WHERE status = 'queued'")) == [])
inv = asyncio.get_event_loop().run_until_complete(
    dbx.get_inventory(dbx.Database(env.DB), "surfer@example.com"))
check("the AI bundle was refunded", inv["ai_bundles"] == 1, inv)
rows = asyncio.get_event_loop().run_until_complete(
    dbx.Database(env.DB).query("SELECT * FROM surfer WHERE id = ?", sid))
check("the abandoned session row was removed", rows == [], rows)

print("\n=== daily analysis cap (protects the Gemini free tier) ===")
cc, cenv = client(fakes.FakeEnv(SCHEMA, DAILY_ANALYSIS_CAP="2"))
ccsrf = login(cc, "capped@example.com")
loop = asyncio.get_event_loop()
loop.run_until_complete(dbx.set_bundles(dbx.Database(cenv.DB), "capped@example.com", "ai", 10))
cc.post("/new_analysis", data={"unit_sys": "metric", "height_cm": "180", "weight_kg": "75",
                               "skill": "Pro", "_csrf_token": ccsrf}, follow_redirects=False)
codes = []
for _ in range(3):
    codes.append(cc.post("/api/analysis/start", json={"tier": "standard", "count": 1},
                         headers={"X-CSRF-Token": ccsrf}).status_code)
check("first two analyses allowed", codes[:2] == [200, 200], codes)
check("third is refused with 429, not a surprise bill", codes[2] == 429, codes)
inv = loop.run_until_complete(dbx.get_inventory(dbx.Database(cenv.DB), "capped@example.com"))
check("the refused attempt spent no bundle", inv["ai_bundles"] == 8, inv["ai_bundles"])

ucc, uenv = client(fakes.FakeEnv(SCHEMA, DAILY_ANALYSIS_CAP="0"))
ucsrf = login(ucc, "free@example.com")
loop.run_until_complete(dbx.set_bundles(dbx.Database(uenv.DB), "free@example.com", "ai", 10))
ucc.post("/new_analysis", data={"unit_sys": "metric", "height_cm": "180", "weight_kg": "75",
                                "skill": "Pro", "_csrf_token": ucsrf}, follow_redirects=False)
ok = [ucc.post("/api/analysis/start", json={"tier": "standard", "count": 1},
               headers={"X-CSRF-Token": ucsrf}).status_code for _ in range(3)]
check("cap of 0 disables the limit", ok == [200, 200, 200], ok)

print("\n=== admin ===")
ca, enva = client()
acsrf = login(ca, "admin@example.com")
r = ca.get("/admin")
check("admin panel renders for the super admin", r.status_code == 200 and "Admin Command" in r.text)
r = ca.post("/admin_update_inventory",
            data={"email": "surfer@example.com", "bundle_type": "coach", "amount": "3",
                  "_csrf_token": acsrf}, follow_redirects=False)
check("admin can set a balance", r.status_code == 303)
r = ca.get("/admin")
check("  ...and it shows in the table", "surfer@example.com" in r.text)

cu, _ = client(enva)
ucsrf = login(cu, "surfer@example.com")
r = cu.get("/admin", follow_redirects=False)
check("non-admin is bounced from /admin", r.status_code == 303 and r.headers["location"] == "/")
r = cu.post("/admin_update_inventory",
            data={"email": "surfer@example.com", "bundle_type": "ai", "amount": "999",
                  "_csrf_token": ucsrf}, follow_redirects=False)
check("non-admin cannot grant themselves bundles", r.status_code == 303 and r.headers["location"] == "/")
r = cu.get("/dashboard")
check("  ...balance unchanged", "999" not in r.text)

print("\n=== shop ===")
r = cu.get("/shop")
check("shop renders", r.status_code == 200)
check("prices come from the catalogue", "$10" in r.text and "$35" in r.text and "$99" in r.text)
check("buy buttons post to /buy/<bundle>", 'action="/buy/ai"' in r.text and 'action="/buy/zoom"' in r.text)
check("no dead links left", 'href="#"' not in r.text)
r = cu.post("/buy/nonsense", data={"_csrf_token": ucsrf}, follow_redirects=False)
check("unknown bundle is a 404", r.status_code == 404, r.status_code)

print(f"\n{'='*46}\n  {PASS} passed, {FAIL} failed\n{'='*46}")
sys.exit(1 if FAIL else 0)
