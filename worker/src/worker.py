"""Worker entrypoint.

Everything enters here. Most requests are handed to the FastAPI app in app.py,
but two are handled before ASGI ever sees them:

    PUT /api/analysis/{id}/video   - browser uploading a clip
    GET /video_serve/{id}          - browser playing one back

Both move a whole video. Going through ASGI would pull the body into Python
memory, and a Worker has 128MB — one clip is enough to kill the isolate. Handled
here, the bytes stay a JavaScript object and are never converted into Python.

Where those bytes go is src/storage.py, which is a deliberate seam: it is
Workers KV today because R2 wants a payment card on file, and swapping it is a
one-file change.

The `scheduled` handler at the bottom is what replaced threading.Thread:
a Cron Trigger sweeping a job table in D1. Cloudflare Queues would be the
natural fit, but it is not on the Workers free plan, and this runs free.
"""

import json
import re
import secrets

import asgi
import js
from workers import Response, WorkerEntrypoint

import auth
import db as dbx
import gemini
import storage
from app import app
from httpjs import to_js
from mailer import send_email

UPLOAD_PATH = re.compile(r"^/api/analysis/(\d+)/video$")
SERVE_PATH = re.compile(r"^/video_serve/(\d+)$")

ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v", "video/mpeg"}
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # matches the Cloudflare body limit


def _json_response(payload: dict, status: int = 200):
    return Response(
        json.dumps(payload),
        status=status,
        headers={"Content-Type": "application/json"},
    )


