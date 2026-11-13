import unittest
from error_classifier import classify_error


class ErrorClassifierTests(unittest.TestCase):
    def test_error_classes(self):
        cases = {
            "This content isn't available to everyone: certain audiences": 'audience-restricted',
            'Login required to view this content': 'login-required',
            'Unsupported URL': 'unsupported-photo',
            'There is no video in this post': 'unsupported-photo',
            'HTTP Error 429: Too Many Requests': 'transient-rate-limit',
            'unexpected extractor failure': 'other',
            '': 'other',
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)


if __name__ == '__main__':
    unittest.main()
