#!/usr/bin/env python3
"""Loopback-only Luna compatibility proxy for the local MLX model server.

The original Luna source and virtual environment were removed, but local
clients still use the OpenAI-compatible Luna port. This small replacement
keeps that contract while forwarding inference to the maintained MLX server.
It deliberately exposes no shell, filesystem, or remote-network operations.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
PORT = int(os.environ.get("LUNA_PORT", "11435"))
UPSTREAM = os.environ.get("LUNA_UPSTREAM_URL", "http://127.0.0.1:8081").rstrip("/")
TIMEOUT = float(os.environ.get("LUNA_UPSTREAM_TIMEOUT", "120"))


class Handler(BaseHTTPRequestHandler):
    server_version = "LunaCompat/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value).encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "GPT Luna compatibility proxy", "upstream": UPSTREAM})
            return
        if self.path == "/v1/models":
            self._proxy("GET")
            return
        self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path in {"/v1/chat/completions", "/v1/completions"}:
            self._proxy("POST")
            return
        self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if method == "POST" else None
        target = f"{UPSTREAM}{self.path}"
        request = Request(target, data=payload, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
                self._send(response.status, body, response.headers.get("Content-Type", "application/json"))
        except HTTPError as exc:
            self._send(exc.code, exc.read(), "application/json")
        except (URLError, TimeoutError, OSError) as exc:
            self._json(503, {"error": {"message": f"Luna upstream unavailable: {exc}", "type": "server_error"}})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Luna compatibility proxy listening on http://{HOST}:{PORT}; upstream={UPSTREAM}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
