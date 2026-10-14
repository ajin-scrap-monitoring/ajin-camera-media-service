# 날짜별 상시 녹화 API 연동 가이드

대상: 백엔드·프론트엔드 담당자. 날짜 녹화는 v2, 기존 이벤트 클립·실시간 MJPEG는 v1을 사용한다.

이 문서의 요청/응답은 구현된 계약이다. 실제 서버 검증 범위와 운영 활성화 상태는 [VALIDATION.md](VALIDATION.md) 및 [SERVER_DEPLOYMENT.md](SERVER_DEPLOYMENT.md)를 참조한다.

## 1. 연결 주소와 역할

- 동일 서버의 백엔드 프로세스: `http://127.0.0.1:8889/api/v2`
- `ajin-media` Docker 네트워크에 연결한 백엔드: `http://camera-media-service:8889/api/v2`
- 내부 API는 인증 헤더 없이 호출한다. 브라우저 사용자 인증·권한 검사는 백엔드/영상 게이트웨이에서 수행한다.
- 미디어 서비스는 백엔드 API를 호출하지 않는다. 백엔드가 설정·목록·상태·준비·삭제 API를 호출하고 준비 완료를 조회한다.
- 날짜 기능은 서버에 `DAILY_RECORDING_ENABLED=true`와 유한한 `MAX_DAILY_STORAGE_BYTES`가 설정되어야 활성화된다. 비활성 배포에서는 v2 경로가 제공되지 않는다.
- 실시간: 기존 `/api/v1/cameras/{camera_id}/live`의 MJPEG. 날짜 영상: HLS. 다운로드: 준비된 MP4. WHEP/WebRTC로 연결하지 않는다.

## 2. 날짜·시간·하루 영상의 의미

- `recording_date`는 한국 날짜 `YYYY-MM-DD` 문자열이다. 표시할 때 이 날짜를 유지한다.
- 시간창은 한국 자정부터 다음 자정 미만: `[00:00, 다음 날 00:00)`.
- 타임스탬프는 UTC RFC3339, 밀리초와 `Z`를 사용한다. 화면에서는 Asia/Seoul로 변환한다.
- 예: 한국 2026-10-14의 시간창은 `2026-10-13T15:00:00.000Z` ~ `2026-10-14T15:00:00.000Z` 미만이다.
- 촬영 시각이 별도로 전달되지 않으므로 현재는 서버 JPEG 수신 시각 기준이다.
- 날짜 없이 HH:mm:ss만 표시하면 자정·날짜 구분이 모호해진다. 날짜 선택 맥락과 상세 날짜/시각을 함께 표시한다.
- 카메라·날짜당 `recording_id` 한 개. 서비스 재시작과 export 재요청에서도 날짜 ID는 유지된다.
- 하루 한 건은 관리 대상 시간창이다. 항상24시간 분량이 확보됐다는 뜻은 아니다.
- 기능을 정오에 시작했다면 자정~정오는 `RECORDING_NOT_STARTED` 누락이다. 기능 적용 이전 날짜를 소급 녹화하지 않는다.

## 3. API 목록

아래 경로는 기본 주소 `/api/v2` 뒤에 붙인다.

| 메서드 | 경로 | 기능 |
|---|---|---|
| GET / PUT | `/cameras/{camera_id}/daily-recording-policy` | 날짜 녹화 설정 |
| GET | `/cameras/{camera_id}/daily-recordings` | 날짜별 목록 |
| GET | `/daily-recordings/{recording_id}` | 상세/누락/준비 상태 |
| GET | `/daily-recordings/{recording_id}/hls/index.m3u8` | 오늘/지난 날짜 재생 목록 |
| GET / HEAD | `/daily-recordings/{recording_id}/media/{segment_id}.ts` | 완료된 영상 조각 |
| POST | `/daily-recordings/{recording_id}/exports` | 지난 날짜 MP4 준비 |
| GET / HEAD | `/daily-recordings/{recording_id}/content` | 준비된 MP4 |
| DELETE | `/daily-recordings/{recording_id}` | 지난 날짜 영상 삭제 |

