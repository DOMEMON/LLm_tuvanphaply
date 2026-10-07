"""Bounded planning graph and evidence execution graph.

Only the completion node calls a model. Maximum two calls (initial + one repair).
No graph checkpointer: authenticated PostgreSQL conversation state is the single
durable memory owner. Graph state, callbacks and outputs are request-local.
"""
import json
import logging
import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.errors import APIError

log = logging.getLogger('backend.chat')


class Planning(TypedDict, total=False):
    messages: list
    completion: Any
    validate: Any
    request_id: Any
    attempts: int
    raw: str
    plan: Any
    error: str


def reason(exc):
    # Never include pydantic input values, prompts or model text in logs.
    value = str(exc)
    return value if value.isupper() and len(value) <= 80 else type(exc).__name__


async def complete(state: Planning):
    try:
        # LangChain Core rather than the monolithic agent package. Preserve the
        # existing llama.cpp transport, JSON grammar, token metrics and pooling.
        raw = await RunnableLambda(state['completion'], name='local_structured_plan').ainvoke(
            state['messages'], config={'callbacks': [], 'tags': ['g8-local']})
        return {'raw': raw, 'error': '', 'attempts': state['attempts'] + 1}
    except (ValueError, KeyError, IndexError) as exc:
        return {'raw': '', 'error': reason(exc), 'attempts': state['attempts'] + 1}


async def validate(state: Planning):
    error = state['error']
    if not error:
        try:
            plan = await RunnableLambda(state['validate'], name='validate_typed_plan').ainvoke(
                state['raw'], config={'callbacks': []})
            log.info('g8_graph_plan request_id=%s attempts=%s tasks=%s',
                     state['request_id'], state['attempts'],
                     json.dumps([{'kind': t.kind, 'code': t.code, 'fields': t.fields}
                                 for t in plan.tasks]))
            return {'plan': plan, 'error': ''}
        except (ValidationError, ValueError, KeyError, IndexError) as exc:
            error = reason(exc)
    log.warning('g8_graph_rejected request_id=%s attempt=%s reason=%s',
                state['request_id'], state['attempts'], error)
    return {'plan': None, 'error': error}


def after_validate(state: Planning):
    return 'done' if state.get('plan') is not None else 'repair' if state['attempts'] < 2 else 'failed'


def repair(state: Planning):
    messages = list(state['messages'])
    hint = ''
    if state['error'] in {'SCOPE_WITHOUT_USER_EVIDENCE', 'SCOPE_NAME_NOT_IN_QUOTE'}:
        hint = (' Với thủ tục mới không ghi nơi thực hiện trong CURRENT_USER, scope={places:[],quote:""}. '
                'Không sao chép địa phương từ lịch sử, PROFILE hay thủ tục khác. '
                'Chỉ kế thừa scope cho chính thủ tục cũ đang hỏi tiếp. Giữ đủ từng ý và field riêng.')
        # Retry with authoritative structured state, excluding narrative history
        # that caused invented geography. Preserve validated scopes themselves.
        messages = [dict(m) for m in messages]
        for message in messages:
            prefix = 'CONTEXT ONLY, not a new request:\n'
            if message['content'].startswith(prefix):
                context = json.loads(message['content'][len(prefix):])
                context['RECENT_USER_REQUESTS'] = []
                for key in ('active', 'pending'):
                    for task in context.get('STATE', {}).get(key, []):
                        task.pop('quote', None)
                message['content'] = prefix + json.dumps(context, ensure_ascii=False)
    if state['error'] == 'MIXED_REQUEST_DROPPED_SUPPORTED_NEED':
        hint = (' Chỉ ý nấu ăn là outside. Phần hỏi thủ tục phải được phân tích riêng: '
                'nhận cha mẹ con là thủ tục hỗ trợ; nếu không rõ nhận con hay nhận con nuôi '
                'thì tạo clarify riêng. Không bỏ ý thứ hai vì ý thứ nhất ngoài phạm vi.')
    if state['error'].startswith('BACKGROUND_DOCUMENT_USE_'):
        code = state['error'].removeprefix('BACKGROUND_DOCUMENT_USE_').lower()
        hint = (' Người dùng đang bổ sung thông tin giấy tờ/tiền sử vào hồ sơ đang hỏi. '
                'Không có yêu cầu đổi thủ tục. Giữ thủ tục đang được hỏi '
                + code + ' trong STATE/ACTIVE_FOCUS; lấy field theo câu hỏi hiện tại, '
                'không lấy tên giấy tờ đang có làm thủ tục mới.')
    if state['error'] == 'LOCAL_SUPPORTED_SERVICE_MUST_NOT_INHERIT_OTHER_TASK_OUTSIDE_SCOPE':
        hint = (' Tách từng nơi nộp khỏi nơi người dùng đang sống. Phần có nơi nộp '
                'Tăng Nhơn Phú vẫn là procedure có scope riêng; chỉ phần yêu cầu '
                'thực hiện tại địa phương khác mới ngoài phạm vi.')
    if state['error'] == 'UNKNOWN_DOMAIN':
        hint = (' Chỉ dùng khóa DOMAIN_NAMES có sẵn. Nếu lĩnh vực người dùng hỏi '
                'không có trong danh mục, dùng clarify với domains=[], candidates=[] '
                'hoặc outside; không chọn lĩnh vực gần giống để lấp chỗ trống.')
    if state['error'] == 'FIELDS_MUST_MATCH_POSITIVE_CURRENT_TASK_NOT_HISTORY_OR_NEGATION':
        hint = (' Gắn field riêng cho từng mệnh đề: mang giấy/bổ sung giấy là required_documents, '
                'mấy ngày/bao lâu là processing_times, nộp ở đâu là receiving_authority. '
                'Không thêm field được phủ định hoặc chỉ nhắc trong tên thủ tục. '
                'Thông tin yêu cầu hiện tại thay thế field của lượt trước.')
    if state['error'] == 'CORRECTION_REQUIRES_PROCEDURE_OR_CLARIFICATION_NOT_CATALOG':
        hint = (' Đây là lời đính chính thủ tục, không phải yêu cầu liệt kê. '
                'Dùng thủ tục người dùng vừa xác nhận và field đang được hỏi trong CONTEXT.')
    if state['error'].startswith('MULTI_LOCATION_'):
        hint = (' Mỗi task phải có scope.quote riêng trích đúng nơi yêu cầu thực hiện '
                'của task đó. Nơi cư trú hoặc nơi của task khác không phải scope của task này. '
                'Không sao chép toàn câu nhiều địa phương vào mọi quote.')
    # Keep the real query last. Appending an error as a new user request both
    # changes CURRENT_USER and anchors the model on its rejected answer.
    position = 1 if messages and messages[0]['role'] == 'system' else 0
    messages.insert(position, {'role': 'system', 'content': 'Kế hoạch trước chưa hợp lệ: ' + state['error'] + hint
                     + '. Đọc lại CURRENT_USER và trả JSON đầy đủ, đúng schema. '
                     'Không tự suy đoán địa phương, không tự thu hẹp một lĩnh vực về thủ tục cũ.'})
    return {'messages': messages}


