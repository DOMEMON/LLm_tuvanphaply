"""Typed, composable reverse queries over approved administrative field evidence.

No procedure IDs, example questions, fee amounts or expected lists live here.
Unknown and conflicting values stay unknown (three-valued AND evaluation).
"""
import re
from .catalog_query import fold


def bind_catalog_conditions(task, config):
    """Bind finite field-language to the task's literal clause, never other tasks.

    Model owns decomposition. Only named groups and unambiguous finite facets
    are normalized; facts still come exclusively from the approved corpus.
    """
    from app.rag.g7.contracts import CatalogPredicate
    if task.kind != 'catalog':
        return task
    q = fold(task.quote)
    # The supported algebra is conjunction, not arbitrary Boolean/numeric fee
    # reasoning. Refuse a lossy rewrite rather than answering a broader list.
    unsupported = (re.search(r'\bhoac\b',q)
        or re.search(r'\bphi\b.{0,18}(?:tren|duoi|lon hon|nho hon|khong qua|it nhat)\s+\d',q)
        or re.search(r'\d[\d ]*\s*(?:dong|vnd)\b',q)
        or re.search(r'hai long|pho bien|ty le|xac suat|phan tram',q))
    if unsupported:
        task.kind,task.question = 'clarify','G8_FILTER_UNSUPPORTED'
        task.code,task.fields,task.domains,task.candidates,task.predicates = '',[],[],[],[]
        return task
    groups = [g for g in config['groups'] if any(' '+fold(a)+' ' in ' '+q+' ' for a in g['aliases'])]
    if groups:
        task.domains = list(dict.fromkeys(d for g in groups for d in g['domains']))
    output = [p for p in task.predicates if p.field not in {'fees','submission_methods','processing_times'}]
    duration = re.search(r'(khong qua|toi da|it nhat|toi thieu|bang|dung)\s+(\d+)\s+ngay',q)
    if duration:
        op = 'lte' if duration[1] in {'khong qua','toi da'} else 'gte' if duration[1] in {'it nhat','toi thieu'} else 'eq'
        output.append(CatalogPredicate(field='processing_times',operator=op,value=duration[2]))
    elif any(p.field=='processing_times' for p in task.predicates):
        for p in task.predicates:
            if p.field!='processing_times':
                continue
            number = re.fullmatch(r'(\d+)\s*(?:ngay(?: lam viec)?)?',fold(p.value))
            if not number or not re.search(r'\b'+number[1]+r'\s+ngay\b',q):
                task.kind,task.question='clarify','G8_FILTER_UNSUPPORTED'
                task.code,task.fields,task.domains,task.candidates,task.predicates='',[],[],[],[]
                return task
            p.value=number[1]
            output.append(p)
    fee = bool(re.search(r'\bphi\b|dong tien|thu tien|mat tien|ton tien',q))
    if fee:
        negative = bool(re.search(r'mien (?:le )?phi|khong (?:co |can |phai )?(?:dong |thu |mat |ton )?(?:le )?phi|khong (?:mat|ton|dong|thu) tien',q))
        positive = bool(re.search(r'(?<!khong )thu phi|co (?:thu |dong )?(?:le )?phi|can dong phi|phai dong phi|mat phi',q))
        conditional = 'truong hop' in q and negative and positive and ('vua' in q or 'tuy' in q)
        value = 'conditional' if conditional else 'free_original' if negative and 'ban chinh' in q else 'paid_copy' if not negative and 'ban sao' in q else 'free' if negative else 'paid'
        output.append(CatalogPredicate(field='fees',operator='eq',value=value))
    online = bool(re.search(r'online|truc tuyen|qua mang',q))
    offline = 'truc tiep' in q
    if online or offline:
        value = 'both' if online and offline else 'online' if online else 'offline'
        exclusive = bool(re.search(r'\bchi\b|duy nhat',q))
        negated = bool(re.search(r'khong (?:co |the |ho tro |nhan |cho phep |hinh thuc |nop |qua )*(?:online|truc tuyen|truc tiep|mang)',q))
        output.append(CatalogPredicate(field='submission_methods',operator='not_contains' if negated else 'eq' if exclusive else 'contains',value=value))
    # Group membership is not a receiving-authority condition. A location
    # predicate requires a receiving cue and a literal value in this clause.
    for p in output[:]:
        if p.field == 'receiving_authority' and not re.search(r'noi nop|nop tai|nop o|tiep nhan|co quan',q):
            output.remove(p)
        elif p.field in {'receiving_authority','required_documents'} and fold(p.value) not in q:
            raise ValueError('FILTER_VALUE_REQUIRES_LITERAL_TASK_EVIDENCE')
    task.predicates = output
    return task


