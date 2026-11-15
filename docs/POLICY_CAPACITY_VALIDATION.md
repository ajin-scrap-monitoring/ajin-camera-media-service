# 녹화 설정 API 용량 검증·경고

설정 GET의 본문과 기존 PUT 요청 필드는 유지한다. PUT 응답에 `capacity_validation`을 추가하며, 별도 POST 사전 검증은 설정을 저장하지 않는다. 예측 용량 부족은 유효한 설정을 자동 변경하거나 기존 영상을 삭제할 근거가 아니다.

| 메서드 | 경로 | 동작 |
| --- | --- | --- |
| GET | `/api/v2/cameras/{camera_id}/daily-recording-policy` | 기존 상시 녹화 설정 |
| PUT | 같은 경로 | 요청 설정을 검증·저장하고 용량 결과 반환 |
| POST | 같은 경로 + `/validate` | 요청 설정의 용량 검증만 수행 |
| GET | `/api/v1/cameras/{camera_id}/recording-policy` | 기존 이벤트 설정 |
| PUT | 같은 경로 | 요청 이벤트 설정 저장 + 용량 결과 |
| POST | 같은 경로 + `/validate` | 이벤트 설정 사전 검증만 수행 |

요청은 기존 JSON 그대로다. 상시 녹화는 `{"enabled":true,"retention_days":30}`, 이벤트는 `{"pre_roll_seconds":30,"post_roll_seconds":30,"retention_days":30}`이다. 필수 필드·타입·허용 범위는 기존과 같으며 추가 요청 필드는 허용하지 않는다. 응답의 capacity_validation을 다시 PUT 요청에 포함하지 않는다.

상시 녹화 응답의 축약 예시:

```json
{
  "enabled": true,
  "retention_days": 30,
  "capacity_validation": {
    "status": "insufficient_capacity",
    "evaluated_at": "2026-01-01T00:00:00.000Z",
    "policy_applied": false,
    "warnings": [{
      "code": "RETENTION_CAPACITY_EXCEEDED",
      "message": "Estimated retention exceeds configured capacity.",
      "conditions": ["daily_budget"]
    }],
    "current": {
      "status": "available",
      "observed_at": "2026-01-01T00:00:00.000Z",
      "budget": {},
      "available_recording_bytes": 1000000,
      "limiting_conditions": [],
      "reason_code": null
    },
    "retention": {},
    "reserves_space": false,
    "existing_recordings_modified": false
  }
}
```

`budget`와`retention`은 위 예시에서만 축약했다. 모든 바이트는 정수byte, 시각은 UTC ISO8601 `Z`다.

## 판정과 저장 의미

- 유효한 PUT은 기존처럼200이다. 요청한 값만 저장하며 `policy_applied=true`다. 용량 경고가 있어도 보관기간을 임의 축소하지 않는다.
- POST `/validate`는200과 `policy_applied=false`를 반환한다. 정책 테이블·녹화 enable 이력·기존 파일·만료 시각을 변경하지 않는다.
- 잘못된 입력은 기존400 `INVALID_ARGUMENT`이며 저장하지 않는다. 미등록 카메라는404다. 카탈로그/마운트 접근 자체가 불가능하면 기존503 저장 오류가 가능하다.
- 검증 요청은 처리1개·대기3개로 제한하고, 초과는503 `SERVICE_BUSY`다. 요청 취소 시에도 이미 시작한 파일 작업이 끝날 때까지 슬롯을 유지한다.
- GET 응답에는 추가 검증 필드가 없다. 인증 게이트웨이와 기존 오류 본문 형식은 그대로다. 설정 응답은 `Cache-Control: no-store`다.

`capacity_validation.status`:

| 값 | 의미 |
| --- | --- |
| `insufficient_capacity` | 현재 기록할 여유가 없거나 요청 기간의 예측량이 한도 초과 |
| `estimated_fit` | 현재 공간 확인과 모든 카메라 표본을 사용한 추정이 한도 내. 장기 보관 보장은 아님 |
| `unknown` | 표본·현재 공간 관측·클립 발생 빈도 등의 근거가 부족 |

