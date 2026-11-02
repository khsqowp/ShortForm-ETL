#!/usr/bin/env python3
"""Targeted re-run of the 65-row place-category backfill fallback, for exactly the
8 source jobs identified as Instagram image-carousel posts (67f94e43052c,
626d09effc7e, 499496d5a5af, 8056a38bed70, e7173744c1d7, 2532d6537253,
b871fe6ffd59, 39cb17d1a829).

Why this script exists instead of re-running scripts/backfill_place_categories.py:
that script calls pipeline.request_analysis(config, raw_text, [], [], transcript, ocr)
-- i.e. image_paths=[] always -- so for these 8 image-post jobs the AI was only ever
shown the already-stored (and, for at least job 626d09effc7e, badly garbled)
tesseract OCR text, never the actual carousel photos. Those photos are still on disk
under data/media/ (no re-download needed; these are photo posts, not videos --
confirmed via jobs.media_type and the absence of any video file in each job's
media/sources/<job id>/ directory) and in several cases show the place name and
address directly as a clean text overlay. This script re-runs request_analysis for
only these 8 jobs with the real images attached as image_paths (mirroring how
analyze_job() already does it for ordinary image-post jobs), then applies the exact
same match_candidate()/extract_place_candidates() address-cross-check logic as
scripts/backfill_place_categories.py (imported, not reimplemented) to decide
whether a category can be assigned. No other place, job, or column is touched.

Usage:
  python3 vision_backfill_8jobs.py --dry-run
  python3 vision_backfill_8jobs.py --apply
"""
import argparse
import json
import sys
import time
import sqlite3
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/home/opc/My_Archive/TELEGRAM_KHSQOWP_LinkCollectbot')
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))

import linkcollect_pipeline as pipeline  # noqa: E402
import linkcollect_app as app  # noqa: E402
import backfill_place_categories as backfill  # noqa: E402  (reused matching logic, unmodified)

DB_PATH = REPO / 'data' / 'linkcollect.sqlite3'
DATA_DIR = REPO / 'data'
SCRATCH = Path('/home/opc/My_Archive/backfill_scratch')
REPORT_JSON = SCRATCH / 'vision_backfill_report.json'
REPORT_TABLE = SCRATCH / 'vision_backfill_report.tsv'

TARGET_JOBS = [
    '67f94e43052c', '626d09effc7e', '499496d5a5af', '8056a38bed70',
    'e7173744c1d7', '2532d6537253', 'b871fe6ffd59', '39cb17d1a829',
]

