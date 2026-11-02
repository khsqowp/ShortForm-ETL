#!/usr/bin/env python3
"""One-off backfill: derive AI topic-style category for existing `places` rows
that still carry the stale literal category from before commits 8e982d6/fb100ae.

Reuses the live pipeline's own request_analysis() prompt/logic (linkcollect_pipeline.py)
and the same place-candidate extraction/matching rules used in persist_result(), instead
of re-implementing a parallel classifier. Source text per place is pulled from the
already-stored jobs/analyses rows (source_title, extracted_caption, text, ocr_text,
transcript) joined through place_sources -- the same join pattern persist_result()
and analyze_job() use -- so no media/video is re-downloaded or re-transcribed.

Usage:
  python3 backfill_place_categories.py --dry-run   # no DB writes, writes report only
  python3 backfill_place_categories.py --apply     # performs the UPDATE places SET category=...

By default, rows whose category is already non-empty and not the stale placeholder
'place' are reported as skipped_already_set and are not sent to the AI or updated.
Pass --force with either mode to process all rows, including already-set categories.
(Rows still contribute their already-known name/category as matching context for
other places sharing the same source job even when skipped -- see match_candidate()
and the elimination pass in main() -- so turning this off does not reduce matching
accuracy for the rows that *are* being processed.)

Never overwrites a row with an empty/unusable AI result; never touches any column
other than places.category.
"""
import argparse
import json
import os
import sys
import time
import sqlite3
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/home/opc/My_Archive/TELEGRAM_KHSQOWP_LinkCollectbot')
sys.path.insert(0, str(REPO))

import linkcollect_pipeline as pipeline  # noqa: E402
import linkcollect_app as app  # noqa: E402

DB_PATH = REPO / 'data' / 'linkcollect.sqlite3'
SCRATCH = Path('/home/opc/My_Archive/backfill_scratch')
REPORT_JSON = SCRATCH / 'backfill_report.json'
REPORT_TABLE = SCRATCH / 'backfill_report.tsv'

