# Daily Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task after implementation is requested. Steps use checkbox (`- [ ]`) syntax for tracking. This document does not authorize deployment or changes to the other team's backend.

**Goal:** 한국 날짜마다 상시 녹화를 한 건으로 조회하고 오늘은 완료 구간을 재생하며, 종료된 날짜는 확보 영상만 단일 MP4로 다운로드하고 누락을 명시한다.

**Architecture:** 기존 JPEG/WSS·MJPEG·이벤트 API 위에 날짜 녹화 서비스를 추가한다. 기존 단기 JPEG 버퍼를 짧게 보호하여 10초 H.264/TS 조각을 생성하고 HLS로 재생한다. 날짜 카탈로그·누락·export를 기존 SQLite 연결/잠금 아래 저장하고 종료 날짜만 비동기 MP4로 병합한다.

**Tech Stack:** Python3.13, asyncio, aiohttp, zoneinfo/tzdata, SQLite WAL/FULL, FFmpeg libx264/HLS/concat, pytest·pytest-asyncio·imageio-ffmpeg.

**Spec:** [2026-10-14-daily-recording-design.md](../specs/2026-10-14-daily-recording-design.md). 원본 외부 API 초안의 제안과 현재 코드의 사실을 이 설계에서 구분했다. 경로·재생·누락 의미는 백엔드 합의 전 제안이다.

## Global Constraints

- 카메라 엣지와 미디어 서비스가 구현 범위다. 백엔드 사용자 인증·피트 연결·화면 API 및 프론트 코드를 변경하지 않는다.
- v1 이벤트 설정/클립/MJPEG와 기존 데이터를 유지한다. MEDIA_API_TOKEN을 추가하지 않는다.
- 날짜 Asia/Seoul, 타임스탬프 UTC RFC3339 밀리초/Z, 시간창은 시작 포함/끝 제외.
- 하루 한 UUID, 카메라·날짜 유일 제약. 오늘 및 finalizing 날짜는 전체 MP4 export 불가.
- 사용자 설명에 따른 기존 혼동은 날짜 없이 24시간제 시각만 표시한 문제다. v1은 이미 날짜 포함 UTC RFC3339를 제공한다. 표시 형식 개선, 날짜별 녹화 집계, 실제 수신/저장 누락 검출을 각각 구분한다.
- 미래 시간과 인코딩 대기 꼬리는 gap 아님. 원본이 없는 시간은 멈춘 영상으로 채우지 않는다.
- 초기10초 조각, 10분 디렉터리 묶음, 1초 초과 무수신 기본 기준, 카메라당 미처리30초 상한.
- 초기 프로파일1920×1080/약30fps/H.264/yuv420p/무음/veryfast/CRF23/threads2. 운영 프로파일은 서버 측정 단계에서 확정한다.
- 추가 조각 인코딩1개, export 동시1개/대기8개. 파일시스템 최소 여유5GiB, export 캐시24시간.
- 날짜 기능은 opt-in으로 배포하고 MAX_DAILY_STORAGE_BYTES 명시 시에만 활성화한다. 기존 보관 기간보다 빨리 자료를 자동 삭제하는 정책은 적용하지 않는다.
- 로컬에는 기존 미커밋 운영 문서/배포 설정이 있다. 실행 시 변경을 보존하고 관련 부분만 수정한다. 이번 작업은 분석·계획 작성이며 제품 구현은 시작하지 않았다.

## Review Focus

1. 자정에 카메라가 완전히 끊겨 있어도 전날과 다음 날 행이 정확히 생성·확정되는가 — Task1/3.
2. 파일 rename 성공 직후 DB 저장 전에 프로세스가 종료되어도 이미 저장한 영상과 날짜 ID가 복구되는가 — Task2/3.
3. 무송신/큐 폐기/인코딩 실패를 미래 시간이나 아직 처리 중인 꼬리와 혼동하지 않는가 — Task3/4.
4. 입력 크기·FPS 변경 및 gap 뒤 PTS가 MP4 병합/브라우저 seek에서 누락을 숨기거나 디코딩을 깨뜨리지 않는가 — Task3/5/8.
5. 재요청·삭제·만료·여유 공간 감소가 동시에 발생해 export가 중복되거나 녹화가 무한 대기하지 않는가 — Task5/7/8.

