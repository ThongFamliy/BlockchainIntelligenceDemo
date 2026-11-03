import os, sys, tempfile, unittest, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vnbi.vendor.evm_sweep_cluster import detect, load

T0 = 1_700_000_000
_n = [0]
def tx(f, t, tok, amt, dt):
    _n[0] += 1
    return [f, t, tok, amt, T0 + dt, f"0xtx{_n[0]:04d}"]

def build():
    r = []
    # A. Sàn thật: 6 deposit, gas từ G1, sweep về H1 sau 5-20 phút, khách khác nhau
    for i in range(6):
        d = f"0xdep{i}"
        r.append(tx("0xg1", d, "ETH", 0.003, i * 100))
        r.append(tx(f"0xuser{i}", d, "USDT", 100 + i * 7, i * 100 + 50))
        r.append(tx(d, "0xh1", "USDT", 100 + i * 7, i * 100 + 600))
    # B. DEX router: nhiều user gửi vào, không có sweep/gas chung
    for i in range(8):
        r.append(tx(f"0xtrader{i}", "0xrouter", "USDT", 500, i * 60))
        r.append(tx("0xrouter", f"0xtrader{i}", "WETH", 0.3, i * 60 + 5))
    # C. Airdrop farm: 7 ví cùng nhận từ 1 distributor, gas chung, sweep về W (độ đa dạng thấp)
    for i in range(7):
        d = f"0xfarm{i}"
        r.append(tx("0xgf", d, "ETH", 0.002, i * 10))
        r.append(tx("0xdistributor", d, "ARB", 1250, i * 10 + 20))
        r.append(tx(d, "0xwfarm", "ARB", 1250, i * 10 + 300))
    # D. Bridge nằm trong exclude: 6 deposit giống hệt sàn nhưng đích bị loại
    for i in range(6):
        d = f"0xbr{i}"
        r.append(tx("0xg2", d, "ETH", 0.003, i * 100))
        r.append(tx(f"0xbu{i}", d, "USDC", 200, i * 100 + 50))
        r.append(tx(d, "0xbridge", "USDC", 200, i * 100 + 400))
    # E. Cụm quá nhỏ: 2 deposit
    for i in range(2):
        d = f"0xsmall{i}"
        r.append(tx("0xg3", d, "ETH", 0.003, i))
        r.append(tx(f"0xsu{i}", d, "USDT", 50, i + 10))
        r.append(tx(d, "0xh3", "USDT", 50, i + 100))
    # F. Sweep quá chậm (3 ngày): không phải sweep tự động
    for i in range(6):
        d = f"0xslow{i}"
        r.append(tx("0xg4", d, "ETH", 0.003, i))
        r.append(tx(f"0xlu{i}", d, "USDT", 80, i + 10))
        r.append(tx(d, "0xh4", "USDT", 80, 3 * 86400 + i))
    return r

class T(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        w = csv.writer(self.f); w.writerow("from,to,token,amount,block_time,tx_hash".split(","))
        w.writerows(build()); self.f.close()
        self.rows = load(self.f.name)
    def tearDown(self): os.unlink(self.f.name)

    def test_finds_only_real_exchange_cluster(self):
        out = detect(self.rows, exclude={"0xbridge"})
        self.assertEqual([c["hot_wallet"] for c in out], ["0xh1"])
        c = out[0]
        self.assertEqual(c["gas_funder"], "0xg1")
        self.assertEqual(c["n_deposit_addresses"], 6)
        self.assertEqual(len(c["evidence"]["sweeps"]), 6)

    def test_farm_rejected_by_depositor_diversity(self):
        out = detect(self.rows, exclude={"0xbridge"})
        self.assertNotIn("0xwfarm", [c["hot_wallet"] for c in out])

    def test_bridge_flagged_without_exclusion(self):
        out = detect(self.rows)
        self.assertIn("0xbridge", [c["hot_wallet"] for c in out])  # chứng minh exclusion list cần thiết

    def test_gas_required(self):
        rows = [r for r in self.rows if not (r["token"] == "ETH" and r["from"] == "0xg1")]
        self.assertNotIn("0xh1", [c["hot_wallet"] for c in detect(rows, exclude={"0xbridge"})])
        self.assertIn("0xh1", [c["hot_wallet"] for c in detect(rows, exclude={"0xbridge"}, require_gas=False)])

if __name__ == "__main__":
    unittest.main(verbosity=2)
