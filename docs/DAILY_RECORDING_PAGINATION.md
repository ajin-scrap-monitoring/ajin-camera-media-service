# 날짜 녹화 요약·상세 페이지 조회

기존 `/api/v2` 날짜 녹화 API와 소형 응답의 공개 필드는 유지한다. 목록·상세에 내부 조각 목록과 내부 파일 경로를 노출하지 않는다. 공개 누락 이력과 재생 시각 매핑은 축약하지 않고 아래 페이지 API에서 원래 순서와 값으로 제공한다.

## 조회 경로

| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| GET | `/api/v2/cameras/{camera_id}/daily-recordings/summary` | 날짜별 요약 목록 |
| GET | `/api/v2/daily-recordings/{recording_id}/summary` | 단일 날짜 요약 |
| GET | `/api/v2/daily-recordings/{recording_id}/gaps` | 원래 gaps 배열의 페이지 |
| GET | `/api/v2/daily-recordings/{recording_id}/available-intervals` | 원래 available_intervals 배열의 페이지 |
| GET | `/api/v2/recording-retention-status` | 보관 용량 추정·설정·저장 상태·현재 용량 대기 |

내부 게이트웨이 인증 책임과 기존 재생·다운로드·삭제·정리 계약은 동일하다. 미디어가 반환한 상대 경로를 게이트웨이의 인증된 경로로 중개한다. 이 서비스는 백엔드 인증 API를 호출하지 않는다. 응답은 `Cache-Control: no-store`다.

## 요약과 목록

요약은 기존 날짜 객체에서 `gaps`, `available_intervals` 두 배열을 제외한 공개 메타데이터다. `recording_id`, `camera_id`, `recording_date`, 상태, coverage, `has_gaps`, `recorded_seconds`, `revision`, 재생·다운로드 경로, 만료·정리 상태는 기존 의미를 유지한다. 배열이 생략됐다는 사실을 누락 없음으로 해석하지 않는다. 내부 `segments`·파일 경로는 제공하지 않는다.

목록 필터 `from_date`(포함), `to_date`(미포함), `status`, `limit`(1~100, 기본20), `cursor`는 기존 목록과 같다. 응답은 `{"items": [...], "next_cursor": null}`이다. 기존 목록과 요약 목록의 커서는 서로 사용할 수 없다.

## 상세 페이지

두 배열 경로는 `limit`(1~500, 기본100)과 `cursor`만 허용한다. 첫 요청에는 커서를 보내지 않는다.

```json
{
  "recording_id": "example-id",
  "revision": 12,
  "snapshot": "opaque-snapshot-id",
  "items": [
    {
      "start_at": "2026-01-01T00:00:00.000Z",
      "end_at": "2026-01-01T00:00:00.100Z",
      "reason_code": "UNKNOWN_CAPTURE_LOSS"
    }
  ],
  "total": 1250,
  "next_cursor": "opaque-signed-cursor"
}
```

`items`의 각 항목은 기존 해당 배열과 동일하다. 시각은 UTC 밀리초 `Z`, 길이·재생 오프셋은 기존 계약대로 초다. `total`은 해당 스냅샷의 항목 수이며 손실 프레임 수가 아니다. `next_cursor=null`이면 끝이다. 응답 바이트 제한에 따라 `limit`보다 적게 반환할 수 있으므로 종료는 항목 수 대신 커서로 판단한다.

커서는 녹화ID·배열 종류·limit·스냅샷에 묶인 HMAC 서명 값이며 발급부터1시간 유효하다. 내부 위치를 직접 만들거나 수정하지 않는다. 모든 페이지의 `snapshot`이 같아야 한 배열로 합칠 수 있다. 두 배열을 같은 기준으로 대조할 때에도 snapshot 일치를 확인한다. 녹화 중 자료는 변경될 수 있다.

| HTTP | error.code | 처리 |
| --- | --- | --- |
| 400 | `INVALID_CURSOR` | 잘못됨·만료·다른 대상/limit의 커서. 첫 페이지부터 다시 조회 |
| 400 | `INVALID_ARGUMENT` | 지원하지 않는 필터·limit·중복 파라미터 |
| 404 | `DAILY_RECORDING_NOT_FOUND` | 존재하지 않는 녹화 |
| 409 | `DAILY_SNAPSHOT_CHANGED` | 페이지 사이에 이력/상태 변경. 기존 페이지를 폐기하고 다시 조회 |
| 503 | `DAILY_RESPONSE_TOO_LARGE` | 기존 전체 응답이 안전 한도 초과. 요약과 페이지 API 사용 |
| 503 | `SERVICE_BUSY` | 카탈로그 조회 처리1개·대기3개 한도. 지연 후 재시도 |
| 503 | `CATALOG_METADATA_ERROR` | 저장 메타데이터가 유효하지 않거나 안전한 읽기 한도 초과 |

