# Hệ thống LLM reusable — G8.5

Đầy đủ source shell backend/frontend và lõi tái sử dụng, **không chọn sẵn dataset nấu ăn, không chứa ReciFineGold hoặc history của deployment đang chạy**. Export này không thay đổi phiên bản trên máy tác giả. Một deployment chọn một knowledge package đã review; thay package/profile cùng contract thì giữ planner, engine, state và UI.

## 1. Đọc code

| Thành phần | Vai trò |
| --- | --- |
| `backend/app/rag/g85/contracts.py` | Task/Plan/State/transition; tách version dataset/profile |
| `planner.py` | LLM hiểu nhiều ý, focus/scope; LangGraph plan/validate/repair |
| `engine.py` | Execute từng task, retrieval, chọn segment IDs, render trạng thái |
| `knowledge.py`, `retriever.py` | BM25 lexical và interface LangChain Core |
| `package_loader.py` | Schema/hash/count/ID/review/capability/scale, fail closed |
| `extensions.py` | Registry policy opt-in, mặc định rỗng; không chạy code từ dataset |
| `backend_bridge.py`, `transport.py` | Boundary HTTP shell/package/model endpoint |
| `backend/app/` | Cookie, ownership, history, FIFO jobs, feedback |
| `frontend/` | UI đọc domain title/attribute/capability qua `/api/v1/domain` |
| `backend/tools/` | Importer JSONL/CSV và validator, không activate khi ingest |

Không kèm loader/policy hành chính G8, recipe config hoặc script thử model cũ; không cần G8/image lịch sử để build.

## 2. Tạo knowledge package

Chuẩn bị Python 3.12 + uv; trong `backend/` chạy `uv sync --frozen`. Input JSONL/CSV có cột chuỗi ID/title/text, tùy chọn URL. Mapping mẫu trong `profiles/mapping.json`. Copy profile template ra vùng data riêng và sửa ID/title/language/scope/classification đúng nguồn. Template không chứng nhận nội dung hay quyền sử dụng.

Thử với ba bản ghi Alpha/Beta/Gamma giả lập, không phải kiến thức thiết bị thật:

```powershell
# Trong backend/
uv run python -m tools.ingest --input ../examples/documents.jsonl --profile ../profiles/documents.template.json --mapping ../profiles/mapping.json --version software-demo-v1 --output ../data/packages/software-demo-v1 --reviewed
uv run python -m tools.validate --package ../data/packages/software-demo-v1
```

`--reviewed` là xác nhận operator sau khi rà nội dung/quyền sử dụng, không phải kiểm duyệt AI. Output dùng version/thư mục mới, không ghi đè package cũ. `data/packages/` đã ignore; không tự commit tài liệu nội bộ. Với dữ liệu thật thay input/profile bằng nguồn của bạn.

```text
manifest.json     # version, file hashes, count, số đoạn approved
profile.json      # title/domain/language/scope/capability/attribute/limits
groups.json       # nhóm chủ đề
entities.jsonl    # stable ID, title, metadata/facts
sources.jsonl     # source ID, URL nếu có, hash
passages.jsonl    # text gốc, source/entity binding, hash, locator, review_status
```

Passage không APPROVED không được dùng; hash/ID sai hay thiếu approved passage làm validator/startup từ chối. Hash là integrity, không phải chữ ký/chứng minh nội dung đúng. Importer không dịch hay sinh tri thức; PDF/DOCX/Excel cần ETL/chunk trước.

## 3. Chạy

```powershell
# Trong thư mục G8.5
Copy-Item .env.example .env
# Sửa DB_PASSWORD, MODEL_URL, MODEL_NAME.
# KNOWLEDGE_PACKAGE_PATH=./data/packages/software-demo-v1
docker compose --env-file .env config --quiet
docker compose up -d --build --wait
```

Mở **http://localhost:13085**; backend **http://localhost:18085/health**, **/api/v1/domain**. Cần model `/v1/chat/completions` hỗ trợ JSON schema; xem README repo. /health xác nhận shell/package/DB, không chứng minh model trả đúng câu.

DB project `public-g85-review` mới, tự migrate; package bind read-only. UI gọi API cùng origin qua nginx. Visitor không cần login, cookie khôi phục phiên/history, content ở PostgreSQL. Template tắt form/checklist/external lookup/actions.

Ví dụ kiểm thử: “Alpha kiểm tra bao lâu, Beta hỗ trợ chế độ nào?”, “Gamma tổng cộng chính xác bao nhiêu phút, riêng bước cuối thì sao?”, “Chỉ cho tôi điều kiện dừng kiểm tra Alpha”. Đối chiếu nguồn: 5 phút bước cuối không phải tổng gồm thời gian chờ chưa ghi.

## 4. Test, bảo mật và giới hạn

```powershell
# Trong backend/: offline, không cần GPU
uv run pytest -q
# Trong frontend/
npm ci
npm test
npm run build
```

17 test reusable với dữ liệu giả lập: review/hash, entity retrieval, version boundary, menu/focus và evidence binding. UI có 46 test API/component/session/visitor/account compatibility. Test không chứng minh relevance đúng mọi câu: đoạn đúng nguồn vẫn có thể chưa trả lời đúng nhu cầu, cần đánh giá live với câu mới.

Lõi có contract/guard tổng quát: quote hiện tại, ID/field, task binding, ordinal, missing evidence; không phải “không có rule”. Không có bảng đáp án theo record. Profile mô tả capability/data; policy nghiệp vụ phải opt-in, kiểm thử liên domain.

Giới hạn: 64 entity trong catalog planner, 8 task/lượt, một lần repair; BM25 lexical, chưa dense multilingual/reranker/dịch có kiểm chứng. Trả text nguồn nguyên văn. Một bước có thời lượng không tự là tổng; thuộc tính thiếu phải báo thiếu. Citation không tự chứng minh semantic completeness.

Không expose model/DB/MCP ra Internet. Public cần HTTPS, cookie Secure, Origin/rate limit, backup/history policy. `.env`/weights/DB/private corpus được ignore. Đổi model cần đo lại planner/selector, không coi mọi endpoint tương đương.

## 5. Cuối cùng: thay dataset cần thay gì?

1. Nguồn mới được phép sử dụng, mapping cột và profile mới đúng ngôn ngữ/phạm vi.
2. Ingest version mới và validate như mục 2. Importer baseline search documents với attributes rỗng; direct cần package có passage gắn attribute và profile exact lookup, không tạo fact bằng model.
3. Đợi hết request/job đang chạy, đổi `KNOWLEDGE_PACKAGE_PATH` trong `.env`.
4. Recreate backend, reload nginx nếu backend đổi IP:

```powershell
docker compose up -d --no-deps --force-recreate --wait backend
docker compose exec frontend nginx -t
docker compose exec frontend nginx -s reload
```

5. Reload UI, tạo hội thoại mới, chạy evaluation dataset mới. State/history khác version không vào context mới; history cũ giữ trong DB. Rollback bằng chọn package cũ, recreate và reload như trên.

Không cần sửa title/UI/planner/engine hay train lại model mỗi dataset cùng contract; cần thay expected task/status/evidence và đo recall/grounding. Parser mới, >64 entity, multilingual embeddings, reranker, action/checklist/policy mới là mở rộng capability cần code + kiểm thử, không chỉ đổi data. Không sửa lõi theo đáp án từng record để tăng điểm test.