## 변경 파일 지도

| 파일 | 책임 |
|---|---|
| 새 `src/camera_media_service/daily_models.py` | 한국 날짜창, 날짜/조각/누락 상태, 시각 계산 |
| 수정 `pyproject.toml`, `uv.lock` | Windows/Linux 공통 시간대 데이터 tzdata 추가 |
| 새 `src/camera_media_service/daily_catalog.py` | SQLite 날짜/조각/누락/export 테이블과 단일 날짜 ID |
| 수정 `recording.py` | 공용 버퍼 snapshot·사용권·정리 보호, 기존 이벤트 잠금/DB 소유권 유지 |
| 수정 `ingest.py`, `pipeline.py` | 연결 상태·버퍼 큐 폐기의 시각/원인 전달 |
| 새 `src/camera_media_service/daily_recording.py` | 시간창 수집, 조각 스케줄, 날짜 마감, 복구·정책·coverage |
| 수정 `encoding.py` | 기존 encode_clip 보존, 새 encode_daily_segment/merge_daily_mp4 |
| 새 `src/camera_media_service/daily_playback.py` | HLS와 실제 시간↔재생 시간 매핑 |
| 새 `src/camera_media_service/daily_export.py` | export 작업·동시 제한·파일 사용권·캐시/날짜 삭제·만료 |
| 새 `src/camera_media_service/media_files.py` | 이미 열린 파일의 공용 Range/HEAD 전송 |
| 새 `src/camera_media_service/daily_http_api.py` | v2 요청 검증/JSON/메타데이터·파일 라우트 |
| 수정 `http_api.py`, `app.py`, `settings.py` | 기존 API 유지, 서비스 생성·생명주기·환경설정 |
| 새 `docs/DAILY_RECORDING_API_CONTRACT.md` | 백엔드 최종 경로·필드·상태·오류·게이트웨이 하위 경로 |
| 수정 `.env.example`, README·ARCHITECTURE·운영 문서·배포 설정 | 활성화 조건과 운영/부하 실측 기록 |

아래 Interfaces는 구현 시 사용할 초기 계약이다. 새 테이블은 RecordingService의 같은 connection/lock을 사용한다. DailyCatalog의 DB 메서드는 호출자가 공용 잠금을 보유하는 동기 메서드이며 자체 잠금을 중첩하지 않는다.

### Task 1: 한국 날짜창과 상태 모델

**Files:** 새 daily_models.py, tests/test_daily_models.py; 수정 pyproject.toml, uv.lock.

**Interfaces:** `DailyWindow.from_date(recording_date: date) -> DailyWindow`; fields `recording_date`, `start_at: float`, `end_at: float`. `seoul_date(timestamp: float) -> date`. `StoredFrame(path: str, received_at: float, size_bytes: int)`.

- [ ] 다음 테스트부터 작성: 2026-10-14의 UTC 시작/끝 `2026-10-13T15:00:00.000Z` / `2026-10-14T15:00:00.000Z`, 길이86400초, 경계 직전/정확한 자정, 윤년·연말 전환. 미래 날짜와 잘못된 날짜 필터는 API 단계에서400.
- [ ] `uv run pytest tests/test_daily_models.py -q`로 새 모델 부재 실패 확인.
- [ ] ZoneInfo/UTC 기반 모델 구현. 호스트 로컬 timezone에 의존하지 않는다. tzdata를 런타임 의존성에 추가하고 lock 갱신 후 Windows의 `ZoneInfo("Asia/Seoul")` 및 Linux 동일 날짜 계산을 확인한다. 현재 Windows 기존 환경의 tzdata 부재는 확인했다.
- [ ] 동일 테스트 명령 통과 확인, task 단위 커밋.

### Task 2: 날짜 카탈로그·버퍼 사용권·복구 자료

