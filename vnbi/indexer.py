"""Indexer reorg-safe. Mỗi dòng dẫn xuất gắn block_hash; reorg -> rollback theo hash rồi replay.

`rpc` chỉ cần 3 hàm: head() -> int, block(n) -> {number, hash, parent_hash, timestamp, transfers:[...]}.
EvmRpc bên dưới là bản JSON-RPC thật (CHƯA kiểm thử với node sống trong môi trường build này);
bản kiểm thử dùng FakeRpc (tests/test_indexer.py).

Giới hạn đã biết: chỉ thấy native transfer ở tầng tx + ERC-20 Transfer log. Internal transfer
(contract gọi contract) cần trace API (Erigon/Reth `trace_block`) -> việc tiếp theo.
"""
import json
import time
import urllib.request

from .db import audit, norm

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STABLES = {"USDT": 1.0, "USDC": 1.0, "DAI": 1.0}


class Indexer:
    def __init__(self, con, chain, rpc, *, start_block=None, prices=None, confirmations=12):
        self.con, self.chain, self.rpc = con, chain, rpc
        self.start_block = start_block
        self.prices = {**STABLES, **(prices or {})}
        self.confirmations = confirmations
        self.rollbacks = 0

    def cursor(self):
        r = self.con.execute("SELECT MAX(number) m FROM blocks WHERE chain=?", (self.chain,)).fetchone()
        return r["m"]

    def _stored(self, n):
        return self.con.execute("SELECT * FROM blocks WHERE chain=? AND number=?", (self.chain, n)).fetchone()

    def sync(self, max_blocks=500):
        head = self.rpc.head()
        cur = self.cursor()
        n = cur + 1 if cur is not None else (self.start_block if self.start_block is not None else head)
        done = 0
        while n <= head and done < max_blocks:
            blk = self.rpc.block(n)
            prev = self._stored(n - 1) if n > 0 else None
            if prev is not None and prev["hash"] != blk["parent_hash"]:
                n = self._rollback(n - 1) + 1
                continue
            self._store(blk)
            n += 1
            done += 1
        return {"head": head, "cursor": self.cursor(), "stored": done, "rollbacks": self.rollbacks}

    def _rollback(self, k):
        """Xoá các block mồ côi từ k lùi về điểm chung; trả về chiều cao còn hợp lệ."""
        while k >= 0:
            row = self._stored(k)
            if row is None or self.rpc.block(k)["hash"] == row["hash"]:
                return k
            with self.con:
                self.con.execute("DELETE FROM transfers WHERE chain=? AND block_hash=?", (self.chain, row["hash"]))
                self.con.execute("DELETE FROM blocks WHERE chain=? AND number=?", (self.chain, k))
            self.rollbacks += 1
            audit(self.con, "indexer", "reorg.rollback", {"chain": self.chain, "number": k, "orphan": row["hash"]})
            k -= 1
        return -1

    def _usd(self, asset, amount):
        p = self.prices.get(asset)
        return None if p is None else amount * p

    def _store(self, blk):
        with self.con:  # nguyên tử: block + transfers cùng thành công hoặc cùng huỷ
            self.con.execute("INSERT OR REPLACE INTO blocks(chain,number,hash,parent_hash,ts) VALUES(?,?,?,?,?)",
                             (self.chain, blk["number"], blk["hash"], blk["parent_hash"], blk["timestamp"]))
            for t in blk["transfers"]:
                self.con.execute(
                    "INSERT OR IGNORE INTO transfers(chain,block_number,block_hash,tx_hash,log_index,from_addr,to_addr,"
                    "asset,amount,usd,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (self.chain, blk["number"], blk["hash"], t["tx_hash"], t.get("log_index", 0),
                     norm(self.chain, t["from"]), norm(self.chain, t["to"]), t["asset"], t["amount"],
                     self._usd(t["asset"], t["amount"]), blk["timestamp"]))

    def finalized_height(self):
        return self.rpc.head() - self.confirmations


class EvmRpc:
    def __init__(self, url, tokens=None, native="ETH", timeout=20):
        """tokens: {contract_lowercase: (symbol, decimals)} - chỉ index các token bạn niêm yết."""
        self.url, self.timeout, self.native = url, timeout, native
        self.tokens = {k.lower(): v for k, v in (tokens or {}).items()}
        self._id = 0

    def _call(self, method, params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = json.loads(r.read())
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    def head(self):
        return int(self._call("eth_blockNumber", []), 16)

    def block(self, n):
        b = self._call("eth_getBlockByNumber", [hex(n), True])
        transfers = []
        for tx in b["transactions"]:
            v = int(tx["value"], 16)
            if v > 0 and tx.get("to"):
                transfers.append({"tx_hash": tx["hash"], "log_index": -1, "from": tx["from"], "to": tx["to"],
                                  "asset": self.native, "amount": v / 1e18})
        if self.tokens:
            logs = self._call("eth_getLogs", [{"blockHash": b["hash"], "topics": [TRANSFER_TOPIC],
                                               "address": list(self.tokens)}])
            for lg in logs:
                if len(lg["topics"]) != 3:  # ERC-721 có 4 topic: bỏ qua
                    continue
                sym, dec = self.tokens[lg["address"].lower()]
                transfers.append({"tx_hash": lg["transactionHash"], "log_index": int(lg["logIndex"], 16),
                                  "from": "0x" + lg["topics"][1][-40:], "to": "0x" + lg["topics"][2][-40:],
                                  "asset": sym, "amount": int(lg["data"], 16) / 10 ** dec})
        return {"number": n, "hash": b["hash"], "parent_hash": b["parentHash"],
                "timestamp": int(b["timestamp"], 16), "transfers": transfers}
