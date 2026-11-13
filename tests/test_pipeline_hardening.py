import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

import linkcollect_pipeline as pipeline


class PipelineHardeningTests(unittest.TestCase):
    def test_download_classification_and_retry_schedule(self):
        cases = [
            ("This content isn't available to everyone", 'audience-restricted', 1),
            ('Login required', 'login-required', 1),
            ('Unsupported URL', 'unsupported-photo', 1),
            ('HTTP Error 429: Too Many Requests', 'transient-rate-limit', 3),
            ('extractor exploded', 'other', 1),
        ]
        for stderr, category, count in cases:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as temp:
                binary = Path(temp) / 'yt-dlp'
                binary.touch()
                sleeps = []
                results = [mock.Mock(returncode=1, stderr=(stderr + ' https://x.test/?token=secret').encode()) for _ in range(3)]
                with mock.patch.object(pipeline, 'MEDIA', Path(temp) / 'media'), \
                        mock.patch.object(pipeline.subprocess, 'run', side_effect=results) as run:
                    result, error = pipeline.download_public_post(
                        {'id': 'job1', 'url': 'https://www.youtube.com/watch?v=abc'},
                        {'LINKCOLLECT_YTDLP_BIN': str(binary)}, sleep_fn=sleeps.append)
                self.assertIsNone(result)
                self.assertIn('[{}]'.format(category), error)
                self.assertNotIn('secret', error)
                self.assertEqual(run.call_count, count)
                self.assertEqual(sleeps, [30, 90][:count - 1])

    def test_transcribe_cache_argument_and_nonzero_log_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(pipeline, 'DATA', Path(temp)), \
                    mock.patch.object(pipeline.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=b'[]')) as run:
                self.assertEqual(pipeline.transcribe_audio('audio.wav', {
                    'LINKCOLLECT_WHISPER_MODEL': 'medium', 'LINKCOLLECT_WHISPER_CACHE': '/cache/path'}), ('', []))
                args = run.call_args.args[0]
                self.assertEqual(args[-1], '/cache/path')
                self.assertIn('download_root', args[2])
                log = (Path(temp) / 'logs' / 'stt_debug.log').read_text()
                self.assertIn('empty-output', log)
                self.assertNotIn('audio', log)

    def test_transcribe_missing_path_and_nonzero_rc_are_logged(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(pipeline, 'DATA', Path(temp)):
            self.assertEqual(pipeline.transcribe_audio('', {}), ('', []))
            with mock.patch.object(pipeline.subprocess, 'run', return_value=mock.Mock(returncode=7, stdout=b'')):
                self.assertEqual(pipeline.transcribe_audio('a.wav', {}), ('', []))
            log = (Path(temp) / 'logs' / 'stt_debug.log').read_text()
            self.assertIn('missing-audio-path', log)
            self.assertIn('nonzero-return-code-7', log)

    def test_transcribe_low_confidence_and_exception_reasons(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(pipeline, 'DATA', Path(temp)):
            low_confidence = [{'text': 'sample transcript with enough words', 'no_speech_prob': 0.9,
                               'avg_logprob': -0.1, 'start': 0, 'end': 1}]
            with mock.patch.object(pipeline.subprocess, 'run', return_value=mock.Mock(
                    returncode=0, stdout=json.dumps(low_confidence).encode())):
                self.assertEqual(pipeline.transcribe_audio('audio.wav', {}), ('', []))
            with mock.patch.object(pipeline.subprocess, 'run', side_effect=OSError('private detail')):
                self.assertEqual(pipeline.transcribe_audio('audio.wav', {}), ('', []))
            log = (Path(temp) / 'logs' / 'stt_debug.log').read_text()
            self.assertIn('filtered-low-confidence', log)
            self.assertIn('exception-OSError', log)
            self.assertNotIn('sample transcript', log)
            self.assertNotIn('private detail', log)

    def test_hard_transcript_correction_is_discarded_without_text_log(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(pipeline, 'DATA', Path(temp)), \
                mock.patch.object(pipeline, 'api_json', return_value={'choices': [{'message': {'content': '완전히 다른 내용입니다'}}]}):
            corrected = pipeline.correct_transcript_typos({'LINKCOLLECT_AI_PROVIDER': 'hermes'},
                '오늘 날씨가 참 좋습니다.', job_id='job-1')
            self.assertEqual(corrected, '오늘 날씨가 참 좋습니다.')
            log = (Path(temp) / 'logs' / 'stt_debug.log').read_text()
            self.assertIn('job=job-1', log)
            self.assertIn('length_ratio=', log)
            self.assertNotIn('오늘', log)

    def test_geocode_search_401_disables_search_for_process(self):
        pipeline._NAVER_SEARCH_DISABLED = False
        config = {'LINKCOLLECT_NCP_SEARCH_API_KEY_ID': 'sid', 'LINKCOLLECT_NCP_SEARCH_API_KEY': 'skey',
                  'LINKCOLLECT_NCP_MAP_API_KEY_ID': 'mid', 'LINKCOLLECT_NCP_MAP_API_KEY': 'mkey'}
        calls = []
        def urlopen(request, timeout):
            calls.append(request.full_url)
            if 'search/v1/local' in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 401, 'no', {}, None)
            return _Response({'addresses': []})
        with mock.patch.object(pipeline.urllib.request, 'urlopen', side_effect=urlopen):
            self.assertIsNone(pipeline.naver_geocode(config, 'Seoul address', 'Cafe'))
            self.assertIsNone(pipeline.naver_geocode(config, 'Seoul address', 'Cafe'))
        self.assertEqual(sum('search/v1/local' in url for url in calls), 1)
        pipeline._NAVER_SEARCH_DISABLED = False

    def test_geocode_map_key_fallback_search_match_and_missing_keys(self):
        pipeline._NAVER_SEARCH_DISABLED = False
        with mock.patch.object(pipeline.urllib.request, 'urlopen', return_value=_Response({'items': [
                {'title': 'Cafe', 'address': 'Seoul', 'roadAddress': '', 'mapx': '1271234567', 'mapy': '375123456'}]})):
            self.assertEqual(pipeline.naver_geocode({'LINKCOLLECT_NCP_MAP_API_KEY_ID': 'id',
                'LINKCOLLECT_NCP_MAP_API_KEY': 'key'}, 'Seoul', 'Cafe'), (37.5123456, 127.1234567))
        pipeline._NAVER_SEARCH_DISABLED = False
        with mock.patch.object(pipeline.urllib.request, 'urlopen') as urlopen:
            self.assertIsNone(pipeline.naver_geocode({}, 'Seoul', 'Cafe'))
            urlopen.assert_not_called()


class _Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.body).encode()


if __name__ == '__main__':
    unittest.main()
