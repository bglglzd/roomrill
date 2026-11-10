"""Loopback UI with scoped session mutations and an owned reconciliation loop."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import secrets
import threading
from urllib.parse import urlsplit

from . import model
from .validation import Error, encoded, keys, loads, require

ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "mark.svg": "image/svg+xml",
    "icon.svg": "image/svg+xml",
}


def make_server(engine, *, port=0, endpoint=None, model_id=None, key_env=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _reply(self, status, body, mime="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else encoded(body)
            self.send_response(status)
            for name, value in (
                ("Content-Type", mime),
                ("Content-Length", str(len(raw))),
                ("Cache-Control", "no-store"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                ),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("Connection", "close"),
            ):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def _host(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            require(
                self.headers.get_all("Host") == [expected],
                "host_refused",
                "Use the printed numeric loopback address",
            )
            require(
                not urlsplit(self.path).query and not urlsplit(self.path).fragment,
                "invalid_input",
                "Query parameters are not supported",
            )
            return "http://" + expected

        def _error(self, error):
            status = (
                400
                if error.code in ("invalid_input", "invalid_endpoint")
                else 503
                if error.code.startswith("model_")
                else 409
            )
            self._reply(status, {"error": {"code": error.code, "message": str(error)}})

        def do_GET(self):
            try:
                self._host()
                if self.path == "/api/session":
                    self._reply(200, {"token": token})
                elif self.path == "/api/state":
                    self._reply(200, engine.state())
                else:
                    name = "index.html" if self.path == "/" else self.path.removeprefix("/")
                    require(name in ASSETS, "not_found", "Unknown resource")
                    self._reply(
                        200, files("roomrill").joinpath("assets", name).read_bytes(), ASSETS[name]
                    )
            except Error as error:
                self._error(error)

        def do_POST(self):
            try:
                origin = self._host()
                require(
                    self.headers.get_all("Origin") in (None, [origin]),
                    "origin_refused",
                    "Origin is outside this session",
                )
                supplied = self.headers.get_all("X-Roomrill-Token")
                require(
                    supplied is not None
                    and len(supplied) == 1
                    and supplied[0].isascii()
                    and hmac.compare_digest(supplied[0], token),
                    "token_refused",
                    "Session token required",
                )
                lengths = self.headers.get_all("Content-Length")
                require(
                    not self.headers.get_all("Transfer-Encoding")
                    and lengths
                    and len(lengths) == 1
                    and lengths[0].isascii()
                    and lengths[0].isdigit(),
                    "invalid_input",
                    "One bounded Content-Length required; Transfer-Encoding refused",
                )
                size = int(lengths[0])
                require(
                    size <= 65536 and self.headers.get_content_type() == "application/json",
                    "invalid_input",
                    "Bounded application/json required",
                )
                self.connection.settimeout(3)
                raw = self.rfile.read(size)
                require(len(raw) == size, "invalid_input", "Incomplete JSON body")
                data = loads(raw)
                path = self.path
                if path == "/api/leases":
                    keys(
                        data,
                        {
                            "id",
                            "room",
                            "owner",
                            "role",
                            "priority",
                            "duration_seconds",
                            "restore_policy",
                            "targets",
                        },
                    )
                    engine.submit(
                        {k: v for k, v in data.items() if k not in ("owner", "role")},
                        {"owner": data["owner"], "role": data["role"]},
                    )
                elif path == "/api/cancel":
                    keys(data, {"id"})
                    engine.cancel(data["id"])
                elif path == "/api/manual":
                    keys(data, {"room", "scope", "value"})
                    engine.manual(data["room"], data["scope"], data["value"])
                elif path == "/api/link":
                    keys(data, {"room", "online"})
                    engine.link(data["room"], data["online"])
                elif path == "/api/delivery":
                    keys(data, {"automatic"})
                    engine.delivery_mode(data["automatic"])
                elif path == "/api/deliver":
                    keys(data, {"id"})
                    engine.deliver(data["id"])
                elif path == "/api/advance":
                    keys(data, {"seconds"})
                    engine.advance(data["seconds"])
                elif path == "/api/reconcile":
                    keys(data, set())
                    engine.reconcile()
                elif path == "/api/draft":
                    keys(data, {"text"})
                    result = model.propose(
                        data["text"],
                        endpoint=endpoint,
                        model=model_id,
                        rooms=[r["id"] for r in engine.state()["rooms"]],
                        key_env=key_env,
                    )
                    self._reply(200, result)
                    return
                else:
                    raise Error("not_found", "Unknown mutation")
                self._reply(200, engine.state())
            except Error as error:
                self._error(error)
            except (OSError, ValueError):
                self._reply(
                    400,
                    {"error": {"code": "invalid_request", "message": "Request could not be read"}},
                )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(engine, *, stop_event=None, **options):
    server = make_server(engine, **options)
    stop = stop_event if stop_event is not None else threading.Event()

    def tick():
        while not stop.wait(1):
            try:
                engine.reconcile()
            except Error:
                # UI reads expose clock/integrity failures; this loop never repairs data.
                pass

    worker = None
    if engine.state()["mode"] == "local-simulator":
        worker = threading.Thread(target=tick, name="roomrill-reconcile", daemon=True)
        worker.start()
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.timeout = 0.2
        while not stop.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=3)
