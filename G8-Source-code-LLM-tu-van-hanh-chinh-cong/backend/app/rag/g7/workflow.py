"""Execute the model's typed tasks against approved evidence, not model memory."""
import unicodedata
import os
from uuid import uuid5

from app.config import get_settings
from app.errors import APIError
from app.rag.answer_plan import LABELS
from app.rag.g3.runtime import load_active_dataset, retrieve_direct_active_dataset
from app.rag.g5 import realtime, source_discrepancy
from app.rag.g5.intent_context import IntentContextState
from app.rag.g5.source_mcp import MCPSourceUpdateClient
from app.rag.g7.catalog import cards, matching_cards
from app.rag.g7.contracts import FIELDS, load_state
from app.rag.g7.planner import understand
from app.rag.planned_grounding import answer_with_plan
from app.rag.retrieval import normalize
from app.rag.schemas import Filters, Grounding, RetrievalInput

LOCAL_SCOPE = 'phường Tăng Nhơn Phú, Thành phố Hồ Chí Minh, Việt Nam'


def allowed_scope(scope):
    # Folding only for matching approved locality aliases, never procedure intent.
    accepted = {
        'country': {'vn', 'vietnam', 'viet nam'},
        'province': {'ho chi minh', 'tp ho chi minh', 'thanh pho ho chi minh', 'tphcm', 'tp hcm', 'hcm', 'sai gon'},
        'ward': {'tang nhon phu', 'phuong tang nhon phu'},
    }
    names = set().union(*accepted.values())
    return (all(normalize(p) in names for p in scope.places)
            and all(not getattr(scope, k) or normalize(getattr(scope, k)) in allowed
                    for k, allowed in accepted.items()))


def empty_grounding(data, request_id, status='NEED_CLARIFICATION', missing=()):
    return Grounding(request_id=request_id, evidence_bundle_id=uuid5(request_id, 'g7-v3'),
                     status=status, corpus_version=data.version, data_classification='D2_COMPANY_REAL',
                     provider='g7-workflow', model='evidence-only', sources=[],
                     missing_information=list(missing)).model_dump(mode='json')


async def procedure_answer(data, task, card, filters, ai, request_id):
    title, pid = card['title'], card['id']
    g = empty_grounding(data, request_id)
    g['procedure_title'] = title
    if not task.fields:
        g['missing_information'] = ['field_intents']
        return ('Bạn muốn xem hồ sơ, lệ phí, nơi nộp, thời gian giải quyết, hình thức nộp '
                'hay tất cả thông tin của thủ tục này?', g)
    texts, missing, citations, checklist = [], [], {}, []
    for field in task.fields:
        if field not in FIELDS:
            texts.append(f"{LABELS.get(field, field)}: Nội dung này chưa được hỗ trợ trong nguồn đang phục vụ.")
            missing.append(field)
            continue
        # Fetch each field atomically so multi-request size never truncates one
        # field's conditions or loses a whole task to the old 8-fragment limit.
        query = RetrievalInput(query=LABELS[field], field_intents=[field], acquisition_mode='direct_catalog',
                               filters=Filters(procedure_id=pid, as_of=filters.as_of))
        bundle, _, absent = retrieve_direct_active_dataset(data, query, uuid5(request_id, field))
        if not bundle.evidence:
            texts.append(f'{LABELS[field]}: Chưa có đủ thông tin trong nguồn đã duyệt.')
            missing.append(field)
            continue
        answer, evidence = await answer_with_plan(ai.client, bundle, data, fields=[field])
        texts.append(answer)
        missing.extend(absent or evidence['missing_information'])
        for citation in evidence['sources']:
            citations[citation['fragment_id']] = citation
        checklist.extend(evidence.get('checklist') or [])
    g.update(status='INSUFFICIENT_DATA' if missing else 'ANSWER',
             sources=list(citations.values()), missing_information=list(dict.fromkeys(missing)),
             checklist=checklist)
    if pid == source_discrepancy.SCHOLARSHIP_ID and 'required_documents' in missing:
        texts.append(source_discrepancy.CONFLICT_NOTICE)
    return '\n\n'.join(texts), g


