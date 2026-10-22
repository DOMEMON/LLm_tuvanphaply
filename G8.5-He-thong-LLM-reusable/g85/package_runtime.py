"""Serve automatic table packages through the original G8.5 planner and engine."""
import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from .runtime.engine import grounding, run_turn
from .runtime.package_loader import read_package
from .package_builder import ROOT, sha
from .tabular_lab.collection import run_collection


class ModelTransport:
    def __init__(self, model):
        self.model = model

    async def complete_g85(self, messages, schema, stage, max_tokens):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        context = [m for m in messages[:-1] if m['role'] != 'system']
        result, _ = self.model.call(system, {'conversation_context': context,
            'question': messages[-1]['content']}, schema, max_tokens=max_tokens)
        return json.dumps(result, ensure_ascii=False)


def run_package(root, question, model, *, raw_state=None, context=(), review_lookups=False):
    started = time.perf_counter()
    if not question.strip() or len(question) > 6000:
        raise ValueError('QUESTION_LENGTH')
    package = read_package(root, ROOT / 'schema/domain-profile.schema.json')
    # Experimental reviewer is opt-in for offline evaluation only. Live probes
    # still expose qualifier/field misbindings; do not promote it silently.
    package.review_lookups = review_lookups
    if len(context) > 2 or any(c.get('source_sha256') != package.table.hash
            or type(c.get('row_id')) is not int or not 1 <= c['row_id'] <= len(package.table.rows) for c in context):
        package.table.close()
        raise ValueError('INVALID_CONTEXT_SOURCE')
    package.recent_entities = context
    table = package.table
    try:
        async def table_query(task, filters, rid):
            try:
                result = run_collection(table, task.quote, model, context=context,
                                        title_columns=package.mapping['title_columns'])
            except ValueError as exc:
                if str(exc) not in ('MODEL_OUTPUT_TRUNCATED', 'OUTPUT_TRUNCATED'):
                    raise
                result = {'results': [{'request_text': task.quote, 'status': 'clarify',
                    'data': [], 'evidence': [], 'reason': 'Chưa lập được truy vấn đầy đủ. Bạn gửi riêng yêu cầu này nhé.'}]}
            g = grounding(package, rid)
            statuses = [r['status'] for r in result['results']]
            status = 'ANSWER' if all(s == 'answer' for s in statuses) else (
                'NEED_CLARIFICATION' if all(s == 'clarify' for s in statuses) else 'INSUFFICIENT_DATA')
            sources = []
            for r in result['results']:
                ev = r.get('evidence')
                if isinstance(ev, dict):
                    sources.append({'fragment_id': 'query-' + sha(json.dumps(ev, sort_keys=True).encode())[:16],
                        'source_id': 'table-source', 'title': task.quote, 'url': None, 'metadata': ev})
            g.update(status=status, tabular_results=result['results'], sources=sources,
                     resolved_entities=result.get('resolved_entities', []),
                     missing_information=[] if status == 'ANSWER' else ['tabular_request'])
            return '\n\n'.join(r.get('answer_text', r.get('reason', '')) for r in result['results']), g

        package.table_query = table_query
        answer, g, state = asyncio.run(run_turn(question, raw_state, [],
            SimpleNamespace(as_of=None, jurisdiction=None, procedure_id=None), ModelTransport(model), uuid4(), package))
        labels = {a['id']: a['label'] for a in package.profile['attributes']}
        results, recent = [], list(context)
        for part in g['parts']:
            pg = part['grounding']
            if 'tabular_results' in pg:
                results.extend(pg['tabular_results'])
                for item in pg.get('resolved_entities', []):
                    recent = [r for r in recent if r['row_id'] != item['row_id']] + [item]
                continue
            status = {'answered': 'answer', 'clarification_needed': 'clarify',
                      'insufficient_data': 'insufficient_data'}.get(part['result_status'], 'insufficient_data')
            sources = pg.get('sources', [])
            row_ids = sorted({s['metadata']['source_locator']['record_index'] for s in sources
                              if 'source_locator' in s.get('metadata', {})})
            fields = pg.get('field_results', [])
            data = []
            if fields:
                values = {}
                for f in fields:
                    if f['attribute'] not in labels:
                        continue
                    label = labels[f['attribute']]
                    values[label] = (table.rows[row_ids[0] - 1][label]
                        if len(row_ids) == 1 and pg.get('retrieval_mode') == 'direct_source_cell'
                        else f['value'])
                if any(v is not None for v in values.values()):
                    data = [values]
                    if status == 'insufficient_data':
                        status = 'partial'
            elif sources and status == 'answer':
                data = [{'Thông tin': part['answer']}]
            evidence = {'source_sha256': table.hash, 'package_version': package.version,
                'matching_rows': len(row_ids), 'columns': list(data[0]) if data else [],
                'source_rows_preview': table.provenance(row_ids[:20], list(data[0]) if data else []),
                'lineage_preview_truncated': len(row_ids) > 20, 'source_cells': sources,
                'retrieval_mode': pg.get('retrieval_mode')}
            results.append({'request_text': part['title'], 'status': status, 'data': data,
                            'reason': part['answer'] if not data or status != 'answer' else '',
                            'evidence': evidence})
            if len(row_ids) == 1 and data:
                index = row_ids[0]
                title_columns = package.mapping['title_columns']
                identified = [{'column': h, 'operator': 'eq', 'value': str(table.rows[index - 1][h])}
                              for h in title_columns if table.rows[index - 1][h] is not None]
                recent = [r for r in recent if r['row_id'] != index] + [
                    {'row_id': index, 'source_sha256': table.hash, 'identified_by': identified,
                     'last_request': question}]
        if len(results) > 8:
            results = [{'request_text': question, 'status': 'clarify', 'data': [], 'evidence': [],
                        'reason': 'Một tin nhắn hỗ trợ tối đa 8 ý. Bạn chia nhỏ yêu cầu nhé.'}]
            state.active, state.focused, state.displayed_options = [], [], []
            recent = list(context)
        # Persist only the most recent two subjects, including ordinal context.
        state.active = state.active[-2:]
        state.focused = state.focused[-2:]
        state.displayed_options = state.displayed_options[-2:]
        return {'results': results, 'runtime_state': state.model_dump(), 'recent_entities': recent[-2:],
                'seconds': round(time.perf_counter() - started, 3), 'package_version': package.version,
                'engine': 'g85-package', 'grounding': g}
    finally:
        table.close()
