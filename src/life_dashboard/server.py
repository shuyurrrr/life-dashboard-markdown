#!/usr/bin/env python3
"""Serve the private life dashboard, rebuilding it from canonical Markdown.

The HTTP listener is deliberately loopback-only.  Tailscale Serve terminates
HTTPS and is the sole remote entrypoint, so the dashboard remains tailnet-only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import threading
from dataclasses import dataclass
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .view import (
    DEFAULT_VAULT,
    load_case_ledger,
    load_life_wheel,
    load_month,
    load_task_ledger,
    parse_day,
    render_case_pool_page,
    render_page,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
DEFAULT_WORK_TOKEN_FILE = Path.home() / ".config" / "life-dashboard" / "token"
WORK_PREFIXES = ("/work-dashboard", "/dashboard")
WORK_COOKIE = "life_dashboard_session"
PWA_ASSET_DIR = Path(__file__).resolve().parents[2] / "static" / "pwa"
PWA_PUBLIC_SUFFIXES = {
    "/manifest.webmanifest",
    "/sw.js",
    "/icon-192.png",
    "/icon-512.png",
    "/apple-touch-icon.png",
}

PWA_HEAD = b'''<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#f4efe7">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="\xe9\xa9\xbe\xe9\xa9\xb6\xe8\x88\xb1">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<script>if('serviceWorker' in navigator){window.addEventListener('load',()=>navigator.serviceWorker.register('sw.js',{scope:'./'}).catch(()=>{}));}</script>
'''

PWA_SERVICE_WORKER = b'''/* Network-only by design: private dashboard content is never cached. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(
  caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key))))
    .then(() => self.clients.claim())
));
self.addEventListener('fetch', event => {
  if (event.request.method === 'GET') event.respondWith(fetch(event.request));
});
'''


@dataclass(frozen=True)
class DashboardConfig:
    vault_root: Path
    month: str | None = None
    work_token_file: Path | None = DEFAULT_WORK_TOKEN_FILE


def load_work_token(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError("refusing symlink work-dashboard token file")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("work-dashboard token must contain at least 32 characters")
    return token


def work_session_value(token: str) -> str:
    return hashlib.sha256(f"life-dashboard-readonly\0{token}".encode()).hexdigest()


def work_cookie_authorized(cookie_header: str, token: str) -> bool:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return False
    supplied = cookie.get(WORK_COOKIE)
    return bool(supplied) and hmac.compare_digest(
        supplied.value, work_session_value(token)
    )


def work_route(path: str) -> tuple[str, str] | None:
    """Return (external prefix, suffix) for either direct or proxied routes."""
    for prefix in WORK_PREFIXES:
        if path == prefix:
            return prefix, ""
        if path.startswith(prefix + "/"):
            return prefix, path[len(prefix) :]
    return None


def pwa_manifest() -> bytes:
    return json.dumps(
        {
            "id": "./",
            "name": "\u751f\u6d3b\u9a7e\u9a76\u8231",
            "short_name": "\u9a7e\u9a76\u8231",
            "description": "\u4e00\u4e2a\u4e8b\u5b9e\u6e90\uff0c\u4e09\u79cd\u65f6\u95f4\u3002",
            "start_url": "./",
            "scope": "./",
            "display": "standalone",
            "background_color": "#f4efe7",
            "theme_color": "#f4efe7",
            "icons": [
                {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def enable_pwa(body: bytes) -> bytes:
    """Inject install metadata only into authenticated dashboard pages."""
    return body.replace(b"</head>", PWA_HEAD + b"</head>", 1)


def login_page() -> bytes:
    return ("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Life Dashboard</title>
<style>:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#332d28;background:#f4efe7}body{margin:0;min-height:100vh;display:grid;place-items:center}.card{width:min(420px,calc(100vw - 40px));background:#fffaf3;border:1px solid #d8cfc3;border-radius:22px;padding:28px;box-shadow:0 18px 55px #5e4d3920}h1{font-size:22px;margin:0 0 8px}p{color:#786f67;line-height:1.55}input{width:100%;box-sizing:border-box;padding:13px;border:1px solid #cabeb0;border-radius:12px;font:inherit;background:#fff}button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:12px;background:#5f714f;color:#fff;font:inherit;font-weight:700}</style></head>
<body><form class="card" method="post" action="/dashboard/login"><h1>人生驾驶舱</h1><p>只读入口。输入独立 dashboard 密钥。</p><input name="token" type="password" autocomplete="current-password" autofocus required><button type="submit">打开</button></form></body></html>""").encode("utf-8")


def build_dashboard(config: DashboardConfig, today: date | None = None) -> tuple[bytes, str]:
    """Build one fresh HTML response and return it with its effective month."""
    today = today or date.today()
    month = config.month or today.strftime("%Y-%m")
    vault = config.vault_root.expanduser()
    source_dir = vault / "daily-log" / month
    records = load_month(source_dir, month)

    if month == today.strftime("%Y-%m"):
        live_today = vault / "today.md"
        if live_today.is_file():
            live_record = parse_day(live_today, today.isoformat())
            records = [record for record in records if record.iso_date != live_record.iso_date]
            records.append(live_record)
            records.sort(key=lambda record: record.iso_date)

    if not records:
        raise ValueError(f"no daily logs found for {month}")

    tasks = load_task_ledger(vault / "任务台.md")
    cases = load_case_ledger(vault / "工作" / "case-ledger.md")
    life_wheel = load_life_wheel(vault / "人生轮.md")
    html = render_page(records, month, tasks, life_wheel, cases)
    return html.encode("utf-8"), month


