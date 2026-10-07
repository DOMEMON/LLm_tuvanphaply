"""HTTP shell boundary for a single active, domain-neutral knowledge package."""
from functools import lru_cache
import os
from pathlib import Path

from app.errors import APIError

from .engine import run_turn
from .package_loader import read_package
from .transport import TRANSPORT_ERRORS
from .extensions import POLICIES
from .contracts import current_package_history


@lru_cache(maxsize=1)
def load_package():
    schema = Path(__file__).resolve().parents[3] / 'g85-domain.schema.json'
    return read_package(os.environ['G85_PACKAGE_PATH'], schema, POLICIES)


def domain_metadata():
    package = load_package()
    return {'id': package.profile['id'], 'title': package.profile['title'], 'version': package.version,
        'entities': len(package.catalog), 'approved_search_passages': len(package.passages),
        'retrieval': 'lexical', 'external_lookup': False, 'policies': package.profile.get('policies', []), 'runtime': 'g85-clean-v1',
        'clean': not package.profile.get('policies') and not package.profile.get('planner_instructions'),
        'capabilities': package.profile['capabilities'], 'adapter': 'documents_v1',
        'attributes': package.profile['attributes'],
        'orchestration': 'langgraph', 'retriever_interface': 'langchain-core',
        'suggestions': [{'title': c['title'], 'prompt': c['title']} for c in list(package.catalog.values())[:3]]}


async def answer_turn(*, query, raw_state, history, filters, ai, request_id, db, conversation):
    try:
        package = load_package()
        history = current_package_history(history, raw_state, package.profile, package.version)
        return await run_turn(query, raw_state, history, filters, ai.client, request_id, package)
    except TRANSPORT_ERRORS as exc:
        raise APIError(503, 'G85_MODEL_UNAVAILABLE', 'Bộ hiểu yêu cầu đang bận hoặc chưa sẵn sàng. Bạn thử lại nhé.') from exc
