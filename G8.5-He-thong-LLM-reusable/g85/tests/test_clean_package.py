import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from g85.runtime.contracts import Plan, State, Task, load_state
from g85.runtime.package_loader import read_package
from g85.runtime.planner import validate_plan
from g85.runtime.retriever import PackageRetriever, retrieve

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'g85/schema/domain-profile.schema.json'


def test_latest_multi_subject_display_replaces_old_menu_order():
    from g85.runtime.contracts import transition
    state = State(dataset_version='v', profile_id='devices', profile_version='1',
                  displayed_options=['item1', 'item0'])
    plan = Plan(relation='continue', tasks=[Task(kind='answer', quote='manuals', entity=e)
                                          for e in ['item0', 'item1']])
    updated = transition(state, plan, ['item0', 'item1'])
    assert updated.displayed_options == ['item0', 'item1']
    plan.tasks = [plan.tasks[1]]
    assert transition(updated, plan, ['item1']).displayed_options == ['item0', 'item1']


def test_old_task_quote_cannot_be_relabelled_as_current_request(tmp_path):
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    plan = Plan(relation='continue', tasks=[Task(kind='answer', quote='previous warranty question',
        entity='item0', mode='search', search_query='Device 0 warranty')])
    with pytest.raises(ValueError, match='TASK_QUOTE_MUST_COPY_CURRENT_REQUEST'):
        validate_plan(plan.model_dump_json(), 'Device 0 charging time?', package,
                      load_state(None, package.profile, package.version))


@pytest.mark.asyncio
async def test_planner_context_keeps_referents_not_old_executable_requests(tmp_path, monkeypatch):
    from g85.runtime.planner import understand
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    state = load_state(None, package.profile, package.version)
    state.focused = ['item0']
    state.displayed_options = ['item1', 'item0']
    state.active = [Task(kind='answer', quote='obsolete request token', entity='item0',
                         mode='search', search_query='obsolete search token')]
    async def complete(client, messages, *args, **kwargs):
        content = json.dumps(messages)
        assert 'obsolete' not in content
        context = json.loads(messages[1]['content'].split('\n', 1)[1])
        assert context['FOCUSED'] == ['item0']
        assert [x['entity'] for x in context['DISPLAYED_OPTIONS']] == ['item1', 'item0']
        return Plan(relation='continue', tasks=[Task(kind='answer', quote='charging time',
            entity='item0', mode='search', search_query='Device 0 charging time')]).model_dump_json()
    monkeypatch.setattr('g85.runtime.planner.complete', complete)
    plan = await understand('charging time?', state, package,
        [SimpleNamespace(role='user', content='obsolete history token')], None, uuid4())
    assert plan.tasks[0].entity == 'item0'


@pytest.mark.asyncio
async def test_provenance_repair_is_a_new_bounded_plan(tmp_path, monkeypatch):
    from g85.runtime.planner import understand
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    query, calls = 'Device 1 charging time?', []
    async def complete(client, messages, schema, *args):
        calls.append(schema)
        if len(calls) == 1:
            return Plan(relation='continue', tasks=[Task(kind='answer', quote='old unrelated request',
                entity='item0', mode='search', search_query='Device 0 warranty')]).model_dump_json()
        assert schema['$defs']['Task']['properties']['quote']['enum'] == [query]
        return Plan(relation='replace', tasks=[Task(kind='answer', quote=query,
            entity='item1', mode='search', search_query=query)]).model_dump_json()
    monkeypatch.setattr('g85.runtime.planner.complete', complete)
    plan = await understand(query, load_state(None, package.profile, package.version), package, [], None, uuid4())
    assert len(calls) == 2 and plan.tasks[0].entity == 'item1'
    assert 'enum' not in calls[0]['$defs']['Task']['properties']['quote']