JSON 요청은 `Content-Type: application/json`. 알 수 없는 본문 필드·중복 필드·잘못된 타입은400, 다른 Content-Type은415다. JSON 필드 이름은 snake_case다.

## 4. 날짜 녹화 설정

```http
PUT /api/v2/cameras/camera-main/daily-recording-policy
Content-Type: application/json

{"enabled":true,"retention_days":30}
```

GET/PUT 성공200 응답도 같은 구조다. enabled는 boolean, retention_days는1~365 integer다. 두 필드 모두 필요하다. 기본값은 enabled=true/retention_days=30이며 서버 전체 날짜 기능 활성화 조건과 별개다.

enabled 변경은 미디어 서비스가 PUT을 처리한 시각부터 적용합니다. 짧게 껐다가 같은 조각 시간 안에 다시 켜도 비활성 구간은 `RECORDING_DISABLED`로 남고, 끄기 전 확보한 영상은 유지합니다.

이벤트의 pre_roll_seconds/post_roll_seconds/retention_days는 `/api/v1/.../recording-policy`에 남는다. 날짜 보관 기간 변경은 다음 날짜부터 적용하고 기존/현재 날짜에 소급하지 않는다. 비활성화한 구간의 녹화는 확보할 수 없다.

## 5. 날짜별 목록

```http
GET /api/v2/cameras/camera-main/daily-recordings?from_date=2026-10-01&to_date=2026-10-15&limit=20
```

| 쿼리 | 의미 |
|---|---|
| from_date | 한국 날짜, 포함 |
| to_date | 한국 날짜, 제외. 10월14일까지 보려면10월15일 |
| limit | 기본20, 최소1/최대100 |
| cursor | 응답의 next_cursor를 그대로 사용 |
| status | 선택. recording/finalizing/closed/failed/deleted/expired |

```json
{"items":[],"next_cursor":null}
```

items는 날짜 내림차순의 녹화 정보다. next_cursor가 있으면 **동일 카메라·필터·limit**으로 다음 페이지를 요청한다. 커서는24시간 유효하며 필터를 바꾸면400 INVALID_CURSOR다. 기본 목록에서는 deleted/expired를 제외하고 영상이 없는 closed/none 날짜는 포함한다.

## 6. 상세 응답과 화면 표시

아래는10초 누락이 있는 녹화 중 날짜의 합성 예시다.

```json
{
  "recording_id":"550e8400-e29b-41d4-a716-446655440000",
  "camera_id":"camera-main",
  "recording_date":"2026-10-14",
  "timezone":"Asia/Seoul",
  "window_start_at":"2026-10-13T15:00:00.000Z",
  "window_end_at":"2026-10-14T15:00:00.000Z",
  "status":"recording",
  "coverage":null,
  "has_gaps":true,
  "coverage_as_of_at":"2026-10-13T15:01:00.000Z",
  "playable_until_at":"2026-10-13T15:01:00.000Z",
  "recorded_seconds":50,
  "gaps":[{"start_at":"2026-10-13T15:00:20.000Z","end_at":"2026-10-13T15:00:30.000Z","reason_code":"CAMERA_DISCONNECTED"}],
  "available_intervals":[
    {"start_at":"2026-10-13T15:00:00.000Z","end_at":"2026-10-13T15:00:20.000Z","playback_start_seconds":0,"playback_end_seconds":20},
    {"start_at":"2026-10-13T15:00:30.000Z","end_at":"2026-10-13T15:01:00.000Z","playback_start_seconds":20,"playback_end_seconds":50}
  ],
  "playback_state":"ready",
  "playback_protocol":"hls",
  "playback_path":"/api/v2/daily-recordings/550e8400-e29b-41d4-a716-446655440000/hls/index.m3u8",
  "export_allowed":false,
  "export_state":"not_requested",
  "download_path":null,
  "size_bytes":null,
  "expires_at":null,
  "export_expires_at":null,
  "error":null
}
```

