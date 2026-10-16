from copy import deepcopy

import pytest

from .grounded import catalog, checked_plan, run_grounded, uncovered_requests, review_projection, repair_field_quotes, repair_request_spans, expand_shared_requests
from .table import Table, execute_plan
from .test_table import table


def task(**changes):
    return {'request_text': 'amount của code 003', 'status': 'answer', 'reason': '', 'operation': 'select',
            'bindings': [{'requested_text': 'amount', 'column': 'amount'}], 'filter_logic': 'and',
            'filters': [{'column': 'code', 'operator': 'eq', 'value': '003'}], **changes}


def test_catalog_searches_whole_column_and_preserves_code(table):
    info = catalog(table, 'amount của code 003')
    code = next(c for c in info['columns'] if c['name'] == 'code')
    assert code['mentioned_values_found_in_full_table'] == [{'value': '003', 'matching_records': 1}]


def test_added_closing_punctuation_can_be_restored_without_changing_query(table):
    q = 'amount của code 003 (code = 003'
    item = task(request_text=q + ')')
    output = {'tasks': [item]}
    assert repair_request_spans(output, q)
    assert item['request_text'] == q
    assert execute_plan(checked_plan(item, table, q), table)['data'] == [{'amount': 30}]


@pytest.mark.parametrize('original, invented', [
    ('amount code 003', 'amount code 002)'),
    ('amount is not 3', 'amount is 3)'),
    ('amount > 3', 'amount < 3)'),
    ('amount code 0030', 'amount code 003)'),
    ('amount code 003', 'amount code 003 extra)'),
])
def test_punctuation_repair_never_changes_meaning(original, invented):
    output = {'tasks': [task(request_text=invented)]}
    assert repair_request_spans(output, original) == []
    assert output['tasks'][0]['request_text'] == invented


def test_partial_labels_are_source_backed_and_keep_qualifiers(tmp_path):
    path = tmp_path / 'visits.csv'
    path.write_text('name,price\nRiver Museum,8\nRiver Museum Evening Tour,12\nCliff Walk,0\n', encoding='utf-8')
    t = Table(path)
    try:
        metadata = catalog(t, 'price for visiting River Museum Evening Tour')
        name = next(c for c in metadata['columns'] if c['name'] == 'name')
        hints = name['partial_label_candidates']
        assert hints[0]['question_phrase'] == 'river museum evening tour'
        assert hints[0]['source_values'] == ['River Museum Evening Tour']
        assert hints[0]['matching_records'] == 1
        assert catalog(t, 'price for invented place')['source_sha256'] == t.hash
    finally:
        t.close()


def test_category_case_variation_resolves_to_unique_source_value(table):
    item = task(request_text='amount where category is a', filters=[{'column': 'category', 'operator': 'eq', 'value': 'a'}])
    plan = checked_plan(item, table, item['request_text'])
    assert plan['filters'][0]['value'] == 'A'
    assert item['filters'][0]['value'] == 'a'
    assert execute_plan(plan, table)['data'] == [{'amount': 10}, {'amount': 30}]


def test_case_normalization_does_not_merge_distinct_identifiers(tmp_path):
    path = tmp_path / 'ids.csv'
    path.write_text('code,amount\nAb,10\naB,20\n', encoding='utf-8')
    t = Table(path)
    try:
        item = task(request_text='amount code AB', filters=[{'column': 'code', 'operator': 'eq', 'value': 'AB'}])
        assert checked_plan(item, t, item['request_text'])['filters'][0]['value'] == 'AB'
    finally:
        t.close()


def test_shared_request_expansion_preserves_fields_and_other_conditions(table):
    items = [task(request_text='amount code 003 and 001 in category A', filters=[
        {'column': 'code', 'operator': 'eq', 'value': '001'},
        {'column': 'code', 'operator': 'eq', 'value': '003'},
        {'column': 'category', 'operator': 'eq', 'value': 'A'}])]
    class Yes:
        def call(self, *args, **kwargs):
            return {'independent_lookups': True}, {}
    assert expand_shared_requests(items, Yes())
    assert len(items) == 2
    assert [execute_plan(checked_plan(t, table, t['request_text']), table)['data'] for t in items] == [
        [{'amount': 30}], [{'amount': 10}]]
    assert all(t['bindings'] == [{'requested_text': 'amount', 'column': 'amount'}] for t in items)


