import httpx

from g85.tabular_lab.methods import Model, obj, STRING
from g85.tabular_lab.service import create_app
from fastapi.testclient import TestClient


def test_model_configuration_is_portable(monkeypatch):
    monkeypatch.setenv('G85_MODEL_URL', 'http://model.example:8180/v1/')
    monkeypatch.setenv('G85_MODEL_NAME', 'review-model')
    model = Model()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': '{"value":"source"}'}}]})

    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        result, _ = model.call('Test', {}, obj({'value': STRING}))
        assert result == {'value': 'source'}
        assert str(requests[0].url) == 'http://model.example:8180/v1/chat/completions'
        import json
        assert json.loads(requests[0].content)['model'] == 'review-model'
    finally:
        model.client.close()


def test_storage_configuration_and_static_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv('G85_DATA_DIR', str(tmp_path / 'datasets'))
    with TestClient(create_app()) as client:
        assert (tmp_path / 'datasets').is_dir()
        assert client.get('/health').json()['query_engine'] == 'g85_package'
        assert client.get('/health').json()['workers'] == 1
        assert client.get('/').status_code == 200
        for asset in ('app.js', 'app.css', 'assets/icons.svg', 'assets/fonts.css'):
            assert client.get('/workspace/' + asset).status_code == 200
