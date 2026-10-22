"""Source/task isolation regressions; expected values never enter runtime code."""
import asyncio
from uuid import uuid4

import pytest

from g85.package_builder import ROOT
from g85.package_runtime import run_package
from g85.runtime.contracts import Plan, Task, load_state
from g85.runtime.package_loader import read_package
from g85.runtime.planner import understand, validate_plan
from g85.runtime.tabular_requests import request_spans, literal_query, missing_contact
from g85.tests.test_auto_package import make_package


class NoInference:
    def call(self, *args, **kwargs):
        raise AssertionError('A closed-form source query must not invoke the model')


@pytest.mark.parametrize('question', ['Bảng này có bao nhiêu dòng?',
    'Bảng này có tổng cộng bao nhiêu dòng dữ liệu, không tính tên cột?',
    'đếm tổng số dòng dữ liệu trong bảng', 'count all records'])
def test_count_uses_complete_source_without_model(tmp_path, question):
    folder, _ = make_package(tmp_path)
    result = run_package(folder, question, NoInference())['results'][0]
    assert result['data'] == [{'result': 2}]
    assert result['evidence']['matching_rows'] == 2


def test_count_with_prose_condition_is_not_an_unfiltered_count(tmp_path):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        assert literal_query(package.table, 'Bảng này có bao nhiêu dòng cần đóng phí?') is None
        assert literal_query(package.table, 'Cho tôi "value" của "Alpha" nếu được miễn cọc.', ['name']) is None
    finally:
        package.table.close()


def test_source_projection_and_nulls_remain_visible(tmp_path):
    folder, _ = make_package(tmp_path)
    result = run_package(folder, 'Liệt kê "name" và "value" trong bảng.', NoInference())['results'][0]
    assert result['status'] == 'partial'
    assert result['data'] == [{'name': 'Alpha', 'value': 0}, {'name': 'Beta', 'value': None}]


