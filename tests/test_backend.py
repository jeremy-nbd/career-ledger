"""Migration parity and HTTP regression tests using fictional data only."""
import copy
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from career_ledger import domain as d
from career_ledger.agent import CareerAgent, request_completion
from career_ledger.server import create_server, load_env
from career_ledger.store import Store
from career_ledger.trash import move_to_trash, restore_from_trash, visible_state

FIXTURE = json.loads(Path(__file__).with_name('legacy.json').read_text())
CONFIG = dict(baseUrl='https://model.example/v1', apiKey='fictional', model='test')


def reply(name, args):
    return dict(choices=[dict(message=dict(tool_calls=[dict(id='call', type='function',
                function=dict(name=name, arguments=json.dumps(args)))]))])


class DomainTests(unittest.TestCase):
    def test_legacy_outputs_are_identical(self):
        state = FIXTURE['state']
        self.assertEqual(d.local_draft(state['sources'][0], state['cards']), FIXTURE['draft'])
        self.assertEqual(d.local_weekly(FIXTURE['notes']), FIXTURE['week']['sections'])
        self.assertEqual(d.weekly_markdown(FIXTURE['week']), FIXTURE['weeklyMarkdown'])
        self.assertEqual(d.resume_for(FIXTURE['run'], state), FIXTURE['resume'])
        self.assertEqual(d.local_match('TypeScript 测试\n负责模型评测', state['cards']), FIXTURE['run']['matches'])

    def test_daily_hash_compatible(self):
        source = d.daily_source(dict(date='2026-11-02', completed='完成测试', next='准备发布'))
        self.assertEqual(source['hash'], FIXTURE['notes'][0]['hash'])
        self.assertEqual(d.source_from(dict(title='标题', content=' 内容 '))['hash'], hashlib.sha256('内容'.encode()).hexdigest())

    def test_dates_inclusive_and_invalid(self):
        notes = FIXTURE['notes']
        self.assertEqual(len(d.weekly_sources(notes, '2026-11-02', '2026-11-02')['notes']), 1)
        self.assertEqual(len(d.weekly_sources(notes, '2026-11-02', '2026-11-03')['notes']), 2)
        for start, end in [('2026-02-30', '2026-03-01'), ('2026-11-03', '2026-11-02'), (None, None)]:
            with self.assertRaises(d.AppError):
                d.weekly_sources(notes, start, end)

    def test_evidence_and_confirmation(self):
        state = FIXTURE['state']
        card = copy.deepcopy(state['cards'][0])
        card['evidence'][0]['quote'] = '伪造引用'
        with self.assertRaises(d.AppError):
            d.validate_card(card, state['sources'])
        with self.assertRaises(d.AppError):
            d.validate_matches([dict(requirement='测试', level='direct', cardIds=[state['cards'][3]['id']], explanation='测试')], '测试', state['cards'])

    def test_cascade_restore_and_duplicate_conflict(self):
        state = copy.deepcopy(FIXTURE['state'])
        source = state['sources'][0]
        self.assertEqual(move_to_trash(state, 'sources', source['id'])['moved'], 2)
        with self.assertRaises(d.AppError):
            restore_from_trash(state, 'cards', state['cards'][0]['id'])
        self.assertEqual(restore_from_trash(state, 'sources', source['id'])['restored'], 2)
        self.assertEqual(state['cards'][0]['revision'], 3)
        move_to_trash(state, 'sources', source['id'])
        state['sources'].append(d.source_from(source))
        with self.assertRaises(d.AppError):
            restore_from_trash(state, 'sources', source['id'])

    def test_store_legacy_corruption_and_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            legacy = copy.deepcopy(FIXTURE['state'])
            del legacy['weeklyReports']
            store.file.write_text(json.dumps(legacy))
            self.assertEqual(store.read()['weeklyReports'], [])
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: store.update(lambda s: s['runs'].append(dict(id=str(i)))), range(30)))
            self.assertEqual(len(store.read()['runs']), 30)
            store.file.write_text('broken')
            with self.assertRaises(ValueError):
                store.update(lambda s: s.clear())
            self.assertEqual(store.file.read_text(), 'broken')

    def test_dotenv_literals_and_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PORT': '9999'}, clear=True):
            path = Path(directory) / '.env'
            path.write_text('PORT=4317\nLLM_API_KEY="fake#literal"\nLLM_MODEL=test # comment\n')
            load_env(path)
            self.assertEqual(os.environ['PORT'], '9999')
            self.assertEqual(os.environ['LLM_API_KEY'], 'fake#literal')
            self.assertEqual(os.environ['LLM_MODEL'], 'test')


