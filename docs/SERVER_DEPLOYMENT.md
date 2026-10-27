# 서버 A 상시 배포 및 백엔드 연결 안내

2026-10-04 서버 A `100.100.27.99`의 기존 운영 배포를 시작했다. 2026-10-15에는 GitHub main에서 날짜별 상시 녹화 버전을 가져와 같은 운영 컨테이너에 적용했다. 실행 상태와 배포 검증 결과는 VALIDATION.md를 참조한다.

2026-10-26 이벤트 시각 기반 구간 추출 버전 `a0e470f`로 운영 컨테이너를 교체했다. 기존 배포 관리 경로 `/srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml`에서 이미지 참조만 바꿨다. 프로젝트 이름 `scrap-media-dev-daf293e`는 배포 관리용 기존 이름으로 유지했으며 실행 코드 버전은 이미지로 확인한다. 환경 파일·포트·네트워크·영구 저장소·자원 한도는 그대로다.

2026-10-27 자동 보관 MP4 버전 `f4cc08f`로 갱신했다. GitHub에서 가져온 소스로 빌드한 이미지의 전체204개 시험을 통과한 뒤, `DAILY_ARCHIVE_ENABLED=false`인 일시적 Compose override로 먼저 배포·백엔드 연결을 점검했다. 기존 환경 파일·인증 정보·포트·네트워크·자원 한도를 보존했다. 자동 전환을 켠 뒤에는 원래 Compose 파일만 사용한다. 검증된 MP4로 TS를 정리한 후에는 TS 전용 이전 이미지로 되돌리지 않는다. MP4를 지원하는 이미지를 유지한 상태에서 자동 정리를 일시 중지해야 한다.

## 백엔드 담당자에게 전달할 값

| 항목 | 값 |
| --- | --- |
| 호스트에서 실행하는 백엔드의 API 기본 주소 | `http://127.0.0.1:8889/api/v1` |
| 공유 Docker 네트워크에서 실행하는 백엔드의 API 기본 주소 | `http://camera-media-service:8889/api/v1` |
| 날짜 녹화 API 기본 주소 | 호스트 `http://127.0.0.1:8889/api/v2`, 공유 네트워크 `http://camera-media-service:8889/api/v2` |
| 공유 Docker 네트워크 이름 | `ajin-media` |
| 카메라 ID | `camera-main` |
| 실시간 영상 | `GET /cameras/camera-main/live` (HTTP MJPEG) |
| 녹화 설정 | `GET / PUT /cameras/camera-main/recording-policy` |
| 이벤트 저장 | `POST /clips`, JSON `{"camera_id":"camera-main","event_id":"고유 이벤트 ID"}`. 선택 `event_at`을 추가하면 기존 녹화에서 재인코딩 없이 구간 추출 |
| 목록 | `GET /clips?camera_id=camera-main` |
| 상태 | `GET /clips/{clip_id}` |
| 재생·다운로드 | `GET /clips/{clip_id}/content` (H.264 MP4, Range 지원) |
| 삭제 | `DELETE /clips/{clip_id}` |
| API 인증 | 별도 토큰 없음. 호스트 공개 포트는 loopback에 제한 |
| 초기 녹화 설정 | 이벤트 전 30초 / 후 30초, 보관 30일 |

이벤트 요청·응답 JSON, 오류 코드와 중복 처리는 [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md), 날짜별 조회·HLS·MP4·삭제는 [DAILY_RECORDING_API_GUIDE.md](DAILY_RECORDING_API_GUIDE.md)를 따른다. 실시간 영상은 MJPEG다. 브라우저는 백엔드의 영상 중개 경로를 사용한다.

백엔드가 컨테이너 안에서 실행되면 `127.0.0.1`은 그 컨테이너 자체다. 백엔드 담당자는 자신의 Compose 서비스에 외부 네트워크 `ajin-media`를 연결하고 `camera-media-service:8889`로 요청해야 한다. 이미 실행 중인 다른 서비스의 네트워크는 이번 배포에서 변경하지 않았다.

