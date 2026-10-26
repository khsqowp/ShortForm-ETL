# LinkCollect

개인용 Telegram 봇 파이프라인입니다. 내가 나 자신에게 보낸 링크·사진·영상(인스타그램/틱톡/유튜브 릴스 등)과 메모를 받아 원본을 보관하고, AI로 내용을 분석·분류한 뒤 "지식 아카이브"와 "장소 아카이브"로 자동 정리해 줍니다.

> 개인 사용을 전제로 만든 사이드 프로젝트입니다. 코드가 공개되어 있지만, 실행에는 소유자 본인의 Telegram 계정·AI API·네이버 지도 API 등 개인 자격 증명이 필요합니다.

## 이게 뭔가요?

평소에 인스타그램/틱톡/유튜브에서 보는 괜찮은 게시물(릴스, 이미지, 글)을 나 자신에게 Telegram으로 전달하면,

1. 원본 링크·캡션·미디어를 안전하게 보관하고
2. AI 비전/텍스트 분류기로 내용을 분석해서
3. 중복을 제거하며 "지식(정보성)" 또는 "장소" 아카이브로 자동 정리합니다.

나중에 "그때 봤던 그 보안 관련 글 뭐였지?" 혹은 "저번에 저장해둔 맛집이 어디였지?" 싶을 때 바로 찾아볼 수 있게 만든 개인 아카이빙 도구입니다.

## 핵심 기능

- **AI 기반 자동 분류**: 캡션·이미지·영상 프레임·음성 전사를 함께 넣어 제목, 요약, 카테고리(`development`/`security`/`investment`/`fashion_beauty`/`other`), 태그를 자동 생성합니다.
- **지식/장소 중복 제거**: 같은 주제의 글을 여러 번 저장해도 정규화된 내용 해시로 묶어 하나의 지식 항목·장소로 합칩니다.
- **재분류 API**: 잘못 분류된 항목(정보성 ↔ 장소)을 다시 분석 대기열로 돌려보내는 `/api/archive/reclassify` 엔드포인트를 제공합니다.
- **영상 분석 지원**: 영상에서 프레임을 추출해 시각적으로 분석하고, 음성은 Whisper로 전사해 텍스트 분석에 함께 사용합니다.
- **이미지 OCR**: 이미지/영상 프레임 속 텍스트를 Tesseract OCR로 추출해 분석 입력에 포함합니다.
- **장소 지오코딩**: 장소로 분류된 항목은 네이버 지도 Geocoding API로 좌표를 구해 지도에 표시합니다.
- **읽기 전용 웹 대시보드**: 최근 접수 현황, 지식 아카이브, 장소 아카이브(네이버 지도 포함)를 하나의 페이지에서 확인합니다.

## 아키텍처 한눈에 보기

프레임워크 없이 순수 Python 표준 라이브러리로 작성된 **두 개의 파일**로 구성됩니다.

| 파일 | 역할 |
|---|---|
| `linkcollect_app.py` | HTTP 서버(API + 대시보드 HTML), Telegram 폴링 워커, 분석 작업 큐 워커, SQLite 연결 관리 |
| `linkcollect_pipeline.py` | 공개 게시물 다운로드(yt-dlp), 영상 프레임/오디오 추출, OCR, Whisper 전사, AI 호출, 분석 결과 저장·중복 제거·지오코딩 |

- **저장소**: SQLite 단일 파일(`data/linkcollect.sqlite3`). 미디어 원본은 `data/media/`에 저장됩니다.
- **실행 방식**: `python3 linkcollect_app.py`로 직접 실행하거나, systemd user service로 상주시킵니다.
- **AI 연동은 두 가지 방식을 모두 지원**합니다 (`LINKCOLLECT_AI_PROVIDER`로 선택):
  - `hermes`(기본값): 로컬 Docker 컨테이너(`LINKCOLLECT_HERMES_CONTAINER`, 기본 `hermes`) 안에서 OpenAI 호환 API를 호출하고, 같은 컨테이너의 `ffmpeg`로 영상 프레임/오디오를 추출합니다. 이 모드는 `docker` CLI가 있어야 동작합니다.
  - `openai`: 외부 OpenAI 호환 API(`LINKCOLLECT_AI_BASE_URL`, 기본 `https://api.openai.com/v1`)를 `LINKCOLLECT_AI_API_KEY`/`LINKCOLLECT_AI_MODEL`로 직접 호출합니다. 이 모드에서는 영상 프레임/오디오 추출을 위한 Docker 컨테이너를 사용하지 않습니다(`extract_video_assets`가 내부적으로 Docker에 의존하기 때문에, `openai` 모드에서는 영상 프레임 추출 자체가 동작하지 않습니다 — 텍스트/이미지/음성 전사는 정상 동작).
