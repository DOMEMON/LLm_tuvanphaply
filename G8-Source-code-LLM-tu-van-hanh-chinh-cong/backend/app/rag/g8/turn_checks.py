"""Bounded consistency checks. Never supply procedural facts or guess an ID."""
import re
import unicodedata

from app.rag.g7.contracts import Scope, Task


def menu_selection(query, state):
    """Resolve only a bare displayed index, retaining a previously scoped subject."""
    if not re.fullmatch(r'\s*\d{1,2}\s*[.)]?\s*', query):
        return None
    position = int(re.search(r'\d+', query).group()) - 1
    if not 0 <= position < len(state.displayed_options):
        return None
    from app.rag.g7.contracts import Plan
    code = state.displayed_options[position]
    previous = next((t for t in state.active if t.code == code), None)
    return Plan(relation='continue', tasks=[Task(kind='procedure', quote=query, code=code,
        scope=previous.scope.model_copy(deep=True) if previous else Scope())])


def explicit_catalog(query, state, catalog):
    from app.rag.g7.contracts import Plan
    from app.rag.g7.planner import _service_place
    from app.rag.g8.catalog_browse import request_for
    # Shared lexical shortcuts handle one menu only. Compound lists and fee
    # predicates need the typed semantic planner to preserve independent needs.
    qfold = fold(query)
    if state.catalog_context.get('typed_predicates'):
        return None  # Typed filters cannot use the legacy context parser.
    if (re.search(r'\bphi\b|dong tien|thu tien|mat tien|\n|;', qfold)
            or len(re.findall(r'liet ke|thu tuc nao|nhung thu tuc|cac thu tuc', qfold)) > 1):
        return None
    request = request_for(query, state)
    if request is not None:
        return Plan(relation='continue' if request.get('explain_value') else 'replace',
            tasks=[Task(kind='catalog', quote=query, domains=request['domains'])])
    q = unicodedata.normalize('NFC', query).casefold()
    if re.search(r'online|trực tuyến|trực tiếp|cùng với|kinh tế|hạ tầng', q):
        return None
    domains = []
    mixed = (re.search(r'khai sinh|kết hôn|khai tử|vay vốn|nấu|chiên|đất đai|hộ kinh doanh|hưởng trợ cấp|khuyết tật|liệt sĩ|người có công|số nhà|sổ nhà|nhà ở|chứng thực|không|đừng|chưa|làm giả|che giấu|giết|ngụy tạo|nguỵ tạo', q)
             or re.search(r'tất cả|toàn bộ|từng|mỗi', q) and re.search(r'giấy tờ|hồ sơ', q))
    if not mixed:
        if re.search(r'\bhộ (?:tịch|tục)\b', q) and not re.search(r'trích lục|bản sao|hộ tịch\s+(?:này|đó)', q):
            domains = ['civil']
        elif re.search(r'văn hóa\s*(?:-|và)?\s*xã hội', q) and re.search(r'lĩnh vực|các thủ tục|liệt kê', q):
            domains = ['welfare', 'education', 'merit']
        elif re.search(r'thủ tục.*liên quan.*(?:người.*(?:mất|chết)|qua đời)|(?:các|những).*thủ tục.*(?:mất|chết|qua đời)', q):
            domains = ['death']
    if domains:
        place = _service_place(query)
        if not place and re.search(r'\b(?:ở|sang|ngoài)\b', q):
            return None
        return Plan(relation='replace', tasks=[Task(kind='catalog', quote=query, domains=domains,
            scope=Scope(places=[place[0]], quote=place[1]) if place else Scope())])
    if re.fullmatch(r'\s*(?:chỉ\s+)?liệt kê(?:\s+ra)?\s+thôi[, ]*(?:(?:không|ko) cần (?:nói )?chi tiết)?[.!]?\s*', q) and state.displayed_options:
        selected = set(state.displayed_options)
        for domain in sorted({d for c in catalog for d in c['domains']}):
            if {c['code'] for c in catalog if domain in c['domains']} == selected:
                return Plan(relation='continue', tasks=[Task(kind='catalog', quote=query, domains=[domain])])
        if len(selected) <= 5:
            return Plan(relation='continue', tasks=[Task(kind='catalog', quote=query, candidates=state.displayed_options)])
    return None


