"""Free official-document connector with reviewed discovery locations, not answers.

Every answer is extracted from a freshly fetched (or short-lived cached) document.
Unknown procedure families/localities and withdrawn documents fail closed.
No arbitrary user URL, web search snippet, model-generated fact, or corpus write.
"""

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener

from app.current_procedure import NoRedirect
from app.realtime_contract import official_url

BASE = "https://congbao.hochiminhcity.gov.vn/cong-bao/van-ban/quyet-dinh/so/44-2026-qd-ubnd/ngay/30-06-2026/"
ATTRIBUTE_URL = BASE + "49534?cbid=49648"
PDF_URL = BASE + "tai-ve/49534?cbid=49648"
LOAN_ATTRIBUTE_URL = "https://vbpl.vn/tw/Pages/ivbpq-thuoctinh.aspx?ItemID=185562"
LOAN_PDF_URL = "https://congbaocdn.chinhphu.vn/180507251028987904/2026/1/15/338signed-1768441315190313926761.pdf"
MAX_BYTES = 8 * 1024**2


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.ignore = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignore += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignore = max(0, self.ignore - 1)

    def handle_data(self, data):
        if not self.ignore:
            self.parts.append(data)


def download(url, timeout=4):
    opener = build_opener(NoRedirect())
    for _ in range(3):
        official_url(url)
        # Source-specific connector: even allowed portal URLs are not document URLs.
        if not url.startswith(BASE) and url not in {LOAN_ATTRIBUTE_URL, LOAN_PDF_URL}:
            raise ValueError("UNREGISTERED_DOCUMENT_URL")
        try:
            response = opener.open(
                Request(url, headers={"User-Agent": "HCC-SourceLookup/3.0"}), timeout=timeout
            )
        except HTTPError as exc:
            if exc.code in {301, 302, 307, 308}:
                target = exc.headers.get("Location")
                exc.close()
                if not target:
                    raise ValueError("NO_REDIRECT_TARGET")
                url = official_url(urljoin(url, target))
                continue
            raise
        with response:
            official_url(response.geturl())
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("DOCUMENT_TOO_LARGE")
            return response.headers.get_content_type(), data
    raise ValueError("DOCUMENT_REDIRECT_LIMIT")


def extract_pages(data, pages):
    result = subprocess.run(
        [sys.executable, "-m", "app.pdf_reader", *map(str, pages)],
        input=data,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout.decode("utf-8"))


def section(text, start, end):
    compact = " ".join(text.split())
    match = re.search(start + r"(.*?)" + end, compact)
    if not match:
        raise ValueError("DOCUMENT_SECTION_CHANGED")
    return match.group(1).strip()


def read_land_rules(fetch=download, parse=extract_pages):
    content_type, html = fetch(ATTRIBUTE_URL)
    if content_type != "text/html":
        raise ValueError("ATTRIBUTE_CONTENT_TYPE")
    parser = TextOnly()
    parser.feed(html.decode("utf-8", errors="strict"))
    text = " ".join(" ".join(parser.parts).split())
    if not re.search(r"Tình trạng hiệu lực:\s*Đang hiệu lực", text):
        raise ValueError("DOCUMENT_NOT_CURRENT")
    if "44/2026/QĐ-UBND" not in text or "01/07/2026" not in text:
        raise ValueError("DOCUMENT_IDENTITY_CHANGED")
    content_type, pdf = fetch(PDF_URL)
    if content_type != "application/pdf" or not pdf.startswith(b"%PDF"):
        raise ValueError("PDF_CONTENT_TYPE")
    # Zero-based PDF pages, validated against the official publication.
    fee_page, place_page, scope_page = parse(pdf, [3, 44, 45])
    fee = section(fee_page, r"3\.\s*[Vv]ề phí, lệ phí:\s*", r"Điều 4\.")
    place = section(
        place_page,
        r"1\.\s*Cơ quan tiếp nhận hồ sơ và trả kết quả:\s*",
        r"2\.\s*Hình thức nộp hồ sơ:",
    )
    scope = section(
        scope_page,
        r"3\.\s*Đối với trường hợp đăng ký đất đai",
        r"4\.\s*Đối với trường hợp đăng ký biến động",
    )
    scope = "Đối với trường hợp đăng ký đất đai" + scope
    return {"fees": fee, "receiving_authority": place + "\n\n" + scope}


def read_loan_rules(fetch=download, parse=extract_pages):
    content_type, html = fetch(LOAN_ATTRIBUTE_URL)
    if content_type != "text/html":
        raise ValueError("ATTRIBUTE_CONTENT_TYPE")
    parser = TextOnly()
    parser.feed(html.decode("utf-8", errors="strict"))
    text = " ".join(" ".join(parser.parts).split())
    if not re.search(r"Hiệu lực:\s*Còn hiệu lực|Tình trạng hiệu lực:\s*Còn hiệu lực", text):
        raise ValueError("DOCUMENT_NOT_CURRENT")
    if "338/2025/NĐ-CP" not in text or "01/01/2026" not in text:
        raise ValueError("DOCUMENT_IDENTITY_CHANGED")
    content_type, pdf = fetch(LOAN_PDF_URL)
    if content_type != "application/pdf" or not pdf.startswith(b"%PDF"):
        raise ValueError("PDF_CONTENT_TYPE")
    page = parse(pdf, [4])[0]
    import unicodedata

    page = unicodedata.normalize("NFC", page)
    value = section(
        page,
        r"Điều 11\.\s*Thủ tục giải quyết vay vốn, xử lý nợ bị rủi ro\s*",
        r"2\.\s*Trong thời hạn",
    )
    return {"receiving_authority": value}
