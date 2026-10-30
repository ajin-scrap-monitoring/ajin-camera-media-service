# 2026-10-30 미디어 연동 추가 계약

대상: 백엔드 담당자. 이 PR의 신규 기능이며 검토·병합·배포 후 운영 주소에서 사용한다. 시험 이미지 검증과 운영 배포를 구분한다. 기존 event_at/시각 없는 호출은 유지한다.

호스트 기본 주소 `http://127.0.0.1:8889`, 공유 네트워크 `http://camera-media-service:8889`. 내부 API에는 인증 토큰을 추가하지 않는다. 브라우저 인증·권한 검사·인증된 다운로드 경로는 백엔드 게이트웨이가 담당하며 상대 content_path를 변환한다. 실제 이메일·문자는 발송하지 않는다.

## R-10 날짜 삭제

기존 `DELETE /api/v2/daily-recordings/{recording_id}`를 사용한다. 동일 날짜의 원본 조각 정리·삭제·재삭제를 직렬화한다. 이미 사라진 서비스 소유 폴더는 성공이며 실제 권한/I/O 오류는 숨기지 않는다.

| 상황 | HTTP |
|---|---|
| 최초 삭제의 파일 정리 완료 / 완료 후 재삭제 | 204, 본문 없음 |
| 일부 정리 후 권한·I/O 실패 | 503 STORAGE_UNAVAILABLE |
| 실패 원인 해결 후 같은 DELETE 재시도 | 완료 시204 |
| 오늘·마감 중 / 원본 사용 작업 존재 | 409 RECORDING_ACTIVE / EXPORT_IN_PROGRESS |
| 없는 날짜 | 404 DAILY_RECORDING_NOT_FOUND |

논리 삭제는 새 콘텐츠 접근을 즉시 차단한다. 파일 정리가 실패해도 논리 삭제를 되돌리지 않는다. `GET /api/v2/daily-recordings/{recording_id}`는 삭제 상태도200으로 제공한다.

```json
{
  "status":"deleted",
  "deleted_at":"2026-10-30T02:00:00.000Z",
  "cleanup_state":"failed",
  "cleanup_completed_at":null,
  "cleanup_error":{"code":"STORAGE_ERROR","message":"Recording cleanup failed."},
  "space_reclaimed":null
}
```

cleanup_state는 pending/failed/complete다. 삭제/만료 전에는 정리 필드가 없을 수 있다. 완료 시 cleanup_completed_at은UTC 밀리초Z, cleanup_error는null이다. 재시작·백그라운드 관리 작업도 미완료 정리를 재시도한다. 취소된 요청의 파일 작업이 끝나기 전 같은 날짜의 정리 잠금을 풀지 않는다.

complete는 서비스 소유 파일 경로 정리 완료다. 열린 핸들·파일시스템 때문에 물리 블록 반환을 단정하지 않으며 space_reclaimed는null이다. 실제 회수량은 호스트 파일시스템에서 별도 측정한다. 503을 백엔드에서204로 바꾸지 않는다. 삭제/만료 콘텐츠 접근은410이다.

## R-07 요청별 전체 구간 추출

```http
POST /api/v1/clips
Content-Type: application/json

{
  "camera_id":"camera-main",
  "event_id":"collection-123-download-1",
  "start_at":"2026-10-30T01:00:00.000Z",
  "end_at":"2026-10-30T01:08:30.000Z"
}
```

- start_at/end_at은 필수 한 쌍이며 event_at과 함께 보낼 수 없다. 카메라 공통 전후 정책은 적용·변경하지 않는다. 요청별 전후 필드는 추가하지 않았으므로 호출자가 시작·종료 시각을 계산한다.
- 시각은UTC RFC3339 밀리초Z, 범위는 `[start_at,end_at)`, 길이는0초 초과/최대3600초다. 시작이 현재 이상이면400 REQUEST_TIME_IN_FUTURE다.
- 끝이 미래이거나 미처리 꼬리가 있으면 접수 시 확보된 완료 영상까지만 제공하고 gaps로 표시한다. 미래 영상을 기다리지 않는다. 시작 영상이 없어도 중간 확보분이 있으면 부분 추출한다.
- 전체 영상 없음404 RECORDING_NOT_FOUND_IN_RANGE, 아직 처리 중인 영상만 있으면409 RECORDING_NOT_READY, 상시 녹화 비활성503 RECORDING_NOT_ENABLED다.
- 오늘 TS 또는 보관 MP4에서 스트림 복사한다. 재인코딩은 없지만 읽기·키프레임 확인·짧은 MP4 구성 시간은 남는다. 형식 혼합으로 병합 불가하면 failed/EXPORT_INCOMPATIBLE_MEDIA다.
- 새 녹화는1초 GOP이며 앞뒤 최대1초 절단 확장을 허용한다. 긴 GOP로 만족하지 못하면 failed/RECORDING_BOUNDARY_UNAVAILABLE다. actual_*와 available_intervals가 실제 결과다.

