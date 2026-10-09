import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from app.errors import APIError
from app.rag.g7.catalog import cards, matching_cards, DESCRIPTIONS, SUPPORT_BOUNDARIES
from app.rag.g7.contracts import Plan, State, Task, Scope, load_state
from app.rag.g7.planner import validate_plan, understand, wire_contract, to_wire
from app.rag.g7.workflow import allowed_scope, execute, transition
from app.rag.schemas import Filters, Grounding


def task_for(data, suffix, fields=(), **kw):
    code = next(c['code'] for c in cards(data) if c['id'].endswith(suffix))
    return Task(kind='procedure', quote='câu hỏi', code=code, fields=list(fields), **kw)


def test_catalog_complete_unique(data):
    catalog = cards(data)
    assert len(catalog) == 36 == len(DESCRIPTIONS)
    assert len({c['id'] for c in catalog}) == 36
    assert len({c['code'] for c in catalog}) == 36


@pytest.mark.parametrize('domain,excluded', [('housing', 'marriage'), ('land', 'marriage'), ('employment', 'birth')])
def test_catalog_no_unrelated_domain(data, domain, excluded):
    selected = matching_cards(cards(data), [domain])
    assert selected and all(excluded not in c['domains'] for c in selected)


@pytest.mark.parametrize('scope,expected', [
    ({}, True), ({'ward': 'Tăng Nhơn Phú'}, True), ({'province': 'TP.HCM'}, True),
    ({'province': 'Hà Nội'}, False), ({'ward': 'Linh Xuân'}, False),
    ({'country': 'Nhật Bản'}, False), ({'country': 'VN', 'province': 'HCM'}, True),
    ({'ward': 'Tăng Nhơn Phú', 'country': 'Japan'}, False),
    ({'ward': 'Tăng Nhơn Phú A'}, False),
    ({'places': ['phường Tăng Nhơn Phú', 'TP.HCM']}, True),
    ({'places': ['Đà Nẵng']}, False),
    ({'places': ['Tăng Nhơn Phú', 'Nhật Bản']}, False),
])
def test_scope(scope, expected):
    assert allowed_scope(Scope(**scope)) is expected


def test_legacy_state_not_silently_inherited(data):
    state = load_state({'procedure_id': 'old', 'version': 'g7-v2'}, data.version)
    assert state.active == []
    assert load_state(State(corpus_version='old').model_dump(), data.version).corpus_version == data.version


def test_contract_rejects_fake_fields():
    with pytest.raises(ValidationError):
        Task(kind='procedure', quote='x', fields=['invented'])


@pytest.mark.parametrize('mutation', ['id', 'domain', 'scope'])
def test_validator_rejects_ungrounded_contract(data, mutation):
    task = task_for(data, '58aa31837615', ['fees'])
    if mutation == 'id': task.code = 'T999'
    if mutation == 'domain': task.domains = ['imaginary']
    if mutation == 'scope': task.scope = Scope(ward='Hà Nội')
    with pytest.raises(ValueError):
        validate_plan(Plan(relation='replace', tasks=[task]), 'câu hỏi', cards(data), State(corpus_version=data.version))


def test_model_paraphrase_is_not_falsely_treated_as_user_evidence(data):
    task = task_for(data, '58aa31837615', ['fees'])
    task.quote = 'model paraphrase'
    plan = validate_plan(Plan(relation='replace', tasks=[task]), 'câu gốc', cards(data), State(corpus_version=data.version))
    assert plan.tasks[0].quote == 'câu gốc'


async def run(data, tasks):
    return await execute(data, Plan(relation='replace', tasks=tasks), State(corpus_version=data.version),
                         Filters(), SimpleNamespace(client=None), uuid4())


async def test_multi_binding_and_citations(data):
    tasks = [task_for(data, '58aa31837615', ['fees', 'required_documents']),
             task_for(data, '6d6862db57ba', ['receiving_authority'])]
    text, g, state = await run(data, tasks)
    assert len(g['parts']) == 2 and len(state.active) == 2
    for task, part in zip(tasks, g['parts']):
        assert {s['fragment_id'].split(':')[0] for s in part['grounding']['sources']} == {part['procedure_id']}
        assert {c['field'] for c in part['grounding']['checklist']} == set(task.fields)
    Grounding.model_validate(g)
    assert text.count('### ') == 2


async def test_multi_request_never_creates_blanket_mcp_offer(data, monkeypatch):
    from app.rag.g7 import workflow
    monkeypatch.setattr(workflow.get_settings(), 'g5_realtime_mcp_enabled', True)
    def forbidden(*args, **kwargs):
        raise AssertionError('A multi-procedure turn must not offer blanket MCP consent')
    monkeypatch.setattr(workflow.realtime, 'offer', forbidden)
    _, _, state = await run(data, [task_for(data, '58aa31837615', ['fees']),
                                  task_for(data, 'c343c85df1af', ['fees'])])
    assert state.pending_realtime is None


