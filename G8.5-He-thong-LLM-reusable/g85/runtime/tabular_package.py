"""Load portable tabular packages, verify cells, and attach generic capabilities."""
from collections import Counter
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator

from ..package_builder import FORMAT, MAX_CELLS, records, sha, validate_mapping
from ..tabular_lab.table import Table
from .knowledge import Package, Passage, tokens


def read_tabular_package(root, schema_path):
    root = Path(root).resolve()
    manifest_bytes = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    required = {'profile.json', 'table.json', 'entities.jsonl', 'passages.jsonl', 'sources.jsonl', 'groups.json'}
    names = set(manifest['files'])
    source_names = names - required
    if (manifest['schema_version'] != FORMAT or not required <= names
            or source_names not in ({'source.csv'}, {'source.xlsx'})):
        raise ValueError('PACKAGE_FILES_INCOMPLETE')
    values = {}
    for name, expected in manifest['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('PACKAGE_FILE_OUTSIDE_ROOT_OR_MISSING')
        if path.stat().st_size != expected['bytes']:
            raise ValueError('PACKAGE_CHECKSUM_MISMATCH')
        data = path.read_bytes()
        if sha(data) != expected['sha256']:
            raise ValueError('PACKAGE_CHECKSUM_MISMATCH')
        if name in required:
            values[name] = [json.loads(line) for line in data.decode('utf-8').splitlines() if line.strip()] if name.endswith('.jsonl') else json.loads(data)
    descriptor = values['table.json']
    if descriptor['source_file'] not in source_names:
        raise ValueError('PACKAGE_SOURCE_MISMATCH')
    physical, mapping = descriptor['physical'], descriptor['mapping']
    table = Table(root / descriptor['source_file'], sheet=physical['format'].get('sheet'),
                  missing_values=physical['missing_values'])
    try:
        if table.hash != manifest['source_sha256'] or table.profile != physical:
            raise ValueError('PACKAGE_SOURCE_PROFILE_MISMATCH')
        if len(table.headers) > 32 or len(table.rows) * len(table.headers) > MAX_CELLS:
            raise ValueError('PACKAGE_TABLE_LIMIT')
        validate_mapping(table, mapping)
        entities, cells = records(table, mapping)
        if (entities != values['entities.jsonl'] or cells != values['passages.jsonl']
                or len(entities) != manifest['row_count'] or len(cells) != manifest['cell_count']):
            raise ValueError('PACKAGE_CELL_BINDING_MISMATCH')
        profile = values['profile.json']
        Draft202012Validator(json.loads(Path(schema_path).read_text(encoding='utf-8'))).validate(profile)
        if (profile['ingestion']['adapter'] != 'tabular_v1' or profile.get('policies')
                or profile.get('planner_instructions') or profile['scope']['service_regions']
                or profile['capabilities']['checklist'] or not profile['retrieval']['exact_lookup']
                or profile['retrieval']['document_search'] != 'lexical' or profile['retrieval']['reranker'] != 'off'
                or profile['limits']['max_tasks'] > 8):
            raise ValueError('UNSUPPORTED_TABULAR_PROFILE')
        expected_attrs = [{'id': f['id'], 'label': f['column'], 'description': f['description']} for f in mapping['fields']]
        if profile['attributes'] != expected_attrs:
            raise ValueError('PACKAGE_ATTRIBUTE_MAPPING_MISMATCH')
        sources = values['sources.jsonl']
        if len(sources) != 1 or sources[0]['id'] != 'table-source' or sources[0]['content_sha256'] != table.hash:
            raise ValueError('PACKAGE_SOURCE_MISMATCH')
        if set(values['groups.json']) != {'collection'}:
            raise ValueError('PACKAGE_GROUP_MISMATCH')
        passages = [Passage(c['id'], c['source_id'], c['text'], c['title'], c['entity'], c['attribute'],
            metadata={'source_locator': c['source_locator'], 'content_sha256': c['content_sha256'],
                      'verification': 'exact_source_cell', 'record_id': c['entity']}) for c in cells]
        package = Package(profile, sha(manifest_bytes), {e['key']: e for e in entities}, values['groups.json'], [], passages, None)
        package.table, package.mapping, package.root = table, mapping, root
        by_field = {(p.entity, p.attribute): p for p in passages}

        async def direct(task, card, filters, rid):
            from .engine import grounding
            g = grounding(package, rid)
            if not task.attributes:
                return 'Bạn muốn hỏi trường thông tin nào: ' + ', '.join(f['column'] for f in mapping['fields']) + '?', g
            fields, citations = [], []
            for attribute in task.attributes:
                p = by_field.get((task.entity, attribute))
                fields.append({'attribute': attribute, 'status': 'answered' if p else 'insufficient_data',
                               'value': p.text if p else None, 'evidence_ids': [p.id] if p else []})
                if p:
                    citations.append(p.citation())
            missing = [f['attribute'] for f in fields if f['status'] != 'answered']
            g.update(status='INSUFFICIENT_DATA' if missing else 'ANSWER', sources=citations,
                     missing_information=missing, field_results=fields,
                     retrieval_mode='direct_source_cell', generation_policy='verbatim_source_cell')
            labels = {f['id']: f['column'] for f in mapping['fields']}
            return '\n\n'.join(labels[f['attribute']] + ': ' + (f['value'] if f['value'] is not None else 'Chưa có dữ liệu.') for f in fields), g

        package.direct = direct
        # Build candidate statistics once per loaded package, not per model call.
        terms = {e['key']: Counter(tokens(e['title'])) for e in entities}
        frequencies = Counter(t for counts in terms.values() for t in counts)

        def select_catalog(query, state, limit=48):
            q = set(tokens(query))
            priority = list(dict.fromkeys(state.displayed_options[-2:] + state.focused[-2:]))
            from .tabular_requests import explicit_rows
            requested = [f'row_{i}' for i in explicit_rows(query) if f'row_{i}' in package.catalog]
            priority = list(dict.fromkeys(requested + priority))
            ranked = sorted(terms, key=lambda key: (-sum(
                math.log(1 + len(terms) / frequencies[t]) for t in q & terms[key].keys()), key))
            selected = list(dict.fromkeys([k for k in priority if k in terms] + ranked))[:limit]
            return {k: package.catalog[k] for k in selected}

        package.select_catalog = select_catalog
        return package
    except BaseException:
        table.close()
        raise
