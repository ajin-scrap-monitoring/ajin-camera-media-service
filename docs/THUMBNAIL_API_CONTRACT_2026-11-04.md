# 녹화 썸네일 API 계약

2026-11-04 요청서 대응. 구현 기준 2026-11-05. 담당 범위는 카메라 미디어 서비스다. 백엔드·게이트웨이·프론트엔드의 후속 구현은 별도다.

2026-11-07 녹화 중 날짜·TS 원본·자동 재시도·원본 종속 정리의 확장은 [날짜 대표 이미지 확장 계약](DAILY_THUMBNAIL_CONTRACT_2026-11-07.md)을 따른다. 아래의 closed/MP4 전용 설명은 확장 이전 범위이며 클립 계약은 유지한다.

## 호환과 지원 범위

기존 v1 클립 및 v2 날짜의 `media-info.thumbnail`은 아래 객체를 그대로 유지한다. 신규 필드는 기존 응답에 추가하지 않는다.

```json
{"status":"unsupported","representative_at":null,"content_path":null}
```

별도 API가 ready 이벤트/요청 구간 MP4와 closed·archive_state=mp4·export_state=ready 날짜 MP4를 지원한다. 현재 날짜, TS만 있는 날짜, 수집/처리 중 클립은 unavailable이다. 썸네일 작업은 영상 export나 클립 생성을 호출하지 않는다. 날짜 원본과 독립 클립은 각각의 수명주기를 따른다.

## 내부 HTTP API

| 대상 | 정보 GET | 준비 POST / JPEG GET·HEAD |
| --- | --- | --- |
| 클립 | `/api/v1/clips/{clip_id}/thumbnail-info` | `/api/v1/clips/{clip_id}/thumbnail` |
| 날짜 | `/api/v2/daily-recordings/{recording_id}/thumbnail-info` | `/api/v2/daily-recordings/{recording_id}/thumbnail` |

ID는 canonical 소문자 하이픈 UUID다. 이벤트 식별자가 아니다. 쿼리 필드는 허용하지 않는다. GET은 생성하지 않는다. POST는 `Content-Type: application/json`과 `{}` 또는 `{"retry_failed":true}`만 받는다. 영상 구간/시각/경로/카메라 변경은 받지 않는다.

신규 접수는 202, 이미 pending/ready 또는 미지원·준비 불가·재시도 미선택은 200이며 JSON 본문에 상태를 반환한다. POST는 `Location`에 해당 thumbnail-info 경로를 반환한다. 같은 ID·원본의 병렬 요청은 하나로 합친다. ready 재POST는 기존 결과를 재사용한다. failed 재시도는 retry_failed=true가 필요하다. unavailable은 원본 준비 후 명시적 POST로 다시 확인할 수 있다. API 클라이언트는 pending일 때 제한적으로 폴링하고 failed에 자동 무한 재시도하지 않는다.

## 응답 JSON

클립은 clip_id, 날짜는 recording_id를 사용하고 나머지 필드는 동일하다. 아래 UUID·시각은 설명용 예시다. 실제 검증 예시는 회신 문서에 기록한다.

```json
{
  "clip_id": "7b2f660a-4753-4cf0-a68b-a6463d4a5db1",
  "camera_id": "camera-main",
  "status": "ready",
  "representative_at": "2026-11-04T01:00:00.000Z",
  "time_basis": "server_received_at",
  "content_path": "/api/v1/clips/7b2f660a-4753-4cf0-a68b-a6463d4a5db1/thumbnail",
  "width": 640,
  "height": 360,
  "size_bytes": 18542,
  "reason_code": null,
  "generated_at": "2026-11-04T01:01:02.000Z"
}
```

| status | 필드 규칙 |
| --- | --- |
| unsupported | 기능 비활성화. 시각·경로·치수·크기·generated_at·reason_code 모두 null |
| unavailable | 유효 이미지 미준비/원본 조건 불충족. reason_code 외의 위 필드 null |
| pending | 대기와 실행을 합친 상태. 시각·경로·치수·크기·generated_at·reason_code 모두 null |
| failed | 제한 시간·저장소·생성 등 실패. reason_code 외의 위 필드 null |
| ready | 실제 JPEG·치수·바이트·생성 시각·내부 상대 경로 존재. 대표 시각 근거가 없으면 아래 null 규칙 적용 |