def _session_from(request, env) -> dict:
    cookie = request.headers.get("Cookie") or ""
    token = None
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == auth.SESSION_COOKIE:
            token = value
            break
    return auth.read_session(token, getattr(env, "SESSION_SECRET", "") or "")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = js.URL.new(request.url)
        path = url.pathname

        if request.method == "PUT":
            match = UPLOAD_PATH.match(path)
            if match:
                return await self._upload_video(request, url, int(match.group(1)))

        if request.method == "GET":
            match = SERVE_PATH.match(path)
            if match:
                return await self._serve_video(request, int(match.group(1)))

        return await asgi.fetch(app, request, self.env)

    # -- streaming upload ---------------------------------------------------

    async def _upload_video(self, request, url, surfer_id: int):
        """Take one clip from the browser and hand it to the video store.

        The bytes stay a JavaScript ArrayBuffer throughout and are never
        converted into Python, so a 20MB clip costs 20MB rather than several
        times that inside Pyodide.
        """
        env = self.env
        session = _session_from(request, env)
        email = session.get("email")

        if not email:
            return _json_response({"detail": "Not signed in."}, 401)

        # This route bypasses the FastAPI middleware, so it does its own CSRF check.
        if not auth.csrf_ok(session, request.headers.get("X-CSRF-Token")):
            return _json_response({"detail": "Bad or missing CSRF token."}, 403)

        claim = session.get("upload") or {}
        if claim.get("surfer_id") != surfer_id:
            return _json_response({"detail": "Unknown upload."}, 400)

        database = dbx.Database(env.DB)
        surfer = await dbx.get_surfer(database, surfer_id)
        if not surfer or surfer["user_email"] != email:
            return _json_response({"detail": "Unknown upload."}, 404)

        existing = await dbx.videos_for_surfer(database, surfer_id)
        if len(existing) >= int(claim.get("count") or 1):
            return _json_response({"detail": "All videos for this session were already uploaded."}, 409)

        content_type = (url.searchParams.get("content_type") or "video/mp4").split(";")[0].strip()
        if content_type not in ALLOWED_VIDEO_TYPES:
            return _json_response({"detail": f"Unsupported video type: {content_type}"}, 415)

        max_bytes = int(getattr(env, "MAX_UPLOAD_BYTES", None) or DEFAULT_MAX_UPLOAD_BYTES)
        declared = request.headers.get("Content-Length")
        size = int(declared) if declared and declared.isdigit() else 0
        if size and size > max_bytes:
            return _json_response(
                {"detail": f"That file is larger than the {max_bytes // (1024 * 1024)}MB limit."},
                413,
            )
        if not request.body:
            return _json_response({"detail": "Empty upload."}, 400)

        # The key is server-generated. Using the client's filename would let a
        # crafted name reach across into another user's prefix.
        key = f"videos/{email}/{surfer_id}/{secrets.token_hex(8)}"

        # Read the body as a JS ArrayBuffer. With R2 this was a ReadableStream
        # that never landed anywhere, but KV needs the whole value, and holding
        # it lets the real size be checked before the write instead of failing
        # partway through. The buffer stays a JS object -- it is never converted
        # into Python bytes, which would multiply the cost inside Pyodide.
        buffer = await request.js_object.arrayBuffer()
        if int(buffer.byteLength) > max_bytes:
            return _json_response(
                {"detail": f"That file is larger than the {max_bytes // (1024 * 1024)}MB limit."},
                413,
            )

        try:
            stored_size = await storage.store_for(env).put(key, buffer, content_type)
        except ValueError as exc:
            return _json_response({"detail": str(exc)}, 413)
        except Exception as exc:  # noqa: BLE001
            print(f"Video store failed for {key}: {exc}")
            return _json_response({"detail": "Could not store the video."}, 502)

        await dbx.add_video(database, surfer_id, key, content_type, stored_size)
        return _json_response({"stored": True, "bytes": stored_size})

    # -- streaming playback -------------------------------------------------

    async def _serve_video(self, request, video_id: int):
        """Play a clip back, streamed from the video store.

        web.py served any video to anyone who guessed an id — there was no check
        at all on this route. Access is enforced here: the owner, or the admin.
        """
        env = self.env
        session = _session_from(request, env)
        email = session.get("email")
        if not email:
            return Response("Not signed in.", status=401)

        database = dbx.Database(env.DB)
        video = await dbx.get_video(database, video_id)
        if not video:
            return Response("No Data", status=404)

        surfer = await dbx.get_surfer(database, video["surfer_id"])
        if not surfer:
            return Response("No Data", status=404)

        is_admin = auth.is_admin(email, getattr(env, "SUPER_ADMIN_EMAIL", "") or "")
        if surfer["user_email"] != email and not is_admin:
            return Response("Not found.", status=404)

        # No Range handling here. R2 could serve a byte range, so a browser
        # could seek into a clip without downloading it; KV returns the whole
        # value or nothing. At the 20MB cap the browser just fetches it all and
        # seeks locally, which is fine, but it is a real capability that was
        # lost in the move off R2.
        try:
            stream = await storage.store_for(env).open(video["object_key"])
        except storage.VideoMissing:
            return Response("This clip is no longer available.", status=404)

        headers = js.Headers.new()
        headers.set("Content-Type", video["content_type"] or "video/mp4")
        headers.set("Cache-Control", "private, max-age=3600")
        headers.set("Accept-Ranges", "none")
        return js.Response.new(stream, to_js({"status": 200, "headers": headers}))

    # -- cron sweeper -------------------------------------------------------

    async def scheduled(self, controller=None, env=None, ctx=None):
        """Run one queued analysis. Fired by the Cron Trigger every minute.

        Every argument is optional on purpose. The Python Workers runtime is in
        open beta and has invoked this handler with both `(controller, env, ctx)`
        and `(controller)` alone, with the bindings read off `self` — and the
        mismatch is not a soft failure. The handler is called unrelaxed, so the
        wrong arity is a TypeError before the first line of the body runs, on
        every tick, once a minute, for as long as it goes unnoticed:

            TypeError: Default.scheduled() takes 3 positional arguments
                       but 4 were given

        Accepting either shape and falling back to `self.env` costs one line
        and cannot be got wrong by a runtime upgrade.

        This is what replaced threading.Thread, and then replaced Cloudflare
        Queues: Queues is not available on the Workers free plan, and this
        project is meant to run at zero cost. The job table in D1 plus a cron
        tick gives the same three properties that mattered — a job is claimed
        by exactly one worker, a failure is retried, and a worker that dies
        mid-analysis does not strand the job forever.

        Deliberately one job per tick. The free plan allows 10ms of CPU per
        cron invocation; almost all of an analysis is spent waiting on the
        network, which does not count toward CPU, but claiming several jobs at
        once would multiply the parsing that does.
        """
        env = env if env is not None else self.env
        database = dbx.Database(env.DB)
        token = secrets.token_hex(16)

        job = await dbx.claim_next_job(database, token)
        if job is None:
            return  # nothing waiting; the common case

        print(f"Sweeper claimed session {job['id']} (attempt {job['attempts']}).")
        try:
            await self._analyse_session(
                {"surfer_id": job["id"], "email": job["user_email"],
                 "bundle": job["bundle_used"]},
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            retrying = await dbx.release_job(database, job["id"], str(exc))
            if retrying:
                print(f"Session {job['id']} failed: {exc} — back in the queue.")
                return

            # Out of attempts. The surfer paid for an analysis they are not
            # going to get, so give the bundles back before giving up. Any clip
            # still attached is one that was never successfully analysed.
            leftover = await dbx.videos_for_surfer(database, job["id"])
            if leftover:
                await dbx.adjust_bundles(
                    database, job["user_email"], job["bundle_used"], len(leftover)
                )
                video_store = storage.store_for(env)
                for video in leftover:
                    await video_store.delete(video["object_key"])
            print(
                f"Session {job['id']} failed permanently: {exc} — "
                f"refunded {len(leftover)} bundle(s)."
            )

    async def _analyse_session(self, job: dict, env=None) -> None:
        env = env if env is not None else self.env
        database = dbx.Database(env.DB)

        surfer_id = int(job["surfer_id"])
        email = job["email"]
        bundle = job.get("bundle") or "ai"

        surfer = await dbx.get_surfer(database, surfer_id)
        if not surfer:
            print(f"Session {surfer_id} vanished before analysis; nothing to do.")
            return

        videos = await dbx.videos_for_surfer(database, surfer_id)
        if not videos:
            # Nothing to analyse. Fail it now rather than let it sit claimed
            # until the stale timeout expires, three times over.
            await dbx.mark_surfer_failed(database, surfer_id, "No videos were uploaded.")
            return

        examples = gemini.format_examples(await dbx.coach_examples(database, 5))
        api_key = getattr(env, "GEMINI_API_KEY", None)
        model = getattr(env, "GEMINI_MODEL", None) or "gemini-2.5-flash"
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        refunds = 0
        accepted = 0
        last_result = None

        for video in videos:
            result = await self._analyse_one(
                env, api_key, model, video, surfer, examples
            )

            if not result.get("is_surfing"):
                # Not surfing (or the call failed): refund and drop the clip, as
                # the shop page promises.
                refunds += 1
                await storage.store_for(env).delete(video["object_key"])
                await dbx.delete_video(database, video["id"])
            else:
                accepted += 1
                last_result = result

        if refunds:
            await dbx.adjust_bundles(database, email, bundle, refunds)

        if accepted and last_result:
            if bundle == "coach":
                # The coach tier stores the model's answer as a draft; a human
                # still has to sign off in /admin before the surfer sees it.
                await dbx.set_ai_recommendation(database, surfer_id, last_result)
            else:
                await dbx.set_final_recommendation(database, surfer_id, last_result)
                await send_email(
                    env,
                    email,
                    "Your Perfect Surfboard Dimensions!",
                    f"Hi!\nYour AI analysis is complete.\n\n"
                    f"Recommended Volume: {last_result['rec_liters']}L\n"
                    f"Length: {last_result['rec_feet']}'{last_result['rec_inches']}\"\n\n"
                    f"Notes: {last_result['skill_assessment_text']}\n\n"
                    f"See your dashboard for more details.",
                )
        else:
            # Every clip was rejected. Everything is refunded, so remove the row
            # rather than leaving a permanently empty session on the dashboard.
            await dbx.delete_surfer(database, surfer_id)

        print(f"Analysis for {email} complete: {accepted} accepted, {refunds} refunded.")

    async def _analyse_one(self, env, api_key, model, video, surfer, examples) -> dict:
        """Upload one clip to Gemini, ask for the sizing, then clean up."""
        # Deliberately outside the try below. A clip that cannot be read is not
        # evidence of anything about the footage, and KV is eventually
        # consistent -- a video written seconds ago may not be visible in this
        # location yet. Letting VideoMissing propagate turns it into a retry on
        # the next sweep. Catching it here and returning "not surfing" would
        # refund and delete a session that was perfectly fine.
        stream = await storage.store_for(env).open(video["object_key"])

        file_name = None
        try:
            uploaded = await gemini.upload_stream(
                api_key,
                stream=stream,
                size_bytes=int(video["size_bytes"]),
                mime_type=video["content_type"],
                display_name=f"surf-{video['id']}",
            )
            file_name = uploaded.get("name")
            active = await gemini.wait_until_active(api_key, file_name)

            raw = await gemini.analyse(
                api_key,
                model,
                file_uri=active.get("uri") or uploaded.get("uri"),
                mime_type=video["content_type"],
                weight_kg=surfer["weight_kg"],
                height_cm=surfer["height_cm"],
                skill=surfer["skill_level"],
                examples=examples,
            )
            return gemini.normalise_result(raw)
        except Exception as exc:  # noqa: BLE001
            # Treated as "not surfing", which refunds the bundle. The surfer is
            # never charged for our failure.
            print(f"Gemini analysis failed for video {video['id']}: {exc}")
            return {"is_surfing": False}
        finally:
            if file_name:
                await gemini.delete_file(api_key, file_name)
