# ajin-camera-media-service

카메라 엣지의 JPEG/WSS 수신, HTTP MJPEG 실시간 전달, 이벤트 전후 MP4 저장과 백엔드 제어 API를 제공하는 Python 서비스입니다.

백엔드 연동 규격은 [BACKEND_API_CONTRACT.md](docs/BACKEND_API_CONTRACT.md)에 있습니다. WHEP/WebRTC 송출은 제공하지 않습니다.

## 실행

Python 3.13과 FFmpeg(libx264 포함)가 필요합니다. `uv sync --frozen --extra dev`로 개발 의존성을 설치합니다. `.env.example`은 설정 예시이며 프로그램이 자동으로 읽지는 않습니다. 환경 변수를 프로세스에 주입하세요.

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

이벤트 최초 접수 시각과 당시 녹화 설정으로 구간을 고정합니다. 영상 구간은 서버 프레임 수신 시각 기준입니다. 실시간 조회 여부와 관계없이 버퍼를 유지하며, 이벤트 구간만 FFmpeg로 인코딩합니다.

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