def fee_class(text):
    raw = text.strip().casefold()
    q = fold(text)
    if q in {'khong', 'khong co', 'khong dong', 'khong thu phi', 'mien phi', 'mien le phi'} or re.fullmatch(r'0+(?:[.,]0+)?(?:\s*(?:đ|đồng|vnd))?', raw):
        return 'free'
    paid = bool(re.search(r'[1-9][\d.,]*\s*(?:đ|đồng|vnd)', raw))
    free = bool(re.search(r'khong thu|mien phi|mien le phi|khong mat phi', q))
    if paid:
        return 'conditional' if free else 'paid'
    return None


def evaluate(text, predicate):
    field, op, value = predicate['field'], predicate['operator'], predicate['value']
    q, wanted = fold(text), fold(value)
    if field == 'fees':
        category = fee_class(text)
        if category is None:
            return None
        if value == 'free_original':
            return category == 'free' or bool(re.search(r'ban chinh\s+(?:khong thu phi|mien phi)', q))
        if value == 'paid_copy':
            return bool(re.search(r'ban sao\s+[1-9][\d ]*\s*(?:d|dong)', q))
        return category in {'paid','conditional'} if value == 'paid' else category == value
    if field == 'submission_methods':
        observed = {'truc tiep': {'offline'}, 'truc tuyen': {'online'}, 'online': {'online'},
                    'ca hai': {'online','offline'}}.get(q)
        if observed is None:
            return None
        target = {'online','offline'} if value == 'both' else {value}
        return observed == target if op == 'eq' else not bool(target & observed) if op == 'not_contains' else target <= observed
    if field == 'processing_times':
        # Only explicit uncomplicated day counts/ranges. Conditions such as
        # "or longer", after-hours, and unspecified schedules need clarification.
        match = re.fullmatch(r'(?:trong\s+)?(?:[a-z]+\s+)?(?:\()?0*(\d+)(?:\))?'
                             r'(?:\s+(?:den\s+)?0*(\d+))?\s+ngay(?: lam viec)?'
                             r'(?:\s+ke tu (?:ngay )?nhan (?:du )?ho so(?: va)?(?: hop le)?)?', q)
        if not match:
            return None
        low = int(match[1])
        high = int(match[2] or low)
        number = float(value)
        return high <= number if op == 'lte' else low >= number if op == 'gte' else low == high == number
    if field in {'required_documents','receiving_authority'}:
        return wanted in q if op == 'contains' else wanted not in q
    return None


def validate_predicate(predicate):
    f, op, v = predicate.field, predicate.operator, predicate.value
    if f == 'fees' and (op != 'eq' or v not in {'paid','free','conditional','free_original','paid_copy'}):
        raise ValueError('INVALID_FEE_PREDICATE')
    if f == 'submission_methods' and (op not in {'eq','contains','not_contains'} or v not in {'online','offline','both'}):
        raise ValueError('INVALID_METHOD_PREDICATE')
    if f == 'processing_times':
        if op not in {'eq','lte','gte'} or not re.fullmatch(r'\d+(?:\.\d+)?',v) or not 0 <= float(v) <= 3650:
            raise ValueError('INVALID_TIME_PREDICATE')
    if f in {'required_documents','receiving_authority'} and (op not in {'contains','not_contains'} or len(v.strip()) < 3):
        raise ValueError('INVALID_TEXT_PREDICATE')


