# ajin-camera-media-service

2026-10-30 연동 PR #1은 main `1bcce51`에 병합하고 운영 서버에 배포했습니다(한국 시간2026-10-31 검증 완료). 요청별 구간 추출·카메라 상태·실제 파일 정보·삭제 수정은 [추가 계약](docs/INTEGRATION_HANDOFF_2026-10-30.md), 담당자 회신은 [회신 문서](docs/INTEGRATION_REPLY_2026-10-30.md)를 참조하세요.

카메라 엣지의 JPEG/WSS 수신, HTTP MJPEG 실시간 전달, 이벤트 전후 MP4와 한국 날짜별 상시 녹화/HLS/하루 MP4 다운로드 API를 제공하는 Python 서비스입니다.

백엔드 연동 규격은 [BACKEND_API_CONTRACT.md](docs/BACKEND_API_CONTRACT.md)에 있습니다. WHEP/WebRTC 송출은 제공하지 않습니다.

동료에게 전달할 날짜 녹화 연동 문서는 [DAILY_RECORDING_API_GUIDE.md](docs/DAILY_RECORDING_API_GUIDE.md)입니다. 날짜 기능은 기본 비활성이며 아래 운영 설정으로 활성화합니다.

## 실행

Python 3.13과 FFmpeg(libx264 포함)가 필요합니다. `uv sync --frozen --extra dev`로 개발 의존성을 설치합니다. `.env.example`은 설정 예시이며 프로그램이 자동으로 읽지는 않습니다. 환경 변수를 프로세스에 주입하세요.

운영과 CI는 Dockerfile의 Debian trixie/시스템 FFmpeg7.1 환경으로 검증합니다. Ubuntu FFmpeg6.1.1은 영상 길이·손상 입력 검증 시험에서 차이가 확인되어 지원 환경으로 검증되지 않았습니다. Linux 개발 번들7.0.2도 TS 읽기 충돌이 있어 시험에는 `IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg`를 지정합니다.

PowerShell 로컬 실행 예시:

```powershell
$env:EDGE_AUTH_SECRET='{"camera-main":"local-edge-token"}'
$env:MEDIA_STORAGE_PATH='./runtime/media'
$env:MEDIA_API_HOST='127.0.0.1'
$env:MEDIA_API_PORT='8889'
$env:FFMPEG_PATH='ffmpeg'
uv run python -m camera_media_service.main
```

예시 엣지 토큰은 운영에서 별도로 교체합니다. 백엔드 HTTP API에는 인증 토큰이 필요하지 않습니다.

- 엣지 수신: `ws://127.0.0.1:8443/api/v1/cameras/camera-main/stream`
- 백엔드 API: `http://127.0.0.1:8889/api/v1`
- 백엔드 HTTP API: 인증 헤더 없이 호출하며 기본적으로 loopback에 바인딩합니다.
- 엣지 외부 연결은 직접 TLS 또는 reverse proxy로 WSS를 제공합니다.

HTTP API와 이벤트 녹화는 서비스 시작 시 항상 활성화됩니다. FFmpeg가 없거나 libx264를 지원하지 않으면 클립 생성이 실패합니다.

## 구현된 API

| 메서드 | 경로(`/api/v1` 기준) | 기능 |
| --- | --- | --- |
| GET / PUT | `/cameras/{camera_id}/recording-policy` | 녹화 설정 조회·저장 |
| GET | `/cameras/{camera_id}/live` | HTTP MJPEG |
| POST | `/clips` | 이벤트 저장 접수·중복 방지 |
| GET | `/clips` | 필터·커서 기반 목록 |
| GET | `/clips/{clip_id}` | 상태·누락 정보 |
| GET | `/clips/{clip_id}/content` | H.264/MP4, 단일 Range, 다운로드 |
| DELETE | `/clips/{clip_id}` | 파일 삭제, 중복 기록 유지 |

이벤트 최초 접수 시각과 당시 녹화 설정으로 구간을 고정합니다. 영상 구간은 서버 프레임 수신 시각 기준입니다. 실시간 조회 여부와 관계없이 버퍼를 유지합니다. 날짜 기능을 켜면 별도의 인코더가 짧은 H.264/TS 조각을 계속 저장하고, 조회 시 날짜당 한 건으로 제공합니다.

## 날짜별 녹화

이벤트 요청 `POST /api/v1/clips`에 `event_at`(UTC 밀리초 Z)을 추가하면 해당 시각 ± 기존 전후 설정으로 상시 녹화에서 추출합니다. 준비된 조각을 재인코딩 없이 MP4로 묶습니다. 기준 시각 영상이 없으면 404, 미래면 400, 처리 중이면 409입니다. 전후 부족·미래는 현재 확보분까지만 제공하고 부분 영상과 누락을 표시합니다. 새 녹화의 키프레임 간격은 1초이며 절단 경계에 최대 1초 추가를 허용합니다. 기존 10초 GOP 자료에서 이 한도를 만족하지 못하면 명시적으로 실패합니다. `event_at` 없는 호출은 기존 수집·인코딩 동작을 유지합니다. 상세 JSON과 오류는 백엔드 규격의 5.1절을 참조하세요.

