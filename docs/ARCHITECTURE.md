# 카메라 미디어 서비스 아키텍처

이 문서는 코드에 존재하는 **현재 구현**과 향후 검토할 **PROPOSED(제안)** 구조를 분리한다.
제안 항목은 구현 완료나 다른 팀과의 계약 확정을 의미하지 않는다.

## 현재 구현

### 런타임 흐름

1. `camera_media_service.main`이 환경 변수로 `Settings`를 만들고 `create_app`을 호출한다.
2. `create_app`이 프로세스 로컬의 `BearerAuthenticator`, `ConnectionRegistry`,
   `LatestFrameStore`, `IngestService`를 생성한다.
3. `WebSocketServer`가 설정된 host/port에서 요청을 받고, 설정 경로의
   `{camera_id}`를 추출한다.
4. handshake 중 `Authorization: Bearer <token>`을 카메라별 설정 토큰과
   constant-time 비교한다. 경로 불일치는 `404`, 인증 실패는 `401`이다.
5. 승인된 연결에 UUID connection ID를 부여한다. 같은 camera ID에는 활성 연결 하나만
   허용하며, 새 연결이 오면 기존 socket을 code `1008`로 닫는다.
6. `IngestService`는 각 binary message를 프레임 하나로 취급한다. 크기와 JPEG 시작/끝
   marker를 검사한 뒤 현재 연결인지 다시 확인하고 메모리 store에 넣는다.
7. `LatestFrameStore`는 camera ID별 `(generation, bytes)` 하나만 보관한다. 새 프레임은
   이전 프레임을 교체하며 put/get 모두 bytes 복사본을 사용한다.

연결 종료와 protocol error 시 registry 항목은 connection ID가 일치할 때만 해제된다.
따라서 교체된 이전 연결의 종료가 새 연결의 등록을 삭제하지 않는다.

### 모듈 책임

| 모듈 | 구현된 책임 |
| --- | --- |
| `settings.py` | 환경 변수 파싱과 범위/조합 검증 |
| `auth.py` | camera ID별 bearer token 인증 |
| `registry.py` | camera ID별 단일 활성 연결 소유권 |
| `ingest.py` | binary/JPEG/크기 검사, 기존 연결 교체, 최신 프레임 저장 |
| `store.py` | camera ID별 최신 한 프레임과 증가 generation 보관 |
| `app.py` | route/auth handshake, WebSocket listener, 선택적 직접 TLS |
| `main.py` | 설정 로드와 서버 수명주기 |

모든 registry와 frame 상태는 단일 프로세스 메모리다. 재시작하면 사라지며 여러 replica
사이에 공유되지 않는다. `wait_for_generation`은 내부 테스트/호출용 polling helper일 뿐
외부 HTTP/WebSocket API가 아니다.

### WebSocket ingest 계약

- 경로: 기본 `/api/v1/cameras/{camera_id}/stream`; 설정으로 변경 가능
- 인증: handshake의 `Authorization: Bearer <token>`; 토큰 맵은
  `EDGE_AUTH_SECRET={"camera-id":"token"}`
- payload: WebSocket binary message 하나당 JPEG 한 장
- 서버→엣지 application message/ACK: 없음
- compression: 없음
- 최대 message/frame: `MAX_FRAME_BYTES`(기본 4 MiB)
- validation: 처음 두 bytes `FF D8`, 마지막 두 bytes `FF D9`만 확인
- close: text `1003`, JPEG marker 오류 `1007`, 제한 초과 `1009`, 같은 카메라의 이전 연결
  교체 `1008`
- TLS: 인증서/키가 모두 있으면 직접 TLS, 모두 없으면 평문 listener. 외부 WSS는 직접 TLS
  또는 신뢰 경계의 reverse proxy가 제공해야 한다.

카메라 송신 구현은 sibling 경로
`../ajin-edge-platform/services/camera-edge`에 있다. 해당 클라이언트는 완성된 카메라별
WSS URL을 설정으로 받고 bearer header와 binary JPEG를 전송한다.

### 설정되어 있지만 동작하지 않는 항목

- `MEDIA_STORAGE_PATH`/`Settings.media_root`: 값을 검증·보관하지만 파일을 쓰지 않는다.
- `STATS_INTERVAL_SECONDS`: 값을 검증·보관하지만 주기 통계를 출력하지 않는다.
- `LOG_FORMAT`: `json` 값만 허용하지만 구조화 로깅 파이프라인은 없다.
- `WEB_APPLICATION_URL`, `WEB_APPLICATION_CREDENTIAL`: 현재는 빈 값만 허용하며 어떤
  browser/backend 호출에도 사용하지 않는다.

즉 현재 서비스는 녹화기, ring buffer, clip archive 또는 browser streaming server가 아니다.

## PROPOSED: 남은 미디어 계층