**Files:** 새 daily_catalog.py, tests/test_daily_catalog.py; 수정 recording.py, tests/test_recording.py.

**Interfaces:** `DailyCatalog(db: sqlite3.Connection)`; `ensure_day(camera_id: str, window: DailyWindow, retention_days: int) -> dict`; `begin_segment(recording_id: str, start_at: float, end_at: float) -> dict`; `complete_segment(segment_id: str, metadata: dict) -> None`. RecordingService에 잠금 아래 사용하는 `pin_frames(owner_id: str, camera_id: str, start_at: float, end_at: float) -> list[StoredFrame]`, `release_frames(owner_id: str) -> None` 추가.

- [ ] 같은 카메라/날짜의10개 요청과 재시작이 같은 UUID인 테스트; 기존 jobs/policies 유지 테스트; 조각 pin이 순환 삭제에 살아남고 해제 후 정리되는 테스트 작성.
- [ ] `uv run pytest tests/test_daily_catalog.py tests/test_recording.py -q` 실행. 새 인터페이스 관련 실패와 기존 테스트 통과를 구분한다.
- [ ] 테이블 생성/유일 제약/정렬 인덱스/사용권 추가. segment 준비 manifest의 UUID·시각을 rename 전에 영속 기록한다. 같은 transaction으로 snapshot과 pin을 고정한다.
- [ ] rename 후 DB commit 전 장애를 주입해 startup recovery가 파일을 검증해 복구하는 테스트 추가. 손상 파일은 ready로 공개하지 않는다.
- [ ] 위 테스트 통과 확인, task 단위 커밋.

### Task 3: 날짜 녹화기·분할 인코딩·누락·자정/재시작

**Files:** 새 daily_recording.py, tests/test_daily_recording.py, tests/test_daily_encoding.py; 수정 encoding.py, recording.py의 notification 경계, ingest.py, pipeline.py, tests/test_ingest.py, tests/test_pipeline.py.

**Interfaces:** `DailyRecordingService(recording: RecordingService, catalog: DailyCatalog, *, clock: Callable[[], float], settings: Settings)`; async `start()`, `tick(now: float)`, `get_day(recording_id: str) -> dict`, `get_policy(camera_id: str) -> dict`, `set_policy(camera_id: str, body: dict) -> dict`, `list_days(camera_id: str, filters: dict, limit: int, after: tuple|None) -> list[dict]`, `close()`. 연결/큐 폐기 보고는 비차단 `notify_loss(camera_id: str, start_at: float, end_at: float, reason_code: str) -> None`로 전달한다. `encode_daily_segment(ffmpeg: str, buffer: Path, destination: Path, frames: list[StoredFrame], profile: dict) -> dict`는 검증된 실제 시작/끝·길이·크기·코덱 정보를 반환한다.

- [ ] 모의 시계로 23:59:50~00:00:10 입력이 날짜별 조각으로 분리되는 테스트, 프레임 없이 자정 전환, 정오 활성화의 RECORDING_NOT_STARTED gap, 중단2일 뒤 날짜별 none/partial 복구 테스트 작성.
- [ ] `uv run pytest tests/test_daily_recording.py tests/test_daily_encoding.py -q`로 실패 확인.
- [ ] 버퍼 commit 알림은 wakeup 용도로만 사용하고 실제 입력 snapshot은 DB에서 가져온다. 알림 손실이 프레임 손실이 되지 않게 한다. 짧은 시간창만 pin하고 인코딩 성공/실패 후 해제한다.
- [ ] 현재 소켓의 연결 종료와 실제 RecordingQueue 프레임 폐기를 원인/시각으로 보고한다. 이전 소켓 교체 종료가 현재 연결의 gap이 되지 않는 테스트와1초 미만의 명시 단절/프레임 폐기 기록 테스트를 추가한다. 상태 알림 처리 실패도 로그/복구 상태로 드러낸다.
- [ ] 10초 슬롯 안의1초 초과 무송신과 명시적으로 확인한 모든 gap을 분리하고 수신 간격을 실제 media PTS에 반영해 H.264/TS를 생성한다. 마지막 프레임을 다음 연결까지 길게 유지하지 않는다. 초기 저장 프로파일과 독립 디코딩 시작을 고정한다.
- [ ] 무송신, 큐 폐기, 파일 쓰기 실패, 깨진 JPEG, FFmpeg 실패/timeout, 입력 크기 변경, 시스템 시각 역행을 각각 주입한다. 새 날짜의 future와 처리 중 꼬리가 gaps에 없고 최종 판정 실패는 실제 누락으로 나타나는지 확인한다.
- [ ] 실제 FFmpeg로 조각 디코딩/프레임 시각/길이를 확인하고 위 테스트 통과, task 단위 커밋.

