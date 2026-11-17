# 용량 압박 시 날짜 녹화 자동 정리

기본 보관 정책은 기존30일이다. 이 기능을 명시적으로 활성화하면 용량 압박으로30일 미만 날짜도 삭제될 수 있다. 최소30일 보관을 보장하지 않는다. 날짜/전체 예산·마운트·최소 여유를 변경하거나 다른 저장소를 사용하지 않는다.

## 대상과 정리 절차

`closed` + `archive_state=mp4` + `export_state=ready`이며 실제 MP4 크기가 카탈로그와 일치하는 날짜만 후보이다. 날짜 종료 시각(UTC epoch), 동률이면 recording_id 순으로 오래된 자료부터 정리한다. 후보 탐색은 오래된 날짜부터 최대1000건이며 초과 시 탐색 한계를 보고한다.

활성 날짜·마감/변환·원본 압축 정리·사용 중 파일 잠금·클립 추출/메타데이터/썸네일/HLS 변환 참조·HTTP MP4/HLS 전송 중 참조를 보호한다. HTTP 참조는 Range/HEAD를 포함한 요청 종료와 취소 시까지 유지한다. 클라이언트가 재생 화면만 열어 둔 채 요청하지 않는 시간 전체를 무기한 보호하는 계약은 아니다.

날짜에 속한 `daily/{id}`, `daily-playback/{id}`, `daily-exports/{id}.{mp4,tmp.mp4,ffconcat}`, `daily-thumbnails/{id}.jpg`만 처리한다. 이벤트 클립·원본 JPEG 버퍼·백엔드 DB·백업·인증/설정·원본 이벤트 이력·영구 식별/중복 검증 기록은 대상이 아니다. 후보 부족 시 다른 범주로 확대하지 않는다.

기존 삭제 잠금·카탈로그 tombstone·cleanup 큐·revision 갱신을 재사용한다. 선택 및 논리 삭제 직전에 보호·범위·크기를 재검사한다. Linux의 no-follow 디렉터리 descriptor와 같은 파일시스템 검증을 사용하며 심볼릭 링크·하드링크·특수 파일·하위 마운트·깊이/탐색 한도 초과는 거절한다. 안전한 descriptor 삭제를 지원하지 않는 플랫폼에서는 용량 정리를 실행하지 않는다. 정상 만료/수동 삭제 계약은 유지한다.

용량 정리의 논리 삭제는 기존 `status=deleted`, `deleted_at`, `cleanup_state` 등과 선택 필드 `deletion_reason=capacity_pressure`로 남는다. 이 필드는 전체/요약 DTO에 선택적으로 추가되며 기존 자료에 없을 수 있다. `expired`로 바꿔 촬영 누락과 혼동하지 않는다. 기존410·cleanup 실패/재시도·revision/커서 무효화·경량 exports 계약을 유지한다. 대형 gaps/available_intervals 이력은 삭제하거나 전체 재해석하지 않는다.

## admission과 같은 압박률

`DailyRecordingService.inspect_capacity(recording=True)`의 같은 관측 budget을 사용한다.

- `R = unallocated_reserved_bytes + additional_reserve_bytes`
- `W = workspace_bytes`, `B = buffer_headroom_bytes`
- 날짜 압박률 = `(daily_used_bytes + R + W) / daily_limit_bytes × 100`
- 전체 압박률 = `(media_used_bytes + R + W + B) / media_limit_bytes × 100`. 전체 예산 미설정이면 이 항목은 없다.
- 물리 공간 압박률 = `(filesystem_total_bytes - filesystem_free_bytes + R + W + B + minimum_free_bytes) / filesystem_total_bytes × 100`

기존 admission이 이미 예약·출력으로 계산한 보관 작업에 작업공간 하한을 다시 더하지 않는다. 이때 W는0일 수 있다. 값 누락·유효하지 않은 관측·마운트 가드 실패는 삭제 권한이 되지 않는다. 기존 admission의 실제 거절은 각 분자>분모/최소여유 조건이며 관측 `shortfall_bytes=max(0,used_bytes-limit_bytes)`로 부족분을 제공한다. 검사 이후 새 작업이 추가될 수 있으므로90% 시작만으로 무중단을 보장하지 않는다.

