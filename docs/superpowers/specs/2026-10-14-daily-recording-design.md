# 날짜별 상시 녹화 설계안

작성일: 2026-10-14, Asia/Seoul. **구현·백엔드 합의 전 제안**이다.

## 요청과 검토 범위

사용자 요청은 한국 날짜별 상시 녹화, 하루 한 건 조회·재생, 종료된 날짜의 단일 MP4 다운로드, 오늘의 완료 구간 재생, 단절 시 부분 저장 및 누락 시간 표시다. 이벤트 유무와 시청자 유무에 관계없이 녹화해야 한다.

외부 참고 자료: [DAILY_RECORDING_API_PROPOSAL.md](<C:/Users/jo/Documents/카카오톡 받은 파일/DAILY_RECORDING_API_PROPOSAL.md>). 자료의 API v2 경로, 10분 분할, 보관·게이트웨이 방식은 제안이며 현재 구현이나 사용자 확정 지시로 취급하지 않았다. 본 문서는 미디어 서비스의 변경과 협업 경계만 다룬다. 상대 백엔드·프론트 코드를 구현하거나 외부 시스템을 변경하지 않는다.

검토 기준: 로컬 main `d84e659`, 현재 미커밋 운영 문서·배포 설정. 이번 검토는 코드 읽기와 서버 디스크 조회이며 신규 기능을 실행·검증한 결과가 아니다.

## 현재 구현과 차이

| 항목 | 현재 상태 | 추가 또는 변경 |
|---|---|---|
| 엣지 JPEG/WSS 수신, 수신 시각 | 구현 | 그대로 사용 |
| HTTP MJPEG 실시간 영상 | 구현 | 그대로 유지 |
| 이벤트 녹화 설정·클립 접수 | 구현 | v1 계약 유지, 날짜 녹화와 분리 |
| JPEG 순환 버퍼 | 최근 최대 300초, 전체 기본 2GiB | 상시 녹화용 단기 프레임 사용권 추가; 하루 전체 JPEG를 보관하지 않음 |
| H.264/MP4 인코딩 | 이벤트 구간에만 실행 | 날짜 녹화용 반복 인코딩 및 자원 제한 추가 |
| 저장 영상 Range·다운로드·삭제 | 이벤트 MP4만 지원 | 파일 전송 로직 재사용, 날짜 영상 관리 API 추가 |
| `coverage`, `gaps` | 이벤트 시간창 기준, 1초 초과 프레임 간격 | 날짜 시간창·원인·현재 날짜 판정 경계로 확장 |
| 재시작 복구 | 진행 중 이벤트 실패, 완료 파일 유지 | 완료 조각 복구·당일 이어쓰기·중단 날짜 정리 추가 |
| `SegmentWriter` | 운영 경로에서 시작하지 않는 JPEG 저장기 | 이름과 달리 MP4 분할 녹화기로 재사용할 수 없음 |
| 날짜별 DB·한국 자정·오늘 재생·하루 MP4 | 없음 | 신규 |
| 보관 | 이벤트 MP4 기간 만료 | 날짜 조각·다운로드 캐시·임시 파일 용량 관리 추가 |

근거: `app.py:serve`, `pipeline.py:LiveFrameHub`, `recording.py:RecordingService`, `encoding.py:encode_clip`, `http_api.py:create_http_app`, `storage.py:SegmentWriter`.

기존 `encode_clip`은 프레임 간 시간차를 유지하므로 단절 중 마지막 화면이 길게 유지될 수 있다. 새 날짜 녹화는 단절 경계를 나눠 누락 시간을 가짜 영상으로 채우지 않는다. v1 동작은 별도 요구 없이 변경하지 않는다.

## 권장 저장·재생 방식

```text
엣지 JPEG/WSS → 기존 수신·최신 프레임
                   ├ HTTP MJPEG 실시간
                   └ 기존 단기 JPEG 버퍼
                        ├ 기존 이벤트 → 이벤트 MP4
                        └ 날짜 녹화기 → 짧은 H.264/TS 조각 + 날짜별 카탈로그
                                              ├ HLS: 오늘/지난 날짜 재생
                                              └ 비동기 병합: 하루 MP4 다운로드
```

