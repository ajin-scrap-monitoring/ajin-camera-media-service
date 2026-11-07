# 날짜 대표 이미지 확장 계약 — 2026-11-07

기존 [썸네일 계약](THUMBNAIL_API_CONTRACT_2026-11-04.md)의 날짜 지원 범위를 확장한다. 클립 API·기존 media-info 호환 응답·인증 경계·크기 제한은 그대로다. 적용 버전과 실제 서버 검증은 [검증 기록](VALIDATION.md)을 따른다.

## 지원 범위와 원본

카메라·날짜 녹화 ID당 대표 JPEG 한 장을 공유한다. 수거·알림·오류 항목마다 이미지나 MP4를 생성하지 않는다. 신규 날짜의 첫 완료 TS 조각이 원본이 될 수 있어, 녹화 중이거나 날짜 MP4가 없어도 준비할 수 있다. 이미지 준비가 날짜 export를 시작하지 않는다.

기존 완료 날짜 MP4와 이미 준비된 JPEG를 우선 재사용한다. TS에서 준비한 이미지는 부모 날짜 녹화에 귀속되며 이후 MP4 보관과 정상 TS 정리 때문에 재생성하지 않는다. 같은 날짜의 새 조각·새 이벤트·조회마다 이미지를 교체하지 않는다. 원본 영상이 모두 없으면 SOURCE_UNAVAILABLE이며 다른 날짜 이미지를 대신 제공하지 않는다.

## API와 백엔드 변경

경로·공개 필드는 추가하지 않는다.

| 기능 | 내부 API |
| --- | --- |
| 상태 | `GET /api/v2/daily-recordings/{recording_id}/thumbnail-info` |
| 명시적 준비·실패 재시도 | `POST /api/v2/daily-recordings/{recording_id}/thumbnail` |
| 이미지 | `GET·HEAD /api/v2/daily-recordings/{recording_id}/thumbnail` |

POST 본문은 기존 `{}` 또는 `{"retry_failed":true}`다. 최초 접수202, 기존 결과/진행 중/보류 상태200, 접수 한도 초과503 규칙을 유지한다. GET·HEAD는 생성하지 않는다. 병렬 POST는 같은 대상의 한 작업으로 합친다. ready 재POST·재시작은 같은 JPEG를 재사용한다.

**백엔드는 날짜의 closed·archive_state=mp4·export_state=ready를 이미지 제공의 필수 조건으로 사용하지 않아야 한다.** 부모 날짜가 접근 가능하고 이미지 status가 ready인지 판단한다. 반환 recording_id·camera_id·고정 content_path를 기존 날짜 상세와 대조한다. 항목 occurredAt의 KST 날짜 연결과 브라우저 인증 경로 변환은 백엔드 담당이다. 대표 시각이 null이어도 부모의 recording_date로 날짜를 연결할 수 있다.

녹화 중 날짜의 응답 예시는 다음과 같다. UUID·시각·바이트는 설명용이며 실제 측정값은 회신에 별도 기록한다.

```json
{
  "recording_id": "c533a054-9448-4d1f-892c-426c05c17921",
  "camera_id": "camera-main",
  "status": "ready",
  "representative_at": "2026-11-06T15:00:00.033Z",
  "time_basis": "server_received_at",
  "content_path": "/api/v2/daily-recordings/c533a054-9448-4d1f-892c-426c05c17921/thumbnail",
  "width": 640,
  "height": 360,
  "size_bytes": 25120,
  "reason_code": null,
  "generated_at": "2026-11-06T15:00:11.000Z"
}
```

대표 시각은 기존 서버 수신/녹화 매핑이며 원본 카메라 촬영 시각이 아니다. 동일 파일 identity와 타임라인의 연결을 입증할 수 없으면 기존처럼 `representative_at=null`, `time_basis=null`, `reason_code=TIMELINE_UNAVAILABLE`인 ready 응답을 허용한다. 준비·원본 검증의 내부 필드는 공개하지 않는다.

## 자동 준비·보류·재시도