def test_physical_row_retained_even_when_title_has_no_row_number(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\n' + ''.join(f'Same,{i}\n' for i in range(150)))
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        selected = package.select_catalog('Trong bảng, "value" của bản ghi số 28 là gì?', load_state(None, package.profile, package.version))
        assert 'row_28' in selected
    finally:
        package.table.close()
    response = run_package(folder, 'Trong bảng, "value" của bản ghi số 28 có giá trị nào?', NoInference())
    assert response['results'][0]['data'] == [{'value': 27}]
    assert response['results'][0]['evidence']['source_rows_preview'][0]['row_id'] == 28


def test_nonexistent_row_is_not_replaced_by_similar_index(tmp_path):
    folder, _ = make_package(tmp_path)
    result = run_package(folder, 'Cho tôi "value" của bản ghi số 128.', NoInference())['results'][0]
    assert result['status'] == 'insufficient_data'
    assert not result['data']


def test_eight_source_requests_run_once_each_without_nested_planning(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\n' + ''.join(f'Item{i},{i}\n' for i in range(1, 9)))
    question = '; '.join(f'Cho tôi "value" của "Item{i}"' for i in range(1, 9))
    response = run_package(folder, question, NoInference())
    assert len(response['results']) == 8
    assert [r['data'] for r in response['results']] == [[{'value': i}] for i in range(1, 9)]
    assert len(response['recent_entities']) == 2


def test_nine_requests_are_rejected_before_execution(tmp_path):
    folder, _ = make_package(tmp_path)
    question = '; '.join(f'Cho tôi "value" của bản ghi số {i}' for i in range(1, 10))
    response = run_package(folder, question, NoInference())
    assert len(response['results']) == 1
    assert response['results'][0]['status'] == 'clarify'


def test_separators_inside_source_quotes_are_not_new_requests():
    query = 'Cho tôi "x; còn y?" của "Alpha"; còn "z" của "Beta"'
    assert request_spans(query) == ['Cho tôi "x; còn y?" của "Alpha"', '"z" của "Beta"']
    assert len(request_spans('Hỏi giá Alpha, với lại nơi giao Beta')) == 2


def test_missing_phone_cannot_be_substituted_with_email(tmp_path):
    folder, _ = make_package(tmp_path, 'name,email\nAlpha,alpha@example.invalid\nBeta,beta@example.invalid\n')
    response = run_package(folder, 'Cho tôi số điện thoại liên hệ của "Alpha".', NoInference())
    assert response['results'][0]['status'] == 'insufficient_data'
    assert response['results'][0]['data'] == []
    response = run_package(folder, 'Cho tôi số điện thoại liên hệ của "Alpha"; chỉ dùng bảng, nếu không có thì nói chưa có.', NoInference())
    assert response['results'][0]['status'] == 'insufficient_data'


def test_subject_name_that_mentions_phone_is_not_a_missing_property(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nPhone number authority,7\n')
    response = run_package(folder, 'Cho tôi "value" của "Phone number authority".', NoInference())
    assert response['results'][0]['data'] == [{'value': 7}]


def test_aggregate_executes_over_nulls_without_inventing_values(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nAlpha,0\nBeta,\nGamma,6\n')
    result = run_package(folder, 'Tính trung bình cột "value" trên toàn bảng, bỏ qua ô thiếu.', NoInference())['results'][0]
    assert result['data'] == [{'result': 3.0}]
    assert result['evidence']['matching_rows'] == 3


def test_unique_source_predicate_does_not_add_filter_column_to_outputs(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nAlpha,7\nBeta,9\n')
    result = run_package(folder, 'Cho tôi "value" của bản ghi có "name" = "Beta".', NoInference())['results'][0]
    assert result['data'] == [{'value': 9}]


def test_explicit_both_followup_preserves_two_source_rows(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nAlpha,7\nBeta,9\n')
    first = run_package(folder, 'Cho tôi "value" của "Alpha"; Cho tôi "value" của "Beta".', NoInference())
    second = run_package(folder, 'Cả hai bản ghi vừa hỏi có "value" thế nào?', NoInference(),
                         raw_state=first['runtime_state'], context=first['recent_entities'])
    assert [r['data'] for r in second['results']] == [[{'value': 7}], [{'value': 9}]]


def test_repair_quotes_are_scoped_to_each_current_request(tmp_path, monkeypatch):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    package.table_query = lambda *a: None
    query = 'Alpha temperature? Beta warranty?'
    calls = []
    async def completion(client, messages, schema, *args):
        calls.append(schema)
        if len(calls) == 1:
            return Plan(relation='replace', tasks=[Task(kind='answer', quote='old stale request', entity='row_1', attributes=['c002'])]).model_dump_json()
        quotes = schema['$defs']['Task']['properties']['quote']['enum']
        assert quotes == ['Alpha temperature', 'Beta warranty']
        assert query not in quotes
        return Plan(relation='replace', tasks=[Task(kind='answer', quote=quotes[i], entity=f'row_{i+1}', attributes=['c002']) for i in range(2)]).model_dump_json()
    monkeypatch.setattr('g85.runtime.planner.complete', completion)
    try:
        plan = asyncio.run(understand(query, load_state(None, package.profile, package.version), package, [], None, uuid4()))
        assert len(plan.tasks) == 2
        assert len(calls) == 2
    finally:
        package.table.close()


def test_truncated_model_plan_has_bounded_repair_instead_of_http_input_error(tmp_path, monkeypatch):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    calls = []
    async def completion(*args, **kwargs):
        calls.append(1)
        raise ValueError('MODEL_OUTPUT_TRUNCATED')
    monkeypatch.setattr('g85.runtime.planner.complete', completion)
    try:
        assert asyncio.run(understand('Alpha unknown property?', load_state(None, package.profile, package.version), package, [], None, uuid4())) is None
        assert len(calls) == 2
    finally:
        package.table.close()


def test_more_specific_source_variant_keeps_literal_qualifier(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nAlpha permit,1\nAlpha permit overseas,2\nBeta permit,3\n')
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        query = 'Alpha permit overseas value?'
        plan = Plan(relation='replace', tasks=[Task(kind='answer', quote=query, entity='row_1', attributes=['c002'])])
        checked = validate_plan(plan.model_dump_json(), query, package, load_state(None, package.profile, package.version))
        assert checked.tasks[0].entity == 'row_2'
    finally:
        package.table.close()


def test_global_condition_cannot_bypass_collection_gate_as_one_record(tmp_path):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    package.table_query = lambda *a: None
    query = 'Which records require payment?'
    try:
        plan = Plan(relation='replace', tasks=[Task(kind='answer', quote=query, entity='row_1', attributes=['c002'])])
        checked = validate_plan(plan.model_dump_json(), query, package, load_state(None, package.profile, package.version))
        assert checked.tasks[0].mode == 'table'
        assert checked.tasks[0].entity == ''
    finally:
        package.table.close()


def test_wide_source_projection_uses_table_instead_of_task_attribute_limit(tmp_path):
    headers = ['name'] + [f'value{i}' for i in range(16)]
    folder, _ = make_package(tmp_path, ','.join(headers) + '\nAlpha,' + ','.join(str(i) for i in range(16)) + '\n')
    question = 'Cho tôi ' + ' và '.join(f'"{h}"' for h in headers[1:]) + ' của bản ghi số 1.'
    result = run_package(folder, question, NoInference())['results'][0]
    assert result['data'] == [{f'value{i}': i for i in range(16)}]


def test_nested_source_quotes_and_duplicate_labels_keep_one_request(tmp_path):
    content = 'name,value\nAlpha “special” award,3\nDuplicate,7\nDuplicate,9\n'
    folder, _ = make_package(tmp_path, content)
    query = 'Cho tôi “value” của “Alpha “special” award”; Cho tôi “value” của “Duplicate”.'
    response = run_package(folder, query, NoInference())
    assert len(response['results']) == 2
    assert response['results'][0]['data'] == [{'value': 3}]
    assert response['results'][1]['data'] == [{'value': 7}, {'value': 9}]


def test_bulk_insert_preserves_exact_cells_types_and_order_across_batches(tmp_path):
    import csv
    from g85.tabular_lab.table import Table
    path = tmp_path / 'typed.csv'
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['code', 'integer', 'fraction', 'date', 'text'])
        for i in range(600):
            writer.writerow([f'{i:04}', 9007199254740993 + i, i / 7, '2026-10-22',
                             'text "quote" ; newline\nNUL\x00 🐧' if i % 2 else ''])
    table = Table(path)
    try:
        assert table.sql('SELECT * FROM data ORDER BY _row_id') == table.rows
        assert table.rows[0]['code'] == '0000'
        assert table.rows[0]['integer'] == 9007199254740993
    finally:
        table.close()


@pytest.mark.parametrize('identifiers', [('0007', '0012'), ('2025-04-01', '2025-04-02')])
def test_shared_projection_keeps_separate_record_predicates(tmp_path, identifiers):
    left, right = identifiers
    folder, _ = make_package(tmp_path, f'name,value\n{left},19\n{right},23\n')
    query = (f'Cho tôi “value” của bản ghi có “name” = "{left}" và '
             f'bản ghi có “name” = "{right}", tách riêng mỗi bản ghi.')
    results = run_package(folder, query, NoInference())['results']
    assert [r['data'] for r in results] == [[{'value': 19}], [{'value': 23}]]
    assert [r['evidence']['source_rows_preview'][0]['row_id'] for r in results] == [1, 2]


def test_two_record_query_is_never_compiled_as_one_conjunction(tmp_path):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        assert literal_query(package.table,
            'Cho tôi “value” của bản ghi có “name” = "Alpha" và bản ghi có “name” = "Missing".') is None
        plan = literal_query(package.table,
            'Cho tôi “value” của bản ghi có “name” = "Alpha" và “value” > -1.')
        assert len(plan['filters']) == 2  # Two predicates on ONE record remain conjunctive.
    finally:
        package.table.close()


def test_secondary_short_label_can_identify_source_row_without_changing_package(tmp_path):
    folder, _ = make_package(tmp_path, 'name,title,value\nCODE-1,Blue item,8\nCODE-2,Red item,13\n')
    results = run_package(folder, 'Cho tôi “value” của “Blue item”; Cho tôi “value” của “Red item”.', NoInference())['results']
    assert [r['data'] for r in results] == [[{'value': 8}], [{'value': 13}]]


def test_reverse_literal_preserves_backslash_and_semicolons(tmp_path):
    folder, _ = make_package(tmp_path, 'name,text\nAlpha,First ; second\\nThird or sum\nBeta,Other\n')
    result = run_package(folder,
        'Liệt kê riêng cột “name” của những dòng có “text” bằng "First ; second\\nThird or sum".', NoInference())['results'][0]
    assert result['data'] == [{'name': 'Alpha'}]
    assert result['evidence']['source_rows_preview'][0]['row_id'] == 1


@pytest.mark.parametrize('value', ['First\\nSecond or sum', 'Value "quoted"', 'Trailing\\'])
def test_json_escaped_filter_is_verified_against_source(tmp_path, value):
    import csv
    import io
    import json
    content = io.StringIO(newline='')
    writer = csv.writer(content)
    writer.writerows([['name', 'text'], ['Alpha', value], ['Beta', 'Other']])
    folder, _ = make_package(tmp_path, content.getvalue())
    query = 'Liệt kê riêng cột “name” của những dòng có “text” bằng ' + json.dumps(value) + '.'
    result = run_package(folder, query, NoInference())['results'][0]
    assert result['status'] == 'answer'
    assert result['data'] == [{'name': 'Alpha'}]
