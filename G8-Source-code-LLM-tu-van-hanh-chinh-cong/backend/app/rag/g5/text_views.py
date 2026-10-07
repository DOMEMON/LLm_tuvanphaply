"""Lossless Vietnamese surface text and an explicit, secondary search fold."""
import re
import unicodedata

from app.rag.retrieval import normalize


def surface(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold()))


def affirmative_tail(text: str) -> str | None:
    """Recognize a confirmation without folding a typed 'đừng' into 'đúng'.

    Return the remaining surface words for the caller's field-only check.
    Folding is a fallback for an unaccented prefix, never a replacement for
    contradictory accents. This helper is NOT consent to external tools.
    """
    words = surface(text).split()
    phrases = {
        "ừ", "đúng", "đúng rồi", "phải", "phải rồi", "vâng", "ok", "oke",
        "chính xác", "đúng rồi tôi muốn hỏi thủ tục đó", "tôi muốn hỏi thủ tục đó",
    }
    for phrase in sorted(phrases, key=lambda p: len(p.split()), reverse=True):
        count = len(phrase.split())
        prefix = " ".join(words[:count])
        if prefix == phrase or (prefix == normalize(prefix) and prefix == normalize(phrase)):
            return " ".join(words[count:])
    return None


def compatible_tokens(query: str, document: str) -> set[str]:
    """A typed accent is evidence, not optional: tất must not match tật.

    Unaccented input remains searchable. Compare token variants rather than
    discarding the original spelling before ranking.
    """
    targets: dict[str, set[str]] = {}
    for token in surface(document).split():
        targets.setdefault(normalize(token), set()).add(token)
    result = set()
    for token in surface(query).split():
        folded = normalize(token)
        if folded in targets and (token == folded or token in targets[folded]):
            result.add(folded)
    return result
