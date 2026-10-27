# 카메라 미디어 서비스 아키텍처

## 런타임

하나의 asyncio 프로세스가 엣지 WebSocket listener(기본 8443)와 백엔드 HTTP listener(기본 loopback 8889)를 실행한다. HTTP API와 이벤트 녹화는 항상 시작하며 기존 SegmentWriter는 시작하지 않는다. 백엔드 API에는 토큰 인증이 없고 기본 loopback 바인딩을 사용한다.

```text
JPEG/WSS → IngestService → LiveFrameHub
                            ├ 최신 프레임 → HTTP MJPEG (시청자당 큐 1)
                            └ 녹화 큐 100 → 디스크 JPEG 버퍼 + SQLite 인덱스
백엔드 HTTP → 설정/이벤트 → SQLite 작업 → FFmpeg → MP4
백엔드 HTTP → 목록/상태/Range/삭제 ────────────────┘
```

## 책임 분리

날짜 기능 활성화 시 `DailyCatalog`는 기존 SQLite 연결/잠금을 공유하며 `DailyRecordingService`가 짧은 JPEG 구간을 pin하여 H.264/TS를 만듭니다. 별도 인코더1개를 사용하고 종료 시 진행 작업과 짧은 꼬리를 저장한 뒤 공용 DB를 닫습니다. 정상 종료에 최대50초를 허용하므로 Compose 종료 유예는60초입니다.

`DailyExportService`는 종료 날짜를 백그라운드에서1개씩 stream-copy MP4로 합칩니다. 원본 조각과 MP4를 동시에 둘 공간을 예약합니다. 전체 프레임 디코딩 검사와 파일·디렉터리 fsync 후 원래 시각→MP4 오프셋을 SQLite에 한 트랜잭션으로 저장하고, 기존 원본 사용 작업이 끝나면 중복 TS를 정리합니다. 검증은 디코더2스레드를 사용하며 재인코딩은 하지 않습니다. MP4가 날짜 보관 만료까지 원본이므로 다운로드 요청은 파일 전송만 수행합니다. 준비 실패·재시작은 자동 재시도하고 원본을 유지합니다.

오늘은 HLS EVENT, 종료 날짜는 VOD/ENDLIST로 제공합니다. `ArchivedPlaybackService`는 보관 MP4에서 요청받은 짧은 구간만 TS로 스트림 복사합니다. 캐시는64MiB/미사용60초이며 동시 작업2개, 대기 포함8개입니다. 이벤트 추출은 오늘 TS/보관 MP4를 모두 지원합니다. 삭제/만료는 UUID 전용 파일만 정리하고, 진행 중 클립·HLS 작업은 원본을 pin합니다. 날짜 상태·누락·실제 시간 매핑은 v2 JSON에 포함합니다.

`daily/날짜UUID/10분블록/조각UUID.ts`, `.prepared.json`과 `daily-exports/날짜UUID.mp4`는 내부 저장 구조입니다. 완료 rename 뒤 DB 반영 전 중단은 검증된 prepared 메타데이터로 복구합니다. 날짜별 첫 조각의 출력 프로파일을 유지하여 같은 날짜 MP4 병합의 호환성을 보존합니다.

| 모듈 | 책임 |
| --- | --- |
| `app.py` | WebSocket 인증·라우팅, 두 listener의 시작/종료 |
| `pipeline.py` | 최신 프레임과 bounded queue fan-out, 서버 수신 시각 |
| `recording.py` | 정책·이벤트 중복·작업 상태 영속화, 버퍼 보호·정리, 만료·삭제 |
| `encoding.py` | 시간 간격을 보존하는 JPEG→H.264/MP4 FFmpeg 작업 |
| `http_api.py` | JSON 검증, 목록 커서, MJPEG, Range 파일 전송 |
| `settings.py` | 환경 설정 |

## 저장 경계

`MEDIA_STORAGE_PATH`는 서비스 전용 영속 디렉터리다. `catalog.sqlite3`는 WAL/FULL 모드로 설정과 작업 및 프레임 인덱스를 보관한다. `buffer/`에는 UUID 이름 JPEG, `clips/`에는 UUID 이름 MP4와 생성 중 임시 파일을 둔다. 카메라·이벤트 식별자를 파일 경로에 사용하지 않는다.

버퍼는 최대 300초와 전체 바이트 한도를 함께 적용한다. 활성 작업의 구간은 삭제에서 제외한다. 보호한 데이터가 한도를 채우면 새 프레임 저장 실패가 발생할 수 있으며, 실시간 수신을 차단하지 않고 누락으로 드러낸다. 녹화 큐도 상한이 있어 느린 저장소에서 프레임이 폐기될 수 있다.

작업 생성은 `(camera_id,event_id)` 유일 제약과 프로세스 내 잠금으로 중복을 방지한다. 접수 시 설정과 서버 시간을 고정한다. 구간 종료 후 대기 중인 쓰기를 반영한 다음 프레임 목록을 고정하여 인코딩한다. FFmpeg는 1개씩 실행한다. 인코딩 결과의 DB 저장이 일시 실패하면 재시도한다.

파일 삭제 성공 뒤 상태를 변경하며, 이미 열린 파일 전송은 완료될 수 있다. 자동 만료는 우선 접근을 차단하고 물리 파일 정리에 실패하면 이후 재시도한다. 성공한 만료 정리는 반복하지 않는다.

재시작 시 기존 활성 작업을 실패로 전환하고 잔여 파일을 정리한다. 완료된 작업과 설정, 중복 기록은 유지한다. 동일 디렉터리를 여러 서비스 프로세스가 공유하는 구성은 지원하지 않는다.

## 외부 계약

정확한 요청·응답·상태·오류 형식은 [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md)가 정의한다. 백엔드 API를 호출하는 기능은 없다. 백엔드가 설정, 이벤트, 조회, 삭제 API를 호출하며 완료 상태도 조회한다. WebRTC/WHEP, 브라우저별 사용자 인증, 이벤트 판단은 구현 범위에 없다.