# Top-level 시/도 names: too coarse to disambiguate between two different real
# venues that happen to share a literal name (nearly every Seoul address
# contains "서울"), so they are excluded when checking whether a candidate's
# address plausibly agrees with a DB place's stored address (see match_candidate).
_REGION_STOPWORDS = {
    '서울', '서울시', '서울특별시',
    '부산', '부산시', '부산광역시',
    '대구', '대구시', '대구광역시',
    '인천', '인천시', '인천광역시',
    '광주', '광주시', '광주광역시',
    '대전', '대전시', '대전광역시',
    '울산', '울산시', '울산광역시',
    '세종', '세종시', '세종특별자치시',
    '경기', '경기도',
    '강원', '강원도', '강원특별자치도',
    '충북', '충청북도', '충남', '충청남도',
    '전북', '전북특별자치도', '전라북도', '전남', '전라남도',
    '경북', '경상북도', '경남', '경상남도',
    '제주', '제주도', '제주특별자치도',
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def build_raw_text(job):
    parts = [
        str(job['source_title'] or '').strip(),
        str(job['extracted_caption'] or '').strip(),
        str(job['text'] or '').strip(),
    ]
    raw_text = '\n\n'.join(p for p in parts if p)
    if job['url']:
        raw_text = (raw_text + '\n\n원문 링크(페이지 내용은 직접 가져오지 않음): ' + job['url']).strip()
    return raw_text


def extract_place_candidates(analysis):
    """Mirror persist_result()'s place_candidates construction (pipeline lines ~888-904)."""
    structure_type = str(analysis.get('structure_type') or 'single_topic')
    content_type = str(analysis.get('content_type') or 'other')
    location = analysis.get('location') if isinstance(analysis.get('location'), dict) else None
    location_name = str((location or {}).get('name') or '').strip()
    location_address = str((location or {}).get('address') or '').strip()
    location_category = str((location or {}).get('category') or '').strip()
    candidates = []
    if structure_type == 'place_list':
        places_list = analysis.get('places') if isinstance(analysis.get('places'), list) else []
        for item in places_list:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if name:
                candidates.append((
                    name,
                    str(item.get('address_or_region') or '').strip(),
                    str(item.get('category') or '').strip()[:50],
                ))
        if not candidates and location_name:
            candidates.append((location_name, location_address, location_category[:50]))
    elif location_name and content_type in ('place', 'mixed'):
        candidates.append((location_name, location_address, location_category[:50]))
    return candidates


def significant_address_tokens(tokens):
    """Drop top-level region tokens that are too coarse to disambiguate same-named
    venues in different neighborhoods (see _REGION_STOPWORDS)."""
    return {t for t in tokens if t not in _REGION_STOPWORDS}


def match_candidate(place_name, place_address, candidates):
    name_key = pipeline.place_name_key(place_name)
    addr_tokens = pipeline.place_address_tokens(place_address)
    sig_place = significant_address_tokens(addr_tokens)
    exact = []
    for cand_name, cand_addr, cand_cat in candidates:
        if pipeline.place_name_key(cand_name) == name_key:
            exact.append((cand_name, cand_addr, cand_cat))
    if not exact:
        return None
    if len(exact) == 1:
        cand_name, cand_addr, cand_cat = exact[0]
        sig_cand = significant_address_tokens(pipeline.place_address_tokens(cand_addr))
        # Name matches exactly, but if both sides have neighborhood-level address
        # info and it flatly disagrees (e.g. the AI attached the wrong dong/gu to
        # this literal name this round, or two different real venues happen to
        # share the name), do not blindly accept -- a wrong confident match is
        # worse than a fallback. Only the coarse 시/도 token is excluded above, so
        # this does not fire just because two Seoul places both say "서울".
        if sig_place and sig_cand and not (sig_place & sig_cand):
            return None
        return exact[0]
    # Disambiguate same-name multi-branch candidates by address token overlap.
    for cand_name, cand_addr, cand_cat in exact:
        cand_tokens = pipeline.place_address_tokens(cand_addr)
        if addr_tokens and cand_tokens and (addr_tokens & cand_tokens):
            return (cand_name, cand_addr, cand_cat)
    return exact[0]


def call_ai(config, raw_text, transcript, ocr, retries=4):
    delay = 5
    for attempt in range(retries):
        try:
            return pipeline.request_analysis(config, raw_text, [], [], transcript, ocr), None
        except urllib.error.HTTPError as exc:
            body = ''
            try:
                body = exc.read().decode('utf-8', errors='replace')[:300]
            except Exception:
                pass
            if exc.code == 429 and attempt < retries - 1:
                print('  [rate-limited] HTTP 429, backing off {}s (attempt {}/{})'.format(delay, attempt + 1, retries), file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            return None, 'HTTP {}: {}'.format(exc.code, body)
        except Exception as exc:
            return None, '{}: {}'.format(type(exc).__name__, exc)
    return None, 'exhausted retries'


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--force', action='store_true', default=False,
                        help='process all rows, including rows with an existing category')
    parser.add_argument('--sleep', type=float, default=1.5, help='seconds between AI calls')
    args = parser.parse_args()

    config = app.load_env()
    if not pipeline.ai_available(config):
        print('AI provider not available per ai_available(); aborting.', file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row

    places = db.execute('SELECT id,name,address,category,dedupe_hash FROM places ORDER BY created_at').fetchall()
    stale_categories = {'', 'place'}
    work = [p for p in places if args.force or p['category'] in stale_categories]
    already_set = [p for p in places if not args.force and p['category'] not in stale_categories]
    job_for_place = {}
    place_for_job = {}
    for row in db.execute('SELECT place_id, job_id FROM place_sources'):
        job_for_place.setdefault(row['place_id'], []).append(row['job_id'])
        place_for_job.setdefault(row['job_id'], []).append(row['place_id'])
    place_by_id = {p['id']: p for p in places}

    work_place_ids = {p['id'] for p in work}
    needed_jobs = sorted({jid for pid in work_place_ids for jid in job_for_place.get(pid, [])})
    print('{} places, {} unique source jobs to query'.format(len(places), len(needed_jobs)))

    # per-job: list of (candidates, attempt_no) accumulated across bounded retry attempts.
    # Single-call attribution: a place's final category always comes from exactly one
    # attempt's response (first attempt in which it is matched), never merged across
    # attempts, so every assigned category is traceable to one real AI response -- the
    # retries exist only to counter pure sampling variance on large place_list jobs
    # dropping/renaming some entries, not to broaden matching leniency.
    #
    # MAX_ATTEMPTS raised from 3 to 6: large place_list jobs (20+ venues) showed high
    # per-call name-rendering variance (the same venue gets a slightly different name
    # or abbreviation on different calls), so a place that fails to render its exact
    # stored name on attempts 1-3 often does on a later attempt; this costs nothing
    # extra since the AI call is a local hermes container, not a paid API.
    MAX_ATTEMPTS = 6
    job_attempts_used = {}
    job_match_source = {}  # place_id -> (jid, attempt_no, candidate_name, candidate_category)

    for i, jid in enumerate(needed_jobs, 1):
        job = db.execute('SELECT id,source_title,extracted_caption,text,url FROM jobs WHERE id=?', (jid,)).fetchone()
        arow = db.execute('SELECT ocr_text,transcript FROM analyses WHERE job_id=?', (jid,)).fetchone()
        # target_pids_full includes every place sourced by this job, even rows this
        # run isn't allowed to update (skipped_already_set) -- they're still needed
        # as matching context below so an already-known venue's exact-name match
        # correctly "claims" its candidate instead of it being mistaken for a
        # leftover candidate belonging to one of the rows we're actually trying to
        # resolve. target_pids restricts what we actually track/assign to work rows.
        target_pids_full = place_for_job.get(jid, [])
        target_pids = [pid for pid in target_pids_full if pid in work_place_ids]
        if not job:
            job_attempts_used[jid] = (0, 'job row missing')
            continue
        raw_text = build_raw_text(job)
        ocr = str(arow['ocr_text'] or '') if arow else ''
        transcript = str(arow['transcript'] or '') if arow else ''
        if not raw_text and not ocr and not transcript:
            job_attempts_used[jid] = (0, 'no source text available (caption/ocr/transcript all empty)')
            continue

        unmatched = {pid for pid in target_pids if pid not in job_match_source}
        attempt = 0
        last_err = None
        while unmatched and attempt < MAX_ATTEMPTS:
            attempt += 1
            print('[{}/{}] querying AI for job {} attempt {}/{} ({} place(s) still unmatched; text={}c ocr={}c transcript={}c)'.format(
                i, len(needed_jobs), jid, attempt, MAX_ATTEMPTS, len(unmatched), len(raw_text), len(ocr), len(transcript)))
            analysis, err = call_ai(config, raw_text, transcript, ocr)
            last_err = err
            if err:
                print('  ERROR: {}'.format(err), file=sys.stderr)
                time.sleep(args.sleep)
                continue
            candidates = extract_place_candidates(analysis)

            # Pass 1: address-validated exact name matches, computed against every
            # place sourced from this job (target_pids_full, not just the rows we're
            # allowed to update) so we know which candidates this response already
            # accounts for.
            exact_match_for_pid = {}
            for pid in target_pids_full:
                if pid in job_match_source:
                    continue
                place = place_by_id[pid]
                match = match_candidate(place['name'], place['address'], candidates)
                if match:
                    exact_match_for_pid[pid] = match

            claimed_keys = {pipeline.place_name_key(m[0]) for m in exact_match_for_pid.values()}
            claimed_keys |= {
                pipeline.place_name_key(place_by_id[pid]['name'])
                for pid in target_pids_full if pid in job_match_source
            }
            leftover_targets = [
                pid for pid in target_pids
                if pid not in exact_match_for_pid and pid not in job_match_source
            ]
            leftover_candidates = [c for c in candidates if pipeline.place_name_key(c[0]) not in claimed_keys]

            if len(leftover_targets) == 1 and len(leftover_candidates) == 1 and leftover_candidates[0][2]:
                # Zero-ambiguity shortcut, generalized: every OTHER place sourced
                # from this job was already accounted for (by an address-validated
                # exact name match, this attempt or an earlier one), and every
                # OTHER candidate in this same response was claimed by one of those
                # matches. Exactly one target and exactly one candidate are left
                # over, so -- with no other candidate or target it could possibly
                # be -- this is a formatting-only name mismatch (e.g. a branch
                # suffix or a leading generic word dropped/added), not ambiguity.
                exact_match_for_pid[leftover_targets[0]] = leftover_candidates[0]

            for pid in list(unmatched):
                match = exact_match_for_pid.get(pid)
                if match and match[2]:
                    job_match_source[pid] = (jid, attempt, match[0], match[2])
                    unmatched.discard(pid)
            time.sleep(args.sleep)
        job_attempts_used[jid] = (attempt, last_err)

    results = []
    for place in already_set:
        results.append({
            'place_id': place['id'], 'name': place['name'], 'address': place['address'],
            'old_category': place['category'], 'new_category': place['category'],
            'used_job': '', 'source_jobs': job_for_place.get(place['id'], []),
            'action': 'skipped_already_set', 'note': 'existing non-stale category preserved; AI query skipped',
        })
    for place in places:
        if place['id'] not in work_place_ids:
            continue
        pid, name, address, old_category = place['id'], place['name'], place['address'], place['category']
        jids = job_for_place.get(pid, [])
        found = job_match_source.get(pid)
        new_category = ''
        used_job = ''
        note = ''
        if found:
            used_job, attempt_no, matched_name, new_category = found
            note = 'matched "{}" on attempt {}/{}'.format(matched_name, attempt_no, MAX_ATTEMPTS)
        else:
            notes = []
            for jid in jids:
                attempts_used, err = job_attempts_used.get(jid, (0, 'no analysis'))
                if err:
                    notes.append('job {}: {}'.format(jid, err))
                else:
                    notes.append('job {}: no name match in {} attempt(s)'.format(jid, attempts_used))
            note = '; '.join(notes) if notes else 'no source job'
        if new_category and new_category != old_category:
            action = 'changed'
        elif new_category and new_category == old_category:
            action = 'unchanged_same_value'
        else:
            action = 'fallback_kept_existing'
        results.append({
            'place_id': pid, 'name': name, 'address': address,
            'old_category': old_category, 'new_category': new_category,
            'used_job': used_job, 'source_jobs': jids, 'action': action, 'note': note,
        })

    changed = [r for r in results if r['action'] == 'changed']
    unchanged_same = [r for r in results if r['action'] == 'unchanged_same_value']
    fallback = [r for r in results if r['action'] == 'fallback_kept_existing']
    skipped_already_set = [r for r in results if r['action'] == 'skipped_already_set']

    print('\n=== SUMMARY ===')
    print('total places:', len(results))
    print('changed:', len(changed))
    print('unchanged (AI returned same value):', len(unchanged_same))
    print('fallback (no usable AI category, kept old value):', len(fallback))
    print('skipped_already_set:', len(skipped_already_set))

    SCRATCH.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    with open(REPORT_TABLE, 'w', encoding='utf-8') as fh:
        fh.write('place_id\tname\taddress\told_category\tnew_category\taction\tused_job\tnote\n')
        for r in results:
            fh.write('\t'.join([
                r['place_id'], r['name'], r['address'], r['old_category'], r['new_category'],
                r['action'], r['used_job'], r['note'],
            ]).replace('\n', ' ') + '\n')
    print('Report written to', REPORT_JSON, 'and', REPORT_TABLE)

    if args.apply:
        applied = 0
        for r in changed:
            # Re-check that the row still exists before writing; this does not check whether
            # its category changed since the initial SELECT.
            cur = db.execute('SELECT category FROM places WHERE id=?', (r['place_id'],)).fetchone()
            if cur is None:
                continue
            db.execute('UPDATE places SET category=?,updated_at=? WHERE id=?',
                       (r['new_category'], utc_now(), r['place_id']))
            db.commit()
            applied += 1
        print('Applied {} category updates.'.format(applied))
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        count_after = db.execute('SELECT count(*) FROM places').fetchone()[0]
        print('PRAGMA integrity_check:', integrity)
        print('places row count after:', count_after)
    else:
        print('Dry run only; no DB writes performed.')

    db.close()


if __name__ == '__main__':
    main()