@pytest.mark.asyncio
async def test_partial_evidence_is_not_reported_as_complete(tmp_path, monkeypatch):
    from g85.runtime.engine import run_turn
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    query = 'Device 0 charging time and warranty length?'
    async def understand(*args):
        return Plan(relation='replace', tasks=[Task(kind='answer', quote=query, entity='item0',
            mode='search', search_query=query)])
    async def complete(client, messages, *args, **kwargs):
        assert query in messages[-1]['content']
        return json.dumps({'results':[{'task_id':'task-1', 'excerpts':[
            {'evidence_id':'p0', 'quote':'Charge device A for 2 hours.'}],
            'missing_needs':['warranty length']}]})
    monkeypatch.setattr('g85.runtime.engine.understand', understand)
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    answer, g, _ = await run_turn(query, {}, [],
        SimpleNamespace(jurisdiction=None, procedure_id=None, as_of=None), None, uuid4(), package)
    assert '2 hours' in answer and 'warranty length' in answer
    assert g['partial'] and g['status'] == 'INSUFFICIENT_DATA'
    assert g['parts'][0]['result_status'] == 'insufficient_data'
    assert g['parts'][0]['grounding']['missing_needs'] == ['warranty length']


@pytest.mark.asyncio
async def test_missing_need_cannot_invent_a_claim(tmp_path, monkeypatch):
    from g85.runtime.engine import extract_answers
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    async def complete(*args, **kwargs):
        return json.dumps({'results':[{'task_id':'task-1', 'excerpts':[],
            'missing_needs':['The warranty lasts 100 years']}]})
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    result = await extract_answers(None, package, {'task-1':('Device 0 warranty', package.passages)},
                                   'Device 0 warranty?', uuid4())
    assert result['task-1'].missing == ['Device 0 warranty']
    assert not result['task-1'].excerpts


@pytest.mark.asyncio
@pytest.mark.parametrize('support', ['absent', 'partial'])
async def test_unsupported_judgment_cannot_turn_related_quote_into_answer(tmp_path, monkeypatch, support):
    from g85.runtime.engine import extract_answers
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    async def complete(*args, **kwargs):
        return json.dumps({'results':[{'task_id':'task-1', 'support':support,
            'support_check':'Charging is given but warranty is not.', 'missing_needs':[],
            'excerpts':[{'evidence_id':'p0','quote':'Charge device A for 2 hours.'}]}]})
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    result = await extract_answers(None, package, {'task-1':('Device 0 warranty', package.passages)},
                                   'Device 0 warranty?', uuid4())
    assert not result['task-1'].excerpts
    assert result['task-1'].missing == ['Device 0 warranty']


def test_source_segments_preserve_numbers_conditions_and_long_tail():
    from g85.runtime.engine import evidence_segments
    text = 'Charge at 3.7 V until green; do not unplug earlier.\nRepeat.\n' + 'Repeat.\n'*80 + 'Final limit: 42.'
    segments = evidence_segments(text)
    assert len(segments) == 64
    assert segments[0] == 'Charge at 3.7 V until green; do not unplug earlier.'
    assert segments[-1].endswith('Final limit: 42.')
    assert sum(s.count('Repeat.') for s in segments) == 81
    assert all(s in text for s in segments)


@pytest.mark.asyncio
async def test_invalid_segment_cannot_erase_another_task(tmp_path, monkeypatch):
    from g85.runtime.engine import extract_answers
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    async def complete(*args, **kwargs):
        return json.dumps({'results':[
            {'task_id':'task-1','support':'complete','missing_needs':[],
             'excerpts':[{'evidence_id':'p0','segment_ids':[99]}]},
            {'task_id':'task-2','support':'complete','missing_needs':[],
             'excerpts':[{'evidence_id':'p1','segment_ids':[1]}]}]})
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    result = await extract_answers(None, package,
        {'task-1':('Device 0 charging', [package.passages[0]]),
         'task-2':('Device 1 charging', [package.passages[1]])}, 'charging?', uuid4())
    assert 'task-1' not in result
    assert result['task-2'].excerpts[0][0] == package.passages[1].text


