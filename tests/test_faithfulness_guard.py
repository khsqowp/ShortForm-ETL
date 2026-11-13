import unittest

from faithfulness_guard import assess_faithfulness


class FaithfulnessTests(unittest.TestCase):
    def test_korean_counter_value_changes_are_hard_rejects(self):
        for raw, corrected in (
            ('삼 개 남았어요', '사 개 남았어요'),
            ('건물은 삼층이에요', '건물은 사층이에요'),
            ('삼 개', '사 개'),
        ):
            with self.subTest(raw=raw, corrected=corrected):
                self.assertIn('numeric-value-change',
                              assess_faithfulness(raw, corrected)['reasons'])

    def test_isolated_korean_numeral_shaped_words_are_soft(self):
        for raw, corrected in (
            ('사과가 맛있어요', '삼과가 맛있어요'),
            ('삼성 제품', '사성 제품'),
            ('일론 머스크', '이론 머스크'),
        ):
            with self.subTest(raw=raw, corrected=corrected):
                result = assess_faithfulness(raw, corrected)
                self.assertNotIn('numeric-value-change', result['reasons'])
                self.assertIn('numeric-ambiguous', result['flags'])

    def test_normalization_and_ambiguous_examples(self):
        for raw, corrected in (
            ('르라보 베일 일구라는 향수인데', '르라보 베이 19라는 향수인데'),
            ('최대 11점 오점 올랐대요', '최대 11.5점 올랐대요'),
        ):
            with self.subTest(raw=raw, corrected=corrected):
                self.assertNotIn('numeric-value-change',
                                 assess_faithfulness(raw, corrected)['reasons'])

    def test_proper_name_change_is_soft(self):
        result = assess_faithfulness('Shaden website', 'Shodan website')
        self.assertTrue(result['accepted'])
        self.assertIn('proper-noun-change', result['flags'])


if __name__ == '__main__':
    unittest.main()
