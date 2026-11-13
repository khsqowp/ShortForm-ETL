"""Offline transcript correction acceptance checks; no external services."""
import difflib
import logging
import re

MIN_LENGTH_RATIO = 0.74
MAX_LENGTH_RATIO = 1.34
MIN_SIMILARITY = 0.54
_LOG = logging.getLogger(__name__)
_SINO = set('영공일이삼사오육칠팔구십백천만')
_NATIVE = {'하나': '1', '한': '1', '둘': '2', '두': '2', '셋': '3', '세': '3',
           '넷': '4', '네': '4', '다섯': '5', '여섯': '6', '일곱': '7',
           '여덟': '8', '아홉': '9', '열': '10'}
_COUNTER = re.compile(r'(?:개|명|번|년|월|시|분|초|원|층|호|차|회|살|번째|점|프로|퍼센트|쪽|곳|대|장|권|잔|병|알|마리|교시|km|kg|%)')


def _sino_value(s):
    digits = {'영': 0, '공': 0, '일': 1, '이': 2, '삼': 3, '사': 4, '오': 5, '육': 6, '칠': 7, '팔': 8, '구': 9}
    if s and all(c in digits for c in s):
        return ''.join(str(digits[c]) for c in s)
    units = {'십': 10, '백': 100, '천': 1000}
    total = section = num = 0
    for c in s:
        if c in digits:
            num = digits[c]
        elif c in units:
            section += (num or 1) * units[c]
            num = 0
        elif c == '만':
            section += num
            total += (section or 1) * 10000
            section = num = 0
    return str(total + section + num)


def numeric_tokens(text):
    """Return (value tokens, uncertain Korean numeral snippets).

    Sino syllables become numbers only in runs of at least two or next to a
    counter/decimal marker/digit. Isolated numeral-shaped words remain uncertain.
    """
    t = str(text or '')
    toks, uncertain = [], []
    stripped = t.strip()
    # A transcript consisting solely of a Korean cardinal is unambiguous as a
    # number. This is generic numeral parsing, not a list of transcript cases.
    if stripped and re.fullmatch(r'[영공일이삼사오육칠팔구십백천만]+', stripped):
        return [_sino_value(stripped)], []
    decimal_only = re.fullmatch(r'점\s*([영공일이삼사오육칠팔구십백천만]+)', stripped)
    if decimal_only:
        return ['.' + _sino_value(decimal_only.group(1))], []
    occupied = []
    for m in re.finditer(r'(?<![0-9])(?:\d+(?:[,.]\d+)*|[,.]\d+)(?![0-9])', t):
        val = m.group().replace(',', '')
        toks.append((m.start(), m.end(), val))
        occupied.append((m.start(), m.end()))
    # Korean decimal phrases, including 점 오 -> .5
    for m in re.finditer(r'((?:[영공일이삼사오육칠팔구십백천만]+|\d+(?:[,.]\d+)*))\s*점\s*([영공일이삼사오육칠팔구십백천만]+)', t):
        if any(a < m.end() and b > m.start() for a,b in occupied): continue
        whole = (_sino_value(m.group(1)) if not m.group(1)[0].isdigit() else m.group(1).replace(',', '')) + '.' + _sino_value(m.group(2))
        toks.append((m.start(), m.end(), whole))
        occupied.append((m.start(),m.end()))
    # ASR may spell a decimal as “11점 오점” (the second 점 is a
    # duplicated spoken punctuation token). Treat it as one decimal value.
    for m in re.finditer(r'(\d+)\s*점\s*([영공일이삼사오육칠팔구십백천만]+)점', t):
        toks = [x for x in toks if not (x[0] < m.end() and x[1] > m.start())]
        toks.append((m.start(), m.end(), m.group(1) + '.' + _sino_value(m.group(2))))
        occupied.append((m.start(), m.end()))
    # Sino and native forms.
    pat = re.compile(r'[영공일이삼사오육칠팔구십백천만]+|하나|한|둘|두|셋|세|넷|네|다섯|여섯|일곱|여덟|아홉|열')
    for m in pat.finditer(t):
        if any(a < m.end() and b > m.start() for a,b in occupied): continue
        s=m.group(); left=t[:m.start()]; right=t[m.end():]
        around = left[-1:] + right[:1]
        right_trimmed = right.lstrip()
        # Korean numeral syllables are numeric only at a lexical boundary, next
        # to an explicit counter, or before common number particles. This keeps
        # words such as 삼성, 사과, 일론, and 일일이 out of the numeric stream.
        left_boundary = not left or not ('가' <= left[-1] <= '힣')
        suffix = re.match(r'(?:라는|라고|로|은|는|이|가|을|를|에|의|만|도|씩|째)', right_trimmed)
        right_boundary = not right_trimmed or not ('가' <= right_trimmed[0] <= '힣')
        adjacent = bool(_COUNTER.match(right_trimmed) or
                        (left and left[-1].isdigit()) or (right and right[0].isdigit()) or
                        '점' in around)
        if s in _NATIVE:
            if adjacent or s in ('다섯','여섯','일곱','여덟','아홉','열'):
                toks.append((m.start(),m.end(),_NATIVE[s]))
            else: uncertain.append((m.start(),m.end(),s))
            continue
        # Multi-syllable cardinal sequences are usually explicit Korean numbers.
        if adjacent or (left_boundary and (right_boundary or suffix) and
                        (len(s) >= 2 or s not in _SINO)):
            toks.append((m.start(),m.end(),_sino_value(s)))
        else:
            uncertain.append((m.start(),m.end(),s))
    toks.sort()
    return [x[2] for x in toks], uncertain


