"""CLI: python -m vnbi <lệnh>. Mặc định DB = bi.db (đổi bằng --db)."""
import argparse
import csv
import json
import os
import sys
from urllib.parse import urlparse

from . import api, cluster, db
from . import labels as L
from .indexer import STABLES
from .risk import RiskEngine
from .vendor.cluster_utxo import read_jsonl
from .vendor.evm_sweep_cluster import load as load_evm_rows


MAINNET_TOKENS = {  # token niêm yết mặc định trên Ethereum mainnet: contract -> (ký hiệu, decimals)
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
}


def load_env(path=".env"):
    """Nạp KEY=VALUE từ .env (nếu có) vào os.environ, không ghi đè biến đã đặt. .env đã nằm trong .gitignore."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def rpc_url():
    """URL RPC (vd. QuickNode) chỉ lấy từ biến môi trường, không bao giờ nằm trong mã nguồn."""
    u = os.environ.get("EVM_RPC_URL") or os.environ.get("QUICKNODE_URL")
    if not u:
        sys.exit("Chưa đặt EVM_RPC_URL. Đặt biến môi trường hoặc ghi vào file .env (xem .env.example).")
    return u


def mask_url(u):
    """Che phần khoá của URL RPC khi in ra: https://host/…abcd."""
    p = urlparse(u)
    tail = p.path.strip("/")
    return f"{p.scheme}://{p.hostname}/" + (("…" + tail[-4:]) if tail else "")


def parse_prices(items):
    p = dict(STABLES)
    for it in items or []:
        k, v = it.split("=")
        p[k.upper()] = float(v)
    return p


def ingest_csv(con, chain, path, prices):
    """CSV: from,to,token,amount,block_time,tx_hash  (cùng định dạng với evm_sweep_cluster)."""
    rows, seen, n = load_evm_rows(path), {}, 0
    for r in rows:
        asset = r["token"] or "ETH"
        li = seen[r["tx"]] = seen.get(r["tx"], -1) + 1
        p = prices.get(asset)
        cur = con.execute(
            "INSERT OR IGNORE INTO transfers(chain,block_number,block_hash,tx_hash,log_index,from_addr,to_addr,asset,amount,usd,ts)"
            " VALUES(?,NULL,'',?,?,?,?,?,?,?,?)",
            (chain, r["tx"], li, db.norm(chain, r["from"]), db.norm(chain, r["to"]), asset, r["amount"],
             None if p is None else r["amount"] * p, r["t"]))
        n += cur.rowcount
    con.commit()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vnbi", description=__doc__)
    ap.add_argument("--db", default="bi.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import-labels"); s.add_argument("csv")
    s = sub.add_parser("ingest-csv"); s.add_argument("csv"); s.add_argument("--chain", default="eth")
    s.add_argument("--price", action="append", help="ASSET=USD, vd ETH=3500")
    s = sub.add_parser("cluster-evm"); s.add_argument("csv"); s.add_argument("--chain", default="eth")
    s.add_argument("--exclude-file")
    s = sub.add_parser("cluster-utxo"); s.add_argument("jsonl"); s.add_argument("--chain", default="btc")
    s = sub.add_parser("rebuild"); s.add_argument("--chain", default="eth")
    s = sub.add_parser("screen"); s.add_argument("chain"); s.add_argument("address")
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8080)
    sub.add_parser("rpc-check")
    s = sub.add_parser("index-evm"); s.add_argument("--chain", default="eth")
    s.add_argument("--start-block", type=int, help="mặc định: block hiện tại - 10")
    s.add_argument("--max-blocks", type=int, default=20); s.add_argument("--price", action="append", help="ETH=3500")
    s = sub.add_parser("demo"); s.add_argument("--out", default="demo_out")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        from .demo import run_demo
        return run_demo(a.out)

    load_env()
    if a.cmd == "rpc-check":
        from .indexer import EvmRpc
        r = EvmRpc(rpc_url())
        print("RPC:", mask_url(rpc_url()), "| block hiện tại:", r.head())
        return 0
    con = db.connect(a.db)
    if a.cmd == "index-evm":
        from .indexer import EvmRpc, Indexer
        rpc = EvmRpc(rpc_url(), tokens=MAINNET_TOKENS)
        start = a.start_block if a.start_block is not None else rpc.head() - 10
        ix = Indexer(con, a.chain, rpc, start_block=start, prices=parse_prices(a.price))
        print(ix.sync(max_blocks=a.max_blocks))
    elif a.cmd == "import-labels":
        print(f"{L.import_csv(con, a.csv)} nhãn đã nạp")
    elif a.cmd == "ingest-csv":
        print(f"{ingest_csv(con, a.chain, a.csv, parse_prices(a.price))} transfer đã nạp")
    elif a.cmd == "cluster-evm":
        ex = set()
        if a.exclude_file:
            ex = {l.split("#")[0].strip().lower() for l in open(a.exclude_file) if l.split("#")[0].strip()}
        r = cluster.evm_sweep_edges(con, a.chain, load_evm_rows(a.csv), exclude=ex)
        print({k: v for k, v in r.items() if k != "detail"}, cluster.rebuild(con, a.chain))
    elif a.cmd == "cluster-utxo":
        print(cluster.utxo_edges(con, a.chain, read_jsonl(a.jsonl)), cluster.rebuild(con, a.chain))
    elif a.cmd == "rebuild":
        print(cluster.rebuild(con, a.chain))
    elif a.cmd == "screen":
        print(json.dumps(RiskEngine(con).screen(a.chain, a.address, actor="cli"), indent=2, ensure_ascii=False, default=str))
    elif a.cmd == "serve":
        api.serve(api.Service(con), a.host, a.port)


if __name__ == "__main__":
    main()
