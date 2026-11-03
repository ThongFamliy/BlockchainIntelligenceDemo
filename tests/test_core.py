import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from vnbi import api, cluster, db
from vnbi import labels as L
from vnbi.indexer import Indexer
from vnbi.risk import RiskEngine, load_policy


@pytest.fixture
def con():
    return db.connect(":memory:")


def tr(con, f, t, usd, ts=100, tx=None, chain="btc", asset="USDT"):
    tx = tx or f"tx-{f}-{t}-{ts}"
    con.execute("INSERT INTO transfers(chain,tx_hash,from_addr,to_addr,asset,amount,usd,ts) VALUES(?,?,?,?,?,?,?,?)",
                (chain, tx, f, t, asset, usd, usd, ts))
    con.commit()


# ---------- clustering ----------
def test_cioh_merge_coinjoin_guard_and_revoke(con):
    txs = [{"txid": "t1", "inputs": ["A", "B"], "outputs": [["X", 1], ["Y", 2]]},
           {"txid": "t2", "inputs": ["B", "C"], "outputs": [["Z", 1]]},
           {"txid": "cj", "inputs": list("PQRSTU"), "outputs": [["o1", 5], ["o2", 5], ["o3", 5], ["o4", 5]]}]
    r = cluster.utxo_edges(con, "btc", txs)
    assert r["coinjoin_skipped"] == ["cj"] and r["edges_added"] == 2
    cluster.rebuild(con, "btc")
    eid, mem = cluster.resolve(con, "btc", "A")
    assert sorted(mem) == ["A", "B", "C"]
    assert cluster.resolve(con, "btc", "P")[1] == ["P"]  # CoinJoin không gộp
    edge = con.execute("SELECT id FROM link_edges WHERE tx='t2'").fetchone()["id"]
    cluster.revoke_edge(con, edge, "analyst", "false positive")
    assert sorted(cluster.resolve(con, "btc", "A")[1]) == ["A", "B"]  # tách cụm sau revoke
    assert con.execute("SELECT COUNT(*) c FROM audit WHERE action='edge.revoke'").fetchone()["c"] == 1


# ---------- labels ----------
def test_label_supersede_keeps_history_and_conflict(con):
    a = L.add_label(con, "eth", "0xABC", "scam_investment", "src1", confidence=0.5)
    b = L.add_label(con, "eth", "0xabc", "scam_investment", "src1", confidence=0.9)  # cùng nguồn -> supersede
    L.add_label(con, "eth", "0xabc", "exchange", "src2", confidence=0.8)
    h = L.history(con, "eth", "0xabc")
    assert len(h) == 3 and h[0]["superseded_by"] == b
    active = L.labels_for(con, "eth", ["0xabc"])
    assert {x["category"] for x in active} == {"scam_investment", "exchange"} and a not in {x["id"] for x in active}
    L.retract_label(con, b, "analyst", "sai")
    assert {x["category"] for x in L.labels_for(con, "eth", ["0xabc"])} == {"exchange"}


# ---------- risk ----------
def risky(con, addr, cat="darknet", conf=0.9, chain="btc"):
    L.add_label(con, chain, addr, cat, "t", confidence=conf, trust_tier=3)


def test_haircut_matches_reference_example(con):
    # Ví dụ chuẩn: input 6 sạch + 2 darknet => 25% darknet ở đầu ra
    risky(con, "B")
    tr(con, "A", "M", 6000); tr(con, "B", "M", 2000)
    tr(con, "M", "C", 7000)
    p = load_policy(); p["gamma"] = 1.0
    r = RiskEngine(con, p).screen("btc", "C", as_of=1000, persist=False)
    x = r["exposure"]["darknet"]
    assert x["kind"] == "indirect" and x["share"] == pytest.approx(0.25) and x["hop"] == 3
    assert r["decision"] == "review"  # R4: gián tiếp >= 10%
    assert [s["address"] for s in x["path"]] == ["C", "M", "B"]


def test_hop_decay(con):
    risky(con, "B")
    tr(con, "B", "M", 1000); tr(con, "M", "C", 1000)
    r = RiskEngine(con).screen("btc", "C", as_of=1000, persist=False)
    assert r["exposure"]["darknet"]["share"] == pytest.approx(0.7 ** 2)  # B ở hop 3 => gamma^2


def test_direct_sanctioned_blocks_and_max_hops(con):
    risky(con, "S", "sanctioned", 0.95)
    r = RiskEngine(con).screen("btc", "S", persist=False)
    assert r["decision"] == "block" and r["exposure"]["sanctioned"]["kind"] == "direct"
    # quá max_hops (3) => không thấy
    tr(con, "S", "a", 100); tr(con, "a", "b", 100); tr(con, "b", "c", 100); tr(con, "c", "d", 100)
    assert RiskEngine(con).screen("btc", "d", as_of=1000, persist=False)["decision"] == "allow"
    assert RiskEngine(con).screen("btc", "c", as_of=1000, persist=False)["decision"] == "allow"  # S ở hop 4
    assert RiskEngine(con).screen("btc", "b", as_of=1000, persist=False)["decision"] == "block"  # S ở hop 3


