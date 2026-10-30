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
SCRATCH = Path(__file__).resolve().parent
REPORT_JSON = SCRATCH / 'backfill_report.json'
REPORT_TABLE = SCRATCH / 'backfill_report.tsv'


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


def match_candidate(place_name, place_address, candidates):
    name_key = pipeline.place_name_key(place_name)
    addr_tokens = pipeline.place_address_tokens(place_address)
    exact = []
    for cand_name, cand_addr, cand_cat in candidates:
        if pipeline.place_name_key(cand_name) == name_key:
            exact.append((cand_name, cand_addr, cand_cat))
    if not exact:
        return None
    if len(exact) == 1:
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
    parser.add_argument('--sleep', type=float, default=1.5, help='seconds between AI calls')
    args = parser.parse_args()

    config = app.load_env()
    if not pipeline.ai_available(config):
        print('AI provider not available per ai_available(); aborting.', file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row

    places = db.execute('SELECT id,name,address,category,dedupe_hash FROM places ORDER BY created_at').fetchall()
    job_for_place = {}
    place_for_job = {}
    for row in db.execute('SELECT place_id, job_id FROM place_sources'):
        job_for_place.setdefault(row['place_id'], []).append(row['job_id'])
        place_for_job.setdefault(row['job_id'], []).append(row['place_id'])
    place_by_id = {p['id']: p for p in places}

    needed_jobs = sorted({jid for ids in job_for_place.values() for jid in ids})
    print('{} places, {} unique source jobs to query'.format(len(places), len(needed_jobs)))

    # per-job: list of (candidates, attempt_no) accumulated across bounded retry attempts.
    # Single-call attribution: a place's final category always comes from exactly one
    # attempt's response (first attempt in which it is matched), never merged across
    # attempts, so every assigned category is traceable to one real AI response -- the
    # retries exist only to counter pure sampling variance on large place_list jobs
    # dropping/renaming some entries, not to broaden matching leniency.
    MAX_ATTEMPTS = 3
    job_attempts_used = {}
    job_match_source = {}  # place_id -> (jid, attempt_no, candidate_name)

    for i, jid in enumerate(needed_jobs, 1):
        job = db.execute('SELECT id,source_title,extracted_caption,text,url FROM jobs WHERE id=?', (jid,)).fetchone()
        arow = db.execute('SELECT ocr_text,transcript FROM analyses WHERE job_id=?', (jid,)).fetchone()
        target_pids = place_for_job.get(jid, [])
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
            for pid in list(unmatched):
                place = place_by_id[pid]
                match = match_candidate(place['name'], place['address'], candidates)
                if not match and len(target_pids) == 1 and len(candidates) == 1:
                    # Zero-ambiguity shortcut: this job sources exactly one place in our
                    # DB and the AI returned exactly one candidate for it in this same
                    # response, so a formatting-only name mismatch (e.g. parenthetical
                    # alt-name dropped between runs) cannot misattribute to a different
                    # venue -- there is no other candidate or target it could be.
                    match = candidates[0]
                if match and match[2]:
                    job_match_source[pid] = (jid, attempt, match[0], match[2])
                    unmatched.discard(pid)
            time.sleep(args.sleep)
        job_attempts_used[jid] = (attempt, last_err)

    results = []
    for place in places:
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

    print('\n=== SUMMARY ===')
    print('total places:', len(results))
    print('changed:', len(changed))
    print('unchanged (AI returned same value):', len(unchanged_same))
    print('fallback (no usable AI category, kept old value):', len(fallback))

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
            # Re-check current DB value right before writing (non-destructive rule: never
            # clobber if something else already updated it to a non-stale value meanwhile).
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
