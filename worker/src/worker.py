"""Worker entrypoint.

Everything enters here. Most requests are handed to the FastAPI app in app.py,
but two are handled before ASGI ever sees them:

    PUT /api/analysis/{id}/video   - browser uploading a clip
    GET /video_serve/{id}          - browser playing one back

Both move a whole video. Going through ASGI would pull the body into Python
memory, and a Worker has 128MB — one clip is enough to kill the isolate. Handled
here, the bytes stay a ReadableStream and pass straight between the socket and
R2 without ever being materialised.

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
        """Stream one clip from the browser into R2.

        `request.body` is a ReadableStream and is handed to R2 untouched, so a
        200MB file costs the same memory as a 200KB one.
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

        try:
            stored = await env.VIDEOS.put(
                key,
                request.body,
                to_js({"httpMetadata": {"contentType": content_type}}),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"R2 upload failed for {key}: {exc}")
            return _json_response({"detail": "Could not store the video."}, 502)

        stored_size = int(getattr(stored, "size", 0) or size or 0)
        await dbx.add_video(database, surfer_id, key, content_type, stored_size)
        return _json_response({"stored": True, "bytes": stored_size})

    # -- streaming playback -------------------------------------------------

    async def _serve_video(self, request, video_id: int):
        """Play a clip back, streamed from R2.

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

        # Forward Range so the browser's video scrubber works without pulling
        # the whole file.
        options = {}
        range_header = request.headers.get("Range")
        if range_header:
            options["range"] = js.Headers.new([["range", range_header]])

        obj = await env.VIDEOS.get(video["object_key"], to_js(options) if options else None)
        if obj is None:
            return Response("No Data", status=404)

        headers = js.Headers.new()
        obj.writeHttpMetadata(headers)
        headers.set("Content-Length", str(obj.size))
        headers.set("Cache-Control", "private, max-age=3600")
        if range_header and getattr(obj, "range", None):
            headers.set(
                "Content-Range",
                f"bytes {obj.range.offset}-{obj.range.offset + obj.range.length - 1}/{obj.size}",
            )

        status = 206 if (range_header and getattr(obj, "range", None)) else 200
        return js.Response.new(obj.body, to_js({"status": status, "headers": headers}))

    # -- cron sweeper -------------------------------------------------------

    async def scheduled(self, controller, env, ctx):
        """Run one queued analysis. Fired by the Cron Trigger every minute.

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
            print(
                f"Session {job['id']} failed: {exc} — "
                + ("back in the queue." if retrying else "giving up after max attempts.")
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
                try:
                    await env.VIDEOS.delete(video["object_key"])
                except Exception as exc:  # noqa: BLE001
                    print(f"Could not delete rejected clip {video['object_key']}: {exc}")
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
        obj = await env.VIDEOS.get(video["object_key"])
        if obj is None:
            print(f"Clip {video['object_key']} missing from R2.")
            return {"is_surfing": False}

        file_name = None
        try:
            uploaded = await gemini.upload_stream(
                api_key,
                stream=obj.body,
                size_bytes=int(obj.size),
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
