"""Bounded Chat Completions tool loop, using the Python standard library."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from .domain import AppError, local_draft, local_match, local_weekly, validate_card, validate_matches, validate_weekly

PROTOCOLS = json.loads(Path(__file__).with_name('protocols.json').read_text(encoding='utf-8'))


def config_from_env():
    return dict(baseUrl=os.getenv('LLM_BASE_URL', ''), apiKey=os.getenv('LLM_API_KEY', ''), model=os.getenv('LLM_MODEL', ''))


def is_configured(config):
    return all(config.get(k) for k in ('baseUrl', 'apiKey', 'model'))


def request_completion(endpoint, payload, api_key):
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise AppError('模型返回内容过大', 502)
    except HTTPError as error:
        raise AppError(f'模型接口返回 HTTP {error.code}；请检查地址、模型与密钥。原始记录仍已保存。', 502) from None
    except AppError:
        raise
    except Exception:
        raise AppError('模型连接失败或超时；你的原始记录仍已保存，请稍后重试', 502) from None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise AppError('模型返回了无效 JSON', 502) from None


class CareerAgent:
    def __init__(self, config, completion=request_completion):
        self.config = config
        self.completion = completion

    def run(self, kind, data, state, mode):
        if mode != 'agent':
            if kind == 'weekly':
                return dict(sections=local_weekly(data['notes']), mode='local',
                            trace=[dict(tool='group_daily_notes', summary=f'归集 {len(data["notes"])} 条日报，合并完全相同的条目并保留来源')])
            if kind == 'draft':
                return local_draft(data, state['cards'])
            return dict(matches=local_match(data['jd'], state['cards']), mode='local',
                        trace=[dict(tool='keyword_match', summary='本地关键词检索；结果均需人工判断')])
        if not is_configured(self.config):
            raise AppError('请先在 .env 配置模型地址、密钥和模型名，或切换本地模式')
        endpoint = self.config['baseUrl'].rstrip('/') + '/chat/completions'
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise AppError('模型地址必须使用 HTTPS；本机模型可使用 HTTP')
        eligible = [c for c in state['cards'] if kind == 'draft' or c['status'] == 'confirmed']
        finish = dict(weekly='finish_weekly', draft='finish_card', match='finish_match')[kind]
        if kind == 'weekly':
            task = dict(task='生成工作周报。合并重复事项，保留实际完成程度和个人贡献。不得把计划写成成果。阻碍只在有后续证据时标为已解决；计划若有后续完成证据则不再列为待办。不得编造下周计划。每一条归纳附原文证据。', start=data['start'], end=data['end'])
        elif kind == 'draft':
            task = dict(task='整理这条记录', sourceId=data['id'])
        else:
            task = dict(task='分析岗位要求与个人经历的证据对应关系', jd=data['jd'])
        messages = [dict(role='system', content=PROTOCOLS[kind]['system']),
                    dict(role='user', content=json.dumps(task, ensure_ascii=False))]
        trace = []
        investigated = False
        for _ in range(7):
            payload = dict(model=self.config['model'], messages=messages, tools=PROTOCOLS[kind]['tools'], tool_choice='required', max_tokens=4000)
            response = self.completion(endpoint, payload, self.config['apiKey'])
            try:
                message = response['choices'][0]['message']
                calls = message['tool_calls']
                if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
                    raise ValueError()
            except (KeyError, IndexError, TypeError, ValueError):
                raise AppError('模型未返回有效工具调用，请确认支持 function calling', 502) from None
            messages.append(dict(role='assistant', content=message.get('content') or None, tool_calls=calls))
            for call in calls:
                name = call.get('function', {}).get('name', '')
                try:
                    args = json.loads(call['function']['arguments'])
                    if not isinstance(args, dict):
                        raise ValueError()
                    if name == 'read_week_notes' and kind == 'weekly':
                        output = data['notes']
                        investigated = True
                        trace.append(dict(tool=name, summary=f'读取 {len(output)} 条所选时间段的日报'))
                    elif name == 'read_source' and kind != 'weekly':
                        output = next((s for s in state['sources'] if s['id'] == args.get('sourceId')), None)
                        if output is None:
                            raise AppError('记录不存在')
                        investigated = investigated or (kind == 'draft' and output['id'] == data['id'])
                        trace.append(dict(tool=name, summary=f'读取「{output["title"]}」'))
                    elif name == 'search_cards' and kind != 'weekly':
                        query = args.get('query', '')
                        query = query.lower() if isinstance(query, str) else ''
                        output = [c for c in eligible if query in f'{c["title"]} {c["action"]} {" ".join(c["tags"])}'.lower()][:30]
                        investigated = investigated or kind == 'match'
                        trace.append(dict(tool=name, summary=f'查询「{query or "全部"}」：{len(output)} 条经历'))
                    elif name == finish:
                        if not investigated:
                            raise AppError('请先读取本次记录或检索经历库')
                        if kind == 'weekly':
                            result = dict(sections=validate_weekly(args, data['notes']))
                        elif kind == 'draft':
                            related = args.get('relatedIds', [])
                            related = related if isinstance(related, list) else []
                            result = dict(card=validate_card(args, state['sources'], data['id']), relatedIds=[cid for cid in related if any(c['id'] == cid for c in eligible)][:5])
                        else:
                            result = dict(matches=validate_matches(args.get('matches'), data['jd'], eligible))
                        return dict(result, mode='agent', trace=trace + [dict(tool=finish, summary='证据引用校验通过，请本人核对')])
                    else:
                        raise AppError('工具不存在')
                except (AppError, ValueError, TypeError, KeyError, AttributeError) as error:
                    output = dict(error=str(error) if isinstance(error, AppError) else '工具参数无效，请修正')
                    trace.append(dict(tool=name, summary='校验未通过，请求模型修正'))
                messages.append(dict(role='tool', tool_call_id=call.get('id', ''), content=json.dumps(output, ensure_ascii=False)))
        raise AppError('达到调查轮次上限，未得到有效结果。原始记录仍保留，可手动整理。', 502)