@pytest.mark.asyncio
async def test_whole_need_missing_cannot_retain_related_source(tmp_path, monkeypatch):
    from g85.runtime.engine import extract_answers
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    async def complete(client, messages, *args, **kwargs):
        payload = json.loads(messages[-1]['content'])
        assert payload['tasks'][0]['current_request_span'] == 'exact warranty length'
        return json.dumps({'results':[{'task_id':'task-1','support':'partial',
            'missing_needs':['Device 0 warranty'],
            'excerpts':[{'evidence_id':'p0','segment_ids':[1]}]}]})
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    result = await extract_answers(None, package, {'task-1':('Device 0 warranty', package.passages)},
        'Device 0 exact warranty length?', uuid4(), {'task-1':'exact warranty length'})
    assert result['task-1'].missing == ['Device 0 warranty']
    assert not result['task-1'].excerpts


@pytest.mark.asyncio
async def test_gap_clause_terminal_punctuation_keeps_supported_subneed(tmp_path, monkeypatch):
    from g85.runtime.engine import extract_answers
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    query = 'What is the warranty, and how long is charging?'
    async def complete(*args, **kwargs):
        return json.dumps({'results':[{'task_id':'task-1','support':'partial',
            'missing_needs':['What is the warranty?'],
            'excerpts':[{'evidence_id':'p0','segment_ids':[1]}]}]})
    monkeypatch.setattr('g85.runtime.engine.complete', complete)
    result = await extract_answers(None, package, {'task-1':(query, package.passages)}, query, uuid4())
    assert result['task-1'].missing == ['What is the warranty']
    assert result['task-1'].excerpts[0][0] == 'Charge device A for 2 hours.'


def test_history_does_not_reintroduce_old_dataset_after_reset_turn():
    from g85.runtime.contracts import current_package_history
    profile = {'id': 'new', 'version': '1'}
    raw = State(profile_id='new', profile_version='1', dataset_version='new-v1').model_dump()
    history = [SimpleNamespace(role='user', content='old subject'),
        SimpleNamespace(role='assistant', grounding={'corpus_version': 'old-v1'}),
        SimpleNamespace(role='user', content='new subject'),
        SimpleNamespace(role='assistant', grounding={'corpus_version': 'new-v1'})]
    assert current_package_history(history, raw, profile, 'new-v1') == history[2:]
    assert current_package_history(history, None, profile, 'new-v1') == []


@pytest.mark.asyncio
async def test_bare_menu_index_respects_search_only_package(tmp_path):
    from g85.runtime.planner import understand
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    state = load_state(None, package.profile, package.version)
    state.displayed_options = ['item0', 'item1']
    plan = await understand('2', state, package, [], None, uuid4())
    assert plan.tasks[0].kind == 'clarify'
    assert plan.tasks[0].candidates == ['item1']


@pytest.mark.asyncio
async def test_free_documents_schema_has_no_fake_attribute_and_requires_search(tmp_path, monkeypatch):
    from g85.runtime.planner import understand
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    state = load_state(None, package.profile, package.version)

    async def completion(client, messages, schema, request_id):
        props = schema['$defs']['Task']['properties']
        assert props['attributes']['maxItems'] == 0
        assert props['mode']['enum'] == ['search']
        assert 'search_query' in schema['$defs']['Task']['required']
        reference = json.loads(messages[0]['content'].split('REFERENCE:\n', 1)[1].split('\nBefore producing', 1)[0])
        assert reference['OUTPUT_SCHEMA'] == schema
        return Plan(relation='replace', tasks=[Task(kind='answer', quote='Device 1', entity='item1',
            mode='search', search_query='Device 1 charging duration')]).model_dump_json()

    monkeypatch.setattr('g85.runtime.planner.complete', completion)
    plan = await understand('Device 1 charging duration?', state, package, [], None, uuid4())
    assert plan.tasks[0].attributes == [] and plan.tasks[0].mode == 'search'


