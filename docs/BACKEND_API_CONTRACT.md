# 카메라 미디어 서비스 — 백엔드 연동 규격 v1

작성일: 2026-09-20

이 문서는 미디어 서비스의 백엔드 API 계약이다. HTTP API와 이벤트 녹화는 서비스 시작 시 항상 활성화된다. 로컬 구현·자동 테스트와 실제 배포·상대 백엔드 연동 완료는 구분한다. 백엔드가 기존에 준비한 WHEP 인터페이스를 대신하여 실시간 영상은 HTTP MJPEG로 제공한다.

## 1. 서비스 경계

```text
카메라 / 라즈베리파이 → 카메라 미디어 서비스 → 백엔드
                         └──── 동일 서버 A ────┘
```

미디어 서비스는 영상 수신, 순환 버퍼, 녹화 설정 저장, 이벤트 클립 생성, 클립 목록 및 파일 관리, 실시간 영상 제공을 담당한다. 백엔드는 이 문서의 API로 요청한다. 내부 파일 경로와 저장소에 직접 접근하지 않는다.

이벤트 저장 기준은 **미디어 서비스가 해당 이벤트를 최초 접수한 시각**이다. 영상 구간 역시 서버에서 기록한 프레임 수신 시각으로 판정한다. 실제 사고 발생 시각이나 엣지 촬영 시각을 전달받아 보정하지 않는다.

## 2. 공통 규격

| 항목 | 규격 |
| --- | --- |
| 기본 주소 | `http://127.0.0.1:8889/api/v1` |
| 주소 적용 조건 | 동일 호스트의 일반 프로세스 기준. 컨테이너로 분리하면 호스트 부분을 접근 가능한 내부 서비스 주소로 설정 |
| 인증 | 없음. 동일 서버 내부 호출용이며 기본 loopback 바인딩 사용 |
| 일반 본문 | UTF-8 JSON, `Content-Type: application/json` |
| 필드명 | `snake_case` |
| 시간 | UTC RFC 3339 문자열, 밀리초 및 `Z` 사용: `2026-09-20T03:00:00.000Z` |
| 카메라 ID | 현재 `camera-main`. 등록되지 않은 ID는 `404 CAMERA_NOT_FOUND` |
| 이벤트 ID | 백엔드 발급, 공백만으로 구성되지 않은 1~128자 문자열, 대소문자 구분 |
| 클립 ID | 미디어 서비스가 발급하는 UUID 문자열 |

본문의 필수 필드 누락, 타입 불일치, 허용 범위 위반, 알 수 없는 필드는 `400 INVALID_ARGUMENT`이다. 요청 본문이 있는 API는 JSON을 사용한다. 지원하지 않는 Content-Type은 `415 UNSUPPORTED_MEDIA_TYPE`이다.

### API 목록

아래 경로는 기본 주소의 `/api/v1` 뒤에 붙는다.

| 메서드 | 경로 | 기능 |
| --- | --- | --- |
| GET | `/cameras/{camera_id}/recording-policy` | 녹화 설정 조회 |
| PUT | `/cameras/{camera_id}/recording-policy` | 녹화 설정 전체 변경 |
| GET | `/cameras/{camera_id}/live` | 실시간 MJPEG |
| POST | `/clips` | 이벤트 클립 생성 요청 |
| GET | `/clips` | 클립 목록 |
| GET | `/clips/{clip_id}` | 클립 상세 및 처리 상태 |
| GET | `/clips/{clip_id}/content` | 저장 영상 재생·다운로드 |
| DELETE | `/clips/{clip_id}` | 저장 영상 삭제 |

## 3. 녹화 설정 조회·변경

```http
GET /api/v1/cameras/camera-main/recording-policy
```

```http
PUT /api/v1/cameras/camera-main/recording-policy
Content-Type: application/json
```

PUT 요청 본문 및 GET·PUT의 `200 OK` 응답:

```json
{
  "pre_roll_seconds": 30,
  "post_roll_seconds": 30,
  "retention_days": 30
}
```