def discard_stale_local_scope(plan, query, state):
    from app.rag.g7.workflow import allowed_scope
    from app.rag.g7.planner import _service_place
    if _service_place(query) or re.search(r'\b(?:tại|ở|sang)\s+', query, re.IGNORECASE):
        return plan
    q = fold(query)
    old = [t.scope for t in [*state.active, *state.pending]]
    for task in plan.tasks:
        names = [*task.scope.places, task.scope.country, task.scope.province, task.scope.ward]
        known_names = any(set(filter(None, names)) == set(filter(None, [*s.places, s.country, s.province, s.ward])) for s in old)
        # A local place invented from profile/history can be removed safely:
        # the source already has that fixed default. Never erase another region.
        literal_local = all(fold(n) in {'tang nhon phu', 'phuong tang nhon phu', 'tp.hcm', 'tp hcm',
            'ho chi minh', 'thanh pho ho chi minh', 'viet nam', 'vietnam', 'vn'} for n in names if n)
        if (any(names) and allowed_scope(task.scope) and (known_names or literal_local)
                and task.scope.quote not in query
                and not any(fold(n) in q for n in names if n)):
            if not any(t.code == task.code and t.scope == task.scope for t in state.active):
                task.scope = Scope()
    return plan


def review_categories(plan, query, catalog, state=None):
    q = unicodedata.normalize('NFC', query).casefold()
    broad_death = (re.search(r'thủ tục.*liên quan.*(?:người.*(?:mất|chết)|qua đời)|(?:các|những).*thủ tục.*(?:mất|chết|qua đời)', q)
        and not re.search(r'khai sinh|kết hôn|khuyết tật|hưởng trợ cấp|liệt sĩ|người có công|nấu|vay vốn|đất đai', q))
    if (broad_death and not re.search(r'\bphi\b|dong tien|thu tien|mat tien', fold(query))
            and len(plan.tasks) == 1 and plan.tasks[0].kind == 'catalog'):
        plan.tasks[0].domains, plan.tasks[0].candidates = ['death'], []
    # Residence alone says nothing about nationality, especially in mixed-place
    # requests. Do not turn "tôi sống ở Hà Nội" into a marriage clarification.
    returned = re.search(r'từ\s+.{1,45}?\s+về|(?:du học|đi làm|làm việc)\s+(?:ở|tại)', q)
    nationality = re.search(r'quốc tịch|người (?:nhật|hàn|mỹ|pháp|nước ngoài|việt)|công dân|yếu tố nước ngoài', q)
    pending = state and any(t.question == 'G8_MARRIAGE_NATIONALITY' for t in state.pending)
    if not nationality and (pending or returned and re.search(r'cưới|kết hôn', q)):
        marriage = {c['code'] for c in catalog if c['label'] in {'marriage_domestic', 'marriage_foreign_element'}}
        for i, task in enumerate(plan.tasks):
            if task.kind == 'procedure' and task.code in marriage:
                plan.tasks[i] = Task(kind='clarify', quote=query, question='G8_MARRIAGE_NATIONALITY')
        if returned:
            for task in plan.tasks:
                if task.kind == 'clarify' and not task.candidates:
                    task.question = 'G8_MARRIAGE_NATIONALITY'
    transport = re.search(r'(?:mang|măng|đưa|vận chuyển).{0,60}(?:qua|sang|ra).{0,50}(?:hỏa táng|hoả táng|mai táng)', q)
    if (transport and re.search(r'được (?:không|ko|k)|có được|được phép|cho phép|xin phép', q)
            and not re.search(r'đăng ký khai tử|hồ sơ khai tử|giấy tờ khai tử|lệ phí khai tử|hỗ trợ|trợ cấp', q)):
        related = {c['code'] for c in catalog if c['label'] in {'death_registration', 'cremation_support'}}
        retained = [t for t in plan.tasks if not (t.kind == 'procedure' and t.code in related)]
        plan.tasks = retained if any(t.kind == 'outside' for t in retained) else retained + [Task(kind='outside', quote=query)]
    return plan


def fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn').replace('đ', 'd')


def requested_fields(text):
    """Positive question clauses only; this is a check, not an intent router."""
    q = fold(text)
    if re.search(r'tat ca|toan bo|day du thong tin|tong quan', q):
        return None
    clauses = re.split(r'[.!?;]|,|\b(?:nhung|va|con)\b', q)
    positive = ' '.join(c for c in clauses if not re.search(
        r'\bdung\b|chua hoi|khong hoi|khong can|chua can|khoi noi|da co|co roi', c))
    positive = re.sub(r'((?:nop o dau|noi nop|dia diem nop|cho nao nhan)\s+)ho so', r'\1', positive)
    patterns = {
        'required_documents': r'ho so|giay to|mang giay|bo sung giay|can giay|chuan bi giay',
        'processing_times': r'bao lau|may ngay|thoi gian|thoi han',
        'receiving_authority': r'nop o dau|noi nop|dia diem nop|cho nao nhan',
        'submission_methods': r'hinh thuc|cach nop|kenh nop|online|truc tuyen',
        'fees': r'le phi|\bphi\b|bao nhieu tien|mat tien|ton tien|voi tien|tien thoi',
        'applicant_scope': r'dieu kien|doi tuong',
    }
    return [field for field, pattern in patterns.items() if re.search(pattern, positive)]


