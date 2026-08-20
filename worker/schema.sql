-- D1 schema for SurfBoard-Measurement.
--
-- This is the SQLAlchemy model set from web.py written out as plain SQLite DDL.
-- D1 *is* SQLite, so the column types carry over unchanged; the only real
-- differences are that booleans become INTEGER 0/1 and timestamps become TEXT
-- holding an ISO-8601 UTC string, because D1 has no native BOOLEAN or DATETIME.
--
-- Apply with:
--   npx wrangler d1 execute surfboard-db --remote --file=./schema.sql

PRAGMA foreign_keys = ON;

-- One row per analysis request (the old `Surfer` model).
CREATE TABLE IF NOT EXISTS surfer (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email        TEXT    NOT NULL,
    timestamp         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    height_cm         REAL    NOT NULL,
    weight_kg         REAL    NOT NULL,
    skill_level       TEXT    NOT NULL,
    is_pro            INTEGER NOT NULL DEFAULT 0,
    bundle_used       TEXT    NOT NULL DEFAULT 'ai',

    -- What the model proposed. On the coach tier this is only shown to the
    -- admin, as a starting point for the human decision.
    ai_rec_liters     REAL,
    ai_rec_feet       INTEGER,
    ai_rec_inches     REAL,
    ai_rec_message    TEXT,

    video_motion_score REAL   NOT NULL DEFAULT 0.0,

    -- The recommendation the surfer actually sees. NULL means "still pending",
    -- which is exactly what the admin queue filters on.
    rec_liters        REAL,
    rec_feet          INTEGER,
    rec_inches        REAL,
    rec_message       TEXT,

    -- Set when the queue consumer fails permanently, so the dashboard can show
    -- something better than a row that hangs on "pending" forever.
    status            TEXT    NOT NULL DEFAULT 'pending',
    error_message     TEXT
);

-- The dashboard lists a surfer's own rows newest-first; the admin queue scans
-- for rec_liters IS NULL. Both are covered here so neither becomes a full scan
-- (D1 bills rows *read*, so an unindexed scan costs real money as data grows).
CREATE INDEX IF NOT EXISTS idx_surfer_email_time ON surfer (user_email, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_surfer_pending    ON surfer (rec_liters) WHERE rec_liters IS NULL;
-- Few-shot examples are pulled from completed pro rows, newest first.
CREATE INDEX IF NOT EXISTS idx_surfer_pro        ON surfer (is_pro, timestamp DESC) WHERE rec_liters IS NOT NULL;

-- Uploaded clips. The bytes live in R2; this table only holds the object key.
CREATE TABLE IF NOT EXISTS surf_video (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    surfer_id    INTEGER NOT NULL REFERENCES surfer(id) ON DELETE CASCADE,
    object_key   TEXT    NOT NULL,
    content_type TEXT    NOT NULL DEFAULT 'video/mp4',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    motion_score REAL    NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_video_surfer ON surf_video (surfer_id);

-- Credit balance per user. Bundles are added by a paid Stripe webhook or by the
-- admin, and spent when an upload is accepted for analysis.
CREATE TABLE IF NOT EXISTS inventory (
    user_email     TEXT PRIMARY KEY,
    ai_bundles     INTEGER NOT NULL DEFAULT 1,
    coach_bundles  INTEGER NOT NULL DEFAULT 0,
    zoom_bundles   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS zoom_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    sender     TEXT NOT NULL CHECK (sender IN ('user', 'admin')),
    message    TEXT NOT NULL,
    timestamp  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_zoom_email_time ON zoom_message (user_email, timestamp ASC);

-- ---------------------------------------------------------------------------
-- Money
-- ---------------------------------------------------------------------------

-- Every Stripe webhook we have already acted on. Stripe retries a webhook until
-- it gets a 2xx, and will happily deliver the same event twice, so the INSERT
-- below is what stops a customer being credited twice for one payment. The
-- PRIMARY KEY does the work: the second INSERT of the same event_id fails, and
-- we skip the credit.
CREATE TABLE IF NOT EXISTS processed_stripe_event (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A local record of money that actually moved. Stripe is the source of truth
-- for payments; this table exists so the admin dashboard and any future
-- reconciliation can answer "what did we sell?" without an API call.
CREATE TABLE IF NOT EXISTS purchase (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email          TEXT    NOT NULL,
    bundle              TEXT    NOT NULL,
    quantity            INTEGER NOT NULL DEFAULT 1,
    amount_total_cents  INTEGER NOT NULL,
    currency            TEXT    NOT NULL DEFAULT 'usd',
    stripe_session_id   TEXT    NOT NULL UNIQUE,
    stripe_payment_intent TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_purchase_email ON purchase (user_email, created_at DESC);