어느 관련 압박률이라도90% 이상이면 active가 되고, 관련 압박률 모두85% 이하가 될 때 해제한다. 85~90%에서는 active를 유지한다. 95%는 긴급 상태이며 정리 실패 자체를 뜻하지 않는다. 압박률·현재 예약·여유는 작업마다 달라질 수 있다.

## 환경변수

접두사는 모두 `MEDIA_CAPACITY_CLEANUP_`이다. **변경 반영에는 미디어 서비스 재시작이 필요**하다. JSON 재읽기나 삭제 실행 HTTP API는 제공하지 않는다.

| 뒤에 붙이는 이름 | 기본값 | 허용 범위/의미 |
| --- | ---: | --- |
| ENABLED | false | true/false, 명시적 활성화 |
| STOP_PERCENT | 85 | 정수1~99 |
| START_PERCENT | 90 | 정수2~99 |
| CRITICAL_PERCENT | 95 | 정수3~100; `0 < stop < start < critical <=100` |
| INTERVAL_SECONDS | 30 | 정수5~3600초. 기존 유지보수 루프 간격5초 및 진행 작업에 의해 실제 시작은 늦어질 수 있음 |
| MAX_DAYS | 1 | 정수1~100, 한 순회에서 정리할 날짜 녹화 **건수** |
| MAX_BYTES | 17179869184 | 정수1~1099511627776byte, 한 순회의 대상 실제 파일 논리 크기 합계 상한 |
| RETRY_SECONDS | 60 | 정수5~900초, 연속 실패 시2배 증가·최대900초 |
| MAX_FAILURES | 3 | 정수1~10, 연속 실패 상한에 도달하면 suspended |
| STALL_SECONDS | 300 | 정수30~86400초, active에서 실제 회수 진전이 없는 정체 판단 |
| NOTIFICATION_SECONDS | 900 | 정수60~86400초,95% 긴급 로그 반복 억제 |
| RESET_ID | 빈 문자열 | 최대64자, 제어문자 불가. 운영자가 원인 해결 후 새 값을 지정하고 재시작하면 실패 횟수/재시도 대기만 초기화 |

잘못된 값·순서·알 수 없는 접두사 설정은 **자동 정리만 비활성화**하고 `INVALID_CAPACITY_CLEANUP_CONFIG`를 보고한다. 녹화 서비스를 중단하거나 넓은 삭제 기본값으로 활성화하지 않는다. 기존 기본30일 정책과 수동/정상 만료 정리는 그대로다. 보관 정책을 API로 변경하면 그 정책이 별도로 적용되며 이 기능이30일로 강제 덮어쓰지 않는다.

한 날짜가 남은 byte 상한보다 크면 `TARGET_EXCEEDS_LIMIT`로 중단하며 더 큰 삭제나 범위 확대를 하지 않는다. SQLite에 active·미완료 대상·시도/실패/알림 상태를 보존해 재시작 후 이어 간다. 미완료 용량 정리는 일반5초 정리 큐가 무제한 재시도하지 않으며 이 정책의 대기/실패 상한을 따른다. disabled/suspended이면 미완료 용량 정리도 자동 재개하지 않는다. RESET_ID는 삭제 대상·예산·보호 범위를 확대하지 않는다.

파일 제거와 실제 공간 회수는 별도다. 삭제 전후 물리 free 증가와 날짜 사용량 감소를 모두 관찰해야 `space_recovery_confirmed=true`이다. 그 차이는 동시 녹화·다른 작업의 영향을 받는 **관측 순변화**이며 삭제 파일의 정확한 블록 수나 누적 절대 회수량이 아니다. 회수가 확인되지 않으면 해당 순회를 중단하고 제한된 재시도/중단 정책으로 처리한다. `estimated_other_growth_bytes`도 파일 크기와 순변화로 산출한 추정치이며 생성량 우세의 진단 근거이지 원인 확정값은 아니다.

## 조회·로그·백엔드 후속 연결

