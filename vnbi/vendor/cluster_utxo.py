#!/usr/bin/env python3
"""UTXO address clustering via the common-input-ownership heuristic (CIOH).

Union-find over input addresses, with a guard that skips transactions that
look like CoinJoins, since those break the heuristic and would over-merge.

Input: JSON Lines, one transaction per line:
  {"txid": "...", "inputs": ["addrA", "addrB"], "outputs": [["addrC", 50000], ["addrD", 120000]]}
  outputs are [address, value_in_sats] pairs.

Usage:
  python cluster_utxo.py txs.jsonl            # prints clusters as JSON
  python cluster_utxo.py --selftest           # runs a tiny built-in test

This is a teaching/prototyping tool. A production system should also keep
per-edge evidence so merges can be audited and reverted.
"""
import json
import sys
from collections import Counter, defaultdict


class UnionFind:
    def __init__(self):
        self.parent = {}
        self.size = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def looks_like_coinjoin(tx, min_inputs=5, min_equal_outputs=3):
    """Heuristic CoinJoin detector.

    Flags a tx with many inputs and several outputs of identical value, the
    classic equal-denomination pattern. It is intentionally conservative about
    merging: a false positive only means we skip a merge (lower recall), while
    a false negative can wrongly merge unrelated people (worse).
    """
    inputs = tx["inputs"]
    values = [v for _, v in tx["outputs"]]
    if len(set(inputs)) < min_inputs or not values:
        return False
    most_common = Counter(values).most_common(1)[0][1]
    return most_common >= min_equal_outputs


def cluster(txs):
    uf = UnionFind()
    skipped = []
    for tx in txs:
        inputs = list(dict.fromkeys(tx["inputs"]))  # dedupe, keep order
        if not inputs:
            continue
        if looks_like_coinjoin(tx):
            skipped.append(tx["txid"])
            for a in inputs:
                uf.find(a)  # register addresses without merging
            continue
        first = inputs[0]
        uf.find(first)
        for other in inputs[1:]:
            uf.union(first, other)
    groups = defaultdict(list)
    for addr in list(uf.parent):
        groups[uf.find(addr)].append(addr)
    return list(groups.values()), skipped


def read_jsonl(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def selftest():
    txs = [
        {"txid": "t1", "inputs": ["A", "B"], "outputs": [["X", 1000], ["Y", 2000]]},
        {"txid": "t2", "inputs": ["B", "C"], "outputs": [["Z", 1500]]},
        {"txid": "t3", "inputs": ["D"], "outputs": [["A", 500]]},
        # CoinJoin-like: 6 inputs from different parties, 4 equal outputs
        {"txid": "cj", "inputs": ["P", "Q", "R", "S", "T", "U"],
         "outputs": [["o1", 100000], ["o2", 100000], ["o3", 100000],
                     ["o4", 100000], ["chg", 7777]]},
    ]
    clusters, skipped = cluster(txs)
    as_sets = sorted(sorted(c) for c in clusters)
    assert ["A", "B", "C"] in as_sets, as_sets          # t1 + t2 chain through B
    assert ["D"] in as_sets, as_sets                     # lone input stays alone
    assert skipped == ["cj"], skipped                    # CoinJoin not merged
    for solo in ("P", "Q", "R", "S", "T", "U"):
        assert [solo] in as_sets, (solo, as_sets)
    print("selftest ok:", as_sets, "skipped:", skipped)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        selftest()
    elif len(sys.argv) == 2:
        cl, sk = cluster(read_jsonl(sys.argv[1]))
        cl.sort(key=len, reverse=True)
        print(json.dumps({"clusters": cl, "coinjoin_skipped": sk}, indent=2))
    else:
        print(__doc__)
        sys.exit(1)