async def test_outside_locality_never_offers_mcp_to_expand_scope(data, monkeypatch):
    from app.rag.g7 import workflow
    monkeypatch.setattr(workflow.get_settings(), 'g5_realtime_mcp_enabled', True)
    def forbidden(*args, **kwargs):
        raise AssertionError('MCP must not bypass locality policy')
    monkeypatch.setattr(workflow.realtime, 'offer', forbidden)
    _, g, state = await run(data, [task_for(data, '58aa31837615', ['fees'],
                                  scope=Scope(places=['Hà Nội'], quote='Hà Nội'))])
    assert state.pending_realtime is None and not g['sources']


async def test_partial_outside_does_not_suppress_answer(data):
    _, g, state = await run(data, [task_for(data, '58aa31837615', ['fees']),
                                  Task(kind='outside', quote='nấu phở')])
    assert g['status'] == 'INSUFFICIENT_DATA'
    assert g['parts'][0]['grounding']['sources']
    assert not g['parts'][1]['grounding']['sources']
    assert len(state.active) == 1


async def test_scope_blocks_only_affected_task(data):
    _, g, state = await run(data, [task_for(data, '58aa31837615', ['fees']),
        task_for(data, '6d6862db57ba', ['fees'], scope=Scope(province='Hà Nội', quote='Hà Nội'))])
    assert g['parts'][0]['grounding']['sources']
    assert g['parts'][1]['kind'] == 'outside'
    assert not g['parts'][1]['grounding']['sources']
    assert len(state.active) == 2
    assert state.active[1].scope.province == 'Hà Nội'


async def test_outside_scope_survives_followup_without_local_evidence(data):
    task = task_for(data, '58aa31837615', ['required_documents'],
                    scope=Scope(province='Hà Nội', quote='Hà Nội'))
    _, first, state = await run(data, [task])
    assert state.focused == [task.code] and not first['sources']
    task = state.active[0].model_copy(update={'fields': ['fees'], 'quote': 'còn lệ phí?'})
    plan = validate_plan(Plan(relation='continue', tasks=[task]), 'còn lệ phí?', cards(data), state)
    _, second, state = await execute(data, plan, state, Filters(), SimpleNamespace(client=None), uuid4())
    assert second['parts'][0]['kind'] == 'outside' and not second['sources']


def test_same_procedure_two_localities_remain_distinct_in_state(data):
    a = task_for(data, '58aa31837615', ['fees'])
    b = task_for(data, '58aa31837615', ['fees'], scope=Scope(province='Hà Nội', quote='Hà Nội'))
    state = transition(State(corpus_version=data.version), Plan(relation='replace', tasks=[a, b]), [a.code])
    assert len(state.active) == 2
    assert state.active[0].scope != state.active[1].scope


async def test_missing_field_preserves_available(data):
    _, g, _ = await run(data, [task_for(data, 'c343c85df1af', ['fees', 'required_documents'])])
    part = g['parts'][0]['grounding']
    assert part['status'] == 'INSUFFICIENT_DATA'
    assert 'fees' in part['missing_information']
    assert 'required_documents' in {c['field'] for c in part['checklist']}


async def test_scholarship_conflict_never_becomes_checklist(data):
    text, g, _ = await run(data, [task_for(data, 'ff5c71001a91', ['required_documents'])])
    assert 'không dùng nó làm checklist' in text
    assert not g['parts'][0]['grounding']['checklist']


async def test_browse_is_not_legal_evidence(data):
    text, g, state = await run(data, [Task(kind='catalog', quote='nhà ở', domains=['housing'])])
    assert 'hôn nhân' not in text.casefold()
    assert not g['sources'] and state.displayed_options


async def test_clarification_does_not_render_generated_facts(data):
    task = Task(kind='clarify', quote='hỏi', question='Hãy nộp 999 triệu đồng nhé?',
                candidates=[task_for(data, '58aa31837615').code])
    text, g, _ = await run(data, [task])
    assert '999' not in text and not g['sources']


async def test_no_fields_asks_only_field(data):
    text, g, state = await run(data, [task_for(data, '58aa31837615')])
    assert g['missing_information'] == ['field_intents']
    assert len(state.active) == 1 and not g['sources']


def test_context_extend_and_partial_resolution(data):
    a = task_for(data, '58aa31837615', ['fees'])
    b = task_for(data, '6d6862db57ba', ['required_documents'])
    state = State(corpus_version=data.version, active=[a], focused=[a.code],
                  pending=[Task(kind='clarify', quote='x', candidates=[b.code])])
    next_state = transition(state, Plan(relation='extend', tasks=[b]), [b.code])
    assert {t.code for t in next_state.active} == {a.code, b.code}
    assert next_state.focused == [b.code] and next_state.pending == []
    # Prior state is not mutated before a successful turn is saved.
    assert len(state.active) == 1 and state.pending


