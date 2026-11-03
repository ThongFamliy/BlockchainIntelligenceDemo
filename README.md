# vnbi — Blockchain Intelligence riêng

Bộ khung BI chạy được (Python 3.10+, không phụ thuộc ngoài, SQLite). Dùng cho compliance / KYT / điều tra phòng thủ.

```
Node/CSV -> indexer (reorg-safe) -> transfers
Nguồn nhãn -> label service (provenance, supersede) ─┐
Heuristic gộp cụm (UTXO co-spend, EVM deposit-sweep) -> link_edges (kèm bằng chứng) -> entity
                                                        └> risk engine (haircut + decay + rule table) -> quyết định + đường đi
                                                                                                          └> API / CLI / audit
```

## Chạy thử
```bash
python -m pytest -q          # 19 test
python -m vnbi demo          # kịch bản tổng hợp end-to-end (địa chỉ giả)
```

## Dùng với dữ liệu của bạn
```bash
python -m vnbi import-labels labels.csv            # chain,address,category,name,source,confidence,trust_tier
python -m vnbi ingest-csv transfers.csv --chain eth --price ETH=3500   # from,to,token,amount,block_time,tx_hash
python -m vnbi cluster-evm transfers.csv --chain eth --exclude-file known_services.txt
python -m vnbi screen eth 0x...
BI_API_KEY=secret python -m vnbi serve --port 8080   # GET /address/{chain}/{addr}, POST /screen, /label, /edge/revoke
```
Indexer thật: `vnbi.indexer.Indexer(con, "eth", EvmRpc(url, tokens={...}))`.sync().

## Nối RPC thật (QuickNode)
```bash
cp .env.example .env          # điền EVM_RPC_URL; .env đã bị .gitignore
python -m vnbi rpc-check      # in block hiện tại, URL được che khoá
python -m vnbi index-evm --max-blocks 20    # nạp USDT/USDC + ETH từ ~10 block gần nhất
```
Khoá RPC chỉ nằm trong biến môi trường, không bao giờ trong mã nguồn. **Không đặt khoá vào bản demo công khai trên Vercel**: API demo không có xác thực nên ai cũng có thể tiêu hết hạn mức của bạn.

## Cổng web (GUI)
```bash
python -m vnbi demo --out demo_out                    # tạo dữ liệu mẫu
python -m vnbi --db demo_out/demo.db serve --port 8080
# mở http://127.0.0.1:8080  (một file HTML, không cần build, không phụ thuộc thư viện)
```
Màn hình: Kiểm tra địa chỉ (quyết định, rule, dấu vết dòng tiền, bằng chứng cụm + gỡ cạnh), Lịch sử, Nhãn (thêm + xem lịch sử), Nhật ký.
Nhập khoá API và tên người thao tác ở góc trái dưới; tên đó được ghi vào audit. Khi mở ra ngoài localhost hãy đặt `BI_API_KEY`, đặt sau reverse proxy có HTTPS và SSO.

## Deploy demo lên Vercel
Repo đã có `api/index.py`, `vercel.json`, `requirements.txt`, `.python-version`. Import repo vào Vercel, không cần cấu hình thêm (Framework: Other).
- Bản Vercel là **demo chỉ đọc, dữ liệu tổng hợp**: DB dựng lại trong `/tmp` mỗi lần khởi động, không thêm nhãn/gỡ cạnh được (API trả 403).
- Vercel là serverless, không có đĩa bền: **đừng dùng cho dữ liệu thật**. Bản thật chạy `python -m vnbi serve` trên VM/container có ổ đĩa + `BI_API_KEY` + HTTPS.
- Lỗi build/chạy: xem Functions → Logs của deployment; kiểm tra `/api/index?path=health` phải trả `{"ok": true, "demo": true}`.

## Nguyên tắc thiết kế
- Mỗi suy luận có **độ tin cậy + bằng chứng**; cạnh gộp cụm revoke được (`revoke_edge`) và cụm tách lại.
- Nhãn không bao giờ bị ghi đè (supersede/retract), có nguồn, phương pháp, tier.
- Rủi ro = **exposure** theo hạng mục (trực tiếp/gián tiếp, hop, tỉ trọng) → bảng rule trong `policy.json` (có version) → quyết định kèm đường đi tx. Không dùng điểm 0-100 mờ.
- Sàn/router là "đích dừng": không truy xuyên qua quỹ đã trộn. Có lọc bụi chống dusting.

## Giới hạn thật (đọc trước khi tin kết quả)
- `EvmRpc` **chưa được thử với node sống** (môi trường build không có mạng); logic reorg đã test bằng RPC giả. Chưa có internal transfer (cần trace API), chưa có Tron/BTC indexer (CSV/JSONL nạp được).
- Danh sách trừng phạt **không đóng gói sẵn**: tự tải từ nguồn chính thức và `import-labels`, cập nhật định kỳ.
- Chưa nhìn xuyên mixer/bridge/CoinJoin; EVM không có co-spend nên cụm chỉ dựa heuristic sweep (precision > recall).
- Ngưỡng trong `policy.json` là mẫu; compliance phải hiệu chỉnh trên ca thật. Không phải tư vấn pháp lý; kiểm tra quy định hiện hành của VN và nước đích.
- Nhãn là hào: không có nhãn tốt thì engine chỉ đúng như dữ liệu bạn nạp. Hướng thực tế: mua feed nền + tự xây nhãn Việt Nam (scam đầu tư, mule, OTC/P2P) từ ca của chính bạn.

## Lộ trình tiếp
1. Indexer Tron/USDT-TRC20 + BTC; trace API cho EVM. 2. ClickHouse/Postgres thay SQLite khi >10M transfer.
3. Re-screening định kỳ + webhook; case management. 4. UI đồ thị điều tra. 5. ML xếp hạng khi đã có đủ ca gắn nhãn.
