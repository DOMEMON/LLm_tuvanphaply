import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from g85.package_builder import build_package, ROOT, sha
from g85.package_runtime import run_package
from g85.runtime.package_loader import read_package
from g85.runtime.contracts import Plan, Task, load_state
from g85.tabular_lab.service import create_app
from g85.tabular_lab.table import Table


class PackageModel:
    """Deliberately small deterministic metadata/planner fixture, not an accuracy eval."""
    def call(self, system, payload, schema, **kwargs):
        if 'field_selection' in schema['properties']:
            return {'field_selection': {k: True for k in schema['properties']['field_selection']['properties']},
                    'unresolved_qualifier': ''}, {}
        if 'collection_operation' in schema['properties']:
            return {'collection_operation': 'structured'}, {}
        if system.startswith('Describe this table'):
            return {'domain': 'Records', 'row_meaning': 'One record', 'warnings': [], 'columns': [
                {'name': c['name'], 'description': c['name'], 'unit': '',
                 'role': 'label' if c['name'] == 'name' else 'measure', 'ambiguous': False}
                for c in payload['columns']]}, {}
        if 'title_columns' in schema['properties']:
            return {'title_columns': ['name'], 'columns': [
                {'name': c['name'], 'description_supported': True} for c in payload['physical']['columns']]}, {}
        question = payload['question'].split('\n')[-1]
        if 'relation' in schema['properties']:
            if 'count' in question:
                tasks = [Task(kind='answer', quote=question, mode='table')]
            else:
                entity = 'row_2' if 'Beta' in question else 'row_1'
                if 'same' in question:
                    context = payload['conversation_context'][0]['content']
                    entity = json.loads(context.split('\n', 1)[1])['FOCUSED'][0]
                tasks = [Task(kind='answer', quote=question, entity=entity, attributes=['c002'])]
            return Plan(relation='continue' if 'same' in question else 'replace', tasks=tasks).model_dump(), {}
        return {'tasks': [{'request_text': question, 'status': 'answer', 'reason': '',
            'operation': 'count_rows', 'bindings': [], 'filters': [], 'filter_logic': 'and'}]}, {}


def make_package(tmp_path, content='name,value\nAlpha,0\nBeta,\n', suffix='.csv'):
    source = tmp_path / ('input' + suffix)
    if suffix == '.csv':
        source.write_text(content, encoding='utf-8')
    else:
        book = Workbook()
        book.active.append(['name', 'value'])
        book.active.append(['Alpha', '001'])
        book.active.append(['Beta', None])
        book.save(source)
    table = Table(source)
    try:
        info = build_package(table, tmp_path / 'package', PackageModel())
    finally:
        table.close()
    return tmp_path / 'package', info


@pytest.mark.parametrize('suffix', ['.csv', '.xlsx'])
def test_roundtrip_preserves_cells_locators_and_nulls(tmp_path, suffix):
    folder, info = make_package(tmp_path, suffix=suffix)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        assert package.version == info['version']
        assert package.table.rows[1]['value'] is None
        value = next(p for p in package.passages if p.entity == 'row_1' and p.attribute == 'c002')
        assert value.text == ('001' if suffix == '.xlsx' else '0')
        assert value.metadata['source_locator']['column'] == 'value'
        assert value.metadata['verification'] == 'exact_source_cell'
        assert all('review_status' not in p.metadata for p in package.passages)
    finally:
        package.table.close()


def test_portable_after_original_removed(tmp_path):
    folder, _ = make_package(tmp_path)
    copied = tmp_path / 'copied'
    shutil.copytree(folder, copied)
    (tmp_path / 'input.csv').unlink()
    result = run_package(copied, 'value Alpha', PackageModel())
    assert result['engine'] == 'g85-package'
    assert result['results'][0]['data'] == [{'value': 0}]
    assert result['results'][0]['evidence']['source_cells'][0]['metadata']['source_locator']['record_index'] == 1


def test_cell_tampering_rejected_even_with_updated_file_hash(tmp_path):
    folder, _ = make_package(tmp_path)
    path = folder / 'passages.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    rows[1]['text'] = '999'
    rows[1]['content_sha256'] = sha(b'999')
    data = ('\n'.join(json.dumps(r) for r in rows) + '\n').encode()
    path.write_bytes(data)
    manifest = json.loads((folder / 'manifest.json').read_text())
    manifest['files']['passages.jsonl'] = {'sha256': sha(data), 'bytes': len(data)}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='CELL_BINDING'):
        read_package(folder, ROOT / 'schema/domain-profile.schema.json')


def test_candidates_over_64_and_duplicate_titles_preserve_row_identity(tmp_path):
    content = 'name,value\n' + ''.join(f'Object {i},{i}\n' for i in range(100)) + 'Object 99,999\n'
    folder, _ = make_package(tmp_path, content)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        state = load_state(None, package.profile, package.version)
        candidates = package.select_catalog('Object 99 value', state)
        assert len(candidates) == 48
        assert {'row_100', 'row_101'} <= candidates.keys()
        assert len(package.catalog) == 101
    finally:
        package.table.close()


def test_missing_cell_and_aggregate_use_different_capabilities(tmp_path):
    folder, _ = make_package(tmp_path)
    missing = run_package(folder, 'value Beta', PackageModel())
    assert missing['results'][0]['status'] == 'insufficient_data'
    count = run_package(folder, 'count records', PackageModel())
    assert count['results'][0]['data'] == [{'result': 2}]
    assert count['results'][0]['evidence']['matching_rows'] == 2