def test_named_document_fallback_is_bounded_and_never_ignores_filters(tmp_path):
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    assert package.search('unmatched-vocabulary', entity='item1') == []
    assert [p.id for p in retrieve(package, 'unmatched-vocabulary', entity='item1')] == ['p1']
    assert retrieve(package, 'unmatched-vocabulary') == []
    assert retrieve(package, 'unmatched-vocabulary', entity='not-an-entity') == []
    assert retrieve(package, 'unmatched-vocabulary', entity='item1', attributes=['other']) == []
    assert retrieve(package, 'unmatched-vocabulary', entity='item0', as_of=date(2026, 10, 5)) == []


def test_package_selection_dry_run_and_update_preserve_other_services(tmp_path):
    from g85.select_package import select_package
    package_path = tmp_path / 'package'
    package_path.mkdir()
    build(package_path)
    compose_path = tmp_path / 'compose.json'
    original = {'services': {'backend': {'environment': {'G85_PACKAGE_PATH': '/run/g85-package', 'UNCHANGED': 'yes'},
        'volumes': [{'type': 'bind', 'source': 'old', 'target': '/run/g85-package'}]},
        'postgres': {'image': 'unchanged', 'volumes': ['existing-volume:/data']}}}
    compose_path.write_text(json.dumps(original))
    before = compose_path.read_bytes()
    assert select_package(package_path, compose_path)['configuration_updated'] is False
    assert compose_path.read_bytes() == before
    result = select_package(package_path, compose_path, apply=True)
    after = json.loads(compose_path.read_text())
    assert result['runtime_restarted'] is False
    assert Path(result['backup']).read_bytes() == before
    assert after['services']['postgres'] == original['services']['postgres']
    assert after['services']['backend']['environment']['UNCHANGED'] == 'yes'
    assert after['services']['backend']['volumes'][0]['source'] == str(package_path.resolve())
    assert after['services']['backend']['volumes'][0]['read_only']
    (package_path/'passages.jsonl').write_text('tampered')
    snapshot = compose_path.read_bytes()
    with pytest.raises(ValueError):
        select_package(package_path, compose_path, apply=True)
    assert compose_path.read_bytes() == snapshot