`GET /api/v2/capacity-cleanup/status` — 쿼리/본문 없음,200 JSON, `Cache-Control:no-store`. 인증은 기존 내부 미디어/백엔드 게이트웨이 책임과 같다. 캐시된 작은 상태만 반환하며 GET으로 측정·정리·재시도·인코딩을 실행하지 않는다.

주요 필드:

- `enabled`, `configuration_error`, `policy`: 실제 유효 정책. 잘못된 설정은 disabled다.
- `active`:90/85 hysteresis 상태. `mode`: idle/active/blocked/cooldown/suspended/error/disabled.
- `observed_at`, `valid_until_at`, `state_fresh`: 관측 시각·유효기한(관측+2×주기+10초)·신선도. 초기/측정 실패/오래된 상태를 정상으로 추정하지 않는다.
- `measurement.budget`: 기존 admission 값과 `filesystem_total_bytes`.
- `measurement.pressures.{daily_budget,total_budget,minimum_free}`: 각 `used_bytes`, `limit_bytes`, `percent`, `shortfall_bytes`. ratio 판정은 정수 비교이며 percent만 소수6자리 표시다.
- `critical`: 현재 측정에서95% 이상 여부. `alert_latched`: 긴급 상태가85% 회복까지 유지되는 알림 상태.
- `notification_sequence`, `last_notification_at`: 긴급 진입·억제 기간 후 반복·85% 회복 시 갱신, 재시작 후 유지.
- `reason_code`, `stalled`, `retry_at`, `consecutive_failures`: 중단/정체/재시도 정보. epoch인 `*_at` 숫자와 UTC ISO8601 문자열 필드를 구분한다.
- `attempts`, `successful_days`, `observed_reclaimed_bytes`: 누적 시도·파일 정리 완료 건수·관측된 free 양의 순증가 합. `successful_days`만으로 공간 회수를 보장하지 않는다.
- `pending`: 중단 복구용 선택된 날짜·예상 bytes·이전 budget, 없으면null.
- `last_attempt`: 대상ID·시도/완료 UTC·예상 bytes·success·회수 확인·관측 free 증가/날짜 사용 감소·기타 증가 추정·error_code. 없으면null.
- `scope=closed_ready_daily_mp4`, `default_retention_days=30`, `minimum_retention_guaranteed=false`.

주요 reason_code는 `TARGETS_EXHAUSTED`, `PROTECTED_TARGETS_ONLY`, `CANDIDATE_SCAN_LIMIT`, `TARGET_EXCEEDS_LIMIT`, 기존409 코드(보호/잠금/재검사 불일치), `CYCLE_LIMIT_REACHED`, `DELETE_ERROR`, `SPACE_NOT_RECLAIMED`, `GENERATION_OUTPACES_CLEANUP`, `FAILURE_LIMIT_REACHED`, `MEASUREMENT_UNAVAILABLE`다. DELETE_ERROR가 나면 pending 원본과 기존 cleanup_state/cleanup_error를 함께 확인한다. journal 손상·저장 실패는 `configuration_error=CLEANUP_STATE_UNAVAILABLE`로 정리만 중지한다.

긴급 로그 `capacity_cleanup_critical`, 회복 로그 `capacity_cleanup_recovered`, 실패 로그 `capacity_cleanup_failed`를 제공한다. 미디어는 메일을 보내지 않는다. 백엔드는 이 계약을 연결해 날짜/전체 예산95%·정리 불가/정체를 기존 운영 메일에 전달하고 실제 통보·회복·중복 억제를 검증해야 한다. **기존 서버75% 경고·90% 긴급메일과 server-capacity-alerts.txt는 변경하지 않는다.** 기존 물리90% 메일만으로 이 정책의95% 알림 연결 완료라고 표시하지 않는다. 새 대시보드 관리 화면은 필요하지 않다.

삭제된 날짜 조회는 기존 상태·deletion_reason·deleted_at·cleanup 상태를 표시하고, 재생/다운로드·exports410을 성공으로 바꾸지 않는다. 경량 DTO에서 생략 배열을 누락 없음으로 채우지 않으며 revision 변경 시 기존 커서를 재사용하지 않는다.