### Task 4: 오늘/지난 날짜 HLS와 시간 매핑

**Files:** 새 daily_playback.py, tests/test_daily_playback.py.

**Interfaces:** `build_daily_playlist(day: dict, segments: list[dict]) -> str`; `build_available_intervals(segments: list[dict]) -> list[dict]`; DailyRecordingService async `playback_snapshot(recording_id: str) -> tuple[dict,list[dict]]`.

- [ ] 오늘의 EVENT 목록이 완료 조각만 포함하고 처음부터 누적되는 테스트; finalizing 후 closed VOD/ENDLIST; gap 뒤 PROGRAM-DATE-TIME/DISCONTINUITY; none 날짜 unavailable 테스트 작성.
- [ ] `uv run pytest tests/test_daily_playback.py -q`로 실패 확인.
- [ ] 상대 UUID 경로만 작성한다. 섬네일/별도 AI 정보는 추가하지 않는다. 날짜 전체 m3u8 크기와 메타데이터 응답 크기를24시간 조각 수 기준으로 확인한다.
- [ ] 00:00~00:00:20 및00:00:30~00:01:00의 실제 시간창이 재생상0~20/20~50초로 매핑되고 gap20~30초가 별도 유지되는 테스트, pending tail 제외 테스트를 통과시킨다.
- [ ] 해당 테스트 및 실제 조각 목록 읽기 확인, task 단위 커밋.

### Task 5: 단일 MP4 준비·재시도·삭제·만료·용량 제한

**Files:** 새 daily_export.py, tests/test_daily_export.py, tests/test_daily_retention.py; 수정 encoding.py.

**Interfaces:** `DailyExportService(daily: DailyRecordingService, settings: Settings)`; async `request_export(recording_id: str) -> tuple[dict,bool]`, `delete_day(recording_id: str) -> None`, `maintenance(now: float) -> None`, `close()`. `merge_daily_mp4(ffmpeg: str, segments: list[dict], temporary: Path, final: Path) -> dict`는 검증된 길이·크기를 반환한다.

- [ ] 오늘409/DAY_NOT_CLOSED, finalizing409, none409; closed의 동시10요청은1job/202; ready200; 실패 후 재시도·응답 유실·재시작의 UUID 유지 테스트 작성.
- [ ] `uv run pytest tests/test_daily_export.py tests/test_daily_retention.py -q` 실패 확인.
- [ ] 입력 규격 확인 후 H.264를 stream copy로 MP4에 remux, faststart와 임시→최종 rename 적용. source revision별 단일 작업, bounded queue, 디스크 예약/사용권을 구현한다.
- [ ] 서로 다른 조각의 시간창과 gap이 있는 실제 단일 MP4를 생성하여 처음/중간/끝을 디코딩한다. 확보 영상 길이 합과 결과 길이를 비교하고24시간 가짜 freeze가 없는지 확인한다. stream 불일치는 명시 오류로 처리한다.
- [ ] export 직전/중간 용량 부족, 동시 삭제/만료, 캐시24시간 만료 후 재준비, 실패 임시 파일 정리 테스트. 예약5GiB와MAX_DAILY_STORAGE_BYTES를 초과해 시작하지 않고 원본 날짜의 coverage를 export 실패 때문에 바꾸지 않는지 확인.
- [ ] 녹화 중/준비 중 삭제409, 종료 날짜 삭제204 반복204 및 파일410, 원본 retention 만료 시 조각·캐시 삭제를 검증한다.
- [ ] 위 테스트 통과 확인, task 단위 커밋.