`DAILY_RECORDING_ENABLED=true`와 양수 `MAX_DAILY_STORAGE_BYTES`를 함께 지정합니다. 기본 프로파일은1920×1080/30fps이며 입력을 정규화하고 한 날짜 안에서 출력 프로파일을 유지합니다. 변경한 프로파일은 다음 날짜부터 적용합니다. 같은 서버의 이벤트 인코딩과 CPU를 공유하므로 실제 영상으로 처리량과 저장량을 측정한 후 사용하세요.

- 한국 자정마다 날짜를 전환합니다. 오늘은 완료된 TS 조각으로, 지난 날짜는 HLS와 단일 MP4로 제공합니다. 날짜 마감 뒤 백그라운드에서 자동으로 MP4를 준비합니다.
- 실제 누락을 `gaps`, `has_gaps`, 종료 날짜의 `coverage=partial/none`으로 반환합니다. 미래 시간과 아직 처리 중인 꼬리는 누락으로 표시하지 않습니다.
- 저장된 H.264 조각을 재인코딩 없이 MP4로 합치고, 전체 프레임 디코딩 검사·디스크 반영·시간 매핑 저장 후 중복 TS를 정리합니다. 완성된 MP4가 보관 기간 동안 원본입니다. 다운로드 요청은 기존 파일을 바로 전송하며 하루 영상 생성 작업을 시작하지 않습니다.
- 하루 MP4의 인덱스는 파일 끝에 둬서 faststart 인덱스 이동에 필요한 전체 파일 재기록을 피합니다. 다운로드는 HTTP Range, 날짜 재생은 HLS를 사용합니다. 이벤트 클립은 faststart를 유지합니다. 추가 저장 공간은 이미 생성된 바이트를 제외한 나머지만 예약하고, 파일 완성 후 읽기 검증 중에는 추가 쓰기 예약을 해제합니다.
- 지난 날짜 HLS는 요청한 짧은 구간만 MP4에서 스트림 복사합니다. 재생용 TS 캐시는 최대64MiB/60초, 동시 작업2개/대기 포함8개로 제한합니다. 이벤트 시각 추출은 오늘 TS 또는 보관 MP4를 사용합니다. 누락 시간은 MP4 길이에 포함하지 않으므로 상세 API의 시간 매핑과 함께 표시합니다.
- API 목록·JSON·오류·Range·게이트웨이 경로는 [연동 가이드](docs/DAILY_RECORDING_API_GUIDE.md)를 참조하세요.

| 환경 변수 | 기본값 / 역할 |
| --- | --- |
| `DAILY_RECORDING_ENABLED` | `false`, v2 서비스 활성화 |
| `DAILY_ARCHIVE_ENABLED` | `true`, 종료 날짜 자동 MP4 준비·중복 TS 정리. `false`는 배포 점검용 일시 중지 |
| `MAX_DAILY_STORAGE_BYTES` | 기본 미지정, 활성화 시 필수. 날짜 조각+MP4+재생 캐시+임시 파일 예산 |
| `MIN_FREE_STORAGE_BYTES` | `5368709120`, 최소 남겨 둘 공간(5GiB) |
| `DAILY_WIDTH`, `DAILY_HEIGHT`, `DAILY_FPS` | `1920`, `1080`, `30` |
| `DAILY_SEGMENT_SECONDS` | `10`, 조각 길이 목표(2..60초) |
| `MAX_DAILY_PENDING_SECONDS` | `30`, 미처리 시간 상한 |
| `DAILY_EXPORT_CACHE_HOURS` | 이전 환경 호환용. 보관 MP4는 날짜 `expires_at`까지 유지 |
| `MAX_DAILY_EXPORT_JOBS` | 이전 환경 호환용. 자동 MP4 준비는 한 번에1개 |

보관 기간 기본30일은 카메라별 v2 정책으로 정합니다. MP4 준비 중 TS와 MP4가 함께 존재할 추가 공간이 필요하며, 부족하면 TS를 유지하고 자동 재시도합니다. 검증은 백그라운드에서 디코더2스레드로 수행하므로 CPU·디스크 읽기 부하는 남습니다. 버퍼·이벤트·DB는 날짜 예산에 포함하지 않습니다. 날짜 예산 도달 시 기존 자료를 임의로 조기 삭제하지 않고 새 저장 누락을 보고합니다. 새 프로파일로 기존 날짜 MP4를 재인코딩하지 않습니다.

## 주요 환경 변수

