# Source code LLM tư vấn hành chính công — G8

Ứng dụng hỏi đáp nhiều ý/nhiều lượt theo corpus hành chính đã review. Backend giữ phiên/history/queue/ownership; LLM local lập plan; LangGraph kiểm tra và sửa có giới hạn; câu trả lời lấy evidence đúng thủ tục/trường. React UI có chat, history, sources, feedback và checklist.

## Source và dữ liệu

- `backend/app/rag/g7/`: planner, task/state, catalog, workflow được G8 kế thừa.
- `backend/app/rag/g8/`: graph/guard, turn checks và catalog filtering.
- `app/rag/g3/`, `g4/`, `g5/`: validator/retrieval/grounding/boundary G8 đang import, không phải các server riêng.
- `app/{accounts,auth,browser_sessions,chat,jobs,workspace}.py`: session và durable FIFO/history.
- `app/{checklists,forms}.py`: checklist và tải form đã review/hash.
- `source-service/`: MCP đọc nguồn hiện tại, opt-in.
- `frontend/`: đầy đủ UI và lockfile.
- `data/administrative/`: JSONL chuẩn hóa đủ phục vụ, decision/review records; không workbook raw, gold questions hay user history.

Release hạch toán 42 dòng nguồn; catalog runtime lọc còn 38 thủ tục hỗ trợ. Không phải dữ liệu mới cập nhật toàn quốc. Phần thiếu/xung đột phải abstain/báo riêng; phạm vi địa phương theo cấu hình nghiệp vụ gốc. Classification `D2_COMPANY_REAL` và provenance không bị đổi chỉ vì source công khai theo yêu cầu bàn giao.

## Chạy độc lập

Chuẩn bị model server `/v1/chat/completions` có JSON schema output, ví dụ Qwen3-8B Q4_K_M qua llama.cpp. Không kèm weights; xem README repo.

```powershell
# Chạy trong thư mục G8
Copy-Item .env.example .env
# Sửa password riêng, MODEL_URL và MODEL_NAME trong .env.
docker compose --env-file .env config --quiet
docker compose up -d --build --wait
```

Mở **http://localhost:13008**; backend **http://localhost:18008/health**, docs local **http://localhost:18008/docs**. `MODEL_URL` mẫu gọi model trên host cổng 8180, không phải endpoint đã cung cấp sẵn. DB mới tự migrate trong project `public-g8-review`; không nhập history/account tác giả. Cookie visitor 90 ngày, không cần login UI; backend vẫn xác thực.

### Form và MCP tùy chọn

Code checklist/form đầy đủ; binary form assets và manifest review phân phối riêng, **không nằm trong source export này**. `G6_FORMS_ENABLED=false` mặc định, checklist/chat vẫn chạy. Muốn tải form cần bộ assets đã rà quyền/nội dung, `FORMS_PATH`, SHA-256 manifest rồi bật flag; không bỏ hash/review để ép tải file.

MCP mặc định tắt, không gọi website bên ngoài. Muốn thử, đặt token review riêng đủ mạnh, `G5_REALTIME_MCP_ENABLED=true`, rồi:

```powershell
docker compose --profile mcp up -d --build --wait
```

Backend chỉ tra nguồn hiện tại sau đồng ý một lần. MCP service không expose host port. Source updates ở volume riêng; tra cứu không tự thành release corpus mới.

## Test

Trong `backend/`:

```powershell
uv sync --frozen
uv run pytest -q
```

56 test offline kiểm tra graph/contract/guard/catalog/hội thoại với corpus đi kèm. Không cần GPU/MCP live. `source-service/` cũng có test, chạy `uv sync --frozen` và `uv run pytest -q` tại thư mục đó.

Trong `frontend/` (46 test bao gồm visitor và account compatibility):

```powershell
npm ci
npm test
npm run build
```

Test live ví dụ: “Khai sinh cần hồ sơ gì, kết hôn có lệ phí không?”, rồi sửa một ý, hỏi tiếp theo thứ tự hoặc hỏi thủ tục ngoài scope. Đối chiếu text/evidence, địa phương và trạng thái từng ý. HTTP 200 không chứng minh nội dung đúng.

## Vận hành và giới hạn

Không `down -v` nếu cần history; không public `.env`/DB. Ports chỉ bind local; public cần HTTPS, cookie Secure, Origin/rate limit và che route nội bộ. Đây là hỗ trợ tham khảo theo nguồn, không bảo đảm hiệu lực pháp lý hiện tại.

G8 chuyên hành chính, có metadata/guard riêng. Để chuyển lĩnh vực bằng package/profile, dùng [G8.5 reusable](../G8.5-He-thong-LLM-reusable/README.md), không chỉ đổi tên corpus G8.
