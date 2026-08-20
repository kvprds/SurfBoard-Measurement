"""D1 data access — the replacement for the SQLAlchemy models in web.py.

There is no ORM here on purpose. D1 speaks prepared statements over a binding,
and every row a query *scans* is billed, so it is worth being able to see the
exact SQL each page runs.

Everything is a plain dict, keyed by column name.
"""

import json
from datetime import datetime, timedelta, timezone


def utcnow() -> str:
    """ISO-8601 UTC, matching the DEFAULT expressions in schema.sql."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _to_py(value):
    """Normalise a value handed back across the JavaScript boundary.

    D1 returns a JS object. Depending on the runtime version it arrives either
    already converted, wrapped in the SDK's binding wrapper, or as a raw
    JsProxy, so unwrap defensively rather than assuming one shape.
    """
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value

    # workers-runtime-sdk wraps binding return values; the JS object is inside.
    inner = getattr(value, "_binding", value)

    to_py = getattr(inner, "to_py", None)
    if callable(to_py):
        return to_py()

    # Last resort: round-trip through JSON, which every D1 result survives.
    try:
        import js

        return json.loads(js.JSON.stringify(inner))
    except Exception:
        return inner


def _param(value):
    """Coerce a Python value into something D1's bind() accepts.

    D1 takes null, numbers, strings and ArrayBuffers. SQLite has no boolean, so
    True/False have to become 1/0 or the bind throws D1_TYPE_ERROR.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return value


class Database:
    """Thin wrapper around the D1 binding (`env.DB`)."""

    def __init__(self, binding):
        self._db = binding

    def _stmt(self, sql: str, params):
        stmt = self._db.prepare(sql)
        if params:
            stmt = stmt.bind(*[_param(p) for p in params])
        return stmt

    async def query(self, sql: str, *params) -> list[dict]:
        """Run a SELECT and return every row as a dict."""
        result = _to_py(await self._stmt(sql, params).all())
        if isinstance(result, dict):
            return result.get("results") or []
        return result or []

    async def first(self, sql: str, *params) -> dict | None:
        rows = await self.query(sql, *params)
        return rows[0] if rows else None

    async def execute(self, sql: str, *params) -> dict:
        """Run an INSERT/UPDATE/DELETE and return D1's meta block.

        The meta block carries `last_row_id`, which is how we recover the id of
        a freshly inserted row without a second round trip.
        """
        result = _to_py(await self._stmt(sql, params).run())
        if isinstance(result, dict):
            return result.get("meta") or {}
        return {}

    async def insert(self, sql: str, *params) -> int:
        """INSERT and return the new row's id."""
        meta = await self.execute(sql, *params)
        return int(meta.get("last_row_id") or 0)

    async def batch(self, statements: list[tuple]) -> None:
        """Run several statements in one D1 round trip, as a transaction.

        `statements` is a list of (sql, params) tuples. D1 wraps a batch in an
        implicit transaction, so this is how multi-table writes stay atomic —
        it is the closest thing D1 has to db.session.commit().
        """
        prepared = [self._stmt(sql, params) for sql, params in statements]
        await self._db.batch(prepared)


# ---------------------------------------------------------------------------
# Inventory — how many bundles a user has left
# ---------------------------------------------------------------------------

# Accepts either the short name used in forms ("ai") or the column itself
# ("ai_bundles"), so callers on both sides of the wire can use the same helper.
BUNDLE_COLUMNS = {
    "ai": "ai_bundles",
    "coach": "coach_bundles",
    "zoom": "zoom_bundles",
}
BUNDLE_COLUMNS.update({v: v for v in list(BUNDLE_COLUMNS.values())})


