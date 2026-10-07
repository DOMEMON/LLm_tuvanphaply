"""Small evidence-backed catalog algebra, configured by each domain adapter.

Only reviewed group aliases and finite facet values are normalized here. Free
questions and independent information requests remain with the semantic planner.
"""
import re
import unicodedata


def fold(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', value.casefold())
                    if unicodedata.category(c) != 'Mn').replace('đ', 'd')
    return ' '.join(re.sub(r'[^\w\s]', ' ', value).split())


def mentioned(text, phrase):
    return ' ' + fold(phrase) + ' ' in ' ' + fold(text) + ' '


def parse_catalog(query, config, previous=None):
    """Return a bounded request, or None when this is not a pure catalog query.

    This grammar composes groups and facets, not whole example utterances.
    Unparsed constraints must not silently become an unfiltered result.
    """
    q = fold(query)
    previous = previous or {}
    groups = [g for g in config.get('groups', []) if any(mentioned(q, a) for a in g['aliases'])]
    follow = bool(re.search(r'nhu tren|nhu cu|van cung|giu nguyen|trong (?:so|nhom|danh sach) (?:do|tren)|cac muc tren', q))
    browse = bool(re.search(r'linh vuc|cac thu tuc|nhung thu tuc|thu tuc nao|liet ke|ke ten|danh sach|danh muc', q))
    if not groups and not (previous and follow):
        return None
    # Keep geographic parsing with the planner; never erase a foreign place.
    if re.search(r'\b(?:tai|o|sang|ngoai)\s+', q):
        return None
    # A particular procedure, a negative group or unrelated clause needs a plan.
    if re.search(r'khai sinh|khai tu|ket hon|cha me con|tinh trang hon nhan|nau|chien|dat ve|visa|vay von|so nha|nha o|chung thuc|che giau|lam gia|giet|trich luc|ban sao', q):
        return None
    if re.search(r'khong (?:liet ke|hoi|can)|dung|chua|tru |ngoai tru|bo nhom|bo linh vuc', q):
        return None
    if re.search(r'ho so|giay to|le phi|mien phi|bao lau|thoi gian|noi nop|dia diem', q):
        # Broad "hộ tịch cần giấy tờ gì" remains a menu, but explicit per-item
        # questions and compound/filter requirements must reach the planner.
        if re.search(r'tung|moi|tat ca|toan bo|le phi|mien phi|bao lau|thoi gian|noi nop|dia diem', q):
            return None
    if not browse and not follow and not re.search(r'ho (?:tich|tuc)', q):
        return None
    domains = list(dict.fromkeys(d for g in groups for d in g['domains'])) or previous.get('domains', [])
    predicates = []
    for field, facet in config.get('facets', {}).items():
        values = [key for key, aliases in facet['aliases'].items() if any(mentioned(q, a) for a in aliases)]
        if values:
            exact = bool(re.search(r'\bchi (?:co the |duoc |chap nhan |ho tro |nop |lam |thuc hien )*(?:qua |bang )?(?:online|truc tuyen|truc tiep)\b|\bduy nhat\b', q))
            predicates.append({'field': field, 'values': values, 'operator': 'equals' if exact else 'contains_all'})
    if not predicates and follow:
        predicates = previous.get('predicates', [])
    if re.search(r'khong (?:can |duoc |ho tro )?(?:nop )?(?:online|truc tuyen|truc tiep)', q):
        return None  # Negated predicates are intentionally not guessed.
    # Two groups with their own different filters cannot be collapsed together.
    if len(groups) > 1 and re.search(r'\bcon\b', q) and predicates:
        return None
    return {'domains': domains, 'predicates': predicates}


def filter_catalog(request, cards, facts, config):
    """Facts maps (entity, field) to approved [(text, citation)] only.

    Missing/unrecognized/conflicting values are unknown, never a match.
    Return stable unique entries with the exact evidence supporting each filter.
    """
    matches, unknown = [], []
    seen = set()
    for card in cards:
        key = card['key']
        if key in seen or not set(card.get('domains', [])) & set(request['domains']):
            continue
        seen.add(key)
        supporting, raw_values, result = [], [], True
        for predicate in request.get('predicates', []):
            facet = config['facets'].get(predicate['field'])
            rows = facts.get((key, predicate['field']), [])
            mapping = {fold(k): set(v) for k, v in facet['values'].items()} if facet else {}
            observed = [mapping.get(fold(text)) for text, _ in rows]
            if not observed or any(v is None for v in observed) or any(v != observed[0] for v in observed):
                result = None
                break
            wanted = set(predicate['values'])
            if not (observed[0] == wanted if predicate['operator'] == 'equals' else wanted <= observed[0]):
                result = False
                break
            supporting.extend(c for _, c in rows)
            raw_values.extend(text for text, _ in rows)
        if result is None:
            unknown.append(key)
        elif result:
            matches.append({'card': card, 'sources': list({c['fragment_id']: c for c in supporting}.values()),
                            'values': list(dict.fromkeys(raw_values))})
    return matches, unknown


def render_catalog(matches, unknown, request):
    filtered = bool(request.get('predicates'))
    intro = ('Các mục có nguồn xác nhận đáp ứng điều kiện lọc:' if filtered
             else 'Trong danh mục dữ liệu hiện có, các thủ tục thuộc nhóm bạn hỏi gồm:')
    lines = [f"{i}. {r['card']['title']}" + (' — ' + '; '.join(r['values']) if filtered else '')
             for i, r in enumerate(matches, 1)]
    text = intro + '\n\n' + ('\n'.join(lines) if lines else 'Chưa tìm thấy mục đáp ứng trong nguồn đã duyệt.')
    if filtered:
        exclusive = any(p['operator'] == 'equals' for p in request['predicates'])
        text += ('\n\nĐã lọc theo đúng hình thức được ghi trong nguồn; mục ghi “Cả hai” không thuộc nhóm chỉ một hình thức.'
                 if exclusive else '\n\nMục ghi “Cả hai” trong nguồn có hỗ trợ cả trực tuyến và trực tiếp.')
    if unknown:
        text += f'\n\nCó {len(unknown)} mục chưa đủ dữ liệu xác nhận điều kiện nên chưa đưa vào kết quả.'
    text += '\n\nĐây là danh mục trong bộ dữ liệu đang phục vụ, không phải toàn bộ thủ tục của lĩnh vực. Bạn có thể chọn tên hoặc số thứ tự để hỏi tiếp.'
    return text