### Task 6: v2 HTTP 계약과 게이트웨이 경로

**Files:** 새 daily_http_api.py, media_files.py, tests/test_daily_http_api.py, tests/test_media_files.py, docs/DAILY_RECORDING_API_CONTRACT.md; 수정 http_api.py 및 tests/test_http_api.py.

**Interfaces:** `register_daily_routes(app: web.Application, daily: DailyRecordingService, exports: DailyExportService) -> None`; `create_http_app(service: RecordingService, *, daily: DailyRecordingService|None=None, exports: DailyExportService|None=None) -> web.Application`; async `stream_file(request, file, *, size: int, filename: str, content_type: str, cache_control: str) -> web.StreamResponse`. file은 호출자가 카탈로그 잠금 아래 열고 전송 종료 후 닫는다.

- [ ] 설계의 각 경로에 HTTP 계약 테스트 작성: 날짜 포함/제외, limit20/100, cursor 필터 결합, 영상 없는 날짜, 오늘 gap 필드, JSON 알 수 없는 필드, 오류 코드, POST 중복 방지, DELETE 반복, GET/HEAD/Range와 attachment.
- [ ] `uv run pytest tests/test_daily_http_api.py tests/test_media_files.py tests/test_http_api.py -q`로 신규 API 테스트 실패와 기존 계약 유지를 확인한다.
- [ ] 기존 Range/파일 전송만 공용 helper로 추출하며 v1의 복수 Range 처리 의미를 유지한다. UUID 카탈로그 조회로 v2 경로를 추가하고 요청 경로를 파일명으로 직접 사용하지 않는다.
- [ ] export/content/HLS/TS 응답 헤더, Range206/416, HEAD 빈 본문, 요청 중 만료/삭제, 상대 경로 변환 계약을 검증한다. 서로 다른 recording_id/segment_id 조합으로 다른 날짜 파일을 가져올 수 없어야 한다.
- [ ] 본 설계의 필드 정의·오늘 다운로드 제한·모든 미디어 하위 경로를 계약 문서에 기록한다. 백엔드가 구현할 화면 API를 이 저장소에 추가하지 않는다.
- [ ] 해당 테스트 및 v1 회귀 통과 확인, task 단위 커밋.

### Task 7: 생명주기·설정·운영 상태·회귀

**Files:** 수정 app.py, settings.py, .env.example, deploy/compose.server.yaml, README.md, docs/ARCHITECTURE.md, docs/REMAINING_WORK.md; 새 tests/test_daily_runtime.py; 수정 tests/test_app.py, tests/test_settings.py.

**Interfaces:** Settings에 `daily_recording_enabled: bool=False`, `daily_segment_seconds: int=10`, `max_daily_storage_bytes: int|None=None`, `min_free_storage_bytes: int=5*1024**3`, `daily_export_cache_hours: int=24`, `max_daily_export_jobs: int=8`, `max_daily_pending_seconds: int=30`를 추가한다. DailyRecordingService는 이 설정을 사용한다.

- [ ] 날짜 기능 opt-in·필수 용량 설정 검증, 기존 환경에서 기존 서비스만 실행, 활성화 시 이벤트/MJPEG/날짜 서비스 동시 실행, 정상 종료 및 중단 재시작 테스트 작성.
- [ ] `uv run pytest tests/test_daily_runtime.py tests/test_app.py tests/test_settings.py -q` 실패 확인.
- [ ] recording→daily→exports 순서로 시작하고 반대로 종료한다. 공용 SQLite는 모든 날짜 작업이 끝난 뒤 기존 RecordingService에서 닫는다. 기존 stats·HTTP listener·WSS 설정을 유지한다.
- [ ] 날짜 저장 장애/인코딩 지연/작업 상한/마지막 완료 시각을 상태와 로그에 노출한다. 현재 CPU2/메모리3GiB 제한은 유지한 상태로 실측하고 근거 없이 증설하지 않는다.
- [ ] `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` 전부 통과. 운영 설정은 실제 프로파일·볼륨 결정 전 자동 활성화하지 않는다. task 단위 커밋.

