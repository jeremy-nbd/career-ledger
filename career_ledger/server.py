"""Loopback HTTP API and application lifecycle."""
import copy
import json
import os
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from . import domain as d
from .agent import CareerAgent, config_from_env, is_configured
from .store import Store
from .trash import move_to_trash, restore_from_trash, visible_state, retain_history

ROOT = Path(__file__).resolve().parent.parent
ASSETS = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/journal.js': ('journal.js', 'text/javascript'), '/deletion.js': ('deletion.js', 'text/javascript'),
          '/style.css': ('style.css', 'text/css'), '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
DEMO = json.loads((ROOT / 'examples/demo.json').read_text(encoding='utf-8'))


def load_env(path=ROOT / '.env'):
    """Load literal dotenv assignments without executing or expanding their contents."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            continue
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0:
                raise ValueError('Invalid quoted environment value')
            value = value[1:end]
        else:
            value = value.split('#', 1)[0].rstrip()
        os.environ.setdefault(key, value)


class Application:
    def __init__(self, data_dir, config=None, agent=None):
        self.store = Store(data_dir)
        self.config = config if config is not None else config_from_env()
        self.agent = agent or CareerAgent(self.config)
        self.active = set()
        self.active_lock = threading.Lock()

    @contextmanager
    def task(self, key):
        with self.active_lock:
            if key in self.active:
                raise d.AppError('这项任务正在生成中，请稍候', 409)
            self.active.add(key)
        try:
            yield
        finally:
            with self.active_lock:
                self.active.remove(key)

    def dispatch(self, method, path, data):
        if method == 'GET' and path == '/api/state':
            return dict(visible_state(self.store.read()), configured=is_configured(self.config),
                        model=self.config.get('model') or None, demoJobs=DEMO['demoJobs'])
        if method == 'GET' and path == '/api/backup':
            return self.store.read()
        if method == 'POST' and path in ('/api/trash', '/api/restore'):
            operation = move_to_trash if path == '/api/trash' else restore_from_trash
            return self.store.update(lambda state: operation(state, data.get('collection'), data.get('id')))
        if method == 'POST' and path in ('/api/sources', '/api/daily'):
            source = d.daily_source(data) if path == '/api/daily' else d.source_from(data)

            def save_source(state):
                active = visible_state(state)['sources']
                old = next((s for s in active if s['hash'] == source['hash']), None)
                if old:
                    return dict(source=old, duplicate=True)
                if len(active) >= 500:
                    raise d.AppError('第一版最多保存 500 条使用中的记录，请先删除不需要的记录')
                state['sources'].insert(0, source)
                return dict(source=source, duplicate=False)
            return self.store.update(save_source)
        if method == 'POST' and path == '/api/weekly':
            return self.weekly(data)
        if method == 'POST' and path == '/api/draft':
            return self.draft(data)
        if method == 'PATCH' and path.startswith('/api/cards/'):
            return self.edit_card(path.rsplit('/', 1)[-1], data)
        if method == 'POST' and path == '/api/match':
            return self.match(data)
        if method == 'POST' and path == '/api/demo':
            def load_demo(state):
                if state['sources'] or state['cards'] or state['runs']:
                    raise d.AppError('演示数据只可载入空账本，以免混入你的真实经历', 409)
                state.update(copy.deepcopy({k: DEMO[k] for k in ('sources', 'cards')}))
                return dict(ok=True)
            return self.store.update(load_demo)
        raise d.AppError('未找到这个页面或操作', 404)

    def weekly(self, data):
        state = visible_state(self.store.read())
        week = d.weekly_sources(state['sources'], data.get('start'), data.get('end'))
        if not week['notes']:
            raise d.AppError('所选时间段没有日报，请调整日期或先补记工作')
        if len(week['notes']) > 30 or sum(len(s['content']) for s in week['notes']) > 60000:
            raise d.AppError('单次周报最多处理 30 条日报、60000 字符，请缩小时间段')
        with self.task(f'weekly:{week["start"]}:{week["end"]}'):
            result = self.agent.run('weekly', week, state, data.get('mode'))
            report = dict(id=d.new_id(), start=week['start'], end=week['end'], createdAt=d.now(),
                          mode=result['mode'], sections=result['sections'], sources=week['notes'], trace=result['trace'])
            report['markdown'] = d.weekly_markdown(report)

            def save(current):
                active_ids = {s['id'] for s in visible_state(current)['sources']}
                if any(s['id'] not in active_ids for s in week['notes']):
                    raise d.AppError('生成期间有日报被删除，请重新生成周报', 409)
                current['weeklyReports'] = retain_history([report] + current['weeklyReports'], 100)
            self.store.update(save)
            return report

    def draft(self, data):
        source_id = data.get('sourceId')
        with self.task(f'draft:{source_id}'):
            state = visible_state(self.store.read())
            source = next((s for s in state['sources'] if s['id'] == source_id), None)
            if source is None:
                raise d.AppError('记录不存在', 404)
            result = self.agent.run('draft', source, state, data.get('mode'))
            card = dict(d.validate_card(result['card'], state['sources'], source_id),
                        id=d.new_id(), status='draft', revision=1, createdAt=d.now(), updatedAt=d.now(),
                        history=[], relatedIds=result['relatedIds'], trace=result['trace'], mode=result['mode'])

            def save(current):
                d.validate_card(card, visible_state(current)['sources'], source_id)
                current['cards'].insert(0, card)
            self.store.update(save)
            return card

    def edit_card(self, card_id, data):
        def save(state):
            active = visible_state(state)
            card = next((c for c in active['cards'] if c['id'] == card_id), None)
            if card is None:
                raise d.AppError('经历不存在或已删除', 404)
            if data.get('revision') != card['revision']:
                raise d.AppError('经历已被其他页面更新，请刷新后再保存', 409)
            if data.get('status') not in ('draft', 'confirmed'):
                raise d.AppError('无效的确认状态')
            checked = d.validate_card(dict(data, evidence=card['evidence']), active['sources'])
            if data['status'] == 'confirmed' and checked['contribution'] == '待确认':
                raise d.AppError('确认前请明确个人贡献')
            snapshot = copy.deepcopy({k: v for k, v in card.items() if k != 'history'})
            history = (card['history'] + [snapshot])[-30:]
            card.update(checked, status=data['status'], updatedAt=d.now(), revision=card['revision'] + 1, history=history)
            return card
        return self.store.update(save)

    def match(self, data):
        jd = d.text(data.get('jd'), '岗位描述', 20000)
        title = d.text(data.get('title'), '岗位名称', 160)
        with self.task('match'):
            state = visible_state(self.store.read())
            result = self.agent.run('match', dict(jd=jd), state, data.get('mode'))
            run = dict(id=d.new_id(), title=title, jd=jd, matches=result['matches'],
                       trace=result['trace'], mode=result['mode'], createdAt=d.now())
            ids = dict.fromkeys(cid for row in run['matches'] for cid in row['cardIds'])
            cards = [c for cid in ids for c in state['cards'] if c['id'] == cid]
            sources = [s for s in state['sources'] if any(e['sourceId'] == s['id'] for c in cards for e in c['evidence'])]
            run['snapshot'] = dict(cards=cards, sources=sources)
            run['markdown'] = d.resume_for(run, run['snapshot'])

            def save(current):
                active = visible_state(current)['cards']
                if any(not any(c['id'] == card['id'] and c['revision'] == card['revision'] for c in active) for card in cards):
                    raise d.AppError('生成期间引用的经历被修改或删除，请重新分析', 409)
                current['runs'] = retain_history([run] + current['runs'], 50)
            self.store.update(save)
            return run


class Handler(BaseHTTPRequestHandler):
    server_version = 'CareerLedger'

    def log_message(self, *_):
        pass  # Request paths and bodies may contain private material.

    def handle_request(self):
        try:
            host = self.headers.get('Host', '')
            if not re.fullmatch(r'(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?', host):
                raise d.AppError('仅支持本机访问', 403)
            if self.headers.get('Origin') not in (None, f'http://{host}'):
                raise d.AppError('不允许跨站访问本地资料', 403)
            path = urlsplit(self.path).path
            if self.command == 'GET' and path in ASSETS:
                name, mime = ASSETS[path]
                return self.send(200, (ROOT / 'public' / name).read_bytes(), mime + '; charset=utf-8')
            data = self.read_body() if self.command in ('POST', 'PATCH') else {}
            result = self.server.application.dispatch(self.command, path, data)
            self.send(200, result)
        except d.AppError as error:
            self.send(error.status, dict(error=str(error)))
        except Exception:
            self.send(500, dict(error='本地服务处理失败，请检查磁盘与数据文件；原始文件不会被自动重置'))

    def read_body(self):
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            raise d.AppError('只接受 JSON 请求', 415)
        if self.headers.get('Transfer-Encoding'):
            raise d.AppError('请使用带 Content-Length 的 JSON 请求', 400)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise d.AppError('无效的请求长度') from None
        if length < 0 or length > 1024 * 1024:
            raise d.AppError('请求过大（最多 1 MB）', 413)
        self.connection.settimeout(15)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, TimeoutError):
            raise d.AppError('JSON 格式无效') from None

    def send(self, status, data, mime='application/json; charset=utf-8'):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    do_GET = handle_request
    do_POST = handle_request
    do_PATCH = handle_request


class LedgerServer(ThreadingHTTPServer):
    daemon_threads = False

    def server_close(self):
        super().server_close()
        self.application.store.close()


def create_server(port=4317, data_dir=None, config=None, agent=None):
    application = Application(data_dir or os.getenv('CAREER_DATA_DIR') or ROOT / 'data', config, agent)
    application.store.acquire_process_lock()
    try:
        server = LedgerServer(('127.0.0.1', port), Handler)
    except Exception:
        application.store.close()
        raise
    server.application = application
    return server


def main():
    load_env()
    server = create_server(port=int(os.getenv('PORT', '4317')))
    print(f'Career Ledger · http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
