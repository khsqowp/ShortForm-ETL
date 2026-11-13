"""Rule-based groups for place archive categories."""

GROUPS = ('음식점', '카페', '놀거리', '기타')
CAFE = ('카페', '커피', '베이커리', '디저트', '북카페', '커피바', '커피앤바', '재즈카페')
FOOD = ('맛집', '식당', '음식점', '술집', '고기', '라멘', '이자카야', '브런치', '바', '횟집', '파스타', '카츠', '오마카세', '요리', '레스토랑', '와인바', '레코드바', '바이닐바', '책과술')
FUN = ('전시', '공원', '포토', '체험', '방탈출', '놀이', '클라이밍', '산책', '팝업', '도서관', '서점', '책방', '숍', '공방', '공간', '라운지', '갤러리', '문화', '라이브러리', '자료실', '쉼터', '스터디룸', '터프팅', '테마파크', '플스방', '필사', '서재')

def classify_group(name, category):
    text = (str(category or '') + ' ' + str(name or '')).casefold()
    if any(word in text for word in CAFE):
        return '카페'
    if any(word in text for word in FOOD):
        return '음식점'
    if any(word in text for word in FUN):
        return '놀거리'
    return '기타'