- **지오코딩**: 네이버 클라우드 플랫폼 Maps API(Geocoding + 클라이언트 지도 스크립트)를 선택적으로 사용합니다.

## 요구 사항 / 환경 변수

코드에서 실제로 참조하는 환경 변수만 정리했습니다. `LINKCOLLECT_ENV_FILE`(기본 `/home/opc/My_Archive/.env`)에 `KEY=value` 형식으로 적어두면 서버가 직접 읽어옵니다(일반 OS 환경 변수가 아님). `LINKCOLLECT_DATA_DIR`, `LINKCOLLECT_ENV_FILE`, `LINKCOLLECT_PORT`만 예외로 프로세스 실행 시의 OS 환경 변수로 읽습니다.

### 서버 기본 설정 (`linkcollect_app.py`, OS 환경 변수)

| 변수 | 용도 | 필수 여부 |
|---|---|---|
| `LINKCOLLECT_DATA_DIR` | DB·미디어·썸네일 저장 경로 | 선택 (기본: 스크립트 폴더의 `data/`) |
| `LINKCOLLECT_ENV_FILE` | 비밀 설정(.env) 파일 경로 | 선택 (기본: `/home/opc/My_Archive/.env`) |
| `LINKCOLLECT_PORT` | HTTP 서버 포트 | 선택 (기본: `18787`) |

### `.env` 파일 설정 (`LINKCOLLECT_ENV_FILE`에 기록)

| 변수 | 용도 | 필수 여부 |
|---|---|---|
| `TELEGRAM_KHSQOWP_LinkCollectbot` | Telegram 봇 토큰 | Telegram 수신 기능에 필수 (없으면 60초마다 재시도만 하며 대기) |
| `TELEGRAM_USER_INFO_ID` | 수신을 허용할 Telegram 사용자 ID (본인 1:1 채팅만 허용) | Telegram 수신 기능에 필수 |
| `LINKCOLLECT_API_TOKEN` | API 호출용 Bearer 토큰(설정 시 Authorization 헤더로 인증 가능) | 선택 |
| `LINKCOLLECT_NCP_MAP_CLIENT_ID` | 네이버 지도 JavaScript SDK 클라이언트 ID(대시보드 지도 렌더링용) | 선택 |
| `LINKCOLLECT_AI_PROVIDER` | `hermes`(기본) 또는 `openai` | 선택 |
| `LINKCOLLECT_HERMES_CONTAINER` | `hermes` 제공자일 때 사용할 Docker 컨테이너 이름 | 선택 (기본: `hermes`, provider가 `hermes`일 때만 사용) |
| `LINKCOLLECT_AI_MODEL` | 사용할 모델명 | `hermes`: 선택(기본 `hermes-agent`) / `openai`: 필수 |
| `LINKCOLLECT_AI_API_KEY` | OpenAI 호환 API 키 | `openai` 제공자일 때 필수 |
| `LINKCOLLECT_AI_BASE_URL` | OpenAI 호환 API Base URL | 선택 (기본: `https://api.openai.com/v1`, `openai` 제공자일 때만 사용) |
| `LINKCOLLECT_YTDLP_BIN` | 공개 게시물 다운로드용 `yt-dlp` 실행 파일 경로 | 선택 (기본 경로에 없으면 `PATH`의 `yt-dlp` 사용) |
| `LINKCOLLECT_WHISPER_PYTHON` | `faster-whisper`가 설치된 Python 인터프리터 경로 | 선택 (기본값 있음) |
| `LINKCOLLECT_WHISPER_MODEL` | `faster-whisper` 모델명 | 선택 (기본: `small`) |
| `LINKCOLLECT_NCP_MAP_API_KEY_ID` | 네이버 클라우드 플랫폼 Geocoding API Key ID | 선택 (없으면 장소 좌표 변환 생략) |
| `LINKCOLLECT_NCP_MAP_API_KEY` | 네이버 클라우드 플랫폼 Geocoding API Key | 선택 (없으면 장소 좌표 변환 생략) |
| `LINKCOLLECT_TRANSCRIPTION_MODEL` | 분석 워커가 설정 변경을 감지하는 내부 서명(signature)에만 포함되는 값 | 선택, 현재 실제 Whisper 모델 선택에는 사용되지 않음(`LINKCOLLECT_WHISPER_MODEL`이 실제 모델을 결정) |