신규 접수는202와Location, 기존 식별 필드 및 아래 필드를 반환한다. GET /clips/{clip_id} 상세도 같은 정보를 제공한다.

```json
{
  "request_mode":"time_range",
  "attempt_count":1,
  "attempt_started_at":"2026-10-30T02:00:00.000Z",
  "status":"processing",
  "requested_start_at":"2026-10-30T01:00:00.000Z",
  "requested_end_at":"2026-10-30T01:08:30.000Z",
  "coverage":"partial",
  "actual_start_at":"2026-10-30T01:00:15.000Z",
  "actual_end_at":"2026-10-30T01:08:30.000Z",
  "gaps":[{"start_at":"2026-10-30T01:00:00.000Z","end_at":"2026-10-30T01:00:15.000Z","reason_code":"RECORDING_UNAVAILABLE"}],
  "available_intervals":[{"start_at":"2026-10-30T01:00:15.000Z","end_at":"2026-10-30T01:08:30.000Z"}],
  "extraction_mode":"stream_copy",
  "size_bytes":null
}
```

접수 시 actual_*는 선택 범위이며 ready에서는 실제 절단 결과로 갱신된다. coverage complete/partial은 파일 성공 상태와 별개다. 누락은 CAMERA_DISCONNECTED 등 관측 사유, RECORDING_UNAVAILABLE, FUTURE_NOT_RECORDED로 표시한다.

상태는 processing/ready/failed로 조회한다. ready 상세의 content_path는 `/api/v1/clips/{clip_id}/content`, size_bytes는 실제 파일 바이트 정수다. 준비 전에는null이다. GET/HEAD content 및 단일 Range206을 지원한다. 목록과 HLS 재생만으로 클립을 생성하지 않으며 다운로드 때만 POST한다. 날짜 전체 다운로드는 별도 자동 준비된 하루 MP4 전송이다.

### 중복·재시도·보관

중복 키는 기존 `(camera_id,event_id)`다. 다운로드 요청마다 고유 event_id를 부여하고 업무 이벤트와의 연결은 백엔드에서 보관한다. 같은 키·같은 구간은200과 같은 작업, 다른 구간/다른 모드가 키를 쓰면409 IDEMPOTENCY_CONFLICT다. 기존 event_at/시각 없는 호출의 중복 처리 규칙은 유지한다.

failed 작업은 같은 본문에 `"retry_failed":true`를 추가해 명시적으로 재시도한다. 실패한 time_range만202로 새 시도를 접수하며 clip_id는 유지하고 attempt_count를 늘린다. 요청 범위와 최초 보관 정책은 유지하되 현재 영상을 다시 선택하므로 gaps는 달라질 수 있다. ready/processing/deleted/expired는 재생성하지 않고 기존 결과200을 반환한다. 재시도 접수 중 삭제되면409 RETRY_NOT_ALLOWED다. 타입/범위 오류400 INVALID_ARGUMENT, 작업 한도503 SERVICE_BUSY다.

결과는 접수 시 이벤트 보관 정책에 따라 완료 후 expires_at까지 유지하며 기존 DELETE /clips/{clip_id}로 삭제한다. 삭제/만료 후 같은 키를 되살리지 않으며 새 파일에는 새 고유 event_id를 쓴다. 추출 중 원본 날짜는pin하여 삭제를409로 막고 자동 만료의 물리 삭제는 작업 종료까지 보류한다. 실패·취소·재시작은pin과 공간 예약을 정리한다.

## R-01 현재 카메라 상태

`GET /api/v1/cameras/{camera_id}/status`:

```json
{
  "camera_id":"camera-main",
  "status_basis":"accepted_jpeg_receipt",
  "status":"healthy",
  "connection_state":"connected",
  "last_frame_received_at":"2026-10-30T02:00:00.000Z",
  "assessed_at":"2026-10-30T02:00:01.000Z",
  "valid_until_at":"2026-10-30T02:00:05.000Z",
  "reason_code":"FRAME_RECEIVED",
  "freshness_seconds":5
}
```