def _script(text):
    counts = {'latin': 0, 'hangul': 0, 'cyrillic': 0, 'arabic': 0, 'devanagari': 0}
    for ch in text:
        n=ord(ch)
        if n < 128 and ch.isalpha(): counts['latin']+=1
        elif 0xAC00 <= n <= 0xD7AF: counts['hangul']+=1
        elif 0x0400 <= n <= 0x052F: counts['cyrillic']+=1
        elif 0x0600 <= n <= 0x06FF: counts['arabic']+=1
        elif 0x0900 <= n <= 0x097F: counts['devanagari']+=1
    return max(counts,key=counts.get) if max(counts.values(),default=0) else 'none'


def _same_text_except_numbers(a, b):
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    numeric_chars = set('영공일이삼사오육칠팔구십백천만0123456789.,%')
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal' and (set(a[i1:i2] + b[j1:j2]) - numeric_chars):
            return False
    return True


def _has_korean_counter_value_change(a, b):
    numeral_chars = set('영공일이삼사오육칠팔구십백천만')
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        old, new = a[i1:i2], b[j1:j2]
        if not old or not new or (set(old + new) - numeral_chars):
            continue
        before = a[max(0, i1-8):min(len(a), i2+12)]
        after = b[max(0, j1-8):min(len(b), j2+12)]
        if _COUNTER.search(before) or _COUNTER.search(after):
            return True
    return False


def assess_faithfulness(raw, corrected):
    a,b=str(raw or ''),str(corrected or '')
    ratio=len(b)/max(1,len(a))
    similarity=difflib.SequenceMatcher(None,a.casefold(),b.casefold(),autojunk=False).ratio()
    reasons=[]; flags=[]
    if not a.strip() or not b.strip(): reasons.append('empty')
    if not MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO: reasons.append('length-ratio')
    if similarity < MIN_SIMILARITY: reasons.append('character-similarity')
    sa,sb=_script(a),_script(b)
    if sa!='none' and sb!='none' and sa!=sb: reasons.append('script-mismatch')
    na,ua=numeric_tokens(a); nb,ub=numeric_tokens(b)
    # Ignore script/similarity/length penalties only when replacing explicit
    # numeral spans leaves the same surrounding text. Numeric equality alone
    # does not excuse unrelated edits elsewhere in the transcript.
    if na and na == nb and _same_text_except_numbers(a, b):
        reasons=[r for r in reasons if r not in ('length-ratio','character-similarity','script-mismatch')]
    # Korean single-syllable words and homophones can remain uncertain. A
    # changed value is hard-rejected when it is isolated to numeric spans and
    # the spans are anchored by counters or explicit number syntax. This also
    # catches Korean-only counter values such as 삼 개 -> 사 개 and 삼층 -> 사층.
    ascii_values_a = re.findall(r'(?<![0-9])(?:\d+(?:[,.]\d+)*|[,.]\d+)(?![0-9])', a)
    ascii_values_b = re.findall(r'(?<![0-9])(?:\d+(?:[,.]\d+)*|[,.]\d+)(?![0-9])', b)
    explicit_digit_change = (ascii_values_a and ascii_values_b and ascii_values_a != ascii_values_b) or (
        bool(ascii_values_a) != bool(ascii_values_b) and len(na) != len(nb))
    korean_counter_change = _has_korean_counter_value_change(a, b)
    if na != nb and (explicit_digit_change or korean_counter_change):
        reasons.append('numeric-value-change')
    if ua or ub: flags.append('numeric-ambiguous')
    # Proper-name changes are review flags, never automatic rejection.
    # Hangul/Latin word substitutions are a conservative proxy for name candidates.
    aw=set(re.findall(r'[가-힣A-Za-z]{2,}',a)); bw=set(re.findall(r'[가-힣A-Za-z]{2,}',b))
    if aw != bw and (aw or bw): flags.append('proper-noun-change')
    flags=list(dict.fromkeys(flags))
    for flag in flags: _LOG.warning('faithfulness soft flag: %s',flag)
    return {'accepted': not reasons, 'reasons': reasons, 'flags': flags,
            'length_ratio': ratio, 'similarity': similarity}


def is_faithful(raw, corrected):
    return assess_faithfulness(raw,corrected)['accepted']
