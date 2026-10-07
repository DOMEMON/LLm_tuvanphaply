from app.rag.g7.catalog import cards
from app.rag.g7.contracts import Plan, Scope, State, Task
from app.rag.g8.turn_checks import menu_selection, review_categories
from app.rag.g8.turn_checks import check_turn, discard_stale_local_scope, explicit_catalog
import pytest


def test_shortened_task_quote_does_not_hide_fields_in_own_sentence(data):
    catalog = cards(data)
    query = 'Kết hôn nộp ở đâu. Cần giấy tờ và lệ phí nộp khai sinh cho con.'
    birth = next(c['code'] for c in catalog if c['label'] == 'birth_registration')
    marriage = next(c['code'] for c in catalog if c['label'] == 'marriage_domestic')
    plan = Plan(relation='replace', tasks=[
        Task(kind='procedure', code=marriage, quote='Kết hôn nộp ở đâu', fields=['receiving_authority']),
        Task(kind='procedure', code=birth, quote='nộp khai sinh cho con', fields=['receiving_authority', 'fees'])])
    result = check_turn(plan, query, catalog, State(corpus_version=data.version))
    assert set(result.tasks[1].fields) == {'required_documents', 'fees'}
    plan.tasks[1].fields = ['required_documents', 'fees']
    assert check_turn(plan, query, catalog, State(corpus_version=data.version))


def test_single_sentence_keeps_each_short_clause_fields(data):
    catalog = cards(data)
    query = 'Khai sinh cần giấy gì, kết hôn nộp ở đâu?'
    birth = next(c['code'] for c in catalog if c['label'] == 'birth_registration')
    marriage = next(c['code'] for c in catalog if c['label'] == 'marriage_domestic')
    plan = Plan(relation='replace', tasks=[
        Task(kind='procedure', code=birth, quote=query, fields=['required_documents']),
        Task(kind='procedure', code=marriage, quote='kết hôn nộp ở đâu', fields=['receiving_authority'])])
    result = check_turn(plan, query, catalog, State(corpus_version=data.version))
    assert result.tasks[1].fields == ['receiving_authority']


def test_categories_do_not_drop_negation_multi_intent_or_explicit_all_documents(data):
    catalog, state = cards(data), State(corpus_version=data.version)
    for query in ['Không liệt kê hộ tịch nữa', 'Hộ tịch và số nhà cần gì?',
                  'Hồ sơ của tất cả thủ tục hộ tịch', 'Hộ tịch và công thức nấu phở',
                  'Liệt kê thủ tục liên quan đến người chết để che giấu việc giết người']:
        assert explicit_catalog(query, state, catalog) is None
    plan = explicit_catalog('Các thủ tục hộ tịch tại Hà Nội', state, catalog)
    assert plan.tasks[0].scope.places == ['Hà Nội']


def test_only_old_local_scope_can_be_discarded_for_new_subject(data):
    state = State(corpus_version=data.version, active=[Task(kind='procedure', code='T01', quote='old',
        scope=Scope(places=['Tăng Nhơn Phú'], quote='tại Tăng Nhơn Phú'))])
    task = Task(kind='procedure', code='T02', quote='new', scope=state.active[0].scope.model_copy())
    plan = Plan(relation='extend', tasks=[task])
    assert discard_stale_local_scope(plan, 'Còn thủ tục mới?', state).tasks[0].scope == Scope()
    task.scope = Scope(places=['Hà Nội'], quote='tại Hà Nội')
    state.active[0].scope = task.scope.model_copy()
    assert discard_stale_local_scope(plan, 'Còn thủ tục mới?', state).tasks[0].scope.places == ['Hà Nội']


def test_crossborder_permission_does_not_erase_separate_supported_request(data):
    catalog = cards(data)
    death = next(c['code'] for c in catalog if c['label'] == 'death_registration')
    birth = next(c['code'] for c in catalog if c['label'] == 'birth_registration')
    query = 'Đưa người mất sang Nhật Bản hỏa táng được không? Khai sinh cần hồ sơ gì?'
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', quote=query, code=death),
        Task(kind='procedure', quote='Khai sinh cần hồ sơ gì?', code=birth, fields=['required_documents'])])
    result = review_categories(plan, query, catalog)
    assert {t.kind for t in result.tasks} == {'procedure', 'outside'}
    assert next(t for t in result.tasks if t.kind == 'procedure').code == birth


