"""Recoverable deletion with source dependency tracking."""
from .domain import AppError, new_id, now

COLLECTIONS = ('sources', 'cards', 'runs', 'weeklyReports')


def visible_state(state):
    result = dict(state, trash=[])
    for collection in COLLECTIONS:
        result[collection] = [item for item in state.get(collection, []) if not item.get('deletedAt')]
        for item in state.get(collection, []):
            if item.get('deletedAt'):
                title = item.get('title') or f'{item["start"]} — {item["end"]} · 周报'
                result['trash'].append(dict(id=item['id'], collection=collection, title=title,
                                            kind=item.get('kind'), deletedAt=item['deletedAt']))
    result['trash'].sort(key=lambda item: item['deletedAt'], reverse=True)
    return result


def find(state, collection, item_id):
    if collection not in COLLECTIONS:
        raise AppError('不支持删除这类内容')
    item = next((item for item in state[collection] if item['id'] == item_id), None)
    if item is None:
        raise AppError('记录不存在', 404)
    return item


def move_to_trash(state, collection, item_id):
    item = find(state, collection, item_id)
    if item.get('deletedAt'):
        return dict(moved=0)
    targets = [item]
    if collection == 'sources':
        targets.extend(c for c in state['cards'] if not c.get('deletedAt') and any(e['sourceId'] == item_id for e in c['evidence']))
    batch, deleted_at = new_id(), now()
    for target in targets:
        target.update(deletedAt=deleted_at, deletionBatch=batch)
        if 'revision' in target:
            target['revision'] += 1
    return dict(moved=len(targets))


def restore_from_trash(state, collection, item_id):
    item = find(state, collection, item_id)
    if not item.get('deletedAt'):
        return dict(restored=0, skipped=0)
    active_ids = {s['id'] for s in state['sources'] if not s.get('deletedAt')}

    def can_restore(card):
        return all(e['sourceId'] in active_ids for e in card['evidence'])

    def restore(target):
        target.pop('deletedAt', None)
        target.pop('deletionBatch', None)
        if 'revision' in target:
            target['revision'] += 1

    if collection == 'cards' and not can_restore(item):
        raise AppError('请先从回收站恢复这条经历引用的原始记录', 409)
    if collection == 'sources' and any(not s.get('deletedAt') and s['hash'] == item['hash'] for s in state['sources']):
        raise AppError('相同原文已重新导入，无法重复恢复；原记录仍保留在回收站', 409)
    batch = item.get('deletionBatch')
    restore(item)
    restored, skipped = 1, 0
    if collection == 'sources':
        active_ids.add(item_id)
        for card in state['cards']:
            if card.get('deletedAt') and card.get('deletionBatch') == batch:
                if can_restore(card):
                    restore(card)
                    restored += 1
                else:
                    skipped += 1
    return dict(restored=restored, skipped=skipped)


def retain_history(items, limit):
    result = []
    active_count = 0
    for item in items:
        if item.get('deletedAt'):
            result.append(item)
        elif active_count < limit:
            result.append(item)
            active_count += 1
    return result