오류 본문은 기존 `{"error":{"code":"...","message":"..."}}` 형식이다. 기존 전체 목록·상세 및 호환 exports 상태 응답은4MiB 예산으로 보호한다. 한도 초과 시 일부 gaps를 조용히 제거한200 응답을 반환하지 않는다. 압축으로 이 제한을 우회하지 않는다.

## 보관 용량 상태

`/api/v2/recording-retention-status`는 설정·파일을 수정하지 않는 읽기 전용 경로다. 각 활성 카메라의 최근 마감 MP4 중 실제 파일 크기가 카탈로그와 일치하고 녹화 길이가 하루의90~110%인 표본을 사용한다. 표본 크기를 실제 재생 길이로 나눠 하루 및 설정 보관기간의 예상 바이트를 계산한다. 표본ID와 카메라별 설정·추정값을 반환한다.

- `status`: `insufficient_capacity` / `estimated_fit` / `unknown`. 표본·저장 상태를 확인할 수 없으면 충분하다고 추정하지 않는다. 추정 적합도 장기 무중단 보장은 아니다.
- `estimated_retention_bytes`: 모든 카메라의 표본이 있을 때 설정 기간 영상의 추정 합계. 미확인 카메라가 있으면null.
- `required_daily_budget_bytes_lower_bound`: 영상 보관 추정 + 보관 작업공간 하한.
- `required_media_budget_bytes_lower_bound`: 위 값 + JPEG 버퍼 상한.
- `required_filesystem_bytes_lower_bound`: 위 값 + 최소 여유 공간. 클립·메타데이터·캐시 증가분을 제외한 하한이다.
- `limiting_conditions`: 확인된 `daily_budget`, `media_budget`, `filesystem` 조건. 빈 배열만으로 여유를 확정하지 않는다.
- `storage`: 기존 `/api/v1/storage/status`와 같은 실제 관측과 시각. 파일시스템 여유와 논리 예산은 별개다.
- `recording_capacity_pending`: 카메라별 실제 용량 대기 구간·조건. 수신 시각(epoch초)과 녹화 대기를 실제 영상 확보로 해석하지 않는다.
- `policy_changed=false`: 자동 보관기간 변경·용량 확보를 위한 미만료 영상 삭제를 실행하지 않는다.

용량 부족 해소는 저장 장치·예산 증설 또는 승인된 보관 정책 변경이 필요하다. 이 조회 자체가 공간을 확보하지 않는다. 기존 만료 정리·원본 pin·중복 작업공간 보호를 유지한다.

## 백엔드 연결 순서

1. 날짜 화면은 요약 목록·요약 상세로 전환한다. 소형 기존 API 사용은 계속 가능하다.
2. 누락 표시·세밀한 재생 매핑이 필요할 때 해당 배열을 커서로 조회한다. 화면 목록 생성 때문에 모든 이력을 한 번에 합치지 않는다.
3. 플레이어는 기존 HLS 경로를 사용한다. 다운로드는 기존 준비된 MP4 경로를 사용한다. 페이지 조회가 영상 생성·재인코딩을 시작하지 않는다.
4. 409 스냅샷 변경과400 커서 만료 시 다시 조회하고,503은 오류/재시도로 표시한다. 삭제 후 재생410·정리 상태의 기존 의미를 유지한다.
5. 보관 상태 조회의 용량 경고를 별도 연결한다. 실제 정책 변경은 운영자의 승인된 설정 요청으로 처리한다.

서버 내부 읽기는 SQLite3.43 이상의 `octet_length`와 incremental blob API를 사용한다. 읽기 전용 트랜잭션과 희소 위치 인덱스를 사용해 큰 JSON을 통째로 파싱하지 않는다. 원본 DB 이력이나 영상을 이동·삭제하는 마이그레이션은 필요하지 않다. 이미 만료·삭제된 자료의 기존 정리 동작은 유지한다.