| 변수 | 기본값 / 역할 |
| --- | --- |
| `EDGE_AUTH_SECRET` | 카메라 ID→엣지 토큰의 JSON 객체, 필수 |
| `MEDIA_INGEST_HOST`, `MEDIA_INGEST_PORT` | `0.0.0.0`, `8443` |
| `MEDIA_INGEST_PATH` | `/api/v1/cameras/{camera_id}/stream` |
| `TLS_CERT_PATH`, `TLS_KEY_PATH` | 둘 다 지정하면 ingest 직접 TLS |
| `MEDIA_API_HOST`, `MEDIA_API_PORT` | `127.0.0.1`, `8889` |
| `MEDIA_STORAGE_PATH` | `/var/lib/camera-media`, 전용 영속 저장 디렉터리 |
| `FFMPEG_PATH` | `ffmpeg`, 실행 파일 경로 |
| `MAX_BUFFER_BYTES` | `2147483648`, 전체 카메라 JPEG 버퍼 한도 |
| `MAX_CLIP_JOBS` | `32`, 동시 미완료 작업 한도 |
| `MAX_FRAME_BYTES` | `4194304`, JPEG 메시지 한도 |
| `STATS_INTERVAL_SECONDS` | `60`, ingest 통계 출력 주기 |
| `LOG_FORMAT` | `json` |

## 저장 및 운영 조건

- 동일 저장 디렉터리를 사용하는 미디어 서비스는 **한 프로세스만** 실행합니다.
- SQLite에 설정·작업·버퍼 인덱스를 저장하고, JPEG와 MP4는 서비스 전용 디렉터리에 저장합니다. 내부 파일을 직접 변경하지 마세요.
- 일반 버퍼는 최근 최대 300초를 유지합니다. 바이트 한도를 먼저 만나면 보호되지 않은 오래된 프레임부터 제거하므로 300초 전체를 항상 보장하지는 않습니다.
- 작업 구간의 프레임은 인코딩 완료까지 보호합니다. 보호된 프레임만으로 한도를 채우면 새 버퍼 쓰기가 실패하며 로그에 기록되고 클립에 누락이 생길 수 있습니다.
- 인코딩은 한 번에 1개, FFmpeg 스레드는 2개입니다. 작업당 인코딩 제한 시간은 900초입니다.
- 버퍼 바이트 한도는 완료된 MP4와 SQLite를 포함하지 않습니다. 완료 파일은 설정된 기간 후 만료 처리합니다. 디스크 여유 공간을 별도로 확보해야 합니다.
- 재시작 시 완료 파일과 설정은 유지하고, 진행 중 작업은 `JOB_INTERRUPTED` 실패로 전환합니다. 같은 이벤트 ID로 재시도해도 새로 녹화하지 않습니다.
- 삭제·만료 메타데이터와 이벤트 중복 기록은 현재 자동 정리하지 않습니다. 문서의 최소 365일 유지 조건을 충족하며, 장기 운영 시 DB 크기를 모니터링합니다.
- HTTP API는 내부 통신용입니다. 컨테이너 내부 바인딩이 필요하면 `MEDIA_API_HOST=0.0.0.0`을 사용하고 호스트 포트는 loopback에 제한합니다.

## 컨테이너

이미지는 FFmpeg를 포함하며 비권한 사용자(UID 10002)로 실행합니다. 영속 볼륨을 `/var/lib/camera-media`에 연결하고 UID 10002에 쓰기 권한을 부여하세요.

```bash
docker build -t camera-media-service .
```

실행 시 `EDGE_AUTH_SECRET`, `MEDIA_API_HOST=0.0.0.0`을 주입하고 API 포트를 `127.0.0.1:8889:8889`로 매핑합니다. 엣지 수신 포트는 환경의 TLS 구성에 맞춰 공개합니다.

## 검증

```bash
uv sync --frozen --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

개발 테스트는 imageio-ffmpeg의 번들 실행 파일과 생성한 JPEG를 사용하여 실제 H.264/MP4 인코딩·디코딩을 검증합니다. 운영 실행은 `FFMPEG_PATH`를 사용합니다. 실제 라즈베리파이·웹캠·백엔드·브라우저 연동과 운영 부하 검증은 별도로 수행해야 합니다.

## 문서

- [백엔드 통신 규격](docs/BACKEND_API_CONTRACT.md)
- [아키텍처](docs/ARCHITECTURE.md)
- [검증 기록](docs/VALIDATION.md)
- [남은 운영 검증](docs/REMAINING_WORK.md)
- [서버 A 배포와 백엔드 연결](docs/SERVER_DEPLOYMENT.md)

## 이용 조건

이 Repository는 코드 검토와 참고를 위해 Public으로 제공하며 프로젝트 소스 코드에 별도 라이선스를 부여하지 않는다. 외부 라이브러리에는 각 라이선스가 적용됩니다.
