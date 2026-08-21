"""The analysis sweeper: job claiming, retries, and the Gemini refund logic.

Cloudflare Queues is not on the Workers free plan, so the job table in D1 plus
a Cron Trigger stands in for it. These tests check that the substitute really
does provide the three properties a queue was giving us: exactly-once claiming,
retry on failure, and recovery from a worker that died holding a job.
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
import asyncio

import fakes  # installs the runtime stubs
import db as dbx
import gemini
import storage
import worker as W

SCHEMA = open(os.path.join(os.path.dirname(_HERE), "schema.sql")).read()
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label} {extra}")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SURFING = {"is_surfing": True, "skill_assessment_text": "Solid pop-up.",
           "rec_liters": 32.5, "rec_feet": 6, "rec_inches": 2}
NOT_SURF = {"is_surfing": False}


def stub_gemini(results):
    """Feed the sweeper a scripted sequence of Gemini verdicts."""
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


async def seed(env, bundle="ai", n_videos=1, queued=True):
    """Create a session with videos, as the upload routes would."""
    D = dbx.Database(env.DB)
    email = "s@example.com"
    await dbx.get_inventory(D, email)
    await dbx.set_bundles(D, email, "ai", 5)
    await dbx.set_bundles(D, email, "coach", 5)
    sid = await dbx.create_surfer(D, email=email, height_cm=180, weight_kg=75,
                                  skill="Intermediate", is_pro=(bundle == "coach"),
                                  bundle_used=bundle)
    for i in range(n_videos):
        key = f"videos/{email}/{sid}/{i}"
        await storage.store_for(env).put(key, fakes.FakeArrayBuffer(b"v" * 2048), "video/mp4")
        await dbx.add_video(D, sid, key, "video/mp4", 2048)
    await dbx.spend_bundle(D, email, bundle, n_videos)
    if queued:
        await dbx.enqueue_analysis(D, sid)
    return D, sid, email


def sweeper(env):
    d = W.Default.__new__(W.Default)
    d.env = env
    return d


def tick(env):
    """One cron firing."""
    return run(sweeper(env).scheduled(None, env, None))


# ---------------------------------------------------------------------------
print("\n=== claiming: exactly one worker gets each job ===")
env = fakes.FakeEnv(SCHEMA)
D, sid, email = run(seed(env))
first = run(dbx.claim_next_job(D, "token-a"))
check("a queued job is claimable", first is not None and first["id"] == sid, first)
check("  ...marked processing", first["status"] == "processing", first["status"])
check("  ...attempt counted", first["attempts"] == 1, first["attempts"])

second = run(dbx.claim_next_job(D, "token-b"))
check("a second worker gets nothing", second is None, second)

print("\n=== retry on failure ===")
retrying = run(dbx.release_job(D, sid, "Gemini timed out"))
check("released back to the queue", retrying is True)
row = run(dbx.get_surfer(D, sid))
check("  ...status is queued again", row["status"] == "queued", row["status"])
check("  ...claim cleared", row["claim_token"] is None, row["claim_token"])
check("  ...error recorded", "timed out" in (row["error_message"] or ""), row["error_message"])
check("re-claimable on the next tick", run(dbx.claim_next_job(D, "token-c")) is not None)

print("\n=== gives up after MAX_ATTEMPTS ===")
run(dbx.release_job(D, sid, "again"))
run(dbx.claim_next_job(D, "token-d"))
still = run(dbx.release_job(D, sid, "and again"))
check(f"third failure stops the retries (MAX_ATTEMPTS={dbx.MAX_ATTEMPTS})", still is False)
row = run(dbx.get_surfer(D, sid))
check("  ...marked failed", row["status"] == "failed", row["status"])
check("  ...no longer claimable", run(dbx.claim_next_job(D, "token-e")) is None)

print("\n=== a dead worker's job is reclaimed, not stranded ===")
env = fakes.FakeEnv(SCHEMA)
D, sid, email = run(seed(env))
run(dbx.claim_next_job(D, "dead-worker"))
check("held claim blocks other workers", run(dbx.claim_next_job(D, "other")) is None)
# Simulate the claim ageing past the visibility timeout.
run(D.execute("UPDATE surfer SET claimed_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", sid))
reclaimed = run(dbx.claim_next_job(D, "rescuer"))
check("stale claim is reclaimable", reclaimed is not None and reclaimed["id"] == sid, reclaimed)
check("  ...attempts kept counting", reclaimed["attempts"] == 2, reclaimed["attempts"])

# ---------------------------------------------------------------------------
print("\n=== cron tick: happy path ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING])
D, sid, email = run(seed(env))
tick(env)
s = run(dbx.get_surfer(D, sid))
check("recommendation written", s["rec_liters"] == 32.5 and s["rec_feet"] == 6, s)
check("status complete", s["status"] == "complete", s["status"])
check("claim released", s["claim_token"] is None)
check("no refund", run(dbx.get_inventory(D, email))["ai_bundles"] == 4)
check("clip kept in R2", len(env.VIDEOS.objects) == 1)

print("\n=== an idle tick does nothing ===")
before = dict(run(dbx.get_inventory(D, email)))
tick(env)
check("no job claimed, nothing changed", run(dbx.get_inventory(D, email)) == before)

print("\n=== one job per tick ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING, SURFING, SURFING])
D, _, email = run(seed(env))
_, sid2, _ = run(seed(env))
_, sid3, _ = run(seed(env))
tick(env)
done = run(D.query("SELECT id FROM surfer WHERE status = 'complete'"))
waiting = run(D.query("SELECT id FROM surfer WHERE status = 'queued'"))
check("exactly one processed", len(done) == 1, done)
check("the rest still queued", len(waiting) == 2, waiting)
tick(env); tick(env)
check("later ticks drain the rest",
      len(run(D.query("SELECT id FROM surfer WHERE status = 'complete'"))) == 3)

print("\n=== not surfing: refunded, session removed ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([NOT_SURF])
D, sid, email = run(seed(env))
tick(env)
check("bundle refunded", run(dbx.get_inventory(D, email))["ai_bundles"] == 5)
check("session row deleted", run(dbx.get_surfer(D, sid)) is None)
check("rejected clip purged from R2", len(env.VIDEOS.objects) == 0)

print("\n=== Gemini error refunds, never charges ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([RuntimeError("API exploded")])
D, sid, email = run(seed(env))
tick(env)
check("user refunded for our failure", run(dbx.get_inventory(D, email))["ai_bundles"] == 5)

print("\n=== mixed batch: 1 good, 2 bad ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING, NOT_SURF, NOT_SURF])
D, sid, email = run(seed(env, n_videos=3))
tick(env)
check("2 of 3 refunded", run(dbx.get_inventory(D, email))["ai_bundles"] == 4)
check("session kept with a recommendation", run(dbx.get_surfer(D, sid))["rec_liters"] == 32.5)
check("only the good clip remains in R2", len(env.VIDEOS.objects) == 1, env.VIDEOS.objects)
check("only the good video row remains", len(run(dbx.videos_for_surfer(D, sid))) == 1)

print("\n=== coach tier: AI answer is a draft, not the verdict ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING])
D, sid, email = run(seed(env, bundle="coach"))
tick(env)
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

print("\n=== daily cap query ===")
check("counts today's sessions", run(dbx.analyses_today(D)) == 1, run(dbx.analyses_today(D)))

print("\n=== Workers KV specifics ===")
env = fakes.FakeEnv(SCHEMA)
store = storage.store_for(env)
run(store.put("k1", fakes.FakeArrayBuffer(b"x" * 4096), "video/mp4"))
check("write sets an expiry so storage cannot creep", env.VIDEOS.ttls["k1"] == 30 * 24 * 3600,
      env.VIDEOS.ttls["k1"])
check("content type is kept as key metadata",
      env.VIDEOS.meta["k1"]["contentType"] == "video/mp4", env.VIDEOS.meta["k1"])
check("reads back", run(store.open("k1")) is not None)

try:
    run(store.put("big", fakes.FakeArrayBuffer(b"x" * (26 * 1024 * 1024)), "video/mp4"))
    check("oversize value refused before the write", False, "no error raised")
except ValueError as exc:
    check("oversize value refused before the write", "caps a value" in str(exc), exc)

try:
    run(store.put("empty", fakes.FakeArrayBuffer(b""), "video/mp4"))
    check("empty upload refused", False, "no error raised")
except ValueError:
    check("empty upload refused", True)

try:
    run(store.open("never-written"))
    check("a missing key raises VideoMissing, never returns None", False, "no error raised")
except storage.VideoMissing:
    check("a missing key raises VideoMissing, never returns None", True)

print("\n=== eventual consistency must not look like 'not surfing' ===")
# KV writes can take up to 60s to be visible in another location. If the sweeper
# reads before then, the clip is absent — and treating that as a verdict would
# refund and delete a session that was perfectly fine.
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING])
D, sid, email = run(seed(env))
spent = run(dbx.get_inventory(D, email))["ai_bundles"]
key = run(dbx.videos_for_surfer(D, sid))[0]["object_key"]
env.VIDEOS.hidden.add(key)          # written, not yet visible here

tick(env)
row = run(dbx.get_surfer(D, sid))
check("session survives an invisible clip", row is not None)
check("  ...requeued rather than judged", row["status"] == "queued", row["status"])
check("  ...no bundle refunded yet", run(dbx.get_inventory(D, email))["ai_bundles"] == spent)
check("  ...clip not deleted", key in env.VIDEOS.values)

env.VIDEOS.hidden.discard(key)      # propagation catches up
tick(env)
row = run(dbx.get_surfer(D, sid))
check("next sweep analyses it normally", row["status"] == "complete", row["status"])
check("  ...recommendation written", row["rec_liters"] == 32.5)

print("\n=== a clip that never appears is refunded, not silently lost ===")
env = fakes.FakeEnv(SCHEMA); stub_gemini([SURFING] * 6)
D, sid, email = run(seed(env))
before = run(dbx.get_inventory(D, email))["ai_bundles"]
key = run(dbx.videos_for_surfer(D, sid))[0]["object_key"]
env.VIDEOS.hidden.add(key)
for _ in range(dbx.MAX_ATTEMPTS + 1):
    tick(env)
    run(D.execute("UPDATE surfer SET claimed_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", sid))
row = run(dbx.get_surfer(D, sid))
check("gives up after MAX_ATTEMPTS", row["status"] == "failed", row["status"])
check("  ...and refunds the surfer", run(dbx.get_inventory(D, email))["ai_bundles"] == before + 1,
      run(dbx.get_inventory(D, email))["ai_bundles"])

print("\n=== the cron handler survives either calling convention ===")
# A signature mismatch here is a TypeError raised before the body runs, once a
# minute, silently, until someone opens `wrangler tail`. The runtime has passed
# both (controller, env, ctx) and (controller) alone across beta releases, so
# both are exercised.
env = fakes.FakeEnv(SCHEMA)
D, sid, email = run(seed(env))
stub_gemini([SURFING])
run(sweeper(env).scheduled(None, env, None))
check("called as scheduled(controller, env, ctx)",
      run(dbx.get_surfer(D, sid))["status"] != "queued")

env = fakes.FakeEnv(SCHEMA)
D, sid, email = run(seed(env))
stub_gemini([SURFING])
run(sweeper(env).scheduled(None))  # env read off self, as a newer runtime does
check("called as scheduled(controller), env off self",
      run(dbx.get_surfer(D, sid))["status"] != "queued")

check("no tick raises when the queue is empty", run(sweeper(env).scheduled()) is None)

print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
