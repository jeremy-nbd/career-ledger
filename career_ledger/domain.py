"""Validation and deterministic transformations for version-1 ledgers."""
import hashlib
import re
import uuid
from datetime import date, datetime, timezone


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def new_id():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def empty_state():
    return dict(version=1, sources=[], cards=[], runs=[], weeklyReports=[])


def text(value, name, maximum=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AppError(f'{name}需为 1–{maximum} 字符')
    return value.strip()


def source_from(data):
    content = text(data.get('content'), '原始记录', 40000)
    return dict(id=new_id(), title=text(data.get('title'), '标题', 160), content=content,
                hash=hashlib.sha256(content.encode()).hexdigest(), createdAt=now())


def date_only(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError()
        date.fromisoformat(value)
    except ValueError:
        raise AppError('请选择有效日期') from None
    return value


def daily_source(data):
    day = date_only(data.get('date'))
    completed = text(data.get('completed'), '今日完成', 12000)
    blockers = data.get('blockers', '')
    next_step = data.get('next', '')
    blockers = blockers.strip() if isinstance(blockers, str) else ''
    next_step = next_step.strip() if isinstance(next_step, str) else ''
    if len(blockers) > 5000 or len(next_step) > 5000:
        raise AppError('阻碍与下一步各最多 5000 字符')
    content = f'日期：{day}\n\n完成事项\n{completed}\n\n问题与阻碍\n{blockers or "未记录"}\n\n下一步\n{next_step or "未记录"}'
    source = source_from(dict(title=f'{day} · 日报', content=content))
    source.update(hash=hashlib.sha256(('daily\n' + content).encode()).hexdigest(), kind='daily',
                  date=day, daily=dict(completed=completed, blockers=blockers, next=next_step))
    return source


def weekly_sources(sources, start, end):
    date_only(start)
    date_only(end)
    if start > end:
        raise AppError('开始日期不能晚于结束日期')
    notes = [s for s in sources if not s.get('deletedAt') and s.get('kind') == 'daily'
             and start <= s['date'] <= end]
    notes.sort(key=lambda s: (s['date'], s['createdAt']))
    return dict(start=start, end=end, notes=notes)


def local_weekly(notes):
    result = {}
    for field in ('completed', 'blockers', 'next'):
        items = {}
        for source in notes:
            for line in source['daily'][field].split('\n'):
                line = line.strip()
                if line:
                    item = items.setdefault(line, dict(text=line, evidence=[]))
                    item['evidence'].append(dict(sourceId=source['id'], quote=line))
        result[field] = list(items.values())
    return result


def validate_evidence(evidence, sources, required_id=None):
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
        raise AppError('至少保留一条原文证据，最多 12 条')
    checked = []
    for entry in evidence:
        if not isinstance(entry, dict):
            raise AppError('无效的引用')
        source = next((s for s in sources if s['id'] == entry.get('sourceId')), None)
        quote = text(entry.get('quote'), '引用原文', 3000)
        if not source or quote not in source['content']:
            raise AppError('引用必须逐字出现在原始记录中')
        checked.append(dict(sourceId=source['id'], quote=quote))
    if required_id and not any(e['sourceId'] == required_id for e in checked):
        raise AppError('缺少本次记录的引用')
    return checked


TERMS = ['Agent', 'AI', 'Python', 'TypeScript', 'React', 'RAG', 'SQL', '测试', '产品', '需求', '数据', '权限', '接口', '状态', '模型', '运营', '报告', '评测', '用户', '设计', '调研', '分析', '沟通', '项目', '流程', '鉴权', '检索', '前端', '后端']
ROLES = ['独立完成', '主导', '负责', '参与', '协助', '待确认']


def tags_for(value):
    return [t for t in TERMS if t.lower() in value.lower()]


def validate_card(data, sources, required_id=None):
    if data.get('contribution') not in ROLES:
        raise AppError('无效的个人贡献类型')
    questions = data.get('questions') or []
    if not isinstance(questions, list) or len(questions) > 8:
        raise AppError('待补充问题最多 8 项')
    title = text(data.get('title'), '经历标题', 160)
    action = text(data.get('action'), '个人动作', 3000)
    outcome = data.get('outcome', '')
    outcome = outcome.strip()[:2000] if isinstance(outcome, str) else ''
    return dict(title=title, action=action, outcome=outcome, contribution=data['contribution'],
                tags=tags_for(f'{title} {action} {outcome}'),
                evidence=validate_evidence(data.get('evidence'), sources, required_id),
                questions=[text(q, '待补充问题', 300) for q in questions])


def local_draft(source, cards):
    quote = source['content'][:2400]
    tags = tags_for(quote)
    related = [c for c in cards if any(t in tags for t in c['tags'])][:3]
    return dict(card=dict(title=source['title'], action=quote, outcome='', contribution='待确认',
                          evidence=[dict(sourceId=source['id'], quote=quote)],
                          questions=['这段工作中，你本人负责什么？', '已经完成或上线了吗？有什么可核实的结果？']),
                relatedIds=[c['id'] for c in related], mode='local',
                trace=[dict(tool='read_source', summary=f'读取「{source["title"]}」'),
                       dict(tool='search_cards', summary=f'按显式标签找到 {len(related)} 条可能相关经历；不自动合并')])


def local_match(jd, cards):
    requirements = [re.sub(r'^[-*\d.、\s]+', '', s).strip() for s in re.split(r'\n|[；;]', jd)]
    rows = []
    for requirement in [r for r in requirements if r][:12]:
        tags = tags_for(requirement)
        candidates = [(c, len(set(c['tags']) & set(tags))) for c in cards if c['status'] == 'confirmed']
        candidates = sorted((pair for pair in candidates if pair[1]), key=lambda pair: -pair[1])[:2]
        rows.append(dict(requirement=requirement, level='adjacent' if candidates else 'gap',
                         cardIds=[c['id'] for c, _ in candidates],
                         explanation='仅发现相同关键词，尚未验证职责、年限或熟练度；请人工判断。' if candidates else '未找到关键词对应的已确认经历，不代表你一定不具备该能力。'))
    return rows


def validate_matches(rows, jd, cards):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 15:
        raise AppError('岗位分析应包含 1–15 项要求')
    result = []
    for row in rows:
        requirement = text(row.get('requirement'), '岗位要求', 1500)
        if requirement not in jd:
            raise AppError('岗位要求必须引用 JD 原句')
        if row.get('level') not in ('direct', 'adjacent', 'gap'):
            raise AppError('无效的匹配等级')
        ids = row.get('cardIds')
        if not isinstance(ids, list) or len(ids) > 5 or any(not isinstance(i, str) for i in ids):
            raise AppError('无效的经历引用')
        ids = list(dict.fromkeys(ids))
        if any(not any(c['id'] == cid and c['status'] == 'confirmed' for c in cards) for cid in ids):
            raise AppError('只能使用已确认经历')
        if row['level'] != 'gap' and not ids:
            raise AppError('匹配结论缺少经历证据')
        if row['level'] == 'gap' and ids:
            raise AppError('证据缺口不应附带匹配经历')
        result.append(dict(requirement=requirement, level=row['level'], cardIds=ids,
                           explanation=text(row.get('explanation'), '匹配说明', 1500)))
    return result


def validate_weekly(data, notes):
    result = {}
    for field in ('completed', 'blockers', 'next'):
        rows = data.get(field)
        if not isinstance(rows, list) or len(rows) > 40:
            raise AppError('每个周报板块最多 40 条')
        result[field] = [dict(text=text(item.get('text'), '周报条目', 2000),
                              evidence=validate_evidence(item.get('evidence'), notes)) for item in rows]
    if not result['completed']:
        raise AppError('周报至少需要一条完成事项')
    return result


def weekly_markdown(report):
    mode = '模型归纳，请对照来源复核' if report['mode'] == 'agent' else '本地归集，合并完全相同条目'
    lines = [f'# 工作周报 · {report["start"]} — {report["end"]}', '', f'基于 {len(report["sources"])} 条日报记录 · {mode}', '']
    references = {s['id']: i for i, s in enumerate(report['sources'], 1)}
    for key, label in [('completed', '期间完成'), ('blockers', '问题与阻碍（记录时状态）'), ('next', '后续计划（请核对是否仍待执行）')]:
        lines.extend([f'## {label}', ''])
        if not report['sections'][key]:
            lines.extend(['所选时间段的日报未记录此项。', ''])
        for item in report['sections'][key]:
            refs = dict.fromkeys(f'[D{references[e["sourceId"]]}]' for e in item['evidence'])
            lines.append(f'- {item["text"]} {" ".join(refs)}')
        lines.append('')
    lines.extend(['---', '', '## 日报原文', ''])
    for source in report['sources']:
        lines.extend([f'### D{references[source["id"]]} · {source["title"]}', ''])
        lines.extend('> ' + line for line in source['content'].split('\n'))
        lines.append('')
    return '\n'.join(lines)


def resume_for(run, state):
    ids = dict.fromkeys(cid for row in run['matches'] for cid in row['cardIds'])
    cards = [c for cid in ids for c in state['cards'] if c['id'] == cid and c['status'] == 'confirmed']
    lines = [f'# {run["title"]} · 经历精选', '', '> 根据本人确认的经历原文生成。确认代表个人核对，不代表第三方认证。', '']
    for i, card in enumerate(cards, 1):
        lines.extend([f'## {card["title"]}', '', f'个人贡献：{card["contribution"]}', '', f'{card["action"]} [E{i}]', ''])
        if card['outcome']:
            lines.extend([f'结果：{card["outcome"]} [E{i}]', ''])
    if not cards:
        lines.extend(['尚无可引用的已确认经历。请先整理并核对经历卡片。', ''])
    lines.extend(['---', '', '## 证据索引（对外投递前可单独保留）', ''])
    for i, card in enumerate(cards, 1):
        lines.extend([f'### E{i} · {card["title"]}', ''])
        for entry in card['evidence']:
            source = next(s for s in state['sources'] if s['id'] == entry['sourceId'])
            lines.extend([f'来源：{source["title"]} · {source["id"]}', ''])
            lines.extend('> ' + line for line in entry['quote'].split('\n'))
            lines.append('')
    return '\n'.join(lines)
