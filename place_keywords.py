"""Validation for concise, evidence-backed representative place keywords."""
import json
import re
import unicodedata

TYPE_WORDS = (
    '놀거리', '볼거리', '복합문화공간', '카페', '맛집', '책방', '식당', '음식점',
    '술집', '바', '장소', '가게', '레스토랑', '베이커리', '디저트',
)
LOW_INFO_WORDS = (
    '좌석', '책상', '책장', '테이블', '콘센트', '노트북 작업', '작업', '열람 공간',
    '공간', '아이템', '예약', '체험', '놀이', '화장실', '음료', '커피', '술', '책',
    '소파', '조명', '편안한', '차분한', '여유로운 분위기', '큰 창', '분위기',
    '차 한 잔', '인테리어 소품', '소품',
)
REGION_WORDS = (
    '서울', '부산', '인천', '대구', '대전', '광주', '울산', '세종', '제주',
    '경기', '강원', '충북', '충남', '전북', '전남', '경북', '경남', '홍대',
    '강남', '성수', '연남', '을지로', '문래', '한강', '백운호수', '남산',
    '인왕산', '창덕궁', '서해', '코엑스', '양화대교', '망원 한강공원',
)
TOKEN_STOPWORDS = frozenset(
    '좋은 좋음 같은 위한 통해 가는 있는 없는 에서 으로 이며 그리고 또한 '
    '너무 매우 다양한 편안한 넓은 오래 조용히 자리마다 읽을 수 있는'.split()
)
TRANSIT_MARKERS = ('도보', '역에서', '역 ', '출구', '분 거리', '걸어서')
GENERIC_MOOD_HEADS = (
    '작고', '마법 같은', '따뜻한', '포근한', '아늑한', '세련된', '감성적인',
    '고급스러운', '이국적인', '편안한', '채광이 좋고', '분위기 좋은',
)


def normalize_keyword(value):
    """Canonical comparison form: Unicode normalization and whitespace/punctuation removal."""
    value = unicodedata.normalize('NFKC', str(value)).casefold()
    value = re.sub(r'[\s\W_]+', '', value, flags=re.UNICODE)
    return value.replace('전망', '뷰')


def _canonical_display(word):
    """Normalize view spelling for display while preserving other phrase spacing."""
    word = re.sub(r'\s+', ' ', word.strip())
    word = re.sub(r'\s*전망', '뷰', word)
    word = re.sub(r'\s+뷰', '뷰', word)
    return word


def _is_type_word(word):
    compact = normalize_keyword(word)
    for term in TYPE_WORDS:
        normalized = normalize_keyword(term)
        if compact == normalized or compact.endswith(normalized):
            return True
    return False


def _has_evidence(word, source_text):
    """Require the whole phrase or every substantive token to occur in gathered text."""
    source = normalize_keyword(source_text)
    phrase = normalize_keyword(word)
    if phrase and phrase in source:
        return True
    tokens = [normalize_keyword(token) for token in re.findall(r'[가-힣A-Za-z0-9]+', word)]
    tokens = [token for token in tokens if len(token) > 1 and token not in TOKEN_STOPWORDS]
    return bool(tokens) and all(token in source for token in tokens)


def _is_transit_or_direction_phrase(word):
    if any(marker in word for marker in TRANSIT_MARKERS):
        return True
    if '위치' in word:
        return bool(re.search(r'\d+\s*분|[가-힣]+역|역\s*\d+번?', word))
    return False


def _is_vague_mood_phrase(word):
    if not word.endswith(('분위기', '느낌')):
        return False
    compact = re.sub(r'\s+', '', word)
    # This geographic resort shorthand is explicitly considered non-specific.
    if '동남아리조트' in compact:
        return True
    if '같은' in compact:
        return True
    return any(head.replace(' ', '') in compact for head in GENERIC_MOOD_HEADS)


def validate_keywords(value, name='', address='', category='', source_text=None, max_count=6):
    """Return at most six distinct, specific keywords grounded in this place's source."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    result, seen = [], set()
    low_info = {normalize_keyword(term) for term in LOW_INFO_WORDS}
    for item in value:
        if not isinstance(item, str):
            continue
        word = _canonical_display(item)
        canonical = normalize_keyword(word)
        if not word or len(word) > 14 or not canonical:
            continue
        if _is_type_word(word):
            continue
        if _is_transit_or_direction_phrase(word) or _is_vague_mood_phrase(word):
            continue
        if canonical in low_info or '소품' in canonical:
            continue
        # A view is a distinctive feature. Keep the view phrase; reject bare region names.
        is_view = any(term in word for term in ('뷰', '전망', '야경', '노을'))
        if not is_view and any(normalize_keyword(region) in canonical for region in REGION_WORDS):
            continue
        if source_text is not None and not _has_evidence(word, source_text):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(word)
        if len(result) >= max_count:
            break
    return result
