"""Rule-based place groups. Specific categories take precedence over names."""

GROUPS = ('음식점', '카페', '놀거리', '기타')

# Order is deliberate: category-specific categories win even when a name contains
# a different group keyword. Longer matches are checked before shorter matches.
_CATEGORY_RULES = (
    ('카페', ('호텔라운지카페', '북카페', '재즈카페', '디저트카페', '베이커리카페', '커피로스터리', '카페', '커피', '베이커리', '디저트')),
    ('음식점', ('용산구한강뷰레스토랑', '레스토랑', '이자카야', '횟집', '맛집', '음식점', '식당', '술집', '와인바', '레코드바', '바이닐바', 'LP바', '책과술', '오마카세', '브런치', '파스타', '라멘', '카츠', '요리')),
    ('놀거리', ('복합문화공간', '테마파크', '플스방', '도서관', '터프팅공방', '소품숍', '공유서재', '무료라운지', '역사자료실', '필사공간', '공방', '서점', '책방', '서재', '자료실', '쉼터', '라운지', '숍', '전시', '체험', '방탈출', '클라이밍', '팝업', '문화공간', '라이브러리', '스터디룸', '놀거리', '놀이공간', '공간')),
)
_NAME_RULES = (
    ('카페', ('호텔라운지카페', '북카페', '재즈카페', '디저트카페', '베이커리카페', '커피로스터리', '카페', '커피', '베이커리', '디저트')),
    ('놀거리', ('복합문화공간', '테마파크', '플스방', '도서관', '터프팅워크룸', '터프팅공방', '소품숍', '공유서재', '무료라운지', '역사자료실', '필사공간', '공방', '서점', '책방', '서재', '자료실', '쉼터', '라운지', '숍', '전시', '체험', '방탈출', '클라이밍', '팝업', '문화공간', '라이브러리', '스터디룸', '공간')),
    ('음식점', ('와인바', '레코드바', '바이닐바', 'LP바', '이자카야', '횟집', '맛집', '식당', '음식점', '술집', '책과술', '레스토랑')),
)


def _match_rules(text, rules):
    matches = []
    for group_index, (group, words) in enumerate(rules):
        for word_index, word in enumerate(words):
            if word.casefold() in text:
                matches.append((len(word), -group_index, -word_index, group))
    if matches:
        return max(matches)[3]
    return None


def classify_group(name, category):
    category_text = str(category or '').strip().casefold()
    # 'place' is the legacy generic label; all nonempty other categories are
    # specific and must not be overridden by a contradictory venue name.
    if category_text and category_text != 'place':
        return _match_rules(category_text, _CATEGORY_RULES) or '기타'
    name_text = str(name or '').casefold()
    return _match_rules(name_text, _NAME_RULES) or '기타'
