from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.rag.g7.contracts import CatalogPredicate, Plan, State, Task, load_state
from app.rag.g8.mentor_catalog import evaluate, fee_class, validate_predicate
from app.rag.g8.mentor_context import bound_context


@pytest.mark.parametrize('quote,value', [
    ('Những thủ tục nào thu tiền bản sao?', 'paid_copy'),
    ('Thủ tục nào không có phí?', 'free'),
    ('Thủ tục nào không mất phí?', 'free'),
    ('Thủ tục nào vừa có trường hợp miễn phí vừa có trường hợp thu phí?', 'conditional'),
])
def test_fee_clause_vocabulary(quote,value):
    from app.rag.g8.mentor_catalog import bind_catalog_conditions
    task=Task(kind='catalog',quote=quote)
    result=bind_catalog_conditions(task,{'groups':[]})
    assert [(p.field,p.value) for p in result.predicates] == [('fees',value)]


def test_catalog_does_not_inherit_another_clause_filter():
    from app.rag.g8.mentor_catalog import bind_catalog_conditions
    task=Task(kind='catalog',quote='Chỉ nhận trực tiếp',predicates=[
        CatalogPredicate(field='fees',operator='eq',value='paid'),
        CatalogPredicate(field='submission_methods',operator='contains',value='trực tiếp')])
    result=bind_catalog_conditions(task,{'groups':[]})
    assert [p.model_dump() for p in result.predicates] == [dict(field='submission_methods',operator='eq',value='offline')]


@pytest.mark.parametrize('query', ['Thủ tục nào miễn phí hoặc chỉ nộp trực tiếp?',
                                 'Thủ tục nào có lệ phí dưới 100000 đồng?'])
def test_unsupported_boolean_or_amount_filter_is_not_broadened(query):
    from app.rag.g8.mentor_catalog import bind_catalog_conditions
    task=bind_catalog_conditions(Task(kind='catalog',quote=query),{'groups':[]})
    assert task.kind=='clarify' and task.question=='G8_FILTER_UNSUPPORTED'
    assert not task.predicates and not task.domains


def test_time_filter_uses_number_and_operator_from_own_clause():
    from app.rag.g8.mentor_catalog import bind_catalog_conditions
    task=Task(kind='catalog',quote='Thủ tục không quá 12 ngày',predicates=[
        CatalogPredicate(field='processing_times',operator='eq',value='3 ngày')])
    result=bind_catalog_conditions(task,{'groups':[]})
    assert [p.model_dump() for p in result.predicates]==[dict(field='processing_times',operator='lte',value='12')]
    task=Task(kind='catalog',quote='Thủ tục có tỷ lệ hài lòng trên 90 phần trăm',predicates=[
        CatalogPredicate(field='processing_times',operator='lte',value='3 ngày')])
    assert bind_catalog_conditions(task,{'groups':[]}).kind=='clarify'


def test_typed_catalog_followup_skips_legacy_parser(data):
    from app.rag.g8.turn_checks import explicit_catalog
    from app.rag.g7.catalog import cards
    state=State(corpus_version=data.version,catalog_context={'typed_predicates':[{'field':'fees','value':'free'}]})
    assert explicit_catalog('Trong số đó, thủ tục nào chỉ nhận trực tiếp?',state,cards(data)) is None


def test_no_cost_does_not_route_to_death(data):
    from app.rag.g7.planner import _death_browse
    from app.rag.g8.turn_checks import review_categories
    query='Các thủ tục hộ kinh doanh nào không mất phí?'
    assert not _death_browse(query)
    plan=Plan(relation='replace',tasks=[Task(kind='catalog',quote=query,domains=['business'])])
    assert review_categories(plan,query,[],None).tasks[0].domains == ['business']


def test_explicit_shared_list_overflow_is_not_dataset_specific():
    from app.rag.g8.mentor_context import exceeds_limit
    names=[f'dịch vụ {i}' for i in range(1,10)]
    assert exceeds_limit('Tôi muốn biết hồ sơ của '+', '.join(names),[])
    assert not exceeds_limit('Tôi muốn biết hồ sơ của '+', '.join(names[:8]),[])
    assert not exceeds_limit('Tôi đã có hồ sơ của '+', '.join(names),[])


