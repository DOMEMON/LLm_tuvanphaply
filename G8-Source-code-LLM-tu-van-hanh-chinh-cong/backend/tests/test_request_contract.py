"""Need boundaries must not guess a subject, substitute a field, or break context."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.rag.g7.catalog import cards
from app.rag.g7.contracts import Plan, State, Task
from app.rag.g8.request_contract import (
    UNBOUND, UNSUPPORTED, bare_unbound_reference, enforce, preflight,
    unsupported_information,
)
from app.rag.g8.turn_checks import check_turn, requested_fields


@pytest.mark.parametrize('question', [
    'Lệ phí là bao nhiêu?', 'Cần giấy tờ gì?', 'Nộp ở đâu?',
    'Thủ tục đó làm bao lâu?', 'Cái thứ hai cần hồ sơ gì?',
    'Cho tôi hỏi thời gian giải quyết?', 'Phương thức nộp hồ sơ là gì?',
    'Tôi muốn biết cần chuẩn bị giấy tờ gì.',
    'Cho mình hỏi lệ phí của thủ tục này?', 'Tôi cần mang giấy tờ gì?',
    'Cái thứ ba có lệ phí không?',
    'Cho tôi hỏi thời hạn giải quyết?', 'Thời hạn là bao lâu?',
    'Cho tôi biết điều kiện?', 'Đối tượng nào?',
])
def test_empty_context_cannot_invent_a_procedure(question):
    state = State(corpus_version='test')
    assert bare_unbound_reference(question, state)
    plan = preflight(question, state, [])
    assert len(plan.tasks) == 1 and plan.tasks[0].question == UNBOUND


@pytest.mark.parametrize('question', [
    'Khai sinh cần giấy tờ gì?', 'Tôi mới sinh con cần giấy tờ gì?',
    'Tôi muốn mở quán kinh doanh, cần hồ sơ gì?',
    'Thủ tục nào không mất phí?', 'Các thủ tục nào chỉ nộp trực tiếp?',
    'Tôi kết hôn với người Nhật cần giấy tờ gì?',
    'Thủ tục nào mất phí?', 'Thủ tục nào có lệ phí?',
])
def test_purpose_specific_or_catalog_question_still_uses_planner(question):
    assert preflight(question, State(corpus_version='test'), []) is None


@pytest.mark.parametrize('state', [
    State(corpus_version='test', active=[Task(kind='procedure', quote='x', code='A')]),
    State(corpus_version='test', focused=['A']),
    State(corpus_version='test', displayed_options=['A', 'B']),
    State(corpus_version='test', pending=[Task(kind='clarify', quote='x', candidates=['A'])]),
])
def test_bound_followup_is_not_blocked(state):
    assert preflight('Cần giấy tờ gì?', state, []) is None


@pytest.mark.asyncio
async def test_unbound_preflight_never_calls_model(data):
    from app.rag.g7.planner import understand

    class NoModel:
        def __getattr__(self, name):
            raise AssertionError('Unbound reference must not call model: ' + name)

    plan = await understand('Lệ phí là bao nhiêu?', State(corpus_version=data.version),
                            cards(data), [], NoModel(), uuid4())
    assert plan.tasks[0].question == UNBOUND


@pytest.mark.parametrize('question', [
    'Số điện thoại cán bộ xử lý đăng ký khai sinh là gì?',
    'Cho tôi email cán bộ nhận hồ sơ đăng ký kết hôn.',
    'Xin hotline của cán bộ tiếp nhận đăng ký khai tử.',
    'Tên người thụ lý đăng ký khai sinh là gì?',
    'Giờ mở cửa làm thủ tục đăng ký kết hôn là khi nào?',
])
def test_known_unsupported_need_is_not_replaced_by_location(data, question):
    catalog = cards(data)
    birth = next(c for c in catalog if c['label'] == 'birth_registration')
    state = State(corpus_version=data.version)
    wrong = Plan(relation='replace', tasks=[Task(kind='procedure', quote=question,
        code=birth['code'], fields=['receiving_authority'])])
    result = check_turn(wrong, question, catalog, state)
    assert [(t.kind, t.question) for t in result.tasks] == [('clarify', UNSUPPORTED)]
    assert not result.tasks[0].fields


@pytest.mark.parametrize('question', [
    'Hồ sơ khai sinh có cần ghi số điện thoại không?',
    'Có phải điền email vào tờ khai trong hồ sơ khai sinh không?',
    'Không hỏi số điện thoại, chỉ hỏi lệ phí khai sinh.',
    'Bỏ qua email, cho tôi nơi nộp khai sinh.',
])
def test_contact_datum_or_negation_is_not_contact_request(question):
    assert not unsupported_information(question)


@pytest.mark.parametrize('prefix', ['Hình thức', 'Phương thức', 'Cách', 'Kênh'])
def test_submission_object_is_not_document_contents(prefix):
    assert requested_fields(prefix + ' nộp hồ sơ là gì?') == ['submission_methods']
    assert requested_fields('Hồ sơ gồm gì và ' + prefix.lower() + ' nộp hồ sơ?') == [
        'required_documents', 'submission_methods']


def test_unsupported_sibling_does_not_destroy_fee_need(data):
    catalog = cards(data)
    birth = next(c for c in catalog if c['label'] == 'birth_registration')
    query = 'Lệ phí khai sinh là bao nhiêu; số điện thoại cán bộ xử lý là gì?'
    first, second = query.split('; ')
    wrong = Plan(relation='replace', tasks=[
        Task(kind='procedure', quote=first, code=birth['code'], fields=['fees']),
        Task(kind='procedure', quote=second, code=birth['code'], fields=['receiving_authority']),
    ])
    result = enforce(wrong, query, State(corpus_version=data.version), catalog)
    assert len(result.tasks) == 2
    assert result.tasks[0].kind == 'procedure' and result.tasks[0].fields == ['fees']
    assert result.tasks[1].question == UNSUPPORTED


def test_compound_full_quote_keeps_fee_and_explicit_refusal(data):
    catalog = cards(data)
    birth = next(c for c in catalog if c['label'] == 'birth_registration')
    query = 'Lệ phí khai sinh và số điện thoại cán bộ xử lý là gì?'
    wrong = Plan(relation='replace', tasks=[Task(kind='procedure', quote=query,
        code=birth['code'], fields=['fees', 'receiving_authority'])])
    result = enforce(wrong, query, State(corpus_version=data.version), catalog)
    assert len(result.tasks) == 2
    supported = next(t for t in result.tasks if t.kind == 'procedure')
    assert supported.fields == ['fees']
    assert any(t.question == UNSUPPORTED for t in result.tasks)


@pytest.mark.asyncio
async def test_rendered_refusal_has_no_unrelated_facts_and_clears_subject(data):
    from app.rag.g7.workflow import execute
    from app.rag.schemas import Filters
    catalog = cards(data)
    birth = next(c for c in catalog if c['label'] == 'birth_registration')
    state = State(corpus_version=data.version, active=[Task(kind='procedure', quote='x', code=birth['code'])],
                  focused=[birth['code']])
    query = 'Số điện thoại cá nhân của cán bộ đăng ký khai sinh là gì?'
    plan = preflight(query, state, catalog)
    answer, grounding, new_state = await execute(data, plan, state, Filters(),
        SimpleNamespace(client=None), uuid4())
    assert 'không dùng một trường thông tin khác' in answer
    part = grounding['parts'][0]['grounding']
    assert not part['sources'] and not part['checklist']
    assert part['missing_information'] == ['unsupported_information_need']
    assert not new_state.active and not new_state.focused
