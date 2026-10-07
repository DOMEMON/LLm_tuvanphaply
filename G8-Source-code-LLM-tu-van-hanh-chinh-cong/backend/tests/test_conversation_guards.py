"""High-confidence G8 safety guards from the reviewed 17-turn conversation."""
import asyncio
from uuid import uuid4

from app.rag.g7.catalog import cards
from app.rag.g7.contracts import Plan, Scope, State, Task
from app.rag.g7.planner import _service_place, reconcile_plan, validate_plan
from app.rag.g7.workflow import allowed_scope, execute
from app.rag.schemas import Filters


def code(catalog, label):
    return next(item['code'] for item in catalog if item['label'] == label)


def procedure(catalog, label, fields=()):
    return Task(kind='procedure', quote='question', code=code(catalog, label), fields=list(fields))


def reviewed(plan, query, catalog, state):
    return reconcile_plan(validate_plan(plan, query, catalog, state), query, catalog, state)


def test_repeated_explicit_hanoi_cannot_fall_back_to_local_source(data):
    catalog = cards(data)
    query = 'Đăng ký khai sinh tại Hà Nội cần giấy gì?'
    for _ in range(2):
        state = State(corpus_version=data.version, active=[procedure(catalog, 'birth_registration')])
        plan = Plan(relation='continue', tasks=[procedure(catalog, 'birth_registration', ['required_documents'])])
        result = reviewed(plan, query, catalog, state)
        assert result.tasks[0].scope.places == ['Hà Nội']
        assert not allowed_scope(result.tasks[0].scope)


def test_service_place_is_not_residence_and_does_not_spread_across_tasks(data):
    catalog = cards(data)
    state = State(corpus_version=data.version)
    query = 'Tôi đang ở Hà Nội nhưng muốn đăng ký khai sinh tại phường Tăng Nhơn Phú, cần hồ sơ gì?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'birth_registration', ['required_documents'])])
    result = reviewed(plan, query, catalog, state)
    assert result.tasks[0].scope.places == ['phường Tăng Nhơn Phú']
    assert allowed_scope(result.tasks[0].scope)
    assert _service_place('Tôi sống tại Hà Nội, cần giấy khai sinh.') is None
    assert _service_place('Làm khai sinh tại TP.HCM cần hồ sơ gì?')[0] == 'TP.HCM'
    mixed = 'Khai sinh tại Tăng Nhơn Phú và kết hôn tại Hà Nội cần gì?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'birth_registration'),
                                           procedure(catalog, 'marriage_domestic')])
    result = reviewed(plan, mixed, catalog, state)
    assert all(task.scope == Scope() for task in result.tasks)


def test_bereavement_listing_is_catalog_not_five_applications(data):
    catalog = cards(data)
    query = 'Bố tôi mới mất, có những thủ tục nào liên quan đến người qua đời?'
    labels = ('death_registration', 'funeral_social_pension', 'cremation_support',
              'funeral_social_assistance', 'marriage_domestic')
    plan = Plan(relation='continue', tasks=[procedure(catalog, label,
                 ['required_documents', 'fees', 'receiving_authority', 'processing_times', 'submission_methods'])
                 for label in labels])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert result.relation == 'replace'
    assert len(result.tasks) == 1
    assert result.tasks[0].kind == 'catalog' and result.tasks[0].domains == ['death']
    assert not result.tasks[0].fields and not result.tasks[0].candidates
    assert not _service_place(query)