def check_turn(plan, query, catalog, state):
    if plan.overflow:
        return plan
    plan = review_categories(plan, query, catalog, state)
    from app.rag.g7.planner import _service_place
    q = unicodedata.normalize('NFC', query).casefold()
    if (all(t.kind == 'outside' for t in plan.tasks)
            and not any(t.question == 'G8_UNSAFE_ASSISTANCE' for t in plan.tasks)
            and re.search(r'và\s+(?:hỏi|xin|tìm hiểu)\s+(?:về\s+)?thủ tục', q)):
        raise ValueError('MIXED_REQUEST_DROPPED_SUPPORTED_NEED')
    # An unresolved accented distinction is not resolved by dropping its marks.
    ambiguous_house = (re.search(r'\bso nha\b', q)
        and any(re.search(r'\bsổ\s+nhà\b', t.quote.casefold()) for t in state.pending)
        and not re.search(r'biển|địa chỉ|đánh số|sổ đỏ|sổ hồng|chứng nhận', q))
    if ambiguous_house and len(plan.tasks) == 1:
        plan.tasks = [Task(kind='clarify', quote=query, question='G8_UNACCENTED_HOUSE_REFERENCE')]
        plan.relation = 'continue'
        return plan
    procedures = [t for t in plan.tasks if t.kind == 'procedure']
    # A document already held is background, not positive evidence to switch.
    background = re.search(r'giấy.{0,45}(?:có rồi|đã có)|(?:đã có|có sẵn).{0,30}giấy', q)
    followup = re.search(r'bổ sung|thêm giấy|giải quyết bao lâu', q)
    new_action = re.search(r'đăng ký|làm thủ tục|chuyển sang|hỏi về', q)
    if background and followup and not new_action and len(state.focused) == 1 and len(plan.tasks) == 1:
        if any(t.code != state.focused[0] for t in procedures):
            label = next(c['label'] for c in catalog if c['code'] == state.focused[0])
            raise ValueError('BACKGROUND_DOCUMENT_USE_' + label.upper())
    correction = (re.match(r'\s*(?:tôi|mình|em)\s+đang hỏi\s+', q)
                  and not re.search(r'các|những|liệt kê|danh mục|lĩnh vực|hộ tịch', q))
    if correction and any(t.kind == 'catalog' for t in plan.tasks):
        raise ValueError('CORRECTION_REQUIRES_PROCEDURE_OR_CLARIFICATION_NOT_CATALOG')
    # Every task owns its service location. Never distribute one place globally.
    multi_places = len(re.findall(r'\btại\s+', q)) > 1
    for task in plan.tasks:
        if task.kind == 'catalog' and not task.predicates:
            from app.rag.g8.catalog_browse import request_for
            filtered = request_for(task.quote, state)
            if re.search(r'\bphi\b|dong tien|mat tien|khong qua|it nhat', fold(task.quote)):
                raise ValueError('CATALOG_CONDITION_REQUIRES_TYPED_PREDICATES')
            if re.search(r'truc tiep|truc tuyen|online|qua mang', fold(task.quote)) and not filtered:
                raise ValueError('CATALOG_CONDITION_REQUIRES_TYPED_PREDICATES')
        if correction and task.kind == 'procedure' and not task.fields:
            previous = next((t for t in state.active if t.code == task.code), None)
            if previous:
                task.fields = list(previous.fields)
        if task.kind == 'outside' and multi_places:
            from app.rag.g7.workflow import allowed_scope
            # Exact catalog names under a quoted local service request cannot
            # be refused just because residence/another task is elsewhere.
            for clause in re.split(r'[.!?;]', task.quote):
                place = _service_place(clause)
                if not place or not allowed_scope(Scope(places=[place[0]])):
                    continue
                span = fold(clause)
                named = [c for c in catalog if re.sub(r'^(thu tuc\s+)?(dang ky\s+)?', '',
                         fold(c['title'])).strip() in span]
                if named:
                    raise ValueError('LOCAL_SUPPORTED_SERVICE_MUST_NOT_INHERIT_OTHER_TASK_OUTSIDE_SCOPE')
        if task.kind not in {'procedure', 'catalog', 'clarify'}:
            continue
        if multi_places and len(plan.tasks) > 1:
            if task.quote == query or task.quote not in query:
                # A separate literal scope span is equally valid evidence.
                scope_span = task.scope.quote
                if not scope_span or scope_span == query or scope_span not in query:
                    raise ValueError('MULTI_LOCATION_REQUIRES_SEPARATE_LITERAL_TASK_QUOTES')
                place = _service_place(scope_span)
            else:
                place = _service_place(task.quote)
            if place:
                task.scope = Scope(places=[place[0]], quote=place[1])
            elif not task.scope.places and not task.scope.country and not task.scope.ward:
                raise ValueError('MULTI_LOCATION_TASK_MISSING_SERVICE_SCOPE')
        if task.kind != 'procedure':
            continue
        span = query if len(plan.tasks) == 1 else task.quote
        # A full-query quote cannot establish independent field ownership.
        if len(plan.tasks) > 1 and span == query:
            continue
        exclusive_sentence = False
        if len(plan.tasks) > 1 and span in query:
            # A clipped quote like "nộp khai sinh" loses the actual question
            # immediately before it. Widen to its sentence only when that
            # sentence belongs to this task and no other procedure task.
            sentences = [s.strip() for s in re.split(r'[.!?;]', query) if s.strip()]
            enclosing = [s for s in sentences if span in s]
            if len(sentences) > 1 and len(enclosing) == 1 and not any(
                    t is not task and t.kind == 'procedure' and t.quote.strip(' .!?;') in enclosing[0]
                    for t in plan.tasks):
                span = enclosing[0]
                exclusive_sentence = True
        # Field-looking words inside official titles are not information asks
        # (chi phí mai táng, trước thời hạn, đối tượng bảo trợ...).
        for card in sorted(catalog, key=lambda c: len(c['title']), reverse=True):
            short = re.sub(r'^(?:thủ tục\s+)', '', card['title'], flags=re.IGNORECASE)
            # Asking to obtain a named certificate is not asking for its file's
            # required documents. E.g. "làm giấy tờ xác nhận ...; nơi nộp?".
            span = re.sub(r'(?:làm|xin|cấp)\s+(?:giấy tờ|giấy)\s+' + re.escape(short),
                          ' [procedure] ', span, flags=re.IGNORECASE)
            span = re.sub(re.escape(card['title']), ' [procedure] ', span, flags=re.IGNORECASE)
        wanted = requested_fields(span)
        if wanted and set(task.fields) != set(wanted):
            if span in query and len(plan.tasks) > 1 and span != query:
                # Correct explicit fields only; subject ID still comes from the
                # model. Each literal local clause owns its information request.
                task.fields = wanted
                continue
            raise ValueError('FIELDS_MUST_MATCH_POSITIVE_CURRENT_TASK_NOT_HISTORY_OR_NEGATION')
    return plan