def test_expansion_requires_semantic_confirmation():
    item = task(request_text='code 001 and 003 must identify the same record', filters=[
        {'column': 'code', 'operator': 'eq', 'value': '001'}, {'column': 'code', 'operator': 'eq', 'value': '003'}])
    class No:
        def call(self, *args, **kwargs):
            return {'independent_lookups': False}, {}
    tasks = [deepcopy(item)]
    assert expand_shared_requests(tasks, No()) == []
    assert tasks == [item]


def test_adjacent_fragments_of_one_name_are_never_split():
    item = task(request_text='price for New York City', bindings=[{'requested_text':'price','column':'amount'}], filters=[
        {'column':'code','operator':'contains','value':'New York'},
        {'column':'code','operator':'contains','value':'City'}])
    class NoCall:
        def call(self, *args, **kwargs):
            raise AssertionError('Compound names must not be offered as independent subjects')
    items = [deepcopy(item)]
    assert expand_shared_requests(items, NoCall()) == []
    assert items == [item]


def test_mixed_fields_remain_associated_with_their_own_subject(table):
    items = [task(request_text='amount code 003 and category code 002', bindings=[
        {'requested_text': 'amount code 003', 'column': 'amount'},
        {'requested_text': 'category code 002', 'column': 'category'}], filters=[
        {'column': 'code', 'operator': 'contains', 'value': '003'},
        {'column': 'code', 'operator': 'contains', 'value': '002'}])]
    class Yes:
        def call(self, *args, **kwargs):
            return {'independent_lookups': True}, {}
    expand_shared_requests(items, Yes())
    assert [execute_plan(checked_plan(t, table, t['request_text']), table)['data'] for t in items] == [
        [{'amount': 30}], [{'category': 'B'}]]


def test_or_list_preserves_subject_order_and_does_not_widen_other_or_conditions(table):
    items = [task(request_text='amount code 003 and 001', filter_logic='or', filters=[
        {'column': 'code', 'operator': 'eq', 'value': '001'},
        {'column': 'code', 'operator': 'eq', 'value': '003'}])]
    class Yes:
        def call(self, *args, **kwargs):
            return {'independent_lookups': True}, {}
    expand_shared_requests(items, Yes())
    assert [t['filters'][0]['value'] for t in items] == ['003', '001']
    mixed = task(request_text='amount code 003 or 001 or category B', filter_logic='or', filters=[
        {'column': 'code', 'operator': 'eq', 'value': '001'},
        {'column': 'code', 'operator': 'eq', 'value': '003'},
        {'column': 'category', 'operator': 'eq', 'value': 'B'}])
    unchanged = [deepcopy(mixed)]
    assert expand_shared_requests(unchanged, Yes()) == []
    assert unchanged == [mixed]


def test_filter_cannot_borrow_value_from_other_request(table):
    query = 'amount của code 003; category của code 002'
    wrong = task(filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}])
    with pytest.raises(ValueError, match='FILTER_VALUE'):
        checked_plan(wrong, table, query)


def test_same_column_cannot_require_two_different_entities(table):
    wrong = task(request_text='code 002 và 003', bindings=[], operation='count_rows', filters=[
        {'column': 'code', 'operator': 'eq', 'value': '002'},
        {'column': 'code', 'operator': 'eq', 'value': '003'}])
    with pytest.raises(ValueError, match='CONTRADICTORY'):
        checked_plan(wrong, table, wrong['request_text'])


def test_null_count_semantics_cannot_silently_return_zero(table):
    wrong = task(request_text='đếm amount trống', operation='count_values',
                 filters=[{'column': 'amount', 'operator': 'is_null', 'value': ''}])
    with pytest.raises(ValueError, match='NULL_RECORDS'):
        checked_plan(wrong, table, wrong['request_text'])
    wrong['operation'] = 'count_rows'
    assert execute_plan(checked_plan(wrong, table, wrong['request_text']), table)['data'] == [{'result': 1}]