대안은 하루 MP4 직접 쓰기, 10분 MP4 단위 저장, 짧은 조각 저장이다. 하루 파일 직접 쓰기는 장애 복구와 오늘 재생에 불리하다. 10분 MP4는 파일 개수가 적지만 완료 구간만 공개하면 오늘 영상의 갱신이 최대 10분 이상 늦어지고 HLS용 재포장이 추가될 수 있다. **초기 구현안은 10초 조각 + HLS**다. 10분은 내부 디렉터리 묶음에 사용한다. 외부 초안의 '10분 파일'과 다른 제안임을 백엔드에 알린다.

- 날짜 범위는 `ZoneInfo("Asia/Seoul")`의 `[00:00, 다음 날 00:00)`이며 API 타임스탬프는 UTC RFC3339다. 예: 한국 2026-10-14는 `2026-10-13T15:00:00Z`부터 `2026-10-14T15:00:00Z` 미만이다.
- Windows 개발 환경의 기존 가상환경에서 `ZoneInfo("Asia/Seoul")`를 확인하니 tzdata 부재로 실패했다. 구현 시 tzdata를 런타임 의존성에 추가하고 Linux/Windows에서 같은 날짜 계산을 테스트한다.
- `(camera_id, recording_date)` 유일 제약, 최초 발급한 UUID 유지. 카메라 단절 시에도 날짜 레코드를 만든다. 기능 활성화일 이전 날짜의 영상은 만들어낼 수 없다.
- 조각 경계는 날짜 시작을 기준으로 10초 슬롯. 단절·코덱 세션 변경·자정에서는 슬롯 안에서도 나눈다. 다음 날짜 프레임을 전날 조각에 포함하지 않는다.
- 초기 실험 프로파일은 기존 입력 기준인 1920×1080, 약 30fps, H.264/yuv420p, 무음, libx264 veryfast/CRF23, threads=2다. 수신 시간 간격을 보존하며 각 조각은 독립 디코딩 가능하게 시작한다. 날짜 동안 인코딩 규격을 고정하고 입력 크기 변경은 고정 해상도로 정규화한다.
- 실제 운영 해상도·FPS·비트레이트는 부하·화질 측정 후 확정할 협업 항목이다. 현재 프로파일로 상시 운영이 가능하다고 가정하지 않는다.
- 실제 저장은 서비스가 생성한 UUID 경로 `daily/<recording_id>/<block_id>/<segment_id>.ts`. 하루 파일은 `daily-exports/<recording_id>.mp4`, 준비 중에는 임시 파일을 사용한다.
- 완료·디코딩 검증·DB 반영된 조각만 재생 목록에 올린다. 10초 수집 시간에 인코딩 시간이 더해지므로 '10초 이하 재생 지연'을 보장하지 않는다.
- 상시 녹화기는 기존 버퍼의 짧은 시간창을 사용한다. 조각 작업이 잡은 프레임은 인코딩 종료까지 보호하고 이후 해제한다. 버퍼 전량을 하루 동안 고정하지 않는다.
- 미완료 조각 대기는 최대 30초/카메라를 초기 상한으로 설정한다. 처리 지연이 상한을 넘으면 원인을 기록한 누락으로 남기며 수신·MJPEG를 무한 대기시키지 않는다.

HLS는 완료 조각만 나열한다. 오늘은 누적 EVENT 목록, 날짜 종료 후에는 ENDLIST가 있는 VOD 목록이다. 연결이 끊긴 구간 뒤에는 DISCONTINUITY와 새 실제 시각을 표시한다. `EXT-X-PROGRAM-DATE-TIME`은 서버 수신 시각과 조각 첫 샘플의 매핑으로 작성한다. HLS만으로 누락 시간 UI가 자동 구현되는 것은 아니다.