```yaml
services:
  backend:
    # 기존 백엔드 설정에 이 네트워크를 추가
    networks:
      - media
networks:
  media:
    external: true
    name: ajin-media
```

실제 카메라가 연결되지 않으면 실시간 영상은 `503 CAMERA_UNAVAILABLE`을 반환한다. 서비스 장애와 구분한다. 영상 입력이 없는 상태에서 이벤트를 요청하면 해당 구간을 확보할 수 없어 녹화 작업이 실패한다.

## 엣지 연결

- 주소: `wss://scrap-server-01.tail2e96c5.ts.net:9443/api/v1/cameras/camera-main/stream`
- 카메라 ID: `camera-main`
- 엣지 장비는 해당 서버에 접근할 수 있는 Tailscale 네트워크에 연결돼 있어야 한다.
- 서버에서 생성한 실제 엣지 설정은 `/home/kyuncho/ajin-camera-media-service/camera-main.edge.env`에 권한 `0600`으로 보관한다. 이 파일의 토큰은 카메라 엣지 설정에만 사용한다. 문서와 Git에는 넣지 않는다.
- WSS는 Tailscale Serve가 TLS를 종료하고 `127.0.0.1:18443`의 컨테이너 ingest로 중개한다.
- 기존 `8443`과 `18899`의 Tailscale 경로는 유지했다.

## 배포 위치와 운영 설정

| 항목 | 값 |
| --- | --- |
| 컨테이너 | `ajin-camera-media-service` |
| 이미지 | `ajin-camera-media-service:f4cc08f` |
| 배포 파일 | `/srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml` |
| 실행 환경 파일 | `/home/kyuncho/ajin-camera-media-service/runtime.env` (비공개 파일, 이번 배포에서 내용 유지) |
| 서버의 영구 저장 경로 | `/var/lib/ajin-camera-media` |
| 저장 경로 소유자 | UID/GID `10002:10002`, 디렉터리 권한 `0750` |
| 저장 내용 | `buffer/` JPEG, `clips/` 이벤트 MP4, 오늘 TS/보관 원본 MP4/제한된 HLS 임시 캐시, SQLite |
| 자동 재시작 | Docker `unless-stopped`, Docker 서비스 자동 시작 활성화 |
| 자원 제한 | CPU 2개, 메모리 3 GiB |
| 버퍼·작업 한도 | JPEG 버퍼 2 GiB, 미완료 녹화 작업 최대 32개 |
| 날짜 녹화 | 활성, 한국 자정 전환, H.264 1920×1080/15fps, 10초 조각, 새 녹화의 키프레임 간격 1초 |
| 날짜 저장 예산 | TS·MP4·변환 임시 파일·HLS 캐시 합산32 GiB, 최소 디스크 여유5 GiB |
| 날짜 보관·다운로드 | 정책30일, 마감 후 자동 MP4 준비·검증 후 중복 TS 정리. MP4는 날짜 만료까지 유지. 실제30일 분량 용량 보장 아님 |
| 로그 | 파일당 10 MiB, 최대 3개 순환 |
| 상태 검사 | 30초마다 클립 목록 API 호출 |

CPU·메모리 한도는 시작 설정이다. 실제 카메라 수와 입력 부하에 따라 측정 후 조정한다. 컨테이너 재생성 후에도 외부 저장 경로의 데이터는 유지한다. 진행 중 녹화 작업은 재시작 시 `JOB_INTERRUPTED`로 실패 처리된다.

```bash
# 상태 및 최근 로그
sudo docker ps --filter name=ajin-camera-media-service
sudo docker logs --tail 100 ajin-camera-media-service

# 설정 조회
curl http://127.0.0.1:8889/api/v1/cameras/camera-main/recording-policy

# 미디어서버만 재시작
sudo docker compose \
  -f /srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml restart camera-media

# 디스크 사용량
sudo du -sh /var/lib/ajin-camera-media
df -h /var/lib/ajin-camera-media
```