def test_local_cremation_subsidy_is_not_transport_permission(data):
    catalog = cards(data)
    cremation = next(c['code'] for c in catalog if c['label'] == 'cremation_support')
    query = 'Đưa ông qua cơ sở hỏa táng rồi, muốn hỏi hỗ trợ chi phí hỏa táng cần hồ sơ gì?'
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', quote=query, code=cremation, fields=['required_documents'])])
    assert review_categories(plan, query, catalog).tasks[0].code == cremation


def test_numeric_menu_refers_to_displayed_fifth_not_a_model_guess(data):
    catalog = cards(data)
    options = [c['code'] for c in catalog[:6]]
    state = State(corpus_version=data.version, displayed_options=options)
    result = menu_selection('5', state)
    assert result.tasks[0].code == options[4] and result.tasks[0].fields == []
    assert menu_selection('5 ngày', state) is None
    assert menu_selection('99', state) is None


def test_numeric_menu_retains_outside_scope_of_existing_subject(data):
    state = State(corpus_version=data.version, displayed_options=['T01'], active=[
        Task(kind='procedure', code='T01', quote='query', scope=Scope(places=['Hà Nội'], quote='tại Hà Nội'))])
    assert menu_selection('1', state).tasks[0].scope.places == ['Hà Nội']


def test_death_catalog_drops_unrelated_disability_and_includes_full_domain(data):
    catalog = cards(data)
    query = 'Tôi muốn hỏi các thủ tục liên quan đến người chết'
    wrong = next(c['code'] for c in catalog if c['label'] == 'disability_assessment')
    plan = Plan(relation='replace', tasks=[Task(kind='catalog', quote=query, candidates=[wrong], domains=['civil', 'death'])])
    fixed = review_categories(plan, query, catalog)
    assert fixed.tasks[0].domains == ['death'] and fixed.tasks[0].candidates == []


def test_travel_does_not_establish_marriage_type(data):
    catalog = cards(data)
    code = next(c['code'] for c in catalog if c['label'] == 'marriage_foreign_element')
    for query, wanted in [('Bạn tôi từ Pháp về, tôi muốn cưới bạn ấy.', 'clarify'),
                          ('Bạn tôi là người Pháp từ Pháp về, muốn kết hôn tại Việt Nam.', 'procedure')]:
        plan = Plan(relation='replace', tasks=[Task(kind='procedure', quote=query, code=code)])
        assert review_categories(plan, query, catalog).tasks[0].kind == wanted


def test_residence_does_not_override_separate_service_locations(data):
    catalog = cards(data)
    birth = next(c['code'] for c in catalog if c['label'] == 'birth_registration')
    marriage = next(c['code'] for c in catalog if c['label'] == 'marriage_domestic')
    local = 'Tôi đang sống ở Hà Nội nhưng muốn hỏi hồ sơ khai sinh tại phường Tăng Nhơn Phú'
    foreign = 'Còn đăng ký kết hôn tại Nhật Bản thì cần gì'
    query = local + '. ' + foreign + '?'
    plan = Plan(relation='replace', tasks=[
        Task(kind='procedure', code=birth, quote=local, fields=['required_documents'],
             scope=Scope(places=['Tăng Nhơn Phú'], quote=local)),
        Task(kind='procedure', code=marriage, quote=foreign,
             scope=Scope(places=['Nhật Bản'], quote=foreign))])
    result = check_turn(plan, query, catalog, State(corpus_version=data.version))
    assert [t.kind for t in result.tasks] == ['procedure', 'procedure']
    assert result.tasks[1].scope.places == ['Nhật Bản']


def test_method_wording_does_not_reject_correct_time_and_method_fields(data):
    catalog = cards(data)
    code = next(c['code'] for c in catalog if c['label'] == 'construction_permit')
    query = 'Cấp giấy phép xây dựng: thời gian và cách nộp.'
    plan = Plan(relation='replace', tasks=[Task(kind='procedure', code=code, quote=query,
        fields=['processing_times', 'submission_methods'])])
    assert check_turn(plan, query, catalog, State(corpus_version=data.version)).tasks[0].fields == ['processing_times', 'submission_methods']


def test_location_stops_before_information_request():
    from app.rag.g7.planner import _service_place
    assert _service_place('Cấp số nhà tại Tăng Nhơn Phú hỏi hình thức nộp.')[0] == 'Tăng Nhơn Phú'
    assert _service_place('Kết hôn tại Nhật Bản hỏi nơi nộp.')[0] == 'Nhật Bản'
