"""Contract regressions for reusable package routing, not a model accuracy score."""
import asyncio

import pytest

from g85.package_builder import ROOT
from g85.package_runtime import ModelTransport, run_package
from g85.runtime.contracts import Plan, Task, load_state
from g85.runtime.package_loader import read_package
from g85.runtime.table_review import review_direct_tasks
from g85.tabular_lab.collection import run_collection
from g85.tests.test_auto_package import PackageModel, make_package


def test_candidate_catalog_cannot_bypass_full_table(tmp_path):
    folder, _ = make_package(tmp_path)
    class CatalogModel(PackageModel):
        def call(self, system, payload, schema, **kwargs):
            if 'relation' in schema['properties']:
                return Plan(relation='replace', tasks=[Task(kind='catalog',
                    quote='count all records', candidates=['row_1'])]).model_dump(), {}
            return super().call(system, payload, schema, **kwargs)
    response = run_package(folder, 'count all records', CatalogModel())
    assert response['results'][0]['data'] == [{'result': 2}]
    assert response['results'][0]['evidence']['matching_rows'] == 2


def test_experimental_reviewer_is_off_for_default_runtime(tmp_path):
    folder, _ = make_package(tmp_path)
    class NoReviewer(PackageModel):
        def call(self, system, payload, schema, **kwargs):
            assert 'field_selection' not in schema['properties']
            return super().call(system, payload, schema, **kwargs)
    assert run_package(folder, 'value Alpha', NoReviewer())['results'][0]['data'] == [{'value': 0}]


def test_one_word_label_hint_is_found_across_complete_source(tmp_path):
    from g85.tabular_lab.grounded import catalog
    folder, _ = make_package(tmp_path, 'name,value\nCedar standard,0\nHarbor suite,5\nCedar deluxe,10\n')
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        source = catalog(package.table, 'List Cedar rooms')
        column = next(c for c in source['columns'] if c['name'] == 'name')
        match = next(c for c in column['partial_label_candidates'] if c['question_phrase'] == 'cedar')
        assert match['matching_records'] == 2
        assert match['source_values'] == ['Cedar standard', 'Cedar deluxe']
    finally:
        package.table.close()


@pytest.mark.parametrize('mode', ['semantic', 'unknown'])
def test_text_condition_never_falls_back_to_unfiltered_menu(tmp_path, mode):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    class Gate:
        def call(self, *args, **kwargs):
            return {'collection_operation': mode}, {}
    try:
        result = run_collection(package.table, 'Which records require payment?', Gate())
        assert result['results'][0]['status'] == 'clarify'
        assert result['results'][0]['data'] == []
        assert result['capability_gate']
    finally:
        package.table.close()


@pytest.mark.parametrize('qualifier', ['', 'special edition'])
def test_review_drops_old_fields_or_clarifies_unsupported_variant(tmp_path, qualifier):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    state = load_state(None, package.profile, package.version)
    plan = Plan(relation='continue', tasks=[Task(kind='answer', quote='Alpha special edition value?',
        entity='row_1', attributes=['c001', 'c002']), Task(kind='answer', quote='Beta value?',
        entity='row_2', attributes=['c002'])])
    class Review:
        def call(self, system, payload, schema, **kwargs):
            fields = schema['properties']['field_selection']['properties']
            return {'field_selection': {k: k == 'c002' for k in fields},
                    'unresolved_qualifier': qualifier if 'c001' in fields else ''}, {}
    try:
        checked = asyncio.run(review_direct_tasks(plan, 'Alpha special edition value? Beta value?', state,
            package, ModelTransport(Review()), 'test'))
        assert checked.tasks[0].kind == ('clarify' if qualifier else 'answer')
        assert checked.tasks[0].attributes == ([] if qualifier else ['c002'])
        assert checked.tasks[1].kind == 'answer'  # independent valid need survives
        assert checked.tasks[1].attributes == ['c002']
    finally:
        package.table.close()


@pytest.mark.parametrize('decision,error', [
    ({'field_selection': {'c001': True}, 'unresolved_qualifier': ''}, 'ADDED_FIELD'),
    ({'field_selection': {'c002': True}, 'unresolved_qualifier': 'invented'}, 'NOT_IN_REQUEST'),
])
def test_review_cannot_invent_fields_or_user_conditions(tmp_path, decision, error):
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    state = load_state(None, package.profile, package.version)
    plan = Plan(relation='replace', tasks=[Task(kind='answer', quote='Alpha value?',
                                             entity='row_1', attributes=['c002'])])
    class Review:
        def call(self, *args, **kwargs):
            return decision, {}
    try:
        with pytest.raises(ValueError, match=error):
            asyncio.run(review_direct_tasks(plan, 'Alpha value?', state, package, ModelTransport(Review()), 'test'))
    finally:
        package.table.close()