def transition(previous, plan, options):
    state = previous.model_copy(deep=True)
    state.pending_realtime = None
    state.awaiting_rephrase = False
    # A rejected locality is still conversation context. Dropping it would make
    # "còn lệ phí?" silently revert to the local default on the next turn.
    selected = [t for t in plan.tasks if t.kind == 'procedure']
    unresolved = [t for t in plan.tasks if t.kind == 'clarify']
    if plan.relation == 'reset':
        state.active, state.pending = [], []
    elif plan.relation == 'replace':
        state.pending = []
        if not selected:
            state.active = []
    # Keep older subjects addressable during follow-ups/additions, without
    # automatically answering them or inheriting fields across subjects.
    selected_codes = {t.code for t in selected}
    state.active = [t for t in state.active if t.code not in selected_codes]
    state.active.extend(selected)
    state.active = state.active[-2:]
    resolved_codes = {t.code for t in selected}
    state.pending = [t for t in state.pending if not resolved_codes.intersection(t.candidates)]
    if unresolved:
        state.pending = unresolved
        if not selected:
            state.active = []  # An unresolved switch must not revive an older subject.
    state.focused = list(dict.fromkeys(t.code for t in selected))
    # Keep the last numbered menu while drilling into its members.
    keep_menu = (plan.relation in {'continue', 'extend'}
                 and len(previous.displayed_options) > 1 and selected
                 and not unresolved and all(t.kind == 'procedure' for t in plan.tasks)
                 and set(options) <= set(previous.displayed_options))
    state.displayed_options = (list(previous.displayed_options) if keep_menu
                               else list(dict.fromkeys(options)))
    state.reference_limited = False
    return state