MAX_ATTEMPTS = 6


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def call_ai_vision(config, raw_text, image_paths, transcript, ocr, retries=4):
    delay = 5
    for attempt in range(retries):
        try:
            return pipeline.request_analysis(config, raw_text, image_paths, [], transcript, ocr), None
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
    parser.add_argument('--sleep', type=float, default=1.5)
    args = parser.parse_args()

    config = app.load_env()
    if not pipeline.ai_available(config):
        print('AI provider not available per ai_available(); aborting.', file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row

    # All places sourced by any of the 8 target jobs (not just unresolved ones) --
    # needed as matching context, exactly like backfill_place_categories.main()'s
    # target_pids_full, so an already-categorized place's exact-name match correctly
    # claims its candidate instead of leaking into an unresolved row's match.
    qmarks = ','.join('?' * len(TARGET_JOBS))
    place_rows = db.execute(
        'SELECT DISTINCT p.id,p.name,p.address,p.category FROM places p '
        'JOIN place_sources ps ON ps.place_id=p.id '
        'WHERE ps.job_id IN (%s)' % qmarks, TARGET_JOBS
    ).fetchall()
    place_by_id = {p['id']: p for p in place_rows}

    place_for_job = {}
    job_for_place = {}
    for row in db.execute('SELECT place_id, job_id FROM place_sources WHERE job_id IN (%s)' % qmarks, TARGET_JOBS):
        place_for_job.setdefault(row['job_id'], []).append(row['place_id'])
        job_for_place.setdefault(row['place_id'], []).append(row['job_id'])

    stale_categories = {'', 'place'}
    work_place_ids = {pid for pid, p in place_by_id.items() if p['category'] in stale_categories}
    already_set_ids = {pid for pid in place_by_id if pid not in work_place_ids}
    print('{} places sourced by the 8 target jobs; {} still unresolved (category in {{"", "place"}})'.format(
        len(place_by_id), len(work_place_ids)))

    job_attempts_used = {}
    job_match_source = {}  # place_id -> (jid, attempt_no, candidate_name, candidate_category)

    for i, jid in enumerate(TARGET_JOBS, 1):
        job = db.execute('SELECT id,source_title,extracted_caption,text,url,carousel_media_paths,media_path FROM jobs WHERE id=?', (jid,)).fetchone()
        arow = db.execute('SELECT ocr_text,transcript FROM analyses WHERE job_id=?', (jid,)).fetchone()
        target_pids_full = place_for_job.get(jid, [])
        target_pids = [pid for pid in target_pids_full if pid in work_place_ids]
        if not job:
            job_attempts_used[jid] = (0, 'job row missing')
            continue

        carousel_paths = job['carousel_media_paths']
        if isinstance(carousel_paths, str) and carousel_paths:
            try:
                carousel_paths = json.loads(carousel_paths)
            except ValueError:
                carousel_paths = []
        if not carousel_paths:
            carousel_paths = [job['media_path']] if job['media_path'] else []
        image_paths = [DATA_DIR / p for p in carousel_paths]
        # No cap to 10 here (unlike analyze_job's production cap): this is a one-off
        # forensic re-run specifically to maximize recall of the carousel images'
        # title-band text, and the request_analysis() call itself has no image-count
        # cap (only video_frames is capped to 8 inside that function).
        image_paths = [p for p in image_paths if pipeline.image_content(p)]

        raw_text = backfill.build_raw_text(job)
        ocr = str(arow['ocr_text'] or '') if arow else ''
        transcript = str(arow['transcript'] or '') if arow else ''

        if not image_paths:
            job_attempts_used[jid] = (0, 'no usable images found on disk for this job')
            continue

        print('[{}/{}] job {}: {} target place(s), {} carousel image(s) attached as vision input'.format(
            i, len(TARGET_JOBS), jid, len(target_pids), len(image_paths)))

        unmatched = {pid for pid in target_pids if pid not in job_match_source}
        attempt = 0
        last_err = None
        while unmatched and attempt < MAX_ATTEMPTS:
            attempt += 1
            print('  attempt {}/{} ({} place(s) still unmatched)'.format(attempt, MAX_ATTEMPTS, len(unmatched)))
            analysis, err = call_ai_vision(config, raw_text, image_paths, transcript, ocr)
            last_err = err
            if err:
                print('  ERROR: {}'.format(err), file=sys.stderr)
                time.sleep(args.sleep)
                continue
            candidates = backfill.extract_place_candidates(analysis)

            exact_match_for_pid = {}
            for pid in target_pids_full:
                if pid in job_match_source:
                    continue
                place = place_by_id[pid]
                match = backfill.match_candidate(place['name'], place['address'], candidates)
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
                exact_match_for_pid[leftover_targets[0]] = leftover_candidates[0]

            for pid in list(unmatched):
                match = exact_match_for_pid.get(pid)
                if match and match[2]:
                    job_match_source[pid] = (jid, attempt, match[0], match[2])
                    unmatched.discard(pid)
            time.sleep(args.sleep)
        job_attempts_used[jid] = (attempt, last_err)

    results = []
    for pid in already_set_ids:
        place = place_by_id[pid]
        results.append({
            'place_id': pid, 'name': place['name'], 'address': place['address'],
            'old_category': place['category'], 'new_category': place['category'],
            'used_job': '', 'source_jobs': job_for_place.get(pid, []),
            'action': 'skipped_already_set', 'note': 'existing non-stale category preserved; AI query skipped',
        })
    for pid in work_place_ids:
        place = place_by_id[pid]
        name, address, old_category = place['name'], place['address'], place['category']
        jids = job_for_place.get(pid, [])
        found = job_match_source.get(pid)
        new_category = ''
        used_job = ''
        note = ''
        if found:
            used_job, attempt_no, matched_name, new_category = found
            note = 'VISION match "{}" on attempt {}/{} (images attached)'.format(matched_name, attempt_no, MAX_ATTEMPTS)
        else:
            notes = []
            for jid in jids:
                attempts_used, err = job_attempts_used.get(jid, (0, 'no analysis'))
                if err:
                    notes.append('job {}: {}'.format(jid, err))
                else:
                    notes.append('job {}: no name match in {} vision attempt(s)'.format(jid, attempts_used))
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
    fallback = [r for r in results if r['action'] == 'fallback_kept_existing']
    skipped_already_set = [r for r in results if r['action'] == 'skipped_already_set']

    print('\n=== SUMMARY (places sourced by the 8 target jobs only) ===')
    print('total places in scope:', len(results))
    print('changed (newly resolved via vision):', len(changed))
    print('still fallback:', len(fallback))
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
            # Compare-and-set protects a category changed by the live service while
            # this one-off AI pass was running. Only overwrite the stale value that
            # was read when the run started; never replace a newly resolved category.
            cur = db.execute(
                'UPDATE places SET category=?,updated_at=? '
                'WHERE id=? AND category=? AND category IN (?,?)',
                (r['new_category'], utc_now(), r['place_id'], r['old_category'], '', 'place'),
            )
            db.commit()
            applied += cur.rowcount
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