실제 응답에는 retention_days, revision, deleted_at 같은 관리 정보가 추가될 수 있다.

| 필드 | 의미와 처리 |
|---|---|
| status | recording=날짜 녹화 중, finalizing=마감 중, closed=날짜 마감, failed=처리 실패, expired=원본 보관 만료, deleted=삭제 |
| coverage | 녹화 중 null. 마감 후 complete/partial/none |
| has_gaps | 오늘도 이미 확인된 누락이 있으면 true. recording과 함께 부분 저장 표시 |
| coverage_as_of_at | 저장/누락 판정이 완료된 시간창의 끝 |
| playable_until_at | 마지막 완료 조각의 원래 시각. 그 이전에도 gaps가 있을 수 있음 |
| recorded_seconds | 실제 저장된 영상 길이 합. 하루 시간창 길이와 다를 수 있음 |
| playback_state | pending/ready/unavailable |
| export_state | not_requested/processing/ready/failed. closed와 별개 |
| size_bytes | 준비 완료 MP4 크기. 준비 전 null |
| expires_at | 날짜 원본 만료 시각. 녹화 중 null |
| export_expires_at | 다운로드용 MP4 캐시 만료 시각. 원본보다 먼저 만료 가능 |
| error | 오류가 있으면 code/message. 정상은 null |

오늘의 window_end_at은 미래다. 그 시각까지 저장됐다는 뜻이 아니다. **아직 오지 않은 시간과 인코딩 중인 꼬리를 gaps로 만들지 않는다.** 미래/처리 중 구간과 실제 누락 구간을 서로 다르게 표시한다.

gaps의 원인 후보: CAMERA_DISCONNECTED, RECORDING_NOT_STARTED, RECORDING_DISABLED, SERVICE_INTERRUPTED, BUFFER_DROPPED, STORAGE_FULL, STORAGE_ERROR, ENCODING_FAILED, UNKNOWN_CAPTURE_LOSS. 연결만 살아 있고 프레임이 멈춘 경우도 누락이 될 수 있다.

## 7. 오늘과 지난 날짜 재생

playback_state가 ready면 playback_path를 인증된 영상 게이트웨이 주소로 변환해 HLS 플레이어에 전달한다. 오늘은 완료된 조각만 추가되는 EVENT 목록이고, 지난 날짜는 끝이 있는 VOD 목록이다. 저장 조각은 기본10초이며 인코딩 시간이 더해져 재생 지연이 생긴다.

게이트웨이는 다음 **두 경로 전체**를 허용해야 한다.

```text
/api/v2/daily-recordings/{recording_id}/hls/index.m3u8
/api/v2/daily-recordings/{recording_id}/media/{segment_id}.ts
```

m3u8 안의 `/api/v2/.../media/...`는 미디어 서비스의 상대 경로다. 게이트웨이가 다른 prefix를 쓰면 목록 안의 URI도 변환한다. 목록만 프록시하고 조각 경로를 빠뜨리면 재생되지 않는다. 브라우저별 HLS 지원에 맞춰 플레이어를 연결한다.

각 독립 TS의 타임스탬프 초기화를 HLS `EXT-X-DISCONTINUITY`로 표시합니다. 이 태그만 보고 누락 여부를 판단하지 마세요. 실제 누락은 JSON의 gaps로 표시합니다. TARGETDURATION은 날짜 안에서 고정됩니다. storage_profile/segment_seconds/hls_target_duration은 서버가 고정한 출력 정보이며 백엔드에서 변경하는 정책 필드가 아닙니다.

- 목록 Content-Type: application/vnd.apple.mpegurl
- 조각 Content-Type: video/mp2t
- 재생 목록/파일: Cache-Control no-store
- 조각은 GET/HEAD와 단일 Range 지원
- PROGRAM-DATE-TIME은 실제 날짜/시각 매핑이다. 누락 표시가 플레이어에 자동으로 구현되지는 않는다.

