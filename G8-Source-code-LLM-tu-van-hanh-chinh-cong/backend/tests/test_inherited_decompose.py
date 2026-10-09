import json
from uuid import uuid4
import httpx
import pytest
from app.rag.g7.contracts import State, Task
from app.rag.g7.catalog import cards
from app.rag.g7.decompose import Resolution, bind_matches
from app.rag.g7.planner import understand


async def test_bounded_decomposition_preserves_original_and_never_supplies_facts(data, monkeypatch):
    monkeypatch.setenv('G7_DECOMPOSE', 'true')
    calls = []
    card = next(c for c in cards(data) if c['label'] == 'birth_registration')
    query = 'Nhà mới có em bé, cần giấy tờ gì?'
    resolution = {'relation': 'replace', 'requests': [{'need': 'Đăng ký khai sinh cho con',
                   'fields': ['required_documents'], 'places': [], 'place_quote': ''}]}
    plan = {'tasks': [{'request_index': 0, 'kind': 'procedure', 'code': card['label']}]}
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        value = resolution if len(calls) == 1 else plan
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(value)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        actual = await understand(query, State(corpus_version=data.version), cards(data), [], client, uuid4())
    assert len(calls) == 2 and actual.tasks[0].code == card['code']
    assert json.loads(calls[1]['messages'][-1]['content'])['CURRENT_USER'] == query
    assert 'NEEDS' in calls[1]['messages'][-1]['content']
    assert calls[0]['response_format']['json_schema']['name'] == 'decompose'
    assert Resolution.model_validate(resolution).requests[0].places == []
    assert actual.tasks[0].fields == ['required_documents']


@pytest.mark.parametrize('indices', [[0], [0, 0], [0, 2], [1, 1]])
def test_binding_rejects_missing_duplicate_or_invented_need(indices):
    resolution = Resolution.model_validate({'relation': 'replace', 'requests': [
        {'need': 'A', 'fields': ['fees']}, {'need': 'B', 'fields': ['required_documents']}]})
    raw = json.dumps({'tasks': [{'request_index': i, 'kind': 'procedure', 'code': 'a'} for i in indices]})
    with pytest.raises(ValueError):
        bind_matches(raw, resolution, 'A và B')


def test_classifier_cannot_override_field_or_scope_binding():
    resolution = Resolution.model_validate({'relation': 'replace', 'requests': [
        {'need': 'A', 'fields': ['fees'], 'places': ['Hà Nội'], 'place_quote': 'Hà Nội'},
        {'need': 'B', 'fields': ['required_documents']}]})
    raw = json.dumps({'tasks': [{'request_index': 1, 'kind': 'procedure', 'code': 'b'},
                               {'request_index': 0, 'kind': 'procedure', 'code': 'a'}]})
    plan = bind_matches(raw, resolution, 'A và B')
    assert [t.code for t in plan.tasks] == ['a', 'b']
    assert plan.tasks[0].fields == ['fees'] and plan.tasks[0].scope.places == ['Hà Nội']
    assert plan.tasks[1].fields == ['required_documents'] and not plan.tasks[1].scope.places