현재 연결과 JPEG 프레이밍 검사를 통과한 프레임 수신의 최신성을 평가한다. 전체 JPEG 디코딩·화질·하드웨어 자체 진단은 아니다. HTTP Cache-Control:no-store다. 백엔드는 valid_until_at 이후 응답을 정상 근거로 사용하지 않고 재조회한다. 기본2초 조회를 권장한다.

| 상태 | 사유 |
|---|---|
| healthy | 현재 연결에서 받은 프레임이5초 미만: FRAME_RECEIVED |
| unknown | NO_OBSERVATION / 첫 프레임 대기 AWAITING_FRAME |
| interrupted | FRAME_STALE / FRAME_TIMEOUT / CONNECTION_CLOSED / CONNECTION_ERROR / CANCELLED / TEXT_FRAME_REJECTED / INVALID_JPEG / FRAME_TOO_LARGE |

connection_state는 connected/disconnected/unknown이며 마지막 수신 시각은 없으면null이다. 인증 실패는 정상 관측을 만들지 않는다. 재연결은 이전 프레임을 정상 근거로 쓰지 않고 옛 연결 종료가 새 연결을 덮어쓰지 않는다. 경과 시간은 단조 시계로, 표시 시각은UTC로 평가한다. 재시작은unknown부터 다시 관측한다. 사고 시작 시각처럼 오인할 수 있는 state_since_at은 제공하지 않는다. 미등록 카메라는404 CAMERA_NOT_FOUND다.

## R-08 실제 파일 정보

- `GET /api/v1/clips/{clip_id}/media-info`: 준비 전409, 삭제/만료410.
- `GET /api/v2/daily-recordings/{recording_id}/media-info`: 오늘도 조회 가능하며 전체 MP4가 없으면 file은null.

클립 응답 예시:

```json
{
  "clip_id":"550e8400-e29b-41d4-a716-446655440000",
  "size_bytes":123456,
  "media_info":{"status":"ready","source":"file_probe","codec":"h264","width":1920,"height":1080,"fps":15.0,"fps_num":15,"fps_den":1,"size_bytes":123456},
  "timestamp_info":{"time_basis":"server_received_at","capture_timestamp_supported":false,"capture_timestamp":null,"capture_clock_source":null,"clock_synchronization":"unavailable"},
  "thumbnail":{"status":"unsupported","representative_at":null,"content_path":null}
}
```

fps는 실제 파일 스트림의 명목 frame_rate이며 원본 촬영률/평균 수신률이 아니다. fps_num/fps_den은30000/1001 같은 분수다. 해상도는픽셀, 크기는바이트다. storage_profile 설정을 실측값으로 사용하지 않는다. 검사는 초기최대3프레임/15초, 동시2개/대기포함8개, 캐시128개로 제한한다. 한도 초과503 METADATA_CAPACITY_EXCEEDED다. 이는 파일 전체 무결성 검사와 별개다.

알 수 없으면 status=unknown과 reason_code를 반환하고 codec/width/height/fps/fps_num/fps_den은null이다. 확인 가능한 size_bytes만 정수다. 사유는 file_unavailable/probe_failed/probe_timeout/probe_unavailable/file_changed/metadata_unavailable이다. 내부 파일 경로는 노출하지 않는다.

날짜 응답의 file은 위 측정 객체이며 segments는 status(ready/partial/unknown),total,measured,mixed,formats를 제공한다. formats 원소는 codec,width,height,fps,fps_num,fps_den,segment_count다. 두 종류 이상 확인되면mixed=true, 모두 측정했고 같으면false, 확인 부족은null이다. 기존 조각은 요청 시 한 조각 표본 검사만 할 수 있으므로 measured<total이면 전체 형식을 확정하지 않는다. 새 조각은 생성 검증 때 실제 정보를 저장한다.

## R-09·R-11·현장 설치

현재 엣지 JPEG 입력에 원본 촬영 타임스탬프가 없으므로 timestamp_info는 위처럼 미지원/null이다. 수신 시각을 촬영 시각으로 표시하지 않는다. 촬영 시각이 필요하면 엣지 시각·출처·동기화 계약을 별도 추가해야 한다.

선택 썸네일은 이번 PR에서 제공하지 않는다. thumbnail.status=unsupported, 대표 시각/경로는null이며 자동 생성 작업도 시작하지 않는다.

R-02·R-03의 lidar-a→LiDAR1/lidar-b→LiDAR2 식별 대응은 요청서 기준이다. 실제 좌표·평면·치수·설치 방향·보정 버전은 센서/현장 담당자가 비공개로 확인해야 한다. 카메라 미디어 서비스가 값을 추정하거나 공개 코드에 고정하지 않는다.
