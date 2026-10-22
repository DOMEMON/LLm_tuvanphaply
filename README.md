# Hệ thống tư vấn pháp lý và tự động tạo knowledge package

Hai hướng triển khai cùng nguyên tắc: **LLM hiểu yêu cầu, chương trình thực thi và đối chiếu dữ liệu nguồn**. Repository gồm mã nguồn, kiểm thử, cấu hình triển khai mẫu và hai hình kiến trúc hiện tại; không chứa model weights, lịch sử người dùng hay cấu hình riêng của máy phát triển.

| Hệ thống | Mục đích | Source và hướng dẫn |
| --- | --- | --- |
| **G8 — Hệ thống tư vấn pháp lý: Hỗ trợ tra cứu thủ tục hành chính** | Tra cứu 36 thủ tục thuộc phường Tăng Nhơn Phú, Thành phố Hồ Chí Minh; multi-intent và hội thoại nhiều lượt | [G8](G8-Source-code-LLM-tu-van-hanh-chinh-cong/README.md) |
| **G8.5 — Tự động tạo package từ dữ liệu dạng bảng cho hệ thống LLM** | Upload CSV/Excel → phân tích metadata → tạo package → hỏi đáp trên dataset được chọn | [G8.5](G8.5-He-thong-LLM-reusable/README.md) |

## Báo cáo và bản cập nhật 22/10/2026

Hai báo cáo IEEE nằm trong [thư mục report](report/README.md):

- [RP1 — Hệ thống tư vấn pháp lý: Hỗ trợ tra cứu thủ tục hành chính](report/RP1_he-thong-tu-van-phap-ly.pdf).
- [RP2 — Hệ thống tự động tạo package từ dữ liệu dạng bảng cho hệ thống LLM](report/RP2_he-thong-tu-dong-tao-package.pdf).

Bản source mới bổ sung kiểm tra yêu cầu/đối tượng cho G8 và sửa ràng buộc câu hỏi bảng cho G8.5: đếm dòng, tra dòng chính xác, tách multi-intent, cùng trường cho nhiều bản ghi, tên hiển thị khác mã định danh và giá trị văn bản có dấu nháy/escape. Nạp bảng dùng các lô có tham số để giảm chi phí chèn từng giá trị, vẫn giữ kiểu và bằng chứng nguồn.

Kiểm thử offline trên bản source công khai: **G8 223/223**, **G8.5 180/180**. Lượt hồi quy API G8.5 trên 11 dataset hoàn tất **221/221**, đạt đủ tiêu chí **218/221**, trong đó DEV **176/176**. Ba trường hợp còn lại là kết quả `partial` do ô thiếu hoặc giới hạn trả 1.000 dòng; các câu hỏi/gold và scorer được giữ nguyên. Chi tiết phạm vi nằm trong [README báo cáo](report/README.md). Các PDF giữ số liệu của phiên bản được đo khi viết bài.

## G8: tra cứu thủ tục hành chính

![Kiến trúc G8 — Hệ thống tư vấn dịch vụ Hành chính công](assets/g8-system-architecture.png)

- Tối đa **8 intent trong một tin nhắn**, kể cả các trường thông tin xen kẽ giữa nhiều thủ tục.
- Ngữ cảnh có cấu trúc giữ **2 thủ tục gần nhất**; lịch sử hội thoại lưu riêng trong PostgreSQL.
- Tra cứu hồ sơ, lệ phí, thời gian, nơi nộp và các trường trong nguồn. Truy vấn ngược dùng bộ lọc có kiểu trên bằng chứng đã rà soát; điều kiện chưa đủ nguồn không được tự suy ra.
- Qwen3-8B qua llama.cpp lập kế hoạch JSON; LangGraph kiểm tra và cho phép tối đa một lần sửa kế hoạch. Kết quả ràng buộc với đúng thủ tục, trường và nguồn.
- React/TypeScript cung cấp chat, lịch sử, nguồn tham chiếu, phản hồi và checklist. Hàng đợi FIFO có hai worker theo cấu hình mẫu.

