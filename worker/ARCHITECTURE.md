# Architecture, and what the move from Flask actually changed

The request was to split this into two native Cloudflare services: **D1** for
the dataset and **Python Workers** for the routes. That is the shape of it, plus
somewhere to keep video, which is load-bearing rather than optional.

It all runs on **free plans, with no payment card on any account**. That
constraint shaped two design decisions, and both cost something real:
[the sweeper](#the-sweeper-is-a-queue-you-can-read) and
[storing video in KV](#video-lives-in-kv-which-is-not-a-blob-store).

```
                    ┌──────────────────────────────────┐
   browser ────────►│   Cloudflare Python Worker       │
                    │   FastAPI · Jinja2 · Pyodide     │
                    │                                  │
                    │   src/worker.py   entry + queue  │
                    │   src/app.py      routes         │
                    │   scheduled()  ◄── Cron Trigger  │
                    └───┬───────────┬──────────────┬───┘
                        │           │              │
              ┌─────────▼─┐   ┌─────▼────┐         │
              │    D1     │   │Workers KV│         ▼
              │  dataset  │   │  videos  │   Gemini · Stripe
              │  + jobs   │   └──────────┘        · Resend
              └───────────┘
```

**D1** holds every table: sessions, recommendations, inventory, chat, and the
purchase ledger. It is SQLite, so `schema.sql` is the SQLAlchemy model set
written out as DDL, essentially unchanged.

**Python Workers** runs all the routes. FastAPI is supported natively — the
runtime ships its own ASGI server, so there is no uvicorn.

**Workers KV** holds the video, because a D1 row caps out around 1MB. R2 is the
right tool for this and was the first implementation, but enabling R2 puts a
payment card on the Cloudflare account. KV needs no card — at a price, below.

**A Cron Trigger** fires `scheduled()` once a minute to run queued analyses,
because a Worker cannot keep a thread alive past its response. Cloudflare Queues
is the natural tool and was the first implementation, but it is the one service
here with no free tier.

---

## Every meaningful change

| Flask | Cloudflare | Why it had to change |
| --- | --- | --- |
| SQLAlchemy ORM | `src/db.py`, prepared statements | D1 speaks SQL over a binding. There is no dialect for it. |
| `sqlite:///surfboard_ai.db` | D1 | The Worker filesystem is read-only and not shared between isolates. |
| `f.save(filepath)` to `uploads/` | Workers KV, via `src/storage.py` | Same reason, plus the 128MB memory ceiling. |
| `threading.Thread` | D1 job table + Cron Trigger | Threads die when the response is sent. Queues would fit, but it is paid-only. |
| `google-genai` SDK | REST via `fetch` | The SDK cannot run under Pyodide. |
| `smtplib` to Gmail:465 | Resend HTTPS API | Workers cannot open a raw TCP socket to SMTP. |
| Flask `session` | HMAC-signed cookie (`src/auth.py`) | No server-side session store; the cookie carries its own integrity. |
| Authlib OAuth client | Hand-rolled OIDC (`src/auth.py`) | Authlib's transports do not exist here. The flow is ~40 lines. |
| `render_template_string` | Jinja2 `DictLoader` | Same engine, same templates, no filesystem to read them from. |
| `@app.before_request` CSRF | ASGI middleware | See the body-consumption note below. |
| `href="#"` on the shop | Stripe Checkout | It never charged anyone. See [PAYMENTS.md](PAYMENTS.md). |

---

## Four things worth reading the code for

### Video never becomes a Python object

A Worker gets 128MB, and Pyodide makes Python objects expensive. Video bytes stay
on the JavaScript side of the boundary the whole way through:

- **Upload.** `PUT /api/analysis/{id}/video` is intercepted in `worker.py`
  *before* the ASGI app sees it. Routing it through FastAPI would read the body
  into Python; handled at the raw request level it is a JS `ArrayBuffer` that
  goes straight to the store. (With R2 this was a `ReadableStream` and nothing
  was held whole at all — see the KV section below for why that changed.)
- **To Gemini.** The sweeper opens the stored clip as a `ReadableStream` and
  hands it to `fetch()` as the request body of a resumable upload. Storage →
  Gemini, nothing in between.
- **Playback.** `GET /video_serve/{id}` returns that stream directly to the
  browser.

The upload cap fell from the Flask app's 500MB to 20MB across two steps: 100MB
is Cloudflare's request-body limit on Free and Pro plans, enforced before your
code runs, and 25 MiB is Workers KV's per-value ceiling, which is the binding
constraint now.

### Video lives in KV, which is not a blob store

R2 is the correct home for uploaded video and this was written against it first.
It is not used because turning R2 on requires a payment card on the Cloudflare
account, even to stay inside the free tier. Workers KV is included in the
Workers free plan with no card.

KV is a configuration and cache store. Using it for video works at this scale
and is honestly a misuse, so `src/storage.py` exists as a seam: it is the only
file that knows where bytes live, and swapping providers is a rewrite of that
one file.

What the substitution costs:

| | R2 | Workers KV |
| --- | --- | --- |
| Max object | 5 TB | **25 MiB** |
| Free storage | 10 GB | **1 GB** (~50 clips at the 20MB cap) |
| Free writes | 1M Class A/month | **1,000/day** |
| Range requests | Yes | **No** — no seeking into a partial download |
| Consistency | Strong | **Eventual**, up to 60s between locations |
| Expiry | Lifecycle rules | `expirationTtl`, per key |
| Payment card | Required | Not required |

Two of those changed the code, not just the config.

**Uploads buffer now.** R2 took the request body as a `ReadableStream`, so a
clip passed from socket to bucket without ever existing whole. KV needs the
complete value, so `worker.py` reads an `ArrayBuffer` instead. The bytes stay a
JavaScript object and are never converted into Python — 20MB costs 20MB, well
inside the Worker's 128MB — and holding it buys something back: the real size is
checked *before* the write rather than failing partway through it.

**A missing clip is no longer evidence.** This is the subtle one. Under R2, `get`
returning None meant the object was gone. Under KV it can also mean *not here
yet* — a write is visible immediately where it was made, but takes up to 60
seconds to reach other locations, and the sweeper may well run somewhere else.

The old code treated a missing video as `{"is_surfing": False}`, which refunds
the bundle and deletes the session. Under KV that would quietly destroy perfectly
good sessions whenever propagation lost a race with the cron. So `storage.open()`
raises `VideoMissing` rather than returning None, the sweeper lets it propagate,
and the job is retried on the next tick instead of judged. After `MAX_ATTEMPTS`
the session fails *and the bundle is refunded* — the surfer is never charged for
storage we could not read.

`tests/test_sweeper.py` models this directly: it hides a key from `get` while
leaving it written, and asserts the session survives, is requeued, keeps its
clip, and completes normally once propagation catches up.

The last thing KV gives back is `expirationTtl`, which R2 has no equivalent for.
Every clip carries an expiry, so storage plateaus instead of growing until it
falls out of the free tier. For a demo that is the right default.

### The sweeper is a queue you can read

Cloudflare Queues has no free tier, and this project runs at zero cost. So the
job lives in the `surfer` table — a `status` column, an `attempts` counter, and
a `claim_token` — and a Cron Trigger sweeps it every minute.

Writing your own queue is usually a bad idea. It is defensible here because only
three properties actually matter, and each is one SQL statement.

**Exactly-once claiming.** Two cron ticks must never both take the same job:

```sql
UPDATE surfer
   SET status='processing', claim_token=?, claimed_at=?, attempts=attempts+1
 WHERE id = (SELECT id FROM surfer
              WHERE status='queued'
                 OR (status='processing' AND claimed_at < ?)   -- stale
              ORDER BY timestamp ASC LIMIT 1)
```

One statement, and SQLite serialises writes, so the loser updates zero rows and
gets `None`. A `SELECT` then `UPDATE` would leave a window where both ticks
believe they own the job — and running Gemini twice on one clip costs money.

**Retries.** `release_job()` puts a failed job back as `queued` and bumps
`attempts`. Past `MAX_ATTEMPTS` it becomes `failed` with the error recorded,
which is what the dead-letter queue was for.

**Recovery from a dead worker.** The `claimed_at < ?` arm of that `WHERE` is a
visibility timeout: a job claimed more than `CLAIM_TIMEOUT_SECONDS` ago is fair
game again. Without it, a sweeper killed mid-analysis would strand its job
forever. The timeout is 10 minutes — comfortably longer than the slowest
realistic analysis, because reclaiming a job that is merely slow means paying
Gemini twice.

The costs of doing it this way, stated plainly: a job waits up to a minute
before it starts, one job runs per tick, and the retry backoff is fixed at one
minute rather than exponential. For a portfolio site none of those matter. If
this ever needed throughput, `wrangler.jsonc` gains a `queues` block, `db.py`
loses about sixty lines, and the sweeper becomes a `queue()` handler again.

`tests/test_sweeper.py` covers all three properties, including two workers
racing for one job and a stale claim being rescued.

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
submitted = _form_fields(body, request.headers.get("content-type", "")).get("_csrf_token")
receive = _replay(body)          # hand the route an unread body
```

`tests/test_app.py` covers it: post a zoom message, then assert it is actually
in the database.

### Forms are parsed without `python-multipart`

`Request.form()` used to be what read that token, and every form POST on the
deployed app — Buy, the stats wizard, every admin action — returned a 500:

```
AssertionError: The `python-multipart` library must be installed to use form parsing.
```

Starlette asserts that package is present before it will parse *any* form body,
including `application/x-www-form-urlencoded`, which needs none of what it does.
It is not in the Worker bundle. Off-platform it happened to be installed, so the
tests were green while production was not — the worst shape a dependency can be
in.

`_form_fields()` in `app.py` parses urlencoded bodies with `urllib.parse.parse_qsl`
and the dependency is gone. Nothing here posts multipart: video uploads bypass
ASGI entirely and arrive as raw `PUT` bodies, so the only form encoding the app
ever sees is the one a `<form>` sends by default. A multipart body yields no
fields rather than raising, which the CSRF gate turns into a 403.

The suite now runs *without* `python-multipart` installed, deliberately, so the
tests exercise the same parser production does.

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

- **Runs entirely on free plans.** Rendering the heaviest page measures ~0.5ms
  of CPU against the free plan's 10ms budget, so server-side rendering is not
  the problem I first assumed it was. The one paid-only service, Queues, is
  replaced by the sweeper above.
- **Analyses start up to a minute late**, because one minute is the fastest a
  Cron Trigger fires.
- **20MB per upload** by default (`MAX_UPLOAD_BYTES`), under KV's hard 25 MiB
  per-value ceiling. Cloudflare's own request-body limit is 100MB on Free and
  Pro plans; KV is the tighter constraint here.
- **~50 clips of storage**, and clips expire after `VIDEO_TTL_DAYS`. Moving to
  R2, Supabase Storage or Cloudinary is a rewrite of `src/storage.py` alone.
- **No video seeking.** KV cannot serve a byte range, so a browser downloads a
  clip whole before it can scrub. Fine at 20MB; not fine at 200MB.
- **40 analyses per day** by default (`DAILY_ANALYSIS_CAP`), guarding Gemini's
  free quota. Set it to `0` to lift the cap.
- **Python Workers are in open beta.** The `python_workers` compatibility flag
  is required, and the API surface can still shift.
- **Gemini file processing is polled** for up to ~3 minutes. Longer clips hit
  the ceiling and land on the dead-letter queue.
- **ID token signatures are not verified** in `claims_from_id_token()`. That is
  sound only because the token is received directly from Google's token endpoint
  over TLS in exchange for a client secret. If an ID token ever reaches this app
  another way, verify against Google's JWKS first — the docstring says so.
