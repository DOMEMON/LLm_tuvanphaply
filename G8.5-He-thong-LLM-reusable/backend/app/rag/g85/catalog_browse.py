"""Catalog composition/filtering uses configured groups and approved passages."""
def legacy_functions():
    # Import the historical domain parser only for an explicitly opted-in package.
    # A clean package never loads it.
    try:
        from .catalog_query import parse_catalog, filter_catalog, render_catalog
    except ModuleNotFoundError:  # Repository tests; prepare copies the shared module.
        from shared.catalog_query import parse_catalog, filter_catalog, render_catalog
    return parse_catalog, filter_catalog, render_catalog


def request_for(query, state, package):
    if not package.catalog_config:
        return None
    parse_catalog, _, _ = legacy_functions()
    return parse_catalog(query, package.catalog_config, state.catalog_context)


def execute_catalog(query, state, package, as_of=None):
    request = request_for(query, state, package)
    if request is None:
        return None
    _, filter_catalog, render_catalog = legacy_functions()
    facts = {}
    for passage in package.passages:
        if as_of:
            stamp = as_of.isoformat()
            if (passage.metadata.get('effective_from') and stamp < passage.metadata['effective_from']
                    or passage.metadata.get('effective_to') and stamp > passage.metadata['effective_to']):
                continue
        facts.setdefault((passage.entity, passage.attribute), []).append((passage.text, passage.citation()))
    matches, unknown = filter_catalog(request, package.catalog.values(), facts, package.catalog_config)
    return {'answer': render_catalog(matches, unknown, request), 'request': request,
        'options': [r['card']['key'] for r in matches],
        'sources': list({s['fragment_id']: s for r in matches for s in r['sources']}.values()),
        'items': [{'procedure_id': r['card'].get('record_id'), 'entity': r['card']['key'],
                   'fragment_ids': [s['fragment_id'] for s in r['sources']]} for r in matches],
        'missing': ['catalog_filter_evidence'] if unknown else [],
        'status': 'INSUFFICIENT_DATA' if unknown else 'ANSWER'}
