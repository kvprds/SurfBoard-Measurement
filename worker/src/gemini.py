"""Gemini video analysis over the REST API.

The Flask app used the `google-genai` SDK. That SDK cannot run on Workers, so
this speaks the same Files + generateContent endpoints directly.

The important difference from the original is how the bytes move. A Worker gets
128MB of memory, so reading a clip into a Python `bytes` would be expensive at
best. Instead the stored clip's ReadableStream is handed straight to fetch() as
the request body, and the video flows from storage to Gemini without the Worker
ever materialising it.
"""

import asyncio
import json

from httpjs import HttpError, fetch_json, fetch_raw

API_ROOT = "https://generativelanguage.googleapis.com"

# Gemini reports a freshly uploaded file as PROCESSING until it has decoded it.
# Polling mirrors the SDK's own wait loop; the ceiling stops a stuck file from
# holding a queue message open until the consumer times out.
POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90  # ~3 minutes


PROMPT_TEMPLATE = """
You are an expert surfboard shaper. Watch this video.
The user claims their skill level is '{skill}'. They weigh {weight}kg and are {height}cm tall.

First, verify if this is actually a video of someone surfing or attempting to surf in the water.
If it is NOT surfing, set "is_surfing" false and leave the rest blank.

If it IS surfing, calculate the ideal surfboard volume (Liters) and length (Feet and Inches).

CRITICAL INSTRUCTIONS:
Here are examples of how the head coach previously sized boards based on weight, skill, and technique.
Study these past decisions and mimic this exact logic and writing style in your assessment:
{examples}

You MUST respond ONLY with a valid JSON object in this exact format:
{{
    "is_surfing": true,
    "skill_assessment_text": "A paragraph explaining their technique, matching the head coach's style.",
    "rec_liters": 32.5,
    "rec_feet": 6,
    "rec_inches": 2
}}
"""


def format_examples(rows: list[dict]) -> str:
    """Turn the coach's stored decisions into few-shot prompt text.

    Same shape as the string web.py built, just read from D1 instead of the ORM.
    """
    if not rows:
        return "No historical data yet. Use your best judgment."

    lines = []
    for r in rows:
        lines.append(
            f"- Surfer Stats: {r.get('weight_kg')}kg, {r.get('height_cm')}cm, "
            f"{r.get('skill_level')} level. You recommended: "
            f"{r.get('rec_feet')}'{r.get('rec_inches')}\", {r.get('rec_liters')}L. "
            f"Your reasoning: {r.get('rec_message')}"
        )
    return "\n".join(lines)


async def upload_stream(
    api_key: str, *, stream, size_bytes: int, mime_type: str, display_name: str
) -> dict:
    """Push a video into the Gemini Files API using the resumable protocol.

    Two calls: one to open an upload session and get a URL, one to send the
    bytes. `stream` comes from src/storage.py and is never read into Python.
    """
    start = await fetch_raw(
        f"{API_ROOT}/upload/v1beta/files?key={api_key}",
        method="POST",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size_bytes),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
        body=json.dumps({"file": {"display_name": display_name}}),
    )
    if not start.ok:
        raise HttpError(start.status, await start.text(), "gemini upload start")

    upload_url = start.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Gemini did not return an upload URL")

    finish = await fetch_raw(
        upload_url,
        method="POST",
        headers={
            "Content-Length": str(size_bytes),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        body=stream,
    )
    text = await finish.text()
    if not finish.ok:
        raise HttpError(finish.status, text, "gemini upload finalize")

    return json.loads(text).get("file", {})


async def wait_until_active(api_key: str, file_name: str) -> dict:
    """Poll a file until Gemini finishes decoding it."""
    for _ in range(MAX_POLL_ATTEMPTS):
        info = await fetch_json(f"{API_ROOT}/v1beta/{file_name}?key={api_key}")
        state = info.get("state")
        if state == "ACTIVE":
            return info
        if state == "FAILED":
            raise RuntimeError("Video processing failed on the Gemini side.")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError("Gemini did not finish processing the video in time.")


async def analyse(
    api_key: str,
    model: str,
    *,
    file_uri: str,
    mime_type: str,
    weight_kg: float,
    height_cm: float,
    skill: str,
    examples: str,
) -> dict:
    """Ask Gemini for the sizing call and return the parsed JSON."""
    prompt = PROMPT_TEMPLATE.format(
        skill=skill, weight=weight_kg, height=height_cm, examples=examples
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"file_data": {"file_uri": file_uri, "mime_type": mime_type}},
                    {"text": prompt},
                ],
            }
        ],
        # Same guarantee the SDK's response_mime_type gave: valid JSON back,
        # so the result does not have to be scraped out of prose.
        "generationConfig": {"responseMimeType": "application/json"},
    }

    result = await fetch_json(
        f"{API_ROOT}/v1beta/models/{model}:generateContent?key={api_key}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload),
    )

    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        # Usually a safety block or a truncated response.
        raise RuntimeError(f"Unexpected Gemini response shape: {json.dumps(result)[:400]}") from exc

    return json.loads(text)


async def delete_file(api_key: str, file_name: str) -> None:
    """Best-effort cleanup. Gemini expires files after 48h regardless."""
    try:
        await fetch_raw(f"{API_ROOT}/v1beta/{file_name}?key={api_key}", method="DELETE")
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail the job
        print(f"Could not delete Gemini file {file_name}: {exc}")


def normalise_result(raw: dict) -> dict:
    """Validate what the model returned before it reaches the database.

    A model can return a plausible-looking object with a string where a number
    belongs. Coercing here keeps that out of D1 and out of the few-shot examples
    the next analysis will be built from.
    """
    if not raw.get("is_surfing"):
        return {"is_surfing": False}

    def number(value, cast, default=0):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    return {
        "is_surfing": True,
        "skill_assessment_text": str(raw.get("skill_assessment_text") or "").strip(),
        "rec_liters": round(number(raw.get("rec_liters"), float), 1),
        "rec_feet": number(raw.get("rec_feet"), int),
        "rec_inches": round(number(raw.get("rec_inches"), float), 1),
    }