하루 MP4는 **확보된 영상만 시간순으로 합친다.** 누락이 있으면 24시간보다 짧다. 다운로드 시작 전 `coverage`, `gaps`, 실제 길이를 화면에 알리고, 재생 시간↔원래 촬영 시간 매핑을 제공한다. 검은 화면 또는 멈춘 프레임으로 누락을 메우지 않는다. 이 의미는 백엔드·프론트가 확인해야 한다.

## 날짜 카탈로그와 누락 의미

사용자 보충 설명: 기존에 날짜/시간을 해석하기 어려웠던 문제는 **실제 날짜 없이 24시간제 시각만 표시된 데서 생긴 혼동**이었다. 이 설명을 실제 영상 소실이나 기존 API의 날짜 누락 증거로 해석하지 않는다. 현재 미디어 v1 API는 이미 날짜를 포함한 UTC RFC3339를 반환한다. 날짜가 생략된 표시 위치가 프론트·영상 자막·별도 응답 중 어디인지는 이 코드 검토로 확인하지 않았다.

따라서 날짜와 시각을 함께 표시하는 형식 개선은 실제 단절 검출과 구분한다. 새 날짜 카탈로그는 표시 문제를 고치기 위해서가 아니라 **상시 녹화를 카메라·한국 날짜별 한 건으로 묶기 위해** 추가한다. `gaps`는 기존 표시 혼동을 근거로 만들지 않고 실제 수신·저장 상태로 판단한다. 카메라 단절 시 부분 저장/누락 표시 요구는 별도로 유지한다.

새 테이블은 기존 `catalog.sqlite3`에 추가한다. 기존 이벤트 테이블과 버퍼 데이터는 유지한다. 같은 SQLite 연결·잠금 소유자 아래 작업하므로 별도 DB 사이의 불완전한 갱신을 만들지 않는다.

- `daily_recordings`: 날짜, 상태, 활성화 시점, 날짜 시작/끝, 판정 진행 경계, revision, 보관 만료.
- `daily_segments`: UUID, 날짜 ID, 시간 범위, media duration, 크기, 고정 codec profile, 상태와 생성 파일. 시간창 겹침을 허용하지 않는다.
- `frame_pins`: 작업 소유 ID별 JPEG 사용권. 기존 `_prune`의 이벤트 보호 조건에 추가한다.
- `daily_gaps`: 시작/끝, 근거 있는 reason_code. 서버 재시작/저장 오류/카메라 단절을 구분할 근거가 없으면 `UNKNOWN_CAPTURE_LOSS`를 사용한다.
- `daily_exports`: 날짜·revision별 단일 작업, 시도 횟수, 파일 준비 상태와 캐시 만료, 오류.

`status`: `recording`, `finalizing`, `closed`, `failed`, `expired`, `deleted`. 녹화 중 일시 저장 실패가 있어도 재개를 시도하고 오류 정보를 별도로 노출한다. `failed`는 복구를 포기한 처리 실패에 사용한다. 날짜가 정상적으로 끝났으나 영상이 전혀 없는 건은 `closed` + `coverage=none`이다.

`coverage`: 오늘은 null, 날짜 확정 후 `complete`, `partial`, `none`. 오늘도 이미 확인된 누락은 `has_gaps=true`와 `gaps`로 반환하여 '녹화 중 + 부분 저장'을 표시할 수 있게 한다. `status=closed`와 `export_state=ready`는 독립이다.

