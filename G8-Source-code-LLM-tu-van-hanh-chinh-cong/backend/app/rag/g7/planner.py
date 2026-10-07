"""Local model is the sole natural-language planner; no lexical routing veto.

Structured output enforces syntax, not semantic correctness. One bounded repair
for invalid contracts; no regex deletion of user clauses, no hidden old router.
"""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata

import httpx
from pydantic import ValidationError
from app.errors import APIError
from app.rag.g7.catalog import DOMAINS, SUPPORT_BOUNDARIES
from app.rag.g7.contracts import Plan, Scope, Task
from app.rag.g7.prompt import SYSTEM as INTENT_SYSTEM
from app.rag.g7.decompose import resolve, classification_contract, bind_matches, MATCH_SYSTEM
from app.rag.retrieval import normalize

log = logging.getLogger('backend.chat')


def output_examples(catalog):
    code = lambda suffix: next(c['code'] for c in catalog if c['id'].endswith(suffix))
    return [{
        'user': 'Tôi hỏi phí khai tử, còn kết hôn cần giấy nào?',
        'output': {'relation': 'replace', 'tasks': [
            {'kind': 'procedure', 'quote': 'phí khai tử', 'code': code('6696b9fb226a'),
             'fields': ['fees'], 'domains': [], 'candidates': [], 'scope': {}, 'question': ''},
            {'kind': 'procedure', 'quote': 'kết hôn cần giấy nào', 'code': code('6d6862db57ba'),
             'fields': ['required_documents'], 'domains': [], 'candidates': [], 'scope': {}, 'question': ''},
        ]}}, {
        'user': 'Kể tên các thủ tục dành cho hộ kinh doanh.',
        'output': {'relation': 'replace', 'tasks': [{'kind': 'catalog', 'quote': 'thủ tục dành cho hộ kinh doanh',
                    'code': '', 'fields': [], 'domains': ['business'], 'candidates': [], 'scope': {}, 'question': ''}]}
    }]


def wire_contract(catalog):
    """A semantic enum for the model; existing state keeps stable internal IDs."""
    titles = {c['code']: c['label'] for c in catalog}
    if len(set(titles.values())) != len(titles):
        raise ValueError('DUPLICATE_CATALOG_TITLE')
    schema = Plan.model_json_schema()
    schema['$defs']['Scope'] = {'type': 'object', 'additionalProperties': False,
        'properties': {'places': {'type': 'array', 'maxItems': 5,
                                  'items': {'type': 'string', 'maxLength': 100}},
                       'quote': {'type': 'string', 'maxLength': 300}}}
    props = schema['$defs']['Task']['properties']
    props['code']['enum'] = ['', *titles.values(), *SUPPORT_BOUNDARIES]
    props['candidates']['items'] = {'type': 'string', 'enum': list(titles.values())}
    return titles, schema


def to_wire(value, titles):
    if isinstance(value, list):
        return [to_wire(v, titles) for v in value]
    if not isinstance(value, dict):
        return value
    return {k: ({'places': [*v.get('places', []), *[v[x] for x in ('country', 'province', 'ward') if v.get(x)]],
                 'quote': v.get('quote', '')} if k == 'scope' and isinstance(v, dict) and '$ref' not in v else
                titles.get(v, v) if k == 'code' and isinstance(v, str) else
                [titles.get(c, c) for c in v] if k == 'candidates' and isinstance(v, list) else to_wire(v, titles))
            for k, v in value.items()}