Ví dụ: “Cho tôi giấy tờ đăng ký kết hôn, nơi nộp khai sinh và lệ phí khai tử”. Khả năng trả lời phụ thuộc dữ liệu nguồn và việc xác định đúng yêu cầu, không phải cam kết đúng với mọi cách hỏi.

## G8.5: từ bảng dữ liệu đến package tự động

![Pipeline G8.5 — tự động tạo, kiểm chứng và sử dụng knowledge package](assets/g85-auto-package-pipeline.png)

G8.5 hiện **không còn chỉ là bản document-RAG phải chuẩn bị package thủ công**. Workspace mới tiếp nhận CSV/XLSX và quản lý nhiều dataset riêng biệt:

1. Đọc bảng có header, chọn một sheet và khai báo ký hiệu ô thiếu khi cần; giữ nguyên giá trị nguồn.
2. LLM đề xuất mô tả/vai trò các cột, rồi rà lại metadata để chọn cột tên hiển thị và tạo mapping.
3. Chương trình tạo profile, ID từng dòng, bằng chứng từng ô, locator và SHA-256; đóng thành package có phiên bản.
4. Loader kiểm chứng schema, hash, ánh xạ và liên kết về nguồn trước khi nạp.
5. Người dùng chọn dataset để hỏi. Runtime tra cứu, tìm đoạn nguồn hoặc truy vấn toàn bảng có kiểm tra; mỗi intent có kết quả và bằng chứng riêng.

Không cần sửa mapping theo tên từng dataset trong luồng upload tự động. Metadata do model đề xuất **không phải chứng nhận hiểu đúng nghiệp vụ**; hệ thống không tự tạo chính sách, giải mã mã số hoặc thay dữ liệu nguồn bằng kiến thức của model.

UI có kéo thả file, tìm/chuyển bảng, xem cấu trúc, hội thoại mới, mở bằng chứng, sao chép, tải CSV, tải package ZIP và sáng/tối. Mỗi dataset có token/version riêng; context giữ hai bản ghi gần nhất. **Chọn một dataset cho mỗi câu hỏi**, chưa hỗ trợ join hay truy vấn chéo các dataset.

## Cấu trúc source

```text
.
├── G8-Source-code-LLM-tu-van-hanh-chinh-cong/
│   ├── backend/                  # FastAPI, hội thoại, jobs, planner/executor G8
│   ├── frontend/                 # React + TypeScript + Vite
│   ├── source-service/           # MCP tùy chọn, mặc định tắt
│   ├── data/administrative/      # Corpus chuẩn hóa và provenance
│   └── compose.yaml
├── G8.5-He-thong-LLM-reusable/
│   ├── g85/package_builder.py    # Bảng → metadata/mapping → package
│   ├── g85/package_runtime.py    # Kết nối package với lõi hỏi đáp
│   ├── g85/runtime/              # Planner, hợp đồng, thực thi, loader
│   ├── g85/tabular_lab/          # API, đọc bảng, DuckDB, context, web tĩnh
│   ├── g85/tests/                # Kiểm thử package/runtime
│   ├── examples/devices.csv      # Dữ liệu giả lập của bản source
│   └── compose.yaml
├── assets/                       # Hai hình kiến trúc hiện tại
├── report/                       # Hai báo cáo IEEE dạng PDF
└── tools/                        # Kiểm tra bản source công khai
```

G8 còn namespace `g3`, `g4`, `g5`, `g7` cho các module nền được import; không phải các hệ thống cũ phải khởi động. G8.5 mới độc lập, không cần database, frontend hay snapshot G8.

## Chạy trên máy riêng

Cần **Docker + Compose** và model server `/v1/chat/completions` hỗ trợ JSON-schema output. Cấu hình tham chiếu dùng **Qwen3-8B GGUF Q4_K_M qua llama.cpp**, không fine-tune để ghi nhớ dataset. Weights và giấy phép model do người triển khai chuẩn bị riêng.

Ví dụ model server cho G8.5; thay đường dẫn/tài nguyên theo máy:

```powershell
llama-server -m /path/to/Qwen3-8B-Q4_K_M.gguf --alias G85-Tabular-Qwen3-8B-Q4 --host 0.0.0.0 --port 18085 --jinja --reasoning off --ctx-size 12288 --parallel 1 --n-gpu-layers 99 --temp 0 --seed 42 --chat-template-kwargs '{"enable_thinking":false}'
```

