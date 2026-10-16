"""Discriminated query operations: row counts are not counts of non-null values.

Incremental experiment based on v2 development failures, not a blind evaluation.
No table-specific fields, labels, examples, answers or conditions.
"""
import time

from .methods import COMMON, STRING, array, obj
from .table import execute_plan


def query_schema(table, *, status_first=False):
    column = {'type': 'string', 'enum': list(table.types)}
    filters = array(obj({'column': column,
        'operator': {'type': 'string', 'enum': ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'is_null', 'not_null']},
        'value': STRING}), 8)
    common = {'status': {'const': 'answer'}, 'filter_logic': {'enum': ['and', 'or'], 'type': 'string'}, 'filters': filters}
    schema = obj({'tasks': array({'anyOf': [
        obj({'operation': {'const': 'count_rows'}, **common}),
        obj({'operation': {'type': 'string', 'enum': ['sum', 'avg', 'min', 'max', 'count_values']},
             'field': column, **common}),
        obj({'operation': {'type': 'string', 'enum': ['select', 'distinct']}, 'columns': array(column, 32),
             'order_by': {'type': 'string', 'enum': ['', *table.types]},
             'direction': {'type': 'string', 'enum': ['asc', 'desc']},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}, **common}),
        obj({'status': {'type': 'string', 'enum': ['insufficient_data', 'clarify']}, 'reason': STRING}),
    ]}, 8)})
    if status_first:
        # Make availability decidable before the constrained field/operation.
        branches = schema['properties']['tasks']['items']['anyOf']
        for index, branch in enumerate(branches):
            props = branch['properties']
            branches[index] = obj({'status': props['status'], **{k: v for k, v in props.items() if k != 'status'}})
    return schema


def flatten(task):
    operation = task.get('operation', 'select')
    return {'status': task['status'], 'operation': 'count' if operation in ('count_rows', 'count_values') else operation,
            'field': task.get('field', ''), 'columns': task.get('columns', []),
            'filter_logic': task.get('filter_logic', 'and'), 'filters': task.get('filters', []),
            'order_by': task.get('order_by', ''), 'direction': task.get('direction', 'asc'), 'limit': task.get('limit', 1000)}


def run_typed(table, profile, question, model, *, use_profile=False, status_first=False):
    started = time.perf_counter()
    physical = {'table_name': 'data', 'columns': [{'name': '_row_id', 'type': 'BIGINT'},
        *[{'name': c['name'], 'type': c['type']} for c in table.columns]],
        'samples': table.rows[:3], 'missing_values': table.profile['missing_values']}
    payload = {'schema': physical, 'question': question}
    if use_profile:
        payload['semantic_proposal'] = profile
    instruction = COMMON + '''You generate operations, not final answers. The executor has ALL rows.
Answer is possible when the requested fields exist, including entities not present in samples.
Select the smallest operation that directly answers each requested result:
- count_rows counts matching records (including records with null values).
- count_values counts non-null values of a field; use only when that is specifically requested.
- sum/avg/min/max calculate a single metric over the matching records.
- select retrieves fields of matching records. A field value for a named record/date is select, not count or sum.
- distinct lists unique values/combinations, not their count.
No extra fields or order_by in aggregates. Only explicit user criteria become filters.
Missing unit conversions or code-label definitions cannot be manufactured from prior knowledge or semantic proposals.
Returning numeric code values does not answer a request to decode what those codes mean.
To decode a code you need a source column providing its meaning; otherwise insufficient_data.
All literals must retain their source spelling and identifiers keep leading zeros.
Each separate request is one task; preserve their order; support up to 8.
'''
    if status_first:
        instruction += '''First decide availability: if the requested information has NO source column,
return status=insufficient_data. Never substitute an unrelated existing column to satisfy the schema.
For SELECT, return all matching source records (limit=1000) unless a smaller number is explicitly requested.
A name is not necessarily unique. Do not silently pick one row, sort, or discard repeated names.
Group requested fields for the SAME subject/filter in one SELECT task. Separate different subjects or calculations.
'''
    output, timing = model.call(instruction, payload, query_schema(table, status_first=status_first))
    results = []
    for task in output['tasks']:
        try:
            results.append(execute_plan(flatten(task), table))
        except Exception as exc:
            results.append({'status': 'error', 'data': [], 'error': str(exc)[:500]})
    return {'results': results, 'model_output': output, 'model_timing': timing,
            'seconds': round(time.perf_counter() - started, 3)}
