"""Every route from web.py, as a FastAPI app running inside a Python Worker.

Two things are worth knowing before reading further.

**Where `env` comes from.** Bindings (D1, KV) hang off the Worker's
`env`, which the ASGI adapter puts in the request scope. `_env(request)` is the
only place that knowledge lives.

**What is *not* here.** Video upload and video playback are handled in
worker.py, before this app sees the request. Both move whole video files, and
routing them through ASGI would read them into Python memory — of which a Worker
has 128MB. Handling them at the raw-request level lets the bytes stream.
"""

import json
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders

import auth
import db as dbx
import payments
import storage
from templates import render

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# POSTs that must not be CSRF-checked: Stripe signs its own webhook, and there
# is no session yet when Google redirects back from sign-in.
CSRF_EXEMPT = {"/webhooks/stripe"}

SKILL_LEVELS = {"Beginner", "Intermediate", "Advanced", "Pro", "Unspecified"}


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------

def _env(request: Request):
    return request.scope["env"]


def _database(request: Request) -> dbx.Database:
    return dbx.Database(_env(request).DB)


def _var(request: Request, name: str, default: str = "") -> str:
    return getattr(_env(request), name, None) or default


def _session(request: Request) -> dict:
    return auth.read_session(
        request.cookies.get(auth.SESSION_COOKIE), _var(request, "SESSION_SECRET")
    )