class ModelStub:
    def __init__(self, tasks):
        self.tasks = tasks
        self.calls = 0

    def call(self, *args, **kwargs):
        self.calls += 1
        if 'task' in args[2]['properties']:
            return {'task': deepcopy(next(t for t in self.tasks if t['request_text'] == args[1]['question']))}, {'seconds': 0}
        return {'tasks': deepcopy(self.tasks)}, {'seconds': 0}


def test_partial_multi_result_keeps_unsupported_task_separate(table):
    model = ModelStub([task(), task(request_text='giá code 001', status='insufficient_data',
                                   reason='Không có giá.', bindings=[], filters=[])])
    result = run_grounded(table, 'amount của code 003; giá code 001', model)
    assert model.calls == 3
    assert [r['status'] for r in result['results']] == ['answer', 'insufficient_data']
    assert result['results'][0]['data'] == [{'amount': 30}]
    assert result['results'][0]['evidence']['source_rows_preview'][0]['row_id'] == 3


def test_duplicate_names_are_not_silently_limited(tmp_path):
    path = tmp_path / 'repeated.csv'
    path.write_text('code,amount\n003,10\n003,30\n', encoding='utf-8')
    t = Table(path)
    try:
        result = run_grounded(t, 'amount của code 003', ModelStub([task()]))
        assert result['results'][0]['data'] == [{'amount': 10}, {'amount': 30}]
    finally:
        t.close()


def test_absent_record_is_decided_by_execution(table):
    request = task(request_text='amount của code 999', filters=[{'column': 'code', 'operator': 'eq', 'value': '999'}])
    result = run_grounded(table, request['request_text'], ModelStub([request]))
    assert result['results'][0]['status'] == 'insufficient_data'
    assert result['results'][0]['evidence']['matching_rows'] == 0


def test_requested_unit_requires_header_evidence(table):
    request = task(request_text='amount theo độ C của code 003')
    result = run_grounded(table, request['request_text'], ModelStub([request]))
    assert result['results'][0]['status'] == 'insufficient_data'
    assert result['results'][0]['validation_error'] == 'REQUESTED_UNIT_NOT_SUPPORTED_BY_SOURCE_HEADER'


def test_over_eight_requests_are_reported_not_silently_truncated(table):
    class Overflow(ModelStub):
        def call(self, *args, **kwargs):
            payload, timing = super().call(*args, **kwargs)
            payload['request_limit_exceeded'] = True
            return payload, timing
    response = run_grounded(table, 'amount của code 003', Overflow([task()]))
    assert response['results'][0]['status'] == 'clarify'
    assert '8' in response['results'][0]['reason']


def test_truncated_results_are_explicitly_partial(tmp_path):
    path = tmp_path / 'many.csv'
    path.write_text('code,amount\n' + '\n'.join('003,10' for _ in range(1001)), encoding='utf-8')
    t = Table(path)
    try:
        response = run_grounded(t, 'amount của code 003', ModelStub([task()]))
        result = response['results'][0]
        assert result['status'] == 'partial'
        assert len(result['data']) == 1000
        assert result['evidence']['matching_rows'] == 1001
    finally:
        t.close()