아래 이름과 인터페이스는 설계 초안이다. 구현 전에 부하·보존 정책과 다른 팀 API 계약을
확정해야 한다.

### 1. 프레임 파이프라인 분리

- `FrameEnvelope(camera_id, received_at, generation, jpeg)`: ingest 이후 내부 불변 메시지.
- `LiveFrameHub`: 최신 프레임과 live consumer fan-out 담당. 느린 consumer가 ingest를
  block하지 않도록 bounded queue와 drop-latest/oldest 정책을 명시한다.
- `FrameValidator`: marker 검사에서 실제 JPEG decode/해상도 제한까지 강화할 수 있는 경계.

제안 내부 계약: ingest는 검증된 envelope를 hub에 한 번 publish하고, 저장과 live delivery는
독립 consumer가 된다. backpressure/drop/메모리 상한은 성능 시험으로 결정한다.

### 2. 브라우저 live delivery

- `BrowserSessionAuthorizer`: backend가 발급한 짧은 수명의 media ticket 검증.
- `LiveDeliveryEndpoint`: 인증된 browser session을 `LiveFrameHub`에 연결.
- `SessionRegistry`: edge ingest session과 분리된 browser consumer 수명주기/상한 관리.

**합의 필요:** WebSocket JPEG, MJPEG, WebRTC 중 프로토콜; URL과 메시지 metadata schema;
ticket 발급/검증 방식; camera/site 권한 모델; 재연결·frame drop semantics. 대시보드 팀은
browser client/UI를, 측정 백엔드 팀은 사용자 권한 및 ticket/event 계약을 소유한다. 이
저장소는 합의된 media-plane endpoint와 검증 adapter만 소유하는 방향을 제안한다.

### 3. 녹화와 보존

- `SegmentWriter`: 원본 프레임을 시간 구간 segment와 atomic metadata로 저장.
- `RingBufferIndex`: camera/time 범위 조회와 저장 용량 상한을 관리.
- `RetentionWorker`: 합의된 기간/용량 정책에 따라 안전하게 만료 처리.
- `StorageBackend` interface: 로컬 filesystem을 첫 구현으로 두되 객체 저장소 교체 경계를
  제공.

`MEDIA_STORAGE_PATH`는 이 계층이 실제 연결되고 시작 시 쓰기 가능성/여유 공간을 확인한
뒤에야 저장 위치 계약이 된다. 현재 기본값만으로 volume이나 녹화가 존재한다고 가정하지
않는다.

### 4. 이벤트 클립

- `EventClipRequest(event_id, camera_id, occurred_at, pre_roll, post_roll)`
- `ClipAssembler`: ring buffer에서 시간 범위를 고정 clip으로 materialize.
- `ClipCatalog`: event ID와 clip locator/checksum/status 매핑.
- `ClipQueryEndpoint`: 권한 확인 후 metadata 또는 제한된 download URL 반환.

**합의 필요:** event ID의 authority, timestamp/timezone와 clock skew, idempotency key,
pre/post-roll, 보존 책임, callback/polling 상태 모델. 이벤트 판단과 측정 데이터는 백엔드
소유이며 이 저장소에서 재구현하지 않는다.

### 5. 운영 경계

- 구조화 로그/metrics: 연결 수, 인증 실패, 수신 FPS/bytes, frame reject/drop, 저장 지연과
  용량을 camera ID 기반 저 cardinality label로 노출한다. 토큰과 원본 영상은 로그 금지.
- health/readiness: listener 생존과 저장소/의존 서비스 readiness를 분리한다.
- 배포/TLS: reverse proxy trust boundary, certificate rotation, graceful shutdown, resource
  limits, volume 권한을 배포 환경별로 검증한다.
- scale-out: registry/latest frame/ring index가 현재 process-local이므로 sticky routing 또는
  외부 coordination/storage 전략 없이는 다중 replica를 지원한다고 선언하지 않는다.

## 소유권 경계

| 영역 | 이 저장소 | 측정 백엔드/대시보드 팀 |
| --- | --- | --- |
| edge ingest wire contract | 서버 구현 및 검증 | 변경 시 영향 검토 |
| media 저장/clip 생성 | 제안상 소유 | event/timestamp/보존 요구 제공 |
| 사용자·사이트·카메라 권한 | 검증 adapter만 | 권한 source of truth와 ticket 발급 |
| browser media endpoint | 합의된 media-plane 구현 | client/protocol 요구와 UI |
| 측정/이벤트 도메인 로직 | 소유하지 않음 | 소유 |
| 배포 연결 | 서비스 이미지/health 요구 | 플랫폼 경계는 공동 합의 |

계약 변경은 최소한 camera ID 규칙, 시간 기준, 인증 주체, 오류/재시도 semantics, 데이터
보존 책임을 문서화하고 양 팀 contract test fixture로 고정한 뒤 진행한다.