| 필드 | 타입 | 범위 | 기본값 | 의미 |
| --- | --- | --- | --- | --- |
| `pre_roll_seconds` | integer | 0~300 | 30 | 최초 접수 이전 저장 시간 |
| `post_roll_seconds` | integer | 0~300 | 30 | 최초 접수 이후 저장 시간 |
| `retention_days` | integer | 1~365 | 30 | 파일 생성 완료 시각부터 보관할 일수 |

- PUT에서는 세 필드를 모두 보내며 전후 시간 합계는 1초 이상이어야 한다.
- 설정은 영속 저장한다. PUT 성공은 적용 및 영속 저장 완료를 의미한다.
- 변경 이후 최초 접수한 이벤트부터 적용한다. 진행 중인 작업과 기존 클립에는 소급 적용하지 않는다.
- 동시 변경은 마지막으로 성공적으로 적용된 설정이 유효하다.
- 버퍼가 실제로 확보하지 못한 과거 영상은 설정 변경으로 복원되지 않는다. 부족한 구간은 부분 저장 규칙을 따른다.
- 버퍼는 최대 300초 및 `MAX_BUFFER_BYTES`(기본 전체 카메라 합계 2 GiB) 한도로 유지한다. 용량이 부족하면 전체 사전 구간을 확보하지 못할 수 있다.

## 4. 실시간 영상

```http
GET /api/v1/cameras/camera-main/live
```

```http
HTTP/1.1 200 OK
Content-Type: multipart/x-mixed-replace; boundary=frame
Cache-Control: no-store
```

응답 본문의 파트 구조(줄바꿈은 실제 전송에서 CRLF):

```text
--frame
Content-Type: image/jpeg
Content-Length: <해당 JPEG 바이트 수>

<JPEG 바이너리>
--frame
Content-Type: image/jpeg
Content-Length: <다음 JPEG 바이트 수>

<다음 JPEG 바이너리>
```

- 수신 JPEG를 재인코딩하지 않고 연속 제공한다. base64나 JSON으로 감싸지 않는다.
- 별도 프레임 메타데이터는 제공하지 않는다. 원본 수신 FPS 전체를 모든 조회 연결에 보장하지 않는다.
- 요청 시 최근 5초 내 수신한 프레임이 없으면 `503 CAMERA_UNAVAILABLE`이다.
- 응답 시작 이후 새 프레임 수신이 5초 이상 없으면 연결을 종료한다.
- 느린 조회 연결에는 중간 프레임이 생략되거나 연결이 종료될 수 있다. 오래된 영상이 무제한 대기하도록 하지 않는다.
- 스트림은 유한한 파일이 아니므로 전체 Content-Length와 Range 기능을 제공하지 않는다.
- 조회 연결 유무와 종료는 녹화 설정, 버퍼 및 이벤트 저장 작업에 영향을 주지 않는다.
- 백엔드가 중개할 경우 응답 전체를 모으지 않고 스트리밍해야 하며, 프록시 응답 버퍼링을 비활성화해야 한다.

## 5. 이벤트 저장 요청

```http
POST /api/v1/clips
Content-Type: application/json
```

```json
{
  "event_id": "evt-0001",
  "camera_id": "camera-main"
}
```

두 필드는 필수이다. 이벤트 발생 시각이나 전후 시간은 요청에 포함하지 않는다.

