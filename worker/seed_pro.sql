-- Pro sizing decisions — the few-shot grounding for Gemini.
--
-- src/db.py:coach_examples() reads `is_pro = 1` rows and src/gemini.py turns
-- them into the "here is how the head coach sized these boards" block of the
-- prompt. With no such rows the prompt degrades to "No historical data yet.
-- Use your best judgment", which is the one thing the few-shot design exists
-- to avoid. schema.sql even carries a dedicated index (idx_surfer_pro) for
-- this query, but nothing ever populated it.
--
-- The measurements come from the dataset that used to live in
-- SurfBoard-Measurement/seed_db.py — ten professional surfers, each with the
-- height, weight, volume and length recorded for them. That script imported
-- functions web.py no longer defined, so it raised ImportError and never ran;
-- the data it carried never reached a database. This file is that data in a
-- form D1 executes.
--
-- NOTE ON rec_message: the source dataset stored the literal placeholder
-- "Auto-trained data" as its reasoning, which is useless as a few-shot
-- example. The notes below are derived from the recorded numbers themselves
-- (volume-to-weight ratio, board length against rider height). They are not
-- transcribed commentary from the coach — no such text ever existed in the
-- dataset. Replace any row's rec_message with the coach's own words when you
-- have them; the sizing figures are the part that came from the coach.
--
-- Re-runnable: the DELETE clears a previous seed without touching real rows,
-- which are never owned by this address.
--
--   npx wrangler d1 execute surfboard-db --remote --file=./seed_pro.sql

DELETE FROM surfer WHERE user_email = 'seed-pro@surfboard.local';

INSERT INTO surfer (
    user_email, timestamp, height_cm, weight_kg, skill_level, is_pro,
    bundle_used, status, rec_liters, rec_feet, rec_inches, rec_message
) VALUES
('seed-pro@surfboard.local', '2024-01-01T00:00:00.000Z', 175, 75, 'Expert', 1, 'coach', 'complete',
 26.8, 5, 9.0,
 'Kelly Slater — 26.8L at 75kg, a 0.36 L/kg ratio. Board length 5''9" matches rider height almost exactly. Textbook high-performance shortboard sizing: minimum paddle float, maximum rail engagement.'),

('seed-pro@surfboard.local', '2024-01-02T00:00:00.000Z', 180, 77, 'Expert', 1, 'coach', 'complete',
 28.5, 5, 11.0,
 'Gabriel Medina — 28.5L at 77kg, 0.37 L/kg. Length 5''11" sits level with his height. Volume kept low enough that the board sinks on demand for vertical turns rather than skating over the section.'),

('seed-pro@surfboard.local', '2024-01-03T00:00:00.000Z', 185, 82, 'Expert', 1, 'coach', 'complete',
 30.5, 6, 2.0,
 'John John Florence — 30.5L at 82kg, 0.37 L/kg. At 6''2" the board runs about an inch past his height; the extra length is carrying the heavier frame through bigger, faster faces, not adding float.'),

('seed-pro@surfboard.local', '2024-01-04T00:00:00.000Z', 175, 77, 'Expert', 1, 'coach', 'complete',
 25.5, 5, 7.0,
 'Italo Ferreira — 25.5L at 77kg, 0.33 L/kg, the lowest ratio in this set, on a 5''7" that is two inches under his height. Deliberately under-volumed for an air-oriented, extremely quick-footed surfer.'),

('seed-pro@surfboard.local', '2024-01-05T00:00:00.000Z', 175, 75, 'Expert', 1, 'coach', 'complete',
 26.8, 5, 9.0,
 'Kelly Slater at Lowers — same 26.8L and 5''9" as his baseline. Held identical across a different wave, which is the point: at this level the sizing tracks the surfer, not the break.'),

('seed-pro@surfboard.local', '2024-01-06T00:00:00.000Z', 180, 78, 'Expert', 1, 'coach', 'complete',
 28.5, 5, 11.0,
 'Griffin Colapinto — 28.5L at 78kg, 0.37 L/kg, 5''11" level with his height. Sits right on the middle of the professional band; a useful anchor for an intermediate surfer of similar build scaling up.'),

('seed-pro@surfboard.local', '2024-01-07T00:00:00.000Z', 170, 64, 'Expert', 1, 'coach', 'complete',
 26.5, 5, 9.0,
 'Carissa Moore — 26.5L at 64kg, 0.41 L/kg, the highest ratio here, on a 5''9" that runs two inches over her height. Lighter riders need proportionally more volume to hold paddle speed; the ratio is not constant across body weights.'),

('seed-pro@surfboard.local', '2024-01-08T00:00:00.000Z', 178, 67, 'Expert', 1, 'coach', 'complete',
 24.5, 5, 10.0,
 'Stephanie Gilmore — 24.5L at 67kg, 0.37 L/kg, on a 5''10" matching her height. The lowest absolute volume in the set: a tall, light frame on a rail-driven board built for flow rather than pop.'),

('seed-pro@surfboard.local', '2024-01-09T00:00:00.000Z', 180, 78, 'Expert', 1, 'coach', 'complete',
 29.6, 6, 0.0,
 'Kanoa Igarashi — 29.6L at 78kg, 0.38 L/kg, on a 6''0" an inch over his height. Slightly more float than Colapinto at identical stats — a reminder that two surfers of the same build get different boards depending on how they surf.'),

('seed-pro@surfboard.local', '2024-01-10T00:00:00.000Z', 180, 81, 'Expert', 1, 'coach', 'complete',
 30.0, 6, 0.0,
 'Jack Robinson — 30.0L at 81kg, 0.37 L/kg, 6''0" just over his height. Heavier build, powerful rail surfing in serious waves; volume scales with weight while the ratio stays put.');