- 새 TS 완료 알림과 신규 MP4 완료 알림을 사용한다. 재시작 시에는 recording/finalizing 날짜만5초마다 최대64개씩 순환 확인한다. 과거 완료 날짜 전체를 스캔해 일괄 생성하지 않는다.
- 기존 알림 한도64개, 생성 동시성1, 접수 기본4개, 최대640×360·512KiB·FFmpeg 작업15초 제한을 유지한다. 큐·디스크 대기 때문에 전체 완료 시간을15초로 보장하지 않는다.
- 녹화 큐·지연·저장 오류 때문에 준비를 보류하면 `status=unavailable, reason_code=RECORDING_BUSY`다. **RECORDING_BUSY는 새 reason_code**이므로 백엔드 허용 목록에 반영해야 한다. 이 경우 이미지 GET/HEAD는 기존 unavailable 규칙대로404다. 부하가 회복되면 서버가 제한된 순환 확인으로 재시도하며, 보류 자체는 디코딩 시도 횟수를 늘리지 않는다.
- 유효한 소스가 아직 없으면 unavailable/SOURCE_NOT_READY, 파일이 사라졌거나 검증에 실패하면 SOURCE_UNAVAILABLE 등 기존 사유를 사용한다. 신규 날짜에 더 이상 DAILY_MP4_REQUIRED 조건을 요구하지 않는다.
- 실제 자동 생성은 대상당 최대3회 시도한다. 첫 번째와 두 번째 자동 시도의 접수 시각을 기준으로 각각60초·120초 이후 다음 시도를 허용하며, 부하와 순환 확인에 따라 더 늦어질 수 있다. 실패 완료 시각부터 계산하는 간격은 아니다. 접수 공간 부족·재시작 복구에서도 기존 시도 횟수를 보존한다.3회 소진 후에는 자동 재시도를 멈추며 기존 failed 또는 unavailable 상태와 사유를 유지한다. failed의 명시적 재시도는 `retry_failed=true`가 필요하고, unavailable는 기존 일반 POST 규칙으로 재준비할 수 있다. 반복 조회는 작업을 만들지 않는다.
- 녹화가 우선이다. 백그라운드 작업은 대기열 압박 시 보류하며 Linux에서 실행 중인 소유 FFmpeg도 일시 정지·재개한다. 소유 프로세스의 종료·회수와 원본 lease 해제는 취소 완료까지 기다린다. 짧은 이미지 작업이 자원 부족으로 실패할 수 있으며 영상의 ready 상태를 실패로 바꾸지 않는다.

## 삭제·만료·원본 보호

TS를 읽는 생성 작업은 날짜 파일 잠금과 기존 clip_recording_pins를 사용해 보관 정리와 직렬화한다. 이미지가 게시되면 원본 TS 한 파일의 존속 대신 부모 날짜·카메라의 생명주기에 연결한다. 부모가 삭제·만료되면 상태·POST·이미지는410이며 진행 중 생성이 뒤늦게 이미지를 다시 게시할 수 없다.

명시적 삭제는 해당 이미지 작업을 취소하고 읽기·프로세스 종료를 기다린 뒤 기존 날짜 삭제를 수행한다. 정리 실패의 cleanup_state·space_reclaimed·재시도와 no-store 규칙은 유지한다. 녹화 중 날짜의 DELETE409 RECORDING_ACTIVE는 그대로다. 다른 영상 추출·보관 작업이 원본 lease를 소유하면 기존409 EXPORT_IN_PROGRESS가 가능하며, 해당 작업의 종료·취소 및 lease 해제 후 재시도한다.

이미지 정리는 원본 삭제/만료에 종속된다. 독립 이벤트 MP4·클립 이미지의 보관 정책은 변경하지 않는다. 백엔드는 같은 날짜 이미지를 공유하는 목록의 표시만 해제하고 발생 이력은 보존한다.

## 녹화 보관 작업과의 관계

날짜 MP4는 기존처럼 다운로드 클릭 전에 백그라운드에서 준비한다. 이번 수정은 파일 입력 처리 속도를8배속으로 제한하고 녹화 압박 시 양보한다.24시간 분량이라면 merge·전체 decode 검증이 각각 최소 약3시간, 둘을 합치면 약6시간이 필요할 수 있으며 부하 대기로 더 길어질 수 있다. 그동안 기존 TS/HLS는 유지한다. 완료 전에는 기존 processing/not-ready 상태이며 다운로드 요청이 인코딩을 시작하지 않는다.

서버 수신·녹화 검증과 백엔드 중개·실제 프론트 화면 검증은 구분한다. 하루 전체 보존과 원본 촬영 시각의 정확도는 짧은 기능 시험으로 보장하지 않는다.