- `coverage_as_of_at`: 누락/저장이 판정된 시간창의 끝. 녹화 중 인코딩 대기 꼬리와 미래 구간은 누락으로 표시하지 않는다.
- `playable_until_at`: 마지막으로 공개된 조각의 실제 종료 시각. 그 앞에도 `gaps`가 있을 수 있다.
- `recorded_seconds`: 실제 공개된 조각들의 media duration 합, 확인 불가 시 null. 시간창에서 누락을 뺀 산술값으로 파일 길이를 꾸미지 않는다.
- `available_intervals`: 실제 시간의 시작/끝 및 누락을 뺀 재생상의 시작/끝 초. 연속 조각은 하나의 연속 시간 구간으로 합쳐서 반환한다. 백엔드·프론트가 날짜 시간축을 구현할 때 사용한다.
- `gaps`는 서로 겹치지 않는 `[start_at,end_at)` 구간이다. 최초 활성화가 정오라면 자정~정오는 `RECORDING_NOT_STARTED`로 표시한다.
- 정상 프레임 간격에 허용 오차를 두고, 1초 초과 무수신 구간을 기본 추정 누락 기준으로 삼는다. 명시적으로 확인한 연결 단절·프레임 폐기·저장 실패는 1초 미만이어도 기록하며, 실제 단절 시각 기록을 우선한다. 경계 허용 오차는 duration을 늘려 메우는 데 사용하지 않는다.
- 연결이 열린 채 송신이 멈춘 경우도 검사한다. 큐 폐기·저장 실패·인코딩 실패로 실제 공개하지 못한 조각도 누락이다. 카메라가 연결되어 있었다는 이유만으로 complete를 선언하지 않는다.
- 원인 코드는 `CAMERA_DISCONNECTED`, `RECORDING_NOT_STARTED`, `RECORDING_DISABLED`, `SERVICE_INTERRUPTED`, `BUFFER_DROPPED`, `STORAGE_FULL`, `STORAGE_ERROR`, `ENCODING_FAILED`, `CLOCK_ADJUSTED`, `UNKNOWN_CAPTURE_LOSS`를 초기 계약으로 제안한다. 연결 교체 때 이전 소켓의 종료를 새 연결의 단절로 잘못 기록하지 않는다.
- 서비스 중단 기간은 재시작 시 이전 판정 경계와 현재 시각을 비교해 복구한다. 여러 날짜를 건너뛴 중단도 활성 카메라의 날짜별 none/partial 기록으로 보인다.
- 시스템 시각 역행/급변은 새 세션을 열고 오류를 표시한다. 조각 시간을 뒤로 덮어쓰거나 겹치는 영상으로 complete를 만들지 않는다. 세부 허용 범위는 테스트로 고정한다.

## 미디어 서비스 API 제안

기존 `/api/v1` API 계약은 그대로 둔다. 신규 경로는 `/api/v2`이며, 백엔드의 `/api/v2/recordings/daily` 화면 API와 구분한다.

| 메서드 | 경로 | 의미 |
|---|---|---|
| GET / PUT | `/api/v2/cameras/{camera_id}/daily-recording-policy` | 날짜 녹화 활성화·보관일 조회/변경 |
| GET | `/api/v2/cameras/{camera_id}/daily-recordings` | 날짜별 목록 |
| GET | `/api/v2/daily-recordings/{recording_id}` | 상세, 누락, 시각 매핑, 재생·다운로드 상태 |
| GET | `/api/v2/daily-recordings/{recording_id}/hls/index.m3u8` | 오늘/지난 날짜 HLS |
| GET / HEAD | `/api/v2/daily-recordings/{recording_id}/media/{segment_id}.ts` | 완료 조각, Range 지원 |
| POST | `/api/v2/daily-recordings/{recording_id}/exports` | 종료된 날짜의 MP4 준비 |
| GET / HEAD | `/api/v2/daily-recordings/{recording_id}/content` | 준비된 단일 MP4, `download=true` 지원 |
| DELETE | `/api/v2/daily-recordings/{recording_id}` | 종료된 날짜의 조각·MP4 삭제 |

목록 필터: `from_date` 포함, `to_date` 제외, 한국 `YYYY-MM-DD`, `limit` 기본 20/최대100, `cursor` 불투명 문자열. 날짜 내림차순, 같은 카메라·날짜 한 건. 활성화 이후 none 기록도 기본 목록에 포함하고 deleted/expired는 명시 필터로 조회한다. 기존 cursor의 서명 방식을 재사용하되 daily 종류와 필터를 서명에 묶는다.

PUT 정책 JSON: `{"enabled":true,"retention_days":30}`. 날짜 보관일은 이벤트 보관일과 별개다. 변경된 보관일은 다음 날짜부터 적용하며 현재/기존 날짜에는 소급하지 않는다. enabled 변경은 당일의 정지/재개 시각을 기록하고 해당 날짜 ID를 유지한다.