def test_replacement_clears_old_context(data):
    a = task_for(data, '58aa31837615', ['fees'])
    state = State(corpus_version=data.version, active=[a])
    updated = transition(state, Plan(relation='replace', tasks=[Task(kind='outside', quote='visa')]), [])
    assert not updated.active


async def test_planner_receives_original_clauses_and_state(data):
    task = task_for(data, '34afa9c1ded0', ['required_documents'])
    query = 'Bỏ qua đi, đóng quán vài tháng để sửa rồi bán tiếp, không nghỉ hẳn.'
    task.quote = query
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': Plan(relation='replace', tasks=[task]).model_dump_json()}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        history = [SimpleNamespace(role='assistant', content='SENSITIVE_FACTUAL_ADDRESS'),
                   SimpleNamespace(role='user', content='Yêu cầu trước')]
        result = await understand(query, State(corpus_version=data.version), cards(data), history, client, uuid4())
    assert result.tasks[0].code == task.code
    assert captured[0]['messages'][-1] == {'role': 'user', 'content': query}
    assert 'CATALOG' in captured[0]['messages'][0]['content']
    assert 'SENSITIVE_FACTUAL_ADDRESS' not in json.dumps(captured)
    # G8 retains two structured subjects, not the raw historical user text.
    assert 'Yêu cầu trước' not in captured[0]['messages'][1]['content']
    assert len(captured) == 1 and 'json_schema' in captured[0]['response_format']


async def test_planner_bounded_failure_no_old_router(data):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(APIError) as exc:
            await understand('hỏi', State(corpus_version=data.version), cards(data), [], client, uuid4())
    assert exc.value.code == 'G7_PLAN_INVALID' and len(calls) == 2


def test_merge_fields_same_procedure_without_merging_scopes(data):
    a = task_for(data, '58aa31837615', ['fees'])
    b = task_for(data, '58aa31837615', ['required_documents'])
    c = task_for(data, '58aa31837615', ['fees'], scope=Scope(province='Hà Nội', quote='Hà Nội'))
    plan = validate_plan(Plan(relation='replace', tasks=[a, b, c]), 'câu hỏi Hà Nội', cards(data), State(corpus_version=data.version))
    assert len(plan.tasks) == 2
    assert plan.tasks[0].fields == ['fees', 'required_documents']


def test_model_selects_semantic_title_but_executor_uses_internal_code(data):
    catalog = cards(data)
    titles, schema = wire_contract(catalog)
    task = task_for(data, '55245834a3c0', ['fees'])
    expected = task.code
    task.code = titles[expected]
    plan = validate_plan(Plan(relation='replace', tasks=[task]), 'câu hỏi', catalog, State(corpus_version=data.version))
    assert plan.tasks[0].code == expected
    # Procedure branches require a real semantic code; catalog owns filters.
    assert set(schema['$defs']['Task']['oneOf'][0]['properties']['code']['enum']) == {*titles.values(), *SUPPORT_BOUNDARIES}
    assert to_wire({'code': expected, 'quote': expected}, titles) == {'code': titles[expected], 'quote': expected}
    assert to_wire(schema, titles) == schema, 'Mapping state values must not corrupt schema property definitions'


async def test_catalog_subset_does_not_expand_to_whole_domains(data):
    catalog = cards(data)
    loans = [c for c in catalog if c['label'].startswith('employment_loan_')]
    task = Task(kind='catalog', quote='hỏi vay', domains=['employment', 'business'],
                candidates=[c['label'] for c in loans])
    plan = validate_plan(Plan(relation='replace', tasks=[task]), 'hỏi vay', catalog, State(corpus_version=data.version))
    answer, g, state = await run(data, plan.tasks)
    assert all(c['title'] in answer for c in loans)
    assert 'Nội quy lao động' not in answer and 'thành lập hộ kinh doanh' not in answer
    assert state.displayed_options == [c['code'] for c in loans]
    assert not g['sources']  # listing is not a legal/evidence answer


def test_catalog_subset_rejects_unknown_id(data):
    task = Task(kind='catalog', quote='x', domains=['business'], candidates=['fake'])
    with pytest.raises(ValueError, match='UNKNOWN_CATALOG_CODE'):
        validate_plan(Plan(relation='replace', tasks=[task]), 'x', cards(data), State(corpus_version=data.version))


async def test_catalog_subset_without_broad_domain(data):
    task = task_for(data, '6d6862db57ba')
    plan = validate_plan(Plan(relation='replace', tasks=[Task(kind='catalog', quote='x', candidates=[task.code])]),
                         'x', cards(data), State(corpus_version=data.version))
    _, _, state = await run(data, plan.tasks)
    assert state.displayed_options == [task.code]