Docker 상태 검사 실패는 상태 표시이며, 그 자체로 컨테이너를 재시작하지 않는다. `unless-stopped`는 프로세스 종료와 Docker 재시작에 적용된다. 서버 전체 재부팅 시험은 기존 서비스에 영향이 있어 이번 배포 검증에 포함하지 않았다.

날짜 녹화에는 원본·임시 파일·HLS 캐시를 합산한 유한 저장 예산과 디스크 여유 검사가 있다. 자동 MP4 준비 중에는 TS와 MP4가 함께 존재할 여유가 필요하다. 부족하면 TS를 유지하고 준비 실패/자동 재시도로 처리한다. 전체 프레임 디코딩 검증은 별도 인코딩 없이 백그라운드 디코더2스레드로 수행하지만 CPU·읽기 부하는 남는다. 이벤트 MP4를 포함한 전체 디렉터리의 파일시스템 하드 쿼터와 오래된 메타데이터 자동 정리는 별도다. 설정된 예산이 부족하면 누락을 표시하므로 실제 하루 저장량으로 필요한 디스크 용량을 확인해야 한다.

## 2026-10-14 날짜 녹화 검증 환경

아래는 운영 교체 전 별도로 만든 검증 환경이다. 구현 커밋 ed98bc4의 코드·API 및129개 서버 시험을 완료했다. 운영 주소8889와 시험 주소18890은 서로 다른 저장소를 사용한다. 현재 운영 날짜 녹화는 위 설정으로 활성화했다.

- 시험 컨테이너: ajin-camera-media-daily-validation, CPU2/메모리3GiB.
- 호스트 API: http://127.0.0.1:18890/api/v2.
- 같은 ajin-media 네트워크의 백엔드에서: http://ajin-camera-media-daily-validation:8889/api/v2.
- 시험 입력: 호스트127.0.0.1:18445 → 컨테이너8443. 합성 전용 토큰을 사용하고 운영 엣지 토큰을 읽거나 교체하지 않았다.
- 시험 저장소: /var/lib/ajin-camera-daily-validation-20261014, 날짜 예산16GiB/최소 여유5GiB.
- 시험 소스는 GitHub feat/daily-recordings에서 직접 fetch하고 Docker 이미지를 빌드했다.
- 운영 주소8889/camera-media-service와 외부 WSS9443은 유지했다. 운영에는 DAILY_RECORDING_ENABLED=true 및32GiB MAX_DAILY_STORAGE_BYTES를 설정했다.

동료용 문서는 [DAILY_RECORDING_API_GUIDE.md](DAILY_RECORDING_API_GUIDE.md) 하나로 전달한다. JSON 목록/정책/상태/MP4 준비/삭제 및 HLS 두 경로·Range 계약을 설명한다. 백엔드 내부 화면 API 경로는 동료가 정한다. 미디어 서비스가 백엔드로 완료 알림을 호출하는 방식은 추가하지 않았다.

운영 용량은 카메라별 실제 GiB/day × 보관일 + MP4 준비 중 중복 저장/HLS 캐시 + JPEG 버퍼/이벤트/DB + 여유 공간으로 계산한다. 상시 운영은32GiB 예산으로 활성화했지만, 합성 도형 영상의 낮은 저장량으로 실현장30일 보관을 보장하지 않는다.

재현 시험 도구는 tools/daily_server_probe.py다. 승인된 격리 환경에서만 실행하며 DAILY_TEST_EDGE_TOKEN 환경 변수로 시험 입력 토큰을 전달한다. --seconds 86400은 실제 시간24시간 시험이다. 서버 재시작 후 입력을 재접속하고, 첫 프레임 전 MJPEG503을 재시도한다. JSON state=running은 완료가 아니며 state=passed와 종료 코드0을 확인한다.