def build_case_pool(config: DashboardConfig) -> bytes:
    """Build the dedicated case page from the same live Markdown ledger."""
    vault = config.vault_root.expanduser()
    cases = load_case_ledger(vault / "工作" / "case-ledger.md")
    return render_case_pool_page(cases).encode("utf-8")



class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: DashboardConfig):
        super().__init__(server_address, DashboardHandler)
        self.dashboard_config = config
        self.build_lock = threading.Lock()
        self.last_success_at: str | None = None
        self.last_month: str | None = None


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {format % args}", flush=True)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str, *, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_dashboard(self) -> None:
        try:
            with self.server.build_lock:
                body, month = build_dashboard(self.server.dashboard_config)
                self.server.last_success_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.server.last_month = month
        except Exception as exc:  # request must fail closed rather than serve stale data
            message = f"dashboard generation failed: {type(exc).__name__}: {exc}\n".encode()
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, message, "text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.OK, enable_pwa(body), "text/html; charset=utf-8")

    def _send_case_pool(self) -> None:
        try:
            with self.server.build_lock:
                body = build_case_pool(self.server.dashboard_config)
        except Exception as exc:
            message = f"case pool generation failed: {type(exc).__name__}: {exc}\n".encode()
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, message, "text/plain; charset=utf-8")
            return
        self._send(HTTPStatus.OK, enable_pwa(body), "text/html; charset=utf-8")

    def _send_pwa_asset(self, suffix: str) -> None:
        if suffix == "/manifest.webmanifest":
            self._send(HTTPStatus.OK, pwa_manifest(), "application/manifest+json; charset=utf-8")
            return
        if suffix == "/sw.js":
            self._send(HTTPStatus.OK, PWA_SERVICE_WORKER, "text/javascript; charset=utf-8")
            return
        filenames = {
            "/icon-192.png": "app-icon-192.png",
            "/icon-512.png": "app-icon-512.png",
            "/apple-touch-icon.png": "app-icon-180.png",
        }
        filename = filenames.get(suffix)
        if not filename:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        path = PWA_ASSET_DIR / filename
        self._send(HTTPStatus.OK, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png")

    def _work_token(self) -> str | None:
        return load_work_token(self.server.dashboard_config.work_token_file)

    def _work_authorized(self, token: str) -> bool:
        return work_cookie_authorized(self.headers.get("Cookie", ""), token)

    def _handle_work_get(self, suffix: str) -> None:
        token = self._work_token()
        if not token:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        if suffix == "":
            suffix = "/"
        if suffix == "/logout":
            cookie = f"{WORK_COOKIE}=; Path=/dashboard; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
            self._redirect("/dashboard/", cookie=cookie)
            return
        if suffix in PWA_PUBLIC_SUFFIXES:
            self._send_pwa_asset(suffix)
            return
        if suffix not in {"/", "/cases"}:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        if not self._work_authorized(token):
            self._send(HTTPStatus.UNAUTHORIZED, login_page(), "text/html; charset=utf-8")
            return
        if suffix == "/cases":
            self._send_case_pool()
        else:
            self._send_dashboard()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        route = work_route(path)
        if route:
            self._handle_work_get(route[1])
            return
        if path == "/healthz":
            payload = {
                "ok": True,
                "last_success_at": self.server.last_success_at,
                "last_month": self.server.last_month,
            }
            self._send(HTTPStatus.OK, json.dumps(payload).encode(), "application/json")
            return
        if path == "/cases":
            self._send_case_pool()
            return
        if path not in {"/", "/index.html"}:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return

        self._send_dashboard()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        route = work_route(path)
        if not route or route[1] != "/login":
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed\n", "text/plain; charset=utf-8")
            return
        token = self._work_token()
        if not token:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
        except ValueError:
            length = 0
        supplied = parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("token", [""])[0]
        if not hmac.compare_digest(supplied, token):
            self._send(HTTPStatus.UNAUTHORIZED, login_page(), "text/html; charset=utf-8")
            return
        cookie = (
            f"{WORK_COOKIE}={work_session_value(token)}; Path=/dashboard; Max-Age=2592000; "
            "Secure; HttpOnly; SameSite=Strict"
        )
        self._redirect("/dashboard/", cookie=cookie)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--month", help="pin YYYY-MM; default follows the current month")
    parser.add_argument("--work-token-file", type=Path, default=DEFAULT_WORK_TOKEN_FILE)
    args = parser.parse_args()

    if args.month and (len(args.month) != 7 or args.month[4] != "-"):
        parser.error("--month must be YYYY-MM")
    config = DashboardConfig(
        vault_root=args.vault_root,
        month=args.month,
        work_token_file=args.work_token_file,
    )
    server = DashboardHTTPServer((args.host, args.port), config)
    print(f"life dashboard listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