def test_dust_filter_and_causality(con):
    risky(con, "S", "scam_investment")
    tr(con, "S", "D", 1); tr(con, "C", "D", 5000)
    assert RiskEngine(con).screen("btc", "D", as_of=1000, persist=False)["decision"] == "allow"  # bụi
    tr(con, "S", "E", 1000, ts=500)
    e = RiskEngine(con)
    assert e.screen("btc", "E", as_of=400, persist=False)["decision"] == "allow"   # chưa xảy ra
    assert e.screen("btc", "E", as_of=600, persist=False)["decision"] != "allow"


def test_exchange_is_terminal_and_low_confidence_ignored(con):
    risky(con, "S", "scam_investment")
    L.add_label(con, "btc", "H", "exchange", "t", confidence=0.9)
    tr(con, "S", "H", 1000); tr(con, "H", "Z", 1000)
    assert RiskEngine(con).screen("btc", "Z", as_of=1000, persist=False)["decision"] == "allow"
    risky(con, "W", "darknet", conf=0.3)  # dưới min_label_confidence
    tr(con, "W", "Y", 1000)
    assert RiskEngine(con).screen("btc", "Y", as_of=1000, persist=False)["decision"] == "allow"


def test_cycles_do_not_hang(con):
    tr(con, "a", "b", 100); tr(con, "b", "c", 100); tr(con, "c", "a", 100)
    assert RiskEngine(con).screen("btc", "a", as_of=1000, persist=False)["decision"] == "allow"


def test_cluster_label_propagates_to_members(con):
    cluster.add_edge(con, "eth", "0xa", "0xb", "evm_sweep", "t", 0.9); cluster.rebuild(con, "eth")
    L.add_label(con, "eth", "0xa", "ransomware", "x", confidence=0.9)
    tr(con, "0xb", "0xc", 1000, chain="eth")
    r = RiskEngine(con).screen("eth", "0xc", as_of=1000, persist=False)
    assert r["exposure"]["ransomware"]["hop"] == 2


# ---------- indexer reorg ----------
class FakeRpc:
    def __init__(self, blocks): self.blocks = blocks
    def head(self): return max(self.blocks)
    def block(self, n): return self.blocks[n]


def mk(n, tag, parent_tag, amt):
    return {"number": n, "hash": f"h{n}{tag}", "parent_hash": f"h{n-1}{parent_tag}", "timestamp": 1000 + n,
            "transfers": [{"tx_hash": f"t{n}{tag}", "from": "0xA", "to": "0xB", "asset": "USDT", "amount": amt}]}


def test_reorg_rollback(con):
    chain_a = {n: mk(n, "a", "a", 10) for n in range(1, 6)}
    ix = Indexer(con, "eth", FakeRpc(chain_a), start_block=1)
    assert ix.sync()["cursor"] == 5
    fork = {**{n: chain_a[n] for n in (1, 2, 3)}, 4: mk(4, "b", "a", 99), 5: mk(5, "b", "b", 99), 6: mk(6, "b", "b", 99)}
    ix.rpc = FakeRpc(fork)
    r = ix.sync()
    assert r["cursor"] == 6 and r["rollbacks"] == 2
    hashes = {x["tx_hash"] for x in con.execute("SELECT tx_hash FROM transfers")}
    assert hashes == {"t1a", "t2a", "t3a", "t4b", "t5b", "t6b"}  # block mồ côi biến mất
    assert ix.sync()["stored"] == 0  # idempotent


# ---------- API ----------
def test_api_flow_and_auth(con):
    svc = api.Service(con, api_key="k")
    assert api.handle(svc, "GET", "/health")[0] == 200
    assert api.handle(svc, "POST", "/screen", {"chain": "btc", "address": "S"})[0] == 401
    h = {"X-API-Key": "k", "X-Actor": "alice"}
    code, out = api.handle(svc, "POST", "/label", {"chain": "btc", "address": "S", "category": "sanctioned",
                                                   "source": "t", "confidence": 0.9}, h)
    assert code == 201
    code, out = api.handle(svc, "GET", "/address/btc/S", None, h)
    assert code == 200 and out["decision"] == "block"
    assert api.handle(svc, "POST", "/screen", {"chain": "btc"}, h)[0] == 400
    assert con.execute("SELECT COUNT(*) c FROM audit WHERE actor='alice'").fetchone()["c"] >= 2


def test_api_portal_endpoints(con):
    from pathlib import Path
    svc = api.Service(con, api_key="")
    api.handle(svc, "POST", "/label", {"chain": "btc", "address": "S", "category": "sanctioned", "source": "t"})
    api.handle(svc, "GET", "/address/btc/S")
    code, st = api.handle(svc, "GET", "/stats")
    assert code == 200 and st["labels"] == 1 and st["screenings"] == {"block": 1}
    code, sc = api.handle(svc, "GET", "/screenings?limit=5")
    assert code == 200 and sc["items"][0]["decision"] == "block"
    assert api.handle(svc, "GET", "/audit?limit=2")[1]["items"]
    assert api.handle(svc, "GET", "/labels/btc/S")[1]["items"][0]["category"] == "sanctioned"
    assert (Path(api.__file__).with_name("web") / "index.html").exists()
