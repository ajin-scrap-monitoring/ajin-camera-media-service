# 검증 기록

## 2026-09-20 HTTP API·이벤트 클립 구현

Windows / Python 3.13.11에서 수행했다.

| 검증 | 결과 |
| --- | --- |
| `uv sync --frozen --extra dev --python .venv/Scripts/python.exe` | 성공 |
| `ruff check . --no-cache` | 통과 |
| `ruff format --check . --no-cache` | 30개 Python 파일 통과 |
| 전체 `pytest` | 82 passed |
| `uv build --python .venv/Scripts/python.exe --out-dir .test-dist` | sdist 및 wheel 생성 성공 |

로컬 기존 캐시 디렉터리의 권한 문제를 피하기 위해 pytest에는 `-p no:cacheprovider --basetemp=./.test-final2`를 지정했다. 테스트 범위는 전체 `tests/`이며 기존 ingest 회귀 테스트를 포함한다.

### 새로 검증한 동작

- 인증 헤더 없는 백엔드 API 호출, 설정 값·JSON 타입·중복 키·알 수 없는 필드 검증.
- 설정과 이벤트 중복 기록의 재시작 후 유지, 진행 중 작업의 명시적 실패 전환.
- 실제 WebSocket으로 JPEG 수신 → 이벤트 전후 수집 → FFmpeg H.264/MP4 생성 → HTTP 조회 → 파일 삭제.
- 생성 MP4의 H.264 디코딩, 해상도, fast-start 배치와 프레임 시간 간격 유지.
- MJPEG 응답의 JPEG 원본 바이트, multipart 헤더, 사용 가능한 영상 없음 처리.
- 클립 목록 페이지네이션 및 잘못된 커서, HTTP Range·다운로드.
- 삭제 반복 호출, 진행 중 삭제 거부, 삭제 실패 시 상태 유지, 자동 만료 및 중복 키 유지.
- 바이트 한도, 활성 이벤트 프레임 보호, 잘못된 JPEG의 인코딩 실패.
- 부분 파일 쓰기 실패의 잔여 파일 정리, 일시적 SQLite 결과 저장 오류의 재시도.
- 느린 디스크 쓰기 반영, 계속 도착하는 미래 프레임 때문에 클립 생성이 무기한 대기하지 않는 처리 경계.
- 버퍼 큐에서 폐기한 프레임의 처리 완료 반영, 성공한 만료 파일 정리의 불필요한 반복 방지.

인코딩 테스트는 개발 의존성 imageio-ffmpeg의 실행 파일과 Pillow로 만든 JPEG를 사용한다. 테스트에서 FFmpeg를 모의 처리하지 않고 실제 파일 생성·디코딩을 검사한다. 저장소 장애 복구 등 특정 오류 테스트만 해당 경계에 장애를 주입한다.

### 검증하지 않은 범위

- 실제 웹캠·라즈베리파이 5, 상대 백엔드 및 대시보드 연동.
- 실제 1080p 30fps·다중 카메라·동시 시청자 부하 및 운영 성능.
- 운영 서버의 FFmpeg 빌드, 프록시·TLS·볼륨 권한, 대상 브라우저 재생.
- Docker 이미지 빌드: 로컬 Docker daemon에 연결할 수 없어 실행하지 못했다. Python sdist/wheel 빌드는 성공했다.
- 전원 장애와 파일시스템 손상, 장시간 디스크 고갈.

배포 전 검증 항목은 REMAINING_WORK.md를 참조한다. 로컬 테스트 통과가 위 항목의 통과를 뜻하지는 않는다.

## 백엔드 API 토큰 제거

백엔드 API 인증과 토큰 기반 활성화 조건을 제거했다. HTTP API와 이벤트 녹화는 항상 시작하며 기본 주소는 127.0.0.1:8889다. 엣지 WebSocket 인증은 유지한다. 목록 커서 검증용 키는 서비스가 SQLite에 자동 생성·보존하고 외부에 공유하지 않는다.

제거 후 검증: 전체 83개 테스트 통과, Ruff 검사·포맷 통과. 토큰 없는 실제 HTTP API 및 이벤트 MP4 생성·삭제, 재시작 후 커서 유효성 유지까지 확인했다.