def validate_plan(plan, query, catalog, state):
    codes = {c['code'] for c in catalog}
    from_wire = {c['label']: c['code'] for c in catalog}
    def scope_key(scope):
        return tuple(sorted(normalize(v) for v in [*scope.places, scope.country, scope.province, scope.ward] if v))
    previous_scopes = {(t.code, scope_key(t.scope), t.scope.quote.casefold())
                       for t in [*state.active, *state.pending]}
    for task in plan.tasks:
        unsafe = task.code == 'unsupported_conceal_harm'
        if task.code in SUPPORT_BOUNDARIES:
            task.kind, task.code, task.fields, task.candidates, task.domains, task.question = 'outside', '', [], [], [], ''
        if unsafe:
            task.question = 'G8_UNSAFE_ASSISTANCE'
        task.code = from_wire.get(task.code, task.code)
        task.candidates = [from_wire.get(c, c) for c in task.candidates]
        if task.quote not in query:
            # A paraphrased/lowercased model span is NOT evidence. Bind it to the
            # server-owned utterance rather than rejecting an otherwise valid
            # plan over quote typography. Intent correctness is measured in evals,
            # not proved by copying words. Geography has its own strict check below.
            task.quote = query
        if task.code and task.code not in codes or not set(task.candidates) <= codes:
            raise ValueError('UNKNOWN_CATALOG_CODE')
        if not set(task.domains) <= DOMAINS.keys():
            raise ValueError('UNKNOWN_DOMAIN')
        if task.kind == 'procedure' and not task.code:
            raise ValueError('PROCEDURE_REQUIRES_CODE')
        if task.kind == 'catalog' and not task.domains and not task.candidates:
            raise ValueError('CATALOG_REQUIRES_DOMAIN')
        if task.kind != 'procedure' and task.code:
            raise ValueError('NON_PROCEDURE_CODE')
        if task.kind not in {'clarify', 'catalog'} and task.candidates:
            raise ValueError('CANDIDATES_REQUIRE_CLARIFY_OR_CATALOG')
        if task.kind in {'outside', 'chat'}:
            # These parts never execute procedure/source lookups. Invented or
            # misclassified geography must not turn a safe refusal into HTTP 502.
            task.scope = type(task.scope)()
        scope_names = [*task.scope.places, task.scope.country, task.scope.province, task.scope.ward]
        if any(scope_names):
            inherited = ((task.code, scope_key(task.scope), task.scope.quote.casefold()) in previous_scopes
                         and plan.relation in {'continue', 'extend'})
            # Letter case is not a change of jurisdiction. Match literal Unicode
            # text without stripping accents, then store the original user span.
            if not inherited and task.scope.quote:
                pattern = r'\s+'.join(re.escape(p) for p in unicodedata.normalize('NFC', task.scope.quote).split())
                match = re.search(pattern if pattern else r'(?!)',
                                  unicodedata.normalize('NFC', query), re.IGNORECASE)
                if match:
                    task.scope.quote = match.group(0)
            if not inherited and (not task.scope.quote or task.scope.quote not in query):
                raise ValueError('SCOPE_WITHOUT_USER_EVIDENCE')
            if not inherited and any(normalize(v) not in normalize(task.scope.quote)
                                     for v in scope_names if v):
                raise ValueError('SCOPE_NAME_NOT_IN_QUOTE')
        task.fields = list(dict.fromkeys(task.fields))
        task.candidates = list(dict.fromkeys(task.candidates))
    merged = []
    for task in plan.tasks:
        existing = next((t for t in merged if t.kind == task.kind == 'procedure'
                         and t.code == task.code
                         and (t.scope.country, t.scope.province, t.scope.ward, t.scope.places) ==
                             (task.scope.country, task.scope.province, task.scope.ward, task.scope.places)), None)
        if existing:
            existing.fields = list(dict.fromkeys([*existing.fields, *task.fields]))
            existing.quote = query
        else:
            merged.append(task)
    plan.tasks = merged
    return plan


def _service_place(query):
    """Return one explicit place of service, never a stated residence.

    This deliberately covers only high-confidence 'tại <place>' clauses. Other
    geography remains the model's job; uncertain extraction must not rewrite a
    multi-service plan or turn a residence into a service location.
    """
    clauses = []
    for match in re.finditer(r'\btại\s+([^\n,;!?]+)', query, re.IGNORECASE):
        before = query[max(0, match.start() - 35):match.start()].casefold()
        if re.search(r'(?:sống|cư trú|thường trú|tạm trú|đang ở)\s*$', before):
            continue
        tail = re.split(r'\.(?=\s|$)', match.group(1), maxsplit=1)[0]
        tail = re.split(r'\s+(?:cần|thì|mà|nhưng|để|gồm|bao lâu|mất bao nhiêu|hồ sơ|giấy tờ|hỏi|cho biết)\b',
                        tail, maxsplit=1, flags=re.IGNORECASE)[0].strip(' .')
        if not tail or tail.casefold() in {'đây', 'đó', 'nhà', 'đâu', 'địa phương tôi'}:
            continue
        # A capitalized place or an administrative unit is much stronger
        # evidence than generic uses such as 'tại quầy' or 'tại nhà'.
        if not (re.match(r'(?i)(?:phường|xã|quận|huyện|tỉnh|thành phố|tp\.?\s*)\b', tail)
                or re.match(r'[A-ZÀ-ỸĐ]', tail)):
            continue
        clauses.append((tail, query[match.start():match.start(1) + len(tail)]))
    return clauses[0] if len(clauses) == 1 else None


