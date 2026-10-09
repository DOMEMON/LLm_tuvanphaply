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

Release hạch toán 42 dòng nguồn; catalog runtime có **36 thủ tục hỗ trợ**. Không phải dữ liệu mới cập nhật toàn quốc. Phần thiếu/xung đột phải abstain/báo riêng; phạm vi địa phương theo cấu hình nghiệp vụ gốc. Classification `D2_COMPANY_REAL` và provenance không bị đổi chỉ vì source công khai theo yêu cầu bàn giao.

## Khả năng hỏi đáp hiện tại

- **Tối đa 8 ý mỗi tin nhắn**, có thể xen kẽ các field của những thủ tục khác nhau. Ví dụ: giấy tờ kết hôn, nơi nộp khai sinh, lệ phí khai tử. Mỗi task giữ field, scope và evidence riêng; vượt giới hạn phải yêu cầu chia nhỏ, không cố tình bỏ ý thứ chín.
- **Context giữ 2 thủ tục gần nhất** bằng state có cấu trúc, không đưa nguyên văn các câu hỏi cũ vào planner. Lịch sử chat vẫn lưu trong PostgreSQL và mở lại được bằng cookie. Nêu tên thủ tục cũ là yêu cầu mới; tham chiếu thứ tự không còn đủ context phải hỏi lại. Menu của một danh sách catalog là bản đồ lựa chọn riêng, không phải context của toàn bộ thủ tục.
- **Truy vấn ngược**: lọc các thủ tục có phí/miễn phí, chỉ trực tiếp/có trực tuyến, thời gian theo số ngày, hoặc field giấy tờ/nơi nộp chứa một cụm từ; có thể kết hợp lĩnh vực và nhiều điều kiện AND. Có thể hỏi nhiều danh sách cùng câu hoặc kết hợp danh sách với tra cứu một thủ tục; tổng vẫn tối đa 8 task. “Trong số đó” lọc tiếp trên kết quả trước do backend giữ ID.

Planner tạo `CatalogPredicate` có kiểu; `mentor_catalog.py` lọc trên field evidence đã review, không dùng danh sách đáp án model ghi nhớ. Miễn phí bản chính nhưng thu phí bản sao không phải miễn phí toàn bộ; “chỉ trực tiếp” khác “cả hai”. Nguồn thiếu/chưa đọc chắc được là unknown, không tự coi là miễn phí hay không hỗ trợ. OR phức tạp, so sánh mức tiền hoặc tiêu chí ngoài nguồn chưa được cam kết; phải làm rõ thay vì trả danh sách rộng hơn. `mentor_context.py` giới hạn context và xử lý các tham chiếu.

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

**179 test offline** kiểm tra graph/contract/guard/catalog/hội thoại với corpus đi kèm, gồm giới hạn 8 task, migration state cũ về 2 thủ tục, bộ lọc ngược và binding evidence riêng từng ý. Test chạy trực tiếp trên source trong thư mục này; không cần snapshot riêng của tác giả, GPU hoặc MCP live. `source-service/` cũng có test, chạy `uv sync --frozen` và `uv run pytest -q` tại thư mục đó. Test offline dùng plan/model giả lập để kiểm tra contract và executor, không phải accuracy benchmark của LLM thật.

Trong `frontend/` (46 test bao gồm visitor và account compatibility):

```powershell
npm ci
npm test
npm run build
```

Test live ví dụ:

```text
Tôi muốn biết giấy tờ kết hôn, nơi nộp khai sinh, lệ phí khai tử.
Liệt kê thủ tục có thu phí; cho tôi hồ sơ kết hôn; liệt kê thủ tục chỉ nộp trực tiếp thuộc văn hóa xã hội.
```

Trong một conversation mới, thử lần lượt:

```text
Kết hôn cần giấy tờ gì?
Thêm khai sinh, nộp ở đâu?
Bây giờ khai tử cần giấy tờ gì?
Hai thủ tục gần nhất nộp ở đâu?
```

Lượt cuối phải là khai sinh + khai tử, không kéo lại kết hôn. Đối chiếu text/evidence, địa phương và trạng thái từng ý. HTTP 200 không chứng minh nội dung đúng; câu đã dùng phát triển chỉ là DEV/regression, cần thêm câu mới để đánh giá độc lập. Model vẫn có thể làm rõ hoặc từ chối quá mức; không coi các bộ lọc có kiểu là đã giải quyết mọi cách hỏi tự nhiên.

## Vận hành và giới hạn

Không `down -v` nếu cần history; không public `.env`/DB. Ports chỉ bind local; public cần HTTPS, cookie Secure, Origin/rate limit và che route nội bộ. Đây là hỗ trợ tham khảo theo nguồn, không bảo đảm hiệu lực pháp lý hiện tại.

G8 chuyên hành chính, có metadata/guard riêng. Để chuyển lĩnh vực bằng package/profile, dùng [G8.5 reusable](../G8.5-He-thong-LLM-reusable/README.md), không chỉ đổi tên corpus G8.