def test_catalog_filter_literal_clause_binding(data):
    from app.rag.g7.catalog import cards
    from app.rag.g7.planner import validate_plan
    query='Liệt kê thủ tục miễn phí; chỉ nhận trực tiếp.'
    task=Task(kind='catalog',quote='LIỆT KÊ THỦ TỤC MIỄN PHÍ')
    direct=Task(kind='catalog',quote='chỉ nhận trực tiếp')
    state=State(corpus_version=data.version)
    plan=validate_plan(Plan(relation='replace',tasks=[task,direct]),query,cards(data),state)
    assert plan.tasks[0].quote == 'Liệt kê thủ tục miễn phí'
    assert [p.field for p in plan.tasks[1].predicates] == ['submission_methods']
    with pytest.raises(ValueError,match='LOCAL_CLAUSE'):
        validate_plan(Plan(relation='replace',tasks=[Task(kind='catalog',quote=query),direct]),query,cards(data),state)


def test_wire_grammar_cannot_emit_filters_on_procedures(data):
    from app.rag.g7.planner import wire_contract
    from app.rag.g7.catalog import cards
    _,schema=wire_contract(cards(data))
    regular,filtered,other=schema['$defs']['Task']['oneOf']
    assert 'catalog' not in regular['properties']['kind']['enum']
    assert regular['properties']['predicates']['maxItems']==0
    assert filtered['properties']['kind']['enum']==['catalog']
    assert filtered['properties']['predicates']['maxItems']==4
    assert '' not in regular['properties']['code']['enum']
    assert {'code','fields'} <= set(regular['required'])
    assert other['properties']['predicates']['maxItems']==0


@pytest.mark.parametrize('raw,category', [('Không','free'),('0.0','free'),('không đồng','free'),
    ('35.000 đồng/lần','paid'),('Bản chính: Không thu phí Bản sao: 8.000đ/bản','conditional'),
    ('Theo quy định',None),('',None),('Liên hệ để biết phí',None)])
def test_fee_semantics_are_data_independent(raw,category):
    assert fee_class(raw) == category


def test_conditional_fee_is_not_unconditionally_free():
    text='Bản chính: Không thu phí Bản sao: 8.000đ/bản'
    for value,result in [('free',False),('paid',True),('free_original',True),('paid_copy',True),('conditional',True)]:
        assert evaluate(text,dict(field='fees',operator='eq',value=value)) is result
    assert evaluate('Theo quy định',dict(field='fees',operator='eq',value='free')) is None


def test_method_exclusion_does_not_treat_missing_as_false():
    p=dict(field='submission_methods',operator='not_contains',value='online')
    assert evaluate('Trực tiếp',p) is True
    assert evaluate('Cả hai',p) is False
    assert evaluate('Chưa có thông tin',p) is None


def test_time_comparison_keeps_range_upper_bound_and_conditions():
    p=dict(field='processing_times',operator='lte',value='3')
    assert evaluate('3–5 ngày làm việc kể từ khi nhận đủ hồ sơ hợp lệ.',p) is None  # unrecognized prose remains unknown
    assert evaluate('3–5 ngày làm việc',p) is False
    assert evaluate('03 ngày làm việc',p) is True
    assert evaluate('Trong ngày (có thể kéo dài hơn)',p) is None


def test_eight_task_limit_and_old_state_migration():
    task=Task(kind='procedure',quote='test',code='A')
    with pytest.raises(ValidationError):
        Plan(relation='replace',tasks=[task]*9)
    old=State(corpus_version='v',active=[task.model_copy(update={'code':str(i)}) for i in range(8)],focused=[str(i) for i in range(8)])
    state=load_state(old.model_dump(),'v')
    assert [t.code for t in state.active] == ['6','7'] and state.focused == ['6','7']
    assert all(t.quote == t.code for t in state.active)


