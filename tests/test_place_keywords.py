import unittest

from place_keywords import validate_keywords


class PlaceKeywordValidationTests(unittest.TestCase):
    def test_filters_type_region_and_generic_keywords(self):
        self.assertEqual(
            validate_keywords(
                ['놀거리', '볼거리', '복합문화공간', '카페', '맛집', '책방', '서울', '좌석', '책상', '콘센트'],
                source_text='서울의 카페에서 놀거리와 볼거리, 복합문화공간, 책방, 좌석과 콘센트를 제공한다',
            ),
            [],
        )

    def test_keeps_menu_and_distinctive_view_feature(self):
        self.assertEqual(
            validate_keywords(['드립커피', '마들렌', '한강 뷰', '한강뷰'], source_text='드립커피와 마들렌, 한강 전망'),
            ['드립커피', '마들렌', '한강 뷰'],
        )

    def test_evidence_requires_phrase_or_all_substantive_tokens(self):
        self.assertEqual(validate_keywords(['크림 라떼'], source_text='시그니처 크림라떼 판매'), ['크림 라떼'])
        self.assertEqual(validate_keywords(['딸기 케이크'], source_text='딸기 음료와 케이크'), ['딸기 케이크'])
        self.assertEqual(validate_keywords(['딸기 케이크'], source_text='딸기 음료만 판매'), [])

    def test_normalizes_whitespace_duplicates_and_caps_count(self):
        self.assertEqual(
            validate_keywords(['드립 커피', '드립커피', '마들렌', '케이크', '푸딩', '와인', 'LP', '라떼'],
                              source_text='드립커피 마들렌 케이크 푸딩 와인 LP 라떼'),
            ['드립 커피', '마들렌', '케이크', '푸딩', '와인', 'LP'],
        )

    def test_missing_source_preserves_legacy_validation_mode(self):
        self.assertEqual(validate_keywords(['드립커피', '카페']), ['드립커피'])


if __name__ == '__main__':
    unittest.main()
