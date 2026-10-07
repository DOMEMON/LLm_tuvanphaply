"""Small isolated PDF worker; never executes document scripts or embedded files."""

import io
import json
import sys


def main():
    # The worker is killed by its caller on timeout. Limit memory on Linux (Docker).
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    from pypdf import PdfReader

    data = sys.stdin.buffer.read(8 * 1024**2 + 1)
    if len(data) > 8 * 1024**2 or not data.startswith(b"%PDF"):
        raise ValueError("PDF_SIZE_OR_TYPE")
    reader = PdfReader(io.BytesIO(data), strict=True)
    pages = [int(page) for page in sys.argv[1:]]
    if len(reader.pages) > 150 or len(pages) > 6 or reader.is_encrypted:
        raise ValueError("PDF_LIMIT")
    result = []
    for number in pages:
        page = reader.pages[number]
        if len(page.get_contents().get_data()) > 4 * 1024**2:
            raise ValueError("PDF_PAGE_LIMIT")
        text = page.extract_text()
        if len(text) > 50000:
            raise ValueError("PDF_TEXT_LIMIT")
        result.append(text)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
