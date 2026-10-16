from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest

from .service import create_app as service_app


def create_app(*args, **kwargs):
    # These transport regressions exercise the original table-plan engine.
    # Package ingestion and the restored runtime have their own integration tests.
    kwargs.setdefault('query_engine', 'grounded')
    return service_app(*args, **kwargs)


class ContractModel:
    """Transport tests use a deterministic planner; live inference is evaluated separately."""
    def call(self, system, payload, schema, **kwargs):
        if 'operation' in schema['properties']:
            return {'operation': 'structured'}, {}
        if system.startswith('Describe this table'):
            return {'domain': 'Test', 'row_meaning': 'Record', 'warnings': [], 'columns': [
                {'name': c['name'], 'description': c['name'], 'unit': 'USD', 'role': 'label', 'ambiguous': True}
                for c in payload['columns']]}, {'seconds': 0}
        return {'tasks': [{'request_text': payload['question'], 'status': 'answer', 'reason': '',
            'operation': 'count_rows', 'bindings': [], 'filter_logic': 'and', 'filters': []}]}, {'seconds': 0}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, ContractModel)) as session:
        yield session


def upload(client, content=b'code,value\n001,10\n002,20\n'):
    response = client.post('/v1/datasets?filename=data.csv', content=content)
    assert response.status_code == 201, response.text
    return response.json()


def ask(client, data, **changes):
    return client.post('/v1/datasets/' + data['dataset_id'] + '/query',
        headers={'X-Dataset-Token': data['dataset_token']},
        json={'version': data['version'], 'question': 'Có bao nhiêu dòng?', **changes})


def test_upload_profile_and_query(client):
    data = upload(client)
    assert data['status'] == 'ready'
    assert data['profile']['physical']['row_count'] == 2
    assert data['profile']['semantic_proposal']['columns'][0]['unit'] == ''
    result = ask(client, data)
    assert result.status_code == 200
    assert result.json()['results'][0]['data'] == [{'result': 2}]
    assert result.json()['version'] == data['version']


def test_datasets_are_isolated_and_do_not_share_tokens(client):
    a = upload(client)
    b = upload(client, b'code,value\n003,30\n')
    assert ask(client, a).json()['results'][0]['data'] == [{'result': 2}]
    assert ask(client, b).json()['results'][0]['data'] == [{'result': 1}]
    bad = {**b, 'dataset_token': a['dataset_token']}
    assert ask(client, bad).status_code == 404
    assert client.get('/v1/datasets/' + a['dataset_id']).status_code == 404


def test_version_mismatch_rejected(client):
    data = upload(client)
    assert ask(client, data, version='0' * 64).status_code == 409


def test_invalid_or_duplicate_headers_rejected(client):
    assert client.post('/v1/datasets?filename=x.csv', content=b'a,A\n1,2\n').status_code == 422
    assert client.post('/v1/datasets?filename=x.exe', content=b'hello').status_code == 422


def test_source_tampering_detected(tmp_path):
    with TestClient(create_app(tmp_path, ContractModel)) as client:
        data = upload(client)
        source = tmp_path / data['dataset_id'] / 'table.csv'
        source.write_bytes(b'code,value\n001,900\n002,20\n')
        assert ask(client, data).status_code == 409


def test_service_restart_preserves_dataset_access(tmp_path):
    with TestClient(create_app(tmp_path, ContractModel)) as first:
        data = upload(first)
    with TestClient(create_app(tmp_path, ContractModel)) as second:
        assert ask(second, data).json()['results'][0]['data'] == [{'result': 2}]


def test_queue_is_bounded_and_health_stays_responsive(tmp_path):
    release = threading.Event()
    block_queries = threading.Event()

    class Slow(ContractModel):
        def call(self, system, payload, schema, **kwargs):
            if block_queries.is_set() and not system.startswith('Describe this table'):
                assert release.wait(10)
            return super().call(system, payload, schema, **kwargs)

    with TestClient(create_app(tmp_path, Slow)) as client:
        data = upload(client)
        block_queries.set()
        with ThreadPoolExecutor(max_workers=8) as callers:
            pending = [callers.submit(ask, client, data) for _ in range(8)]
            try:
                deadline = time.monotonic() + 5
                while client.get('/health').json()['pending'] < 8 and time.monotonic() < deadline:
                    time.sleep(.01)
                assert client.get('/health').json()['pending'] == 8
                assert ask(client, data).status_code == 429
                assert client.post('/v1/datasets?filename=blocked.csv', content=b'a,b\n1,2\n').status_code == 429
            finally:
                release.set()
            assert all(f.result(timeout=10).status_code == 200 for f in pending)
        assert client.get('/health').json()['pending'] == 0


