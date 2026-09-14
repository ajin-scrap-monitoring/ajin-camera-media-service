# 남은 작업

상태 표기의 `PROPOSED`는 설계 후보이며 확정 약속이 아니다. 우선순위는 현재 ingest를
안전하게 운영하고 다른 팀과 계약을 고정하는 순서다.

## P0 — 계약과 안전 경계

### PROPOSED: 브라우저 전달·인증 계약 결정

- 완료 조건: 대시보드/백엔드 담당자와 프로토콜(WebSocket JPEG/MJPEG/WebRTC), endpoint,
  메시지 schema, 짧은 수명 media ticket, camera/site 권한, 오류·재연결 semantics를 승인된
  문서와 양쪽 contract fixture로 고정
- 의존성: 대시보드 재생 요구, 백엔드 인증/인가 source of truth, 예상 동시 시청자와 지연 목표
- 비고: 현재 `WEB_APPLICATION_*`는 빈 값만 허용하므로 연동 구현으로 간주할 수 없음

### PROPOSED: 운영 TLS와 secret 배포 검증

- 완료 조건: 외부 WSS 또는 reverse proxy trust boundary가 명시되고, 인증서/토큰 rotation,
  secret 주입, non-root 파일 권한, graceful shutdown을 staging에서 검증
- 의존성: 배포 플랫폼, DNS/certificate 소유자, edge 재연결 정책

### PROPOSED: ingest resource/abuse 시험

- 완료 조건: 연결·카메라 수, FPS, 최대 JPEG, malformed/slow sender, reconnect storm에서
  CPU/RSS/latency 상한과 실패 방식이 합의된 기준을 충족
- 의존성: 실제 목표 부하와 운영 한도

## P1 — 기록과 이벤트 클립

### PROPOSED: recording/ring buffer

- 완료 조건: `MEDIA_STORAGE_PATH`를 실제 writer가 사용하고, crash-safe segment/index,
  camera/time 조회, disk quota, retention, 재시작 복구 및 손상 격리 테스트가 통과
- 의존성: codec/container, segment 길이, pre-roll, 보존 기간/용량, storage backend 결정
- 제외 확인: 현재 `LatestFrameStore`는 최신 한 장만 메모리에 두며 녹화가 아님

### PROPOSED: event clip lookup

- 완료 조건: 동일 event 요청이 멱등적이고, pre/post-roll clip 상태·checksum·locator를 권한
  있는 호출자만 조회하며 missing/expired/partial 범위를 명시적으로 반환
- 의존성: 백엔드의 event ID, camera mapping, UTC timestamp/clock-skew, callback 또는 polling
  계약. 측정/이벤트 판단 로직은 다른 팀 소유

## P1 — live delivery 구현

### PROPOSED: ingest와 consumer fan-out 분리

- 완료 조건: 느린 browser/recorder가 ingest를 block하지 않고, bounded queue/drop 정책과
  메모리 상한이 동시성 테스트로 검증됨
- 의존성: P0 브라우저 프로토콜, 지연/FPS 품질 목표

### PROPOSED: browser session lifecycle

- 완료 조건: 인증, 만료, 권한 철회, 연결 수 제한, disconnect cleanup, camera offline 상태가
  contract/integration test로 검증됨
- 의존성: backend ticket 검증과 dashboard client 구현

## P2 — 관측성과 배포 완성도

### PROPOSED: logs/metrics/health

- 완료 조건: 연결·인증·수신·reject/drop·저장 상태의 JSON log와 metrics가 비밀/영상 payload를
  노출하지 않고, liveness/readiness와 alert 기준이 staging에서 확인됨
- 의존성: 관측 플랫폼과 label/cardinality 정책
- 제외 확인: 현재 `STATS_INTERVAL_SECONDS`와 `LOG_FORMAT`은 runtime telemetry를 만들지 않음

### PROPOSED: 복구·용량·호환성 시험

- 완료 조건: process/host 재시작, full disk, readonly volume, index 손상, certificate rotation,
  edge/client version skew, retention race를 자동 시험하고 runbook을 작성
- 의존성: storage 및 배포 설계 확정

## 완료로 간주하지 않는 것

- 단위 테스트 통과만으로 실제 카메라·네트워크·브라우저 acceptance를 대체하지 않는다.
- Docker image build만으로 TLS, volume, resource limit, 롤백이 검증됐다고 보지 않는다.
- 설정 필드가 존재하는 것만으로 storage, 통계, web application integration이 구현됐다고
  보지 않는다.