def _death_browse(query):
    q = query.casefold()
    listing = (re.search(r'(?:liệt kê|kể tên|danh sách).{0,90}(?:thủ tục|dịch vụ)', q)
               or re.search(r'(?:có|những|các).{0,35}thủ tục.{0,30}(?:nào|gì)', q))
    death = (re.search(r'qua đời|khai tử|mai táng|hỏa táng|từ trần', q)
             or re.search(r'(?:bố|mẹ|cha|ông|bà|người thân|người nhà|người).{0,30}(?:mất|chết)', q))
    return bool(listing and death and not re.search(r'kết hôn|khai sinh', q))


def _completed_or_rejected_marriage(query):
    q = query.casefold()
    return bool(re.search(r'(?:đã|vừa)\s+(?:cưới|kết hôn)|'
                          r'(?:cưới|kết hôn).{0,20}\brồi\b(?=\s*(?:[,.;!?]|$|bây giờ|giờ|mà|nên|thì))|'
                          r'(?:không|chẳng)\s+(?:hỏi|cần|muốn|định|phải).{0,25}(?:kết hôn|cưới)|'
                          r'cần gì\s+(?:đăng ký\s+)?kết hôn', q))


def _positive_marriage_information_request(query):
    q = query.casefold()
    return bool(re.search(r'(?:phí|\btiền\b).{0,30}(?:đăng ký\s+)?kết hôn|'
                          r'kết hôn.{0,24}(?:phí|\btiền\b)', q))


def _bind_explicit_service_place(plan, query):
    place = _service_place(query)
    if place and len(plan.tasks) == 1 and plan.tasks[0].kind in {'procedure', 'catalog', 'clarify'}:
        plan.tasks[0].scope = Scope(places=[place[0]], quote=place[1])
    return plan


def _explicit_coordinated_services(query, catalog):
    """Recognize named coordinated services, not incidental document words."""
    q = query.casefold()
    names = [('khai sinh', 'birth_registration'), ('khai tử', 'death_registration'),
             ('kết hôn', 'marriage_domestic')]
    hits = [(q.find(term), label) for term, label in names if q.find(term) >= 0]
    if len(hits) < 2 or re.search(r'\btại\s+', q):
        return None  # Per-service jurisdictions must remain model-owned.
    hits.sort()
    for left, right in zip(hits, hits[1:]):
        between = q[left[0] + len(next(term for term, label in names if label == left[1])):right[0]]
        if not re.search(r'\b(?:và|lẫn|rồi|cùng với)\b|[,/]', between):
            return None
    # Only shared, positive field requests qualify for this legacy shortcut.
    if re.search(r'không|đừng|chưa|ở đâu|bao lâu|mấy ngày|mang giấy|số nhà', q):
        return None
    fee = bool(re.search(r'phí|\btiền\b|mất tiền|tốn bao nhiêu', q))
    documents = bool(re.search(r'hồ sơ|giấy tờ|chuẩn bị giấy', q))
    if fee == documents:
        return None  # Mixed or absent field questions stay model-led.
    by_label = {c['label']: c['code'] for c in catalog}
    return ([by_label[label] for _, label in hits], ['fees' if fee else 'required_documents'])