미준비 예: `status=unavailable, reason_code=NOT_PREPARED`. 대표 시각이 입증되지 않는 ready 파일도 이미지 제공은 허용하며 `representative_at=null`, `time_basis=null`, `reason_code=TIMELINE_UNAVAILABLE`로 반환한다. null을 카메라 촬영 시각이나 임의 중앙 시각으로 대체하지 않는다. camera_id와 요청 ID를 백엔드가 기존 상세와 대조한다. 내부 파일 경로·예외 본문·원본 fingerprint는 응답하지 않는다.

대표 프레임은 MP4 안의 실제 첫 디코딩 프레임이다. 누락 구간을 채운 임의 이미지나 검은 대체 그림을 만들지 않는다. 실제 검은 프레임은 정상 영상으로 인정한다. 신규 파일 완료 콜백이 동일 파일 identity에 연결한 녹화 타임라인을 제공한 경우 첫 유효 구간 시작을 representative_at으로 반환한다. 누락된 요청 시작이나 중앙을 사용하지 않는다. 프레임의 원본 카메라 촬영 시각을 보장하지 않는다. 타임라인은 기존 서버 수신 시각/녹화 매핑의 정확도와 프레임·키프레임 절단 오차를 그대로 따른다. 과거 파일에서 매핑의 동일성 근거가 없으면 시각은 null이다. 모든 시각 문자열은 UTC ISO 8601 밀리초 Z 형식이다.

## 생성·저장 제한

- 실제 JPEG, 최대640×360 경계, 종횡비 유지, 확대하지 않음. width/height/size_bytes는 생성 파일 검사 결과다.
- 기본 최대524288bytes(512KiB). 초과 시 실패하며 성공 경로를 제공하지 않는다.
- 디코딩 동시성1, admission 기본4개(실행 포함), 작업당15초. 큐 대기 시간은15초에서 제외되므로 전체 완료 시간을15초로 보장하지 않는다. 작업 준비·디스크 지연도 별도다. FFmpeg 디코더·필터·JPEG 인코더는 각1스레드 제한이다.
- `THUMBNAILS_ENABLED=true`, `MAX_THUMBNAIL_JOBS=4`(1..16), `THUMBNAIL_TIMEOUT_SECONDS=15`(1..30), `MAX_THUMBNAIL_BYTES=524288`(16384..524288). 비활성화는 기존 동영상 기능에 영향이 없다.
- 신규 ready 영상 완료 뒤 제한된 백그라운드 준비를 시도한다. 알림 큐64개, 0.25초마다 최대4개 확인, 생성 admission 초과는 failed/CAPACITY_EXCEEDED이며 명시적 재시도가 필요하다. 알림 큐마저 꽉 찬 경우 자동 준비가 생략될 수 있으므로 NOT_PREPARED도 POST로 보완한다.
- 과거 영상은 선택한 ID의 POST로만 준비한다. 시작·목록·GET이 전체 원본을 검사/변환하지 않는다. 재시작 후 ready 이미지·메타데이터를 재사용하고 미완료 pending은 JOB_INTERRUPTED로 복구한다. 강제 종료 후 남은 pending도 조회 시 복구한다.
- 같은 SD의 서비스 루트 아래 clip-thumbnails·daily-thumbnails와 별도 thumbnails.sqlite3에 저장한다. 임시 JPEG도 용량에 포함한다. 날짜 썸네일은 기존 날짜 예산, 전체 파일/DB/임시 파일은 기존 전체 예산에 포함한다. 보관 기간·기존 예산·최소 여유 공간을 변경하지 않는다.
- 원본의 device/inode/size/mtime/ctime identity를 연결해 교체 시 무효화한다. JPEG 교체/누락도 감지한다. 운영 중 서비스 소유 파일을 외부에서 수정하지 않는다.
- 명시적 삭제는 생성 취소·FFmpeg 종료를 기다리고 원본과 이미지를 정리한다. 삭제 완료 전 동시 재준비를 직렬화한다. 만료 시 새 요청은 즉시410이며, 진행 중 읽기/저장소 오류로 물리 정리는 늦을 수 있다. 날짜는 기존 cleanup_state·space_reclaimed 계약을 유지하고, 클립의 선택 이미지 정리 오류는 원본 삭제 성공을 실패로 뒤집지 않고 백그라운드 정리한다. 정리 재시도는 기존 thumbnail 행32개씩 순회하므로 즉시 공간 회수를 보장하지 않는다.

