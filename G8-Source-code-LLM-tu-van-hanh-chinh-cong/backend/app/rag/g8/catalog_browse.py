"""Administrative adapter for the shared, evidence-backed catalog evaluator."""
import json
import re
from pathlib import Path

from .catalog_query import fold, parse_catalog, filter_catalog, render_catalog

CONFIG = json.loads(Path(__file__).with_name('catalog-config.json').read_text(encoding='utf-8'))


def request_for(query, state):
    previous = getattr(state, 'catalog_context', {})
    # Explain a value in the last method-filter menu, not an old procedure.
    # Only a standalone definition question qualifies; compound requests stay
    # with the planner. The definition comes from the adapter's finite mapping.
    if (any(p.get('field') == 'submission_methods' for p in previous.get('predicates', []))
            and re.fullmatch(r'(?:the |vay )?ca hai (?:la sao|la gi|nghia la gi|nghia la sao)', fold(query))):
        return dict(previous, explain_value='cả hai')
    # The extra word "việc" does not change exclusive method semantics.
    normalized = re.sub(r'làm\s+việc\s+(trực tiếp|trực tuyến)', r'làm \1', query, flags=re.IGNORECASE)
    return parse_catalog(normalized, CONFIG, previous)


async def execute_catalog(data, task, state, filters, ai, rid, catalog):
    if task.predicates:
        from .mentor_catalog import execute_filtered
        within = re.search(r'trong (?:so|nhom|danh sach) (?:do|tren)|cac muc tren',fold(task.quote))
        if within and not state.catalog_context:
            return {'answer':'Bạn muốn lọc trong danh sách nào? Mình chưa có một danh sách trước đó đủ rõ để đối chiếu.',
                'request':{},'options':[],'sources':[],'items':[],
                'missing':['catalog_reference'],'status':'NEED_CLARIFICATION'}
        # Intersect server-owned previous result IDs, never model-guessed IDs.
        pool = [c for c in catalog if c['code'] in state.displayed_options] if within else catalog
        result = await execute_filtered(data, task, filters, ai, rid, pool)
        if within:
            result['answer'] = 'Lọc tiếp trong danh sách vừa trả lời.\n\n' + result['answer']
            result['request']['within_previous'] = True
        return result
    from app.rag.g7.contracts import Task
    from app.rag.g7.workflow import procedure_answer
    request = request_for(task.quote, state)
    if request is None:
        return None
    candidates = [dict(c, key=c['code']) for c in catalog]
    if request.get('explain_value'):
        for card in candidates:
            if not set(card['domains']) & set(request['domains']):
                continue
            _, evidence = await procedure_answer(data, Task(kind='procedure', quote=task.quote,
                code=card['code'], fields=['submission_methods']), card, filters, ai, rid)
            sources = [s for s in evidence['sources'] if s['fragment_id'] in data.fragments
                and fold(data.fragments[s['fragment_id']]['text']) == fold(request['explain_value'])]
            if sources:
                context = {k: v for k, v in request.items() if k != 'explain_value'}
                return {'answer': '“Cả hai” ở cột hình thức nộp nghĩa là hỗ trợ cả trực tuyến (online) và trực tiếp. '
                    'Không có nghĩa là bắt buộc nộp hai lần. Mục ghi “Cả hai” không thuộc nhóm chỉ làm trực tiếp.',
                    'request': context, 'options': list(state.displayed_options), 'sources': sources,
                    'items': [{'procedure_id': card['id'], 'fragment_ids': [s['fragment_id'] for s in sources]}],
                    'missing': [], 'status': 'ANSWER'}
        return None
    facts = {}
    for card in candidates:
        if not set(card['domains']) & set(request['domains']):
            continue
        for predicate in request['predicates']:
            field = predicate['field']
            _, evidence = await procedure_answer(data, Task(kind='procedure', quote=task.quote,
                code=card['code'], fields=[field]), card, filters, ai, rid)
            facts[(card['key'], field)] = [(data.fragments[s['fragment_id']]['text'], s)
                for s in evidence['sources'] if s['fragment_id'] in data.fragments]
    matches, unknown = filter_catalog(request, candidates, facts, CONFIG)
    sources = list({s['fragment_id']: s for r in matches for s in r['sources']}.values())
    return {'answer': render_catalog(matches, unknown, request), 'request': request,
        'options': [r['card']['code'] for r in matches], 'sources': sources,
        'items': [{'procedure_id': r['card']['id'], 'fragment_ids': [s['fragment_id'] for s in r['sources']]} for r in matches],
        'missing': ['catalog_filter_evidence'] if unknown else [],
        'status': 'INSUFFICIENT_DATA' if unknown else 'ANSWER'}
