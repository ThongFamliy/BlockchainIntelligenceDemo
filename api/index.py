"""Điểm vào Vercel (Python serverless). Bản DEMO công khai: dữ liệu tổng hợp, chỉ đọc.

Mọi đường dẫn được vercel.json rewrite về /api/index?path=<đường dẫn gốc>.
Vercel chỉ cho ghi vào /tmp và mỗi instance có DB riêng, nên DB demo được dựng lại khi khởi động.
Để dùng thật (ghi nhãn, dữ liệu của bạn) hãy chạy `python -m vnbi serve` trên máy chủ có đĩa bền.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vnbi import api as bi_api  # noqa: E402
from vnbi import db  # noqa: E402
from vnbi.demo import seed_demo  # noqa: E402

_svc = None
_INDEX = Path(__file__).resolve().parent.parent / "vnbi" / "web" / "index.html"


def get_service():
    global _svc
    if _svc is None:
        tmp = os.environ.get("VNBI_TMP", "/tmp")
        path = os.path.join(tmp, "vnbi-demo.db")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
        con = db.connect(path)
        seed_demo(con, os.path.join(tmp, "vnbi-demo"))
        _svc = bi_api.Service(con, api_key="", read_only=True)
    return _svc


def resolve_path(raw):
    """/api/index?path=stats&limit=5 -> '/stats?limit=5'; đường dẫn trực tiếp giữ nguyên."""
    u = urlparse(raw)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if "path" in q or u.path.rstrip("/") == "/api/index":
        path = "/" + q.pop("path", "").lstrip("/")
    else:
        path = u.path
    return path + ("?" + urlencode(q) if q else "")


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _do(self, method):
        path = resolve_path(self.path)
        if method == "GET" and path.split("?")[0] in ("/", "/index.html"):
            return self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, json.dumps({"error": "JSON không hợp lệ"}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        code, out = bi_api.handle(get_service(), method, path, body, dict(self.headers))
        self._send(code, json.dumps(out, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self): self._do("GET")
    def do_POST(self): self._do("POST")
    def log_message(self, *a): pass