def _ordinal_fields(query):
    q = query.casefold()
    fields = []
    if re.search(r'hồ sơ|giấy tờ|chuẩn bị giấy|mang giấy', q): fields.append('required_documents')
    if re.search(r'phí|mất tiền|tốn bao nhiêu', q): fields.append('fees')
    if re.search(r'nộp ở đâu|nơi nộp|chỗ nào nhận', q): fields.append('receiving_authority')
    if re.search(r'bao lâu|thời gian giải quyết|mấy ngày', q): fields.append('processing_times')
    if re.search(r'online|trực tuyến|hình thức nộp', q): fields.append('submission_methods')
    return fields


def reconcile_plan(plan, query, catalog, state):
    """Enforce a few high-confidence intent invariants after typed validation.

    These checks do not provide legal facts or search by exact question text.
    Everything else stays model-led; ambiguous cases should remain clarifications.
    """
    index = {c['code']: c for c in catalog}
    q = unicodedata.normalize('NFC', query).casefold()
    house_book = re.search(r'(?<!\w)sổ\s+nhà(?!\w)', q)
    house_number = re.search(r'(?<!\w)số\s+nhà(?!\w)', q)
    book_is_background = re.search(r'(?:đã có|có sẵn|mang theo|sẵn có)\s+sổ\s+nhà', q)
    uncertain_book_name = re.search(r'(?:không|chưa)\s+(?:biết|rõ)|gọi.{0,25}là gì', q)
    explicit_first_registration = re.search(
        r'đăng ký đất đai.{0,50}lần đầu|lần đầu.{0,50}đăng ký đất đai', q)
    housing_labels = {'house_number_assignment', 'first_land_registration',
                      'confirm_housing_status', 'confirm_house_land_location'}
    if (len(plan.tasks) == 1 and house_book and not house_number and not book_is_background
            and not explicit_first_registration and any(
                (t.kind == 'procedure' and t.code in index and index[t.code]['label'] in housing_labels)
                or (t.kind == 'clarify' and any(c in index and index[c]['label'] in housing_labels
                                                for c in t.candidates))
                or (t.kind == 'catalog' and uncertain_book_name)
                for t in plan.tasks)):
        # The diacritic is explicit evidence: a book/certificate request is not
        # a request for an address number. The exact document is still unclear,
        # so do not guess first land registration or reuse last turn's T10 fee.
        plan.tasks = [Task(kind='clarify', quote=query, question='G8_HOUSE_BOOK_NUMBER')]
        plan.relation = 'replace'
        return plan
    number_correction = re.search(
        r'(?:(?:tôi|mình)\s+nói|ý\s+(?:tôi|mình)?\s*là)\s+số\s+nhà', q)
    other_housing_request = re.search(
        r'đăng ký đất đai|xác nhận vị trí|tình trạng nhà ở|sổ đỏ|sổ hồng|cấp giấy chứng nhận', q)
    if (number_correction and not other_housing_request and len(plan.tasks) > 1
            and all(t.kind == 'procedure' and t.code in index
                    and index[t.code]['label'] in housing_labels for t in plan.tasks)):
        number_tasks = [t for t in plan.tasks if index[t.code]['label'] == 'house_number_assignment']
        if len(number_tasks) == 1:
            # An explicit correction to the address number is one request;
            # an incidental "địa chỉ" must not become a second land procedure.
            plan.tasks = number_tasks
            plan.relation = 'replace'
            return plan
    if any(t.question == 'G8_UNSAFE_ASSISTANCE' for t in plan.tasks):
        return plan  # Never let a legacy browsing guard undo a refusal.
    if _death_browse(query):
        # A request for the *list* is not five simultaneous applications. The
        # reviewed death domain excludes stale marriage context, while the
        # executor still labels support as conditional and non-exhaustive.
        plan.tasks = [Task(kind='catalog', quote=query, domains=['death'])]
        plan.relation = 'replace'

    named_services = _explicit_coordinated_services(query, catalog)
    if (named_services and all(t.kind == 'clarify' or (t.kind == 'procedure' and t.code in named_services[0])
                              for t in plan.tasks)):
        existing = {t.code: t for t in plan.tasks if t.kind == 'procedure'}
        plan.tasks = [Task(kind='procedure', quote=query, code=c, fields=named_services[1],
                           scope=existing[c].scope if c in existing else Scope()) for c in named_services[0]]

    if _completed_or_rejected_marriage(query) and not _positive_marriage_information_request(query):
        marriage = {c['code'] for c in catalog
                    if c['label'] in {'marriage_domestic', 'marriage_foreign_element'}}
        for task in plan.tasks:
            if task.kind == 'clarify' and task.candidates:
                task.candidates = [c for c in task.candidates if c not in marriage]
                if len(task.candidates) == 1:
                    remaining = index[task.candidates[0]]
                    if 'business' in remaining['domains'] and re.search(r'mở\s+(?:quán|tiệm)|kinh doanh', query.casefold()):
                        task.kind, task.code, task.candidates = 'procedure', remaining['code'], []
            if task.kind == 'clarify' and not task.candidates:
                alternatives = [c for pending in state.pending for c in pending.candidates
                                if c in index and c not in marriage]
                if len(set(alternatives)) == 1:
                    code = alternatives[0]
                    prior = next(p for p in state.pending if code in p.candidates)
                    task.kind, task.code, task.candidates = 'procedure', code, []
                    task.fields = list(prior.fields)
                elif len(state.focused) == 1 and state.focused[0] in index and state.focused[0] not in marriage:
                    prior = next((p for p in state.active if p.code == state.focused[0]), None)
                    if prior:
                        task.kind, task.code, task.candidates = 'procedure', prior.code, []
                        task.fields = list(prior.fields)
            if task.kind == 'procedure' and task.code in marriage and not re.search(
                    r'(?:muốn|cần|xin|hỏi).{0,40}(?:đăng ký|làm).{0,12}kết hôn', query.casefold()):
                task.kind, task.code, task.fields, task.candidates = 'clarify', '', [], []

    opening = re.search(r'\bmở\s+(?:quán|tiệm|cửa hàng|hộ kinh doanh)\b', query.casefold())
    suspension = re.search(r'tạm\s*(?:ngừng|nghỉ)|đóng.{0,35}(?:mở|bán)\s*lại|nghỉ.{0,35}(?:mở|bán)\s*lại',
                           query.casefold())
    if opening and not suspension:
        creation = {c['code'] for c in catalog if c['label'] == 'create_household_business'}
        temporary = {c['code'] for c in catalog if c['label'] == 'business_temporary_suspension'}
        if any(t.kind == 'procedure' and t.code in creation for t in plan.tasks):
            plan.tasks = [t for t in plan.tasks if not (t.kind == 'procedure' and t.code in temporary)]
            if not re.search(r'xây|sửa nhà|khởi công|quy hoạch|giấy phép xây dựng', query.casefold()):
                unrelated = {'construction_permit', 'notify_construction_start', 'planning_information'}
                plan.tasks = [t for t in plan.tasks if not (t.kind == 'procedure'
                    and index[t.code]['label'] in unrelated)]

    if re.match(r'\s*hồ sơ\b', query.casefold()) and not re.search(
            r'phí|nơi nộp|thời gian|online|trực tuyến|tất cả|toàn bộ', query.casefold()):
        for task in plan.tasks:
            if task.kind == 'procedure':
                task.fields = ['required_documents']

    ordinal = re.search(r'\b(?:cái|việc|mục|thủ tục)\s+(?:đầu(?: tiên)?|thứ nhất|số 1)\b', query.casefold())
    plural = re.search(r'\b(?:cả hai|cả 2|hai cái|thứ hai|số 2|và cái)\b', query.casefold())
    if ordinal and not plural and len(state.displayed_options) > 1:
        first = state.displayed_options[0]
        chosen = [t for t in plan.tasks if t.kind == 'procedure' and t.code == first]
        fields = _ordinal_fields(query)
        plan.tasks = [Task(kind='procedure', quote=query, code=first,
                           fields=fields or (chosen[0].fields if chosen else []),
                           scope=chosen[0].scope if chosen else Scope())]
        plan.relation = 'continue'

    # A single explicit service-place clause on a single-service request is
    # authoritative even when the model omitted scope. Never apply one place
    # globally to a multi-intent request with different jurisdictions.
    return _bind_explicit_service_place(plan, query)


