"""Label service: provenance + versioning + conflict-aware. Nhãn là "hào" của BI."""
import csv
import json
import time

from .db import audit, norm

# Hạng mục "đích dừng": quỹ gộp (sàn, router) -> không truy ngược xuyên qua vì tiền đã trộn.
TERMINAL = {"exchange", "payment_processor", "defi_router"}


def add_label(con, chain, address, category, source, *, name=None, method="manual",
              confidence=0.7, trust_tier=1, evidence=None, actor="system"):
    """Thêm nhãn. Nhãn cũ cùng (address, category, source) bị supersede, không xoá."""
    address = norm(chain, address)
    now = int(time.time())
    cur = con.execute(
        "INSERT INTO labels(chain,address,category,name,source,method,confidence,trust_tier,evidence,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (chain, address, category, name, source, method, float(confidence), int(trust_tier),
         json.dumps(evidence, ensure_ascii=False) if evidence is not None else None, now))
    new_id = cur.lastrowid
    con.execute(
        "UPDATE labels SET superseded_by=? WHERE chain=? AND address=? AND category=? AND source=?"
        " AND superseded_by IS NULL AND id<>?",
        (new_id, chain, address, category, source, new_id))
    con.commit()
    audit(con, actor, "label.add", {"id": new_id, "chain": chain, "address": address,
                                    "category": category, "source": source, "confidence": confidence})
    return new_id


def retract_label(con, label_id, actor, reason):
    """Rút nhãn (vd. xác minh sai). Giữ dòng, đánh dấu superseded_by = -1."""
    con.execute("UPDATE labels SET superseded_by=-1 WHERE id=?", (label_id,))
    con.commit()
    audit(con, actor, "label.retract", {"id": label_id, "reason": reason})


def labels_for(con, chain, addresses, min_confidence=0.0):
    """Nhãn đang hiệu lực của một tập địa chỉ (thành viên 1 entity)."""
    rows = con.execute(
        "SELECT * FROM labels WHERE chain=? AND superseded_by IS NULL AND confidence>=?"
        " AND address IN (SELECT value FROM json_each(?))"
        " ORDER BY trust_tier DESC, confidence DESC, id DESC",
        (chain, min_confidence, json.dumps(list(addresses)))).fetchall()
    return [dict(r) for r in rows]


def history(con, chain, address):
    rows = con.execute("SELECT * FROM labels WHERE chain=? AND address=? ORDER BY id",
                       (chain, norm(chain, address))).fetchall()
    return [dict(r) for r in rows]


def import_csv(con, path, actor="import"):
    """CSV: chain,address,category,name,source,confidence,trust_tier  (name/confidence/tier tuỳ chọn).

    Dùng để nạp danh sách công khai bạn tự tải (vd. địa chỉ bị trừng phạt từ nguồn chính thức,
    cập nhật định kỳ - KHÔNG hardcode, danh sách thay đổi liên tục) và nhãn nội bộ.
    """
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            add_label(con, r["chain"].strip(), r["address"], r["category"].strip(),
                      r.get("source") or "csv", name=r.get("name") or None, method="import",
                      confidence=float(r.get("confidence") or 0.7),
                      trust_tier=int(r.get("trust_tier") or 1), actor=actor)
            n += 1
    return n
