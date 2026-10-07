"""Reject contradicted plans; let the model repair once, never supply legal facts."""
import re
import unicodedata


UNSAFE_MARKER = 'G8_UNSAFE_ASSISTANCE'


def semantic_check(plan, query, catalog):
    q = unicodedata.normalize('NFC', query).casefold()
    # Catalog membership is reviewed metadata. A narrow marriage browse must
    # not be broadened by the parent's civil tag or unrelated candidate IDs.
    marriage_browse = (re.search(r'liên quan.{0,20}(?:kết hôn|hôn nhân)', q)
        and not re.search(r'hộ tịch|khai sinh|khai tử|cha mẹ con|qua đời|mất|chết|vay|đất|nhà ở', q))
    if marriage_browse and len(plan.tasks) == 1 and plan.tasks[0].kind == 'catalog':
        task = plan.tasks[0]
        allowed = {c['code'] for c in catalog if 'marriage' in c['domains']}
        if 'marriage' in task.domains or set(task.candidates) & allowed:
            if task.candidates:
                task.candidates = [c for c in task.candidates if c in allowed]
            task.domains = ['marriage']
    docs_only = re.match(r'\s*(?:(?:tôi|mình|em)\s+)?(?:muốn\s+)?'
                         r'(?:hỏi|biết|xem|xin|cho\s+xem)\s+(?:thành phần\s+)?hồ sơ\b', q)
    other_fields = re.search(r'phí|tiền|ở đâu|nơi nộp|chỗ nào|bao lâu|mấy ngày|thời gian|'
                             r'thời hạn|online|trực tuyến|hình thức|tất cả|toàn bộ|'
                             r'trình tự|các bước|căn cứ|nghị định|điều kiện|đối tượng', q)
    if docs_only and not other_fields and len(plan.tasks) == 1 and plan.tasks[0].kind == 'procedure':
        # Narrow the information fields, never choose a procedure by this cue.
        plan.tasks[0].fields = ['required_documents']
    # A broad category is not automatically the previous focused procedure.
    # Explicit subprocedures and deictic follow-ups stay model-owned.
    civil = re.search(r'\bhộ tịch\b', q)
    specified = re.search(r'khai sinh|khai tử|kết hôn|hôn nhân|độc thân|cha mẹ con|cha con|mẹ con', q)
    deictic = re.search(r'hộ tịch\s+(?:này|đó|vừa nói)', q)
    if civil and not specified and not deictic:
        allowed = {c['code'] for c in catalog if 'civil' in c['domains']}
        explicit_all = re.search(r'tất cả|toàn bộ|mỗi thủ tục|từng thủ tục', q)
        procedures = [t for t in plan.tasks if t.kind == 'procedure']
        if explicit_all and procedures and {t.code for t in procedures} == allowed:
            return plan
        for task in plan.tasks:
            if len(plan.tasks) > 1 and not re.search(r'\bhộ tịch\b', task.quote.casefold()):
                continue  # A separately quoted business/etc request is not civil.
            if task.kind == 'procedure':
                raise ValueError('BROAD_DOMAIN_REQUIRES_CATALOG_OR_CLARIFY')
            if task.kind == 'catalog':
                # Whole-domain browsing is not a top-k recommendation. The
                # model selected civil and all candidates belong to it. The
                # wire limits candidates to five, but civil has six entries.
                expanded = {c['code'] for c in catalog if set(c['domains']) & set(task.domains)}
                if expanded == allowed and set(task.candidates) <= allowed:
                    task.domains, task.candidates = ['civil'], []
                selected = (set(task.candidates) if task.candidates else
                    {c['code'] for c in catalog if set(c['domains']) & set(task.domains)})
                if selected != allowed:
                    raise ValueError('CIVIL_DOMAIN_REQUIRES_DOMAINS_CIVIL_ONLY')
                # Equivalent domain unions (civil + birth + marriage) are safe;
                # canonicalize metadata, not the user's words or legal facts.
                task.domains, task.candidates = ['civil'], []
            if task.kind == 'clarify' and not set(task.candidates) <= allowed:
                raise ValueError('CIVIL_CLARIFICATION_HAS_UNRELATED_CANDIDATES')
    return plan