def _base_url(request: Request) -> str:
    """Where this app is reachable, for OAuth and Stripe redirect URLs."""
    configured = _var(request, "APP_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _with_session(response, request: Request, session: dict):
    """Re-issue the session cookie so CSRF tokens and wizard state persist."""
    token = auth.sign_session(session, _var(request, "SESSION_SECRET"))
    response.headers.append(
        "set-cookie",
        auth.session_cookie_header(token, secure=_base_url(request).startswith("https")),
    )
    return response


def _page(request: Request, session: dict, page: str, **context) -> HTMLResponse:
    """Render a template, always supplying a fresh CSRF token."""
    context.setdefault("csrf_token", auth.ensure_csrf(session))
    return _with_session(HTMLResponse(render(page, **context)), request, session)


def _error_page(request, session, title, message, back="/dashboard", status=400):
    response = HTMLResponse(
        render("error", title=title, message=message, back=back), status_code=status
    )
    return _with_session(response, request, session)


def _redirect(request: Request, location: str, session: dict | None = None):
    response = RedirectResponse(location, status_code=303)
    if session is not None:
        _with_session(response, request, session)
    return response


def _is_admin(request: Request, session: dict) -> bool:
    return auth.is_admin(session.get("email"), _var(request, "SUPER_ADMIN_EMAIL"))


# ---------------------------------------------------------------------------
# Global middleware: CSRF and security headers
# ---------------------------------------------------------------------------

class SecurityMiddleware:
    """CSRF gate plus security headers, as pure ASGI middleware.

    Deliberately not an @app.middleware("http") function. The CSRF token can
    arrive as a form field, and reading the body inside a BaseHTTPMiddleware
    consumes the receive channel — the route handler that runs afterwards then
    sees an empty form, silently dropping every field the user submitted.
    Wrapping `receive` here lets the body be read twice: once to find the token,
    once by the route.

    The Flask version only ever looked for a hidden form field. An
    X-CSRF-Token header is accepted too, because the upload page now drives its
    requests with fetch() instead of a form submit.
    """

    UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        send = _add_security_headers(send)

        if scope["method"] in self.UNSAFE_METHODS and scope["path"] not in CSRF_EXEMPT:
            request = Request(scope, receive)
            secret = getattr(scope.get("env"), "SESSION_SECRET", "") or ""
            session = auth.read_session(request.cookies.get(auth.SESSION_COOKIE), secret)

            submitted = request.headers.get("x-csrf-token")
            if not submitted:
                body = await request.body()
                form = await Request(scope, _replay(body)).form()
                submitted = form.get("_csrf_token")
                receive = _replay(body)  # hand the route an unread body

            if not auth.csrf_ok(session, submitted):
                response = JSONResponse({"detail": "Bad or missing CSRF token."}, status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def _replay(body: bytes):
    """A receive callable that replays an already-consumed body once."""
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _add_security_headers(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers.setdefault("X-Frame-Options", "DENY")
            headers.setdefault("X-Content-Type-Options", "nosniff")
            headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        await send(message)

    return wrapped


app.add_middleware(SecurityMiddleware)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.get("/")
async def home(request: Request):
    session = _session(request)
    if session.get("email"):
        return _redirect(request, "/dashboard")
    return _page(
        request, session, "home",
        google_configured=bool(_var(request, "GOOGLE_CLIENT_ID")),
    )


@app.get("/privacy")
async def privacy(request: Request):
    return _page(request, _session(request), "privacy")


# ---------------------------------------------------------------------------
# Google sign-in
# ---------------------------------------------------------------------------

@app.get("/login")
async def login(request: Request):
    client_id = _var(request, "GOOGLE_CLIENT_ID")
    if not client_id:
        return _redirect(request, "/")

    session = _session(request)
    # Random per-attempt value, echoed back by Google and compared below. It is
    # what proves the callback belongs to a sign-in this browser started.
    session["oauth_state"] = secrets.token_urlsafe(24)

    url = auth.build_auth_url(
        client_id, f"{_base_url(request)}/authorize", session["oauth_state"]
    )
    return _redirect(request, url, session)


@app.get("/authorize")
async def authorize(request: Request):
    from httpjs import fetch_json

    session = _session(request)
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state or state != session.get("oauth_state"):
        return _error_page(
            request, {}, "Login Failed",
            "That sign-in link did not match this browser session. Please try again.",
            back="/", status=400,
        )
    session.pop("oauth_state", None)

    try:
        token = await auth.exchange_code(
            fetch_json,
            code=code,
            client_id=_var(request, "GOOGLE_CLIENT_ID"),
            client_secret=_var(request, "GOOGLE_CLIENT_SECRET"),
            redirect_uri=f"{_base_url(request)}/authorize",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"OAuth token exchange failed: {exc}")
        return _error_page(request, {}, "Login Failed",
                           "Could not complete sign-in with Google.", back="/", status=502)

    claims = auth.claims_from_id_token(token.get("id_token", ""))
    email = (claims.get("email") or "").strip().lower()

    if not email or not claims.get("email_verified", True):
        return _error_page(request, {}, "Login Failed",
                           "Google did not return a verified email address.", back="/", status=400)

    # A fresh session on login: never carry state across an identity change.
    new_session = {"email": email, "name": claims.get("name") or email}
    auth.ensure_csrf(new_session)
    return _redirect(request, "/dashboard", new_session)


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse("/", status_code=303)
    response.headers.append("set-cookie", auth.clear_cookie_header())
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard")
async def dashboard(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")
    if _is_admin(request, session):
        return _redirect(request, "/admin")

    database = _database(request)
    email = session["email"]

    return _page(
        request, session, "dashboard",
        user={"email": email},
        inv=await dbx.get_inventory(database, email),
        surfs=await dbx.list_surfers_for_user(database, email),
        zoom_messages=await dbx.zoom_messages(database, email),
    )


@app.post("/send_zoom_message")
async def send_zoom_message(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")

    form = await request.form()
    message = (form.get("message") or "").strip()
    if message:
        await dbx.add_zoom_message(_database(request), session["email"], "user", message[:2000])
    return _redirect(request, "/dashboard")


# ---------------------------------------------------------------------------
# New analysis wizard
# ---------------------------------------------------------------------------

@app.get("/new_analysis")
async def new_analysis_form(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")
    return _page(request, session, "new_analysis")


@app.post("/new_analysis")
async def new_analysis_submit(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")

    form = await request.form()

    def number(field: str) -> float:
        try:
            return float(form.get(field) or 0)
        except (TypeError, ValueError):
            return 0.0

    if form.get("unit_sys") == "imperial":
        height_cm = round(number("height_ft") * 30.48 + number("height_in") * 2.54, 1)
        weight_kg = round(number("weight_lbs") * 0.453592, 1)
    else:
        height_cm = round(number("height_cm"), 1)
        weight_kg = round(number("weight_kg"), 1)

    # web.py accepted whatever arrived, so a zero or absurd figure went straight
    # into the Gemini prompt. Bounds here keep the request sane before it costs
    # an API call.
    if not (50 <= height_cm <= 250) or not (20 <= weight_kg <= 250):
        return _error_page(
            request, session, "Check your measurements",
            "Height must be between 50-250cm and weight between 20-250kg.",
            back="/new_analysis",
        )

    skill = form.get("skill") or "Unspecified"
    if skill not in SKILL_LEVELS:
        skill = "Unspecified"

    session["pending_stats"] = {"height": height_cm, "weight": weight_kg, "skill": skill}
    return _redirect(request, "/upload_video", session)


@app.get("/upload_video")
async def upload_video_page(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")
    if "pending_stats" not in session:
        return _redirect(request, "/new_analysis")

    inv = await dbx.get_inventory(_database(request), session["email"])
    return _page(request, session, "upload_video", inv=inv)


# ---------------------------------------------------------------------------
# Analysis API — the three steps the upload page drives
# ---------------------------------------------------------------------------

@app.post("/api/analysis/start")
async def analysis_start(request: Request):
    """Spend the bundles and open a session row.

    Charging up front is deliberate, and it is what the original app's
    "SecureToken" copy on the shop page promises: the bundle is only really
    gone once the footage is accepted. Every failure path from here on refunds.
    """
    session = _session(request)
    if not session.get("email"):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)

    stats = session.get("pending_stats")
    if not stats:
        return JSONResponse({"detail": "Start a new analysis first."}, status_code=400)

    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        payload = {}

    count = int(payload.get("count") or 0)
    if not 1 <= count <= 5:
        return JSONResponse({"detail": "Select between 1 and 5 videos."}, status_code=400)

    is_pro = payload.get("tier") == "pro"
    kind = "coach" if is_pro else "ai"
    email = session["email"]
    database = _database(request)

    # Gemini's free tier allows a few hundred requests a day. Refusing here
    # keeps a busy day from silently turning into a bill, and gives the surfer a
    # real answer instead of an analysis that fails later for reasons they
    # cannot see. Set DAILY_ANALYSIS_CAP to 0 to lift it.
    cap = int(_var(request, "DAILY_ANALYSIS_CAP", "0") or 0)
    if cap and await dbx.analyses_today(database) + count > cap:
        return JSONResponse(
            {"detail": "This demo has hit its analysis limit for today. Please try again tomorrow."},
            status_code=429,
        )

    if not await dbx.spend_bundle(database, email, kind, count):
        return JSONResponse(
            {"detail": f"You do not have {count} {kind} bundle(s) available."},
            status_code=402,
        )

    surfer_id = await dbx.create_surfer(
        database,
        email=email,
        height_cm=stats["height"],
        weight_kg=stats["weight"],
        skill=stats["skill"],
        is_pro=is_pro,
        bundle_used=kind,
    )

    # Remember what was charged, so /submit can refund precisely if the upload
    # never completes.
    session["upload"] = {"surfer_id": surfer_id, "kind": kind, "count": count}
    return _with_session(
        JSONResponse({"surfer_id": surfer_id, "expected_videos": count}), request, session
    )


@app.post("/api/analysis/{surfer_id}/submit")
async def analysis_submit(request: Request, surfer_id: int):
    """Mark the finished upload ready for analysis.

    This is where threading.Thread used to be. A Worker cannot keep a thread
    alive past its response, so the job is recorded in D1 and a Cron Trigger
    picks it up — which also buys the retries and stale-claim recovery a bare
    thread never had. See db.claim_next_job.
    """
    session = _session(request)
    if not session.get("email"):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)

    claim = session.get("upload") or {}
    if claim.get("surfer_id") != surfer_id:
        return JSONResponse({"detail": "Unknown upload."}, status_code=400)

    database = _database(request)
    surfer = await dbx.get_surfer(database, surfer_id)
    if not surfer or surfer["user_email"] != session["email"]:
        return JSONResponse({"detail": "Unknown upload."}, status_code=404)

    videos = await dbx.videos_for_surfer(database, surfer_id)
    if not videos:
        # Nothing arrived: undo the charge and drop the row.
        await dbx.adjust_bundles(database, session["email"], claim["kind"], claim["count"])
        await dbx.delete_surfer(database, surfer_id)
        session.pop("upload", None)
        return _with_session(
            JSONResponse({"detail": "No videos were uploaded."}, status_code=400),
            request, session,
        )

    # Marks the row 'queued'. The Cron Trigger in worker.py sweeps it within a
    # minute. Cloudflare Queues would be the natural home for this, but it is
    # not on the Workers free plan and this project runs entirely free.
    await dbx.enqueue_analysis(database, surfer_id)

    session.pop("upload", None)
    session.pop("pending_stats", None)
    return _with_session(JSONResponse({"queued": True}), request, session)


# ---------------------------------------------------------------------------
# Shop and payments
# ---------------------------------------------------------------------------

@app.get("/shop")
async def shop(request: Request):
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")

    inv = await dbx.get_inventory(_database(request), session["email"])
    prices = {
        key: f"${bundle['amount_cents'] / 100:,.2f}".rstrip("0").rstrip(".")
        for key, bundle in payments.BUNDLES.items()
    }
    return _page(request, session, "shop", inv=inv, prices=prices)


@app.post("/buy/{bundle_key}")
async def buy(request: Request, bundle_key: str):
    """Start a Checkout Session and send the browser to Stripe."""
    session = _session(request)
    if not session.get("email"):
        return _redirect(request, "/")
    if bundle_key not in payments.BUNDLES:
        return _error_page(request, session, "Unknown bundle",
                           "That product does not exist.", back="/shop", status=404)

    try:
        url = await payments.create_checkout_session(
            _env(request),
            bundle_key=bundle_key,
            email=session["email"],
            base_url=_base_url(request),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Stripe checkout creation failed: {exc}")
        return _error_page(request, session, "Checkout unavailable",
                           "We could not reach the payment provider. No charge was made.",
                           back="/shop", status=502)

    return _redirect(request, url, session)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """The only route that grants a paid bundle.

    Returns 200 for anything already handled or not interesting, so Stripe stops
    retrying; 400 only when the signature does not verify.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    secret = _var(request, "STRIPE_WEBHOOK_SECRET")

    if not payments.verify_signature(raw, request.headers.get("stripe-signature", ""), secret):
        print("Rejected a Stripe webhook with an invalid signature.")
        return JSONResponse({"detail": "Invalid signature."}, status_code=400)

    event = payments.parse_event(raw)
    try:
        status = await payments.handle_event(_database(request), event)
    except Exception as exc:  # noqa: BLE001
        # A 500 makes Stripe retry, which is what we want for a transient
        # database error — the idempotency table makes the retry safe.
        print(f"Webhook {event.get('id')} failed: {exc}")
        return JSONResponse({"detail": "Processing failed."}, status_code=500)

    print(f"Stripe webhook {event.get('id')}: {status}")
    return JSONResponse({"received": True, "status": status})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/admin")
async def admin_panel(request: Request):
    session = _session(request)
    if not _is_admin(request, session):
        return _redirect(request, "/")

    database = _database(request)
    pending = await dbx.list_pending_surfers(database)

    # The template iterates s.videos; the ORM used to populate that lazily.
    for surfer in pending:
        surfer["videos"] = await dbx.videos_for_surfer(database, surfer["id"])

    return _page(
        request, session, "admin",
        pending=pending,
        inventories=await dbx.all_inventories(database),
        zoom_chats=await dbx.zoom_threads(database),
    )


@app.post("/admin_reply_zoom")
async def admin_reply_zoom(request: Request):
    session = _session(request)
    if not _is_admin(request, session):
        return _redirect(request, "/")

    form = await request.form()
    target, message = (form.get("target_email") or "").strip(), (form.get("message") or "").strip()
    if target and message:
        await dbx.add_zoom_message(_database(request), target, "admin", message[:2000])
    return _redirect(request, "/admin")


@app.post("/admin_decide/{surfer_id}")
async def admin_decide(request: Request, surfer_id: int):
    """The coach's final sizing call, which also emails the surfer."""
    from mailer import send_email

    session = _session(request)
    if not _is_admin(request, session):
        return _redirect(request, "/")

    database = _database(request)
    surfer = await dbx.get_surfer(database, surfer_id)
    if not surfer:
        return _error_page(request, session, "Not found",
                           "That session no longer exists.", back="/admin", status=404)

    form = await request.form()
    try:
        rec = {
            "rec_feet": int(form.get("ft")),
            "rec_inches": float(form.get("in")),
            "rec_liters": float(form.get("lit")),
            "skill_assessment_text": (form.get("admin_notes") or "").strip(),
        }
    except (TypeError, ValueError):
        return _error_page(request, session, "Invalid numbers",
                           "Feet, inches and liters must all be numbers.",
                           back="/admin", status=400)

    await dbx.set_final_recommendation(database, surfer_id, rec)

    await send_email(
        _env(request),
        surfer["user_email"],
        "Your Pro Surf Analysis Results",
        f"Hi there!\nOur expert has manually analyzed your video.\n\n"
        f"\U0001f30a Volume: {rec['rec_liters']}L\n"
        f"\U0001f4cf Length: {rec['rec_feet']}'{rec['rec_inches']}\"\n\n"
        f"Expert Notes:\n{rec['skill_assessment_text']}\n\nGo shred!\n- The Surfing AI Team",
    )
    return _redirect(request, "/admin")


@app.post("/admin_update_inventory")
async def admin_update_inventory(request: Request):
    session = _session(request)
    if not _is_admin(request, session):
        return _redirect(request, "/")

    form = await request.form()
    target = (form.get("email") or "").strip().lower()
    kind = form.get("bundle_type")
    try:
        amount = int(form.get("amount"))
    except (TypeError, ValueError):
        return _redirect(request, "/admin")

    if target and kind in dbx.BUNDLE_COLUMNS:
        await dbx.set_bundles(_database(request), target, kind, amount)
    return _redirect(request, "/admin")


@app.post("/admin_delete/{surfer_id}")
async def admin_delete(request: Request, surfer_id: int):
    """Delete a session and refund the bundle it consumed."""
    session = _session(request)
    if not _is_admin(request, session):
        return _redirect(request, "/")

    database = _database(request)
    surfer = await dbx.get_surfer(database, surfer_id)
    if not surfer:
        return _redirect(request, "/admin")

    # Drop the stored clips too. They are by far the largest thing this app
    # keeps, and the KV free tier is 1 GB for the whole account.
    videos = await dbx.videos_for_surfer(database, surfer_id)
    video_store = storage.store_for(_env(request))
    for video in videos:
        await video_store.delete(video["object_key"])

    await dbx.adjust_bundles(database, surfer["user_email"], surfer["bundle_used"], 1)
    await dbx.delete_surfer(database, surfer_id)
    return _redirect(request, "/admin")