def build(root, *, direct=True, rejected=False):
    profile = json.loads((ROOT / 'g85/profiles/documents.template.json').read_text(encoding='utf-8'))
    profile.update(id='synthetic_devices', title='Device manuals', language='en', service_region_aliases=[],
        data_classification='D0_SYSTEM_SMOKE', attributes=[{'id': 'charging', 'label': 'Charging', 'description': 'Charging instructions'}] if direct else [])
    profile['scope'] = {'description': 'Answers from the synthetic device manuals in this test package.',
        'included_topics': ['Device manuals'], 'excluded_requests': [], 'service_regions': [], 'evidence_policy': 'approved_sources_only'}
    profile['retrieval']['exact_lookup'] = direct
    texts = ['Charge device A for 2 hours.', 'Device B has no charging duration specified.']
    passages = [{'id': 'p'+str(i), 'source_id': 's'+str(i), 'entity': 'item'+str(i), 'attribute': 'charging' if direct else '',
        'title': 'Device '+str(i), 'text': text, 'content_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'review_status': 'REJECTED' if rejected and i == 1 else 'APPROVED',
        'metadata': {'effective_to': '2026-10-01'} if i == 0 else {}} for i,text in enumerate(texts)]
    values = {'profile.json': profile, 'groups.json': {'devices': 'Devices'},
        'entities.jsonl': [{'key': 'item'+str(i), 'title': 'Device '+str(i), 'domains': ['devices']} for i in range(2)],
        'sources.jsonl': [{'id': 's'+str(i), 'url': None} for i in range(2)], 'passages.jsonl': passages}
    manifest = {'schema_version': 'g85-package-v1', 'dataset_version': 'fixture-v1',
        'approved_passages': 1 if rejected else 2, 'files': {}}
    for name,value in values.items():
        payload = (''.join(json.dumps(row)+'\n' for row in value) if name.endswith('.jsonl') else json.dumps(value)).encode()
        (root/name).write_bytes(payload)
        manifest['files'][name] = {'sha256': hashlib.sha256(payload).hexdigest(),
                                  **({'count':len(value)} if name.endswith('.jsonl') else {})}
    (root/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return values


def change(root, name, value):
    payload = (''.join(json.dumps(row)+'\n' for row in value) if name.endswith('.jsonl') else json.dumps(value)).encode()
    (root/name).write_bytes(payload)
    manifest = json.loads((root/'manifest.json').read_text())
    manifest['files'][name]['sha256'] = hashlib.sha256(payload).hexdigest()
    (root/'manifest.json').write_text(json.dumps(manifest))


def test_package_is_domain_neutral_and_uses_real_langchain_retriever(tmp_path):
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    assert package.policy is None and package.preflight is None and package.catalog_config == {}
    assert package.direct is None and package.profile['scope']['service_regions'] == []
    documents = PackageRetriever(package=package, entity='item0').invoke('Charge device A')
    assert documents[0].page_content == 'Charge device A for 2 hours.'
    assert retrieve(package, 'Charge device A', 'item0')[0].id == documents[0].metadata['passage_id']


@pytest.mark.asyncio
async def test_direct_lookup_keeps_verbatim_text_missing_fields_and_date_filter(tmp_path):
    build(tmp_path)
    package = read_package(tmp_path, SCHEMA)
    task = Task(kind='answer', quote='charging', entity='item0', attributes=['charging'])
    answer,g = await package.direct(task, package.catalog['item0'], SimpleNamespace(as_of=None), uuid4())
    assert 'Charge device A for 2 hours.' in answer and g['sources'][0]['fragment_id'] == 'p0'
    assert g['data_classification'] == 'D0_SYSTEM_SMOKE'
    answer,g = await package.direct(task, package.catalog['item0'], SimpleNamespace(as_of=date(2026,10,5)), uuid4())
    assert g['status'] == 'INSUFFICIENT_DATA' and not g['sources']


def test_modified_source_fails_checksum_and_rejected_passages_never_serve(tmp_path):
    build(tmp_path, rejected=True)
    assert len(read_package(tmp_path, SCHEMA).passages) == 1
    path = tmp_path/'passages.jsonl'
    path.write_bytes(path.read_bytes().replace(b'2 hours', b'8 hours'))
    with pytest.raises(ValueError, match='CHECKSUM'):
        read_package(tmp_path, SCHEMA)


@pytest.mark.parametrize('mutation,error', [('source','DANGLING'),('text','CONTENT'),('field','ATTRIBUTE')])
def test_manifest_cannot_hide_broken_evidence_references(tmp_path, mutation, error):
    values = build(tmp_path)
    row = values['passages.jsonl'][0]
    row[{'source':'source_id','text':'text','field':'attribute'}[mutation]] = 'invalid'
    change(tmp_path, 'passages.jsonl', values['passages.jsonl'])
    with pytest.raises(ValueError, match=error):
        read_package(tmp_path, SCHEMA)


def test_same_friendly_version_different_package_resets_old_context(tmp_path):
    values = build(tmp_path)
    first = read_package(tmp_path, SCHEMA)
    old = State(dataset_version=first.version, profile_id=first.profile['id'], profile_version=first.profile['version'], focused=['item0'])
    values['profile.json']['title'] = 'Updated source scope'
    change(tmp_path, 'profile.json', values['profile.json'])
    second = read_package(tmp_path, SCHEMA)
    assert first.version != second.version
    assert load_state(old.model_dump(), second.profile, second.version).focused == []


def test_no_implicit_administrative_policy_or_unsupported_search_backend(tmp_path):
    values = build(tmp_path)
    values['profile.json']['policies'] = ['administrative']
    change(tmp_path, 'profile.json', values['profile.json'])
    with pytest.raises(ValueError, match='POLICY_NOT_INSTALLED'):
        read_package(tmp_path, SCHEMA)
    values['profile.json']['policies'] = []
    values['profile.json']['retrieval']['document_search'] = 'hybrid'
    change(tmp_path, 'profile.json', values['profile.json'])
    with pytest.raises(ValueError, match='NOT_INSTALLED'):
        read_package(tmp_path, SCHEMA)


def test_document_package_cannot_silently_execute_direct_lookup(tmp_path):
    build(tmp_path, direct=False)
    package = read_package(tmp_path, SCHEMA)
    state = State(dataset_version=package.version, profile_id=package.profile['id'], profile_version=package.profile['version'])
    task = Task(kind='answer', quote='Device 0', entity='item0')
    with pytest.raises(ValueError, match='EXACT_LOOKUP_DISABLED'):
        validate_plan(Plan(relation='replace',tasks=[task]).model_dump_json(), 'Device 0', package, state)


def test_custom_extension_is_opt_in_and_cannot_bypass_id_validation(tmp_path):
    values = build(tmp_path)
    values['profile.json']['policies'] = ['bad_test_plugin']
    change(tmp_path,'profile.json',values['profile.json'])
    def bad(plan, query, state, package):
        plan.tasks[0].entity = 'fabricated'
        return plan
    package = read_package(tmp_path,SCHEMA, {'bad_test_plugin':bad})
    state = State(dataset_version=package.version,profile_id=package.profile['id'],profile_version='clean-1')
    plan = Plan(relation='replace',tasks=[Task(kind='answer',quote='Device 0',entity='item0')])
    with pytest.raises(ValueError,match='UNKNOWN_ENTITY'):
        validate_plan(plan.model_dump_json(),'Device 0',package,state)


def test_generic_filters_distinguish_includes_only_and_unknown_values(tmp_path):
    from g85.runtime.catalog_filter import select
    from g85.runtime.contracts import Predicate
    values = build(tmp_path)
    values['entities.jsonl'][0]['facts'] = {'charging':['USB','AC']}
    values['entities.jsonl'][1]['facts'] = {'charging':['USB']}
    change(tmp_path,'entities.jsonl',values['entities.jsonl'])
    package = read_package(tmp_path,SCHEMA)
    candidates = list(package.catalog.values())
    includes = [Predicate(attribute='charging',operator='contains_any',values=['USB'])]
    only = [Predicate(attribute='charging',operator='equals_set',values=['USB'])]
    assert len(select(package,candidates,includes)[0]) == 2
    matches,unknown,sources = select(package,candidates,only)
    assert [c['key'] for c in matches] == ['item1'] and not unknown
    assert sources[0]['fragment_id'] == 'p1'
    matches,unknown,_ = select(package,candidates,includes,date(2026,10,5))
    assert [c['key'] for c in unknown] == ['item0']


def test_import_maps_arbitrary_column_names_without_editing_core(tmp_path):
    from g85.ingest import ingest
    profile=json.loads((ROOT/'g85/profiles/documents.template.json').read_text(encoding='utf-8'))
    source=tmp_path/'records.jsonl'
    text='Unchanged source text.\nSecond line.'
    source.write_text(json.dumps({'ref':'manual-a','heading':'Device A','body':text}),encoding='utf-8')
    mapping={'id':'ref','title':'heading','text':'body'}
    with pytest.raises(ValueError,match='REVIEW'):
        ingest(source,profile,mapping,'fixture-v1',tmp_path/'package')
    result=ingest(source,profile,mapping,'fixture-v1',tmp_path/'package',reviewed=True)
    assert result['activated'] is False
    package=read_package(tmp_path/'package',SCHEMA)
    assert package.passages[0].text == text and package.direct is None
    assert package.catalog['manual-a']['title'] == 'Device A'


def test_catalog_capacity_is_checked_before_serving_and_fifty_options_roundtrip(tmp_path):
    values = build(tmp_path)
    values['entities.jsonl'] = [{'key':'item'+str(i), 'title':'Device '+str(i), 'domains':['devices']} for i in range(65)]
    change(tmp_path,'entities.jsonl',values['entities.jsonl'])
    manifest = json.loads((tmp_path/'manifest.json').read_text())
    manifest['files']['entities.jsonl']['count'] = 65
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='CATALOG_EXCEEDS_64'):
        read_package(tmp_path,SCHEMA)
    state = State(dataset_version='v',profile_id='test',profile_version='1',
        displayed_options=['item'+str(i) for i in range(50)])
    assert len(State.model_validate(state.model_dump()).displayed_options) == 50


