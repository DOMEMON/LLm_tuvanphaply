"""Bound conversational subjects separately from transient catalog selection IDs."""
import re
from .catalog_query import fold


def bound_context(state):
    # The last two unique procedures, ordered oldest -> newest. No raw long
    # utterance is kept in a procedure task: it can mention discarded subjects.
    unique = {}
    for task in state.active:
        unique.pop(task.code, None)
        unique[task.code] = task.model_copy(deep=True)
    state.active = list(unique.values())[-2:]
    for task in state.active:
        task.quote = task.code
    retained = {t.code for t in state.active}
    state.focused = [c for c in state.focused if c in retained][-2:]
    state.pending = state.pending[-2:]
    # Catalog IDs are a UI selection map, not remembered procedure fields.
    # A long ordinary answer is NOT renumbered as a two-item menu: doing so
    # would make "item 2" select the original item 8. Ask for an explicit name
    # or "two most recent" when the discarded list can no longer be resolved.
    if not state.catalog_context and len(state.displayed_options) > 2:
        state.reference_limited = True
        state.displayed_options = []
    return state


def recent_pair(query, state):
    from app.rag.g7.contracts import Plan, Task
    from .turn_checks import requested_fields
    q = fold(query)
    if not re.match(r'^(?:cho toi |cho minh )?hai thu tuc gan nhat\b', q):
        return None
    if len(state.active) != 2:
        return Plan(relation='continue',tasks=[Task(kind='clarify',quote=query)])
    fields = requested_fields(query)
    if not fields:
        return None
    return Plan(relation='continue',tasks=[Task(kind='procedure',quote=query,code=t.code,
        fields=fields,scope=t.scope.model_copy(deep=True)) for t in state.active])


def forgotten_ordinal(query,state):
    from app.rag.g7.contracts import Plan,Task
    if not state.reference_limited:
        return None
    q=fold(query)
    if (re.fullmatch(r'\d{1,2}',q) or re.search(
            r'\b(?:muc|thu tuc|cai)\s+(?:so\s+\d+|thu\s+(?:\d+|hai|ba|tu|nam|sau|bay|tam)|dau tien)\b',q)):
        return Plan(relation='continue',tasks=[Task(kind='clarify',quote=query,question='G8_CONTEXT_LIMIT')])
    return None


def exceeds_limit(query, catalog):
    """Catch explicitly named lists >8 without knowing any procedure's facts.

    Arbitrary natural-language lists still use the planner overflow contract.
    Avoid contextual/negative mentions; only count clear request lists.
    """
    q = fold(query)
    if len(re.findall(r'[,;\n]',query)) < 8 or not re.search(r'ho so|le phi|noi nop|giay to|thoi gian',q):
        return False
    if re.search(r'khong hoi|dung hoi|da co|tru |ngoai tru',q):
        return False
    found=set()
    for card in catalog:
        title=re.sub(r'^(?:thu tuc )?(?:dang ky )?', '',fold(card['title'])).strip()
        # Leading labels such as HỒ SƠ are presentation, not procedure identity.
        title=re.sub(r'^ho so ', '', title)
        if title and ' '+title+' ' in ' '+q+' ':
            found.add(card['code'])
    # A numbered list explicitly declares independent needs even if a title is
    # abbreviated; schema cannot represent a ninth and must ask to split.
    numbered=len(re.findall(r'(?:^|\n)\s*\d+[.)]\s+',query))
    # A single field heading followed by a comma-separated noun list declares
    # one need per item, even with abbreviated catalog titles. Do not apply to
    # prose, document-detail lists or background/negative mentions.
    shared = re.fullmatch(r'(?:toi muon biet|cho toi biet|cho toi|toi can|minh can)\s+'
        r'(?:ho so|le phi|noi nop|thoi gian giai quyet)\s+cua\s+(.+)',
        ','.join(fold(chunk) for chunk in query.split(',')))
    items = [s.strip(' .!?') for s in shared[1].split(',')] if shared else []
    explicit_list = (len(items)>8 and all(1 <= len(s.split()) <= 14 for s in items)
        and not re.search(r'gom|bao gom|giay to|tai lieu|ban sao|ban chinh|vi du|\bda\b|\bnhung\b',shared[1]))
    return len(found)>8 or numbered>8 or explicit_list