### Task 8: 서버·브라우저·백엔드 연동 인수 검증

**Files:** 새 tests/test_daily_smoke.py; 수정 docs/VALIDATION.md, docs/SERVER_DEPLOYMENT.md, docs/DAILY_RECORDING_API_CONTRACT.md.

**Interfaces:** 기존 WSS JPEG 입력과 새 v2 HTTP/HLS를 사용한다. 백엔드/프론트 담당자에게 계약과 테스트용 recording_id를 제공한다.

- [ ] 빠른 실제 인코딩 smoke 테스트: 날짜 경계·단절 포함 입력→목록 한 건/날짜→HLS→closed→exports→단일MP4→Range→재시작→삭제. 테스트용 시계를 사용한 검증과 실시간24시간 검증을 구분한다.
- [ ] 서비스 서버에서 격리된 데이터 경로·컨테이너로 카메라 수별 연속 녹화, 이벤트 동시 인코딩, MJPEG, export, 강제 재시작, 디스크 부족을 검증한다. 실제 장비 입력과 합성 입력 결과를 구분한다.
- [ ] 최소24시간 + 자정 전환 실측: 입력/저장FPS, 영상 길이·gap, CPU/메모리, 조각 대기 지연, GiB/day, export 시간/추가 공간 기록. 단일 시점 CPU 수치를 지속 처리 가능 증거로 사용하지 않는다.
- [ ] 현재 약70G 여유는 예시1080p30의 원본+하루export에 부족할 수 있다. 측정한 카메라 수×일일 저장량×보관일 + export/임시/기존클립/예약을 확보하는 볼륨을 결정한다. 확보 전 상시 운영 활성화는 완료로 보고하지 않는다.
- [ ] 백엔드 담당자가 인증된 게이트웨이와 새 화면 API를 연결하고 프론트 담당자가 HLS 플레이어·날짜 시간축·부분 저장·오늘 다운로드 제한을 연결한다. 미디어 담당자는 경로·헤더·샘플·오류 검증을 지원한다.
- [ ] 날짜가 선택 화면/상세 시각에 유지되고 UTC→한국 시간 변환 후 자정의 날짜도 맞는지 확인한다. HH:mm:ss만 표시된 기존 혼동을 영상 소실로 처리하지 않는다.
- [ ] 대상 브라우저에서 오늘의 누적 재생·시간 탐색, 누락 시간 표시, 지난 날짜 처음/중간/끝, 수GB MP4 다운로드와 Range 재시작을 확인한다. 브라우저는 보통 별도 HLS 플레이어 연동이 필요하며 HTML video URL만 바꿔 완료했다고 보지 않는다.
- [ ] 합성/실장비, 단위/통합/브라우저, 계획/완료를 구분하여 검증 기록을 작성한다. 그 뒤 기존 서비스와 분리 검증한 변경을 배포하고 완료 조건을 다시 확인한다.

## 자체 검토

- 사용자 기능8항목 모두 Task1~8에 연결했다. 오늘 구간 재생과 단일 하루 다운로드는 별도 경로다.
- 초안의 오늘 재생 프로토콜/10분 분할/없음 날짜 표현을 이번 설계에서는 HLS/10초 조각/closed+none으로 제안했다. 확정 사항으로 보고하지 않는다.
- 저장 공간·화질·오늘 재생 지연 목표·브라우저 게이트웨이는 코드만으로 확정할 수 없어 운영 실측/협업 완료 조건에 명시했다.
- 기존 미커밋 운영 파일과 v1 계약은 보존한다. 새 작업은 미디어 서비스 범위에 한정한다.
- 구현·서버 지속 녹화·실제 백엔드 통합 테스트는 이 계획 작성으로 완료되지 않는다.
