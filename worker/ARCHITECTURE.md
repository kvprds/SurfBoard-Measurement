# Architecture, and what the move from Flask actually changed

The request was to split this into two native Cloudflare services: **D1** for
the dataset and **Python Workers** for the routes. That is the shape of it. Two
more bindings turned out to be load-bearing, and they are called out below
rather than folded in quietly.

```
                    ┌──────────────────────────────────┐
   browser ────────►│   Cloudflare Python Worker       │
                    │   FastAPI · Jinja2 · Pyodide     │
                    │                                  │
                    │   src/worker.py   entry + queue  │
                    │   src/app.py      routes         │
                    └───┬───────┬───────┬──────────┬───┘
                        │       │       │          │
              ┌─────────▼─┐ ┌───▼────┐ ┌▼────────┐ │
              │    D1     │ │   R2   │ │ Queues  │ │
              │  dataset  │ │ videos │ │ analysis│ │
              └───────────┘ └────────┘ └────┬────┘ │
                                            │      │
                                   consumer ┘      │
                                                   ▼
                                    Gemini · Stripe · Resend
```

**D1** holds every table: sessions, recommendations, inventory, chat, and the
purchase ledger. It is SQLite, so `schema.sql` is the SQLAlchemy model set
written out as DDL, essentially unchanged.

**Python Workers** runs all the routes. FastAPI is supported natively — the
runtime ships its own ASGI server, so there is no uvicorn.

**R2** exists because a D1 row caps out around 1MB. Video cannot live in the
database. R2's egress is free, which is what keeps playback off the cost sheet.

**Queues** exists because a Worker cannot keep a thread alive past its response.
It also brings retries and a dead-letter queue, which the original
`threading.Thread` never had.

---

## Every meaningful change

| Flask | Cloudflare | Why it had to change |
| --- | --- | --- |
| SQLAlchemy ORM | `src/db.py`, prepared statements | D1 speaks SQL over a binding. There is no dialect for it. |
| `sqlite:///surfboard_ai.db` | D1 | The Worker filesystem is read-only and not shared between isolates. |
| `f.save(filepath)` to `uploads/` | Streamed into R2 | Same reason, plus the 128MB memory ceiling. |
| `threading.Thread` | Cloudflare Queue + consumer | Threads die when the response is sent. |
| `google-genai` SDK | REST via `fetch` | The SDK cannot run under Pyodide. |
| `smtplib` to Gmail:465 | Resend HTTPS API | Workers cannot open a raw TCP socket to SMTP. |
| Flask `session` | HMAC-signed cookie (`src/auth.py`) | No server-side session store; the cookie carries its own integrity. |
| Authlib OAuth client | Hand-rolled OIDC (`src/auth.py`) | Authlib's transports do not exist here. The flow is ~40 lines. |
| `render_template_string` | Jinja2 `DictLoader` | Same engine, same templates, no filesystem to read them from. |
| `@app.before_request` CSRF | ASGI middleware | See the body-consumption note below. |
| `href="#"` on the shop | Stripe Checkout | It never charged anyone. See [PAYMENTS.md](PAYMENTS.md). |

---

## Three things worth reading the code for

### Video never enters Python memory

A Worker gets 128MB. A single clip can exceed that, so the bytes are never
materialised on either leg of the journey:

- **Upload.** `PUT /api/analysis/{id}/video` is intercepted in `worker.py`
  *before* the ASGI app sees it. Routing it through FastAPI would read the body
  into Python; handled at the raw request level, `request.body` stays a
  `ReadableStream` and goes straight to `env.VIDEOS.put()`.
- **To Gemini.** The consumer opens the R2 object and hands `obj.body` to
  `fetch()` as the request body of a resumable upload. R2 → Gemini, nothing in
  between.
- **Playback.** `GET /video_serve/{id}` returns the R2 stream directly, and
  forwards `Range` so scrubbing works without pulling the whole file.

This is also why the upload cap moved from 500MB to 100MB: that is Cloudflare's
request body limit on Free and Pro plans, and it is enforced before your code
runs. Beyond it you need presigned URLs straight to R2, which is a bigger
change than this migration needed.

### The CSRF middleware is not `@app.middleware("http")`

It looks like it should be, and it was, and it was broken.

Starlette's `BaseHTTPMiddleware` gives you a `Request`. Calling `await
request.form()` on it to read the CSRF token consumes the receive channel. The
route handler that runs next builds its own `Request` from the same scope, finds
the body already drained, and sees an **empty form** — so every field the user
submitted silently vanishes while the request still returns 200.

`SecurityMiddleware` in `app.py` is pure ASGI and replays the body:

```python
body = await request.body()
form = await Request(scope, _replay(body)).form()
submitted = form.get("_csrf_token")
receive = _replay(body)          # hand the route an unread body
```

`tests/test_app.py` covers it: post a zoom message, then assert it is actually
in the database.

### Bundles are spent in SQL, not in Python

The Flask version did read-modify-write:

```python
inv.ai_bundles -= required_bundles      # two requests, one lost update
```

Two overlapping requests both read the same starting balance, and one write is
lost. For something a customer paid for, that is worth closing:

```python
UPDATE inventory SET ai_bundles = ai_bundles - ?
 WHERE user_email = ? AND ai_bundles >= ?
```

The guard is in the `WHERE` clause, so the check and the decrement are one
statement. `changes = 0` means there was not enough balance. Same idea as the
webhook's `INSERT OR IGNORE`, applied to spending instead of granting.

---

## Fixed along the way

Small things, all in code that had to be touched anyway:

- **`/video_serve/<id>` had no access control.** Any visitor, signed in or not,
  could read any video by guessing an integer. It now requires the owner or the
  admin.
- **`send_email` swallowed everything** with a bare `except: pass`. Failures are
  logged now.
- **Height and weight were unvalidated**, so `0cm / 0kg` went into the Gemini
  prompt and cost an API call. Bounds are checked before anything is spent.
- **The admin panel ran a query per chat participant** inside a loop. One query,
  grouped in Python. D1 bills per row read.
- **Session cookies were `Secure = False`**, hardcoded. Workers are always
  HTTPS; the flag is on.
- **The upload "SecureToken"** was a session-stored nonce that prevented double
  submits but did nothing on failure. Bundles are now refunded on every failure
  path — no video uploaded, not surfing, or Gemini erroring.

---

## Known limits

- **Workers Paid ($5/mo) is required**, not optional. The free plan's 10ms CPU
  budget is below what server-side rendering costs, and Queues is not on the
  free plan at all.
- **100MB per upload.** A Cloudflare account-plan limit, enforced upstream of
  your code.
- **Python Workers are in open beta.** The `python_workers` compatibility flag
  is required, and the API surface can still shift.
- **Gemini file processing is polled** for up to ~3 minutes. Longer clips hit
  the ceiling and land on the dead-letter queue.
- **ID token signatures are not verified** in `claims_from_id_token()`. That is
  sound only because the token is received directly from Google's token endpoint
  over TLS in exchange for a client secret. If an ID token ever reaches this app
  another way, verify against Google's JWKS first — the docstring says so.
