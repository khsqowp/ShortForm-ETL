import json
import os
import sqlite3
import tempfile
import unittest
import sys
import types
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


class PlaceFeatures(unittest.TestCase):
    def test_classifier_real_category_fixtures(self):
        cases = {
            '강동구핸드드립카페': '카페', '디저트카페': '카페', '작업카페': '카페',
            '연남동맛집': '음식점', '사당이자카야': '음식점', '용산구한강뷰레스토랑': '음식점',
            '광진구도서관': '놀거리', '터프팅공방': '놀거리', '테마파크': '놀거리',
            '자동차문화공간': '놀거리', 'place': '기타',
        }
        for category, expected in cases.items():
            with self.subTest(category=category):
                self.assertEqual(classify_group('', category), expected)

    def test_keyword_validation(self):
        raw = [' 드립커피 ', '드립커피', '마들렌', '홍대카페', '서울', '카페', '수제버거', 4]
        self.assertEqual(validate_keywords(raw), ['드립커피', '마들렌', '수제버거'])
        self.assertEqual(len(validate_keywords(['a'] * 7)), 1)
        self.assertEqual(validate_keywords('not json'), [])
        self.assertEqual(validate_keywords(['가나다라마바사아자차카타파하가나다라마바사아자차카타파하']), [])

    def test_precision(self):
        self.assertEqual(address_precision('서울 마포구 와우산로 149'), 'venue')
        self.assertEqual(address_precision('서울 마포구 홍대입구역 인근'), 'area-only')

    def test_migration_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            db = sqlite3.connect(os.path.join(td, 'test.sqlite3'))
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
            pipeline.ensure_schema(db)
            pipeline.ensure_schema(db)
            cols = [r[1] for r in db.execute('PRAGMA table_info(places)')]
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
            denied, denied_result = request('/map', 'evil.example')
            self.assertFalse(denied.request_is_authorized())
            self.assertEqual(denied_result, {})
            map_handler, map_result = request('/map', 'localhost:' + str(app.PORT))
            map_handler.request_is_authorized = lambda: True
            map_handler.do_GET()
            html = map_result['body'].decode()
            self.assertEqual(map_result['status'], 200)
            self.assertNotIn('<img src=x onerror=alert(1)>', html)
            self.assertIn("textContent=String(v||'')", html)
            app.DATA, app.DB_PATH = old_data, old_db


if __name__ == '__main__':
    unittest.main()