async def execute(data, plan, state, filters, ai, request_id):
    catalog = cards(data)
    index = {c['code']: c for c in catalog}
    parts, options, texts = [], [], []
    catalog_context = {}
    for i, task in enumerate(plan.tasks):
        rid = uuid5(request_id, f'task:{i}')
        pid, title = None, f'Yêu cầu {i+1}'
        g = empty_grounding(data, rid)
        effective_kind = task.kind
        if not allowed_scope(task.scope):
            effective_kind = 'outside'
            title = 'Ngoài địa phương hỗ trợ'
            if task.kind == 'procedure':
                title = index[task.code]['title'] + ' — ngoài địa phương hỗ trợ'
                options.append(task.code)
            answer = (f'Hiện mình chỉ có nguồn phục vụ {LOCAL_SCOPE}. '
                      'Mình không dùng hồ sơ, lệ phí hoặc nơi nộp ở đây để áp cho địa phương bạn yêu cầu.')
            g = empty_grounding(data, rid, 'INSUFFICIENT_DATA', ['jurisdiction'])
        elif task.kind == 'procedure':
            card = index[task.code]
            pid, title = card['id'], card['title']
            if filters.procedure_id and filters.procedure_id != pid:
                answer = 'Thủ tục được chọn trong bộ lọc khác với câu hỏi. Bạn bỏ bộ lọc hoặc xác nhận lại nhé.'
                g['missing_information'] = ['procedure_id']
            else:
                answer, g = await procedure_answer(data, task, card, filters, ai, rid)
            options.append(task.code)
        elif task.kind == 'catalog':
            from app.rag.g8.catalog_browse import execute_catalog
            result = await execute_catalog(data, task, state, filters, ai, rid, catalog)
            if result is not None:
                catalog_context = result['request']
                options.extend(result['options'])
                g.update(status=result['status'], sources=result['sources'], missing_information=result['missing'])
                parts.append({'task_id': f'task-{i+1}', 'kind': 'catalog', 'procedure_id': None,
                    'title': 'Danh mục thủ tục đang hỗ trợ', 'answer': result['answer'], 'grounding': g,
                    'catalog_items': result['items']})
                texts.append('### Danh mục thủ tục đang hỗ trợ\n\n' + result['answer'])
                continue
            title = 'Danh mục thủ tục đang hỗ trợ'
            catalog_context = {'menu_only': True, 'domains': task.domains}
            # Model-selected catalog subset is authoritative after ID validation.
            # Do not broaden two loan services into all labor/business services.
            matches = ([index[code] for code in task.candidates] if task.candidates
                       else matching_cards(catalog, task.domains))
            options.extend(c['code'] for c in matches)
            answer = ('Trong bộ dữ liệu đang phục vụ tại ' + LOCAL_SCOPE + ', có các thủ tục liên quan:\n\n'
                      + '\n'.join(f"{n}. {c['title']}" for n, c in enumerate(matches, 1))
                      + '\n\nĐây là danh mục hiện có của hệ thống, không phải toàn bộ thủ tục của lĩnh vực. '
                        'Việc liệt kê không có nghĩa mọi thủ tục đều áp dụng cho bạn; các chế độ hỗ trợ có điều kiện riêng. '
                        'Bạn có thể chọn tên hoặc số thứ tự để hỏi tiếp.')
            # Catalog listing is not a source-grounded legal answer.
            g['missing_information'] = ['procedure_id']
        elif task.kind == 'clarify':
            title = 'Cần làm rõ nhu cầu'
            candidates = [index[c] for c in task.candidates]
            options.extend(task.candidates)
            # Do not render free-form model prose as administrative facts, even
            # inside a question. Present only server-owned catalog alternatives.
            answer = ('Bạn đang muốn hỏi về ' + candidates[0]['title'] + ', đúng không?'
                      if len(candidates) == 1 else
                      'Bạn muốn thực hiện việc nào trong các mục dưới đây? Nếu chưa đúng, bạn mô tả rõ nhu cầu nhé.'
                      if candidates else 'Bạn nói rõ việc muốn thực hiện hoặc loại giấy tờ muốn xin nhé.')
            if candidates:
                answer += '\n\n' + '\n'.join(f"{n}. {c['title']}" for n, c in enumerate(candidates, 1))
            if task.question == 'G8_CONTEXT_LIMIT':
                answer = 'Mình chỉ giữ ngữ cảnh hai thủ tục gần nhất, chưa xác định chắc số thứ tự trong câu trả lời dài trước đó. Bạn ghi tên thủ tục, hoặc hỏi rõ hai thủ tục gần nhất nhé.'
            if task.question == 'G8_FILTER_UNSUPPORTED':
                answer = 'Mình chưa hỗ trợ chắc điều kiện lọc này. Bạn tách thành các danh sách riêng hoặc hỏi theo mức có phí/miễn phí, hình thức nộp, thời gian, giấy tờ hay nơi tiếp nhận nhé. Mình chưa trả một danh sách rộng hơn thay cho yêu cầu của bạn.'
            if task.question == 'G8_TASK_LIMIT':
                answer = 'Mỗi tin nhắn hỗ trợ tối đa 8 ý. Bạn chia yêu cầu thành các nhóm tối đa 8 ý để mình xử lý đầy đủ nhé.'
            if task.question == 'G8_MARRIAGE_NATIONALITY':
                answer = 'Hai bạn có quốc tịch nào? Việc từ nước ngoài về chưa đủ để chọn loại thủ tục kết hôn.'
            if not candidates and task.question == 'G8_HOUSE_BOOK_NUMBER':
                answer = ('Bạn viết “sổ nhà”, khác với “số nhà” (số địa chỉ căn nhà). '
                          'Bạn muốn hỏi loại giấy tờ nào về nhà, đất? '
                          'Mình chưa lấy lệ phí cấp số nhà để trả lời yêu cầu này.')
            if task.question == 'G8_UNACCENTED_HOUSE_REFERENCE':
                answer = ('Bạn vừa phân biệt sổ nhà với số nhà. Cụm không dấu “so nha” '
                          'chưa cho biết bạn muốn hỏi giấy tờ nhà đất hay số địa chỉ. '
                          'Bạn xác nhận loại nào để mình tra thời gian hoặc thông tin bạn cần nhé.')
            g['missing_information'] = ['procedure_id']
        elif task.kind == 'outside':
            title = 'Phần ngoài phạm vi hỗ trợ'
            answer = ('Phần yêu cầu này chưa nằm trong danh mục thủ tục hành chính mình hỗ trợ. '
                      'Mình không lấy một thủ tục khác thay thế để trả lời.')
            if task.question == 'G8_UNSAFE_ASSISTANCE':
                answer = ('Mình không hỗ trợ che giấu hành vi gây hại, làm sai lệch nguyên nhân tử vong '
                          'hoặc làm giả giấy tờ. Nếu có người còn nguy hiểm, hãy liên hệ lực lượng '
                          'khẩn cấp tại địa phương; trình báo trung thực và không can thiệp chứng cứ.')
            g = empty_grounding(data, rid, 'INSUFFICIENT_DATA', ['supported_procedure'])
        else:
            title = 'Trợ lý hành chính'
            answer = ('Mình hỗ trợ tra cứu thủ tục trong dữ liệu của ' + LOCAL_SCOPE
                      + '. Bạn có thể hỏi một hay nhiều thủ tục, hoặc xem danh mục theo lĩnh vực.')
        parts.append({'task_id': f'task-{i+1}', 'kind': effective_kind, 'procedure_id': pid,
                      'title': title, 'answer': answer, 'grounding': g})
        texts.append(f'### {title}\n\n{answer}')
    next_state = transition(state, plan, options)
    next_state.catalog_context = catalog_context
    from app.rag.g8.mentor_context import bound_context
    next_state = bound_context(next_state)
    if sum(t.kind == 'catalog' for t in plan.tasks) > 1:
        next_state.displayed_options = []
        next_state.catalog_context = {}  # Several independently numbered menus need a named selection.
    # Preserve G6 one-shot MCP consent for a single unambiguous active request.
    # Multi-task turns do not authorize a lookup for all tasks with a bare "yes".
    if len(plan.tasks) == 1 and parts[0]['kind'] == 'procedure':
        task, part = plan.tasks[0], parts[0]
        legacy = IntentContextState(corpus_version=data.version, procedure_id=part['procedure_id'],
                                    field_intents=[f for f in task.fields if f in FIELDS],
                                    jurisdiction=LOCAL_SCOPE, as_of=filters.as_of)
        part['answer'] = realtime.offer(legacy, data, part['answer'], part['grounding'],
                                        enabled=get_settings().g5_realtime_mcp_enabled)
        next_state.pending_realtime = legacy.pending_realtime.model_dump(mode='json') if legacy.pending_realtime else None
        texts[0] = f"### {part['title']}\n\n{part['answer']}"
    statuses = [p['grounding']['status'] for p in parts]
    overall = ('ANSWER' if all(s == 'ANSWER' for s in statuses) else
               'INSUFFICIENT_DATA' if any(p['grounding']['sources'] for p in parts)
               or all(s == 'INSUFFICIENT_DATA' for s in statuses) else 'NEED_CLARIFICATION')
    g = empty_grounding(data, request_id, overall)
    g['sources'] = list({s['fragment_id']: s for p in parts for s in p['grounding']['sources']}.values())
    g['parts'] = parts
    g['provider'] = 'g7-model-led'
    g['model'] = os.environ.get('G7_PLANNER_MODEL', 'G7-Qwen3-4B-Instruct')
    g['prompt_version'] = 'g7-v3-planner'
    g['missing_information'] = list(dict.fromkeys(f for p in parts for f in p['grounding']['missing_information']))
    if len(parts) == 1:
        g['checklist'] = parts[0]['grounding'].get('checklist')
        g['procedure_title'] = parts[0]['grounding'].get('procedure_title')
    content = '\n\n'.join(texts)
    if g['sources']:
        content = f'Phạm vi nguồn: {LOCAL_SCOPE}.\n\n' + content
    return content, g, next_state