## 실행 방법

### 직접 실행

```bash
export LINKCOLLECT_DATA_DIR=/path/to/data      # 선택
export LINKCOLLECT_ENV_FILE=/path/to/.env      # 선택
export LINKCOLLECT_PORT=18787                  # 선택
python3 linkcollect_app.py
```

서버는 `127.0.0.1`에만 바인딩됩니다. 외부에서 접근하려면 SSH 터널 등을 통해야 합니다.

### systemd user service (상주 실행)

실제 운영 중인 유닛 파일 내용을 그대로 옮긴 것은 아니며, 코드에서 쓰는 값들을 기준으로 구성한 예시입니다.

```ini
# ~/.config/systemd/user/linkcollect.service
[Unit]
Description=LinkCollect archive bot
After=network.target

[Service]
WorkingDirectory=/home/opc/My_Archive/TELEGRAM_KHSQOWP_LinkCollectbot
ExecStart=/usr/bin/python3 linkcollect_app.py
Environment=LINKCOLLECT_DATA_DIR=/home/opc/My_Archive/TELEGRAM_KHSQOWP_LinkCollectbot/data
Environment=LINKCOLLECT_ENV_FILE=/home/opc/My_Archive/.env
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now linkcollect.service
```

### 외부 의존성

두 `.py` 파일 자체는 **Python 표준 라이브러리만** 사용합니다(`sqlite3`, `http.server`, `subprocess`, `urllib` 등). 다만 기능별로 아래 외부 도구를 `subprocess`/`docker exec`로 호출합니다. 해당 기능이 필요 없다면 설치하지 않아도 나머지는 그대로 동작합니다.

| 도구 | 쓰이는 곳 | 없을 때 동작 |
|---|---|---|
| `docker` + Hermes 컨테이너 | `LINKCOLLECT_AI_PROVIDER=hermes`일 때 AI 호출 및 영상 프레임/오디오 추출(컨테이너 내부 `ffmpeg`) | AI 분석 불가(대기 상태로 남음), 영상 분석 불가 |
| `yt-dlp` | 공개 게시물(인스타그램/틱톡/유튜브/X/threads) 원본 다운로드 | 링크만 접수되고 다운로드 실패 사유가 기록됨 |
| `faster-whisper` (별도 Python venv) | 영상 음성 전사 | 전사 없이 자막(있는 경우)만 사용 |
| `tesseract` (한국어+영어) | 이미지/영상 프레임 OCR | OCR 텍스트 없이 분석 진행 |
| 네이버 클라우드 플랫폼 Maps API | 장소 지오코딩 + 대시보드 지도 렌더링 | 장소 좌표 없이 이름/주소만 저장 |
| Telegram Bot API | 메시지 수신/응답 | 봇 수신 기능 비활성 |

## 주의 (disclaimer)

이 도구는 **소유자 개인의 지식 아카이빙 전용**으로 만들어졌습니다. 보안/침투테스트 관련 콘텐츠를 분류할 수 있는 것은 소유자가 보안 진단 업무를 하면서 접하는 전문 자료를 개인적으로 정리하기 위함이며, 저작권이 있는 콘텐츠를 스크래핑하거나 재배포하는 용도로 사용하지 않습니다. 이 저장소를 가져가 사용하는 경우, 전송·보관·분석하는 콘텐츠의 저작권 및 각 플랫폼 이용약관 준수에 대한 책임은 전적으로 사용자 본인에게 있습니다. `yt-dlp`를 사용하는 다운로드 기능 역시 **공개된 콘텐츠만** 대상으로 하며, 로그인이나 비공개 접근 제한을 우회하지 않습니다.

## 라이선스

라이선스: 아직 지정되지 않음 (개인 프로젝트)
