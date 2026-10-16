"""Execute model-selected predicates over approved categorical data, no NLP rules."""


def select(package, candidates, predicates, as_of=None):
    if not predicates:
        return candidates, [], []
    matched, uncertain, citations = [], [], {}
    for card in candidates:
        unknown, excluded, supports = False, False, []
        for predicate in predicates:
            facts = set(card.get('facts', {}).get(predicate.attribute, []))
            evidence = [p for p in package.passages if p.entity == card['key']
                        and p.attribute == predicate.attribute and package.valid_at(p, as_of)]
            if not facts or not evidence:
                unknown = True
                continue
            wanted = set(predicate.values)
            match = (bool(facts & wanted) if predicate.operator == 'contains_any' else
                     facts == wanted if predicate.operator == 'equals_set' else not bool(facts & wanted))
            if not match:
                excluded = True
            supports.extend(evidence)
        if excluded:
            continue
        if unknown:
            uncertain.append(card)
            continue
        matched.append(card)
        citations.update({p.id: p.citation() for p in supports})
    return matched, uncertain, list(citations.values())
