"""Clustering = tập giả thuyết "các địa chỉ này cùng một chủ".

Mỗi cạnh gộp lưu (signal, tx, confidence) để audit; revoke 1 cạnh rồi rebuild sẽ tách cụm.
Gộp thận trọng: precision quan trọng hơn recall (1 merge sai làm nhiễm hàng nghìn địa chỉ).
"""
import hashlib
import json
import time
from collections import defaultdict

from .db import audit, norm
from .vendor.cluster_utxo import UnionFind, looks_like_coinjoin
from .vendor.evm_sweep_cluster import detect as detect_sweeps

MAX_CLUSTER = 5000  # cụm lớn hơn mức này bị báo "oversized" để người duyệt


def add_edge(con, chain, a, b, signal, tx="", confidence=0.7):
    a, b = norm(chain, a), norm(chain, b)
    if a == b:
        return False
    a, b = sorted((a, b))
    cur = con.execute(
        "INSERT OR IGNORE INTO link_edges(chain,a,b,signal,tx,confidence,created_at) VALUES(?,?,?,?,?,?,?)",
        (chain, a, b, signal, tx, float(confidence), int(time.time())))
    return cur.rowcount == 1


def utxo_edges(con, chain, txs, confidence=0.9):
    """Common-input-ownership, bỏ qua tx giống CoinJoin. txs: iterable theo định dạng cluster_utxo."""
    added, skipped = 0, []
    for tx in txs:
        ins = list(dict.fromkeys(tx["inputs"]))
        if len(ins) < 2:
            continue
        if looks_like_coinjoin(tx):
            skipped.append(tx["txid"])
            continue
        for other in ins[1:]:
            added += add_edge(con, chain, ins[0], other, "utxo_cioh", tx["txid"], confidence)
    con.commit()
    return {"edges_added": added, "coinjoin_skipped": skipped}


def evm_sweep_edges(con, chain, rows, **kw):
    """Cụm deposit-address của sàn/custodian: nhiều địa chỉ nhận -> sweep về cùng hot wallet."""
    out = detect_sweeps(rows, **kw)
    added = 0
    for c in out:
        sweep_tx = {s["deposit"]: s["sweep_tx"] for s in c["evidence"]["sweeps"]}
        for d in c["deposit_addresses"]:
            added += add_edge(con, chain, c["hot_wallet"], d, "evm_sweep", sweep_tx[d], c["confidence"])
    con.commit()
    return {"clusters": len(out), "edges_added": added, "detail": out}


def entity_id_for(chain, root):
    return "E-" + hashlib.sha1(f"{chain}:{root}".encode()).hexdigest()[:12]


def rebuild(con, chain, min_confidence=0.6):
    """Tính lại thành phần liên thông từ các cạnh active. Trả về thống kê."""
    uf = UnionFind()
    for r in con.execute("SELECT a,b FROM link_edges WHERE chain=? AND active=1 AND confidence>=?",
                         (chain, min_confidence)):
        uf.union(r["a"], r["b"])
    groups = defaultdict(list)
    for addr in list(uf.parent):
        groups[uf.find(addr)].append(addr)
    con.execute("DELETE FROM entity_members WHERE chain=?", (chain,))
    oversized = []
    for members in groups.values():
        eid = entity_id_for(chain, min(members))
        if len(members) > MAX_CLUSTER:
            oversized.append(eid)
        con.executemany("INSERT INTO entity_members(chain,address,entity_id) VALUES(?,?,?)",
                        [(chain, m, eid) for m in members])
    con.commit()
    return {"entities": len(groups), "addresses": sum(len(m) for m in groups.values()),
            "oversized": oversized}


def revoke_edge(con, edge_id, actor, reason, min_confidence=0.6):
    row = con.execute("SELECT chain FROM link_edges WHERE id=?", (edge_id,)).fetchone()
    if not row:
        raise KeyError(edge_id)
    con.execute("UPDATE link_edges SET active=0 WHERE id=?", (edge_id,))
    con.commit()
    audit(con, actor, "edge.revoke", {"id": edge_id, "reason": reason})
    return rebuild(con, row["chain"], min_confidence)


def resolve(con, chain, address):
    """address -> (entity_id, [members]). Địa chỉ lẻ: entity 'addr:<a>' với 1 thành viên."""
    address = norm(chain, address)
    r = con.execute("SELECT entity_id FROM entity_members WHERE chain=? AND address=?",
                    (chain, address)).fetchone()
    if not r:
        return f"addr:{address}", [address]
    mem = [x["address"] for x in con.execute(
        "SELECT address FROM entity_members WHERE chain=? AND entity_id=? ORDER BY address",
        (chain, r["entity_id"]))]
    return r["entity_id"], mem


def entity_ids(con, chain, addresses):
    """Tra hàng loạt address -> entity_id (mặc định addr:<a>)."""
    rows = con.execute(
        "SELECT address, entity_id FROM entity_members WHERE chain=? AND address IN (SELECT value FROM json_each(?))",
        (chain, json.dumps(list(addresses)))).fetchall()
    m = {r["address"]: r["entity_id"] for r in rows}
    return {a: m.get(a, f"addr:{a}") for a in addresses}


def edge_evidence(con, chain, entity_id):
    ms = resolve_members(con, chain, entity_id)
    rows = con.execute(
        "SELECT id,a,b,signal,tx,confidence,active FROM link_edges WHERE chain=?"
        " AND (a IN (SELECT value FROM json_each(?)) OR b IN (SELECT value FROM json_each(?)))",
        (chain, json.dumps(ms), json.dumps(ms))).fetchall()
    return [dict(r) for r in rows]


def resolve_members(con, chain, entity_id):
    if entity_id.startswith("addr:"):
        return [entity_id[5:]]
    return [x["address"] for x in con.execute(
        "SELECT address FROM entity_members WHERE chain=? AND entity_id=?", (chain, entity_id))]
