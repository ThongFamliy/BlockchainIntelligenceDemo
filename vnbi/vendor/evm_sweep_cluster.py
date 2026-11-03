#!/usr/bin/env python3
"""Phát hiện cụm "deposit address của sàn" trên EVM bằng heuristic sweep + gas funder.

Input CSV: from,to,token,amount,block_time,tx_hash
  - token = ETH/NATIVE/(rỗng) -> chuyển native (dùng để nhận diện nạp gas)
  - block_time = epoch giây hoặc ISO-8601
Output: JSON danh sách cụm, mỗi cụm có điểm tin cậy, lý do và evidence (tx_hash) cho từng cạnh.

Đây là heuristic, KHÔNG phải sự thật: kết quả là giả thuyết cần xác minh (precision > recall).
Dùng cho compliance / điều tra phòng thủ.
"""
import argparse, csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone

NATIVE = {"", "ETH", "NATIVE", "BNB", "MATIC", "AVAX"}
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


def parse_time(s):
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "from": r["from"].strip().lower(), "to": r["to"].strip().lower(),
                "token": r["token"].strip().upper(), "amount": float(r["amount"]),
                "t": parse_time(r["block_time"]), "tx": r["tx_hash"].strip()})
    rows.sort(key=lambda x: x["t"])
    return rows


def detect(rows, *, sweep_window=3600, min_ratio=0.9, min_deposits=5,
           gas_lookback=86400, max_gas=0.05, min_funder_cover=0.6,
           min_depositor_diversity=0.6, min_amount=1.0, require_gas=True,
           exclude=frozenset()):
    exclude = {a.lower() for a in exclude} | {ZERO, DEAD}
    inbound = defaultdict(list)    # token inbound của từng địa chỉ
    outbound = defaultdict(list)   # token outbound
    native_in = defaultdict(list)  # gas nhận vào
    for r in rows:
        if r["token"] in NATIVE:
            if r["amount"] <= max_gas:
                native_in[r["to"]].append(r)
        else:
            inbound[r["to"]].append(r)
            outbound[r["from"]].append(r)

    # 1. Tìm cạnh sweep d -> h
    sweeps = {}  # (d,h,token) -> evidence
    for d, outs in outbound.items():
        if d in exclude:
            continue
        for o in outs:
            h = o["to"]
            if h == d or h in exclude or o["amount"] < min_amount:
                continue
            # d phải gần như chỉ gửi về 1 đích cho token này (không phải ví người dùng thật)
            dests = defaultdict(float)
            for x in outs:
                if x["token"] == o["token"]:
                    dests[x["to"]] += x["amount"]
            if dests[h] / sum(dests.values()) < 0.9:
                continue
            # có nhận token từ bên ngoài ngay trước đó (không tính tiền từ chính h: vòng tròn)
            recv = [i for i in inbound[d] if i["token"] == o["token"]
                    and 0 <= o["t"] - i["t"] <= sweep_window and i["from"] != h]
            if not recv:
                continue
            total = sum(i["amount"] for i in recv)
            if not (min_ratio <= o["amount"] / total <= 1.0 + 1e-9):
                continue
            key = (d, h, o["token"])
            if key not in sweeps:  # giữ sweep đầu tiên
                sweeps[key] = {"deposit": d, "hot": h, "token": o["token"],
                               "sweep_tx": o["tx"], "sweep_t": o["t"], "amount": o["amount"],
                               "deposit_txs": [i["tx"] for i in recv],
                               "depositors": sorted({i["from"] for i in recv})}

    # 2. Gom theo ví đích
    by_hot = defaultdict(list)
    for s in sweeps.values():
        by_hot[s["hot"]].append(s)

    clusters = []
    for h, ss in by_hot.items():
        deposits = {}
        for s in ss:
            deposits.setdefault(s["deposit"], s)
        if len(deposits) < min_deposits:
            continue
        # 3. Gas funder chung
        funder_cover = defaultdict(list)
        for d, s in deposits.items():
            seen = set()
            for g in native_in.get(d, []):
                if 0 <= s["sweep_t"] - g["t"] <= gas_lookback and g["from"] not in seen \
                        and g["from"] not in exclude and g["from"] != d:
                    seen.add(g["from"])
                    funder_cover[g["from"]].append((d, g["tx"]))
        top_f, covered = (None, [])
        if funder_cover:
            top_f, covered = max(funder_cover.items(), key=lambda kv: len(kv[1]))
        cover = len(covered) / len(deposits)
        if require_gas and cover < min_funder_cover:
            continue
        # 4. Đa dạng người gửi: farm/airdrop thì nguồn nạp là 1 distributor
        all_dep = [x for s in deposits.values() for x in s["depositors"]]
        diversity = len(set(all_dep)) / max(len(all_dep), 1)
        if diversity < min_depositor_diversity:
            continue
        span = max(s["sweep_t"] for s in deposits.values()) - min(s["sweep_t"] for s in deposits.values())
        n = len(deposits)
        score = 0.35 + min(n, 20) / 20 * 0.25 + cover * 0.25 + diversity * 0.15
        reasons = [f"{n} địa chỉ nhận token rồi sweep >= {min_ratio:.0%} số dư về {h} trong <= {sweep_window}s",
                   f"{cover:.0%} địa chỉ được nạp gas bởi {top_f}" if top_f else "không thấy gas funder chung",
                   f"độ đa dạng người gửi {diversity:.2f} (cao = nhiều khách khác nhau)"]
        clusters.append({
            "hot_wallet": h, "gas_funder": top_f, "confidence": round(min(score, 0.95), 2),
            "n_deposit_addresses": n, "sweep_time_span_s": span, "reasons": reasons,
            "label_suggestion": "exchange_or_custodial_deposit_cluster (cần xác minh)",
            "deposit_addresses": sorted(deposits),
            "evidence": {"sweeps": [{"deposit": d, "sweep_tx": s["sweep_tx"], "deposit_txs": s["deposit_txs"]}
                                    for d, s in sorted(deposits.items())],
                         "gas_txs": [{"deposit": d, "tx": tx} for d, tx in covered]}})
    return sorted(clusters, key=lambda c: -c["confidence"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--exclude-file", help="file txt: mỗi dòng 1 địa chỉ cần loại (router, bridge, mixer, CEX đã biết...)")
    ap.add_argument("--sweep-window", type=int, default=3600)
    ap.add_argument("--min-deposits", type=int, default=5)
    ap.add_argument("--no-require-gas", action="store_true")
    a = ap.parse_args()
    ex = set()
    if a.exclude_file:
        ex = {l.split("#")[0].strip().lower() for l in open(a.exclude_file) if l.split("#")[0].strip()}
    out = detect(load(a.csv), sweep_window=a.sweep_window, min_deposits=a.min_deposits,
                 require_gas=not a.no_require_gas, exclude=ex)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
