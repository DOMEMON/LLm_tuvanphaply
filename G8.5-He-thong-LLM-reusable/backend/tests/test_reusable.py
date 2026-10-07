"""Domain-neutral regression tests; no downloaded dataset and no live model."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.rag.g85 import engine
from app.rag.g85.contracts import Plan, State, Task, current_package_history, load_state, transition
from app.rag.g85.package_loader import read_package
from app.rag.g85.planner import validate_plan
from tools.ingest import ingest
from tools.validate import SCHEMA

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def package(tmp_path):
    profile = json.loads((ROOT / 'profiles/documents.template.json').read_text(encoding='utf-8'))
    profile['id'] = 'synthetic_devices'
    profile['title'] = 'Tài liệu giả lập — kiểm thử phần mềm'
    profile['scope']['description'] = 'Hỏi đáp về thiết bị và quy trình trong tài liệu giả lập.'
    ingest(ROOT / 'examples/documents.jsonl', profile,
           {'id': 'id', 'title': 'title', 'text': 'text'}, 'test-v1', tmp_path / 'package', reviewed=True)
    return read_package(tmp_path / 'package', SCHEMA)

def task(entity='alpha', quote='Kiểm tra Alpha mất bao lâu?'):
    return Task(kind='answer', entity=entity, quote=quote, mode='search', search_query=quote)

def test_no_domain_policies_and_independent_entity_search(package):
    assert package.profile['policies'] == []
    assert package.policy is None and package.direct is None
    assert len(package.catalog) == 3
    results = package.search('kiểm tra', entity='alpha')
    assert results and {p.entity for p in results} == {'alpha'}

def test_requires_explicit_source_review(tmp_path, package):
    with pytest.raises(ValueError, match='SOURCE_REVIEW_REQUIRED'):
        ingest(ROOT / 'examples/documents.jsonl', package.profile, {}, 'test', tmp_path / 'new')

def test_package_checksum_tampering_is_rejected(tmp_path, package):
    target = tmp_path / 'package/passages.jsonl'
    target.write_text(target.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='CHECKSUM_MISMATCH'):
        read_package(tmp_path / 'package', SCHEMA)

@pytest.mark.parametrize('field,value', [('dataset_version','other'),('profile_id','other'),('profile_version','other')])
def test_switching_package_resets_context(package, field, value):
    state = load_state(None, package.profile, package.version)
    state.focused = ['alpha']
    raw = state.model_dump()
    raw[field] = value
    assert load_state(raw, package.profile, package.version).focused == []
    assert current_package_history([SimpleNamespace(role='user', content='old')], raw,
                                   package.profile, package.version) == []

def test_followup_retains_menu_order(package):
    state = load_state(None, package.profile, package.version)
    state = transition(state, Plan(relation='replace', tasks=[task('beta'), task('alpha')]), ['beta','alpha'])
    state = transition(state, Plan(relation='continue', tasks=[task('alpha')]), ['alpha'])
    assert state.focused == ['alpha']
    assert state.displayed_options == ['beta', 'alpha']

def test_unrelated_outside_turn_does_not_destroy_focus(package):
    state = load_state(None, package.profile, package.version)
    state.focused = ['alpha']
    updated = transition(state, Plan(relation='replace', tasks=[Task(kind='outside',quote='Mua vé giúp tôi')]), [])
    assert updated.focused == ['alpha']

@pytest.mark.parametrize('entity', ['collection','unknown'])
def test_planner_rejects_non_catalog_entity(package, entity):
    state = load_state(None, package.profile, package.version)
    plan = Plan(relation='replace', tasks=[task(entity)])
    with pytest.raises(ValueError):
        validate_plan(plan.model_dump_json(), plan.tasks[0].quote, package, state)

def test_source_slices_preserve_condition(package):
    text = package.search('kiểm tra', entity='alpha')[0].text
    segments = engine.evidence_segments(text)
    assert all(s in text for s in segments)
    assert any('hoặc đến khi đèn báo xanh' in s for s in segments)
    assert engine.source_quote('kiểm tra chỉ mất 12 phút', text) is None

@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [[999], [True], [0]])
async def test_invalid_segment_ids_are_not_rendered(package, monkeypatch, ids):
    passage = package.search('kiểm tra', entity='alpha')[0]
    async def complete(*args, **kwargs):
        return json.dumps({'results':[{'task_id':'t1','support_check':'test','support':'complete',
            'missing_needs':[],'excerpts':[{'evidence_id':passage.id,'segment_ids':ids}]}]})
    monkeypatch.setattr(engine, 'complete', complete)
    result = await engine.extract_answers(None, package, {'t1':('kiểm tra', [passage])}, 'kiểm tra', uuid4())
    assert 't1' not in result

@pytest.mark.asyncio
async def test_cross_task_citation_rejected_without_erasing_other_task(package, monkeypatch):
    first = package.search('kiểm tra', entity='alpha')[0]
    second = package.search('kiểm tra', entity='beta')[0]
    async def complete(*args, **kwargs):
        return json.dumps({'results':[
            {'task_id':'t1','support_check':'test','support':'complete','missing_needs':[],
             'excerpts':[{'evidence_id':second.id,'segment_ids':[1]}]},
            {'task_id':'t2','support_check':'test','support':'complete','missing_needs':[],
             'excerpts':[{'evidence_id':second.id,'segment_ids':[1]}]}]})
    monkeypatch.setattr(engine, 'complete', complete)
    result = await engine.extract_answers(None, package,
        {'t1':('Alpha',[first]),'t2':('Beta',[second])}, 'Alpha và Beta', uuid4())
    assert 't1' not in result
    assert result['t2'].excerpts[0][1].entity == 'beta'

@pytest.mark.asyncio
async def test_absent_value_does_not_render_related_text(package, monkeypatch):
    passage = package.search('kiểm tra', entity='beta')[0]
    async def complete(*args, **kwargs):
        return json.dumps({'results':[{'task_id':'t1','support_check':'test','support':'absent',
            'missing_needs':['mức tiêu thụ điện'],
            'excerpts':[{'evidence_id':passage.id,'segment_ids':[1]}]}]})
    monkeypatch.setattr(engine, 'complete', complete)
    result = await engine.extract_answers(None, package,
        {'t1':('mức tiêu thụ điện',[passage])}, 'Cho mức tiêu thụ điện của Beta', uuid4())
    assert result['t1'].excerpts == []
    assert result['t1'].missing == ['mức tiêu thụ điện']

def test_profile_with_uninstalled_policy_fails_closed(tmp_path, package):
    profile = copy.deepcopy(package.profile)
    profile['policies'] = ['uninstalled']
    with pytest.raises(ValueError, match='POLICY_NOT_INSTALLED'):
        ingest(ROOT/'examples/documents.jsonl', profile, {'id':'id','title':'title','text':'text'},
               'bad-policy', tmp_path/'bad', reviewed=True)