def test_invalid_xlsx_is_a_client_error_not_server_failure(client):
    assert client.post('/v1/datasets?filename=bad.xlsx', content=b'not an archive').status_code == 422


def test_conversation_revision_is_durable_and_dataset_scoped(tmp_path):
    with TestClient(create_app(tmp_path, ContractModel)) as client:
        a, b = upload(client), upload(client)
        endpoint = '/v1/datasets/' + a['dataset_id'] + '/conversations'
        headers = {'X-Dataset-Token': a['dataset_token']}
        response = client.post(endpoint, headers=headers, json={'version': a['version']})
        assert response.status_code == 201
        state = response.json()
        assert ask(client, a, conversation_id=state['conversation_id']).status_code == 422
        result = ask(client, a, conversation_id=state['conversation_id'], revision=0)
        assert result.json()['conversation']['revision'] == 1
        assert ask(client, a, conversation_id=state['conversation_id'], revision=0).status_code == 409
        assert ask(client, b, conversation_id=state['conversation_id'], revision=1).status_code == 404
        assert client.get(endpoint + '/' + state['conversation_id']).status_code == 404
    with TestClient(create_app(tmp_path, ContractModel)) as restarted:
        stored = restarted.get(endpoint + '/' + state['conversation_id'], headers=headers)
        assert stored.json()['revision'] == 1
        assert ask(restarted, a, conversation_id=state['conversation_id'], revision=1).json()['conversation']['revision'] == 2


def test_conversation_focus_survives_restart_and_is_not_shared(tmp_path):
    class Selector(ContractModel):
        def call(self, system, payload, schema, **kwargs):
            if system.startswith('Describe this table') or 'operation' in schema['properties']:
                return super().call(system,payload,schema,**kwargs)
            follow = 'same' in payload['question']
            return {'tasks':[{'request_text':payload['question'],'status':'answer','reason':'','operation':'select',
                'bindings':[{'requested_text':'value','column':'value'}], 'filter_logic':'and',
                'filters':[] if follow else [{'column':'code','operator':'eq','value':'001'}],
                'context_ref':1 if follow else 0,'reference_text':'same' if follow else ''}]}, {}
    with TestClient(create_app(tmp_path,Selector)) as client:
        data=upload(client)
        url='/v1/datasets/'+data['dataset_id']+'/conversations'
        headers={'X-Dataset-Token':data['dataset_token']}
        a=client.post(url,headers=headers,json={'version':data['version']}).json()
        b=client.post(url,headers=headers,json={'version':data['version']}).json()
        result=ask(client,data,question='value code 001',conversation_id=a['conversation_id'],revision=0).json()
        assert result['conversation']['recent_entities'][0]['row_id'] == 1
        empty=ask(client,data,question='value of same',conversation_id=b['conversation_id'],revision=0).json()
        assert empty['results'][0]['status'] == 'clarify'
    with TestClient(create_app(tmp_path,Selector)) as client:
        result=ask(client,data,question='value of same',conversation_id=a['conversation_id'],revision=1).json()
        assert result['results'][0]['data'] == [{'value':10}]


def test_workspace_and_dataset_contract_are_available(client):
    assert client.get('/').status_code == 200
    assert 'text/javascript' in client.get('/workspace/app.js').headers['content-type']
    data=upload(client)
    contract=data['profile']['contract']
    assert contract['source_sha256'] == data['version']
    assert contract['missing_cells'] == 0
    assert contract['semantic_verification'] == 'not_performed'


def test_experimental_engine_requires_explicit_selection(tmp_path, monkeypatch):
    from . import service
    def forbidden(*args, **kwargs):
        raise AssertionError('Experimental engine must not be activated implicitly')
    monkeypatch.setattr(service, 'run_reusable', forbidden)
    with TestClient(create_app(tmp_path, ContractModel, query_engine='grounded')) as client:
        assert client.get('/health').json()['query_engine'] == 'grounded'
        data = upload(client)
        assert ask(client, data).json()['results'][0]['status'] == 'answer'


def test_experimental_engine_is_available_for_isolated_evaluation(tmp_path):
    with TestClient(create_app(tmp_path, ContractModel, query_engine='reusable_experimental')) as client:
        health = client.get('/health').json()
        assert health['query_engine'] == 'reusable_experimental'
        assert health['semantic_text_filter_enabled'] is False
        data = upload(client)
        assert ask(client, data).json()['results'][0]['status'] == 'answer'
