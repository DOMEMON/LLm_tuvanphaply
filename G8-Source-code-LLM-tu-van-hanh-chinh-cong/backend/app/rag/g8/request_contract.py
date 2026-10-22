"""G8 information-need boundaries, not procedure IDs or factual answers.

Only reject known unsupported information kinds and bare unbound references.
Natural-language service identification remains with the semantic planner.
"""
import re
import unicodedata

from app.rag.g7.contracts import Plan, Task

UNSUPPORTED = 'G8_UNSUPPORTED_INFORMATION'
UNBOUND = 'G8_SUBJECT_REQUIRED'


def fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn').replace('đ', 'd')


def without_titles(text, catalog):
    for card in sorted(catalog, key=lambda c: len(c['title']), reverse=True):
        text = re.sub(re.escape(card['title']), ' [subject] ', text, flags=re.I)
    return text


def unsupported_information(text):
    q = fold(text)
    # Supplying a phone/email as a document datum is not requesting an officer's
    # contact details. Negated requests and administrative e-filing remain valid.
    if re.search(r'ho so.{0,45}(?:ghi|dien|can|gom|bao gom).{0,35}(?:so dien thoai|email)|'
                 r'(?:dien|ghi).{0,30}(?:so dien thoai|email).{0,30}(?:to khai|ho so)', q):
        return False
    pattern = (r'\bso dien thoai\b|\bsdt\b|\bhotline\b|\bemail\b|\bdia chi thu dien tu\b|'
               r'\b(?:ho ten|ten) (?:can bo|nguoi thu ly|chuyen vien)\b|'
               r'\b(?:lich lam viec|gio mo cua|gio dong cua)\b|'
               r'\bso tai khoan (?:ngan hang|ca nhan)\b|\bma theo doi ho so\b')
    for match in re.finditer(pattern, q):
        prefix = q[max(0, match.start() - 40):match.start()]
        if not re.search(r'(?:khong can|khong hoi|dung (?:cho|noi)|chua hoi|bo qua)\s*(?:ve\s+)?$', prefix):
            return True
    return False


def unsupported_clauses(query, catalog):
    clauses = re.split(r';|\n|\bvà\b|\bđồng thời\b', query, flags=re.I)
    return [clause.strip(' .!?') for clause in clauses if clause.strip(' .!?')
        and unsupported_information(without_titles(clause, catalog))
        and not information_fields(clause, catalog)]


def information_fields(text, catalog):
    from app.rag.g8.turn_checks import requested_fields
    q = fold(without_titles(text, catalog))
    # An officer receiving/processing a file is background context for a
    # contact question, not a request for the contents of that file.
    q = re.sub(r'(?:truc tiep\s+)?(?:xu ly|tiep nhan|nhan|thu ly)\s+ho so',
               ' officer_activity ', q)
    return requested_fields(q)


def bare_unbound_reference(query, state):
    if state.active or state.focused or state.displayed_options or any(t.candidates for t in state.pending):
        return False
    from app.rag.g8.turn_checks import FIELD_PATTERNS, requested_fields
    q = re.sub(r'[^\w\s]', ' ', fold(query))
    if re.search(r'\b(?:nhung|cac|liet ke|danh muc|thu tuc nao)\b', q):
        return False  # A catalog question intentionally has no single subject.
    if not requested_fields(q) and not re.search(r'\bthu\s+(?:hai|ba|tu|nam|\d+)\b', q):
        return False
    # Share the field lexicon with check_turn. Otherwise a known field phrase
    # such as "thời hạn" can leak through simply because its component words
    # were missing from a separate neutral-word list.
    for pattern in FIELD_PATTERNS.values():
        q = re.sub(pattern, ' ', q)
    if not q.strip():
        return True
    # Closed neutral vocabulary: any purpose-specific word (birth, marriage,
    # bereavement, etc.) leaves the request with the model. No name matching.
    filler = (r'toi|minh|em|ban|anh|chi|cho|xin|biet|hoi|muon|can|xem|ve|cua|mang|co|khong|'
              r'le|phi|ho|so|giay|to|thanh|phan|nop|o|dau|noi|tiep|nhan|'
              r'thoi|gian|giai|quyet|xu|ly|bao|lau|may|ngay|hinh|phuong|thuc|cach|kenh|'
              r'thu|tuc|do|nay|vua|hoi|nhu|tren|cai|muc|thu|hai|ba|tu|nam|'
              r'dau|tien|cuoi|la|gi|nao|the|con|va|lam|het|mat|ton|bao|nhieu|tien|duoc|chuan|bi|\d+')
    return bool(re.fullmatch(r'\s*(?:' + filler + r')(?:\s+(?:' + filler + r'))*\s*', q))


def preflight(query, state, catalog):
    if bare_unbound_reference(query, state):
        return Plan(relation='replace', tasks=[Task(kind='clarify', quote=query, question=UNBOUND)])
    clauses = unsupported_clauses(query, catalog)
    if len(clauses) == 1 and len(re.split(r';|\n|\bvà\b|\bđồng thời\b', query, flags=re.I)) == 1:
        return Plan(relation='replace', tasks=[Task(kind='clarify', quote=query, question=UNSUPPORTED)])
    return None


def enforce(plan, query, state, catalog):
    shortcut = preflight(query, state, catalog)
    if shortcut is not None:
        return shortcut
    clauses = unsupported_clauses(query, catalog)
    if not clauses:
        return plan
    result = []
    for task in plan.tasks:
        span = query if len(plan.tasks) == 1 else task.quote
        if unsupported_information(without_titles(span, catalog)):
            fields = information_fields(span, catalog)
            if not fields:
                result.append(Task(kind='clarify', quote=task.quote, question=UNSUPPORTED))
                continue
            if task.kind == 'procedure':
                # A full literal compound quote may cover a supported fee need
                # and unsupported contact need. Retain only its explicit fields.
                task.fields = fields
        result.append(task)
    for clause in clauses:
        if not any(t.question == UNSUPPORTED and clause in t.quote for t in result):
            result.append(Task(kind='clarify', quote=clause, question=UNSUPPORTED))
    if len(result) > 8:
        return Plan(relation='replace', tasks=[Task(kind='clarify', quote=query, question='G8_TASK_LIMIT')], overflow=True)
    # Keep the literal user order for independently quoted needs.
    result.sort(key=lambda t: query.casefold().find(t.quote.casefold()) if t.quote.casefold() in query.casefold() else len(query))
    plan.tasks = result
    return plan