async def get_inventory(db: Database, email: str) -> dict:
    """Fetch a user's balance, creating the starter row on first sight.

    Mirrors get_inventory() in web.py, including the one free AI bundle that new
    accounts start with. The INSERT is OR IGNORE so two simultaneous requests
    cannot both create the row.
    """
    row = await db.first("SELECT * FROM inventory WHERE user_email = ?", email)
    if row:
        return row

    await db.execute(
        "INSERT OR IGNORE INTO inventory (user_email, ai_bundles, coach_bundles, zoom_bundles)"
        " VALUES (?, 1, 0, 0)",
        email,
    )
    return await db.first("SELECT * FROM inventory WHERE user_email = ?", email)


async def adjust_bundles(db: Database, email: str, column: str, delta: int) -> None:
    """Add (or, with a negative delta, spend) bundles.

    The arithmetic happens inside SQL rather than read-modify-write in Python.
    Two overlapping requests would otherwise both read the same starting balance
    and one of the writes would be lost — which, for something a customer paid
    for, is a bug worth avoiding.

    `column` is never user input; callers pass a literal from BUNDLE_COLUMNS.
    """
    if column not in BUNDLE_COLUMNS:
        raise ValueError(f"refusing to update unknown column {column!r}")
    column = BUNDLE_COLUMNS[column]
    await get_inventory(db, email)
    await db.execute(
        f"UPDATE inventory SET {column} = MAX(0, {column} + ?) WHERE user_email = ?",
        delta,
        email,
    )


async def set_bundles(db: Database, email: str, column: str, amount: int) -> None:
    """Overwrite a balance outright — the admin panel's 'set' behaviour."""
    if column not in BUNDLE_COLUMNS:
        raise ValueError(f"refusing to update unknown column {column!r}")
    column = BUNDLE_COLUMNS[column]
    await get_inventory(db, email)
    await db.execute(
        f"UPDATE inventory SET {column} = ? WHERE user_email = ?",
        max(0, amount),
        email,
    )


async def spend_bundle(db: Database, email: str, kind: str, count: int) -> bool:
    """Atomically consume `count` bundles, or return False if there aren't enough.

    The guard lives in the WHERE clause, so the check and the decrement are a
    single statement. Checking the balance in Python first and then writing
    would leave a window where the same bundle gets spent twice.
    """
    column = BUNDLE_COLUMNS[kind]
    await get_inventory(db, email)
    meta = await db.execute(
        f"UPDATE inventory SET {column} = {column} - ?"
        f" WHERE user_email = ? AND {column} >= ?",
        count,
        email,
        count,
    )
    return int(meta.get("changes") or 0) > 0


# ---------------------------------------------------------------------------
# Surfers (analysis requests)
# ---------------------------------------------------------------------------

async def create_surfer(
    db: Database,
    *,
    email: str,
    height_cm: float,
    weight_kg: float,
    skill: str,
    is_pro: bool,
    bundle_used: str,
) -> int:
    return await db.insert(
        "INSERT INTO surfer"
        " (user_email, timestamp, height_cm, weight_kg, skill_level, is_pro, bundle_used, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        email,
        utcnow(),
        height_cm,
        weight_kg,
        skill,
        is_pro,
        bundle_used,
    )


async def get_surfer(db: Database, surfer_id: int) -> dict | None:
    return await db.first("SELECT * FROM surfer WHERE id = ?", surfer_id)


async def list_surfers_for_user(db: Database, email: str) -> list[dict]:
    return await db.query(
        "SELECT * FROM surfer WHERE user_email = ? ORDER BY timestamp DESC LIMIT 100",
        email,
    )


async def list_pending_surfers(db: Database) -> list[dict]:
    """The admin review queue: anything without a final recommendation."""
    return await db.query(
        "SELECT * FROM surfer WHERE rec_liters IS NULL ORDER BY timestamp ASC LIMIT 200"
    )


async def coach_examples(db: Database, limit: int = 5) -> list[dict]:
    """The coach's most recent sizing decisions, used as few-shot examples.

    This is the whole point of keeping the dataset in D1: the prompt is grounded
    in real decisions a human made, read fresh on every analysis.
    """
    return await db.query(
        "SELECT weight_kg, height_cm, skill_level, rec_feet, rec_inches, rec_liters, rec_message"
        " FROM surfer"
        " WHERE rec_liters IS NOT NULL AND is_pro = 1"
        " ORDER BY timestamp DESC LIMIT ?",
        limit,
    )


