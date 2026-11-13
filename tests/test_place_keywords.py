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
            ['드립커피', '마들렌', '한강뷰'],
        )

    def test_canonicalizes_view_spacing_and_outlook_variants(self):
        self.assertEqual(
            validate_keywords(
                ['한강 뷰', '한강뷰', '한강 전망', '노을 뷰', '노을뷰', '노을 전망', '야경 뷰', '야경뷰'],
                source_text='한강 전망 노을 전망 야경 뷰',
            ),
            ['한강뷰', '노을뷰', '야경뷰'],
        )

    def test_drops_overlength_phrases_without_truncating(self):
        self.assertEqual(
            validate_keywords(
                ['덕수궁 돌담길이 내려다보이는 통창 자리', '서해를 품은 압도적인 노을 뷰', '통창 자리'],
                source_text='덕수궁 돌담길이 내려다보이는 통창 자리와 서해를 품은 압도적인 노을 뷰, 통창 자리',
            ),
            ['통창 자리'],
        )

    def test_filters_generic_terms_but_keeps_distinctive_features(self):
        self.assertEqual(
            validate_keywords(
                ['차 한 잔', '인테리어 소품', '소품', '귀여운 소품', '하프메뉴', '대형 스피커', '기찻길'],
                source_text='하프메뉴 주문이 가능하고 대형 스피커의 음악과 기찻길 전망이 특징이다',
            ),
            ['하프메뉴', '대형 스피커', '기찻길'],
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

    def test_filters_transit_and_direction_phrases(self):
        self.assertEqual(
            validate_keywords(
                ['봉천역에서 도보 2~3분', '마곡역 근처', '3번 출구', '걸어서 5분 거리',
                 '역 앞 위치 2분', '루프탑', '통창 자리'],
            ),
            ['루프탑', '통창 자리'],
        )

    def test_filters_vague_mood_phrases_and_keeps_concrete_features(self):
        self.assertEqual(
            validate_keywords(
                ['작고 마법 같은 분위기', '동남아 리조트 분위기', '채광이 좋고 따뜻한 분위기',
                 '호텔 라운지 같은 분위기',
                 '유럽풍 공간', '70년대 빈티지 오디오', '일본 교토 가정식 분위기'],
            ),
            ['유럽풍 공간', '70년대 빈티지 오디오', '일본 교토 가정식 분위기'],
        )


if __name__ == '__main__':
    unittest.main()
