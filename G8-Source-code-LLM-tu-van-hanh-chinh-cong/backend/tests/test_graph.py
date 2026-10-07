"""Regression gates on the actual prepared G8 graph, with no live model."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx

from app.errors import APIError
from app.rag.g7.catalog import cards
from app.rag.g7.contracts import Plan, Scope, State, Task
from app.rag.g7.planner import validate_plan, reconcile_plan, _bind_explicit_service_place
from app.rag.g7.workflow import execute
from app.rag.g8.graph import plan_with_graph, verify_evidence
from app.rag.g8.guards import semantic_check
from app.rag.g8.turn_checks import check_turn


def test_multi_negative_fee_never_overwrites_three_tasks(data):
    catalog = cards(data)
    labels = ['birth_registration', 'marriage_domestic', 'house_number_assignment']
    fields = ['required_documents', 'receiving_authority', 'processing_times']
    query = 'Khai sinh cần mang giấy gì, kết hôn thì nộp ở đâu, còn cấp số nhà mất bao lâu? Đừng trả lệ phí vì tôi chưa hỏi.'
    tasks = [Task(kind='procedure', quote=query, code=next(c['code'] for c in catalog if c['label'] == label),
                  fields=[field]) for label, field in zip(labels, fields)]
    result = reconcile_plan(Plan(relation='replace', tasks=tasks), query, catalog, State(corpus_version=data.version))
    assert [t.fields for t in result.tasks] == [[f] for f in fields]
    assert len(result.tasks) == 3


def test_menu_survives_drilldown_but_not_unrelated_switch(data):
    from app.rag.g7.workflow import transition
    state = State(corpus_version=data.version, displayed_options=['T03', 'T12', 'T36'])
    task = Task(kind='procedure', code='T36', quote='mục cuối')
    result = transition(state, Plan(relation='continue', tasks=[task]), ['T36'])
    assert result.displayed_options == state.displayed_options
    task.code = 'T10'
    result = transition(result, Plan(relation='replace', tasks=[task]), ['T10'])
    assert result.displayed_options == ['T10']


def test_multi_jurisdictions_bound_to_individual_quotes(data):
    from app.rag.g7.workflow import allowed_scope
    q = 'Hồ sơ khai sinh tại Tăng Nhơn Phú. Kết hôn tại Nhật Bản cần gì?'
    tasks = [Task(kind='procedure', code='T12', quote='Hồ sơ khai sinh tại Tăng Nhơn Phú', fields=['required_documents']),
             Task(kind='procedure', code='T03', quote='Kết hôn tại Nhật Bản cần gì')]
    result = check_turn(Plan(relation='replace', tasks=tasks), q, cards(data), State(corpus_version=data.version))
    assert allowed_scope(result.tasks[0].scope)
    assert not allowed_scope(result.tasks[1].scope)
    result.tasks[1].quote = q
    assert check_turn(result, q, cards(data), State(corpus_version=data.version))
    result.tasks[1].scope = Scope()
    with pytest.raises(ValueError, match='MULTI_LOCATION'):
        check_turn(result, q, cards(data), State(corpus_version=data.version))


def test_background_document_does_not_justify_switch(data):
    q = 'Giấy khai sinh tôi có rồi, trước đây cũng từng ly hôn. Vậy cần bổ sung giấy nào và giải quyết bao lâu?'
    state = State(corpus_version=data.version, focused=['T18'])
    task = Task(kind='procedure', code='T36', quote=q, fields=['required_documents', 'processing_times'])
    with pytest.raises(ValueError, match='BACKGROUND_DOCUMENT'):
        check_turn(Plan(relation='replace', tasks=[task]), q, cards(data), state)
    task.code = 'T18'
    assert check_turn(Plan(relation='continue', tasks=[task]), q, cards(data), state)


def test_days_must_not_reuse_fee(data):
    q = 'xin so nha thi mat may ngay?'
    plan = Plan(relation='continue', tasks=[Task(kind='procedure', code='T10', quote=q, fields=['fees'])])
    with pytest.raises(ValueError, match='FIELDS_MUST_MATCH'):
        check_turn(plan, q, cards(data), State(corpus_version=data.version))


def test_field_guard_all_catalog_titles_and_ordinals(data):
    catalog = cards(data)
    for card in catalog:
        q = 'Tôi hỏi thành phần hồ sơ của thủ tục: ' + card['title']
        plan = Plan(relation='replace', tasks=[Task(kind='procedure', code=card['code'], quote=q,
                    fields=['required_documents'])])
        assert check_turn(plan, q, catalog, State(corpus_version=data.version))
    from app.rag.g8.turn_checks import requested_fields
    assert requested_fields('Cái đầu tiên cần giấy tờ gì?') == ['required_documents']
    assert set(requested_fields('Ờ, hồ sơ với tiền thôi nhé.')) == {'required_documents', 'fees'}
    assert requested_fields('Quay sang khai sinh, chỗ nào nhận hồ sơ?') == ['receiving_authority']
    assert requested_fields('Hồ sơ thôi, khỏi nói nơi nộp nữa.') == ['required_documents']


def test_real_switch_allowed_despite_background_paper(data):
    q = 'Giấy khai sinh tôi có rồi. Giờ chuyển sang đăng ký nhận cha mẹ con, cần bổ sung giấy nào?'
    state = State(corpus_version=data.version, focused=['T18'])
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', code='T36', quote=q,
                 fields=['required_documents'])])
    assert check_turn(plan, q, cards(data), state).tasks[0].code == 'T36'


def test_explicit_catalog_request_not_treated_as_correction(data):
    q = 'Tôi đang hỏi các thủ tục hộ tịch.'
    plan = Plan(relation='replace', tasks=[Task(kind='catalog', quote=q, domains=['civil'])])
    assert check_turn(plan, q, cards(data), State(corpus_version=data.version))


def test_unaccented_unresolved_house_is_not_automatically_number(data):
    state = State(corpus_version=data.version, pending=[Task(kind='clarify', quote='Tôi hỏi sổ nhà')])
    q = 'xin so nha thi mat may ngay?'
    plan = Plan(relation='continue', tasks=[Task(kind='procedure', code='T10', quote=q, fields=['processing_times'])])
    assert check_turn(plan, q, cards(data), state).tasks[0].kind == 'clarify'
    q = 'Ý tôi là số nhà địa chỉ, mất mấy ngày?'
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', code='T10', quote=q, fields=['processing_times'])])
    assert check_turn(plan, q, cards(data), state).tasks[0].kind == 'procedure'


def test_local_service_not_refused_because_second_task_outside(data):
    q = 'Hồ sơ khai sinh tại Tăng Nhơn Phú. Kết hôn tại Nhật Bản cần gì?'
    plan = Plan(relation='replace', tasks=[Task(kind='outside', quote=q)])
    with pytest.raises(ValueError, match='LOCAL_SUPPORTED_SERVICE'):
        check_turn(plan, q, cards(data), State(corpus_version=data.version))


async def test_graph_one_call_valid_and_request_isolated():
    calls = []
    async def completion(messages):
        value = messages[-1]['content']
        calls.append(value)
        await asyncio.sleep(0)
        return value
    def parser(raw):
        return Plan(relation='replace', tasks=[Task(kind='chat', quote=raw)])
    results = await asyncio.gather(*(plan_with_graph([{'role': 'user', 'content': q}],
        completion, parser, uuid4()) for q in ['A', 'B']))
    assert calls == ['A', 'B']
    assert [p.tasks[0].quote for p in results] == ['A', 'B']


async def test_graph_repairs_semantic_failure_once():
    calls = []
    async def completion(messages):
        calls.append(messages)
        return 'bad' if len(calls) == 1 else 'good'
    def parser(raw):
        if raw == 'bad':
            raise ValueError('SCOPE_WITHOUT_USER_EVIDENCE')
        return Plan(relation='replace', tasks=[Task(kind='chat', quote='good')])
    result = await plan_with_graph([{'role': 'user', 'content': 'question'}], completion, parser, uuid4())
    assert len(calls) == 2 and result.tasks[0].kind == 'chat'
    assert calls[1][-1]['content'] == 'question'
    assert 'SCOPE_WITHOUT_USER_EVIDENCE' in calls[1][0]['content']


async def test_graph_cancel_propagates():
    started = asyncio.Event()
    async def cancel(messages):
        started.set()
        await asyncio.Event().wait()
    job = asyncio.create_task(plan_with_graph([], cancel, lambda raw: None, uuid4()))
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job


async def test_transport_failure_is_not_a_plan_repair():
    calls = 0
    async def unavailable(messages):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError('offline')
    with pytest.raises(httpx.ConnectError):
        await plan_with_graph([], unavailable, lambda raw: None, uuid4())
    assert calls == 1


def test_whitespace_scope_is_original_user_evidence(data):
    query = 'Đăng ký khai tử ở địa phương Tăng Nhơn Phú  rồi mai táng nơi khác.'
    card = next(c for c in cards(data) if c['label'] == 'death_registration')
    task = Task(kind='procedure', code=card['code'], quote=query, fields=['required_documents'],
                scope=Scope(places=['Tăng Nhơn Phú'], quote='ở địa phương Tăng Nhơn Phú rồi mai táng'))
    result = validate_plan(Plan(relation='replace', tasks=[task]), query, cards(data), State(corpus_version=data.version))
    assert result.tasks[0].scope.quote in query
    task.scope = Scope(places=['Hà Nội'], quote='ở địa phương Hà Nội')
    with pytest.raises(ValueError, match='SCOPE_WITHOUT_USER_EVIDENCE'):
        validate_plan(result, query, cards(data), State(corpus_version=data.version))


def test_broad_civil_does_not_reuse_marriage(data):
    catalog = cards(data)
    marriage = next(c for c in catalog if c['label'] == 'marriage_domestic')
    plan = Plan(relation='continue', tasks=[Task(kind='procedure', code=marriage['code'], quote='hộ tịch')])
    with pytest.raises(ValueError, match='BROAD_DOMAIN'):
        semantic_check(plan, 'Tôi muốn làm thủ tục hộ tịch, cần giấy tờ gì?', catalog)
    # A genuinely named procedure and explicit reference must not be blocked.
    assert semantic_check(plan, 'Hộ tịch này cần thêm gì?', catalog) is plan
    assert semantic_check(plan, 'Trong hộ tịch, kết hôn cần giấy gì?', catalog) is plan
    listing = Plan(relation='replace', tasks=[Task(kind='catalog', domains=['civil'], quote='hộ tịch')])
    assert semantic_check(listing, 'Liệt kê thủ tục hộ tịch', catalog) is listing
    listing.tasks[0].domains = ['civil', 'birth', 'marriage']
    assert semantic_check(listing, 'Liệt kê thủ tục hộ tịch', catalog).tasks[0].domains == ['civil']
    listing.tasks[0].candidates = [c['code'] for c in catalog if 'civil' in c['domains']][:5]
    expanded = semantic_check(listing, 'Liệt kê tất cả thủ tục hộ tịch', catalog)
    assert expanded.tasks[0].candidates == [] and expanded.tasks[0].domains == ['civil']
    listing.tasks[0].domains = ['civil', 'death']
    with pytest.raises(ValueError, match='CIVIL_DOMAIN'):
        semantic_check(listing, 'Liệt kê thủ tục hộ tịch', catalog)


def test_marriage_catalog_uses_membership_not_parent_civil_domain(data):
    catalog = cards(data)
    task = Task(kind='catalog', quote='liên quan đến kết hôn', domains=['civil', 'marriage'],
                candidates=['T18', 'T03', 'T26', 'T36', 'T15'])
    plan = Plan(relation='replace', tasks=[task])
    result = semantic_check(plan, 'Có những thủ tục nào liên quan đến kết hôn?', catalog)
    assert result.tasks[0].domains == ['marriage']
    assert result.tasks[0].candidates == ['T18', 'T03', 'T26']
    task = Task(kind='catalog', quote='hộ tịch', domains=['civil'])
    plan = Plan(relation='replace', tasks=[task])
    assert semantic_check(plan, 'Liệt kê các thủ tục hộ tịch.', catalog).tasks[0].domains == ['civil']


async def test_unsafe_boundary_has_no_citations_or_checklist(data):
    query = 'Làm giả giấy tờ để che giấu giết người'
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', quote=query, code='unsupported_conceal_harm')])
    state = State(corpus_version=data.version)
    plan = validate_plan(plan, query, cards(data), state)
    # Idempotent validation must preserve backend-owned refusal reason.
    plan = validate_plan(plan, query, cards(data), state)
    from app.rag.schemas import Filters
    answer, grounding, _ = await execute(data, plan, state, Filters(), SimpleNamespace(client=None), uuid4())
    assert 'không hỗ trợ che giấu' in answer
    assert not grounding['sources'] and not grounding.get('checklist')


def test_death_browsing_guard_cannot_undo_refusal(data):
    query = 'Liệt kê các thủ tục sau khi tôi giết người rồi ngụy tạo tai nạn để làm khai tử.'
    task = Task(kind='procedure', quote=query, code='unsupported_conceal_harm')
    state = State(corpus_version=data.version)
    plan = validate_plan(Plan(relation='replace', tasks=[task]), query, cards(data), state)
    result = reconcile_plan(plan, query, cards(data), state)
    assert result.tasks[0].kind == 'outside'


def test_evidence_gate_rejects_cross_procedure():
    part = {'kind': 'procedure', 'procedure_id': 'birth', 'grounding': {'sources': [
        {'fragment_id': 'marriage:fees:001'}]}}
    with pytest.raises(APIError) as exc:
        verify_evidence({'result': ('answer', {'parts': [part]}, None)})
    assert exc.value.code == 'G8_EVIDENCE_BINDING'


def test_explicit_documents_only_field_guard_preserves_other_fields(data):
    catalog = cards(data)
    card = next(c for c in catalog if c['label'] == 'resistance_participant_benefit')
    def plan():
        return Plan(relation='replace', tasks=[Task(kind='procedure', code=card['code'],
            quote='hồ sơ', fields=['required_documents', 'processing_times'])])
    q = 'Tôi muốn biết hồ sơ giải quyết chế độ người hoạt động kháng chiến giải phóng dân tộc.'
    assert semantic_check(plan(), q, catalog).tasks[0].fields == ['required_documents']
    assert semantic_check(plan(), q + ' Và thời gian giải quyết?', catalog).tasks[0].fields == [
        'required_documents', 'processing_times']


def test_uncertain_procedure_still_respects_explicit_geographic_boundary(data):
    from app.rag.g7.workflow import allowed_scope
    for place, allowed in [('Nhật Bản', False), ('phường Tăng Nhơn Phú', True)]:
        query = f'Tôi muốn đăng ký tại {place}, cần giấy gì?'
        plan = Plan(relation='replace', tasks=[Task(kind='clarify', quote=query)])
        plan = _bind_explicit_service_place(plan, query)
        assert plan.tasks[0].kind == 'clarify'
        assert allowed_scope(plan.tasks[0].scope) is allowed
