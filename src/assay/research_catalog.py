"""One discovery surface for research records and optional compute registries."""
import math


def registries(service, compute_factory, source):
    if source not in {'research', 'compute', 'all'}:
        raise ValueError('source must be research, compute or all')
    rows = []
    if source != 'compute':
        rows.append(('research', service().research.factor_registry))
    if source != 'research':
        try:
            rows.append(('compute', compute_factory().reg))
        except RuntimeError:
            if source == 'compute':
                raise
    return rows


def query(service, compute_factory, source='all', **filters):
    limit, offset = filters.pop('limit', 20), filters.pop('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('require 1 <= limit <= 500 and offset >= 0')
    stores = registries(service, compute_factory, source)
    merged = {}
    for name, reg in stores:
        for row in reg.query(**filters, limit=limit + offset, offset=0):
            key = (row['id'], row['spec'], row['version'])
            if key in merged:
                merged[key]['sources'].append(name)
            else:
                merged[key] = dict(row, sources=[name])
    key = filters.get('sort_by', 'rank_icir')
    descending = filters.get('descending', True)
    def order(row):
        value = row.get(key)
        valid = type(value) in (int, float) and math.isfinite(value)
        if valid and key in {'rank_ic', 'rank_icir', 'ic', 'icir'}:
            value = abs(value)
        return (not valid, (-value if descending else value) if valid else 0,
                row['id'], row['spec'], row['version'])
    rows = sorted(merged.values(), key=order)[offset:offset + limit]
    return dict(count=len(rows), factors=rows, available_sources=[n for n, _ in stores])


def factor(service, compute_factory, factor_id, source='all'):
    records = {name: value for name, reg in registries(service, compute_factory, source)
               if (value := reg.factor(factor_id)) is not None}
    if not records:
        return {'error': {'code': 'NOT_FOUND', 'message': f'no factor {factor_id}'}}
    result = dict(next(iter(records.values())), registries=records)
    if 'research' in records:
        result['research_history'] = service().research.factor_history(factor_id)
    return result