def test_validation_repair_runs_once_and_preserves_good_result(table):
    class RepairModel(ModelStub):
        def call(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 3:
                return {'t0_b0': 'amount'}, {'seconds': 0}
            item = task()
            if self.calls < 3:
                item['bindings'][0]['requested_text'] = 'not a phrase in the question'
            return {'tasks': [item]}, {'seconds': 0}
    model = RepairModel([])
    response = run_grounded(table, 'amount của code 003', model)
    assert model.calls == 3
    assert response['repair_count'] == 1
    assert response['results'][0]['data'] == [{'amount': 30}]


def test_header_quote_repair_preserves_cross_domain_multi_field_mapping(table):
    requests = [
        task(request_text='how much for code 003', bindings=[{'column': 'amount', 'requested_text': 'amount'}]),
        task(request_text='which group for code 002', bindings=[{'column': 'category', 'requested_text': 'category'}],
             filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}]),
    ]
    class QuoteModel(ModelStub):
        def call(self, *args, **kwargs):
            if self.calls == 2:
                self.calls += 1
                return {'t0_b0': 'how much', 't1_b0': 'which group'}, {'seconds': 0}
            return super().call(*args, **kwargs)
    result = run_grounded(table, 'how much for code 003; which group for code 002', QuoteModel(requests))
    assert [r['data'] for r in result['results']] == [[{'amount': 30}], [{'category': 'B'}]]
    assert result['results'][0]['evidence']['field_bindings'] == [{'column': 'amount', 'requested_text': 'how much'}]
    assert result['model_output']['tasks'][0]['filters'] == requests[0]['filters']
    assert result['model_output']['tasks'][1]['filters'] == requests[1]['filters']


def test_quote_repair_cannot_borrow_another_tasks_words(table):
    request = task(request_text='how much for code 003')
    with pytest.raises(ValueError, match='FIELD_NOT_BOUND'):
        checked_plan(request, table, 'how much for code 003; amount code 002')
    class BadQuote:
        def call(self, *args, **kwargs):
            return {'t0_b0': 'amount'}, {'seconds': 0}
    before = deepcopy(request)
    with pytest.raises(ValueError, match='FIELD_NOT_BOUND'):
        repair_field_quotes([request], BadQuote())
    assert request == before


def test_shortage_recheck_restores_only_the_task_with_a_source_field(table):
    absent = task(request_text='forecast?', status='insufficient_data', reason='No forecast.', bindings=[], filters=[])
    denied = task(status='insufficient_data', reason='No amount.', bindings=[], filters=[])
    class IsolatedModel(ModelStub):
        def call(self, system, payload, output_schema, **kwargs):
            if 'task' in output_schema['properties']:
                assert payload['question'] == 'amount của code 003'
                assert 'forecast' not in payload['question']
                return {'task': task()}, {'seconds': 0}
            return super().call(system, payload, output_schema, **kwargs)
    response = run_grounded(table, 'forecast?; amount của code 003', IsolatedModel([absent, denied]))
    assert [r['status'] for r in response['results']] == ['insufficient_data', 'answer']
    assert response['results'][1]['data'] == [{'amount': 30}]
    assert response['availability_rechecks'][0]['task'] == 1


def test_shortage_recheck_cannot_bypass_filter_binding(table):
    denied = task(status='insufficient_data', reason='No amount.', bindings=[], filters=[])
    class WrongRecovery(ModelStub):
        def call(self, system, payload, output_schema, **kwargs):
            if 'task' in output_schema['properties']:
                return {'task': task(filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}])}, {'seconds': 0}
            return super().call(system, payload, output_schema, **kwargs)
    response = run_grounded(table, denied['request_text'], WrongRecovery([denied]))
    assert response['results'][0]['status'] in ('clarify', 'insufficient_data')
    assert response['results'][0]['data'] == []


def test_bad_plan_after_repair_fails_closed(table):
    wrong = task(filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}])
    model = ModelStub([wrong])
    response = run_grounded(table, 'amount của code 003', model)
    assert model.calls == 3
    assert response['results'][0]['status'] == 'clarify'
    assert response['results'][0]['data'] == []


def test_canonical_header_annotation_does_not_block_literal_label(tmp_path):
    path = tmp_path / 'annotated.csv'
    path.write_text('code,Length (mm)\n003,10\n', encoding='utf-8')
    t = Table(path)
    request = task(request_text='Length of code 003', bindings=[{'requested_text': 'Length (mm)', 'column': 'Length (mm)'}])
    try:
        response = run_grounded(t, request['request_text'], ModelStub([request]))
        assert response['results'][0]['data'] == [{'Length (mm)': 10}]
        assert response['results'][0]['evidence']['field_bindings'] == [{'requested_text': 'Length', 'column': 'Length (mm)'}]
        request['bindings'][0]['requested_text'] = 'invented measurement'
        with pytest.raises(ValueError, match='FIELD_NOT_BOUND'):
            checked_plan(request, t, request['request_text'])
    finally:
        t.close()


