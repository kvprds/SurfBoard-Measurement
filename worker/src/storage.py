"""Where uploaded clips live.

This file is a seam. Everything else in the app talks to `VideoStore` and knows
nothing about what is behind it, so moving to a different provider means
rewriting this one file and changing the binding in wrangler.jsonc.

**Currently: Workers KV.** R2 is the right tool for video and was the first
implementation, but enabling R2 requires a payment card on the Cloudflare
account even for the free tier. KV is included in the Workers free plan with no
card, so that is what this uses.

Be clear-eyed about the trade. KV is a configuration and cache store; it is not
a blob store, and using it as one has real limits:

* **25 MiB per value**, a hard ceiling. MAX_UPLOAD_BYTES sits below it.
* **1 GB per account** on the free plan — about 50 clips at the 20MB cap.
* **1,000 writes per day** on the free plan.
* **Eventually consistent.** A write is visible immediately in the location that
  made it, but can take up to 60 seconds elsewhere. `get` returning None
  therefore does not prove a video is gone, only that it is not here *yet* —
  which is why `VideoMissing` exists rather than a plain None check.
* **No range requests.** The whole value comes back or nothing, so a browser
  cannot seek into a partially-downloaded clip the way it could with R2.

What KV gives back is `expirationTtl`, which R2 has no equivalent for: clips
expire on their own, so storage cannot quietly grow past the free tier.

If you outgrow this, the options that need no card are Supabase Storage (1GB
free) and Cloudinary (video-oriented); R2 becomes available the moment a card is
on file. Any of them is a rewrite of this file alone.
"""

from httpjs import to_js

# KV's hard ceiling. Uploads are checked against MAX_UPLOAD_BYTES, which should
# stay under this to leave room for KV's own per-value overhead.
KV_VALUE_LIMIT_BYTES = 25 * 1024 * 1024

DEFAULT_TTL_DAYS = 30
MIN_TTL_SECONDS = 60  # KV rejects anything shorter


class VideoMissing(Exception):
    """The clip could not be read.

    Raised instead of returning None so callers cannot casually treat a missing
    video as an empty one. Under KV's eventual consistency this is often
    temporary, so the analysis sweeper turns it into a retry rather than a
    verdict — misreading it as "this is not surfing" would refund and delete a
    session that was perfectly fine.
    """


class VideoStore:
    def __init__(self, binding, ttl_days: int = DEFAULT_TTL_DAYS):
        self._kv = binding
        self._ttl_seconds = max(MIN_TTL_SECONDS, int(ttl_days) * 24 * 60 * 60)

    async def put(self, key: str, buffer, content_type: str) -> int:
        """Store a clip. `buffer` is a JavaScript ArrayBuffer.

        The bytes stay a JS object and are never converted into Python, so a
        20MB clip costs 20MB rather than several times that inside Pyodide.

        R2 took a ReadableStream here, which was better — nothing was ever held
        whole. KV needs the complete value, and taking an ArrayBuffer means the
        size can be checked *before* the write rather than failing partway
        through it.
        """
        size = int(buffer.byteLength)
        if size <= 0:
            raise ValueError("Refusing to store an empty video.")
        if size > KV_VALUE_LIMIT_BYTES:
            raise ValueError(
                f"Video is {size} bytes; Workers KV caps a value at {KV_VALUE_LIMIT_BYTES}."
            )

        await self._kv.put(
            key,
            buffer,
            expirationTtl=self._ttl_seconds,
            # Metadata rides along with the key, so the content type survives
            # without a second lookup. It has its own 1024-byte budget.
            metadata=to_js({"contentType": content_type, "size": size}),
        )
        return size

    async def open(self, key: str):
        """Return a ReadableStream of the clip, for playback or for Gemini.

        Raises VideoMissing rather than returning None — see the class docstring.
        """
        stream = await self._kv.get(key, type="stream")
        if stream is None:
            raise VideoMissing(key)
        return stream

    async def delete(self, key: str) -> None:
        """Remove a clip. Never raises; cleanup must not fail the caller."""
        try:
            await self._kv.delete(key)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete stored video {key}: {exc}")


def store_for(env) -> VideoStore:
    return VideoStore(env.VIDEOS, ttl_days=int(getattr(env, "VIDEO_TTL_DAYS", None) or DEFAULT_TTL_DAYS))