def test_past_marriage_is_not_current_business_application(data):
    catalog = cards(data)
    marriage, business = code(catalog, 'marriage_domestic'), code(catalog, 'create_household_business')
    query = 'Tôi cưới vợ rồi, giờ hai vợ chồng muốn mở quán kinh doanh. Cần hồ sơ gì?'
    plan = Plan(relation='replace', tasks=[Task(kind='clarify', quote=query,
                                              candidates=[business, marriage], fields=['required_documents'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert len(result.tasks) == 1 and result.tasks[0].kind == 'procedure'
    assert result.tasks[0].code == business and result.tasks[0].fields == ['required_documents']
    # A completed marriage does not suppress a different explicit marriage-related
    # service, such as obtaining confirmation of marital status.
    query = 'Tôi đã cưới rồi, giờ xin giấy xác nhận tình trạng hôn nhân.'
    target = procedure(catalog, 'confirm_marital_status')
    assert reviewed(Plan(relation='replace', tasks=[target]), query, catalog,
                    State(corpus_version=data.version)).tasks[0].code == target.code


def test_rejecting_one_pending_choice_uses_remaining_business_choice(data):
    catalog = cards(data)
    marriage, business = code(catalog, 'marriage_domestic'), code(catalog, 'create_household_business')
    previous = Task(kind='clarify', quote='Mở quán', candidates=[business, marriage],
                    fields=['required_documents'])
    state = State(corpus_version=data.version, pending=[previous], displayed_options=[business, marriage])
    query = 'Tôi đã cưới vợ rồi mà cần gì kết hôn nữa?'
    plan = Plan(relation='continue', tasks=[Task(kind='clarify', quote=query, candidates=[marriage])])
    result = reviewed(plan, query, catalog, state)
    assert result.tasks[0].kind == 'procedure'
    assert result.tasks[0].code == business and result.tasks[0].fields == ['required_documents']


def test_singular_first_option_does_not_repeat_second(data):
    catalog = cards(data)
    birth, marriage = code(catalog, 'birth_registration'), code(catalog, 'marriage_domestic')
    state = State(corpus_version=data.version, displayed_options=[birth, marriage])
    plan = Plan(relation='continue', tasks=[procedure(catalog, 'birth_registration', ['submission_methods']),
                                             procedure(catalog, 'marriage_domestic', ['submission_methods'])])
    result = reviewed(plan, 'Cái đầu có nộp online không?', catalog, state)
    assert [task.code for task in result.tasks] == [birth]
    plural_plan = Plan(relation='continue', tasks=[procedure(catalog, 'birth_registration', ['submission_methods']),
                                                    procedure(catalog, 'marriage_domestic', ['submission_methods'])])
    assert len(reviewed(plural_plan, 'Cả hai có nộp online không?', catalog, state).tasks) == 2


def test_ordinal_recovers_even_if_model_asks_to_clarify(data):
    catalog = cards(data)
    marriage, birth = code(catalog, 'marriage_domestic'), code(catalog, 'birth_registration')
    state = State(corpus_version=data.version, displayed_options=[marriage, birth])
    plan = Plan(relation='continue', tasks=[Task(kind='clarify', quote='Việc thứ nhất cần chuẩn bị giấy gì?',
                                               candidates=[birth, marriage])])
    result = reviewed(plan, 'Việc thứ nhất cần chuẩn bị giấy gì?', catalog, state)
    assert [(t.code, t.fields) for t in result.tasks] == [(marriage, ['required_documents'])]


def test_explicit_named_pair_stays_two_and_ordered(data):
    catalog = cards(data)
    marriage, birth = code(catalog, 'marriage_domestic'), code(catalog, 'birth_registration')
    plan = Plan(relation='replace', tasks=[Task(kind='clarify', quote='phí kết hôn', candidates=[]),
                                           procedure(catalog, 'birth_registration', ['fees'])])
    result = reviewed(plan, 'Cho mình phí kết hôn rồi phí khai sinh.', catalog,
                      State(corpus_version=data.version))
    assert [(t.code, t.fields) for t in result.tasks] == [(marriage, ['fees']), (birth, ['fees'])]
    # A mere mention of a marriage certificate in a birth-document question
    # is not a second application.
    query = 'Khai sinh cần giấy chứng nhận kết hôn trong hồ sơ không?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'birth_registration', ['required_documents'])])
    assert len(reviewed(plan, query, catalog, State(corpus_version=data.version)).tasks) == 1


def test_explicit_three_service_fee_request_recovers_omitted_middle(data):
    catalog = cards(data)
    birth, death, marriage = [code(catalog, label) for label in
                              ('birth_registration', 'death_registration', 'marriage_domestic')]
    query = 'Cho tôi lệ phí khai sinh, khai tử và đăng ký kết hôn'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'birth_registration', ['fees']),
                                           procedure(catalog, 'marriage_domestic', ['fees'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert [(t.code, t.fields) for t in result.tasks] == [(birth, ['fees']), (death, ['fees']), (marriage, ['fees'])]


def test_separate_money_and_documents_are_never_shared_across_services(data):
    catalog = cards(data)
    query = 'Tôi hỏi tiền đăng ký kết hôn và giấy tờ đăng ký khai tử, không cần hồ sơ kết hôn.'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'marriage_domestic', ['fees']),
                                           procedure(catalog, 'death_registration', ['required_documents'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert [(t.code, t.fields) for t in result.tasks] == [
        (code(catalog, 'marriage_domestic'), ['fees']),
        (code(catalog, 'death_registration'), ['required_documents'])]


def test_opening_shop_does_not_imply_suspension_or_construction(data):
    catalog = cards(data)
    labels = ('create_household_business', 'business_temporary_suspension',
              'construction_permit', 'notify_construction_start', 'planning_information')
    plan = Plan(relation='replace', tasks=[procedure(catalog, label, ['required_documents']) for label in labels])
    result = reviewed(plan, 'Tôi đã cưới vợ rồi, giờ muốn mở quán kinh doanh, cần hồ sơ gì?',
                      catalog, State(corpus_version=data.version))
    assert [t.code for t in result.tasks] == [code(catalog, 'create_household_business')]


def test_local_place_followed_by_gom_gi_is_not_a_new_place(data):
    catalog = cards(data)
    query = 'Hồ sơ đăng ký kết hôn với người nước ngoài tại phường Tăng Nhơn Phú gồm gì?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'marriage_foreign_element', ['required_documents'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert result.tasks[0].scope.places == ['phường Tăng Nhơn Phú']
    assert allowed_scope(result.tasks[0].scope)


def test_hoso_prefix_does_not_request_every_field(data):
    catalog = cards(data)
    query = 'Hồ sơ khai sinh tại Tăng Nhơn Phú và kết hôn tại Hà Nội cần gì?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'birth_registration',
                 ['required_documents', 'receiving_authority', 'processing_times', 'submission_methods']),
                 procedure(catalog, 'marriage_domestic', ['required_documents'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert [t.fields for t in result.tasks] == [['required_documents'], ['required_documents']]


def test_accented_house_book_does_not_inherit_house_number_fee(data):
    catalog = cards(data)
    number = procedure(catalog, 'house_number_assignment', ['fees'])
    state = State(corpus_version=data.version, active=[number], focused=[number.code])
    for query in ('Thế còn lệ phí làm sổ nhà?', 'Thế còn lậ phí làm sổ nhà?'):
        plan = Plan(relation='continue', tasks=[procedure(catalog, 'house_number_assignment', ['fees'])])
        result = reviewed(plan, query, catalog, state)
        assert result.relation == 'replace'
        assert len(result.tasks) == 1
        assert result.tasks[0].kind == 'clarify'
        assert result.tasks[0].question == 'G8_HOUSE_BOOK_NUMBER'
        assert not result.tasks[0].candidates
    query = 'Tôi muốn hỏi sổ nhà.'
    suggested = Plan(relation='continue', tasks=[Task(kind='clarify', quote=query,
                          candidates=[number.code])])
    result = reviewed(suggested, query, catalog, state)
    assert result.tasks[0].kind == 'clarify' and not result.tasks[0].candidates
    query = 'Tôi muốn hỏi về sổ nhà nhưng không biết chính xác gọi giấy đó là gì.'
    listed = Plan(relation='replace', tasks=[Task(kind='catalog', quote=query, domains=['housing'])])
    result = reviewed(listed, query, catalog, state)
    assert result.tasks[0].kind == 'clarify' and not result.tasks[0].candidates


def test_explicit_house_number_and_background_house_book_remain_number(data):
    catalog = cards(data)
    state = State(corpus_version=data.version)
    for query in ('Lệ phí làm số nhà bao nhiêu?',
                  'Tôi đã có sổ nhà; giờ xin số nhà cho căn nhà, mất tiền không?'):
        plan = Plan(relation='replace', tasks=[procedure(catalog, 'house_number_assignment', ['fees'])])
        result = reviewed(plan, query, catalog, state)
        assert len(result.tasks) == 1
        assert result.tasks[0].kind == 'procedure'
        assert result.tasks[0].code == code(catalog, 'house_number_assignment')


def test_house_number_correction_drops_incidental_land_task_only(data):
    catalog = cards(data)
    number = procedure(catalog, 'house_number_assignment', ['fees'])
    location = procedure(catalog, 'confirm_house_land_location', ['receiving_authority'])
    state = State(corpus_version=data.version, pending=[Task(kind='clarify', quote='sổ nhà')])
    plan = Plan(relation='continue', tasks=[number, location])
    query = 'Ý tôi là số nhà, địa chỉ để nhận thư; lệ phí bao nhiêu?'
    result = reviewed(plan, query, catalog, state)
    assert result.relation == 'replace'
    assert [(t.code, t.fields) for t in result.tasks] == [(number.code, ['fees'])]
    query = 'Ý tôi là số nhà và xác nhận vị trí nhà - đất; cả hai cần giấy tờ gì?'
    plan = Plan(relation='replace', tasks=[number, location])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert [t.code for t in result.tasks] == [number.code, location.code]


def test_explicit_first_land_registration_is_not_blocked_by_house_book(data):
    catalog = cards(data)
    query = 'Sổ nhà lần đầu đăng ký đất đai thì hồ sơ thế nào?'
    plan = Plan(relation='replace', tasks=[procedure(catalog, 'first_land_registration', ['required_documents'])])
    result = reviewed(plan, query, catalog, State(corpus_version=data.version))
    assert result.tasks[0].kind == 'procedure'
    assert result.tasks[0].code == code(catalog, 'first_land_registration')


def test_house_book_clarification_is_server_owned_and_clears_stale_focus(data):
    catalog = cards(data)
    number = procedure(catalog, 'house_number_assignment', ['fees'])
    state = State(corpus_version=data.version, active=[number], focused=[number.code])
    query = 'Thế còn lệ phí làm sổ nhà?'
    plan = reviewed(Plan(relation='continue', tasks=[number]), query, catalog, state)
    content, grounding, next_state = asyncio.run(execute(data, plan, state, Filters(), None, uuid4()))
    assert 'sổ nhà' in content and 'số nhà' in content
    assert 'Mình chưa lấy lệ phí cấp số nhà' in content
    assert grounding['status'] == 'NEED_CLARIFICATION'
    assert not grounding['sources']
    assert grounding['parts'][0]['procedure_id'] is None
    assert not next_state.active and not next_state.focused
