"""SQLite schema + helpers. Đủ cho MVP; sản xuất lớn: Postgres (labels/cases) + ClickHouse (transfers)."""
import json
import sqlite3
import time

EVM_CHAINS = {"eth", "bsc", "polygon", "arbitrum", "optimism", "base", "avalanche"}


def norm(chain, addr):
    """EVM không phân biệt hoa/thường; BTC/Tron thì có."""
    a = addr.strip()
    return a.lower() if chain in EVM_CHAINS else a


SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks(
  chain TEXT NOT NULL, number INTEGER NOT NULL, hash TEXT NOT NULL,
  parent_hash TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY(chain, number));

CREATE TABLE IF NOT EXISTS transfers(
  id INTEGER PRIMARY KEY, chain TEXT NOT NULL, block_number INTEGER,
  block_hash TEXT NOT NULL DEFAULT '', tx_hash TEXT NOT NULL,
  log_index INTEGER NOT NULL DEFAULT 0, from_addr TEXT NOT NULL, to_addr TEXT NOT NULL,
  asset TEXT NOT NULL, amount REAL NOT NULL, usd REAL, ts INTEGER NOT NULL,
  UNIQUE(chain, block_hash, tx_hash, log_index));
CREATE INDEX IF NOT EXISTS ix_tr_to   ON transfers(chain, to_addr, ts);
CREATE INDEX IF NOT EXISTS ix_tr_from ON transfers(chain, from_addr, ts);
CREATE INDEX IF NOT EXISTS ix_tr_blk  ON transfers(block_hash);

-- Nhãn: không bao giờ ghi đè, chỉ supersede (giữ lịch sử).
CREATE TABLE IF NOT EXISTS labels(
  id INTEGER PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL,
  category TEXT NOT NULL, name TEXT, source TEXT NOT NULL, method TEXT,
  confidence REAL NOT NULL DEFAULT 0.5, trust_tier INTEGER NOT NULL DEFAULT 1,
  evidence TEXT, created_at INTEGER NOT NULL, superseded_by INTEGER);
CREATE INDEX IF NOT EXISTS ix_lab_addr ON labels(chain, address);

-- Cạnh gộp cụm kèm bằng chứng để audit / revert.
CREATE TABLE IF NOT EXISTS link_edges(
  id INTEGER PRIMARY KEY, chain TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
  signal TEXT NOT NULL, tx TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
  UNIQUE(chain, a, b, signal, tx));

CREATE TABLE IF NOT EXISTS entity_members(
  chain TEXT NOT NULL, address TEXT NOT NULL, entity_id TEXT NOT NULL,
  PRIMARY KEY(chain, address));
CREATE INDEX IF NOT EXISTS ix_em_ent ON entity_members(chain, entity_id);

CREATE TABLE IF NOT EXISTS screenings(
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, chain TEXT NOT NULL, address TEXT NOT NULL,
  decision TEXT NOT NULL, risk_level TEXT NOT NULL, policy_version TEXT, result TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, payload TEXT);
"""


def connect(path=":memory:"):
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    if path != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def audit(con, actor, action, payload=None):
    con.execute("INSERT INTO audit(ts,actor,action,payload) VALUES(?,?,?,?)",
                (int(time.time()), actor, action, json.dumps(payload, ensure_ascii=False, default=str)))
    con.commit()
