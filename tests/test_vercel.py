import json, os, sys, threading, tempfile, urllib.request, urllib.error
from http.server import HTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["VNBI_TMP"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
import index as vercel_entry  # api/index.py
from vnbi.demo import A

import pytest


@pytest.fixture(scope="module")
def base():
    srv = HTTPServer(("127.0.0.1", 0), vercel_entry.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def get(url, data=None):
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers["Content-Type"], r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers["Content-Type"], e.read()


def test_resolve_path_variants():
    r = vercel_entry.resolve_path
    assert r("/api/index?path=") == "/"
    assert r("/api/index?path=stats") == "/stats"
    assert r("/api/index?path=screenings&limit=5") == "/screenings?limit=5"
    assert r("/api/index?path=address/eth/0xabc") == "/address/eth/0xabc"
    assert r("/stats") == "/stats"  # truy cập trực tiếp không qua rewrite


def test_rewritten_and_direct_routes(base):
    code, ct, body = get(base + "/api/index?path=")
    assert code == 200 and "text/html" in ct and b"vnbi" in body
    assert json.loads(get(base + "/api/index?path=health")[2]) == {"ok": True, "demo": True}
    st = json.loads(get(base + "/api/index?path=stats")[2])
    assert st["labels"] == 3 and st["entities"] == 1
    code, _, body = get(base + f"/api/index?path=address/eth/{A('X')}")
    r = json.loads(body)
    assert code == 200 and r["decision"] == "review"
    assert json.loads(get(base + f"/address/eth/{A('SANC')}")[2])["decision"] == "block"  # đường dẫn trực tiếp


def test_demo_is_read_only(base):
    code, _, body = get(base + "/api/index?path=label", {"chain": "eth", "address": "0x1", "category": "mixer", "source": "x"})
    assert code == 403 and "chỉ đọc" in json.loads(body)["error"]
    assert get(base + "/api/index?path=edge/revoke", {"id": 1, "reason": "x"})[0] == 403
