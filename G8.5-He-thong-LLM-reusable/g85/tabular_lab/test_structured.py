from jsonschema import validate, ValidationError
import pytest

from .structured_v3 import flatten, query_schema
from .table import execute_plan
from .test_table import table


def test_null_row_count_is_a_distinct_operation(table):
    task = {'status': 'answer', 'operation': 'count_rows', 'filter_logic': 'and',
            'filters': [{'column': 'amount', 'operator': 'is_null', 'value': ''}]}
    validate({'tasks': [task]}, query_schema(table))
    assert execute_plan(flatten(task), table)['data'] == [{'result': 1}]
    task['field'] = 'amount'
    with pytest.raises(ValidationError):
        validate({'tasks': [task]}, query_schema(table))


def test_aggregate_cannot_include_unnecessary_sort(table):
    task = {'status': 'answer', 'operation': 'max', 'field': 'amount', 'filter_logic': 'and', 'filters': []}
    validate({'tasks': [task]}, query_schema(table))
    assert execute_plan(flatten(task), table)['data'] == [{'result': 30}]
    task['order_by'] = 'amount'
    with pytest.raises(ValidationError):
        validate({'tasks': [task]}, query_schema(table))


def test_unavailable_information_requires_no_fake_query(table):
    task = {'status': 'insufficient_data', 'reason': 'No mapping column available.'}
    validate({'tasks': [task]}, query_schema(table))
    assert execute_plan(flatten(task), table)['status'] == 'insufficient_data'


def test_guarded_schema_decides_status_before_operation(table):
    schema = query_schema(table, status_first=True)
    for branch in schema['properties']['tasks']['items']['anyOf']:
        assert next(iter(branch['properties'])) == 'status'
    validate({'tasks': [{'status': 'insufficient_data', 'reason': 'Column absent.'}]}, schema)
    validate({'tasks': [{'status': 'answer', 'operation': 'count_rows',
                        'filter_logic': 'and', 'filters': []}]}, schema)


def test_guarded_schema_rejects_unknown_source_column(table):
    task = {'status': 'answer', 'operation': 'sum', 'field': 'invented_field',
            'filter_logic': 'and', 'filters': []}
    with pytest.raises(ValidationError):
        validate({'tasks': [task]}, query_schema(table, status_first=True))