상세 예시: 오늘, 10초 무수신이 이미 확인되었고 마지막 조각 이후는 아직 처리 중인 합성 데이터다.

```json
{
  "recording_id": "550e8400-e29b-41d4-a716-446655440000",
  "camera_id": "camera-main",
  "recording_date": "2026-10-14",
  "timezone": "Asia/Seoul",
  "window_start_at": "2026-10-13T15:00:00.000Z",
  "window_end_at": "2026-10-14T15:00:00.000Z",
  "status": "recording",
  "coverage": null,
  "has_gaps": true,
  "coverage_as_of_at": "2026-10-13T15:01:00.000Z",
  "playable_until_at": "2026-10-13T15:01:00.000Z",
  "recorded_seconds": 50,
  "gaps": [{"start_at":"2026-10-13T15:00:20.000Z","end_at":"2026-10-13T15:00:30.000Z","reason_code":"CAMERA_DISCONNECTED"}],
  "available_intervals": [
    {"start_at":"2026-10-13T15:00:00.000Z","end_at":"2026-10-13T15:00:20.000Z","playback_start_seconds":0,"playback_end_seconds":20},
    {"start_at":"2026-10-13T15:00:30.000Z","end_at":"2026-10-13T15:01:00.000Z","playback_start_seconds":20,"playback_end_seconds":50}
  ],
  "playback_state": "ready",
  "playback_protocol": "hls",
  "playback_path": "/api/v2/daily-recordings/550e8400-e29b-41d4-a716-446655440000/hls/index.m3u8",
  "export_allowed": false,
  "export_state": "not_requested",
  "download_path": null,
  "size_bytes": null,
  "expires_at": null,
  "export_expires_at": null,
  "error": null
}
```

`playback_state`: pending/ready/unavailable. `export_state`: not_requested/processing/ready/failed. `size_bytes`는 다운로드 MP4의 크기이며 오늘 누적된 모든 조각의 크기를 뜻하지 않는다.

MP4 준비는 당일의 전체 다운로드를 허용하지 않는다. 날짜가 끝나도 finalizing 중이면 준비 요청을 거부한다. closed이고 영상이 있으면 최초/진행 중 요청은 202, 준비 완료 상태는 200이다. 같은 recording_id와 revision의 동시 요청은 같은 작업으로 처리한다. 실패 후에는 동일 날짜 ID로 명시 재시도할 수 있다. 재시작 시 진행 중 export는 failed로 바꾸지만 정상 날짜 원본은 보존한다. 일반적인 병합은 같은 규격의 H.264를 재인코딩하지 않고 MP4로 remux한다. 검증 불가/규격 불일치는 오류로 표시하고 하루 전체 재인코딩을 자동으로 시작하지 않는다.

| 상황 | HTTP / 오류 코드 제안 |
|---|---|
| 날짜·JSON·필터 잘못됨 | 400 INVALID_ARGUMENT |
| 미등록 카메라/없는 날짜 건 | 404 CAMERA_NOT_FOUND / DAILY_RECORDING_NOT_FOUND |
| 오늘 전체 준비 요청 | 409 DAY_NOT_CLOSED |
| 날짜 마감 처리 중 | 409 DAY_FINALIZING |
| 영상 없음 | 409 NO_RECORDING_DATA |
| MP4 미준비 | 409 EXPORT_NOT_READY |
| 활성 날짜 또는 진행 중 export 삭제 | 409 RECORDING_ACTIVE / EXPORT_IN_PROGRESS |
| 삭제/보관 만료 | 410 DAILY_RECORDING_DELETED / DAILY_RECORDING_EXPIRED |
| 파일 일부/전체 소실 | 503 RECORDING_STORAGE_ERROR, 카탈로그 coverage 재평가 |
| 전송 범위 잘못됨 | 416 RANGE_NOT_SATISFIABLE |
| 디스크 부족/내보내기 대기 한도 | 503 INSUFFICIENT_STORAGE / EXPORT_CAPACITY_EXCEEDED |