Model nghe `0.0.0.0` để container truy cập qua host; **phải chặn truy cập bên ngoài bằng firewall**. Không public inference endpoint không có xác thực. Endpoint khác cần được kiểm thử lại schema, context, chất lượng và thời gian xử lý.

Trong thư mục hệ thống muốn chạy:

```powershell
Copy-Item .env.example .env
# Sửa model URL/name; G8 còn cần DB_PASSWORD riêng đủ mạnh.
docker compose --env-file .env config --quiet
docker compose up -d --build --wait
```

| Hệ thống | Địa chỉ local mặc định |
| --- | --- |
| G8 | http://localhost:13008 — backend http://localhost:18008/health |
| G8.5 | http://localhost:13085 — UI, `/docs`, `/health` cùng origin |

G8.5 mở trang là có thể upload, **không cần tạo package trước khi chạy**. Model phải sẵn sàng trước khi upload/hỏi. `/health` chỉ báo API/hàng đợi, không chứng nhận model sẵn sàng. Các địa chỉ trên không phải link public luôn hoạt động.

G8 giữ dữ liệu trong volume PostgreSQL; G8.5 dùng volume `datasets`. Không dùng `docker compose down -v` nếu cần giữ dữ liệu. Không tái sử dụng volume của deployment cũ khi chưa sao lưu/kiểm tra tương thích.

## Kiểm thử

G8 backend, trong `G8-Source-code-LLM-tu-van-hanh-chinh-cong/backend/`:

```powershell
uv sync --frozen
uv run pytest -q
```

G8 frontend, trong `frontend/`:

```powershell
npm ci
npm test
npm run build
```

G8.5, trong `G8.5-He-thong-LLM-reusable/`, Python 3.12:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest g85/tests g85/tabular_lab -q
```

Linux/macOS dùng `.venv/bin/python`. Test offline dùng dữ liệu tổng hợp và model giả lập; không cần GPU và **không phải accuracy benchmark LLM**. README từng hệ thống có hướng dẫn UI. Từ gốc repo chạy `python tools/check_public_tree.py` để rà file đưa lên Git.

## Phạm vi và bảo mật

- **G8** phục vụ nguồn của phường Tăng Nhơn Phú, không tự áp dụng cho địa phương khác hoặc bảo đảm thủ tục còn hiệu lực. 36 thủ tục runtime khác với 42 dòng release nguồn.
- **G8.5** nhận một bảng có header mỗi upload, CSV UTF-8 hoặc XLSX; tối đa 20 MiB, 32 cột và 100.000 ô cho package. Chưa có PDF/DOCX, join, workbook bố cục tự do, quy đổi đơn vị hoặc giải mã codebook. Bộ lọc ngữ nghĩa tự do trên văn bản chưa bật trong API.
- Chuỗi `NA` được giữ nguyên trừ khi người dùng khai báo nó là ký hiệu thiếu; ô thiếu không tự là 0. Hash kiểm tra tính toàn vẹn, không chứng minh dữ liệu/metadata đúng về ngữ nghĩa.
- **G8** dùng cookie trình duyệt để khôi phục lịch sử ở server; tài khoản Google giống nhau trên hai máy không tự đồng bộ cookie. **G8.5** giữ danh sách bảng/token/lịch sử hiển thị trong `sessionStorage` của tab; file và context lưu riêng ở server. Chưa có đồng bộ tài khoản giữa thiết bị.
- Compose mẫu chỉ bind localhost. G8.5 chưa có tenant/account auth đầy đủ, thu hồi token, quota đĩa hay chức năng xóa dữ liệu cho production. Public cần HTTPS, xác thực, rate limit, quản trị dữ liệu và sao lưu, không chỉ đổi host sang `0.0.0.0`.

Thư mục `report/` chứa hai PDF được công bố cùng source. Không kèm bộ benchmark riêng, dataset thử tải từ bên thứ ba, tài khoản, `.env` thật, tunnel state, logs hay weights. Quyền sử dụng model, thư viện và dữ liệu bên thứ ba vẫn theo giấy phép tương ứng.
