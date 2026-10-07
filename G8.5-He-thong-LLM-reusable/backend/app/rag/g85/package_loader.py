"""Versioned knowledge packages; no administrative imports or domain rules.

The manifest is an operator-controlled integrity boundary, not a signature or a
claim of factual accuracy. Source text is preserved; only APPROVED passages serve.
"""
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .knowledge import Package, Passage


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_package(root, schema_path, policy_registry=None):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 'g85-package-v1':
        raise ValueError('PACKAGE_SCHEMA_UNSUPPORTED')
    required = {'profile.json', 'entities.jsonl', 'sources.jsonl', 'passages.jsonl', 'groups.json'}
    if set(manifest['files']) != required:
        raise ValueError('PACKAGE_FILES_INCOMPLETE')
    values = {}
    for name, expected in manifest['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('PACKAGE_FILE_OUTSIDE_ROOT_OR_MISSING')
        data = path.read_bytes()
        if digest(data) != expected['sha256']:
            raise ValueError('PACKAGE_CHECKSUM_MISMATCH: ' + name)
        text = data.decode('utf-8')
        values[name] = [json.loads(line) for line in text.splitlines() if line.strip()] if name.endswith('.jsonl') else json.loads(text)
        if name.endswith('.jsonl') and len(values[name]) != expected['count']:
            raise ValueError('PACKAGE_COUNT_MISMATCH')
    profile = values['profile.json']
    Draft202012Validator(json.loads(Path(schema_path).read_text(encoding='utf-8'))).validate(profile)
    if profile['ingestion']['adapter'] != 'documents_v1':
        raise ValueError('PACKAGE_ADAPTER_UNSUPPORTED')
    # Extensions must be explicitly installed; never execute arbitrary data code.
    registry = policy_registry or {}
    for name in profile.get('policies', []):
        if name not in registry or not callable(registry[name]):
            raise ValueError('POLICY_NOT_INSTALLED: ' + name)
    if profile['retrieval']['document_search'] != 'lexical' or profile['retrieval']['reranker'] != 'off':
        raise ValueError('RETRIEVAL_IMPLEMENTATION_NOT_INSTALLED')
    if profile['capabilities']['checklist']:
        raise ValueError('CHECKLIST_EXTENSION_NOT_INSTALLED')
    if profile['limits']['max_tasks'] > 8 or profile['limits']['max_search_rounds'] != 1 or profile['limits']['max_synthesis_calls'] != 1:
        raise ValueError('UNSUPPORTED_RUNTIME_LIMIT')
    attributes = {a['id'] for a in profile['attributes']}
    if len(attributes) != len(profile['attributes']) or profile['retrieval']['exact_lookup'] and not attributes:
        raise ValueError('INVALID_ATTRIBUTE_SCHEMA')

    def index(rows, key):
        result = {row[key]: row for row in rows}
        if len(result) != len(rows) or any(not isinstance(k, str) or not k for k in result):
            raise ValueError('DUPLICATE_OR_INVALID_ID: ' + key)
        return result

    catalog = index(values['entities.jsonl'], 'key')
    # This lightweight planner includes its complete catalog in the prompt.
    # Reject an unsupported scale at onboarding rather than crashing mid-chat.
    if len(catalog) > 64:
        raise ValueError('CATALOG_EXCEEDS_64_ENTITIES_REQUIRES_CANDIDATE_RETRIEVAL')
    sources = index(values['sources.jsonl'], 'id')
    groups = values['groups.json']
    rows = values['passages.jsonl']
    index(rows, 'id')
    for card in catalog.values():
        if not card.get('title') or not set(card.get('domains', [])) <= set(groups):
            raise ValueError('INVALID_CATALOG_METADATA')
        for attribute, facts in card.get('facts', {}).items():
            if attribute not in attributes or not isinstance(facts, list) or any(not isinstance(v, str) or not v for v in facts):
                raise ValueError('INVALID_CATALOG_FACTS')
    passages = []
    for row in rows:
        if row['source_id'] not in sources or row.get('entity') and row['entity'] not in catalog:
            raise ValueError('DANGLING_PASSAGE_REFERENCE')
        if row.get('attribute') and row['attribute'] not in attributes:
            raise ValueError('UNKNOWN_PASSAGE_ATTRIBUTE')
        if not isinstance(row['text'], str) or not row['text'].strip() or digest(row['text'].encode('utf-8')) != row['content_sha256']:
            raise ValueError('PASSAGE_CONTENT_INVALID')
        if row.get('review_status') != 'APPROVED':
            continue
        source = sources[row['source_id']]
        metadata = dict(row.get('metadata') or {})
        metadata.update(content_sha256=row['content_sha256'], dataset_version=manifest['dataset_version'],
                        review_status='APPROVED', source_locator=row.get('source_locator'), attribute=row.get('attribute', ''))
        passages.append(Passage(id=row['id'], source_id=row['source_id'], text=row['text'], title=row['title'],
            entity=row.get('entity', ''), attribute=row.get('attribute', ''), url=source.get('url'), metadata=metadata))
    if len(passages) != manifest['approved_passages'] or not passages:
        raise ValueError('APPROVED_PASSAGE_COUNT_MISMATCH')
    # Version changes when any manifest-controlled package bytes change, even if
    # an operator forgets to bump the friendly source version.
    version = manifest['dataset_version'] + '-pkg-' + digest((root / 'manifest.json').read_bytes())[:12]
    package = Package(profile, version, catalog, groups, [], passages, None)
    if profile.get('policies'):
        def review(plan, query, state):
            for name in profile['policies']:
                plan = registry[name](plan, query, state, package)
            return plan
        package.policy = review
    if profile['retrieval']['exact_lookup']:
        async def direct(task, card, filters, rid):
            from .engine import grounding
            g = grounding(package, rid)
            if not task.attributes:
                return 'Bạn muốn hỏi ' + ', '.join(a['label'] for a in profile['attributes']) + ' hay tất cả thông tin của mục này?', g
            labels = {a['id']: a['label'] for a in profile['attributes']}
            answers, citations, missing, field_results = [], {}, [], []
            for attribute in task.attributes:
                found = [p for p in passages if p.entity == task.entity and p.attribute == attribute and package.valid_at(p, filters.as_of)]
                if found:
                    answers.append(labels[attribute] + ':\n\n' + '\n\n'.join(p.text for p in found))
                    citations.update({p.id: p.citation() for p in found})
                else:
                    answers.append(labels[attribute] + ': Chưa có thông tin trong nguồn đã duyệt.')
                    missing.append(attribute)
                field_results.append({'attribute': attribute, 'status': 'answered' if found else 'insufficient_data',
                    'evidence_ids': [p.id for p in found], 'value': '\n\n'.join(p.text for p in found)})
            g.update(status='INSUFFICIENT_DATA' if missing else 'ANSWER', sources=list(citations.values()), missing_information=missing,
                     retrieval_mode='direct_catalog', generation_policy='verbatim_field_lookup', field_results=field_results)
            return '\n\n'.join(answers), g
        package.direct = direct
    return package