def test_api_default_package_download_auth_and_restart_context(tmp_path):
    with TestClient(create_app(tmp_path, PackageModel)) as client:
        assert client.get('/health').json()['query_engine'] == 'g85_package'
        response = client.post('/v1/datasets?filename=records.csv', content=b'name,value\nAlpha,0\nBeta,\n')
        assert response.status_code == 201, response.text
        d = response.json()
        base = '/v1/datasets/' + d['dataset_id']
        headers = {'x-dataset-token': d['dataset_token']}
        assert d['profile']['package']['rows'] == 2
        assert client.get(base + '/package').status_code == 404
        archive = client.get(base + '/package', headers=headers)
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert 'source.csv' in z.namelist()
            assert 'dataset.json' not in z.namelist()
        conversation = client.post(base + '/conversations', headers=headers, json={'version': d['version']}).json()
        request = {'version': d['version'], 'conversation_id': conversation['conversation_id'], 'revision': 0, 'question': 'value Alpha'}
        response = client.post(base + '/query', headers=headers, json=request)
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['data'] == [{'value': 0}]
    with TestClient(create_app(tmp_path, PackageModel)) as client:
        request.update(revision=1, question='value of same')
        response = client.post(base + '/query', headers=headers, json=request)
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['data'] == [{'value': 0}]


def test_legacy_upload_migrates_without_losing_token_or_source(tmp_path):
    with TestClient(create_app(tmp_path, PackageModel, query_engine='grounded')) as old:
        d = old.post('/v1/datasets?filename=x.csv', content=b'name,value\nAlpha,0\n').json()
    with TestClient(create_app(tmp_path, PackageModel)) as new:
        response = new.post('/v1/datasets/' + d['dataset_id'] + '/query',
            headers={'x-dataset-token': d['dataset_token']}, json={'version': d['version'], 'question': 'value Alpha'})
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['data'] == [{'value': 0}]
        assert (tmp_path / d['dataset_id'] / 'table.csv').exists()


def test_anonymous_search_requires_full_table_execution(tmp_path):
    from g85.runtime.planner import validate_plan
    folder, _ = make_package(tmp_path)
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        package.table_query = lambda *args: None
        plan = Plan(relation='replace', tasks=[Task(kind='answer', quote='value over 10',
            mode='search', search_query='value over 10')])
        checked = validate_plan(plan.model_dump_json(), 'value over 10', package,
                                load_state(None, package.profile, package.version))
        assert checked.tasks[0].mode == 'table'
        assert checked.tasks[0].quote == 'value over 10'
        assert checked.tasks[0].search_query == ''
    finally:
        package.table.close()


def test_na_and_leading_zero_are_values_unless_explicitly_missing(tmp_path):
    folder, _ = make_package(tmp_path, 'name,value\nNA,001\nBeta,002\n')
    package = read_package(folder, ROOT / 'schema/domain-profile.schema.json')
    try:
        assert package.table.rows[0]['name'] == 'NA'
        assert package.table.rows[0]['value'] == '001'
        assert package.table.types['value'] == 'VARCHAR'
    finally:
        package.table.close()


@pytest.mark.parametrize('information', [False, True])
def test_empty_field_plan_distinguishes_bare_name_and_missing_property(tmp_path, information):
    class MissingModel(PackageModel):
        def call(self, system, payload, schema, **kwargs):
            if 'checks' in schema['properties']:
                return {'checks': [{'task': 0, 'information_requested': information}]}, {}
            if 'relation' in schema['properties']:
                q=payload['question'].split('\n')[-1]
                return Plan(relation='replace',tasks=[Task(kind='answer', quote=q, entity='row_1')]).model_dump(), {}
            if 'results' in schema['properties']:
                return {'results': [{'task_id': 'task-1', 'support_check': 'Requested property is absent.',
                    'support': 'absent', 'missing_needs': ['requested property'], 'excerpts': []}]}, {}
            return super().call(system,payload,schema,**kwargs)
    folder,_=make_package(tmp_path)
    result=run_package(folder, 'Alpha warranty?' if information else 'Alpha', MissingModel())
    assert result['results'][0]['status']==('insufficient_data' if information else 'clarify')
    assert not result['results'][0]['data']


@pytest.mark.parametrize('reuse', [False, True])
def test_field_followup_changes_subject_without_overriding_explicit_name_request(tmp_path, reuse):
    class Followup(PackageModel):
        def call(self, system, payload, schema, **kwargs):
            if 'reuse_previous_fields' in schema['properties']:
                return {'reuse_previous_fields': reuse}, {}
            if 'relation' in schema['properties']:
                q=payload['question'].split('\n')[-1]
                return Plan(relation='continue',tasks=[Task(kind='answer', quote=q, entity='row_2', attributes=['c001'])]).model_dump(), {}
            return super().call(system,payload,schema,**kwargs)
    folder,_=make_package(tmp_path,'name,value\nAlpha,0\nBeta,40\n')
    first=run_package(folder,'value Alpha',PackageModel())
    second=run_package(folder,'And for Beta?' if reuse else 'name Beta',Followup(),raw_state=first['runtime_state'],context=first['recent_entities'])
    assert second['results'][0]['data']==([{'value':40}] if reuse else [{'name':'Beta'}])
