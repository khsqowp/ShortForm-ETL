import json
import os
import sqlite3
import tempfile
import unittest
import sys
import types
import threading
from http.server import HTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from unittest.mock import patch

# Python 3.13 removed cgi; the legacy upload parser is not exercised in tests.
if 'cgi' not in sys.modules:
    try:
        import cgi  # noqa: F401
    except ModuleNotFoundError:
        sys.modules['cgi'] = types.ModuleType('cgi')
import linkcollect_app as app
import linkcollect_pipeline as pipeline
from address_precision import address_precision
from place_groups import classify_group
from place_keywords import validate_keywords
from scripts.backfill_place_keywords import backfill


class PlaceFeatures(unittest.TestCase):
    def test_classifier_real_category_fixtures(self):
        cases = {
            '강동구핸드드립카페': '카페', '디저트카페': '카페', '작업카페': '카페',
            '연남동맛집': '음식점', '사당이자카야': '음식점', '용산구한강뷰레스토랑': '음식점',
            '광진구도서관': '놀거리', '터프팅공방': '놀거리', '테마파크': '놀거리',
            '자동차문화공간': '놀거리', 'place': '기타',
            '수바코|소품숍': '놀거리',
            '터프팅워크룸 바이주피터|터프팅공방': '놀거리',
            '겨울이 머무는 집|place': '기타',
            '앙쩨띠 커피로스터리|place': '카페',
            '홍대카페|복합문화공간': '놀거리',
            '르다|용산구한강뷰레스토랑': '음식점',
            '문학살롱 초고|책과술': '음식점',
            '무슨 이름|북카페류': '카페',
            '수바코|음식점': '음식점',
            '가게|LP바': '음식점',
        }
        for fixture, expected in cases.items():
            if '|' in fixture:
                name, category = fixture.split('|')
            else:
                name, category = '', fixture
            with self.subTest(fixture=fixture):
                self.assertEqual(classify_group(name, category), expected)

    def test_keyword_validation(self):
        raw = [' 드립커피 ', '드립커피', '마들렌', '홍대카페', '서울', '카페', '수제버거', 4]
        self.assertEqual(validate_keywords(raw), ['드립커피', '마들렌', '수제버거'])
        self.assertEqual(len(validate_keywords(['a'] * 7)), 1)
        self.assertEqual(validate_keywords('not json'), [])
        self.assertEqual(validate_keywords(['가나다라마바사아자차카타파하가나다라마바사아자차카타파하']), [])
        self.assertEqual(validate_keywords({'keywords': ['라떼']}), [])
        self.assertEqual(validate_keywords(['라떼', None, 1, '홍대', '맛집', '라떼']), ['라떼'])
        self.assertEqual(validate_keywords(['a', 'b', 'c', 'd', 'e', 'f', 'g']), ['a', 'b', 'c', 'd', 'e', 'f'])

    def test_precision(self):
        self.assertEqual(address_precision('서울 마포구 와우산로 149'), 'venue')
        self.assertEqual(address_precision('서울 마포구 홍대입구역 인근'), 'area-only')
        self.assertEqual(address_precision('서울 영등포구 문래동2가'), 'area-only')
        self.assertEqual(address_precision('서울 영등포구 문래동2가 12-3'), 'venue')
        self.assertEqual(address_precision('서울 영등포구 경인로 12 3층'), 'venue')

    def test_migration_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            db = sqlite3.connect(os.path.join(td, 'test.sqlite3'))
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
            db.execute('''CREATE TABLE places(id TEXT PRIMARY KEY,dedupe_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,address TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT 'place',
                latitude REAL,longitude REAL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            pipeline.ensure_schema(db)
            first = [r[1] for r in db.execute('PRAGMA table_info(places)')]
            pipeline.ensure_schema(db)
            cols = [r[1] for r in db.execute('PRAGMA table_info(places)')]
            self.assertEqual(cols, first)
            self.assertEqual(cols.count('category_group'), 1)
            self.assertEqual(cols.count('keywords'), 1)
            db.close()

    def test_map_key_fallback(self):
        with patch.object(app, 'load_env', return_value={'LINKCOLLECT_NCP_MAP_API_KEY_ID': 'fallback-key'}):
            self.assertEqual(app.naver_map_client_id(), 'fallback-key')
        with patch.object(app, 'load_env', return_value={'LINKCOLLECT_NCP_MAP_CLIENT_ID': 'primary', 'LINKCOLLECT_NCP_MAP_API_KEY_ID': 'fallback'}):
            self.assertEqual(app.naver_map_client_id(), 'primary')

    def test_http_api_shape_auth_and_hostile_text(self):
        with tempfile.TemporaryDirectory() as td:
            old_data, old_db = app.DATA, app.DB_PATH
            app.DATA = type(old_data)(td)
            app.DB_PATH = app.DATA / 'test.sqlite3'
            with app.db_conn() as db:
                db.execute("INSERT INTO places(id,dedupe_hash,name,address,category,category_group,keywords,latitude,longitude,geocode_status,created_at,updated_at,naver_map_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ('a' * 16, 'hash', '<img src=x onerror=alert(1)>', '서울 마포구', '작업카페', '카페', json.dumps(['드립커피'], ensure_ascii=False), 37.5, 127.0, 'resolved', 'now', 'now', ''))
            def request(path, host):
                handler = app.Handler.__new__(app.Handler)
                handler.path = path
                handler.headers = {'Host': host}
                handler.wfile = __import__('io').BytesIO()
                result = {}
                handler.json = lambda status, value: result.update(status=status, body=value)
                handler.respond = lambda status, body, content_type: result.update(status=status, body=body, content_type=content_type)
                return handler, result
            api_handler, api_result = request('/api/places', 'localhost:' + str(app.PORT))
            api_handler.request_is_authorized = lambda: True
            api_handler.do_GET()
            body = api_result['body']
            self.assertEqual(api_result['status'], 200)
            place = body['places'][0]
            self.assertEqual(place['category_group'], '카페')
            self.assertEqual(place['keywords'], ['드립커피'])
            self.assertEqual(place['precision'], 'area-only')
            self.assertIn('latitude', place)
            map_handler, map_result = request('/map', 'localhost:' + str(app.PORT))
            map_handler.request_is_authorized = lambda: True
            map_handler.do_GET()
            html = map_result['body'].decode()
            self.assertEqual(map_result['status'], 200)
            self.assertNotIn('<img src=x onerror=alert(1)>', html)
            self.assertIn("textContent=String(v||'')", html)
            self.assertNotIn("innerHTML=.*p.name", html)
            app.DATA, app.DB_PATH = old_data, old_db

    def test_http_real_handler_api_map_and_forbidden_host_origin(self):
        with tempfile.TemporaryDirectory() as td:
            old_data, old_db, old_port, old_load_env = app.DATA, app.DB_PATH, app.PORT, app.load_env
            app.DATA = type(old_data)(td)
            app.DB_PATH = app.DATA / 'http.sqlite3'
            try:
                server = HTTPServer(('127.0.0.1', 0), app.Handler)
            except OSError:
                # Restricted sandboxes may forbid loopback sockets; exercise the
                # identical Handler methods directly, including status codes.
                def direct(path, host, origin=None):
                    handler = app.Handler.__new__(app.Handler)
                    handler.path = path
                    handler.headers = {'Host': host}
                    if origin:
                        handler.headers['Origin'] = origin
                    handler.wfile = __import__('io').BytesIO()
                    result = {}
                    handler.json = lambda status, value: result.update(status=status, body=value)
                    handler.respond = lambda status, body, content_type: result.update(status=status, body=body, content_type=content_type)
                    handler.do_GET()
                    return result
                with app.db_conn() as db:
                    db.execute("INSERT INTO places(id,dedupe_hash,name,address,category,category_group,keywords,latitude,longitude,geocode_status,created_at,updated_at,naver_map_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ('b' * 16, 'hash2', '<img src=x onerror=alert(1)>', '문래동2가', '소품숍', '놀거리', '[]', None, None, 'pending', 'now', 'now', ''))
                app.load_env = lambda: {}
                api = direct('/api/places', 'localhost:' + str(app.PORT))
                self.assertEqual(api['status'], 200)
                self.assertEqual(api['body']['places'][0]['name'], '<img src=x onerror=alert(1)>')
                page = direct('/map', 'localhost:' + str(app.PORT))
                self.assertEqual(page['status'], 200)
                html = page['body'].decode('utf-8')
                self.assertNotIn('<img src=x onerror=alert(1)>', html)
                self.assertIn("text('name',p.name)", html)
                self.assertEqual(direct('/map', 'evil.example')['status'], 403)
                self.assertEqual(direct('/map', 'localhost:' + str(app.PORT), 'https://evil.example')['status'], 403)
                app.DATA, app.DB_PATH, app.PORT, app.load_env = old_data, old_db, old_port, old_load_env
                return
            app.PORT = server.server_port
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                with app.db_conn() as db:
                    db.execute("INSERT INTO places(id,dedupe_hash,name,address,category,category_group,keywords,latitude,longitude,geocode_status,created_at,updated_at,naver_map_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ('b' * 16, 'hash2', '<img src=x onerror=alert(1)>', '문래동2가', '소품숍', '놀거리', '[]', None, None, 'pending', 'now', 'now', ''))
                base = 'http://127.0.0.1:{}'.format(app.PORT)
                with urlopen(Request(base + '/api/places', headers={'Host': '127.0.0.1:{}'.format(app.PORT)})) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    self.assertEqual(response.status, 200)
                self.assertEqual(data['places'][0]['name'], '<img src=x onerror=alert(1)>')
                with urlopen(Request(base + '/map', headers={'Host': '127.0.0.1:{}'.format(app.PORT)})) as response:
                    html = response.read().decode('utf-8')
                    self.assertEqual(response.status, 200)
                self.assertNotIn('<img src=x onerror=alert(1)>', html)
                self.assertIn("text('name',p.name)", html)
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + '/map', headers={'Host': 'evil.example'}))
                self.assertEqual(denied.exception.code, 403)
                with self.assertRaises(HTTPError) as denied_origin:
                    urlopen(Request(base + '/map', headers={'Host': '127.0.0.1:{}'.format(app.PORT), 'Origin': 'https://evil.example'}))
                self.assertEqual(denied_origin.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.DATA, app.DB_PATH, app.PORT = old_data, old_db, old_port

    def test_backfill_dry_run_and_apply_with_fake_ai(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'test.sqlite3')
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            db.executescript('''CREATE TABLE places(id TEXT PRIMARY KEY,name TEXT,address TEXT,category TEXT,keywords TEXT,hidden INTEGER DEFAULT 0);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,created_at TEXT,text TEXT);
                CREATE TABLE analyses(job_id TEXT,summary TEXT,cleaned_content TEXT,stt_corrected_text TEXT,transcript_cleaned TEXT,transcript TEXT);
                CREATE TABLE place_sources(place_id TEXT,job_id TEXT);''')
            db.execute("INSERT INTO places VALUES('p1','가게A','서울 마포구','카페','[]',0)")
            db.execute("INSERT INTO jobs VALUES('j1','now','메뉴 원문')")
            db.execute("INSERT INTO analyses VALUES('j1','요약','크림라떼가 인기다','','','')")
            db.execute("INSERT INTO place_sources VALUES('p1','j1')")
            calls = []
            def fake_ai(config, place, source):
                calls.append((place['name'], source))
                return ['크림라떼', '카페', '홍대', '크림라떼']
            out = os.path.join(td, 'proposal.json')
            proposal = backfill(db, {}, out, apply=False, ai_callable=fake_ai)
            self.assertEqual(proposal[0]['keywords'], ['크림라떼'])
            self.assertEqual(db.execute("SELECT keywords FROM places WHERE id='p1'").fetchone()[0], '[]')
            applied = backfill(db, {}, out, apply=True, ai_callable=fake_ai)
            self.assertEqual(applied[0]['keywords'], ['크림라떼'])
            self.assertEqual(json.loads(db.execute("SELECT keywords FROM places WHERE id='p1'").fetchone()[0]), ['크림라떼'])
            self.assertIn('가게A', calls[0][1])
            self.assertEqual(backfill(db, {}, out, apply=True, ai_callable=fake_ai), [])
            db.close()


if __name__ == '__main__':
    unittest.main()
