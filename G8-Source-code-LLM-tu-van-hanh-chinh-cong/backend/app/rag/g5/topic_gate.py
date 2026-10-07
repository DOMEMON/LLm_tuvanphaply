"""Current-turn procedure evidence, independent of the conversation's active topic.

An old procedure is a prior, not a hard filter. This gate deliberately separates
new-procedure requests from mentions of documents inside the old procedure.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from app.rag.catalog_policy import is_serving_candidate, preferred_procedure_id
from app.rag.g3.retrieval import procedure_names
from app.rag.g5.dictionary import RoutingDictionary, focus_query
from app.rag.g5.routing_evidence import (
    distinctive_candidates,
    evidence_followup,
    field_only_followup,
    lexical_winner,
    positive_text,
    rank_candidates,
    routing_text,
)
from app.rag.retrieval import normalize

NEW_PROCEDURE_REQUEST = re.compile(
    r"\b(?:muon|can|dinh|hoi|lam|xin)\s+(?:lam\s+)?(?:thu tuc|dang ky|xin cap|cap|"
    r"chuyen|doi|lam giay|xin giay)\b|\b(?:chuyen sang|doi sang|khong hoi .+ nua)\b"
)
CORRECTION_REQUEST = re.compile(
    r"\b(?:chua dung y|sua lai|dinh chinh|chuyen (?:han )?sang|doi sang|thay cho|"
    r"bi nham|thuc su can|khong (?:phai|tra|hoi|lam|muon).{0,80}|bo .{0,40} nhe)\b"
)
DOCUMENT_INVENTORY = re.compile(
    r"\b(?:da co|co san|da chuan bi|con thieu|can bo sung|giay to con lai)\b"
)
INVENTORY_FOLLOWUP = re.compile(r"\b(?:con thieu|can bo sung|con can|con gi nua|giay to con lai)\b")
GENERIC_TITLE_WORDS = frozenset({"thu", "tuc", "ho", "so", "co", "yeu", "to"})
HYBRID_SWITCH_MIN_SCORE = 0.70
HYBRID_SWITCH_MIN_LEXICAL = 0.35
# Dense similarity is corroborating evidence, not a veto when the lexical winner
# is exact and unambiguous. Real colloquial wording can sit just below 0.55.
HYBRID_SWITCH_MIN_DENSE = 0.50


@dataclass(frozen=True, slots=True)
class TopicDecision:
    action: Literal["KEEP", "SWITCH", "CLARIFY", "DEFER"]
    procedure_id: str | None = None
    reason: str = ""
    candidate_ids: tuple[str, ...] = ()
    clarification_hint: str = ""


def _phrase_in(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _surface_tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", unicodedata.normalize("NFC", text).lower()))


def _surface_phrase(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", text).lower()))


def _has_diacritic(token: str) -> bool:
    return token != normalize(token.replace("đ", "d"))


def _accent_conflict(query: str, title: str) -> bool:
    """Reject a different accented word that only matches after accent stripping.

    An unaccented user query remains eligible for retrieval. Two *different*
    accented words, such as "sổ" and "số", are not interchangeable evidence.
    """

    query_tokens = _surface_phrase(query).split()
    title_tokens = _surface_phrase(title).split()
    # Compare within a shared phrase, not unrelated words elsewhere in a long
    # sentence ("hồ sơ" must not conflict with "hỗ trợ").
    for i in range(len(query_tokens) - 1):
        pair = query_tokens[i : i + 2]
        for j in range(len(title_tokens) - 1):
            target = title_tokens[j : j + 2]
            if normalize(" ".join(pair)) != normalize(" ".join(target)):
                continue
            if not all(a == b or _has_diacritic(a) for a, b in zip(pair, target)):
                continue
            if any(
                a != b and _has_diacritic(a) and _has_diacritic(b) for a, b in zip(pair, target)
            ):
                return True
    return False


def _fold_tokens(text: str) -> list[str]:
    return normalize(text.replace("đ", "d").replace("Đ", "D")).split()


def _specific_variant(data, base_id: str, query: str) -> str | None:
    """Prefer a catalog variant when current-turn words name its qualifiers."""

    base = set(_fold_tokens(data.procedures[base_id]["title"])) - GENERIC_TITLE_WORDS
    query_sequence = _fold_tokens(query)
    query_tokens = set(query_sequence)
    ranked: list[tuple[int, str]] = []
    for procedure_id in data.accepted:
        if procedure_id == base_id or not is_serving_candidate(procedure_id):
            continue
        title_tokens = set(_fold_tokens(data.procedures[procedure_id]["title"]))
        if not base <= title_tokens:
            continue
        qualifiers = (title_tokens - base) - GENERIC_TITLE_WORDS
        matched = qualifiers & query_tokens
        negated = any(
            token in matched and "khong" in query_sequence[max(0, index - 5) : index]
            for index, token in enumerate(query_sequence)
        )
        if (
            len(matched) >= 2
            and not _accent_conflict(query, data.procedures[procedure_id]["title"])
            and not negated
        ):
            ranked.append((len(matched), procedure_id))
    if not ranked:
        return None
    best = max(score for score, _ in ranked)
    winners = {procedure_id for score, procedure_id in ranked if score == best}
    return next(iter(winners)) if len(winners) == 1 else None


def _explicit_match(data, query: str) -> str | None:
    text = normalize(query)
    surface_query = _surface_phrase(query)
    matches: list[tuple[int, int, str]] = []
    for procedure_id, procedure in data.procedures.items():
        surface_title = _surface_phrase(procedure["title"])
        if _phrase_in(surface_query, surface_title):
            matches.append((len(surface_title.split()), 2, preferred_procedure_id(procedure_id)))
        for alias in procedure.get("aliases", []):
            surface_alias = _surface_phrase(alias)
            if _phrase_in(surface_query, surface_alias):
                matches.append(
                    (len(surface_alias.split()), 2, preferred_procedure_id(procedure_id))
                )
        for name in procedure_names(data, procedure):
            variants = {name}
            if name.startswith("ho so ") and len(name.split()) >= 4:
                variants.add(name.removeprefix("ho so "))
            for variant in variants:
                if _phrase_in(text, variant) and not _accent_conflict(query, procedure["title"]):
                    matches.append(
                        (
                            len(variant.split()),
                            int(variant == name),
                            preferred_procedure_id(procedure_id),
                        )
                    )
    if not matches:
        return None
    best = max((length, original) for length, original, _ in matches)
    winners = {
        procedure_id for length, original, procedure_id in matches if (length, original) == best
    }
    if len(winners) != 1:
        return None
    base_id = next(iter(winners))
    return _specific_variant(data, base_id, query) or base_id


def _positive_correction_clause(query: str) -> str:
    """Keep the positive target of a correction, not the rejected old topic."""

    text = normalize(query.replace("đ", "d").replace("Đ", "D"))
    markers = (
        r"\bthuc su can(?: hoi| lam)?(?: la)?\s+",
        r"\bchuyen (?:han |chu de )sang\s+",
        r"\bdoi sang\s+",
    )
    for pattern in markers:
        matches = list(re.finditer(pattern, text))
        if matches:
            return query[matches[-1].end() :]

    contrast = re.search(r"\bkhong phai\b", text)
    if contrast:
        left = query[: contrast.start()].strip(" ,;:-")
        right = query[contrast.end() :]
        replacement = re.search(r"\b(?:ma|chu|thay vao do)\b\s+", normalize(right))
        if replacement:
            return right[replacement.end() :]
        if len(set(normalize(left).split()) - GENERIC_TITLE_WORDS) >= 2:
            return left

    if re.search(r"\bthay cho\b", text):
        prefix = re.split(r"\bthay cho\b", text, maxsplit=1)[0]
        positive = re.split(r"(?:\btoi\b|\bminh\b|\bem\b)\s+can\s+(?:hoi|lam)\s+", prefix)
        if len(positive) > 1:
            return positive[-1]
    positive = positive_text(query)
    return positive if positive != routing_text(query) else query


def _specific_lexical_support(data, matched: frozenset[str]) -> bool:
    """Common administrative tokens cannot prove a semantic model's guess.

    Use catalog document frequency rather than a list tuned to test questions.
    Exact names and qualified dictionary rules have already been handled.
    """
    titles = [
        set(routing_text(p["title"]).split())
        for pid, p in data.procedures.items()
        if is_serving_candidate(pid) and pid in data.accepted
    ]
    frequencies = Counter(token for words in titles for token in words)
    threshold = max(2, len(titles) * 0.12)
    return any(frequencies[token] < threshold for token in matched)


async def decide_topic(
    data,
    query: str,
    active_id: str | None,
    hybrid_router=None,
    *,
    dictionary: RoutingDictionary | None = None,
) -> TopicDecision:
    """Select only strong current-turn evidence; otherwise preserve model judgment.

    Dense retrieval can suggest a switch but cannot override an exact title, a
    document-inventory follow-up, or a low-margin/low-lexical candidate.
    """

    active_id = preferred_procedure_id(active_id)
    text = normalize(query.replace("đ", "d").replace("Đ", "D"))
    # "Trong nước" describes the place of registration, not both parties'
    # nationality. Do not let a previous foreign-marriage topic answer this.
    if (
        re.search(r"\b(?:ket hon|hon thu|cuoi vo|cuoi chong|lay vo|lay chong)\b", text)
        and re.search(r"\btrong nuoc\b", text)
        and not re.search(r"\b(?:nuoc ngoai|ngoai quoc|khac quoc tich)\b", text)
        and not re.search(
            r"\b(?:ca hai|hai ben|chung toi deu|hai nguoi deu|deu la)\b.{0,35}"
            r"\b(?:nguoi viet|cong dan viet|quoc tich viet)\b",
            text,
        )
    ):
        return TopicDecision(
            "CLARIFY",
            reason="domestic_marriage_nationality_unknown",
            candidate_ids=("d2_title_6d6862db57ba", "d2_title_1620179477b4"),
            clarification_hint=(
                "Hai người đăng ký kết hôn đều là công dân Việt Nam hay có bên là người nước ngoài?"
            ),
        )
    correction = bool(CORRECTION_REQUEST.search(text))
    focused_query, focused = focus_query(query) if dictionary is not None else (query, False)
    explicit_query = (
        _positive_correction_clause(focused_query)
        if correction and dictionary is None
        else focused_query
    )
    explicit = _explicit_match(data, explicit_query)
    new_request = bool(NEW_PROCEDURE_REQUEST.search(text) or correction)
    inventory = (
        bool(DOCUMENT_INVENTORY.search(text))
        and bool(INVENTORY_FOLLOWUP.search(text))
        and not new_request
    )

    if inventory and active_id is not None:
        return TopicDecision("KEEP", reason="document_inventory")

    if active_id is not None and field_only_followup(query):
        return TopicDecision("KEEP", reason="field_only_followup")
    if focused and not focused_query.strip():
        return TopicDecision(
            "CLARIFY",
            reason="dictionary_rejected_request",
            clarification_hint="Bạn muốn hỏi thủ tục nào thay cho nhu cầu vừa loại trừ?",
        )

    ranked = rank_candidates(data, explicit_query)
    # One folded syllable (e.g. việc, tất/tật) cannot justify showing a title.
    candidate_ids = tuple(item.procedure_id for item in ranked if len(item.matched) >= 2)[:4]
    winner = lexical_winner(ranked)
    # Two independently spelled out names are different from incidental nouns
    # in a life situation. Ignore nested base/variant names here.
    named = [
        (pid, name)
        for pid, procedure in data.procedures.items()
        for name in procedure_names(data, procedure)
        if _phrase_in(normalize(explicit_query), name)
    ]
    independent = {
        preferred_procedure_id(pid)
        for pid, name in named
        if not any(name != other and _phrase_in(other, name) for _, other in named)
    }
    if len(independent) > 1:
        return TopicDecision("CLARIFY", reason="multiple_procedures", candidate_ids=candidate_ids)
    anchors = {
        pid
        for pid in distinctive_candidates(data, explicit_query)
        if not _accent_conflict(query, data.procedures[pid]["title"])
    }
    if (
        active_id
        and (explicit is None or explicit == active_id)
        and evidence_followup(data, query, active_id)
    ):
        return TopicDecision("KEEP", reason="evidence_followup")
    if dictionary is not None:
        match = dictionary.match(explicit_query, active_id)
        if match.action == "CLARIFY":
            return TopicDecision(
                "CLARIFY",
                reason=match.reason,
                candidate_ids=match.ids,
                clarification_hint=match.clarification_hint,
            )
        if match.action == "RESOLVE":
            resolved = match.ids[0]
            if (
                explicit is not None
                and explicit != resolved
                and _specific_variant(data, explicit, routing_text(explicit_query)) != resolved
            ):
                return TopicDecision(
                    "CLARIFY",
                    reason="dictionary_catalog_conflict",
                    candidate_ids=tuple(dict.fromkeys((explicit, resolved))),
                )
            if resolved == active_id:
                return TopicDecision("KEEP", reason="dictionary_evidence")
            return TopicDecision("SWITCH", resolved, "dictionary_evidence", match.ids)
        # The affirmative part may name a procedure with a short catalog phrase
        # rather than a dictionary alias. It still goes through lexical checks.
    if active_id and re.fullmatch(
        r"(?:y (?:toi|minh|em) (?:la )?)?(?:dang ky|lam) lan dau",
        normalize(explicit_query),
    ):
        return TopicDecision("KEEP", reason="contextual_followup")
    # Qualifiers can be a whole turn: "Còn với người nước ngoài?". Restrict
    # this inheritance to variants in the same catalog family.
    if active_id and explicit is None:
        variant = _specific_variant(data, active_id, positive_text(query))
        if variant:
            explicit = variant

    # Exact names can mention two independent procedures. Do not silently pick
    # the longest one and answer only half the question.
    if len(ranked) > 1 and winner is None and len(anchors) != 1:
        first, second = ranked[:2]
        if len(first.matched - second.matched) >= 2 and len(second.matched - first.matched) >= 2:
            return TopicDecision(
                "CLARIFY", reason="multiple_procedures", candidate_ids=candidate_ids
            )
        if (first.matched == second.matched and len(first.matched) >= 2
                and first.coverage >= 0.85 and explicit is None):
            return TopicDecision(
                "CLARIFY", reason="missing_procedure_qualifier", candidate_ids=candidate_ids
            )

    if explicit is not None and explicit == active_id:
        return TopicDecision("KEEP", reason="current_procedure_explicit")
    if explicit is not None:
        if explicit not in data.accepted:
            return TopicDecision("CLARIFY", reason="procedure_not_approved")
        if active_id is None or new_request or not inventory:
            return TopicDecision("SWITCH", explicit, "explicit_catalog_name")
        return TopicDecision("KEEP", reason="document_inventory")
    if len(anchors) == 1:
        anchored = next(iter(anchors))
        if dictionary is not None and not any(
            item.procedure_id == anchored and _specific_lexical_support(data, item.matched)
            for item in ranked
        ):
            return TopicDecision(
                "CLARIFY", reason="dictionary_weak_anchor", candidate_ids=candidate_ids
            )
        # A generic base cannot erase an unresolved population/variant question.
        if anchored == active_id:
            return TopicDecision("KEEP", reason="current_turn_catalog_match")
        return TopicDecision("SWITCH", anchored, "catalog_lexical_evidence", candidate_ids)
    if winner and not _accent_conflict(query, data.procedures[winner]["title"]):
        if dictionary is not None and not _specific_lexical_support(data, ranked[0].matched):
            return TopicDecision(
                "CLARIFY", reason="dictionary_weak_anchor", candidate_ids=candidate_ids
            )
        if winner not in data.accepted:
            return TopicDecision("CLARIFY", reason="procedure_not_approved")
        if winner == active_id:
            return TopicDecision("KEEP", reason="current_turn_catalog_match")
        return TopicDecision("SWITCH", winner, "catalog_lexical_evidence", candidate_ids)

    if hybrid_router is not None:
        candidate = await hybrid_router.route(
            data, explicit_query if dictionary is not None else query
        )
        candidate_id = (
            preferred_procedure_id(candidate.procedure_id) if candidate is not None else None
        )
        lexical_support = any(
            item.procedure_id == candidate_id
            and len(item.matched) >= 2
            and (dictionary is None or _specific_lexical_support(data, item.matched))
            for item in ranked
        )
        if candidate is not None and candidate_id == active_id and lexical_support:
            # Only an equally verified current-topic candidate can justify KEEP.
            # A low-confidence dense hit must not lock the old context.
            if candidate.score >= HYBRID_SWITCH_MIN_SCORE and candidate.lexical_score >= 0.35:
                return TopicDecision("KEEP", reason="semantic_current_procedure")
        if (
            candidate is not None
            and lexical_support
            and candidate_id in data.accepted
            and not _accent_conflict(query, data.procedures[candidate_id]["title"])
            and candidate.score >= HYBRID_SWITCH_MIN_SCORE
            and candidate.lexical_score >= HYBRID_SWITCH_MIN_LEXICAL
            and candidate.dense_score >= HYBRID_SWITCH_MIN_DENSE
        ):
            return TopicDecision("SWITCH", candidate_id, "hybrid_high_confidence")

    if (
        dictionary is not None
        and ranked
        and not any(_specific_lexical_support(data, item.matched) for item in ranked[:2])
    ):
        return TopicDecision(
            "CLARIFY", reason="dictionary_weak_anchor", candidate_ids=candidate_ids
        )

    if not new_request or (
        ranked and not _accent_conflict(query, data.procedures[ranked[0].procedure_id]["title"])
    ):
        return TopicDecision(
            "DEFER", reason="needs_model_interpretation", candidate_ids=candidate_ids
        )

    # A first turn or a new/corrected procedure request that the approved catalog
    # cannot confidently map must not inherit or fabricate procedure evidence.
    return TopicDecision("CLARIFY", reason="new_procedure_unresolved", candidate_ids=candidate_ids)
