import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Python 3.13+ removed cgi; these tests do not exercise legacy upload parsing.
if 'cgi' not in sys.modules:
    try:
        import cgi  # noqa: F401
    except ModuleNotFoundError:
        sys.modules['cgi'] = types.ModuleType('cgi')

from scripts import backfill_place_keywords as keywords


class BackfillPlaceKeywordsTests(unittest.TestCase):
    def test_build_prompt_keeps_template_and_source_braces_literal(self):
        place = {'name': "가게{% 메뉴}"}
        source = '원문 {"quoted": "value"} and \'single quotes\' and "double quotes"'
        prompt = keywords.build_prompt(place, source)
        self.assertIn('형식: {"keywords":["드립커피"]}', prompt)
        self.assertIn("대상 장소: 가게{% 메뉴}", prompt)
        self.assertTrue(prompt.endswith(source))

    def test_ask_keywords_parses_supported_response_shapes(self):
        responses = [
            ('{"keywords":["라떼"]}', ['라떼']),
            ('```json\n{"keywords":["마들렌"]}\n```', ['마들렌']),
            ('결과는 다음과 같습니다: {"keywords":["드립커피"]} 감사합니다.', ['드립커피']),
            ('not json', []),
        ]
        for content, expected in responses:
            with self.subTest(content=content):
                response = {'choices': [{'message': {'content': content}}]}
                with patch.object(keywords.pipeline, 'api_json', return_value=response):
                    self.assertEqual(keywords.ask_keywords({}, {'name': '가게{%}'}, '원문'), expected)

    def test_ask_keywords_payload_omits_response_format(self):
        response = {'choices': [{'message': {'content': '{"keywords":[]}'} }]}
        with patch.object(keywords.pipeline, 'api_json', return_value=response) as api_json:
            keywords.ask_keywords({}, {'name': '가게'}, '원문')
        payload = api_json.call_args.args[2]
        self.assertNotIn('response_format', payload)

    def test_api_exception_records_failure_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            db = sqlite3.connect(os.path.join(td, 'test.sqlite3'))
            db.row_factory = sqlite3.Row
            db.executescript('''CREATE TABLE places(id TEXT PRIMARY KEY,name TEXT,address TEXT,category TEXT,keywords TEXT,hidden INTEGER DEFAULT 0);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,created_at TEXT,text TEXT);
                CREATE TABLE analyses(job_id TEXT,summary TEXT,cleaned_content TEXT,stt_corrected_text TEXT,transcript_cleaned TEXT,transcript TEXT);
                CREATE TABLE place_sources(place_id TEXT,job_id TEXT);''')
            for place_id, name in [('p1', '실패장소'), ('p2', '성공장소')]:
                db.execute('INSERT INTO places VALUES(?,?,?,?,?,0)', (place_id, name, '주소', '식당', '[]'))
                job_id = 'j' + place_id
                db.execute('INSERT INTO jobs VALUES(?,?,?)', (job_id, 'now', '라떼 원문'))
                db.execute('INSERT INTO place_sources VALUES(?,?)', (place_id, job_id))

            response = {'choices': [{'message': {'content': '{"keywords":["라떼"]}'}}]}
            def api_json(*args, **kwargs):
                if '실패장소' in args[2]['messages'][1]['content']:
                    raise RuntimeError('API unavailable')
                return response

            output = os.path.join(td, 'proposal.json')
            with patch.object(keywords.pipeline, 'api_json', side_effect=api_json):
                results = keywords.backfill(db, {}, output)
            self.assertEqual([item['place_id'] for item in results['proposals']], ['p2'])
            self.assertEqual(results['proposals'][0]['keywords'], ['라떼'])
            self.assertEqual(results['errors'][0]['place_id'], 'p1')
            self.assertEqual(results['errors'][0]['error_type'], 'RuntimeError')
            self.assertLessEqual(len(results['errors'][0]['message']), 200)
            self.assertEqual(results['stats'], {'attempted': 2, 'ok': 1, 'failed': 1, 'empty': 0})
            with open(output, encoding='utf-8') as proposal_file:
                payload = json.load(proposal_file)
                self.assertEqual(payload['proposals'][0]['place_id'], 'p2')
                self.assertEqual(payload['errors'][0]['place_id'], 'p1')
            db.close()

    def test_script_help_works_from_another_cwd(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(repo, 'scripts', 'backfill_place_keywords.py')
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, script, '--help'], cwd=cwd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--out', result.stdout)


if __name__ == '__main__':
    unittest.main()