@pytest.mark.asyncio
async def test_search_reports_only_fields_of_selected_evidence(tmp_path, monkeypatch):
    from g85.runtime.engine import run_turn
    build(tmp_path)
    package=read_package(tmp_path,SCHEMA)
    async def understand(*args):
        return Plan(relation='replace',tasks=[Task(kind='answer',quote='charging',entity='item0',
            mode='search',search_query='Charge device A')])
    async def complete(*args,**kwargs):
        return json.dumps({'results':[{'task_id':'task-1','excerpts':[{
            'evidence_id':'p0','quote':'Charge device A for 2 hours.'}]}]})
    monkeypatch.setattr('g85.runtime.engine.understand',understand)
    monkeypatch.setattr('g85.runtime.engine.complete',complete)
    _,g,_=await run_turn('charging',{},[],SimpleNamespace(jurisdiction=None,procedure_id=None,as_of=None),
        None,uuid4(),package)
    field=g['parts'][0]['grounding']['field_results'][0]
    assert field == {'attribute':'charging','status':'answered','coverage':'excerpt_only',
        'evidence_ids':['p0'],'value':'Charge device A for 2 hours.'}


@pytest.mark.asyncio
async def test_generic_catalog_exposes_exact_menu_members_and_state(tmp_path, monkeypatch):
    from g85.runtime.engine import run_turn
    build(tmp_path)
    package=read_package(tmp_path,SCHEMA)
    async def understand(*args):
        return Plan(relation='replace',tasks=[Task(kind='catalog',quote='devices',domains=['devices'])])
    monkeypatch.setattr('g85.runtime.engine.understand',understand)
    _,g,state=await run_turn('devices',{},[],SimpleNamespace(jurisdiction=None,procedure_id=None,as_of=None),
        None,uuid4(),package)
    items=g['parts'][0]['catalog_items']
    assert [i['entity'] for i in items] == state.displayed_options == ['item0','item1']


@pytest.mark.asyncio
async def test_filter_schema_is_derived_from_nonadministrative_package(tmp_path, monkeypatch):
    from g85.runtime.planner import understand
    values=build(tmp_path)
    values['profile.json']['attributes'][0]['filterable']=True
    values['entities.jsonl'][0]['facts']={'charging':['USB','AC']}
    change(tmp_path,'profile.json',values['profile.json'])
    change(tmp_path,'entities.jsonl',values['entities.jsonl'])
    package=read_package(tmp_path,SCHEMA)
    seen=[]
    async def complete(client,messages,schema,*args,**kwargs):
        seen.append((messages,schema))
        return Plan(relation='replace',tasks=[Task(kind='catalog',quote='devices',domains=['devices'])]).model_dump_json()
    monkeypatch.setattr('g85.runtime.planner.complete',complete)
    await understand('devices',load_state(None,package.profile,package.version),package,[],None,uuid4())
    messages,schema=seen[0]
    assert 'predicates' in schema['$defs']['Task']['required']
    assert schema['$defs']['Predicate']['properties']['attribute']['enum'] == ['charging']
    assert 'equals_set' in messages[0]['content'] and 'Device' in messages[0]['content']
    assert 'khai sinh' not in messages[0]['content'].lower()