**빈 시간을 숨기지 않는 UI:** 00~24시 날짜 시간축에 gaps를 표시한다. 영상은 확보 구간만 이어서 재생하므로 available_intervals로 원래 시각을 표시한다. 누락 구간을 선택하면 '해당 시간 영상 없음'을 보여주고 다음 확보 구간으로 이동할지 명시한다. 단순 재생 elapsed seconds를 자정에 더해 촬영 시각으로 표시하면 누락 뒤 시간이 틀어진다.

## 8. 하루 MP4 다운로드

오늘은 전체 다운로드 버튼을 비활성화한다. 다음 날짜로 넘어가고 status가 closed가 된 뒤 준비 요청한다. 부분 저장 날짜도 확보된 영상이 있으면 다운로드할 수 있다.

```http
POST /api/v2/daily-recordings/{recording_id}/exports
```

본문 없이 호출하거나 `{}`를 보낸다. 최초/진행 중202, 이미 준비됐으면200. 응답은 해당 날짜 정보이고 Location은 상세 조회 경로다. 동일 날짜의 중복 요청은 같은 준비 작업으로 처리한다.

1. POST exports로 준비 접수.
2. GET 상세를 적당한 간격으로 조회. 초기 권장 간격2초, 긴 작업은 간격을 늘린다.
3. export_state=ready면 download_path를 게이트웨이 주소로 변환.
4. `GET {download_path}?download=true`로 다운로드.
5. failed면 error.code를 표시하고, 원인이 해결된 후 같은 recording_id로 다시 POST.

파일은 H.264/MP4 무음이며 실제 확보 영상만 포함한다. 부분 저장 파일은 실제 길이와 원래 누락 시간을 함께 알려야 한다. MP4 캐시가 만료되면 다시 준비할 수 있고 날짜 ID는 바뀌지 않는다.

Content-Type video/mp4, Content-Disposition attachment, 파일명 `daily_<한국날짜>_<recording_id>.mp4`. Content-Length/Accept-Ranges 및206 응답의 Content-Range를 지원한다. 파일 전체를 메모리에 모아 응답하지 않는다.

```http
GET /api/v2/daily-recordings/{recording_id}/content?download=true
Range: bytes=0-1048575
```

## 9. 삭제

```http
DELETE /api/v2/daily-recordings/{recording_id}
```

종료된 날짜를 삭제하면 조각·다운로드 MP4를 정리한다. 성공204, 반복 삭제204, 이후 콘텐츠410. 오늘/마감 중 날짜 또는 export 진행 중에는409다. 다른 날짜, 이벤트 클립, 실시간 시청은 별도다. 삭제/만료 메타데이터는 남는다.

## 10. 오류 처리

```json
{"error":{"code":"DAY_NOT_CLOSED","message":"Today cannot be exported."}}
```

| HTTP / code | 의미 |
|---|---|
| 400 INVALID_ARGUMENT | 필드·날짜·범위·limit 잘못됨 |
| 400 INVALID_CURSOR | 커서 만료·변조·필터 변경 |
| 404 CAMERA_NOT_FOUND | 등록되지 않은 카메라 |
| 404 DAILY_RECORDING_NOT_FOUND / SEGMENT_NOT_FOUND | 존재하지 않는 날짜/완료 조각 |
| 409 DAY_NOT_CLOSED / DAY_FINALIZING | 오늘 또는 날짜 마감 중 |
| 409 NO_RECORDING_DATA | 그 날짜에 확보된 영상 없음 |
| 409 PLAYBACK_NOT_READY | 공개 가능한 완료 조각 없음 |
| 409 EXPORT_NOT_READY | MP4 준비 필요/진행 중/실패 |
| 409 RECORDING_ACTIVE / EXPORT_IN_PROGRESS | 삭제할 수 없는 진행 중 상태 |
| 410 DAILY_RECORDING_DELETED / DAILY_RECORDING_EXPIRED | 삭제/원본 보관 만료 |
| 415 UNSUPPORTED_MEDIA_TYPE | JSON 요청의 Content-Type 잘못됨 |
| 416 RANGE_NOT_SATISFIABLE | 잘못된 파일 범위 |
| 503 INSUFFICIENT_STORAGE / EXPORT_CAPACITY_EXCEEDED | 용량 부족/준비 대기 한도 |
| 503 RECORDING_STORAGE_ERROR / STORAGE_UNAVAILABLE | 파일 또는 카탈로그 저장소 문제 |