def test_empty_source_cell_is_not_an_answered_value(table):
    request = task(request_text='amount của code 002', filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}])
    response = run_grounded(table, request['request_text'], ModelStub([request]))
    assert response['results'][0]['status'] == 'insufficient_data'
    assert response['results'][0]['data'] == []
    assert response['results'][0]['evidence']['matching_rows'] == 1


def test_partial_fields_keep_available_value(table):
    request = task(request_text='amount và category của code 002', bindings=[
        {'requested_text': 'amount', 'column': 'amount'}, {'requested_text': 'category', 'column': 'category'}],
        filters=[{'column': 'code', 'operator': 'eq', 'value': '002'}])
    response = run_grounded(table, request['request_text'], ModelStub([request]))
    assert response['results'][0]['status'] == 'partial'
    assert response['results'][0]['data'] == [{'amount': None, 'category': 'B'}]


def test_omitted_semicolon_request_does_not_silently_pass(table):
    response = run_grounded(table, 'amount của code 003; category của code 002', ModelStub([task()]))
    assert response['results'][0]['status'] == 'clarify'
    assert response['uncovered_requests'] == ['category của code 002']


def test_semicolon_coverage_allows_one_task_covering_multiple_segments():
    question = 'Show code 001; 002; 003 together.'
    assert uncovered_requests(question, [{'request_text': question}]) == []


def test_ninth_request_cannot_be_dropped_when_model_flag_is_wrong(table):
    requests = [f'amount của code {i:03}' for i in range(1, 10)]
    tasks = [task(request_text=q, filters=[{'column': 'code', 'operator': 'eq', 'value': f'{i:03}'}])
             for i, q in enumerate(requests[:8], 1)]
    response = run_grounded(table, '; '.join(requests), ModelStub(tasks))
    assert response['results'][0]['status'] == 'clarify'
    assert response['uncovered_requests'] == [requests[-1]]


def test_projection_reviewer_cannot_change_predicates_or_add_columns(table):
    request = task(request_text='show amount where category is A',
                   filters=[{'column': 'category', 'operator': 'eq', 'value': 'A'}], bindings=[
                       {'column': 'amount', 'requested_text': 'amount'},
                       {'column': 'category', 'requested_text': 'category'}])
    class Projection:
        def call(self, *args, **kwargs):
            return {'decisions': [{'task_index': 0, 'return_columns': ['amount'], 'reason': 'Only amount requested.'}]}, {}
    original = deepcopy(request['filters'])
    review_projection([request], request['request_text'], Projection())
    assert request['filters'] == original
    assert execute_plan(checked_plan(request, table, request['request_text']), table)['data'] == [
        {'amount': 10}, {'amount': 30}]


def test_filter_field_can_also_be_explicitly_requested(table):
    request = task(request_text='show amount and category where category is A',
                   bindings=[{'column': c, 'requested_text': c} for c in ('amount', 'category')],
                   filters=[{'column': 'category', 'operator': 'eq', 'value': 'A'}])
    assert len(execute_plan(checked_plan(request, table, request['request_text']), table)['data']) == 2
    class InvalidProjection:
        def call(self, *args, **kwargs):
            return {'decisions': [{'task_index': 0, 'return_columns': ['code'], 'reason': ''}]}, {}
    with pytest.raises(ValueError, match='INVALID_PROJECTION'):
        review_projection([request], request['request_text'], InvalidProjection())


