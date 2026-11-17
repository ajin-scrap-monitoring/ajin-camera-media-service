# 날짜 녹화 exports 경량 상태 응답

기존 `POST /api/v2/daily-recordings/{recording_id}/exports`는 그대로 유지한다. 대형 누락 이력이 있는 날짜에는 아래 경량 경로를 사용한다.

## 확정 계약

`POST /api/v2/daily-recordings/{recording_id}/exports/summary`

- 쿼리 인수 없음. `?view=summary`를 포함한 알 수 없는 인수는400이다.
- 본문 없음 또는 빈 JSON 객체 `{}`만 허용한다. JSON을 보내면 `Content-Type: application/json`을 사용한다.
- 응답 DTO는 기존 `GET /api/v2/daily-recordings/{recording_id}/summary`와 같은 공개 필드·단위·의미다. `gaps`, `available_intervals`, 내부 `segments`는 포함하지 않는다. 생략을 빈 배열이나 누락 없음으로 대체하지 않는다.
- `export_state=ready`면200, 그 외 통과한 상태는202다. `processing`, `failed`, `not_requested` 및 error 값은 그대로 반환한다. failed를 성공 완료로 표시하지 않는다.
- `Location: /api/v2/daily-recordings/{recording_id}/summary`이며 성공 응답은 `Cache-Control: no-store`다. 실제 다운로드는 기존 download_path/content 경로를 사용한다.
- 이 POST는 기존과 같은 **호환용 상태 확인**이다. 새 보관 파일 작업·재인코딩·재시도 job을 시작하지 않는다. 일반 GET 요약만으로 대체하면 파일 존재 등의 검사 의미가 달라진다.

요약은 recording_id·camera_id·recording_date·timezone·시간창·status·coverage·has_gaps·확정/재생 시각·recorded_seconds·revision·playback/export/archive 상태·경로·실제 크기·만료·정리 상태 등 기존 공개 메타데이터다. 시각은 기존 UTC ISO8601 `Z`, 길이는초, 크기는byte, retention_days는일이다. 필드 존재/nullable 여부는 기존 요약과 동일하다. 내부 파일 경로는 반환하지 않는다.

## 오류와 검사

기존 export 가능 여부, 삭제/만료 및 실제 준비된 MP4 파일 검사를 공유한다. 경량 경로에만 검사 없는 성공 응답을 만들지 않는다.

| HTTP | error.code | 조건 |
| --- | --- | --- |
| 400 | `INVALID_ARGUMENT` | 허용하지 않는 쿼리·본문 |
| 404 | `DAILY_RECORDING_NOT_FOUND` | 존재하지 않는 녹화 |
| 409 | `DAY_NOT_CLOSED` | 오늘·닫히지 않은 날짜 |
| 409 | `DAY_FINALIZING` | 날짜 마감 중 |
| 409 | `NO_RECORDING_DATA` | 저장된 영상 길이 없음 |
| 410 | `DAILY_RECORDING_DELETED` | 논리 삭제 |
| 410 | `DAILY_RECORDING_EXPIRED` | 상태 또는 expires_at 기준 만료 |
| 503 | `RECORDING_STORAGE_ERROR` | MP4 보관 상태인데 실제 MP4가 없음 |
| 503 | `CATALOG_SUMMARY_UNAVAILABLE` | 경량 메타데이터 캐시 누락·64KiB 초과·유효하지 않음 |

마운트·DB 등의 저장소 오류는 기존503 오류로 처리한다. 오류 본문은 기존 `{"error":{"code":"...","message":"..."}}` 형태다. 기존 전체 exports의4MiB 보호와 백엔드의8MiB 보호를 완화하지 않는다. 기존 전체 경로의 Location은 기존 상세 경로를 유지한다.

## 대형 이력을 읽지 않는 방식

경량 경로는 daily_states의 크기가 제한된 스칼라 메타데이터만 읽고, 최신 revision 카운터를 반영해 공개 필드를 반환한다. 기본/변경 스냅샷과 누락 배열·재생 매핑 배열·희소 인덱스를 조회하거나 재구성하지 않는다. 크기는 SQLite octet_length로 확인하며 큰 JSON을 로드한 뒤 필터링하지 않는다.

캐시가 없거나 유효하지 않을 때는503을 반환하고, 원본 배열 replay로 우회하지 않는다. 일반 서비스 초기화는 이 캐시를 구축하지만, 손상된 상태의 복구 자체를 이 경량 요청이 실행하지 않는다. 카탈로그 잠금을 소유한 상태에서 검사와 파일 stat을 작업 스레드에서 수행하고, 취소돼도 작업 종료까지 잠금을 유지한다.

## 백엔드 연결 범위

카메라 소유 관계 확인에 필요한 선조회는 기존 요약 경로를 사용할 수 있다. 이후 경량 exports POST를 호출하고 요약 DTO로 처리한다. 전체 detail 선조회나 전체 DailyRecording 응답 파싱을 함께 남기면 경로 전체가 경량화되지 않는다.

인증·권한·CSRF·공개 다운로드 경로 중개는 기존 백엔드 책임이다. 미디어는 새 인증값을 요구하지 않는다. 프론트가 요약 DTO를 이미 수용하더라도 실제 백엔드 연결·화면 E2E 결과는 별도로 확인해야 한다. 이 계약은 새 파일 생성이나 임의 촬영 시각 탐색 기능을 포함하지 않는다.