## HTTP·오류

정상 이미지 GET/HEAD는 200, `Content-Type: image/jpeg`, 실제 `Content-Length`다. HEAD 본문은 비어 있다. JPEG는 작으므로 Range 분할 전송을 계약하지 않으며 전체200으로 응답한다. JSON·이미지·오류는 `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`다. 조건부 캐시/리다이렉트는 사용하지 않는다.

| 상황 | 응답 |
| --- | --- |
| 잘못된 UUID/쿼리/POST 필드·타입 | 400 INVALID_ARGUMENT |
| 알 수 없는 ID/등록되지 않은 카메라 | 404 |
| 원본 삭제·만료 | JSON·POST·이미지 모두410. 부모 자원의 기존 오류 코드 유지 |
| 이미지 pending | 409 THUMBNAIL_NOT_READY |
| 이미지 unsupported/unavailable | 404 THUMBNAIL_UNAVAILABLE |
| 이미지 failed | 503 THUMBNAIL_UNAVAILABLE |
| admission 가득 참 | POST503 THUMBNAIL_CAPACITY_EXCEEDED |
| 선택 기능 저장소를 시작할 수 없음/종료 중 | 503 THUMBNAIL_STORAGE_UNAVAILABLE |
| 기타 디스크/DB 오류 | 기존 API 오류 경계의503 STORAGE_UNAVAILABLE |

JSON reason_code: NOT_PREPARED, SOURCE_NOT_READY, DAILY_MP4_REQUIRED, SOURCE_UNAVAILABLE, SOURCE_CHANGED, IMAGE_CHANGED, IMAGE_UNAVAILABLE, NO_VIDEO_FRAME, TIMELINE_UNAVAILABLE, GENERATION_TIMEOUT, GENERATION_FAILED, INVALID_IMAGE, IMAGE_TOO_LARGE, STORAGE_UNAVAILABLE, CAPACITY_EXCEEDED, JOB_INTERRUPTED. 기존 저장 공간 검사에서 발생한 INSUFFICIENT_STORAGE 등은 기존 저장소 계약을 따른다. reason_code가 있어도 status=ready/TIMELINE_UNAVAILABLE이면 이미지 사용이 가능하다. 동영상은 썸네일 실패 때문에 failed로 바뀌지 않는다.

오류 본문은 기존 형식 `{"error":{"code":"...","message":"..."}}`다. 백엔드의 UUID 검증422·인증401과 미디어400을 혼동하지 않는다.

## 백엔드 연결 작업

현재 미디어 v1/v2 HTTP API에는 API 토큰 인증이 없다. 과거 제거된 MEDIA_API_TOKEN을 다시 도입하지 않는다. 기존 전용 Docker 내부망·호스트 loopback 경계를 유지하고 브라우저가 미디어에 직접 접근하지 않도록 한다. 요청서의 ‘기존 v1 서버 간 토큰’ 전제는 현재 구현과 다르다. 엣지 ingest 인증과 미디어 HTTP 인증은 별개다.

백엔드는 전용 JSON 스키마·POST 준비 프록시를 추가하고, 로그인/카메라·대상 권한 확인과 미디어 반환 ID/camera_id/고정 내부 경로 대조를 수행한다. 외부 URL·임의 호스트·쿠키 전달·경로 이동은 허용하지 않는다. Nginx 이미지 GET/HEAD마다 세션과 대상 접근을 확인하고 미디어를 upstream으로 사용한다. FastAPI에서 이미지 바이트를 변환·저장·중계하지 않는다.

공개 JSON 후보는 요청서의 `/api/v1/media/clips/{clip_id}/thumbnail-info`, `/api/v2/recordings/daily/{recording_id}/thumbnail-info`, 게이트웨이 후보는 `/media/clips/{clip_id}/thumbnail`, `/media/daily-recordings/{recording_id}/thumbnail`이다. 이는 백엔드가 구현할 경로이며 미디어 배포만으로 생성되지 않는다. 내부 content_path는 요청 ID에 맞춘 위 고정 경로와 정확히 일치할 때만 변환한다. 최종 공개 경로·인증 검사 구현은 백엔드에서 확정한다. 기존 media-info 스키마는 바꾸지 않는다.