def test_two_entity_context_selects_second_without_copying_old_literals(table):
    first = task(request_text='amount code 001', filters=[{'column': 'code', 'operator': 'eq', 'value': '001'}])
    initial = run_grounded(table, 'amount code 001; amount của code 003', ModelStub([first, task()]))
    context = initial['recent_entities']
    assert [e['row_id'] for e in context] == [1, 3]
    followup = task(request_text='category của cái thứ hai',
        bindings=[{'column': 'category', 'requested_text': 'category'}], filters=[],
        context_ref=2, reference_text='cái thứ hai')
    result = run_grounded(table, followup['request_text'], ModelStub([followup]), context=context)
    assert result['results'][0]['data'] == [{'category': 'A'}]
    assert result['results'][0]['evidence']['source_rows_preview'][0]['row_id'] == 3
    followup['context_ref'] = 3
    with pytest.raises(ValueError, match='INVALID_CONTEXT_REFERENCE'):
        checked_plan(followup, table, followup['request_text'], context)


def test_context_rejects_other_source_and_never_stores_group_as_single_entity(table):
    with pytest.raises(ValueError, match='INVALID_CONTEXT_SOURCE'):
        run_grounded(table, 'amount?', ModelStub([]), context=[{'row_id': 1, 'source_sha256': '0' * 64}])
    request = task(request_text='amount where category A', filters=[{'column': 'category', 'operator': 'eq', 'value': 'A'}])
    response = run_grounded(table, request['request_text'], ModelStub([request]))
    assert response['recent_entities'] == []


def test_context_evicts_oldest_and_preserves_on_clarification(table):
    context = []
    for code in ('001', '002', '003'):
        request = task(request_text=f'category code {code}',
            bindings=[{'column': 'category', 'requested_text': 'category'}],
            filters=[{'column': 'code', 'operator': 'eq', 'value': code}])
        context = run_grounded(table, request['request_text'], ModelStub([request]), context=context)['recent_entities']
    assert [e['row_id'] for e in context] == [2, 3]
    request = task(request_text='cái đó?', status='clarify', reason='Bạn muốn hỏi đối tượng nào?')
    assert run_grounded(table, 'cái đó?', ModelStub([request]), context=context)['recent_entities'] == context


def test_unit_in_filter_does_not_require_returning_that_column(tmp_path):
    path = tmp_path / 'units.csv'
    path.write_text('code,price_USD\n001,0\n002,9\n', encoding='utf-8')
    t = Table(path)
    try:
        request = task(request_text='Show code where price_USD = 0',
            bindings=[{'column':'code','requested_text':'code'}],
            filters=[{'column':'price_USD','operator':'eq','value':'0'}])
        assert execute_plan(checked_plan(request,t,request['request_text']),t)['data'] == [{'code':'001'}]
        request['request_text'] = 'Show price in EUR where price_USD = 0'
        request['bindings'] = [{'column':'price_USD','requested_text':'price'}]
        with pytest.raises(ValueError, match='REQUESTED_UNIT'):
            checked_plan(request,t,request['request_text'])
    finally:
        t.close()


def test_historical_literal_must_belong_to_selected_context_row(table):
    context=[{'row_id':i,'source_sha256':table.hash,
              'identified_by':[{'column':'code','operator':'eq','value':code}]}
             for i,code in ((1,'001'),(3,'003'))]
    request=task(request_text='amount của cái thứ nhất',context_ref=1,reference_text='cái thứ nhất')
    with pytest.raises(ValueError,match='FILTER_VALUE'):
        checked_plan(request,table,request['request_text'],context)
    request['filters'][0]['value']='001'
    assert execute_plan(checked_plan(request,table,request['request_text'],context),table)['data']==[{'amount':10}]


def test_bad_projection_review_keeps_unaffected_intent(table):
    first=task(request_text='amount và category với category A',bindings=[
        {'column':'amount','requested_text':'amount'},{'column':'category','requested_text':'category'}],
        filters=[{'column':'category','operator':'eq','value':'A'}])
    class BadProjection(ModelStub):
        def call(self,*args,**kwargs):
            if self.calls==2:
                return {'decisions':[{'task_index':0,'return_columns':['code']}]},{}
            return super().call(*args,**kwargs)
    result=run_grounded(table,first['request_text']+'; amount của code 003',BadProjection([first,task()]))
    assert [r['status'] for r in result['results']]==['clarify','answer']
    assert result['results'][1]['data']==[{'amount':30}]
