"""Literal request contracts derived from the loaded table, without domain facts.

Only closed-form operations, explicit source columns/identifiers and verified
conversation references bypass the semantic planner. Everything else stays with it.
"""
import json
import re
import unicodedata
from collections import Counter

from .contracts import Plan, Task


def folded(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn').replace('đ', 'd')


def request_spans(query):
    """Split explicit request boundaries outside quoted source names/values."""
    mask = list(query)
    for value, left, right in quoted(query):
        mask[left:right] = '#' * (right - left)
    visible = ''.join(mask)
    boundaries = [(m.start(), m.end()) for m in re.finditer(
        r'[;?\n]+|,?\s*\b(?:với lại|đồng thời|còn|with that|also)\b\s*', visible, re.I)]
    spans, start = [], 0
    for left, right in boundaries:
        if re.match(r'(?:chi (?:dung|su dung|lay thong tin tu)|only (?:use|from)|neu khong co|if (?:missing|unavailable))\b',
                    folded(visible[right:].lstrip())):
            continue
        span = query[start:left].strip(' ,;\n')
        if span:
            spans.append(span)
        start = right
    tail = query[start:].strip(' ,;\n')
    if tail:
        spans.append(tail)
    return spans or [query]


def explicit_rows(query):
    return [int(m.group(1)) for m in re.finditer(
        r'\b(?:ban ghi(?: so)?|dong(?: so)?|record(?: number)?|row(?: number)?)\s*#?\s*(\d+)\b', folded(query))]


def quoted(text):
    results, stack, start = [], [], None
    pairs = {'"': '"', '“': '”', '‘': '’', '`': '`'}
    for index, char in enumerate(text):
        if (len(text[:index]) - len(text[:index].rstrip('\\'))) % 2:
            continue
        if not stack and char in pairs:
            start, stack = index, [pairs[char]]
        elif stack:
            if char == stack[-1]:
                stack.pop()
                if not stack:
                    results.append((text[start + 1:index], start, index + 1))
            elif char in ('“', '‘'):
                stack.append(pairs[char])
    return results


def literal_wording(question):
    """Unknown qualifiers must go to the semantic path, never be discarded."""
    visible = question
    for value, _, _ in quoted(question):
        visible = visible.replace(value, ' ' * len(value))
    text = folded(visible)
    text = text.replace('khong lay cot khac', '')
    vocabulary = set(('cho toi tra cuu trong bang cua ban ghi so co gia tri nao la gi '
                      'liet ke rieng cot nhung dong bang list columns where '
                      'muon biet rieng va tach moi cot theo thu tu tra tung yeu cau '
                      'ca hai vua hoi roi bay gio doi sang con the bao nhieu '
                      'record row number value values of what is are show give me '
                      'for the and both previous records equals equal').split())
    return set(re.findall(r'[^\W\d_]+', text)) <= vocabulary


def shared_record_questions(question):
    """Expand a shared projection only at explicit, unquoted record boundaries."""
    mask = list(question)
    for _, left, right in quoted(question):
        mask[left:right] = '#' * (right - left)
    visible = ''.join(mask)
    boundaries = list(re.finditer(
        r'\s+(?:và|and)\s+(?=(?:bản ghi có|record with)\b)', visible, re.I))
    first = re.search(r'\b(?:bản ghi có|record with)\b', visible, re.I)
    if not boundaries or first is None:
        return []
    prefix, start, parts = question[:first.start()], first.start(), []
    for boundary in boundaries:
        parts.append(prefix + question[start:boundary.start()])
        start = boundary.end()
    parts.append(prefix + question[start:])
    return parts


def exact_label_columns(table, title_columns):
    # A package may use a machine ID as its title. Short text cells can also
    # identify a record, but only through exact source matching, never similarity.
    # All matches are retained so repeated/ambiguous labels cannot pick one row.
    return list(dict.fromkeys([*title_columns, *(h for h in table.headers
        if table.types[h] == 'VARCHAR' and all(
            row[h] is None or len(str(row[h])) <= 160 for row in table.rows))]))


def missing_contact(query, table):
    # Contact concepts are language/type vocabulary, not a dataset-specific rule.
    # Names inside quotes may contain these words without requesting that field.
    visible = query
    for value, _, _ in quoted(query):
        if (not any(folded(value) == folded(h) for h in table.headers) and any(
                r[h] is not None and folded(str(r[h])) == folded(value)
                for r in table.rows for h in table.headers)):
            visible = visible.replace(value, ' ' * len(value))
    text = folded(visible)
    concepts = {'phone': ('so dien thoai', 'phone number', 'telephone number'),
                'email': ('dia chi email', 'email address')}
    for key, phrases in concepts.items():
        asked = next((p for p in phrases if p in text), None)
        if not asked:
            continue
        prefix = text[:text.index(asked)].rstrip()
        if re.search(r'\b(?:khong|not|without)(?:\s+\w+){0,3}$', prefix):
            continue
        headers = re.sub(r'[_/.-]+', ' ', ' '.join(folded(h) for h in table.headers))
        supported = (bool(re.search(r'\b(?:dien thoai|phone|telephone|tel|sdt)\b', headers))
                     if key == 'phone' else any(p in headers for p in ('email', 'e mail')))
        if not supported:
            return asked
    return None


def source_variants(task, package):
    """Find a strictly more specific source title, never invent a qualifier.

    A correction requires a shared multi-word subject and additional literal
    title terms in the same request. Field names cannot act as qualifiers.
    """
    if not package.mapping['title_columns'] or task.entity not in package.catalog:
        return []
    if not hasattr(package, '_variant_terms'):
        header = package.mapping['title_columns'][0]
        terms = {f'row_{i}': set(re.findall(r'\w+', unicodedata.normalize('NFC', str(row[header] or '')).casefold()))
                 for i, row in enumerate(package.table.rows, 1)}
        counts = Counter(word for words in terms.values() for word in words)
        common = {w for w, n in counts.items() if len(terms) >= 8 and n / len(terms) >= .15}
        package._variant_terms = {k: words - common for k, words in terms.items()}
    terms = package._variant_terms
    requested = set(re.findall(r'\w+', unicodedata.normalize('NFC', task.quote).casefold()))
    field_words = {w for f in package.mapping['fields'] if f['id'] in task.attributes
                   for w in re.findall(r'\w+', f['column'].casefold())}
    requested -= field_words
    selected = terms.get(task.entity, set()) & requested
    if len(selected) < 2:
        return []
    better = {key: words & requested for key, words in terms.items()
              if key in package.catalog and selected < (words & requested)}
    if not better:
        return []
    size = max(map(len, better.values()))
    return [key for key, words in better.items() if len(words) == size]


def literal_query(table, question, title_columns=(), context=()):
    """Return a typed executable plan only when all bindings are explicit."""
    if shared_record_questions(question):
        return None  # Separate records must not become AND predicates on one row.
    title_columns = exact_label_columns(table, title_columns)
    q = folded(question.strip().rstrip('.?!'))
    q = re.sub(r'^tra rieng tung yeu cau theo thu tu:\s*', '', q)
    syntax = list(question)
    for _, left, right in quoted(question):
        syntax[left:right] = ' ' * (right - left)
    syntax = folded(''.join(syntax))
    base = {'status': 'answer', 'operation': 'select', 'field': '', 'columns': [],
            'filters': [], 'filter_logic': 'and', 'order_by': '', 'direction': 'asc', 'limit': 1000}
    counts = (
        r'(?:trong )?bang(?: nay)? co (?:tong cong )?bao nhieu dong(?: du lieu)?(?:,? khong tinh ten cot)?',
        r'dem (?:tong )?(?:so )?dong(?: du lieu)?(?: trong bang)?',
        r'count (?:all )?(?:the )?(?:rows|records)',
    )
    if any(re.fullmatch(pattern, q) for pattern in counts):
        return {**base, 'operation': 'count'}
    names = {folded(h): h for h in table.headers}
    mentions = [(names[folded(value)], start, end) for value, start, end in quoted(question) if folded(value) in names]
    # Conservative support for explicit source-column comparisons. No prose
    # interpretation, implicit codebook, arbitrary SQL or OR inference is allowed.
    conditions, consumed = [], set()
    for column, start, end in mentions:
        match = re.match(r'\s*(>=|<=|!=|=|>|<|bằng|equals?)\s*', question[end:], re.I)
        if match:
            value_start = end + match.end()
            quoted_value = next((item for item in quoted(question) if item[1] == value_start), None)
            if quoted_value:
                value, left, value_end = quoted_value
                # JSON-rendered literals can contain escaped backslashes/quotes.
                # Decode only when the decoded value is independently present in
                # this source column. Never normalize arbitrary source strings.
                if question[left] == '"':
                    try:
                        decoded = json.loads(question[left:value_end])
                    except ValueError:
                        decoded = value
                    if decoded != value:
                        raw_match = any(row[column] == value for row in table.rows)
                        decoded_match = any(row[column] == decoded for row in table.rows)
                        if raw_match and decoded_match:
                            return None
                        if decoded_match:
                            value = decoded
            else:
                number = re.match(r'-?\d+(?:\.\d+)?', question[value_start:])
                if not number:
                    return None
                value, value_end = number.group(), value_start + number.end()
            operator = {'=': 'eq', 'bằng': 'eq', 'equal': 'eq', 'equals': 'eq',
                        '!=': 'ne', '>': 'gt', '<': 'lt', '>=': 'gte', '<=': 'lte'}[match.group(1).casefold()]
            conditions.append({'column': column, 'operator': operator, 'value': value})
            consumed.update(s for _, s, _ in mentions if start <= s < value_end)
    if conditions and re.search(r'\b(?:hoac|or|ngoai tru|except)\b', syntax):
        return None
    output = list(dict.fromkeys(column for column, start, end in mentions if start not in consumed))
    if not output:
        return None
    # A negative projection needs semantic planning unless it is merely the
    # standard suffix "không lấy cột khác" after the requested columns.
    prefix = folded(question[:min(start for column, start, end in mentions if start not in consumed)])
    if re.search(r'\b(?:khong|not|without)\b', prefix):
        return None
    aggregate = re.fullmatch(
        r'(?:tinh |cho toi |cho biet )?(?:gia tri )?(trung binh|tong|lon nhat|nho nhat|average|sum|maximum|minimum)'
        r'(?: cua)?(?: cot)?\s*["“`]([^"”`]+)["”`](?: la bao nhieu)?(?: tren toan bang)?(?:,? bo qua o thieu)?', q)
    if aggregate and len(output) == 1 and not conditions:
        op = {'trung binh': 'avg', 'tong': 'sum', 'lon nhat': 'max', 'nho nhat': 'min',
              'average': 'avg', 'sum': 'sum', 'maximum': 'max', 'minimum': 'min'}[aggregate.group(1)]
        if op in ('sum', 'avg') and table.types[output[0]] not in ('BIGINT', 'DOUBLE'):
            return None
        return {**base, 'operation': op, 'field': output[0]}
    listing = re.fullmatch(r'(?:liet ke|hien thi|list|show)\s+(.+?)\s+(?:trong bang|in (?:the )?table)', q)
    if listing:
        remainder = listing.group(1)
        remainder = re.sub(r'["“`]([^"”`]+)["”`]', '', remainder)
        if not re.sub(r'\b(?:va|and)\b|[,\s]', '', remainder):
            return {**base, 'columns': output}
    if re.search(r'\b(?:trung binh|tong|lon nhat|nho nhat|dem|bao nhieu dong|average|sum|count|maximum|minimum)\b', syntax):
        return None
    if not literal_wording(question):
        return None
    rows = explicit_rows(question)
    if rows:
        if len(set(rows)) != 1:
            return None
        return {**base, 'columns': output, 'filters': [{'column': '_row_id', 'operator': 'eq', 'value': str(rows[0])}]}
    if conditions:
        return {**base, 'columns': output, 'filters': conditions}
    # Exact quoted labels/IDs are checked against the full source, never top-k.
    labels = [value for value, start, end in quoted(question) if folded(value) not in names]
    ids = []
    for value in labels:
        matching = [i for i, row in enumerate(table.rows, 1) if any(
            row[h] is not None and folded(str(row[h])) == folded(value) for h in title_columns)]
        ids.extend(matching)
    ids = list(dict.fromkeys(ids))
    if len(ids) == 1:
        return {**base, 'columns': output, 'filters': [{'column': '_row_id', 'operator': 'eq', 'value': str(ids[0])}]}
    if len(labels) == 1 and len(ids) > 1:
        identity = [h for h in title_columns if all(table.rows[i - 1][h] is not None
                    and folded(str(table.rows[i - 1][h])) == folded(labels[0]) for i in ids)]
        if len(identity) == 1:
            # Duplicate labels are one lookup returning several physical rows,
            # not several additional user intents.
            return {**base, 'columns': output, 'filters': [
                {'column': identity[0], 'operator': 'eq', 'value': str(table.rows[ids[0] - 1][identity[0]])}]}
    if context and re.search(r'\b(?:vua roi|vua hoi|ban ghi do|that record|previous record)\b', q):
        if re.search(r'\b(?:ca hai|both)\b', q):
            return None  # Preflight expands one source-bound task per referent.
        return {**base, 'columns': output, 'filters': [{'column': '_row_id', 'operator': 'eq', 'value': str(context[-1]['row_id'])}]}
    return None


def literal_preflight(query, state, package):
    """Construct plans from verified source identifiers and exact headers."""
    if not getattr(package, 'table', None):
        return None
    spans = request_spans(query)
    if len(spans) > 8:
        return Plan(relation='replace', tasks=[Task(kind='clarify', quote=query,
            question='Một tin nhắn hỗ trợ tối đa 8 intent. Bạn chia nhỏ yêu cầu nhé.')])
    tasks = []
    table = package.table
    context = getattr(package, 'recent_entities', ())
    labels = {f['column']: f['id'] for f in package.mapping['fields']}
    for span in spans:
        if missing_contact(span, table):
            tasks.append(Task(kind='answer', mode='table', quote=span))
            continue
        shared = shared_record_questions(span)
        if shared:
            from ..tabular_lab.table import compile_plan
            if len(tasks) + len(shared) > 8:
                return None
            for part in shared:
                bound = literal_query(table, part, package.mapping['title_columns'], context)
                if (bound is None or bound['operation'] != 'select'
                        or not bound['filters'] or len(bound['columns']) > 12):
                    return None
                try:
                    compiled = compile_plan(bound, table)
                    rows = table.sql(compiled[2] + ' LIMIT 2', compiled[1])
                except ValueError:
                    return None
                if len(rows) != 1:
                    return None  # Let the semantic path resolve missing/ambiguous subjects.
                tasks.append(Task(kind='answer', quote=span, entity=f'row_{rows[0]["_row_id"]}',
                                  attributes=[labels[h] for h in bound['columns']]))
            continue
        plan = literal_query(table, span, package.mapping['title_columns'], context)
        if plan is None and literal_wording(span):
            fields = list(dict.fromkeys(h for value, _, _ in quoted(span)
                                       for h in table.headers if folded(value) == folded(h)))
            ids = explicit_rows(span)
            if len(ids) < 2:
                subject_values = [value for value, _, _ in quoted(span)
                                  if not any(folded(value) == folded(h) for h in table.headers)]
                ids = ([i for value in subject_values for i, row in enumerate(table.rows, 1)
                        if any(row[h] is not None and folded(str(row[h])) == folded(value)
                               for h in exact_label_columns(table, package.mapping['title_columns']))] if len(subject_values) > 1 else [])
            if len(fields) == 1 and 1 < len(ids) <= 8 and all(1 <= i <= len(table.rows) for i in ids):
                tasks.extend(Task(kind='answer', quote=span, entity=f'row_{i}',
                                  attributes=[labels[fields[0]]]) for i in ids)
                continue
        # An unambiguous "both" reference keeps source row identities.
        if plan is None and literal_wording(span) and len(context) == 2 and re.search(r'\b(?:ca hai|both)\b', folded(span)):
            fields = [names for value, _, _ in quoted(span) for names in table.headers if folded(names) == folded(value)]
            if fields and len(fields) <= 12 and re.search(r'\b(?:vua hoi|vua roi|previous|records)\b', folded(span)):
                tasks.extend(Task(kind='answer', quote=span, entity=f'row_{c["row_id"]}',
                                  attributes=[labels[h] for h in fields]) for c in context)
                continue
        if plan is None:
            return None
        filters = plan['filters']
        if plan['operation'] == 'select' and len(plan['columns']) <= 12 and len(filters) == 1 and filters[0]['column'] == '_row_id':
            row_id = int(filters[0]['value'])
            if 1 <= row_id <= len(table.rows):
                tasks.append(Task(kind='answer', quote=span, entity=f'row_{row_id}',
                                  attributes=[labels[h] for h in plan['columns']]))
                continue
        # Resolve unique exact literal predicates through the executor, not LLM.
        if plan['operation'] == 'select' and len(plan['columns']) <= 12 and filters:
            from ..tabular_lab.table import compile_plan
            try:
                compiled = compile_plan(plan, table)
                ids = table.sql(compiled[2] + ' LIMIT 2', compiled[1])
            except ValueError:
                return None
            if len(ids) == 1:
                tasks.append(Task(kind='answer', quote=span, entity=f'row_{ids[0]["_row_id"]}',
                                  attributes=[labels[h] for h in plan['columns']]))
                continue
        tasks.append(Task(kind='answer', mode='table', quote=span))
    if len(tasks) > 8:
        return None
    relation = 'continue' if context and re.search(r'\b(?:vua roi|vua hoi|previous record)\b', folded(query)) else 'replace'
    return Plan(relation=relation, tasks=tasks)


def execute_literal(table, question, plan):
    from ..tabular_lab.table import execute_plan
    result = execute_plan(plan, table)
    ev = result['evidence']
    ids = [r['_row_id'] for r in table.sql(ev['selection_query'] + ' LIMIT 20', ev['params'])]
    ev['source_rows_preview'] = table.provenance(ids, ev['columns'])
    ev['lineage_preview_truncated'] = ev['matching_rows'] > len(ids)
    if not result['data'] or (plan['operation'] in ('sum', 'avg', 'min', 'max') and result['data'][0]['result'] is None):
        result.update(status='insufficient_data', data=[], reason='Không có giá trị nguồn phù hợp để trả lời yêu cầu này.')
    elif plan['operation'] in ('select', 'distinct'):
        if all(v is None for r in result['data'] for v in r.values()):
            result.update(status='insufficient_data', data=[], reason='Các trường được hỏi chưa có giá trị trong nguồn.')
        elif ev['matching_rows'] > len(result['data']):
            result.update(status='partial', reason='Kết quả vượt giới hạn 1.000 dòng; hãy thu hẹp điều kiện.')
        elif any(v is None for r in result['data'] for v in r.values()):
            result.update(status='partial', reason='Một số trường được hỏi chưa có giá trị trong nguồn.')
    result['request_text'] = question
    return {'results': [result]}
