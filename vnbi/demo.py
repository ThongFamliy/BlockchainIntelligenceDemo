"""Demo đầu-cuối trên dữ liệu TỔNG HỢP (địa chỉ giả, không liên quan thực thể thật).

Kịch bản:
  X     : nhận tiền từ L1; L1 trộn 25% tiền của S1 (nhãn lừa đảo) với tiền sạch  -> gián tiếp, review
  SANC  : địa chỉ có nhãn "sanctioned" (giả)                                    -> trực tiếp, block
  D     : bị "dusting" 1 USDT từ S1 nhưng chủ yếu tiền sạch                      -> lọc bụi, allow
  Z     : nhận từ ví nóng của sàn (cụm 6 deposit sweep về H) dù S1 từng nạp vào sàn -> đích dừng, allow
"""
import csv
import hashlib
import json
import os

from . import cluster, db
from . import labels as L
from .cli import ingest_csv, parse_prices
from .risk import RiskEngine
from .vendor.evm_sweep_cluster import load as load_rows

T0 = 1_700_000_000


def A(name):
    return "0x" + hashlib.sha1(name.encode()).hexdigest()


def build_rows():
    r, n = [], [0]

    def tx(f, t, tok, amt, dt):
        n[0] += 1
        r.append([A(f), A(t), tok, amt, T0 + dt, f"0xdemo{n[0]:04d}"])

    tx("S1", "L1", "USDT", 10000, 0)
    tx("C1", "L1", "USDT", 30000, 10)
    tx("L1", "X", "USDT", 8000, 3600)
    tx("S1", "D", "USDT", 1, 20)
    tx("C2", "D", "USDT", 5000, 30)
    tx("SANC", "Q", "USDT", 900, 40)
    for i in range(6):  # cụm deposit của sàn giả "ExampleEx"
        dep, amt = f"DEP{i}", 100 + i * 7
        payer = "S1" if i == 0 else f"USER{i}"
        tx("G", dep, "ETH", 0.003, 100 * i)
        tx(payer, dep, "USDT", amt if i else 500, 100 * i + 50)
        tx(dep, "H", "USDT", amt if i else 500, 100 * i + 600)
    tx("H", "Z", "USDT", 2000, 5000)
    return r


def build_labels():
    return [
        ["eth", A("S1"), "scam_investment", "Nhóm lừa đảo đầu tư (giả lập)", "internal_case", 0.9, 3],
        ["eth", A("SANC"), "sanctioned", "Địa chỉ bị trừng phạt (giả lập)", "demo_list", 0.95, 3],
        ["eth", A("H"), "exchange", "ExampleEx hot wallet (giả lập)", "internal_case", 0.9, 3],
    ]


def run_demo(out="demo_out"):
    os.makedirs(out, exist_ok=True)
    tp, lp = os.path.join(out, "transfers.csv"), os.path.join(out, "labels.csv")
    with open(tp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["from", "to", "token", "amount", "block_time", "tx_hash"])
        w.writerows(build_rows())
    with open(lp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "address", "category", "name", "source", "confidence", "trust_tier"])
        w.writerows(build_labels())

    con = db.connect(os.path.join(out, "demo.db"))
    con.executescript("DELETE FROM transfers; DELETE FROM labels; DELETE FROM link_edges; "
                      "DELETE FROM entity_members; DELETE FROM screenings; DELETE FROM audit;")
    print("nạp:", ingest_csv(con, "eth", tp, parse_prices(["ETH=3500"])), "transfers,",
          L.import_csv(con, lp), "nhãn")
    r = cluster.evm_sweep_edges(con, "eth", load_rows(tp))
    print("cụm sweep:", {k: v for k, v in r.items() if k != "detail"}, cluster.rebuild(con, "eth"))
    for c in r["detail"]:
        print("  lý do:", *c["reasons"], sep="\n    - ")

    eng = RiskEngine(con)
    as_of = T0 + 86400
    for name in ("X", "SANC", "D", "Z"):
        res = eng.screen("eth", A(name), as_of=as_of, actor="demo")
        print(f"\n== {name}  -> {res['decision'].upper()} ({res['risk_level']})  entity={res['entity_id']}")
        for f in res["rules_fired"]:
            print(f"   [{f['rule']}] {f['name']}: {f['category']} {f['kind']} {f['share']:.1%}")
        for cat, x in res["exposure"].items():
            hops = " <- ".join(s["address"][:8] + ("(" + s["labels"][0][0] + ")" if s["labels"] else "") for s in x["path"])
            print(f"   đường đi: {hops}   txs: {x['path'][0].get('received_via_txs', [])}")
        if not res["rules_fired"]:
            print("   không có rule nào kích hoạt")
    print("\nFile mẫu:", tp, lp, "| DB:", os.path.join(out, "demo.db"))
    return 0
