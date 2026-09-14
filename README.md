# ajin-camera-media-service

카메라 엣지에서 전송한 JPEG 프레임을 인증된 WebSocket으로 수신하고, 카메라별 최신
프레임 한 장을 프로세스 메모리에 유지하는 Python 서비스입니다.

현재 구현 범위는 **ingest와 단일 활성 세션 관리**입니다. 녹화, 디스크 저장, 브라우저 전달
API, 이벤트 클립 조회는 아직 구현되지 않았습니다. 상세한 현재 구조와 제안 구조는
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), 우선순위와 완료 조건은
[`docs/REMAINING_WORK.md`](docs/REMAINING_WORK.md)를 참고하세요.
검증 범위는 [검증 기록](docs/VALIDATION.md)에 있습니다.

## 저장소 관계

- 이 저장소: 미디어 수신, 세션 관리, 최신 프레임 및 향후 미디어 저장/전달 계층
- 카메라 엣지: `../ajin-edge-platform/services/camera-edge`
- 측정 백엔드/대시보드: 다른 팀 소유. 인증·이벤트·브라우저 계약은 합의 후 확정

엣지의 `CAMERA_ID=cam-1`이라면 `MEDIA_WSS_URL`은 배포 주소와 이 서비스의 경로를
조합한 `wss://<host>/api/v1/cameras/cam-1/stream` 형태여야 합니다. 엣지는 JPEG 한 장을
WebSocket binary message 하나로 전송하고 `Authorization: Bearer <token>` 헤더를 사용합니다.

## 빠른 시작

요구 사항은 Python 3.13과 `uv`입니다.

```bash
uv sync --frozen --extra dev
```

`.env.example`을 참고해 환경 변수를 설정합니다. `EDGE_AUTH_SECRET`은 카메라 ID와 토큰의
JSON 객체이며 비어 있으면 실행이 거부됩니다. 실제 토큰과 인증서 키는 저장소에 커밋하지
마세요.

```bash
EDGE_AUTH_SECRET='{"cam-1":"local-only-token"}' \
uv run python -m camera_media_service.main
```

기본 주소는 `0.0.0.0:8443`, 경로는
`/api/v1/cameras/{camera_id}/stream`입니다. `TLS_CERT_PATH`와 `TLS_KEY_PATH`가 모두 비어
있으면 서버는 평문 WebSocket으로 동작하므로 신뢰된 내부망 또는 TLS 종료 reverse proxy
뒤에서만 사용해야 합니다. 외부에서 직접 연결하는 WSS 구성은 두 경로를 모두 지정해야
합니다.

## 로컬 검사

기존 검사 명령은 다음과 같습니다.

```bash
uv sync --frozen --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 컨테이너

```bash
docker build -t ajin-camera-media-service .
docker run --rm -p 8443:8443 \
  -e EDGE_AUTH_SECRET='{"cam-1":"replace-in-deployment"}' \
  ajin-camera-media-service
```

이미지는 비권한 사용자 `cameramedia`(UID 10002)로
`python -m camera_media_service.main`을 실행합니다. 위 예시는 기능 확인용 평문 WebSocket
구성이며 하드웨어 또는 운영 배포 승인 사례가 아닙니다.

## 주요 설정

| 환경 변수 | 기본값/제약 | 현재 사용 여부 |
| --- | --- | --- |
| `EDGE_AUTH_SECRET` | 비어 있지 않은 JSON 객체 | handshake 인증에 사용 |
| `MEDIA_INGEST_HOST` | `0.0.0.0` | listener bind |
| `MEDIA_INGEST_PORT` | `8443` | listener bind |
| `MEDIA_INGEST_PATH` | `/api/v1/cameras/{camera_id}/stream` | route 매칭 |
| `MAX_FRAME_BYTES` | `4194304` | WebSocket 및 ingest 크기 제한 |
| `TLS_CERT_PATH`, `TLS_KEY_PATH` | 함께 설정하거나 함께 비움 | 직접 TLS 선택 |
| `MEDIA_STORAGE_PATH` | `/var/lib/camera-media` | **파싱만 함; 디스크 저장 미구현** |
| `STATS_INTERVAL_SECONDS` | `60` | **파싱만 함; 통계 로깅 미구현** |
| `LOG_FORMAT` | `json`만 허용 | **검증만 함; 구조화 로깅 미구현** |
| `WEB_APPLICATION_URL`, `WEB_APPLICATION_CREDENTIAL` | 현재 빈 값만 허용 | **연동 미구현** |

## 현재 ingest 계약

- 정확한 경로가 아니면 HTTP `404`, 카메라/토큰 인증 실패는 HTTP `401`
- WebSocket compression 비활성화
- 한 message가 한 JPEG이며 SOI(`FF D8`)와 EOI(`FF D9`)만 검사
- text message는 close `1003`, JPEG 형식 오류는 `1007`, 크기 초과는 `1009`
- 같은 카메라의 새 연결은 이전 연결을 close `1008`로 교체
- 서버는 ingest 연결로 응답 프레임이나 ACK를 전송하지 않음

이 계약의 JPEG 검사는 완전한 디코딩이나 이미지 무결성 검증이 아닙니다.

## 문서

- [아키텍처](docs/ARCHITECTURE.md)
- [남은 작업과 완료 조건](docs/REMAINING_WORK.md)
- [검증 범위](docs/VALIDATION.md)

## 이용 조건

이 Repository는 코드 검토와 참고를 위해 Public으로 제공하며 프로젝트 소스 코드에 별도 라이선스를 부여하지 않는다.
외부 라이브러리에는 각 라이선스가 적용됩니다.