파일은 기존 단일 Range 규칙과 Content-Length/Content-Range/Accept-Ranges를 재사용한다. MP4는 video/mp4, HLS는 application/vnd.apple.mpegurl, TS는 video/mp2t. 다운로드 이름은 `daily_<한국날짜>_<recording_id>.mp4`다. 브라우저에 실제 파일 경로를 제공하지 않는다. JSON의 영상 경로와 m3u8 내 조각 경로는 게이트웨이가 안전하게 변환할 상대 경로다.

날짜 삭제는 204, 반복 삭제도 204, 이후 파일 접근은 410. 실시간 수신·이벤트 클립 및 다른 날짜의 영상은 영향을 받지 않는다.

## 장애 복구·자원·보관

- 조각은 임시 파일에 인코딩, 검증 후 원자적 rename, DB 공개 순으로 반영한다. 파일만 있고 DB 반영 전에 죽은 경우 복구할 수 있도록 준비 manifest에 UUID와 시각을 먼저 영속 기록한다.
- 재시작 시 완료 조각은 유지하고 미완성 조각은 검증 후 복구하거나 손실로 표시한다. 불완전 파일을 정상 재생 목록에 포함하지 않는다.
- 정상 종료에서는 새 작업을 멈추고 짧은 마지막 조각을 완료한다. 종료 시간 초과 시 다음 시작에서 중단 구간을 판정한다.
- 초기 추가 인코딩은 한 번에 1개. 녹화 조각이 export보다 우선하며 export도 별도 동시 상한 1개/대기 상한 8개로 제한한다. 이벤트 인코딩과 겹칠 때 CPU 제한 내에서 동작하는지 부하 단계에서 확인한다.
- 날짜 원본 보관은 날짜 종료 + 정책 retention_days. 다운로드용 중복 MP4 캐시는 초기 24시간이며 `export_expires_at`로 공개한다. 캐시가 없어지면 다시 준비 요청할 수 있다.
- 날짜 기능 기본 rollout은 비활성. 활성화하려면 `MAX_DAILY_STORAGE_BYTES`를 명시하고 운영 프로파일을 결정한다. 기존 환경에서 기능 추가만으로 디스크를 전부 쓰지 않는다.
- 파일 쓰기와 export 전에 전체 여유 공간·날짜 저장 한도를 검사한다. 날짜 한도에는 원본 조각·export MP4·실패/중단 임시 파일·작업 예약을 모두 포함한다. 초기 파일시스템 여유 예약은 5GiB. export는 예상 최종 MP4 크기 + 여유 예약을 확보한 경우에만 시작한다.
- 부족하면 새 export를 거부하고 날짜 녹화에서 저장하지 못한 시간을 STORAGE_FULL로 표시한다. 보관 기간 이전의 기존 영상을 동의 없이 삭제하지 않는다. 기존 ingest/MJPEG와 이벤트 작업이 일시 저장소 부족 때문에 함께 멈추지 않도록 실패를 격리한다.
- 만료 시에는 원본 조각·MP4·임시 작업을 함께 정리한다. 활성 export가 잡은 파일은 정리와 경쟁하지 않도록 lease를 적용한다. 정리 실패는 재시도하고 만료 콘텐츠 접근은 즉시 차단한다.

### 확인된 저장 용량과 배포 조건

2026-10-14에 서버 `100.100.27.99`의 `/var/lib/ajin-camera-media`를 `df -h`로 조회했다: 114G 중 39G 사용, **약70G 여유**. 계정은 kyuncho이며 SSH 키는 인증에만 사용했다. 서버 파일·서비스를 변경하지 않았다.

2026-10-04 합성 1080p30 이벤트 MP4: 60초 45,431,161바이트. 단순 일일 환산은 65,420,871,840바이트 ≈65.4GB/60.9GiB/카메라/일, 30일은 약1.96TB다. 실제 영상의 비트레이트 보장이 아니라 용량 검토용 예시다. 같은 하루의 다운로드 MP4를 추가하면 약 두 배 공간이 필요할 수 있어 현재 여유 공간으로는 이 예시의 원본+export 동시 보관을 충족하지 못한다.