async def answer_turn(*, query, raw_state, history, filters, ai, request_id, db, conversation):
    settings = get_settings()
    data = load_active_dataset(str(settings.g3_private_dataset_path), settings.rag_corpus_version,
                               settings.g3_expected_rows, settings.g3_corpus_sha256)
    state = load_state(raw_state, data.version)
    # Explicit API locality filters must obey the same hard scope as chat.
    if filters.jurisdiction and normalize(filters.jurisdiction) not in {
        normalize(LOCAL_SCOPE), 'tang nhon phu', 'phuong tang nhon phu'}:
        return (f'Hiện hệ thống chỉ hỗ trợ nguồn tại {LOCAL_SCOPE}.',
                empty_grounding(data, request_id, 'INSUFFICIENT_DATA', ['jurisdiction']), state)
    catalog = cards(data)
    index = {c['code']: c for c in catalog}
    focused = [t for t in state.active if t.code in state.focused and t.code in index]
    direct_lookup = False
    if len(focused) == 1 and allowed_scope(focused[0].scope):
        task = focused[0]
        legacy = IntentContextState(corpus_version=data.version, procedure_id=index[task.code]['id'],
            field_intents=[f for f in task.fields if f in FIELDS], jurisdiction=LOCAL_SCOPE,
            as_of=filters.as_of)
        original = source_discrepancy.original_record_reply(legacy, data, query)
        if original is not None:
            return original, empty_grounding(data, request_id, 'INSUFFICIENT_DATA', ['required_documents']), state
        if not state.pending_realtime:
            last_answer = next((m for m in reversed(history) if m.role == 'assistant'), None)
            if last_answer is not None:
                offer = realtime.explicit_request(legacy, data, query, filters,
                    answer=last_answer.content, grounding=last_answer.grounding,
                    enabled=settings.g5_realtime_mcp_enabled)
                if offer:
                    state.pending_realtime = offer.model_dump(mode='json')
                    direct_lookup = True
    if state.pending_realtime:
        legacy = IntentContextState(corpus_version=data.version,
                                    pending_realtime=realtime.PendingLookup.model_validate(state.pending_realtime))
        action, pending = realtime.consume(legacy, 'đồng ý' if direct_lookup else query, filters,
                                           enabled=settings.g5_realtime_mcp_enabled)
        state.pending_realtime = legacy.pending_realtime.model_dump(mode='json') if legacy.pending_realtime else None
        if action:
            # Persist consumption before I/O: retries cannot replay consent.
            conversation.rag_context = state.model_dump(mode='json')
            await db.commit()
            client = MCPSourceUpdateClient(url=settings.g5_source_mcp_url,
                                            timeout_seconds=settings.g5_realtime_mcp_timeout_seconds) if action == 'yes' else None
            answer, g = await realtime.resolve(pending, action, client)
            if pending.awaiting_variant:
                state.pending_realtime = pending.model_dump(mode='json')
            return answer, g, state
    query = unicodedata.normalize('NFC', query)
    try:
        from app.rag.g8.graph import run_turn
        result = await run_turn(query=query, state=state, catalog=catalog, history=history,
            ai=ai, request_id=request_id, data=data, filters=filters,
            understand=understand, execute=execute)
    except APIError as exc:
        # A failed switch must not make the next short follow-up answer the old
        # topic. Keep the durable messages, invalidate only the derived context.
        state.active, state.pending, state.focused, state.displayed_options = [], [], [], []
        state.pending_realtime = None
        state.awaiting_rephrase = True
        conversation.rag_context = state.model_dump(mode='json')
        await db.commit()
        if exc.code == 'G7_PLAN_INVALID':
            # Semantic contract failures are a conversational clarification, not
            # a transport failure. Never execute the rejected plan or reuse focus.
            # Keep this distinguishable in evidence/evaluation, not a fake success.
            answer = ('Mình chưa phân biệt chắc việc bạn muốn làm hoặc địa phương bạn muốn nộp. '
                      'Bạn nói rõ nhu cầu hiện tại và nơi muốn thực hiện nhé. '
                      'Mình chưa dùng thủ tục cũ để trả lời câu này.')
            g = empty_grounding(data, request_id, missing=['intent_resolution'])
            g['provider'], g['model'] = 'g7-safe-clarification', 'no-model-answer'
            g['parts'] = [{'task_id': 'clarification', 'kind': 'clarify', 'procedure_id': None,
                           'title': 'Cần làm rõ yêu cầu', 'answer': answer,
                           'grounding': dict(g)}]
            return answer, g, state
        raise
    return result