def test_invalid_filter_cannot_execute():
    with pytest.raises(ValueError):
        validate_predicate(CatalogPredicate(field='fees',operator='contains',value='anything'))


@pytest.mark.asyncio
async def test_two_recent_subjects_after_eight_and_replacement(data):
    from app.rag.g7.catalog import cards
    from app.rag.g7.workflow import execute
    from app.rag.schemas import Filters
    chosen=cards(data)[:8]
    state=State(corpus_version=data.version)
    tasks=[Task(kind='procedure',quote='long query with eight subjects',code=c['code'],fields=['fees']) for c in chosen]
    _,_,state=await execute(data,Plan(relation='replace',tasks=tasks),state,Filters(),SimpleNamespace(client=None),uuid4())
    assert [t.code for t in state.active] == [c['code'] for c in chosen[-2:]]
    assert state.displayed_options==[]  # Do not silently renumber eight as two.
    from app.rag.g8.mentor_context import forgotten_ordinal
    assert forgotten_ordinal('Thủ tục thứ hai cần giấy tờ gì?',state).tasks[0].kind=='clarify'
    assert forgotten_ordinal('Kết hôn cần gì?',state) is None
    tasks=[Task(kind='procedure',quote='new question',code=chosen[0]['code'],fields=['submission_methods'])]
    _,_,state=await execute(data,Plan(relation='replace',tasks=tasks),state,Filters(),SimpleNamespace(client=None),uuid4())
    assert [t.code for t in state.active] == [chosen[-1]['code'],chosen[0]['code']]


@pytest.mark.asyncio
async def test_filter_and_procedure_evidence_bind_independently(data):
    from app.rag.g7.catalog import cards
    from app.rag.g7.workflow import execute
    from app.rag.g8.graph import verify_evidence
    from app.rag.schemas import Filters
    catalog=cards(data)
    birth=next(c for c in catalog if c['label']=='birth_registration')
    plan=Plan(relation='replace',tasks=[
        Task(kind='catalog',quote='charged services',predicates=[CatalogPredicate(field='fees',operator='eq',value='paid')]),
        Task(kind='procedure',quote='birth documents',code=birth['code'],fields=['required_documents']),
        Task(kind='catalog',quote='free services',predicates=[CatalogPredicate(field='fees',operator='eq',value='free')])])
    result=await execute(data,plan,State(corpus_version=data.version),Filters(),SimpleNamespace(client=None),uuid4())
    assert verify_evidence({'result':result})
    parts=result[1]['parts']
    assert len(parts)==3 and parts[1]['procedure_id']==birth['id']
    paid={r['procedure_id'] for r in parts[0]['catalog_items']}
    free={r['procedure_id'] for r in parts[2]['catalog_items']}
    assert len(paid)==6 and len(free)==29 and not paid&free
    assert birth['id'] in paid and birth['id'] not in free


@pytest.mark.asyncio
async def test_reverse_followup_intersects_backend_ids_including_empty(data):
    from app.rag.g7.catalog import cards
    from app.rag.g8.catalog_browse import execute_catalog
    from app.rag.schemas import Filters
    catalog=cards(data)
    birth=next(c for c in catalog if c['label']=='birth_registration')
    task=Task(kind='catalog',quote='Trong số đó, thủ tục nào có phí?',predicates=[
        CatalogPredicate(field='fees',operator='eq',value='paid')])
    state=State(corpus_version=data.version,displayed_options=[birth['code']],catalog_context={'menu_only':True})
    result=await execute_catalog(data,task,state,Filters(),SimpleNamespace(client=None),uuid4(),catalog)
    assert result['options']==[birth['code']]
    state.displayed_options=[]
    result=await execute_catalog(data,task,state,Filters(),SimpleNamespace(client=None),uuid4(),catalog)
    assert result['options']==[]  # An empty previous list cannot broaden to all.
    state.catalog_context={}
    result=await execute_catalog(data,task,state,Filters(),SimpleNamespace(client=None),uuid4(),catalog)
    assert result['status']=='NEED_CLARIFICATION'