따라서 **카메라 수, 화질 프로파일, 보관일, 실제 디스크/추가 볼륨을 결정하고 24시간 실측을 통과한 뒤 상시 기능을 활성화**한다. 기존 배포의 CPU2/메모리3GiB 제한에서 이벤트 60초 생성에 수집 종료 후 약25초 걸린 결과는 지속 녹화·이벤트 동시 처리의 통과 증거가 아니다.

## 백엔드에 전달할 협업 내용

1. 기존 실시간 MJPEG·이벤트 v1 API는 유지하고 날짜 v2 API를 별도로 연결한다.
2. 오늘 재생은 HLS를 제안한다. index.m3u8와 media/{segment_id}.ts 전체 하위 경로, Range·HEAD·Content-Type을 게이트웨이 허용 규격에 포함한다. 상대 서비스가 게이트웨이를 준비하지 않으면 브라우저 통합은 미완료다.
3. 하루 건의 closed, coverage, playback_state, export_state를 각각 처리한다. 오늘 미래 시간과 인코딩 중 꼬리를 누락으로 표시하지 않는다.
4. 누락은 날짜 시간축에 표시하고, 실제 재생이 누락을 건너뛰더라도 시간 표시가 연속 촬영처럼 보이지 않도록 available_intervals를 사용한다.
   날짜 선택 화면에서는 선택된 한국 날짜를 함께 표시하고 상세 시각은 `YYYY-MM-DD HH:mm:ss`와 시간대를 제공한다. 화면의 시간축 눈금에 HH:mm만 쓰더라도 날짜 맥락은 유지한다. 기존 v1 API는 이미 날짜를 포함하므로 표시 단계에서 이를 버리지 않아야 한다.
5. 종료된 날짜만 POST exports→상태 조회→content?download=true 흐름을 사용한다. 다운로드 MP4에는 확보 영상만 포함됨을 알린다.
6. 사용자 권한·인증과 카메라/피트 매핑은 백엔드/게이트웨이가 담당한다. 내부 API의 기존 loopback·무인증 계약을 유지하며 MEDIA_API_TOKEN을 재도입하지 않는다. 브라우저에 내부 API를 그대로 공개하는 것으로 접근 권한이 구현되지는 않는다.
7. 운영 보관일·화질·디스크 용량·오늘 재생 갱신 목표는 이번 문서의 가정과 측정 결과로 합의한다. 내부 10분 파일 길이는 사용자 확정 요구가 아니다.

## 구현 완료의 판정

자동 테스트는 한국 자정, 무프레임 날짜, 연결/무송신/큐/디스크/인코딩 손실, 조각 공개 이전 장애, 중복 export, 삭제·만료 경쟁, 오늘의 미래 시간 제외, 일정하지 않은 FPS·입력 크기를 검증해야 한다. 실제 FFmpeg로 조각 디코딩과 단일 MP4 생성·seek·길이를 확인한다.

서버에서는 최소24시간 연속 입력·자정 전환·단절·재시작, 이벤트 동시 생성, MJPEG 동시 시청, export 중 녹화 지속과 저장 용량/CPU/메모리/대기시간을 측정한다. 브라우저+인증된 게이트웨이에서 오늘 시각 탐색·누락 표시·지난 날짜 전체 재생·대용량 다운로드를 검증한 뒤 운영 완료로 보고한다.

## 기술 근거

- [FFmpeg HLS / segment / concat muxer·demuxer](https://ffmpeg.org/ffmpeg-formats.html): 짧은 조각과 재포장 기능, keyframe과 stream 호환 조건.
- [RFC8216 HLS](https://www.rfc-editor.org/rfc/rfc8216): EVENT/VOD, PROGRAM-DATE-TIME, DISCONTINUITY, ENDLIST. 프로토콜만으로 업무 누락 표시가 구현되지는 않음.