class AgentTests(unittest.TestCase):
    def test_tool_loop_repairs_evidence(self):
        state = FIXTURE['state']
        source = state['sources'][0]
        card = FIXTURE['draft']['card']
        invalid = dict(card, evidence=[dict(sourceId=source['id'], quote='假的')])
        responses = iter([reply('read_source', dict(sourceId=source['id'])), reply('finish_card', invalid), reply('finish_card', card)])
        payloads = []
        def completion(endpoint, payload, key):
            self.assertEqual(endpoint, 'https://model.example/v1/chat/completions')
            payloads.append(copy.deepcopy(payload))
            return next(responses)
        result = CareerAgent(CONFIG, completion).run('draft', source, state, 'agent')
        self.assertEqual(result['card']['contribution'], '待确认')
        self.assertIn('校验未通过', result['trace'][1]['summary'])
        self.assertIn('error', payloads[-1]['messages'][-1]['content'])

    def test_budget_requires_investigation(self):
        calls = []
        def completion(*args):
            calls.append(1)
            return reply('finish_card', FIXTURE['draft']['card'])
        with self.assertRaisesRegex(d.AppError, '轮次上限'):
            CareerAgent(CONFIG, completion).run('draft', FIXTURE['state']['sources'][0], FIXTURE['state'], 'agent')
        self.assertEqual(len(calls), 7)

    def test_weekly_and_match_tools(self):
        week = dict(start='2026-11-02', end='2026-11-03', notes=FIXTURE['notes'])
        responses = iter([reply('read_week_notes', {}), reply('finish_weekly', FIXTURE['week']['sections'])])
        result = CareerAgent(CONFIG, lambda *a: next(responses)).run('weekly', week, FIXTURE['state'], 'agent')
        self.assertEqual(result['sections'], FIXTURE['week']['sections'])
        responses = iter([reply('search_cards', dict(query='')), reply('finish_match', dict(matches=FIXTURE['run']['matches']))])
        result = CareerAgent(CONFIG, lambda *a: next(responses)).run('match', dict(jd='TypeScript 测试\n负责模型评测'), FIXTURE['state'], 'agent')
        self.assertEqual(result['matches'], FIXTURE['run']['matches'])

    def test_transport_encodes_tools_and_parses_response(self):
        import io
        expected = reply('read_source', dict(sourceId='example'))
        with patch('career_ledger.agent.urlopen', return_value=io.BytesIO(json.dumps(expected).encode())) as transport:
            self.assertEqual(request_completion('https://example.com/v1/chat/completions', {'messages': [{'content': '中文'}]}, 'fake'), expected)
            request = transport.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer fake')
            self.assertEqual(json.loads(request.data)['messages'][0]['content'], '中文')
            self.assertEqual(transport.call_args.kwargs['timeout'], 45)
        with patch('career_ledger.agent.urlopen', return_value=io.BytesIO(b'not json')):
            with self.assertRaisesRegex(d.AppError, 'JSON'):
                request_completion('https://example.com', {}, 'fake')

    def test_transport_error_redacts_body(self):
        from urllib.error import HTTPError
        with patch('career_ledger.agent.urlopen', side_effect=HTTPError('https://example.com', 401, 'private key', {}, None)):
            with self.assertRaises(d.AppError) as caught:
                request_completion('https://example.com', {}, 'fake')
            self.assertIn('HTTP 401', str(caught.exception))
            self.assertNotIn('private', str(caught.exception))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(0, self.temp.name, config={})
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()
        self.temp.cleanup()

    def api(self, path, data=None, method=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        raw = json.dumps(data).encode() if data is not None else None
        connection.request(method or ('POST' if data is not None else 'GET'), path, body=raw,
                           headers=headers or {'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, json.loads(body) if response.getheader('Content-Type', '').startswith('application/json') else body

    def daily(self, day='2026-11-02'):
        status, result = self.api('/api/daily', dict(date=day, completed='完成 TypeScript 测试', next='准备发布'))
        self.assertEqual(status, 200)
        return result['source']

    def test_normal_entry_points_and_snapshots(self):
        source = self.daily()
        self.assertEqual(self.daily()['id'], source['id'])
        status, card = self.api('/api/draft', dict(sourceId=source['id'], mode='local'))
        self.assertEqual(status, 200)
        status, _ = self.api('/api/cards/' + card['id'], dict(card, status='confirmed'), 'PATCH')
        self.assertEqual(status, 400)
        status, confirmed = self.api('/api/cards/' + card['id'], dict(card, status='confirmed', contribution='参与'), 'PATCH')
        self.assertEqual(status, 200)
        self.assertEqual(self.api('/api/cards/' + card['id'], card, 'PATCH')[0], 409)
        status, run = self.api('/api/match', dict(title='工程师', jd='TypeScript 测试', mode='local'))
        self.assertEqual(status, 200)
        self.assertIn('[E1]', run['markdown'])
        status, report = self.api('/api/weekly', dict(start='2026-11-02', end='2026-11-06', mode='local'))
        self.assertEqual(status, 200)
        self.assertEqual(len(report['sources']), 1)
        self.assertEqual(self.api('/api/trash', dict(collection='sources', id=source['id']))[1]['moved'], 2)
        state = self.api('/api/state')[1]
        self.assertEqual(state['cards'], [])
        self.assertEqual(state['runs'][0]['markdown'], run['markdown'])
        self.assertEqual(state['weeklyReports'][0]['markdown'], report['markdown'])
        self.assertTrue(self.api('/api/backup')[1]['sources'][0]['deletedAt'])
        self.assertEqual(self.api('/api/restore', dict(collection='sources', id=source['id']))[1]['restored'], 2)

    def test_static_security_and_bad_inputs(self):
        self.assertIn(b'Career Ledger', self.api('/')[1])
        self.assertEqual(self.api('/.env')[0], 404)
        self.assertEqual(self.api('/api/state', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.api('/api/state', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.api('/api/daily', {}, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.api('/api/weekly', dict(date='2026-11-02'))[0], 400)
        self.assertEqual(self.api('/api/daily', [1, 2])[0], 400)
        self.assertNotIn('apiKey', self.api('/api/state')[1])

    def test_custom_range_excludes_deleted_and_outside_dates(self):
        for day in ('2026-12-29', '2026-12-30', '2027-01-01', '2027-01-04', '2027-01-05'):
            self.daily(day)
        source = self.daily('2027-01-02')
        self.api('/api/trash', dict(collection='sources', id=source['id']))
        status, report = self.api('/api/weekly', dict(start='2026-12-30', end='2027-01-04'))
        self.assertEqual(status, 200)
        self.assertEqual([s['date'] for s in report['sources']], ['2026-12-30', '2027-01-01', '2027-01-04'])

    def test_inflight_deletion_and_duplicate_task(self):
        source = self.daily()
        started, release = threading.Event(), threading.Event()
        class SlowAgent:
            def run(self, kind, data, state, mode):
                started.set()
                release.wait(5)
                return dict(sections=d.local_weekly(data['notes']), mode='local', trace=[])
        self.server.application.agent = SlowAgent()
        request = dict(start='2026-11-02', end='2026-11-03')
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.api, '/api/weekly', request)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.api('/api/weekly', request)[0], 409)
                self.api('/api/trash', dict(collection='sources', id=source['id']))
            finally:
                release.set()
            self.assertEqual(pending.result()[0], 409)
        self.assertEqual(self.api('/api/state')[1]['weeklyReports'], [])

    def test_old_ledger_loaded_by_api(self):
        self.server.application.store.file.write_text(json.dumps(FIXTURE['state']))
        state = self.api('/api/state')[1]
        self.assertEqual(state['cards'], FIXTURE['state']['cards'])

    def test_demo_and_process_exclusion(self):
        self.assertEqual(self.api('/api/demo', {})[0], 200)
        self.assertEqual(self.api('/api/demo', {})[0], 409)
        with self.assertRaises(RuntimeError):
            create_server(0, self.temp.name, config={})
