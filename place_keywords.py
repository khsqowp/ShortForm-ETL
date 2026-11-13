"""Validation for concise, representative place keywords."""
import re

TYPE_WORDS = ('카페', '맛집', '식당', '음식점', '술집', '바', '장소', '가게', '레스토랑', '베이커리', '디저트')
REGION_RE = re.compile(r'(?:서울|부산|인천|대구|대전|광주|울산|세종|제주|경기|강원|충북|충남|전북|전남|경북|경남|홍대|강남|성수|연남|을지로|문래|[가-힣]{1,6}(?:구|군|시|동|읍|면|역))')

def validate_keywords(value, name='', address='', category=''):
    if isinstance(value, str):
        try:
            import json
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    result, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            continue
        word = ' '.join(item.strip().split())
        tokens = word.split()
        has_type = any(token in TYPE_WORDS or any(token.endswith(t) for t in TYPE_WORDS if t != '바') for token in tokens)
        if not word or len(word) > 20 or has_type or REGION_RE.search(word):
            continue
        if word.casefold() in seen:
            continue
        seen.add(word.casefold())
        result.append(word)
        if len(result) == 6:
            break
    return result
