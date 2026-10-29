#!/usr/bin/env python3
"""Private Telegram and web intake for LinkCollect."""
import cgi
import json
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import linkcollect_pipeline as pipeline

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LINKCOLLECT_DATA_DIR', ROOT / 'data'))
MEDIA = DATA / 'media'
ENV_FILE = Path(os.environ.get('LINKCOLLECT_ENV_FILE', '/home/opc/My_Archive/.env'))
HOST = '127.0.0.1'
PORT = int(os.environ.get('LINKCOLLECT_PORT', '18787'))
MAX_UPLOAD = 128 * 1024 * 1024
DB_PATH = DATA / 'linkcollect.sqlite3'
LOG = logging.getLogger('linkcollect')


def load_env():
    result = {}
    try:
        lines = ENV_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return result
    for line in lines:
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, v = s.split('=', 1)
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def db_conn():
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    os.chmod(MEDIA, 0o700)
    db = sqlite3.connect(DB_PATH, timeout=20)
    os.chmod(DB_PATH, 0o600)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
      text TEXT NOT NULL DEFAULT '', media_path TEXT NOT NULL DEFAULT '',
      media_name TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'received', created_at TEXT NOT NULL,
      analysis TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
      telegram_file_id TEXT NOT NULL DEFAULT '')''')
    db.execute('CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, processed_at TEXT NOT NULL)')
    db.commit()
    pipeline.ensure_schema(db)
    return db


def insert_job(db, source, url='', text='', media_path='', media_name='', media_type='', telegram_file_id='', status='received'):
    job_id = uuid.uuid4().hex[:12]
    db.execute('INSERT INTO jobs(id,source,url,text,media_path,media_name,media_type,status,created_at,telegram_file_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (job_id, source, url[:4000], text[:20000], media_path, media_name,
                media_type, status, datetime.now(timezone.utc).isoformat(), telegram_file_id))
    return job_id


def create_job(source, url='', text='', media_path='', media_name='', media_type='', telegram_file_id='', status='received'):
    with db_conn() as db:
        return insert_job(db, source, url, text, media_path, media_name, media_type, telegram_file_id, status)


def jobs_list():
    with db_conn() as db:
        rows = db.execute('''SELECT j.id,j.source,j.url,j.text,j.media_name,j.media_type,j.status,j.created_at,
            COALESCE(a.title,'') AS title,COALESCE(a.category,'') AS category,
            COALESCE(a.summary,j.analysis) AS summary,COALESCE(a.cleaned_content,'') AS cleaned_content,
            COALESCE(a.transcript,'') AS transcript,COALESCE(a.transcript_cleaned,'') AS transcript_cleaned,
            COALESCE(a.stt_corrected_text,'') AS stt_corrected_text,
            COALESCE(a.native_subtitle_text,'') AS native_subtitle_text,COALESCE(a.transcript_segments,'') AS transcript_segments,
            COALESCE(a.ocr_text,'') AS ocr_text,
            COALESCE(j.source_title,'') AS source_title,COALESCE(j.extracted_caption,'') AS extracted_caption,
            COALESCE(a.location_name,'') AS location_name,
            COALESCE(a.location_address,'') AS location_address,COALESCE(a.error,'') AS analysis_error,
            COALESCE((SELECT json_group_array(t.name) FROM job_tags jt JOIN tags t ON t.id=jt.tag_id WHERE jt.job_id=j.id),'[]') AS tag_list
            FROM jobs j LEFT JOIN analyses a ON a.job_id=j.id WHERE j.status != 'analysis_failed' ORDER BY j.created_at DESC LIMIT 100''').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item['transcript_segments'] = json.loads(item.get('transcript_segments') or '[]')
        except (TypeError, ValueError):
            item['transcript_segments'] = []
        try:
            item['tags'] = json.loads(item.pop('tag_list') or '[]')
        except (TypeError, ValueError):
            item['tags'] = []
        result.append(item)
    return result


THUMB_DIR = DATA / 'thumbnails'


def job_media(job_id):
    with db_conn() as db:
        row = db.execute('SELECT media_path, media_type FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row or not row['media_path']:
        return None
    return row['media_path'], row['media_type']


def video_thumbnail_path(job_id, media_path):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMB_DIR / (job_id + '.jpg')
    if thumb_path.exists() and thumb_path.stat().st_size > 0:
        return thumb_path
    source_path = DATA / media_path
    if not source_path.is_file():
        return None
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-ss', '0', '-i', str(source_path), '-frames:v', '1',
             '-vf', 'scale=480:-1', '-q:v', '4', str(thumb_path)],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not thumb_path.exists() or thumb_path.stat().st_size == 0:
        thumb_path.unlink(missing_ok=True)
        return None
    return thumb_path


def linked_archive_sources(db, relation_table, relation_key, archive_id):
    rows = db.execute(f'''SELECT j.id AS job_id,COALESCE(j.url,'') AS url,COALESCE(j.source,'') AS source,
        COALESCE(NULLIF(j.source_title,''),NULLIF(a.title,''),'') AS title,
        COALESCE(NULLIF(j.extracted_caption,''),NULLIF(j.text,''),'') AS caption,
        COALESCE(a.summary,'') AS summary,COALESCE(a.transcript,'') AS transcript,
        COALESCE(a.transcript_cleaned,'') AS transcript_cleaned,COALESCE(a.stt_corrected_text,'') AS stt_corrected_text,COALESCE(a.native_subtitle_text,'') AS native_subtitle_text,COALESCE(a.transcript_segments,'') AS transcript_segments,j.created_at
        FROM {relation_table} s JOIN jobs j ON j.id=s.job_id
        LEFT JOIN analyses a ON a.job_id=j.id
        WHERE s.{relation_key}=? ORDER BY j.created_at DESC LIMIT 50''', (archive_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item['transcript_segments'] = json.loads(item.get('transcript_segments') or '[]')
        except (TypeError, ValueError):
            item['transcript_segments'] = []
        result.append(item)
    return result


def knowledge_list(include_hidden=False):
    with db_conn() as db:
        rows = db.execute('''SELECT k.id,k.title,k.category,k.summary,k.content,k.language,k.created_at,k.tags AS raw_tags,k.hidden,
            COUNT(s.job_id) AS source_count,
            (SELECT s2.job_id FROM knowledge_sources s2 JOIN jobs j2 ON j2.id=s2.job_id
             WHERE s2.knowledge_id=k.id AND j2.media_path!='' ORDER BY j2.created_at DESC LIMIT 1) AS thumbnail_job_id,
            COALESCE(da.cleaned_content,'') AS cleaned_content,
            COALESCE(da.transcript,'') AS transcript,
            COALESCE(da.transcript_cleaned,'') AS transcript_cleaned,
            COALESCE(da.stt_corrected_text,'') AS stt_corrected_text,
            COALESCE(da.native_subtitle_text,'') AS native_subtitle_text, COALESCE(da.transcript_segments,'') AS transcript_segments,
            COALESCE(da.ocr_text,'') AS ocr_text,
            COALESCE(dj.source_title,'') AS source_title,
            COALESCE(dj.extracted_caption,'') AS extracted_caption
            FROM knowledge_items k LEFT JOIN knowledge_sources s ON s.knowledge_id=k.id
            LEFT JOIN jobs dj ON dj.id=(SELECT s3.job_id FROM knowledge_sources s3 JOIN jobs j3 ON j3.id=s3.job_id
             WHERE s3.knowledge_id=k.id ORDER BY j3.created_at DESC LIMIT 1)
            LEFT JOIN analyses da ON da.job_id=dj.id
            WHERE (? OR k.hidden=0) GROUP BY k.id ORDER BY k.updated_at DESC LIMIT 200''', (int(include_hidden),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item['transcript_segments'] = json.loads(item.get('transcript_segments') or '[]')
            except (TypeError, ValueError):
                item['transcript_segments'] = []
            item['thumbnail_job_id'] = item.get('thumbnail_job_id') or ''
            raw_tags = item.pop('raw_tags', '') or ''
            item['tags'] = [tag.strip() for tag in raw_tags.split(',') if tag.strip()]
            item['sources'] = linked_archive_sources(db, 'knowledge_sources', 'knowledge_id', item['id'])
            result.append(item)
        return result


def places_list(include_hidden=False):
    with db_conn() as db:
        rows = db.execute('''SELECT p.id,p.name,p.address,p.category,p.latitude,p.longitude,p.naver_map_url,
            p.geocode_status,p.region_sido,p.region_sigungu,p.hidden,p.created_at,COUNT(s.job_id) AS source_count,
            (SELECT s2.job_id FROM place_sources s2 JOIN jobs j2 ON j2.id=s2.job_id
             WHERE s2.place_id=p.id AND j2.media_path!='' ORDER BY j2.created_at DESC LIMIT 1) AS thumbnail_job_id
            FROM places p LEFT JOIN place_sources s ON s.place_id=p.id
            WHERE (? OR p.hidden=0) GROUP BY p.id ORDER BY p.updated_at DESC LIMIT 200''', (int(include_hidden),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['thumbnail_job_id'] = item.get('thumbnail_job_id') or ''
            tag_rows = db.execute('''SELECT DISTINCT t.name FROM place_sources ps
                JOIN job_tags jt ON jt.job_id=ps.job_id JOIN tags t ON t.id=jt.tag_id
                WHERE ps.place_id=? ORDER BY t.name''', (item['id'],)).fetchall()
            item['tags'] = [tag['name'] for tag in tag_rows]
            item['sources'] = linked_archive_sources(db, 'place_sources', 'place_id', item['id'])
            result.append(item)
        return result


def telegram_call(token, method, data=None):
    url = 'https://api.telegram.org/bot{}/{}'.format(token, method)
    body = urllib.parse.urlencode(data or {}).encode() if data is not None else None
    try:
        req = urllib.request.Request(url, data=body)
        result = json.loads(urllib.request.urlopen(req, timeout=35).read())
    except Exception as exc:
        raise RuntimeError('Telegram API request failed') from exc
    if not result.get('ok'):
        raise RuntimeError('Telegram API returned an error')
    return result['result']


def telegram_send(token, chat_id, text):
    telegram_call(token, 'sendMessage', {'chat_id': chat_id, 'text': text[:4000]})


def telegram_download(token, file_id):
    meta = telegram_call(token, 'getFile', {'file_id': file_id})
    remote = meta.get('file_path', '')
    if not remote:
        return '', ''
    url = 'https://api.telegram.org/file/bot{}/{}'.format(token, remote)
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('Telegram Bot API download limit exceeded')
    suffix = Path(remote).suffix.lower()
    suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,8}', suffix) else '.bin'
    name = uuid.uuid4().hex + suffix
    dest = MEDIA / name
    dest.write_bytes(data)
    os.chmod(dest, 0o600)
    return str(dest.relative_to(DATA)), name


def process_update(token, allowed_user, update):
    update_id = int(update.get('update_id', 0))
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    sender = message.get('from') or {}
    chat_id = str(chat.get('id', ''))
    sender_id = str(sender.get('id', ''))
    # Only the configured user's private chat is accepted. Groups are rejected.
    if chat.get('type') != 'private' or chat_id != str(allowed_user) or sender_id != str(allowed_user):
        return True
    text = (message.get('text') or message.get('caption') or '').strip()
    if text.startswith('/start') or text.startswith('/help'):
        telegram_send(token, chat_id, 'LinkCollect 연결 완료. 링크·캡션·사진·영상을 보내면 원본을 보관하고 분석 대기열에 넣습니다. AI 및 영상 분석 설정이 완료되면 요약·분류·태그·장소 정보를 저장합니다.')
        return True
    match = re.search(r'https?://\S+', text)
    url = match.group(0).rstrip('.,!?)"]}') if match else ''
    file_id, media_type = '', ''
    if message.get('photo'):
        file_id, media_type = message['photo'][-1].get('file_id', ''), 'image'
    elif message.get('video'):
        file_id, media_type = message['video'].get('file_id', ''), 'video'
    elif message.get('document'):
        doc = message['document']
        file_id, media_type = doc.get('file_id', ''), doc.get('mime_type', 'file')
    if not text and not file_id:
        telegram_send(token, chat_id, '링크, 텍스트, 사진 또는 영상 파일을 보내주세요.')
        return True
    with db_conn() as db:
        prior = db.execute('SELECT job_id FROM telegram_updates WHERE update_id=?', (update_id,)).fetchone()
        if prior:
            job = db.execute('SELECT status FROM jobs WHERE id=?', (prior['job_id'],)).fetchone()
            job_id = prior['job_id']
            prior_status = job['status'] if job else 'received'
        else:
            job_id = ''
            prior_status = ''
    if job_id:
        if prior_status == 'media_failed':
            telegram_send(token, chat_id, '이 메시지는 접수 ID {}로 기록됐지만 첨부를 받지 못했습니다. 파일을 다시 보내주세요.'.format(job_id))
        else:
            telegram_send(token, chat_id, '이미 접수된 메시지입니다: {}'.format(job_id))
        return True
    media_path = media_name = ''
    if file_id:
        try:
            media_path, media_name = telegram_download(token, file_id)
        except Exception:
            LOG.exception('Telegram media download failed; retaining Telegram file reference')
    try:
        with db_conn() as db:
            job_status = 'media_failed' if file_id and not media_path else 'received'
            job_id = insert_job(db, 'telegram', url, text, media_path, media_name, media_type, file_id, job_status)
            db.execute('INSERT INTO telegram_updates(update_id,job_id,processed_at) VALUES(?,?,?)',
                       (update_id, job_id, datetime.now(timezone.utc).isoformat()))
    except Exception:
        if media_path:
            (DATA / media_path).unlink(missing_ok=True)
        raise
    if file_id and not media_path:
        telegram_send(token, chat_id, '링크·문구는 접수했지만 첨부 파일을 가져오지 못했습니다. 파일을 다시 보내거나 더 작은 파일로 보내주세요. 접수 ID: {}'.format(job_id))
    else:
        telegram_send(token, chat_id, '접수 완료: {} (원본 보관 완료, 분석 대기열 등록)'.format(job_id))
    return True


def telegram_worker():
    while True:
        cfg = load_env()
        token = cfg.get('TELEGRAM_KHSQOWP_LinkCollectbot', '')
        allowed_user = cfg.get('TELEGRAM_USER_INFO_ID', '')
        if not token or not allowed_user:
            LOG.warning('Telegram collector settings missing; retrying in 60 seconds')
            time.sleep(60)
            continue
        try:
            me = telegram_call(token, 'getMe')
            break
        except Exception:
            LOG.error('Telegram authentication failed; retrying in 60 seconds')
            time.sleep(60)
    LOG.info('Telegram collector authenticated as @%s', me.get('username', 'unknown'))
    with db_conn() as db:
        row = db.execute('SELECT MAX(update_id) AS latest FROM telegram_updates').fetchone()
        offset = int(row['latest'] or 0) + 1
    while True:
        try:
            updates = telegram_call(token, 'getUpdates', {'offset': offset, 'timeout': 25,
                'allowed_updates': json.dumps(['message'])})
            for update in updates:
                process_update(token, allowed_user, update)
                offset = max(offset, int(update.get('update_id', 0)) + 1)
        except Exception:
            LOG.exception('Telegram polling or update processing failed; retrying')
            time.sleep(10)


def analysis_worker():
    """Process accepted jobs serially; persist them as waiting when AI is unconfigured."""
    last_config_signature = None
    while True:
        try:
            config = load_env()
            ready = pipeline.ai_available(config)
            signature = (config.get('LINKCOLLECT_AI_PROVIDER', 'hermes'),
                         config.get('LINKCOLLECT_HERMES_CONTAINER', 'hermes'),
                         config.get('LINKCOLLECT_AI_BASE_URL', ''),
                         config.get('LINKCOLLECT_AI_API_KEY', ''),
                         config.get('LINKCOLLECT_AI_MODEL', ''),
                         config.get('LINKCOLLECT_TRANSCRIPTION_MODEL', ''))
            retry_waiting = ready and signature != last_config_signature
            last_config_signature = signature
            with db_conn() as db:
                row = db.execute("SELECT id FROM jobs WHERE status='received' OR (status='waiting_ai' AND ?) ORDER BY created_at LIMIT 1",
                                 (1 if retry_waiting else 0,)).fetchone()
            if row:
                state = pipeline.process_one(sys.modules[__name__], row['id'])
                LOG.info('Analysis job %s finished with state %s', row['id'], state)
            else:
                pipeline.resolve_pending_places(config)
                time.sleep(5)
        except Exception:
            LOG.exception('Analysis queue failed; retrying')
            time.sleep(10)


HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LinkCollect</title>__NAVER_MAP_SCRIPT__<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#131c31;--line:#263451;--ink:#eef3ff;--muted:#91a0bd;--accent:#a6f07d;--blue:#83c5ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 20% 0,#1b2a49 0,transparent 40%),var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1000px;margin:auto;padding:38px 22px 70px}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-weight:800;letter-spacing:.05em}.pill,.state{border:1px solid #405c49;color:var(--accent);border-radius:99px;padding:4px 10px;font-size:11px}h1{font-size:clamp(32px,6vw,56px);line-height:1.08;margin:42px 0 14px}.intro,.meta{color:var(--muted)}.intro{max-width:700px;margin-bottom:28px}.panel,.card{background:linear-gradient(145deg,#16223a,#111a2d);border:1px solid var(--line);border-radius:18px;padding:20px}.field{margin-bottom:14px}label{display:block;color:#b7c5dc;font-size:13px;margin-bottom:7px}input,textarea{width:100%;background:#0c1426;color:var(--ink);border:1px solid #344564;border-radius:10px;padding:12px;font:inherit}textarea{min-height:80px}button{border:0;border-radius:10px;padding:12px 16px;background:var(--accent);color:#10220c;font:inherit;font-weight:700;cursor:pointer}.secondary{background:#263653;color:var(--ink)}.hint{font-size:12px;color:var(--muted)}.section{display:flex;justify-content:space-between;align-items:center;margin:34px 0 12px}.grid{display:grid;gap:11px}.card{padding:16px}.card a{color:#9bd1ff;overflow-wrap:anywhere}.card p{white-space:pre-wrap;overflow-wrap:anywhere}.tag{color:var(--blue);font-size:11px;text-transform:uppercase}.meta{font-size:12px}.empty{border:1px dashed #3a4d6d;padding:24px;text-align:center;border-radius:15px;color:var(--muted)}#status{color:var(--accent);min-height:22px}@media(max-width:600px){main{padding:24px 14px}}
</style></head><body><main><header class="top"><div class="brand">LINKCOLLECT / ARCHIVE</div><div class="pill">PRIVATE · SSH TUNNEL</div></header><h1>좋은 링크와 미디어를<br>한곳에 모으세요.</h1><p class="intro">링크·캡션·사진·영상을 보관하고 AI 연결 시 내용을 요약·분류해 지식 항목과 장소 정보로 나눠 저장합니다. 분석 결과와 태그, 지도 검색을 이 화면에서 확인합니다.</p><section class="panel"><form id="form"><div class="field"><label for="url">게시물 링크</label><input id="url" type="url" placeholder="https://www.instagram.com/reel/..."></div><div class="field"><label for="text">캡션 또는 메모</label><textarea id="text" placeholder="게시물 문구를 붙여넣으세요"></textarea></div><div class="field"><label for="file">이미지 또는 영상 파일 (선택)</label><input id="file" type="file" accept="image/*,video/*"></div><p class="hint">공개 링크는 지원 사이트에서 원문 미디어를 내려받아 분석합니다. 로그인·비공개·접근 제한은 우회하지 않습니다. 링크와 캡션을 함께 입력하면 다운로드가 막혀도 캡션은 분석합니다.</p><div id="status"></div><button type="submit">아카이브에 추가</button></form></section><div class="section"><h2>최근 접수</h2><button class="secondary" id="refresh">새로고침</button></div><div id="jobs" class="grid"><div class="empty">불러오는 중…</div></div><div class="section"><h2>정보성 아카이브</h2></div><div id="knowledge" class="grid"><div class="empty">분석된 정보성 자료가 여기에 쌓입니다.</div></div><div class="section"><h2>장소 아카이브</h2></div><div id="map" class="panel" style="height:420px;margin-bottom:14px"><div class="empty">장소 좌표와 네이버 지도 키를 설정하면 여기에 핀이 표시됩니다.</div></div><div id="places" class="grid"><div class="empty">분석된 장소가 여기에 쌓입니다.</div></div></main><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function refresh(){let b=document.querySelector('#jobs');try{let d=await(await fetch('/api/jobs')).json();b.innerHTML=d.jobs.length?d.jobs.map(j=>`<article class="card"><div class="top"><span class="tag">${esc(j.source)}${j.media_type?' · '+esc(j.media_type):''}</span><span class="state">${esc(j.status)}</span></div>${j.url?`<p><a href="${esc(j.url)}" target="_blank" rel="noreferrer">${esc(j.url)}</a></p>`:''}${j.title?`<h3>${esc(j.title)}</h3>`:''}${j.summary?`<p><strong>요약</strong><br>${esc(j.summary)}</p>`:''}${j.cleaned_content?`<p>${esc(j.cleaned_content)}</p>`:''}${j.source_title?`<p class="meta">원문 제목: ${esc(j.source_title)}</p>`:''}${j.extracted_caption?`<p class="meta">원문 캡션: ${esc(j.extracted_caption)}</p>`:''}${j.ocr_text?`<p class="meta">화면 OCR: ${esc(j.ocr_text)}</p>`:''}${j.transcript?`<p class="meta">음성/자막: ${esc(j.transcript)}</p>`:''}${j.text?`<p class="meta">캡션: ${esc(j.text)}</p>`:''}${j.tags&&j.tags.length?`<p class="meta">태그: ${j.tags.map(t=>'#'+esc(t)).join(' ')}</p>`:''}${j.location_name?`<p>장소: ${esc(j.location_name)} ${j.location_address?esc(j.location_address):''}</p>`:''}${j.analysis_error?`<p class="meta">분석 상태: ${esc(j.analysis_error)}</p>`:''}${j.media_name?`<p class="meta">첨부: ${esc(j.media_name)}</p>`:''}<div class="meta">${esc(j.id)} · ${esc(new Date(j.created_at).toLocaleString())}</div></article>`).join(''):'<div class="empty">아직 접수된 항목이 없습니다. Telegram 봇이나 위 양식으로 추가하세요.</div>'}catch(e){b.innerHTML='<div class="empty">API 연결 실패. SSH 터널 상태를 확인하세요.</div>'}}let naverMap=null;let naverMarkers=[];function renderMap(places){let points=places.filter(p=>p.latitude!==null&&p.longitude!==null);let el=document.querySelector('#map');if(!window.naver||!window.naver.maps){if(!points.length)el.innerHTML='<div class="empty">장소 좌표 또는 네이버 지도 JavaScript 키가 설정되지 않았습니다.</div>';return}if(!naverMap){let center=points.length?new naver.maps.LatLng(points[0].latitude,points[0].longitude):new naver.maps.LatLng(37.5665,126.978);naverMap=new naver.maps.Map(el,{center:center,zoom:points.length?13:8})}naverMarkers.forEach(m=>m.setMap(null));naverMarkers=[];points.forEach(p=>{let marker=new naver.maps.Marker({position:new naver.maps.LatLng(p.latitude,p.longitude),map:naverMap,title:p.name});naverMarkers.push(marker)});if(points.length)naverMap.setCenter(new naver.maps.LatLng(points[0].latitude,points[0].longitude))}async function refreshStructured(){try{let kd=await(await fetch('/api/knowledge')).json();document.querySelector('#knowledge').innerHTML=kd.items.length?kd.items.map(x=>`<article class="card"><div class="tag">${esc(x.category)}</div><h3>${esc(x.title)}</h3><p>${esc(x.summary)}</p><div class="meta">연결된 원본 ${esc(x.source_count)}건</div></article>`).join(''):'<div class="empty">분석된 정보성 자료가 없습니다.</div>';let pd=await(await fetch('/api/places')).json();renderMap(pd.places);document.querySelector('#places').innerHTML=pd.places.length?pd.places.map(x=>`<article class="card"><div class="tag">${esc(x.category)}</div><h3>${esc(x.name)}</h3><p>${esc(x.address)}</p><div class="meta">${x.latitude!==null&&x.longitude!==null?`좌표 ${esc(x.latitude)}, ${esc(x.longitude)} · `:''}원본 ${esc(x.source_count)}건 · <a href="${esc(x.naver_map_url)}" target="_blank" rel="noreferrer">네이버 지도</a></div></article>`).join(''):'<div class="empty">분석된 장소가 없습니다.</div>'}catch(e){}}document.querySelector('#refresh').onclick=()=>{refresh();refreshStructured()};document.querySelector('#form').onsubmit=async e=>{e.preventDefault();let s=document.querySelector('#status');s.textContent='접수 중…';let file=document.querySelector('#file').files[0];try{let r;if(file){let f=new FormData();f.append('url',document.querySelector('#url').value);f.append('text',document.querySelector('#text').value);f.append('file',file);r=await fetch('/api/upload',{method:'POST',body:f})}else{r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value,text:document.querySelector('#text').value})})}let d=await r.json();if(!r.ok)throw Error(d.error||'접수 실패');s.textContent='접수 완료 · '+d.id;e.target.reset();await refresh()}catch(err){s.textContent='실패 · '+err.message}};refresh();refreshStructured();setInterval(()=>{refresh();refreshStructured()},15000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = 'LinkCollect/0.1'
    def log_message(self, fmt, *args):
        LOG.info('%s - %s', self.address_string(), fmt % args)
    def respond(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def json(self, status, value):
        self.respond(status, json.dumps(value, ensure_ascii=False).encode())
    def binary(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'private, max-age=3600')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def request_is_local(self):
        host = self.headers.get('Host', '')
        allowed_hosts = ('127.0.0.1:' + str(PORT), 'localhost:' + str(PORT))
        if host not in allowed_hosts:
            return False
        origin = self.headers.get('Origin')
        allowed_origins = ('http://127.0.0.1:' + str(PORT), 'http://localhost:' + str(PORT))
        return origin is None or origin in allowed_origins

    # Ochestration의 백엔드가 SSH 터널을 거쳐 호출할 때 보내는 고정 토큰. 터널이 이미 네트워크 경계를
    # 맡고 있어 토큰 없이도 안전하지만, 터널/호스트 검사와 별개의 2차 인증으로 둔다. 토큰이 비어있으면
    # (.env에 설정 안 함) 기존 호스트/오리진 검사만으로 동작해 LinkCollect 자체 대시보드는 그대로 쓸 수 있다.
    def request_is_authorized(self):
        token = load_env().get('LINKCOLLECT_API_TOKEN', '').strip()
        if token:
            auth = self.headers.get('Authorization', '')
            if auth == 'Bearer ' + token:
                return True
        return self.request_is_local()
    def content_length(self):
        raw = self.headers.get('Content-Length', '')
        if not raw.isdigit():
            return None
        return int(raw)
    def serve_thumbnail(self, job_id):
        media = job_media(job_id)
        if media is None:
            return self.json(404, {'error': 'no media for this job'})
        media_path, media_type = media
        media_type = media_type or ''
        if media_type.startswith('image/'):
            full_path = DATA / media_path
            if not full_path.is_file():
                return self.json(404, {'error': 'media file missing'})
            try:
                body = full_path.read_bytes()
            except OSError:
                return self.json(404, {'error': 'media file missing'})
            return self.binary(200, body, media_type)
        if media_type.startswith('video/'):
            thumb_path = video_thumbnail_path(job_id, media_path)
            if thumb_path is None:
                return self.json(404, {'error': 'thumbnail unavailable'})
            try:
                body = thumb_path.read_bytes()
            except OSError:
                return self.json(404, {'error': 'thumbnail unavailable'})
            return self.binary(200, body, 'image/jpeg')
        return self.json(404, {'error': 'unsupported media type'})

    def do_GET(self):
        if not self.request_is_authorized():
            return self.json(403, {'error': 'forbidden host or origin'})
        path = urllib.parse.urlsplit(self.path).path
        if path == '/':
            map_client_id = load_env().get('LINKCOLLECT_NCP_MAP_CLIENT_ID', '')
            map_script = ('<script src="https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId=' + urllib.parse.quote(map_client_id, safe='') + '"></script>' if map_client_id else '')
            return self.respond(200, HTML.replace('__NAVER_MAP_SCRIPT__', map_script).encode(), 'text/html; charset=utf-8')
        if path == '/api/jobs':
            return self.json(200, {'jobs': jobs_list()})
        if path == '/api/knowledge':
            include_hidden = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('include_hidden') == ['1']
            return self.json(200, {'items': knowledge_list(include_hidden)})
        if path == '/api/places':
            include_hidden = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('include_hidden') == ['1']
            return self.json(200, {'places': places_list(include_hidden)})
        if path == '/health':
            return self.json(200, {'status': 'ok'})
        thumb_match = re.fullmatch(r'/api/jobs/([0-9a-f]{8,40})/thumbnail', path)
        if thumb_match:
            return self.serve_thumbnail(thumb_match.group(1))
        return self.json(404, {'error': 'not found'})
    def do_POST(self):
        if not self.request_is_authorized():
            return self.json(403, {'error': 'forbidden host or origin'})
        path = urllib.parse.urlsplit(self.path).path
        length = self.content_length()
        if length is None:
            return self.json(400, {'error': 'invalid Content-Length'})
        if length > MAX_UPLOAD:
            return self.json(413, {'error': '요청 파일은 128MB 이하만 허용됩니다'})
        match = re.fullmatch(r'/api/places/([0-9a-f]{16})/regeocode', path)
        if match:
            if length > 4096:
                return self.json(413, {'error': 'Request is too large'})
            try:
                payload = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                return self.json(400, {'error': 'Invalid JSON'})
            if not isinstance(payload, dict) or not isinstance(payload.get('address'), str):
                return self.json(400, {'error': 'Address string is required'})
            address = payload['address'].strip()
            if not address or len(address) > 500:
                return self.json(400, {'error': 'Address must be between 1 and 500 characters'})
            place_id = match.group(1)
            with db_conn() as db:
                pipeline.ensure_schema(db)
                row = db.execute('SELECT id, name FROM places WHERE id=?', (place_id,)).fetchone()
                if not row:
                    return self.json(404, {'error': 'Place was not found'})
                dedupe_hash = pipeline.normalized_hash(row['name'], address)
                duplicate = db.execute('SELECT id FROM places WHERE dedupe_hash=? AND id<>?',
                                       (dedupe_hash, place_id)).fetchone()
                if duplicate:
                    return self.json(409, {'error': 'A place with the same name and address already exists'})
                naver_url = 'https://map.naver.com/p/search/' + urllib.parse.quote(address)
                db.execute("UPDATE places SET address=?, dedupe_hash=?, latitude=NULL, longitude=NULL, geocode_status='pending', geocode_attempted_at='', naver_map_url=?, updated_at=? WHERE id=?",
                           (address, dedupe_hash, naver_url, pipeline.utc_now(), place_id))
                db.commit()
            return self.json(200, {'id': place_id, 'geocode_status': 'pending'})
        if path == '/api/archive/visibility':
            try:
                payload = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                return self.json(400, {'error': 'JSON 형식을 확인하세요'})
            if not isinstance(payload, dict):
                return self.json(400, {'error': '요청 형식을 확인하세요'})
            kind, item_id, hidden = payload.get('kind'), payload.get('id'), payload.get('hidden')
            if kind not in ('knowledge', 'place') or not isinstance(hidden, bool):
                return self.json(400, {'error': 'kind 또는 hidden 값이 올바르지 않습니다'})
            if not isinstance(item_id, str) or not re.fullmatch(r'[0-9a-f]{16}', item_id):
                return self.json(400, {'error': 'id는 16자리 소문자 16진수여야 합니다'})
            table = 'knowledge_items' if kind == 'knowledge' else 'places'
            with db_conn() as db:
                pipeline.ensure_schema(db)
                cursor = db.execute('UPDATE {} SET hidden=?,updated_at=? WHERE id=?'.format(table), (int(hidden), pipeline.utc_now(), item_id))
                if cursor.rowcount == 0:
                    return self.json(404, {'error': '항목을 찾을 수 없습니다'})
                db.commit()
            return self.json(200, {'id': item_id, 'kind': kind, 'hidden': hidden})
        if path == '/api/archive/reclassify':
            try:
                payload = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                return self.json(400, {'error': 'JSON 형식을 확인하세요'})
            if not isinstance(payload, dict):
                return self.json(400, {'error': '요청 형식을 확인하세요'})
            kind, target, item_id = payload.get('kind'), payload.get('target'), payload.get('id')
            if kind not in ('knowledge', 'place') or target not in ('place', 'information'):
                return self.json(400, {'error': 'kind 또는 target 값이 올바르지 않습니다'})
            if not isinstance(item_id, str) or not re.fullmatch(r'[0-9a-f]{16}', item_id):
                return self.json(400, {'error': 'id는 16자리 소문자 16진수여야 합니다'})
            source_table = 'knowledge_sources' if kind == 'knowledge' else 'place_sources'
            source_id = 'knowledge_id' if kind == 'knowledge' else 'place_id'
            aggregate_table = 'knowledge_items' if kind == 'knowledge' else 'places'
            with db_conn() as db:
                pipeline.ensure_schema(db)
                rows = db.execute('SELECT job_id FROM {} WHERE {}=?'.format(source_table, source_id),
                                  (item_id,)).fetchall()
                job_ids = [row['job_id'] for row in rows]
                if not job_ids:
                    return self.json(404, {'error': '연결된 원본을 찾을 수 없습니다'})
                for job_id in job_ids:
                    db.execute('DELETE FROM {} WHERE {}=? AND job_id=?'.format(source_table, source_id),
                               (item_id, job_id))
                    db.execute("UPDATE jobs SET status='received', analysis_attempts=0, reclassify_hint=? WHERE id=?",
                               (target, job_id))
                remaining = db.execute('SELECT 1 FROM {} WHERE {}=? LIMIT 1'.format(source_table, source_id),
                                       (item_id,)).fetchone()
                if not remaining:
                    db.execute('DELETE FROM {} WHERE id=?'.format(aggregate_table), (item_id,))
                db.commit()
            return self.json(200, {'requeued': len(job_ids)})
        if path == '/api/jobs':
            try:
                payload = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                return self.json(400, {'error': 'JSON 형식을 확인하세요'})
            url, text = str(payload.get('url', '')).strip(), str(payload.get('text', '')).strip()
            if url and urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
                return self.json(400, {'error': 'http 또는 https 링크만 접수합니다'})
            if not url and not text:
                return self.json(400, {'error': '링크나 메모를 입력하세요'})
            return self.json(201, {'id': create_job('web', url, text), 'status': 'received'})
        if path == '/api/upload':
            ctype = self.headers.get('Content-Type', '')
            if not ctype.startswith('multipart/form-data'):
                return self.json(400, {'error': 'multipart/form-data 형식이 필요합니다'})
            try:
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':ctype,'CONTENT_LENGTH':str(length)})
                fields = {}
                for item in (form.list or []):
                    if not item.name or item.name in fields:
                        raise ValueError('duplicate or missing multipart field')
                    fields[item.name] = item
                def text_value(key):
                    item = fields.get(key)
                    if item is None:
                        return ''
                    if getattr(item, 'filename', None) is not None:
                        raise ValueError('invalid text field')
                    return str(item.value or '').strip()
                url, text = text_value('url'), text_value('text')
                if url and urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
                    return self.json(400, {'error': 'http 또는 https 링크만 접수합니다'})
                upload = fields.get('file')
                if upload is None or getattr(upload, 'file', None) is None or not getattr(upload, 'filename', None):
                    return self.json(400, {'error': '첨부 파일을 선택하세요'})
                raw_name = Path(upload.filename or 'upload.bin').name
                suffix = Path(raw_name).suffix.lower()
                if suffix not in {'.jpg','.jpeg','.png','.webp','.gif','.mp4','.mov','.m4v','.webm'}:
                    return self.json(415, {'error': '지원하는 이미지 또는 영상 형식이 아닙니다'})
            except ValueError:
                return self.json(400, {'error': 'multipart 필드 형식을 확인하세요'})
            except Exception:
                LOG.exception('Multipart upload parsing failed')
                return self.json(400, {'error': 'multipart/form-data 내용을 읽을 수 없습니다'})
            name = uuid.uuid4().hex + suffix
            dest = MEDIA / name
            try:
                total = 0
                with dest.open('wb') as out:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UPLOAD:
                            raise OverflowError('upload exceeds size cap')
                        out.write(chunk)
                os.chmod(dest, 0o600)
                media_type = mimetypes.guess_type(raw_name)[0] or 'application/octet-stream'
                job_id = create_job('web', url, text, str(dest.relative_to(DATA)), raw_name, media_type)
            except OverflowError:
                dest.unlink(missing_ok=True)
                return self.json(413, {'error': '요청 파일은 128MB 이하만 허용됩니다'})
            except Exception:
                dest.unlink(missing_ok=True)
                LOG.exception('Media upload persistence failed')
                return self.json(500, {'error': '파일을 저장하지 못했습니다'})
            return self.json(201, {'id': job_id, 'status': 'received'})
        return self.json(404, {'error': 'not found'})


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    db_conn()
    with db_conn() as db:
        db.execute("UPDATE jobs SET status='received' WHERE status='processing'")
    threading.Thread(target=telegram_worker, daemon=True, name='telegram-poller').start()
    threading.Thread(target=analysis_worker, daemon=True, name='analysis-worker').start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info('web intake listening on %s:%s', HOST, PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()
