"""HTTP API (stdlib, không phụ thuộc). Logic nằm ở handle() để test không cần socket.

  GET  /health
  GET  /address/{chain}/{addr}   -> entity, nhãn, exposure, quyết định
  POST /screen   {chain,address,as_of?}
  POST /label    {chain,address,category,source,confidence?,name?,trust_tier?}
  POST /edge/revoke {id,reason}
Đặt biến môi trường BI_API_KEY để bật xác thực header X-API-Key.
"""
import hmac
import json
import os
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cluster
from . import labels as L
from .risk import RiskEngine


class Service:
    def __init__(self, con, policy=None, api_key=None, read_only=False):
        self.read_only = read_only  # demo công khai: chặn mọi thao tác ghi nhãn / gỡ cạnh
        self.con, self.engine = con, RiskEngine(con, policy)
        self.api_key = api_key if api_key is not None else os.environ.get("BI_API_KEY")
        self.lock = threading.Lock()


def handle(svc, method, path, body=None, headers=None):
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if path == "/health":
        return 200, {"ok": True, "demo": svc.read_only}
    if svc.api_key and not hmac.compare_digest(headers.get("x-api-key", ""), svc.api_key):
        return 401, {"error": "unauthorized"}
    actor = headers.get("x-actor", "api")
    if svc.read_only and method == "POST" and path.split("?")[0] in ("/label", "/edge/revoke"):
        return 403, {"error": "Bản demo công khai ở chế độ chỉ đọc: không thêm nhãn hay gỡ cạnh được. Chạy bản riêng của bạn để dùng đầy đủ."}
    parts = [p for p in path.split("?")[0].split("/") if p]
    try:
        limit = min(int(path.split("limit=")[1].split("&")[0]), 500) if "limit=" in path else 50
    except ValueError:
        limit = 50
    try:
        with svc.lock:
            if method == "GET" and parts == ["stats"]:
                q = lambda s: svc.con.execute(s).fetchone()[0]
                by = {r[0]: r[1] for r in svc.con.execute("SELECT decision, COUNT(*) FROM screenings GROUP BY 1")}
                return 200, {"transfers": q("SELECT COUNT(*) FROM transfers"),
                             "labels": q("SELECT COUNT(*) FROM labels WHERE superseded_by IS NULL"),
                             "entities": q("SELECT COUNT(DISTINCT entity_id) FROM entity_members"),
                             "edges": q("SELECT COUNT(*) FROM link_edges WHERE active=1"),
                             "screenings": by, "policy_version": svc.engine.p["version"]}
            if method == "GET" and parts == ["screenings"]:
                rows = svc.con.execute("SELECT id,ts,chain,address,decision,risk_level,policy_version FROM screenings"
                                       " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return 200, {"items": [dict(r) for r in rows]}
            if method == "GET" and parts == ["audit"]:
                rows = svc.con.execute("SELECT id,ts,actor,action,payload FROM audit ORDER BY id DESC LIMIT ?",
                                       (limit,)).fetchall()
                return 200, {"items": [dict(r) for r in rows]}
            if method == "GET" and len(parts) == 3 and parts[0] == "labels":
                return 200, {"items": L.history(svc.con, parts[1], parts[2])}
            if method == "GET" and len(parts) == 3 and parts[0] == "address":
                r = svc.engine.screen(parts[1], parts[2], actor=actor)
                eid = r["entity_id"]
                r["edge_evidence"] = cluster.edge_evidence(svc.con, parts[1], eid) if not eid.startswith("addr:") else []
                return 200, r
            if method == "POST" and parts == ["screen"]:
                return 200, svc.engine.screen(body["chain"], body["address"], as_of=body.get("as_of"), actor=actor)
            if method == "POST" and parts == ["label"]:
                lid = L.add_label(svc.con, body["chain"], body["address"], body["category"], body["source"],
                                  name=body.get("name"), confidence=body.get("confidence", 0.7),
                                  trust_tier=body.get("trust_tier", 1), method="api", actor=actor)
                return 201, {"id": lid}
            if method == "POST" and parts == ["edge", "revoke"]:
                return 200, cluster.revoke_edge(svc.con, body["id"], actor, body.get("reason", ""))
    except (KeyError, TypeError, ValueError) as e:
        return 400, {"error": f"bad request: {e!r}"}
    return 404, {"error": "not found"}


def serve(svc, host="127.0.0.1", port=8080):
    class H(BaseHTTPRequestHandler):
        def _do(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            code, out = handle(svc, method, self.path, body, dict(self.headers))
            data = json.dumps(out, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):  # giao diện web, không cần khoá
                data = (Path(__file__).with_name("web") / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            self._do("GET")
        def do_POST(self): self._do("POST")
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer((host, port), H)
    print(f"vnbi API: http://{host}:{port}  (auth: {'on' if svc.api_key else 'OFF'})")
    srv.serve_forever()