경고 코드는 기계 판독용이며 message를 조건 분기로 사용하지 않는다.

| warnings[].code | 의미 |
| --- | --- |
| `RETENTION_CAPACITY_EXCEEDED` | 제안된 상시 보관기간의 예상 사용량 초과 |
| `CURRENT_STORAGE_INSUFFICIENT` | 버퍼·예약·작업공간·최소 여유 반영 후 현재 녹화 여유 없음 |
| `CAPACITY_ESTIMATE_UNAVAILABLE` | 상시 녹화 표본 또는 보관량 추정 근거 부족 |
| `STORAGE_OBSERVATION_UNAVAILABLE` | 현재 저장 공간 확인 실패·예약 상태 변화 등 |
| `CLIP_RETENTION_ESTIMATE_UNAVAILABLE` | 이벤트 빈도·산출물 크기 근거가 없어 이벤트 보관량 추정 불가 |

## 계산과 한계

`current`는 해당 정책 종류의 실제 admission과 같은 계산을 사용한다. 상시 녹화 전용 보관 작업공간 하한은 상시 정책에 적용하고, 이벤트 정책은 기존 이벤트 admission 조건으로 검사한다. 날짜 사용량, 전체 미디어 사용량, 다른 작업의 미할당 예약, 이미 작성된 예약 산출물의 차감, 보관 작업공간, JPEG 버퍼의 미사용 여유, 파일시스템 최소 여유를 반영한다. `available_recording_bytes`는 이 조건 중 가장 작은 추가 녹화 여유를0 이상으로 표시한다. 이벤트 전용 구성에서는 기존 전체 공간 검사 결과를 사용하고 상세 산정이 없으면 바이트는null이다. 검사 자체가 저장 거절 통계나 예약을 증가시키지 않는다.

현재 공간 한계의 `daily_budget`, `total_budget`, `minimum_free`와 보관 추정 한계의 `daily_budget`, `media_budget`, `filesystem`은 기존 계산 출처를 구분하는 코드다. 실제 저장소 오류·마운트 상태는 물리 여유0과 구분한다.

`retention`은 제안된 상시 정책을 메모리에서 대입한 전체 카메라의 용량 추정이다. 다른 카메라의 정책도 포함하며, 단일 요청 카메라의 여유만 보고 충분하다고 판단하지 않는다. 기존 `/api/v2/recording-retention-status`와 같은 최근 마감 MP4 표본·하한 계산을 사용하며 표본ID·카메라별 제안 정책·추정 바이트를 포함한다. 물리 상태 또는 표본이 불확실하면 충분하다고 추정하지 않는다. 이벤트 설정의 retention은null이며 이벤트 발생 빈도 없이 날짜 녹화량을 이벤트 보관량으로 대신 계산하지 않는다.

이 검증은 공간 예약이 아니며, 설정 이후 입력량·다른 작업·실제 여유가 달라질 수 있다. 실제 녹화·추출은 기존 admission 검사에서 공간 부족 시503 `INSUFFICIENT_STORAGE` 등 기존 오류를 반환한다. API 설정 경고를 해결 완료나30일 보장으로 표시하지 않는다.

사용자가 명시적으로 녹화를 끄거나 보관기간을 줄이는 요청은 현재 용량 부족 때문에 막지 않는다. 기존 날짜 녹화에 이미 복사된 retention_days·expires_at, 기존 클립의 expires_at을 소급 변경하지 않는다. 사전 검증이나 설정 변경 자체가 기존 영상을 정리하지 않는다. 기존 만료 정리는 기존 만료 기준대로 계속 동작한다.

## 백엔드 연결

설정 화면은 필요하면 POST `/validate`로 미리 확인하고, 실제 저장은 기존 PUT을 사용한다. PUT 응답의 기존 설정 필드를 저장 결과로 사용하고 capacity_validation을 경고로 표시한다. `policy_applied`와 경고를 함께 보여 저장 여부와 보관 가능성을 구분한다. 판단 불가를 정상으로 표시하지 않는다. 서버 예측치를 임의의 확정 최대 보관일로 변환하지 않는다. 이후 운영 중 변동은 기존 보관 상태 조회 API로 확인한다.
