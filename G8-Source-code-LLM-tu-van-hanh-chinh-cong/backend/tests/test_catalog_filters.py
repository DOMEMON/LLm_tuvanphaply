from types import SimpleNamespace
from uuid import uuid4
import pytest


@pytest.mark.asyncio
async def test_filtered_catalog_has_evidence_and_exact_menu(data):
    from app.rag.g7.catalog import cards
    from app.rag.g7.contracts import State
    from app.rag.g7.workflow import execute
    from app.rag.g8.turn_checks import explicit_catalog
    from app.rag.g8.graph import verify_evidence
    from app.rag.schemas import Filters
    state = State(corpus_version=data.version)
    query = 'Các thủ tục hộ tịch chỉ có thể nộp qua online'
    plan = explicit_catalog(query, state, cards(data))
    result = await execute(data, plan, state, Filters(), SimpleNamespace(client=None), uuid4())
    _, g, next_state = result
    assert g['status'] == 'ANSWER' and g['sources']
    expected = next(c['code'] for c in cards(data) if c['label'] == 'confirm_marital_status')
    assert next_state.displayed_options == [expected]
    assert verify_evidence({'result': result})
    g['parts'][0]['grounding']['sources'][0]['fragment_id'] = 'other:submission_methods:1'
    with pytest.raises(Exception, match='Nguồn lọc'):
        verify_evidence({'result': result})


@pytest.mark.asyncio
async def test_only_direct_and_method_definition_preserve_menu(data):
    from app.rag.g7.catalog import cards
    from app.rag.g7.contracts import State
    from app.rag.g7.workflow import execute
    from app.rag.g8.turn_checks import explicit_catalog
    from app.rag.g8.graph import verify_evidence
    from app.rag.schemas import Filters
    state = State(corpus_version=data.version)
    catalog = cards(data)
    query = 'cho tôi những thủ tục chỉ có thể làm trực tiếp nhưng thuộc lĩnh vực xã hội - văn hóa'
    plan = explicit_catalog(query, state, catalog)
    result = await execute(data, plan, state, Filters(), SimpleNamespace(client=None), uuid4())
    text, g, state = result
    assert g['status'] == 'ANSWER' and verify_evidence({'result': result})
    assert len(state.displayed_options) == 3
    titles = [c['title'] for c in catalog if c['code'] in state.displayed_options]
    assert any('THCS' in t for t in titles)
    assert any('Tổ quốc ghi công' in t for t in titles)
    assert any('học bổng' in t for t in titles)
    assert state.catalog_context['predicates'][0]['operator'] == 'equals'
    menu = state.displayed_options[:]
    plan = explicit_catalog('cả hai là sao', state, catalog)
    assert plan.relation == 'continue'
    result = await execute(data, plan, state, Filters(), SimpleNamespace(client=None), uuid4())
    text, g, state = result
    assert 'cả trực tuyến (online) và trực tiếp' in text
    assert g['status'] == 'ANSWER' and g['sources']
    assert verify_evidence({'result': result})
    assert state.displayed_options == menu
    assert 'explain_value' not in state.catalog_context
    # No global trigger, and no swallowing another supported request.
    assert explicit_catalog('cả hai là sao', State(corpus_version=data.version), catalog) is None
    assert explicit_catalog('cả hai là sao và kết hôn bao lâu', state, catalog) is None


def test_exclusive_forms_do_not_change_inclusive_filter():
    from app.rag.g8.catalog_browse import request_for
    from app.rag.g7.contracts import State
    state = State(corpus_version='test')
    for action in ('nộp', 'làm', 'thực hiện'):
        r = request_for(f'Những thủ tục xã hội văn hóa chỉ có thể {action} trực tiếp', state)
        assert r['predicates'][0]['operator'] == 'equals'
    r = request_for('Những thủ tục xã hội văn hóa có thể làm trực tiếp', state)
    assert r['predicates'][0]['operator'] == 'contains_all'