async def set_ai_recommendation(db: Database, surfer_id: int, rec: dict) -> None:
    """Store the model's answer in the *ai_* columns (coach tier).

    The surfer does not see these; they are the draft the coach reviews.
    """
    await db.execute(
        "UPDATE surfer SET ai_rec_liters = ?, ai_rec_feet = ?, ai_rec_inches = ?,"
        " ai_rec_message = ?, status = 'awaiting_coach', claim_token = NULL,"
        " error_message = NULL WHERE id = ?",
        rec.get("rec_liters"),
        rec.get("rec_feet"),
        rec.get("rec_inches"),
        rec.get("skill_assessment_text"),
        surfer_id,
    )


async def set_final_recommendation(db: Database, surfer_id: int, rec: dict) -> None:
    """Store the recommendation the surfer sees, completing the request."""
    await db.execute(
        "UPDATE surfer SET rec_liters = ?, rec_feet = ?, rec_inches = ?,"
        " rec_message = ?, status = 'complete', claim_token = NULL,"
        " error_message = NULL WHERE id = ?",
        rec.get("rec_liters"),
        rec.get("rec_feet"),
        rec.get("rec_inches"),
        rec.get("skill_assessment_text"),
        surfer_id,
    )


# ---------------------------------------------------------------------------
# The analysis job queue, built on the surfer table
# ---------------------------------------------------------------------------
#
# Cloudflare Queues would be the obvious tool, but it is not on the Workers free
# plan. A Cron Trigger (free, 5 per account) sweeps this instead. What a queue
# gives you and this has to reproduce: exactly-once claiming, retries, and
# recovery when a worker dies holding a job.

MAX_ATTEMPTS = 3

# How long a claim is honoured before another tick may steal it. Longer than the
# slowest realistic analysis, so a job that is merely slow is never processed
# twice — running Gemini twice on one clip costs real money.
CLAIM_TIMEOUT_SECONDS = 600


async def enqueue_analysis(db: Database, surfer_id: int) -> None:
    """Mark a session ready for the sweeper to pick up."""
    await db.execute(
        "UPDATE surfer SET status = 'queued' WHERE id = ? AND status = 'pending'",
        surfer_id,
    )


async def claim_next_job(db: Database, token: str) -> dict | None:
    """Atomically take ownership of one waiting job, or return None.

    The claim is a single UPDATE. SQLite serialises writes, so two cron ticks
    running at once cannot both come away believing they own the same row — the
    second one updates zero rows and gets None.

    A row stuck in 'processing' past CLAIM_TIMEOUT_SECONDS is fair game again.
    That is the recovery path for a sweeper that was killed mid-analysis; a real
    queue would call it visibility timeout.
    """
    stale_before = _iso_seconds_ago(CLAIM_TIMEOUT_SECONDS)
    now = utcnow()

    meta = await db.execute(
        "UPDATE surfer"
        "   SET status = 'processing', claim_token = ?, claimed_at = ?, attempts = attempts + 1"
        " WHERE id = ("
        "     SELECT id FROM surfer"
        "      WHERE status = 'queued'"
        "         OR (status = 'processing' AND claimed_at < ?)"
        "      ORDER BY timestamp ASC LIMIT 1"
        " )",
        token,
        now,
        stale_before,
    )
    if int(meta.get("changes") or 0) == 0:
        return None

    return await db.first("SELECT * FROM surfer WHERE claim_token = ?", token)


