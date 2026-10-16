import pytest

from .reusable import run_reusable, validate_routing, subject_candidates
from .semantic_filter import execute_semantic
from .table import Table
from .test_table import table


def request(parts, **changes):
    return {'parts': parts, 'kind': 'structured', 'condition': '', 'source_columns': [],
            'display_columns': [], 'semantic_operation': 'list', **changes}


def test_shared_field_list_becomes_independent_record_queries(table):
    class Model:
        def call(self, system, payload, schema, **kwargs):
            if 'operation' in schema['properties']:
                return {'operation': 'lookup'}, {}
            if 'subjects' in schema['properties']:
                return {'subjects': ['001', '002', '003']}, {}
            if 'candidate' in schema['properties']:
                key = next(k for k, c in payload['candidates'].items() if c['quote'] == payload['question'] and c['column'] == 'code')
                return {'candidate': key}, {}
            return {key: ['category'] for key in schema['properties']}, {}
    result = run_reusable(table, 'category of code 001, 002 and 003', Model())
    assert [r['data'] for r in result['results']] == [[{'category': 'A'}], [{'category': 'B'}], [{'category': 'A'}]]
    assert [e['row_id'] for e in result['recent_entities']] == [2, 3]
    assert all(len(r['evidence']['params']) == 1 for r in result['results'])


def test_routing_cannot_invent_a_subject_or_condition(table):
    q = 'category of 001'
    task = {'request_text': q, 'mode': 'lookup', 'subject': '999', 'subject_column': 'code',
            'output_columns': ['category'], 'source_columns': []}
    with pytest.raises(ValueError, match='SUBJECT_NOT'):
        validate_routing({'overflow': False, 'tasks': [task]}, q, table, subject_candidates(table, q))
    task['request_text'] = 'high price'
    with pytest.raises(ValueError, match='REQUEST_NOT'):
        validate_routing({'overflow': False, 'tasks': [task]}, q, table, [])


def test_unreliable_semantic_operation_is_not_enabled_by_default(table):
    class Model:
        def call(self, *args, **kwargs):
            return {'operation': 'semantic_filter'}, {}
    response = run_reusable(table, 'Which records are inexpensive?', Model())
    assert response['capability_gate'] == 'semantic_filter_experimental'
    assert response['results'][0]['status'] == 'clarify'
    assert response['results'][0]['data'] == []


def test_invented_subject_returns_clarification_without_changing_context(table):
    class Model:
        def call(self, system, payload, schema, **kwargs):
            return ({'operation': 'lookup'} if 'operation' in schema['properties'] else {'subjects': ['invented']}), {}
    result = run_reusable(table, 'category of 001', Model())
    assert result['validation_error'] == 'SUBJECT_NOT_IN_QUESTION'
    assert result['results'][0]['status'] == 'clarify'
    assert result['recent_entities'] == []


@pytest.fixture
def travel(tmp_path):
    p = tmp_path / 'travel.csv'
    p.write_text('name,access\nA,Wheelchair accessible\nB,Not wheelchair accessible\n'
                 'C,Wheelchair accessible with prior booking\nD,\nE,Wheelchair accessible\n', encoding='utf-8')
    t = Table(p)
    yield t
    t.close()


class SemanticModel:
    def call(self, system, payload, schema, **kwargs):
        result = {}
        for key, cells in payload['source_records'].items():
            value = cells['access']
            verdict = ('non_match' if value.startswith('Not') else
                       'conditional' if 'booking' in value else 'match')
            result[key] = {'verdict': verdict, 'quotes': [{'column': 'access', 'text': value}]}
        return result, {}


def semantic_request(**changes):
    return request(['wheelchair accessible'], kind='semantic_filter', condition='wheelchair accessible',
                   source_columns=['access'], display_columns=['name'], **changes)


def test_semantic_filter_covers_all_rows_and_preserves_conditions(travel):
    result = execute_semantic(travel, semantic_request(), SemanticModel())
    assert result['status'] == 'partial'
    assert result['evidence']['classification_counts'] == {'match': 2, 'non_match': 1, 'conditional': 1, 'unknown': 1}
    assert {r['name'] for r in result['data']} == {'A', 'C', 'E'}
    assert result['evidence']['evaluated_rows'] == 5
    assert result['evidence']['evaluated_distinct_values'] == 4
    assert result['semantic_groups'][1]['data'][0]['name'] == 'C'
    assert len(result['evidence']['semantic_decisions']) == 5


def test_semantic_count_does_not_count_conditional_or_unknown_as_certain(travel):
    result = execute_semantic(travel, semantic_request(semantic_operation='count'), SemanticModel())
    assert result['data'] == [{'Phù hợp': 2, 'Có điều kiện': 1, 'Chưa đủ thông tin': 1}]


def test_invented_supporting_quotes_are_not_accepted(travel):
    class Bad(SemanticModel):
        def call(self, *args, **kwargs):
            result, timing = super().call(*args, **kwargs)
            for decision in result.values():
                decision['verdict'] = 'match'
                decision['quotes'][0]['text'] = 'invented evidence'
            return result, timing
    result = execute_semantic(travel, semantic_request(), Bad())
    assert result['evidence']['classification_counts']['unknown'] == 5
    assert result['data'] == []


def test_no_top_k_semantic_filter_when_full_scan_exceeds_budget(tmp_path):
    p = tmp_path / 'large.csv'
    p.write_text('name,access\n' + '\n'.join(f'N{i},Description {i}' for i in range(129)), encoding='utf-8')
    t = Table(p)
    try:
        assert execute_semantic(t, semantic_request(), SemanticModel())['status'] == 'clarify'
    finally:
        t.close()