비동기 준비 실패는 상세의 export_state=failed와 error로 표시된다. 예: EXPORT_FAILED, EXPORT_INCOMPATIBLE_MEDIA, JOB_INTERRUPTED, STORAGE_ERROR. 재시작 후 미완료 export는 실패로 조회되며 재요청할 수 있다. 날짜 원본의 부분 저장 상태와 export 실패를 혼동하지 않는다.

## 11. 기존 이벤트 기능과의 관계

`/api/v1/clips`는 이벤트 전후 영상이다. `/api/v2/.../daily-recordings`는 상시 녹화다. 이벤트가 없는 날짜도 상시 목록에 나타나며 이벤트 클립 API를 하루 영상 API처럼 사용하지 않는다. 사건 이력을 어느 날짜 영상과 연결할지는 백엔드의 역할이다.

## 12. 연동 확인 순서

1. GET 날짜 정책200과 목록200 확인.
2. 오늘의 날짜·한국 시각·recording/null coverage 확인.
3. 완료 조각 생성 후 HLS 목록과 조각 모두 조회·재생 확인.
4. 카메라를 끊었다가 연결한 테스트 날짜에서 has_gaps와 누락 시간 표시 확인.
5. 지난 날짜 closed/partial의 MP4 준비202→ready→다운로드200/Range206 확인.
6. 오늘 준비409, 영상 없음409, 삭제204/재조회410 확인.
7. 게이트웨이를 통한 브라우저 seek·날짜 시간축·사용자 권한 확인.

미디어 기능의 서버 테스트와 상대 백엔드/대시보드 통합 검증은 별도다. 운영에서는 실제 카메라 수·화질·하루 저장량·보관일·MP4 추가 공간을 확인하고 유한한 저장 한도를 설정한다. 용량 부족 시 준비 요청이 거부되거나 날짜 녹화가 부분 저장으로 표시될 수 있다.

## 13. 동료가 바로 확인할 서버 시험 주소 (2026-10-14)

- 같은 서버의 일반 프로세스: http://127.0.0.1:18890/api/v2.
- ajin-media 네트워크의 백엔드 컨테이너: http://ajin-camera-media-daily-validation:8889/api/v2.
- 시험 카메라: camera-main. 합성 영상이 들어오는 격리 시험 환경이며 실장비/운영 영상으로 취급하지 않습니다.
- 시작 날짜2026-10-14의 recording_id: b346c3c5-7662-42e1-bea4-216bf8a9b67f. 자정 뒤에는 다음 날짜에 새 ID가 생기므로 목록으로 조회합니다.
- 한국 시간2026-10-14 22:53부터24시간 시험을 진행 중입니다. 오늘의 MP4 준비는409이며 날짜 마감 후 허용됩니다. 시작 전/시험 중 단절이 있어 complete를 기대하지 않습니다.
- 운영8889의 날짜 기능 활성화는 용량/프로파일 확정 뒤 별도로 진행합니다. 운영30일 보관 공간을 이번16GiB 시험 예산으로 확정하지 않았습니다.

내부 API를 사용자 브라우저에 직접 노출하지 말고 기존 백엔드의 인증된 중개를 통해 연결합니다. 상대 백엔드/대시보드의 새 API 구현과 최종 연동 검증은 이 미디어 서버 시험의 완료 항목에 포함하지 않습니다.
