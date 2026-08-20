"""Outbound HTTP, using the runtime's own fetch().

Python Workers can use httpx or aiohttp, but every outbound call ultimately goes
through the JavaScript fetch() the runtime provides. Calling it directly keeps
the dependency list short and, more importantly, lets a `ReadableStream` be
passed straight through as a request body — which is what makes it possible to
push a video from R2 to Gemini without ever holding it in memory.
"""

import json as _json

import js
from js import Object
from pyodide.ffi import to_js as _to_js


def to_js(obj):
    """Convert a Python dict into a real JavaScript object (not a Map)."""
    return _to_js(obj, dict_converter=Object.fromEntries)


async def fetch_raw(url: str, *, method: str = "GET", headers: dict | None = None, body=None):
    """Call fetch() and hand back the raw JS Response."""
    init = {"method": method}
    if headers:
        init["headers"] = headers
    if body is not None:
        init["body"] = body
    return await js.fetch(url, to_js(init))


async def fetch_text(url: str, *, method: str = "GET", headers: dict | None = None, body=None) -> str:
    response = await fetch_raw(url, method=method, headers=headers, body=body)
    return await response.text()


async def fetch_json(url: str, *, method: str = "GET", headers: dict | None = None, body=None) -> dict:
    """Fetch and parse JSON, raising with the response body on a non-2xx.

    Bubbling the body up matters: Google and Stripe both explain what went wrong
    in the error payload, and swallowing it (as the Flask app's bare `except:`
    did) turns a fixable mistake into a silent failure.
    """
    response = await fetch_raw(url, method=method, headers=headers, body=body)
    text = await response.text()

    if not response.ok:
        raise HttpError(response.status, text, url)

    try:
        return _json.loads(text)
    except ValueError as exc:
        raise HttpError(response.status, f"non-JSON response: {text[:400]}", url) from exc


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {body[:400]}")
