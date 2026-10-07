"""Versioned, read-only procedure vocabulary. Never an administrative fact source.

No global Dataset mutation, fuzzy auto-correction, network, or per-user cache.
Each rule is AND-of-OR phrase groups, with word boundaries and positive evidence.
Ambiguities and scope boundaries are policy decisions, not low-confidence scores.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.catalog_policy import PREFERRED_PROCEDURES
from app.rag.retrieval import normalize

DEFAULT_PATH = Path(__file__).with_name("dictionaries") / "vi_admin_v1.json"
# Package pin: update deliberately with the reviewed vocabulary and its tests.
DEFAULT_SHA256 = "64c9f7f75bcdb67a51b1252436c7281c34f7ef3bdfc0fa0f5591fc44b7e4607e"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Entry(FrozenModel):
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    rules: tuple[tuple[str, ...], ...] = ()
    context_rules: tuple[tuple[str, ...], ...] = ()
    clarification_rules: tuple[tuple[str, ...], ...] = ()
    context_from: tuple[str, ...] = ()
    exclude_groups: tuple[str, ...] = ()
    shadow_of: str | None = None
    note: str


class Guard(FrozenModel):
    key: str
    all: tuple[str, ...] = Field(min_length=1)
    ids: tuple[str, ...] = Field(max_length=4)
    context_from: tuple[str, ...] = ()
    always: bool = False
    fallback_only: bool = False
    note: str


class Vocabulary(FrozenModel):
    version: str
    schema_version: Literal[1]
    corpus_version: str
    corpus_sha256: str
    provenance: str
    source_workbook_sha256: str
    groups: dict[str, str]
    entries: tuple[Entry, ...]
    ambiguities: tuple[Guard, ...]
    boundaries: tuple[Guard, ...]

    @model_validator(mode="after")
    def validate_rules(self):
        ids = {e.id for e in self.entries}
        canonical = {e.id for e in self.entries if e.shadow_of is None}
        if len(ids) != len(self.entries):
            raise ValueError("DICTIONARY_DUPLICATE_ID")
        aliases: dict[str, str] = {}
        guards = [*self.ambiguities, *self.boundaries]
        if len({g.key for g in guards}) != len(guards):
            raise ValueError("DICTIONARY_DUPLICATE_GUARD")
        for e in self.entries:
            if e.shadow_of:
                if e.shadow_of not in canonical or e.aliases or e.rules or e.context_rules:
                    raise ValueError("DICTIONARY_INVALID_SHADOW")
            elif not e.aliases:
                raise ValueError("DICTIONARY_MISSING_VOCABULARY")
            if not set(e.context_from) <= canonical or (e.context_rules and not e.context_from):
                raise ValueError("DICTIONARY_INVALID_CONTEXT")
            for alias in e.aliases:
                key = normalize(alias)
                if len(key.split()) < 2 or (key in aliases and aliases[key] != e.id):
                    raise ValueError("DICTIONARY_UNSAFE_ALIAS_COLLISION")
                aliases[key] = e.id
            for rule in (*e.rules, *e.context_rules, *e.clarification_rules):
                if not rule:
                    raise ValueError("DICTIONARY_EMPTY_RULE")
                for group in rule:
                    self.phrases(group)
            for group in e.exclude_groups:
                self.phrases(group)
        for guard in guards:
            if not set((*guard.ids, *guard.context_from)) <= canonical:
                raise ValueError("DICTIONARY_UNKNOWN_GUARD_ID")
            for group in guard.all:
                self.phrases(group)
        return self

    def phrases(self, group: str) -> tuple[str, ...]:
        value = self.groups.get(group[1:]) if group.startswith("@") else group
        if not value or any(not normalize(p) for p in value.split("|")):
            raise ValueError("DICTIONARY_INVALID_PHRASE_GROUP")
        return tuple(dict.fromkeys(p.strip() for p in value.split("|")))


@dataclass(frozen=True)
class DictionaryMatch:
    action: Literal["RESOLVE", "CLARIFY", "NONE"]
    ids: tuple[str, ...] = ()
    reason: str = ""
    rule_keys: tuple[str, ...] = ()
    clarification_hint: str = ""


@dataclass(frozen=True)
class Phrase:
    surface: tuple[str, ...]
    folded: tuple[str, ...]


def _surface(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold()))


def focus_query(text: str) -> tuple[str, bool]:
    """Retain affirmative requests, not explicitly rejected clauses/background.

    This is a conservative routing view. It NEVER replaces the user message in
    SQL, patch evidence, field detection, or answer grounding.
    """
    kept = []
    changed = False
    rejected_continuation = False
    for raw in re.split(r"([.!?;\n]+)|,", text):
        if raw is None:
            continue
        if re.fullmatch(r"[.!?;\n]+", raw):
            rejected_continuation = False
            continue
        surface = list(_surface(raw))
        folded = [normalize(t) for t in surface]
        clause = " ".join(folded)
        if rejected_continuation:
            # Commas inside an official title do not end its rejection. A new
            # affirmative request or sentence does, not a trailing noun phrase.
            if re.match(
                r"(?:(?:toi|minh|em|nha|gia dinh) )?(?:muon|can|xin|hoi|vua|moi|da|dang)\b|"
                r"(?:bay gio|thay vao do)\b",
                clause,
            ):
                rejected_continuation = False
            else:
                changed = True
                continue
        negative_matches = re.finditer(
            r"\b(?:(?:khong|ko|dung) (?:phai|xin|lam|muon|can|hoi|tra|"
            r"dang ky|dang ki|chuyen truong)|chua hoi)\b",
            clause,
        )
        negative = None
        for candidate in negative_matches:
            position = len(clause[:candidate.start()].split())
            if folded[position] == "dung" and (
                surface[position] not in {"dung", "đừng"}
                or (position > 0 and folded[position - 1] == "noi")
            ):
                # "nội dung đăng ký", "dùng", "đúng" are not "đừng đăng ký".
                # Preserve unaccented negation except the noun "noi dung".
                continue
            negative = candidate
            break
        trailing = re.search(r"\b(?:khong|ko) (?:can|lam|hoi)(?: nua)?$", clause)
        if negative:
            changed = True
            rejected_continuation = True
            index = len(clause[: negative.start()].split())
            after = " ".join(folded[index:])
            contrast = next(
                (
                    m
                    for m in re.finditer(r"\b(?:ma|nhung|thay vao do|nua)\b", after)
                    if surface[index + len(after[: m.start()].split())] not in {"mã", "má", "mạ"}
                ),
                None,
            )
            if contrast and contrast.end() < len(after):
                start = index + len(after[: contrast.end()].split())
                surface = surface[start:]
                rejected_continuation = False
            elif index > 0 and folded[index - 1] in {"chu", "nhung"}:
                surface = surface[: index - 1]
            elif trailing:
                surface = []
            else:
                surface = []
        else:
            # Asking for paper A for another application's file is not asking to
            # perform B. Only trim this specific document-purpose construction.
            purpose = re.search(r"\bde (?:lam|nop|bo sung|hoan thien) ho so\b", clause)
            if purpose and "giay" in folded[: len(clause[: purpose.start()].split())]:
                changed = True
                surface = surface[: len(clause[: purpose.start()].split())]
        if surface:
            kept.append(" ".join(surface))
    return (" ; ".join(kept), changed) if changed else (text, False)


@lru_cache(maxsize=4096)
def _phrase(text: str) -> Phrase:
    surface = _surface(text)
    return Phrase(surface, tuple(normalize(t) for t in surface))


def _positions(query: Phrase, phrase: Phrase) -> frozenset[int]:
    result: set[int] = set()
    size = len(phrase.folded)
    for start in range(len(query.folded) - size + 1):
        if query.folded[start : start + size] != phrase.folded:
            continue
        # Accent stripping permits unaccented input, not a different accented word
        # (sổ nhà vs số nhà). Orthographic hoả/hỏa is explicitly listed in groups.
        if any(
            q != p and q != normalize(q) and p != normalize(p)
            for q, p in zip(query.surface[start : start + size], phrase.surface, strict=True)
        ):
            continue
        before = " ".join(query.folded[max(0, start - 5) : start])
        if re.search(
            r"\b(?:khong|ko|dung)(?: phai| co| xin| lam| muon| can| hoi| la| tra){0,4}$", before
        ):
            continue
        result.update(range(start, start + size))
    return frozenset(result)


@dataclass(frozen=True)
class RoutingDictionary:
    vocabulary: Vocabulary
    sha256: str

    def select_clarification(self, text: str, candidate_ids) -> str | None:
        """Use only qualifiers for the options actually offered in this chat.

        These rules never apply to fresh chats and cannot override a new explicit
        procedure or a scope boundary (the caller enforces that precedence).
        """
        focused, _ = focus_query(text)
        query = _phrase(focused)
        winners = []
        for entry in self.vocabulary.entries:
            if entry.id not in candidate_ids:
                continue
            if any(
                any(_positions(query, _phrase(p)) for p in self.vocabulary.phrases(g))
                for g in entry.exclude_groups
            ):
                continue
            if any(
                all(
                    any(_positions(query, _phrase(p)) for p in self.vocabulary.phrases(g))
                    for g in rule
                )
                for rule in entry.clarification_rules
            ):
                winners.append(entry.id)
        return winners[0] if len(winners) == 1 else None

    def bind(self, data) -> RoutingDictionary:
        """Check the complete snapshot, not only its friendly version label."""
        v = self.vocabulary
        if data.version != v.corpus_version or data.fingerprint != v.corpus_sha256:
            raise ValueError("DICTIONARY_CORPUS_MISMATCH")
        if {e.id for e in v.entries} != set(data.procedures):
            raise ValueError("DICTIONARY_CATALOG_COVERAGE_MISMATCH")
        for e in v.entries:
            if e.name != data.procedures[e.id]["title"]:
                raise ValueError("DICTIONARY_TITLE_MISMATCH")
            if e.shadow_of != PREFERRED_PROCEDURES.get(e.id):
                raise ValueError("DICTIONARY_SHADOW_MISMATCH")
            if not e.shadow_of and e.id not in data.accepted:
                raise ValueError("DICTIONARY_UNAPPROVED_PROCEDURE")
        return self

    def match(self, text: str, active_id: str | None = None) -> DictionaryMatch:
        v = self.vocabulary
        text, changed = focus_query(text)
        if changed and not text.strip():
            return DictionaryMatch(
                "CLARIFY",
                reason="dictionary_rejected_request",
                clarification_hint="Bạn muốn hỏi thủ tục nào thay cho nhu cầu vừa loại trừ?",
            )
        surface = _surface(text[:4000])
        query = Phrase(surface, tuple(normalize(t) for t in surface))
        # Per-call matches; the shared LRU contains only artifact phrases.
        cache: dict[str, frozenset[int]] = {}

        def group_positions(group: str) -> frozenset[int]:
            if group not in cache:
                positions: set[int] = set()
                for phrase in v.phrases(group):
                    found = _positions(query, _phrase(phrase))
                    # "Mất bao lâu" asks about elapsed time, not a lost
                    # registration certificate. Do not let this one token
                    # activate a replacement-document rule.
                    if normalize(phrase) == "mat":
                        found = frozenset(
                            i for i in found if query.folded[i + 1 : i + 3] != ("bao", "lau")
                        )
                    if group in {"@death", "@bereavement"}:
                        # A death event is not losing money/papers or a negated event.
                        event = _phrase(phrase).folded
                        size = len(event)
                        valid: set[int] = set()
                        for start in sorted(found):
                            if query.folded[start:start + size] != event:
                                continue
                            before = " ".join(query.folded[max(0, start - 3):start])
                            if re.search(r"\b(?:chua|khong|chang|sap)(?: he| tung| bi)?$", before):
                                continue
                            if event[-1] == "mat" and query.folded[start + size:start + size + 1] in {
                                ("tien",), ("giay",), ("vi",), ("do",), ("viec",),
                                ("dien",), ("tich",), ("trom",), ("cap",),
                            }:
                                continue
                            valid.update(range(start, start + size))
                        found = frozenset(valid)
                    positions.update(found)
                cache[group] = frozenset(positions)
            return cache[group]

        def rule_positions(groups: tuple[str, ...]) -> frozenset[int]:
            matches = [group_positions(g) for g in groups]
            if not matches or not all(matches):
                return frozenset()
            positions = frozenset().union(*matches)
            # Do not join distant incidental mentions in a long prompt.
            return positions if max(positions) - min(positions) <= 55 else frozenset()

        def guard_matches(g: Guard) -> bool:
            return (not g.context_from or active_id in g.context_from) and bool(
                rule_positions(g.all)
            )

        boundaries = [g for g in v.boundaries if guard_matches(g)]
        if any(g.key == "copy_not_signature" for g in boundaries) and group_positions(
            "chứng thực chữ ký|chứng chữ ký"
        ):
            return DictionaryMatch(
                "CLARIFY", ("d2_title_1e0d3df69ad5",), "dictionary_scope_boundary",
                clarification_hint=(
                    "Nguồn hiện có hỗ trợ chứng thực chữ ký; chưa có thủ tục chứng thực "
                    "bản sao (sao y) riêng để hướng dẫn phần đó. Đây là hai nhu cầu khác nhau. "
                    "Bạn muốn xem phần chứng thực chữ ký trước không?"
                ),
            )
        if group_positions("giấy báo tử") and group_positions(
            "xin giấy đó|xin giấy này|xin giấy báo tử|làm giấy báo tử"
        ) and not group_positions("đăng ký khai tử|đăng kí khai tử"):
            return DictionaryMatch(
                "CLARIFY", reason="dictionary_scope_boundary",
                clarification_hint=(
                    "Mình hiểu bạn đang hỏi xin giấy báo tử. Nguồn hiện có chỉ có thủ tục "
                    "đăng ký khai tử, chưa có hướng dẫn riêng về việc cấp giấy báo tử. "
                    "Mình chưa thể xác nhận hồ sơ và nơi cấp giấy đó từ nguồn này."
                ),
            )
        if any(g.key == "scholarship_outside" for g in boundaries):
            if group_positions("chưa rõ|chưa biết|chưa nói"):
                return DictionaryMatch(
                    "CLARIFY",
                    ("d2_title_ff5c71001a91",),
                    "dictionary_ambiguity",
                    clarification_hint="Bạn hỏi học bổng chính sách, du học hay doanh nghiệp? "
                    "Nguồn hiện có chỉ hỗ trợ thủ tục học bổng chính sách.",
                )
            if group_positions("@meritorious") and group_positions("sinh viên|đại học|học tập"):
                return DictionaryMatch(
                    "CLARIFY",
                    ("d2_title_56784de1011e",),
                    "dictionary_scope_boundary",
                    clarification_hint="Nguồn có thủ tục hỗ trợ học tập cho nhóm liên quan "
                    "người có công, không có thủ tục học bổng doanh nghiệp. "
                    "Bạn muốn xem phần hỗ trợ học tập không?",
                )
        # Naming company and household as undecided alternatives is not yet a
        # request for company registration. Keep the supported option available.
        if (
            group_positions("chưa biết|chưa quyết|chưa chọn")
            and group_positions("hộ kinh doanh")
            and group_positions("công ty")
        ):
            return DictionaryMatch(
                "CLARIFY",
                ("d2_title_e5945d1751b7",),
                "dictionary_ambiguity",
                clarification_hint=(
                    "Bạn chọn hộ kinh doanh hay công ty? Nguồn hiện có chỉ hỗ trợ hộ kinh doanh."
                ),
            )
        if boundaries:
            return DictionaryMatch(
                "CLARIFY",
                tuple(dict.fromkeys(pid for g in boundaries for pid in g.ids))[:4],
                "dictionary_scope_boundary",
                tuple(g.key for g in boundaries),
                " ".join(g.note for g in boundaries[:2]),
            )

        matches: dict[str, frozenset[int]] = {}
        for e in v.entries:
            if e.shadow_of or any(group_positions(g) for g in e.exclude_groups):
                continue
            supports = [group_positions(alias) for alias in e.aliases]
            supports.extend(rule_positions(rule) for rule in e.rules)
            if active_id in e.context_from:
                supports.extend(rule_positions(rule) for rule in e.context_rules)
            support = frozenset().union(*supports)
            if support:
                matches[e.id] = support
        # Specific/base precedence must be declared by exclusions (e.g. foreign
        # marriage), never inferred from token-count dominance. Two matched
        # procedures may be contradictory requests even when one span is longer.
        winners = tuple(matches)
        ambiguous = [g for g in v.ambiguities if not g.fallback_only and guard_matches(g)]
        if not winners and not ambiguous:
            ambiguous = [g for g in v.ambiguities if g.fallback_only and guard_matches(g)]
        unresolved = [g for g in ambiguous if g.always or not set(winners) & set(g.ids)]
        if unresolved or len(winners) > 1:
            ids = tuple(dict.fromkeys([*winners, *(pid for g in unresolved for pid in g.ids)]))
            correction = re.search(r"(?:sửa lại|đính chính)\s*:\s*(.+)$", text, re.IGNORECASE)
            selected = self.select_clarification(correction.group(1), ids) if correction else None
            if selected:
                return DictionaryMatch("RESOLVE", (selected,), "dictionary_evidence")
            return DictionaryMatch(
                "CLARIFY",
                ids[:4],
                "dictionary_ambiguity",
                tuple(g.key for g in unresolved),
                " ".join(g.note for g in unresolved[:2]),
            )
        if len(winners) == 1:
            return DictionaryMatch("RESOLVE", winners, "dictionary_evidence")
        return DictionaryMatch("NONE")


@lru_cache(maxsize=4)
def load_dictionary(
    path: str = str(DEFAULT_PATH), sha256: str = DEFAULT_SHA256
) -> RoutingDictionary:
    content = Path(path).read_bytes()
    if len(content) > 1_000_000 or hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError("DICTIONARY_HASH_MISMATCH")
    return RoutingDictionary(Vocabulary.model_validate(json.loads(content)), sha256)


def configured_dictionary(settings, data) -> RoutingDictionary | None:
    if not settings.g6_dictionary_enabled:
        return None
    return load_dictionary(
        str(settings.g6_dictionary_path or DEFAULT_PATH),
        settings.g6_dictionary_sha256 or DEFAULT_SHA256,
    ).bind(data)