@pytest.mark.parametrize('code', list(SUPPORT_BOUNDARIES))
async def test_unavailable_need_cannot_execute_nearby_procedure(data, code):
    task = Task(kind='procedure', quote='hỏi', code=code, fields=['fees'])
    plan = validate_plan(Plan(relation='replace', tasks=[task]), 'hỏi', cards(data), State(corpus_version=data.version))
    assert plan.tasks[0].kind == 'outside' and not plan.tasks[0].code
    _, g, _ = await run(data, plan.tasks)
    assert not g['sources'] and not g['parts'][0]['procedure_id']
    assert all(c['code'] != code for c in cards(data))


def test_scope_quote_typography_preserves_diacritics_and_original_span(data):
    task = task_for(data, '58aa31837615', ['fees'],
        scope=Scope(ward='Tăng Nhơn Phú', quote='hồ sơ tại Tăng Nhơn Phú'))
    plan = validate_plan(Plan(relation='replace', tasks=[task]), 'Hồ sơ tại Tăng Nhơn Phú?',
                         cards(data), State(corpus_version=data.version))
    assert plan.tasks[0].scope.quote == 'Hồ sơ tại Tăng Nhơn Phú'
    task.scope.quote = 'ho so tai Tang Nhon Phu'
    with pytest.raises(ValueError, match='SCOPE_WITHOUT_USER_EVIDENCE'):
        validate_plan(plan, 'Hồ sơ tại Tăng Nhơn Phú?', cards(data), State(corpus_version=data.version))


def test_new_scope_wire_inherits_legacy_scope_only_for_same_procedure(data):
    old = task_for(data, '58aa31837615', ['fees'], scope=Scope(province='Hà Nội', quote='tại Hà Nội'))
    state = State(corpus_version=data.version, active=[old], focused=[old.code])
    task = old.model_copy(update={'scope': Scope(places=['Hà Nội'], quote='tại Hà Nội')})
    plan = Plan(relation='continue', tasks=[task])
    validate_plan(plan, 'còn hồ sơ?', cards(data), state)
    task.code = task_for(data, '6d6862db57ba').code
    with pytest.raises(ValueError, match='SCOPE_WITHOUT_USER_EVIDENCE'):
        validate_plan(plan, 'còn hồ sơ?', cards(data), state)


async def test_failed_understanding_invalidates_derived_focus(data, monkeypatch):
    from app.rag.g7 import workflow
    task = task_for(data, '6d6862db57ba', ['fees'])
    previous = State(corpus_version=data.version, active=[task], focused=[task.code])
    conversation = SimpleNamespace(rag_context=previous.model_dump())
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(workflow, 'load_active_dataset', lambda *args: data)
    monkeypatch.setattr(workflow, 'understand', AsyncMock(side_effect=APIError(502, 'G7_PLAN_INVALID', 'failed')))
    answer, g, returned_state = await workflow.answer_turn(query='Một chủ đề mới', raw_state=previous.model_dump(), history=[],
        filters=Filters(), ai=SimpleNamespace(client=None), request_id=uuid4(), db=db, conversation=conversation)
    actual = State.model_validate(conversation.rag_context)
    assert actual.awaiting_rephrase and not actual.focused and not actual.active
    assert returned_state.awaiting_rephrase
    assert g['status'] == 'NEED_CLARIFICATION' and not g['sources']
    assert g['provider'] == 'g7-safe-clarification'
    assert g['missing_information'] == ['intent_resolution']
    assert g['parts'][0]['procedure_id'] is None and g['parts'][0]['kind'] == 'clarify'
    assert 'địa phương' in answer
    db.commit.assert_awaited_once()


async def test_planner_outage_remains_retryable_error(data, monkeypatch):
    from app.rag.g7 import workflow
    state = State(corpus_version=data.version)
    conversation = SimpleNamespace(rag_context=state.model_dump())
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(workflow, 'load_active_dataset', lambda *args: data)
    monkeypatch.setattr(workflow, 'understand', AsyncMock(side_effect=APIError(503, 'G7_PLANNER_UNAVAILABLE', 'offline')))
    with pytest.raises(APIError) as error:
        await workflow.answer_turn(query='hỏi', raw_state=state.model_dump(), history=[],
            filters=Filters(), ai=SimpleNamespace(client=None), request_id=uuid4(), db=db, conversation=conversation)
    assert error.value.code == 'G7_PLANNER_UNAVAILABLE'


@pytest.mark.parametrize('suffix', list(DESCRIPTIONS))
async def test_every_procedure_evidence_bound_to_own_id(data, suffix):
    _, g, _ = await run(data, [task_for(data, suffix, ['required_documents', 'fees'])])
    assert all(s['fragment_id'].split(':')[0] == 'd2_title_' + suffix for s in g['sources'])