def describe(p):
    f, op, value = p['field'], p['operator'], p['value']
    if f == 'fees':
        return {'paid':'Có khoản thu phí (có thể tùy trường hợp)', 'free':'Không thu phí theo nguồn',
                'conditional':'Có cả trường hợp miễn và thu phí', 'free_original':'Không thu phí bản chính',
                'paid_copy':'Có thu phí bản sao'}[value]
    if f == 'submission_methods':
        name = {'online':'trực tuyến', 'offline':'trực tiếp', 'both':'cả trực tuyến và trực tiếp'}[value]
        return ('Chỉ nhận ' if op == 'eq' else 'Không hỗ trợ ' if op == 'not_contains' else 'Có hỗ trợ ') + name
    if f == 'processing_times':
        return 'Thời gian ghi trong nguồn ' + {'lte':'không quá ', 'gte':'ít nhất ', 'eq':'bằng '}[op] + value + ' ngày'
    return ('Giấy tờ' if f == 'required_documents' else 'Nơi nộp') + (' không chứa ' if op == 'not_contains' else ' có ') + value


async def execute_filtered(data, task, filters, ai, rid, catalog):
    from app.rag.g7.contracts import Task
    from app.rag.g7.workflow import procedure_answer
    predicates = [p.model_dump() for p in task.predicates]
    for p in task.predicates:
        validate_predicate(p)
    matches, unknown = [], []
    for card in catalog:
        if task.domains and not set(card['domains']) & set(task.domains):
            continue
        if task.candidates and card['code'] not in task.candidates:
            continue
        outcomes, supporting, values = [], {}, []
        for p in predicates:
            _, g = await procedure_answer(data, Task(kind='procedure', quote=task.quote,
                code=card['code'], fields=[p['field']]), card, filters, ai, rid)
            rows = [data.fragments[s['fragment_id']]['text'] for s in g['sources'] if s['fragment_id'] in data.fragments]
            results = [evaluate(text, p) for text in rows]
            known = results[0] if results and all(r is not None and r == results[0] for r in results) else None
            outcomes.append(known)
            if known:
                supporting.update({s['fragment_id']:s for s in g['sources']})
                values.extend(rows)
        if False in outcomes:
            continue
        if None in outcomes:
            unknown.append(card['id'])
        else:
            matches.append((card,list(supporting.values()),list(dict.fromkeys(values))))
    lines = [f"{i}. {c['title']} — " + '; '.join(values) for i,(c,_,values) in enumerate(matches,1)]
    answer = 'Điều kiện: ' + '; '.join(describe(p) for p in predicates) + '.\n\n'
    answer += '\n'.join(lines) if lines else 'Chưa tìm thấy thủ tục đáp ứng đầy đủ điều kiện trong nguồn đã duyệt.'
    if any(p['field'] == 'fees' for p in predicates):
        answer += ('\n\nMức phí giữ nguyên điều kiện trong nguồn: bản chính miễn phí và bản sao có phí là hai trường hợp khác nhau. '
                   'Nhóm “không thu phí” chỉ gồm mục có nguồn xác nhận miễn phí toàn bộ; thông tin thiếu không được coi là miễn phí.')
    if any(p['field'] == 'submission_methods' and p['operator'] == 'eq' for p in predicates):
        answer += '\n\nMục ghi “Cả hai” không thuộc nhóm chỉ có một hình thức nộp.'
    if unknown:
        answer += f'\n\nCó {len(unknown)} thủ tục chưa đủ dữ liệu xác nhận điều kiện; chưa đưa vào danh sách.'
    answer += '\n\nKết quả chỉ trong bộ dữ liệu đang phục vụ. Bạn có thể chọn tên hoặc số thứ tự để hỏi tiếp.'
    sources = {s['fragment_id']:s for _,citations,_ in matches for s in citations}
    return {'answer':answer, 'request':{'domains':task.domains, 'typed_predicates':predicates},
            'options':[c['code'] for c,_,_ in matches], 'sources':list(sources.values()),
            'items':[{'procedure_id':c['id'], 'fragment_ids':[s['fragment_id'] for s in citations]} for c,citations,_ in matches],
            'unknown_ids':unknown, 'missing':['catalog_filter_evidence'] if unknown else [],
            'status':'INSUFFICIENT_DATA' if unknown else 'ANSWER'}
