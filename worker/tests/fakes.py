"""Off-platform stand-ins for the Worker runtime, so the app can be driven with
a normal ASGI test client. The D1 fake is backed by real SQLite, so the SQL in
db.py/payments.py is genuinely executed."""
import json, sqlite3, sys, types

# --- stub the Worker-only modules before app.py imports them ---------------
js = types.ModuleType("js")
class _JSON:
    @staticmethod
    def stringify(o): return json.dumps(o)
js.JSON = _JSON; js.fetch = None; js.Object = object; js.URL = None; js.Headers = None
js.Response = None
sys.modules["js"] = js

ffi = types.ModuleType("pyodide.ffi")
ffi.to_js = lambda o, **k: o
ffi.JsProxy = type("JsProxy", (), {})
pyodide = types.ModuleType("pyodide"); pyodide.ffi = ffi
sys.modules["pyodide"] = pyodide; sys.modules["pyodide.ffi"] = ffi

httpjs = types.ModuleType("httpjs")
class HttpError(RuntimeError):
    def __init__(self, status, body, url=""): self.status, self.body = status, body; super().__init__(body)
httpjs.HttpError = HttpError
httpjs.to_js = lambda o: o
async def _unused(*a, **k): raise AssertionError("network call not stubbed for this test")
httpjs.fetch_json = _unused; httpjs.fetch_raw = _unused; httpjs.fetch_text = _unused
sys.modules["httpjs"] = httpjs

workers = types.ModuleType("workers")
workers.Response = None; workers.WorkerEntrypoint = object
sys.modules["workers"] = workers
asgi_mod = types.ModuleType("asgi"); asgi_mod.fetch = None
sys.modules["asgi"] = asgi_mod


# --- D1 fake ---------------------------------------------------------------
class _Stmt:
    def __init__(self, conn, sql): self._c, self._sql, self._p = conn, sql, ()
    def bind(self, *p): self._p = p; return self
    async def all(self):
        cur = self._c.execute(self._sql, self._p)
        rows = [dict(r) for r in cur.fetchall()]
        return {"success": True, "results": rows, "meta": {"changes": cur.rowcount}}
    async def run(self):
        cur = self._c.execute(self._sql, self._p); self._c.commit()
        return {"success": True, "results": [],
                "meta": {"changes": cur.rowcount if cur.rowcount > 0 else 0,
                         "last_row_id": cur.lastrowid}}

class FakeD1:
    def __init__(self, schema_sql):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema_sql)
    def prepare(self, sql): return _Stmt(self.conn, sql)
    async def batch(self, stmts):
        for s in stmts: await s.run()


class FakeR2:
    def __init__(self): self.objects = {}
    async def put(self, key, body, opts=None):
        self.objects[key] = body if isinstance(body, bytes) else b"x" * 1024
        return types.SimpleNamespace(size=len(self.objects[key]))
    async def get(self, key, opts=None):
        if key not in self.objects: return None
        return types.SimpleNamespace(size=len(self.objects[key]), body=self.objects[key])
    async def delete(self, key): self.objects.pop(key, None)


class FakeEnv:
    def __init__(self, schema_sql, **overrides):
        self.DB = FakeD1(schema_sql)
        self.VIDEOS = FakeR2()
        self.SESSION_SECRET = "t" * 64
        self.SUPER_ADMIN_EMAIL = "admin@example.com"
        self.APP_BASE_URL = "https://test.local"
        self.GOOGLE_CLIENT_ID = "cid"
        self.GOOGLE_CLIENT_SECRET = "csec"
        self.STRIPE_SECRET_KEY = "sk_test_x"
        self.STRIPE_WEBHOOK_SECRET = "whsec_test"
        self.GEMINI_API_KEY = "gk"
        self.GEMINI_MODEL = "gemini-2.5-flash"
        self.RESEND_API_KEY = None
        self.EMAIL_FROM = "t@t"
        self.MAX_UPLOAD_BYTES = "26214400"
        self.DAILY_ANALYSIS_CAP = "0"
        for k, v in overrides.items(): setattr(self, k, v)


def with_env(app, env):
    """Inject `env` into the ASGI scope the way the Workers adapter does."""
    async def wrapper(scope, receive, send):
        if scope["type"] == "http": scope["env"] = env
        await app(scope, receive, send)
    return wrapper