async def understand(query, state, catalog, history, client, request_id):
    from app.rag.g8.turn_checks import menu_selection, explicit_catalog
    selected_menu = menu_selection(query, state) or explicit_catalog(query, state, catalog)
    if selected_menu is not None:
        return selected_menu
    query = unicodedata.normalize('NFC', query)
    if state.awaiting_rephrase:
        history = []
    model = os.environ.get('G7_PLANNER_MODEL', 'G7-Qwen3-4B-Instruct')
    base = os.environ.get('G7_PLANNER_URL', 'http://llama-server:8080/v1').rstrip('/')
    index = {c['code']: c for c in catalog}
    titles, schema = wire_contract(catalog)
    # The grammar has the full enum. Do not repeat its 36 long titles twice in
    # textual schema as well; CATALOG supplies each semantic key once.
    payload = {'OUTPUT_SCHEMA': Plan.model_json_schema(), 'EXAMPLES': output_examples(catalog), 'DOMAIN_NAMES': DOMAINS,
               'CATALOG': [{k: v for k, v in c.items() if k not in {'id', 'label'}} for c in catalog],
               'STATE': {
                   'awaiting_rephrase': state.awaiting_rephrase,
                   'active': [t.model_dump() for t in state.active if t.code in state.focused],
                   'pending': [t.model_dump() for t in state.pending],
                   'other_topics': [{'code': t.code, 'title': index[t.code]['title']}
                                    for t in state.active if t.code not in state.focused and t.code in index],
               },
               # Factual assistant answers contain local addresses and document
               # names. They are NOT user intent and caused invented scope and
               # false switches. The structured state already records the offered
               # choices, fields, questions and active subjects without these facts.
               'RECENT_USER_REQUESTS': [m.content[:1200] for m in history if m.role == 'user'][-2:],
               'ACTIVE_FOCUS': [{'code': c, 'title': index[c]['title']} for c in state.focused if c in index],
               'DISPLAYED_OPTIONS_IN_ORDER': [{'position': i, 'code': c, 'title': index[c]['title']}
                    for i, c in enumerate(state.displayed_options, 1) if c in index],
               'CURRENT_USER': query}
    payload['OUTPUT_SCHEMA']['$defs']['Scope'] = schema['$defs']['Scope']
    payload['CATALOG'].extend({'code': code, 'supported': False, 'purpose': purpose}
                             for code, purpose in SUPPORT_BOUNDARIES.items())
    if os.environ.get('G8_COMPACT_PLAN') == 'true':
        # Experiment only: omit empty optional task fields, preserve scope/quote.
        for example in payload['EXAMPLES']:
            for task in example['output']['tasks']:
                for key in ('code', 'fields', 'domains', 'candidates', 'question'):
                    if not task.get(key):
                        task.pop(key, None)
        payload['OUTPUT_RULE'] = ('Bỏ trường tùy chọn có giá trị rỗng trong JSON task. '
            'Giữ quote và scope; không bỏ ý định, địa phương hay field người dùng yêu cầu.')
    payload = to_wire(payload, titles)
    # Keep the actual request as the final user turn. Mixing catalog, history
    # and the request in one long JSON user message encouraged replaying old
    # tasks, especially after a correction or an ordinal selection.
    static_keys = ('OUTPUT_SCHEMA', 'EXAMPLES', 'DOMAIN_NAMES', 'CATALOG')
    static = {k: payload[k] for k in static_keys}
    if 'OUTPUT_RULE' in payload:
        static['OUTPUT_RULE'] = payload.pop('OUTPUT_RULE')
    context = {k: v for k, v in payload.items() if k not in static_keys and k != 'CURRENT_USER'}
    messages = [
        {'role': 'system', 'content': INTENT_SYSTEM
                                    + '\nCURRENT_USER là tin nhắn user cuối cùng.\nREFERENCE:\n'
                                    + json.dumps(static, ensure_ascii=False)},
        {'role': 'user', 'content': 'CONTEXT ONLY, not a new request:\n' + json.dumps(context, ensure_ascii=False)},
        {'role': 'assistant', 'content': 'Đã nhận ngữ cảnh. Mình sẽ lập kế hoạch chỉ cho yêu cầu tiếp theo.'},
        {'role': 'user', 'content': query},
    ]
    started = time.monotonic()
    resolution = None

    async def completion(turn_messages):
        call_started = time.monotonic()
        response = await client.post(base + '/chat/completions', json={
            'model': model, 'messages': turn_messages,
            'temperature': 0.6 if os.environ.get('G7_PLANNER_THINKING') == 'true' else 0,
            'top_p': 0.95, 'top_k': 20, 'seed': 42,
            'max_tokens': 3500, 'stream': False,
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'turn_plan', 'strict': True, 'schema': schema}},
        }, timeout=155)
        response.raise_for_status()
        response_data = response.json()
        usage = response_data.get('usage') or {}
        timings = response_data.get('timings') or {}
        log.info('g8_completion %s', json.dumps({
            'request_id': str(request_id), 'ms': round((time.monotonic()-call_started)*1000),
            'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens'),
            'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens'),
            'prompt_ms': timings.get('prompt_ms'), 'predicted_ms': timings.get('predicted_ms'),
        }))
        choice = response_data['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ValueError('PLAN_TRUNCATED')
        return choice['message']['content']

    try:
        async with asyncio.timeout(160):
            if os.environ.get('G7_DECOMPOSE', 'false').lower() == 'true':
                resolution = await resolve(query, state, catalog, history, client, base, model)
                schema = classification_contract(list(titles.values()), len(resolution.requests))
                messages = [
                    {'role': 'system', 'content': MATCH_SYSTEM + '\nOUTPUT_SCHEMA:\n'
                     + json.dumps(schema, ensure_ascii=False) + '\nCATALOG:\n'
                     + json.dumps(payload['CATALOG'], ensure_ascii=False) + '\nDOMAINS:\n'
                     + json.dumps(DOMAINS, ensure_ascii=False)},
                    {'role': 'user', 'content': json.dumps({'CURRENT_USER': query,
                     'NEEDS': [{'request_index': i, 'need': r.need} for i, r in enumerate(resolution.requests)]}, ensure_ascii=False)},
                ]
            from app.rag.g8.graph import plan_with_graph
            from app.rag.g8.guards import semantic_check
            from app.rag.g8.turn_checks import check_turn

            def validated(raw):
                draft = bind_matches(raw, resolution, query) if resolution else Plan.model_validate_json(raw)
                from app.rag.g8.turn_checks import discard_stale_local_scope
                draft = discard_stale_local_scope(draft, query, state)
                plan = validate_plan(_bind_explicit_service_place(draft, query), query, catalog, state)
                log.info('g8_graph_draft request_id=%s tasks=%s', request_id,
                         json.dumps([{'kind': t.kind, 'code': t.code, 'fields': t.fields,
                                      'has_scope': bool(t.scope.places or t.scope.country or t.scope.ward)} for t in plan.tasks]))
                plan = reconcile_plan(plan, query, catalog, state)
                plan = semantic_check(plan, query, catalog)
                plan = check_turn(plan, query, catalog, state)
                # Revalidate after reconciliation too; a guard cannot bypass the
                # same ID/field/geography contract used on raw model output.
                plan = validate_plan(plan, query, catalog, state)
                log.info('g8_plan_candidate request_id=%s tasks=%s', request_id,
                         json.dumps([{'kind': t.kind, 'code': t.code, 'fields': t.fields,
                                      'domains': t.domains, 'candidates': t.candidates} for t in plan.tasks]))
                return plan

            return await plan_with_graph(messages, completion, validated, request_id)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise APIError(503, 'G7_PLANNER_UNAVAILABLE',
                       'Bộ hiểu yêu cầu đang bận hoặc chưa sẵn sàng. Bạn thử lại giúp mình nhé.') from exc
    except (ValueError, KeyError, IndexError) as exc:
        raise APIError(502, 'G7_PLAN_INVALID', 'Chưa phân tích được yêu cầu đầy đủ. Bạn thử lại nhé.') from exc
    raise APIError(502, 'G7_PLAN_INVALID', 'Chưa phân tích được yêu cầu một cách tin cậy. Bạn thử diễn đạt lại nhé.')
