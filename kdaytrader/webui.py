"""브라우저 대시보드: 표준 라이브러리 HTTP 서버로 상태 JSON 과 단일 페이지 UI 를 제공한다 (추가 의존성 없음)."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).with_name("webui.html")


class WebServer:
    def __init__(self, engine, host: str = "127.0.0.1", port: int = 8787):
        self.engine = engine
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        engine = self.engine
        html = _HTML_PATH.read_text(encoding="utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 콘솔 소음 제거
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/state"):
                    try:
                        body = json.dumps(engine.snapshot(), ensure_ascii=False, default=str).encode("utf-8")
                    except Exception as e:  # pragma: no cover
                        body = json.dumps({"error": str(e)}).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                elif self.path in ("/", "/index.html"):
                    self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path == "/api/toggle_auto":
                    engine.trader.auto_trade = not engine.trader.auto_trade
                    self._send(200, json.dumps({"auto_trade": engine.trader.auto_trade}).encode(), "application/json")
                elif self.path == "/api/close_all":
                    ts = engine.last_ts()
                    engine.trader.force_close_all(ts, "수동 전량 청산")
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="webui")
        self._thread.start()
        log.info("웹 대시보드: %s", self.url)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
