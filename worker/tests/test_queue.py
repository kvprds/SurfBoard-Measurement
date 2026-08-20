import sys, asyncio, types
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
import fakes
import db as dbx, gemini
import worker as W

SCHEMA = open(os.path.join(os.path.dirname(_HERE), "schema.sql")).read()
PASS = FAIL = 0
def check(l, c, x=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  ok   {l}")
    else: FAIL += 1; print(f"  FAIL {l} {x}")

SURFING  = {"is_surfing": True, "skill_assessment_text": "Solid pop-up.",
            "rec_liters": 32.5, "rec_feet": 6, "rec_inches": 2}
NOT_SURF = {"is_surfing": False}

def stub_gemini(results):
    """Feed the consumer a scripted sequence of Gemini verdicts."""
    seq = list(results)
    async def up(*a, **k): return {"name": "files/x", "uri": "u"}
    async def wait(*a, **k): return {"state": "ACTIVE", "uri": "u"}
    async def an(*a, **k):
        r = seq.pop(0)
        if isinstance(r, Exception): raise r
        return r
    async def dele(*a, **k): return None
    gemini.upload_stream, gemini.wait_until_active = up, wait
    gemini.analyse, gemini.delete_file = an, dele

async def setup(env, bundle, n_videos, ai_start=5, coach_start=5):
    D = dbx.Database(env.DB)
    email = "s@example.com"
    await dbx.get_inventory(D, email)
    await dbx.set_bundles(D, email, "ai", ai_start)
    await dbx.set_bundles(D, email, "coach", coach_start)
    sid = await dbx.create_surfer(D, email=email, height_cm=180, weight_kg=75,
                                  skill="Intermediate", is_pro=(bundle == "coach"),
                                  bundle_used=bundle)
    for i in range(n_videos):
        key = f"videos/{email}/{sid}/{i}"
        await env.VIDEOS.put(key, b"v" * 2048)
        await dbx.add_video(D, sid, key, "video/mp4", 2048)
    await dbx.spend_bundle(D, email, bundle, n_videos)
    return D, sid, email

def run(coro): return asyncio.new_event_loop().run_until_complete(coro)

print("\n=== happy path: AI tier, one surfing clip ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING])
D, sid, email = run(setup(env, "ai", 1))
d = W.Default.__new__(W.Default); d.env = env
run(d._analyse_session({"surfer_id": sid, "email": email, "bundle": "ai"}))
s = run(dbx.get_surfer(D, sid))
check("final recommendation written", s["rec_liters"] == 32.5 and s["rec_feet"] == 6, s)
check("status is complete", s["status"] == "complete", s["status"])
check("no refund issued", run(dbx.get_inventory(D, email))["ai_bundles"] == 4)
check("clip kept in R2", len(env.VIDEOS.objects) == 1)

print("\n=== not surfing: bundle refunded, session removed ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([NOT_SURF])
D, sid, email = run(setup(env, "ai", 1))
d = W.Default.__new__(W.Default); d.env = env
run(d._analyse_session({"surfer_id": sid, "email": email, "bundle": "ai"}))
check("bundle refunded", run(dbx.get_inventory(D, email))["ai_bundles"] == 5)
check("session row deleted", run(dbx.get_surfer(D, sid)) is None)
check("rejected clip purged from R2", len(env.VIDEOS.objects) == 0)

print("\n=== Gemini error is treated as a refund, never a charge ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([RuntimeError("API exploded")])
D, sid, email = run(setup(env, "ai", 1))
d = W.Default.__new__(W.Default); d.env = env
run(d._analyse_session({"surfer_id": sid, "email": email, "bundle": "ai"}))
check("user refunded for our failure", run(dbx.get_inventory(D, email))["ai_bundles"] == 5)

print("\n=== mixed batch: 1 good, 2 bad ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING, NOT_SURF, NOT_SURF])
D, sid, email = run(setup(env, "ai", 3))
d = W.Default.__new__(W.Default); d.env = env
run(d._analyse_session({"surfer_id": sid, "email": email, "bundle": "ai"}))
inv = run(dbx.get_inventory(D, email))
check("2 of 3 bundles refunded", inv["ai_bundles"] == 4, inv["ai_bundles"])
check("session kept with a recommendation", run(dbx.get_surfer(D, sid))["rec_liters"] == 32.5)
check("only the good clip remains in R2", len(env.VIDEOS.objects) == 1, env.VIDEOS.objects)
check("only the good video row remains", len(run(dbx.videos_for_surfer(D, sid))) == 1)

print("\n=== coach tier: AI answer is a draft, not the verdict ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING])
D, sid, email = run(setup(env, "coach", 1))
d = W.Default.__new__(W.Default); d.env = env
run(d._analyse_session({"surfer_id": sid, "email": email, "bundle": "coach"}))
s = run(dbx.get_surfer(D, sid))
check("draft stored in ai_rec_*", s["ai_rec_liters"] == 32.5, s["ai_rec_liters"])
check("surfer-visible rec still empty", s["rec_liters"] is None, s["rec_liters"])
check("status awaits the coach", s["status"] == "awaiting_coach", s["status"])
check("still in the admin queue", len(run(dbx.list_pending_surfers(D))) == 1)

print("\n=== few-shot examples come from completed pro rows ===")
run(dbx.set_final_recommendation(D, sid, {"rec_liters": 30.0, "rec_feet": 5, "rec_inches": 11,
                                          "skill_assessment_text": "Coach reasoning here."}))
ex = run(dbx.coach_examples(D, 5))
check("coach decision is now an example", len(ex) == 1 and ex[0]["rec_liters"] == 30.0, ex)
check("formats into prompt text", "Coach reasoning here." in gemini.format_examples(ex))

print(f"\n{'='*46}\n  {PASS} passed, {FAIL} failed\n{'='*46}")
sys.exit(1 if FAIL else 0)