async def release_job(db: Database, surfer_id: int, error: str) -> bool:
    """Hand a failed job back, or give up on it after MAX_ATTEMPTS.

    Returns True if it will be retried, False if it is now marked failed.
    """
    row = await db.first("SELECT attempts FROM surfer WHERE id = ?", surfer_id)
    attempts = int((row or {}).get("attempts") or 0)

    if attempts >= MAX_ATTEMPTS:
        await db.execute(
            "UPDATE surfer SET status = 'failed', error_message = ?, claim_token = NULL"
            " WHERE id = ?",
            error[:500],
            surfer_id,
        )
        return False

    await db.execute(
        "UPDATE surfer SET status = 'queued', error_message = ?, claim_token = NULL,"
        " claimed_at = NULL WHERE id = ?",
        error[:500],
        surfer_id,
    )
    return True


async def analyses_today(db: Database) -> int:
    """How many analyses have been started since midnight UTC.

    Gemini's free tier allows a few hundred requests a day. This is what the
    daily cap in app.py checks, so a burst of traffic degrades into "try again
    tomorrow" rather than a broken demo and a surprise bill.
    """
    row = await db.first(
        "SELECT COUNT(*) AS n FROM surfer WHERE timestamp >= ?",
        datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z"),
    )
    return int((row or {}).get("n") or 0)


def _iso_seconds_ago(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def mark_surfer_failed(db: Database, surfer_id: int, message: str) -> None:
    await db.execute(
        "UPDATE surfer SET status = 'failed', error_message = ? WHERE id = ?",
        message[:500],
        surfer_id,
    )


async def delete_surfer(db: Database, surfer_id: int) -> None:
    """Remove a request and its video rows (ON DELETE CASCADE handles the latter)."""
    await db.execute("DELETE FROM surfer WHERE id = ?", surfer_id)


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

async def add_video(
    db: Database, surfer_id: int, object_key: str, content_type: str, size_bytes: int
) -> int:
    return await db.insert(
        "INSERT INTO surf_video (surfer_id, object_key, content_type, size_bytes, motion_score)"
        " VALUES (?, ?, ?, ?, 100.0)",
        surfer_id,
        object_key,
        content_type,
        size_bytes,
    )


async def get_video(db: Database, video_id: int) -> dict | None:
    return await db.first("SELECT * FROM surf_video WHERE id = ?", video_id)


async def videos_for_surfer(db: Database, surfer_id: int) -> list[dict]:
    return await db.query("SELECT * FROM surf_video WHERE surfer_id = ?", surfer_id)


async def delete_video(db: Database, video_id: int) -> None:
    await db.execute("DELETE FROM surf_video WHERE id = ?", video_id)


# ---------------------------------------------------------------------------
# Zoom chat
# ---------------------------------------------------------------------------

async def add_zoom_message(db: Database, email: str, sender: str, message: str) -> None:
    await db.execute(
        "INSERT INTO zoom_message (user_email, sender, message, timestamp) VALUES (?, ?, ?, ?)",
        email,
        sender,
        message,
        utcnow(),
    )


async def zoom_messages(db: Database, email: str) -> list[dict]:
    return await db.query(
        "SELECT * FROM zoom_message WHERE user_email = ? ORDER BY timestamp ASC LIMIT 500",
        email,
    )


async def all_inventories(db: Database) -> list[dict]:
    return await db.query("SELECT * FROM inventory ORDER BY user_email LIMIT 500")


async def zoom_threads(db: Database) -> dict[str, list[dict]]:
    """Every chat thread, grouped by user, for the admin panel.

    web.py issued one query per participant inside a loop. That is the classic
    N+1, and on D1 each of those round trips is billed, so this pulls the whole
    set once and groups it in Python.
    """
    rows = await db.query(
        "SELECT * FROM zoom_message ORDER BY user_email, timestamp ASC LIMIT 2000"
    )
    threads: dict[str, list[dict]] = {}
    for row in rows:
        threads.setdefault(row["user_email"], []).append(row)

    # Users who bought a Zoom call but have not written anything yet still need
    # to show up in the admin list.
    buyers = await db.query(
        "SELECT user_email FROM inventory WHERE zoom_bundles > 0 LIMIT 500"
    )
    for row in buyers:
        threads.setdefault(row["user_email"], [])
    return threads