신규 접수 응답:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
Location: /api/v1/clips/550e8400-e29b-41d4-a716-446655440000
```

```json
{
  "clip_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_id": "evt-0001",
  "camera_id": "camera-main",
  "status": "collecting",
  "accepted_at": "2026-09-20T03:00:00.000Z",
  "requested_start_at": "2026-09-20T02:59:30.000Z",
  "requested_end_at": "2026-09-20T03:00:30.000Z"
}
```

### 저장 및 중복 규칙

- `accepted_at`은 최초 접수 시각이며 재요청으로 변경하지 않는다.
- 접수 당시 설정을 작업에 고정한다. 저장 대상 구간은 `[accepted_at - pre_roll_seconds, accepted_at + post_roll_seconds)`이다.
- 이미 확보한 사전 구간은 순환 삭제로부터 보호하고 이후 구간을 추가 수집한다.
- 성공 응답 전 작업과 중복 판단 정보를 영속 기록한다. 재시작 후 복구할 수 없는 작업은 `failed`로 조회 가능하게 한다.
- 현재 구현은 재시작 시 진행 중이던 모든 작업을 `JOB_INTERRUPTED`로 전환한다. 완료 클립과 설정은 유지한다.
- `(camera_id, event_id)`가 같으면 중복이다. 원자적으로 하나의 작업만 생성한다.
- 중복 요청은 `200 OK`와 위와 동일한 필드 구성의 기존 작업 정보를 반환한다. `status`는 현재 상태이며 `Location`도 기존 클립을 가리킨다.
- 삭제·만료·실패한 작업도 같은 이벤트로 새로 생성하지 않는다.
- 중복 판단 정보는 최초 접수 후 최소 365일, 그리고 해당 작업 또는 보관 파일이 존재하는 동안 유지한다. 그 이후에는 동일 키의 중복 방지를 보장하지 않는다.
- 다른 이벤트 ID의 겹치는 구간은 각각 별도의 클립 ID로 제공한다.
- 응답 유실이나 타임아웃으로 재시도할 때는 **동일한 이벤트 ID**를 사용해야 한다.
- 카메라가 일시 단절됐더라도 작업을 접수할 수 있다. 확보된 버퍼와 종료 시각까지 수신한 영상으로 결과를 결정한다. 접수 자체가 불가능한 저장소 장애·처리 용량 초과는 `503`으로 반환한다.
- `202`는 영상 저장 완료를 뜻하지 않는다. `post_roll_seconds`가 0이면 바로 `processing` 상태로 응답할 수 있다.

## 6. 클립 상태·상세 조회

```http
GET /api/v1/clips/550e8400-e29b-41d4-a716-446655440000
```

`200 OK` 응답:

```json
{
  "clip_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_id": "evt-0001",
  "camera_id": "camera-main",
  "status": "ready",
  "coverage": "complete",
  "accepted_at": "2026-09-20T03:00:00.000Z",
  "requested_start_at": "2026-09-20T02:59:30.000Z",
  "requested_end_at": "2026-09-20T03:00:30.000Z",
  "recording_policy": {
    "pre_roll_seconds": 30,
    "post_roll_seconds": 30,
    "retention_days": 30
  },
  "actual_start_at": "2026-09-20T02:59:30.020Z",
  "actual_end_at": "2026-09-20T03:00:29.987Z",
  "gaps": [],
  "completed_at": "2026-09-20T03:00:32.000Z",
  "expires_at": "2026-10-20T03:00:32.000Z",
  "deleted_at": null,
  "content_path": "/api/v1/clips/550e8400-e29b-41d4-a716-446655440000/content",
  "error": null
}
```

### 상태값

| `status` | 의미 |
| --- | --- |
| `collecting` | 이벤트 이후 구간 수집 중 |
| `processing` | 영상 파일 생성 중 |
| `ready` | 파일 생성 완료, 재생 가능 |
| `failed` | 작업 실패 |
| `deleted` | DELETE 요청으로 삭제 완료 |
| `expired` | 보관 기간 만료, 파일 접근 불가 |

정상 흐름은 `collecting → processing → ready`이다. 작업 실패 시 `failed`, 수동 삭제 시 `ready/failed → deleted`, 보관 만료 시 `ready → expired`로 전이한다. `deleted`와 `expired`는 종결 상태다.

### 필드 및 누락 규칙

- `recording_policy`는 작업 접수 당시 설정 스냅샷이다.
- `coverage`는 수집 완료 전 `null`, 수집 완료 후 `complete`, `partial`, `none`이다. 파일 생성 성공 여부와는 별개이다.
- `actual_start_at`, `actual_end_at`은 확보한 최초·최종 프레임의 서버 수신 시각이다. 수집 완료 전 또는 확보한 영상이 없으면 `null`이다.
- 요청 시작부터 첫 프레임, 인접 프레임 사이, 마지막 프레임부터 요청 종료까지 **1초 초과의 공백**이 있으면 `partial`이며 `gaps`에 기록한다. `complete`는 이 기준 이하의 작은 프레임 누락까지 없다는 뜻은 아니다.
- `gaps` 원소는 `{"start_at": "...", "end_at": "..."}` 형식이다. 수집 완료 전에는 빈 배열이다. 영상이 전혀 없으면 요청 전체 구간을 기록한다.
- 일부 영상이라도 파일 생성에 성공하면 `ready`와 `coverage: "partial"`을 반환한다.
- 확보한 영상이 없으면 `failed`, `coverage: "none"`, 오류 코드 `NO_FRAMES_AVAILABLE`이다.
- `completed_at`은 파일 생성 성공 시각이며 성공 전에는 `null`이다. 실패를 포함한 모든 종결 상태에서 값이 생기는 필드는 아니다.
- `expires_at`은 성공 시 `completed_at + retention_days`로 정하며, 성공 전에는 `null`이다.
- `deleted_at`은 수동 삭제 완료 시각이다. 자동 만료는 `expires_at`으로 표현한다.
- `content_path`는 `ready`에서만 상대 경로로 제공하며 나머지는 `null`이다. 내부 파일 경로가 아니다.
- `error`는 작업 실패 시 `{"code": "...", "message": "..."}`이다. 실패 작업을 삭제하면 원래 오류 정보는 유지한다.
- 상태 확인은 조회 API로 수행한다. 완료 콜백은 제공하지 않는다. 권장 조회 간격은 2초이며 `ready`, `failed`, `deleted`, `expired`에서 완료 대기를 종료한다.

부분 저장 시 필드 예시:

```json
{
  "status": "ready",
  "coverage": "partial",
  "actual_start_at": "2026-09-20T02:59:40.000Z",
  "actual_end_at": "2026-09-20T03:00:29.987Z",
  "gaps": [
    {
      "start_at": "2026-09-20T02:59:30.000Z",
      "end_at": "2026-09-20T02:59:40.000Z"
    }
  ],
  "error": null
}
```

위 예시는 상세 응답의 일부 필드만 발췌한 것이다.

## 7. 클립 목록

```http
GET /api/v1/clips?camera_id=camera-main&status=ready&limit=20
```

| 쿼리 | 규칙 |
| --- | --- |
| `camera_id` | 선택, 정확히 일치하는 카메라 필터 |
| `event_id` | 선택, 정확히 일치하는 이벤트 필터 |
| `from` | 선택, `accepted_at`이 이 시각 이상 |
| `to` | 선택, `accepted_at`이 이 시각 미만 |
| `status` | 선택, 상태값 하나. 생략하면 `deleted`, `expired` 제외 |
| `limit` | 정수 1~100, 기본 20 |
| `cursor` | 다음 페이지용 불투명 문자열 |

- 시각 필터가 모두 있으면 `from < to`여야 한다.
- 정렬은 `accepted_at` 내림차순, 동일 시각은 `clip_id` 내림차순이다.
- 다음 페이지는 같은 필터·limit에 반환받은 cursor를 추가한다. 커서를 해석하거나 수정하지 않는다.
- 목록은 실시간 상태를 반영하며 페이지 사이에 작업 상태가 바뀔 수 있다.
- 다음 커서를 발급한 경우 발급 후 최소 24시간 유효하다. 잘못됐거나 만료된 커서는 `400 INVALID_CURSOR`이다.
- `items`의 각 원소는 6절의 전체 상세 응답 형식이다.

빈 결과 또는 마지막 페이지 응답 형태:

```json
{
  "items": [],
  "next_cursor": null
}
```

`deleted`와 `expired`도 명시적 상태 필터로 조회할 수 있다. 삭제·만료 메타데이터는 중복 방지 기록 유지 기간 동안 조회 가능하며 이후 정리될 수 있다.

## 8. 저장 영상 재생·다운로드

```http
GET /api/v1/clips/550e8400-e29b-41d4-a716-446655440000/content
Range: bytes=0-1048575
```

| 항목 | 규격 |
| --- | --- |
| 파일 형식 | MP4 |
| 영상 코덱 | H.264, 오디오 없음 |
| Content-Type | `video/mp4` |
| 전체 조회 | `200 OK`, 전체 파일의 Content-Length |
| 부분 조회 | 단일 bytes Range 지원, `206 Partial Content`, Content-Range 및 해당 부분 Content-Length |
| 범위 지원 안내 | `Accept-Ranges: bytes` |
| 만족할 수 없는 범위 | `416 RANGE_NOT_SATISFIABLE`, `Content-Range: bytes */<파일 크기>` |
| 다운로드 | `?download=true`일 때 `Content-Disposition: attachment; filename="<clip_id>.mp4"` |

- 일반 재생은 `Content-Disposition: inline`으로 제공한다.
- 다중 Range 요청은 v1에서 지원하지 않으며 Range를 무시하고 전체 파일을 `200`으로 반환한다.
- 출력은 브라우저 재생 호환성을 고려한 H.264 8-bit YUV 4:2:0과 fast-start MP4로 생성한다. MP4는 실시간 전달과 별도로 이벤트 구간만 인코딩한다.
- 파일 내부 재생 시간 0은 `actual_start_at`에 대응한다. 프레임 간 시간 간격을 유지하고, 중간 공백은 직전 프레임 유지로 표현한다. 부족한 선행·후행 구간을 정상 촬영 영상으로 꾸며 넣지 않는다. 누락 판정은 반드시 상세 응답의 `coverage`와 `gaps`로 확인한다.
- `collecting`, `processing`, `failed`의 파일 요청은 `409 CLIP_NOT_READY`이다.
- `deleted`는 `410 CLIP_DELETED`, `expired`는 `410 CLIP_EXPIRED`이다.
- 백엔드가 파일을 중개할 경우 Range 요청 및 Content-Range·Accept-Ranges·Content-Type·Content-Length와 응답 상태를 보존해야 한다.

## 9. 저장 영상 삭제

```http
DELETE /api/v1/clips/550e8400-e29b-41d4-a716-446655440000
```

요청 본문은 없다.

| 현재 상태 | 결과 |
| --- | --- |
| `ready` | 파일 삭제 후 `deleted`로 변경, `204 No Content` |
| `failed` | 잔여 임시 파일 정리 후 `deleted`로 변경, `204 No Content` |
| `collecting`, `processing` | `409 CLIP_IN_PROGRESS`, 변경 없음 |
| `deleted`, `expired` | `204 No Content`, 기존 상태 유지 |
| 존재하지 않는 클립 | `404 CLIP_NOT_FOUND` |

- 삭제 성공 응답은 본문이 없다. 설정된 보관 기간과 관계없이 요청한 파일을 삭제한다.
- 파일 삭제 실패는 `503 STORAGE_UNAVAILABLE`이며 `deleted`로 처리하지 않는다.
- 삭제 완료 이후 신규 파일 접근을 차단한다. 이미 시작된 전송은 완료될 수 있으며 이미 전송한 데이터는 회수하지 않는다.
- 상세 메타데이터와 이벤트 중복 방지 기록은 유지한다. 동일 이벤트의 재요청으로 파일을 다시 생성하지 않는다.
- 자동 만료 시에도 파일 접근은 차단하며 상태는 `expired`이다. 실제 정리 작업이 지연돼도 만료 파일을 제공하지 않는다.
- 작업 취소, 일괄 삭제, 삭제 복구 API는 v1에 포함하지 않는다.

## 10. 오류 규격

일반 API 오류 응답은 `application/json`이다.

```json
{
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "pre_roll_seconds must be between 0 and 300."
  }
}
```

`code`는 프로그램 분기용 고정값이고 `message`는 설명용이며 문구는 변경될 수 있다.

| HTTP 상태 | code | 의미 |
| --- | --- | --- |
| 400 | `INVALID_ARGUMENT` | 요청 필드·범위·형식 오류 |
| 400 | `INVALID_CURSOR` | 커서 오류·만료 |
| 404 | `CAMERA_NOT_FOUND` | 등록되지 않은 카메라 |
| 404 | `CLIP_NOT_FOUND` | 존재하지 않거나 기록이 정리된 클립 |
| 409 | `CLIP_NOT_READY` | 파일 제공 불가 상태 |
| 409 | `CLIP_IN_PROGRESS` | 진행 중 작업 삭제 요청 |
| 410 | `CLIP_DELETED` | 수동 삭제된 파일 |
| 410 | `CLIP_EXPIRED` | 만료된 파일 |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | JSON API의 Content-Type 오류 |
| 416 | `RANGE_NOT_SATISFIABLE` | 파일 범위 오류 |
| 503 | `CAMERA_UNAVAILABLE` | 실시간 영상 사용 불가 |
| 503 | `STORAGE_UNAVAILABLE` | 저장소 사용·삭제 불가 |
| 503 | `SERVICE_BUSY` | 작업 접수 용량 초과 |
| 500 | `INTERNAL_ERROR` | 기타 내부 오류 |

실시간·파일 응답을 이미 시작한 이후의 오류는 JSON으로 바꾸지 않고 연결을 종료한다.

작업 접수 후 발생한 오류는 상세 조회 자체의 HTTP 오류가 아니라, `200 OK` 상세 응답 안의 `status: "failed"`와 `error`로 제공한다.

| 작업 error.code | 의미 |
| --- | --- |
| `NO_FRAMES_AVAILABLE` | 요청 구간에 확보한 프레임 없음 |
| `ENCODING_FAILED` | 파일 인코딩 실패 |
| `STORAGE_UNAVAILABLE` | 파일 생성·저장 중 저장소 오류 |
| `JOB_INTERRUPTED` | 재시작 등으로 작업 복구 불가 |
| `INTERNAL_ERROR` | 기타 작업 오류 |

## 11. 연동 순서

1. 녹화 설정을 조회하거나 변경한다.
2. 실시간 조회는 필요할 때 MJPEG에 연결한다. 녹화를 위해 이 연결을 유지할 필요는 없다.
3. 이벤트가 발생하면 POST `/clips`에 이벤트 ID와 카메라 ID를 보낸다.
4. 반환된 클립 ID로 상세 조회하여 완료 상태를 확인한다.
5. `ready`이면 content 경로로 재생·다운로드한다. `partial` 여부도 상세에서 확인한다.
6. 필요하면 DELETE로 저장 파일을 삭제한다.

## 12. 연동 확인 항목

- 기본 설정 조회·변경 및 서비스 재시작 후 유지.
- 인증 헤더 없는 API 호출, 등록되지 않은 카메라, 잘못된 설정 값.
- MJPEG 수신, 연결 종료, 무시청 상태에서 이벤트 녹화.
- 전후 구간 저장, 동일 이벤트 재요청 및 응답 유실 후 재시도.
- 버퍼 부족·카메라 단절 시 부분 저장 또는 영상 없음 실패.
- 목록 필터·페이지네이션, 상세 상태 조회.
- MP4 재생·단일 Range 조회·다운로드.
- 완료 클립 삭제, 반복 삭제, 삭제 파일 410, 진행 중 삭제 409.
- 보관 기간 만료, 삭제·만료 후 동일 이벤트 중복 생성 방지.

## 참고 표준

- HTTP 상태와 Range 의미: <https://www.rfc-editor.org/rfc/rfc9110.html>
- MJPEG multipart 형식: <https://ffmpeg.org/ffmpeg-formats.html#mpjpeg>

API 경로, 필드, 기본값, 시간·삭제 정책은 이 프로젝트의 계약이다. MJPEG와 저장 영상 파일 외의 일반 응답은 위 JSON 규격을 따른다.
