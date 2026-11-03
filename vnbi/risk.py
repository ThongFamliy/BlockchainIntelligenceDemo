"""Exposure (taint haircut + hop decay) -> rule table -> quyết định có giải thích.

Mô hình: số dư mỗi entity là hỗn hợp tỉ lệ thuận với dòng tiền vào (haircut/proportional).
  exposure(e) = sum_src  (flow_src / total_inflow) * exposure(src)
Gặp entity có nhãn rủi ro -> dừng, tính gamma^(hop-1). Gặp entity "đích dừng" (sàn, router) -> dừng, 0.
Lọc bụi (min_usd, min_share) chống dusting/poisoning. Chỉ dùng giao dịch ts <= as_of (nhân quả).
"""
import json
import time
from pathlib import Path

from . import cluster
from . import labels as L
from .db import audit, norm

ACTION_ORDER = {"allow": 0, "review": 1, "hold": 2, "block": 3}
LEVEL = {"allow": "low", "review": "medium", "hold": "high", "block": "severe"}


def load_policy(path=None):
    return json.loads(Path(path or Path(__file__).with_name("policy.json")).read_text(encoding="utf-8"))


class RiskEngine:
    def __init__(self, con, policy=None):
        self.con = con
        self.p = policy or load_policy()

    # ---------- entity info ----------
    def _info(self, chain, eid, members):
        labs = L.labels_for(self.con, chain, members, self.p["min_label_confidence"])
        risky, terminal = {}, None
        for lb in labs:  # đã sắp theo tier, confidence giảm dần -> nhãn đầu mỗi hạng mục là tốt nhất
            if lb["category"] in L.TERMINAL:
                terminal = terminal or lb
            elif lb["category"] in self.p["severity"]:
                risky.setdefault(lb["category"], lb)
        return {"labels": labs, "risky": risky, "terminal": terminal,
                "conflict": bool(risky and terminal)}

    def _inflows(self, chain, members, as_of):
        """Dòng tiền vào entity từ bên ngoài, gộp theo entity nguồn."""
        mj = json.dumps(members)
        rows = self.con.execute(
            "SELECT from_addr, tx_hash, usd FROM transfers WHERE chain=? AND ts<=?"
            " AND to_addr IN (SELECT value FROM json_each(?))"
            " AND from_addr NOT IN (SELECT value FROM json_each(?))",
            (chain, as_of, mj, mj)).fetchall()
        eids = cluster.entity_ids(self.con, chain, list({r["from_addr"] for r in rows}))
        agg = {}
        for r in rows:
            if r["usd"] is None:
                self._unpriced += 1
                continue
            e = eids[r["from_addr"]]
            a = agg.setdefault(e, {"rep": r["from_addr"], "usd": 0.0, "txs": []})
            a["usd"] += r["usd"]
            a["txs"].append((r["usd"], r["tx_hash"]))
        for a in agg.values():
            a["txs"] = [t for _, t in sorted(a["txs"], reverse=True)[:3]]
        return agg

    # ---------- đệ quy exposure ----------
    def _exp(self, chain, addr, hop, as_of, stack):
        eid, members = cluster.resolve(self.con, chain, addr)
        if eid in stack:
            return {}
        info = self._info(chain, eid, members)
        step = {"entity": eid, "address": addr, "n_members": len(members),
                "labels": [(x["category"], x["name"], x["source"], x["confidence"], x["id"]) for x in info["labels"][:3]]}
        if info["risky"]:
            g = self.p["gamma"] ** (hop - 1)
            return {cat: {"share": g, "best": g, "path": [step], "hop": hop, "label": lb}
                    for cat, lb in info["risky"].items()}
        if info["terminal"] or hop >= self.p["max_hops"]:
            return {}
        flows = self._inflows(chain, members, as_of)
        total = sum(f["usd"] for f in flows.values())
        if total <= 0:
            return {}
        out = {}
        top = sorted(flows.items(), key=lambda kv: -kv[1]["usd"])[: self.p["fanout"]]
        for e, f in top:
            w = f["usd"] / total
            if w < self.p["min_share"] or f["usd"] < self.p["min_usd"]:
                continue
            for cat, h in self._exp(chain, f["rep"], hop + 1, as_of, stack | {eid}).items():
                c = w * h["share"]
                cur = out.setdefault(cat, {"share": 0.0, "best": 0.0, "path": None, "hop": h["hop"], "label": h["label"]})
                cur["share"] += c
                if c > cur["best"]:
                    cur.update(best=c, hop=h["hop"], label=h["label"],
                               path=[{**step, "received_via_txs": f["txs"], "flow_usd": round(f["usd"], 2),
                                      "flow_share": round(w, 4)}] + h["path"])
        return out

    # ---------- API chính ----------
    def screen(self, chain, address, *, as_of=None, actor="system", persist=True):
        address = norm(chain, address)
        as_of = int(as_of if as_of is not None else time.time())
        self._unpriced = 0
        eid, members = cluster.resolve(self.con, chain, address)
        info = self._info(chain, eid, members)
        hits = self._exp(chain, address, 1, as_of, frozenset())
        direct = bool(info["risky"])

        exposure = {}
        for cat, h in hits.items():
            exposure[cat] = {"kind": "direct" if direct else "indirect", "share": round(min(h["share"], 1.0), 4),
                             "hop": h["hop"], "label_confidence": h["label"]["confidence"],
                             "label_id": h["label"]["id"], "label_source": h["label"]["source"],
                             "path": h["path"]}
        fired = []
        for rule in self.p["rules"]:
            for cat in rule["categories"]:
                x = exposure.get(cat)
                if not x or x["share"] < rule.get("min_share", 0.0):
                    continue
                if rule["kind"] != "any" and rule["kind"] != x["kind"]:
                    continue
                if x["label_confidence"] < rule.get("min_label_confidence", 0.0):
                    continue
                fired.append({"rule": rule["id"], "name": rule["name"], "category": cat,
                              "kind": x["kind"], "share": x["share"], "action": rule["action"]})
        action = max((f["action"] for f in fired), key=ACTION_ORDER.get, default="allow")

        notes = []
        if len(members) > 1:
            notes.append(f"Entity gộp từ {len(members)} địa chỉ bằng heuristic - đây là giả thuyết, xem edge_evidence.")
        if info["conflict"]:
            notes.append("Entity vừa có nhãn rủi ro vừa có nhãn dịch vụ gộp: cần người duyệt.")
        if any(x["label_confidence"] < 0.7 for x in exposure.values()):
            notes.append("Có nhãn độ tin cậy < 0.7: mức rủi ro mang tính chỉ dẫn.")
        if self._unpriced:
            notes.append(f"{self._unpriced} transfer chưa có giá USD bị bỏ qua khi tính tỉ trọng.")
        notes.append(f"Mô hình {self.p['taint_model']}, gamma={self.p['gamma']}, tối đa {self.p['max_hops']} hop; "
                     "không nhìn xuyên mixer/bridge/sàn gộp.")

        result = {"chain": chain, "address": address, "entity_id": eid, "n_members": len(members),
                  "labels": [{k: lb[k] for k in ("id", "category", "name", "source", "method", "confidence", "trust_tier")}
                             for lb in info["labels"]],
                  "decision": action, "risk_level": LEVEL[action], "rules_fired": fired,
                  "exposure": exposure, "policy_version": self.p["version"], "as_of": as_of, "notes": notes}
        if persist:
            self.con.execute("INSERT INTO screenings(ts,chain,address,decision,risk_level,policy_version,result)"
                             " VALUES(?,?,?,?,?,?,?)",
                             (int(time.time()), chain, address, action, LEVEL[action], self.p["version"],
                              json.dumps(result, ensure_ascii=False, default=str)))
            self.con.commit()
            audit(self.con, actor, "screen", {"chain": chain, "address": address, "decision": action})
        return result