def failed(state: Planning):
    raise APIError(502, 'G7_PLAN_INVALID', 'Chưa phân biệt chắc yêu cầu. Bạn làm rõ giúp mình nhé.')


def make_planning_graph():
    graph = StateGraph(Planning)
    graph.add_node('completion', complete)
    graph.add_node('validate', validate)
    graph.add_node('repair', repair)
    graph.add_node('failed', failed)
    graph.add_edge(START, 'completion')
    graph.add_edge('completion', 'validate')
    graph.add_conditional_edges('validate', after_validate,
                                {'done': END, 'repair': 'repair', 'failed': 'failed'})
    graph.add_edge('repair', 'completion')
    graph.add_edge('failed', END)
    return graph.compile()


PLANNING = make_planning_graph()


async def plan_with_graph(messages, completion, validator, request_id):
    result = await PLANNING.ainvoke({'messages': messages, 'completion': completion,
        'validate': validator, 'request_id': request_id, 'attempts': 0},
        config={'recursion_limit': 8, 'callbacks': []})
    return result['plan']


class Turn(TypedDict, total=False):
    context: dict
    plan: Any
    result: Any


async def plan_turn(state: Turn):
    c = state['context']
    start = time.monotonic()
    plan = await c['understand'](c['query'], c['state'], c['catalog'], c['history'],
                                 c['ai'].client, c['request_id'])
    log.info('g8_graph_stage request_id=%s node=plan ms=%s',
             c['request_id'], round((time.monotonic() - start) * 1000))
    return {'plan': plan}


async def execute_turn(state: Turn):
    c = state['context']
    start = time.monotonic()
    result = await c['execute'](c['data'], state['plan'], c['state'], c['filters'], c['ai'], c['request_id'])
    log.info('g8_graph_stage request_id=%s node=evidence_execute ms=%s',
             c['request_id'], round((time.monotonic() - start) * 1000))
    return {'result': result}


def verify_evidence(state: Turn):
    # This checks evidence *binding*, not the truth/current validity of a law.
    # No generated answer is accepted as an alternative source.
    _, grounding, _ = state['result']
    for part in grounding.get('parts', []):
        citations = part['grounding'].get('sources') or []
        if part['kind'] == 'catalog' and part.get('catalog_items') is not None:
            permitted = {fid: row['procedure_id'] for row in part['catalog_items'] for fid in row['fragment_ids']}
            for source in citations:
                fid = source['fragment_id']
                if fid not in permitted or fid.split(':', 1)[0] != permitted[fid]:
                    raise APIError(502, 'G8_EVIDENCE_BINDING', 'Nguồn lọc danh mục chưa khớp thủ tục.')
            continue
        if citations and part['kind'] != 'procedure':
            raise APIError(502, 'G8_EVIDENCE_BINDING', 'Chưa đối chiếu được nguồn của câu trả lời.')
        for source in citations:
            if source['fragment_id'].split(':', 1)[0] != part['procedure_id']:
                raise APIError(502, 'G8_EVIDENCE_BINDING', 'Nguồn chưa khớp thủ tục yêu cầu.')
    grounding['prompt_version'] = 'g8-langgraph-v1'
    return {'result': state['result']}


def make_turn_graph():
    graph = StateGraph(Turn)
    graph.add_node('plan', plan_turn)
    graph.add_node('evidence_execute', execute_turn)
    graph.add_node('verify_evidence', verify_evidence)
    graph.add_edge(START, 'plan')
    graph.add_edge('plan', 'evidence_execute')
    graph.add_edge('evidence_execute', 'verify_evidence')
    graph.add_edge('verify_evidence', END)
    return graph.compile()


TURN = make_turn_graph()


async def run_turn(**context):
    result = await TURN.ainvoke({'context': context}, config={'recursion_limit': 5, 'callbacks': []})
    return result['result']
