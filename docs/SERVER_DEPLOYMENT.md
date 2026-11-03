# 서버 A 상시 배포 및 백엔드 연결 안내

2026-11-03 현재 운영 이미지는 `ajin-camera-media-service:8d5d584`이며 영상 저장소는 실제 SD `/srv/ajin-camera-storage/sd/camera-media`다. PR #3·#4·#5의 구현·성능 후속 수정을 포함한다. 현재 재시작은 아래 systemd 단위를 사용한다. 원래 Compose 파일과 SD override를 함께 적용해야 한다. 이전 날짜의 배포 설명은 당시 이력이며 현재 설정은 이 절과 아래 표를 따른다.

2026-10-04 서버 A `100.100.27.99`의 기존 운영 배포를 시작했다. 2026-10-15에는 GitHub main에서 날짜별 상시 녹화 버전을 가져와 같은 운영 컨테이너에 적용했다. 실행 상태와 배포 검증 결과는 VALIDATION.md를 참조한다.

2026-10-26 이벤트 시각 기반 구간 추출 버전 `a0e470f`로 운영 컨테이너를 교체했다. 기존 배포 관리 경로 `/srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml`에서 이미지 참조만 바꿨다. 프로젝트 이름 `scrap-media-dev-daf293e`는 배포 관리용 기존 이름으로 유지했으며 실행 코드 버전은 이미지로 확인한다. 환경 파일·포트·네트워크·영구 저장소·자원 한도는 그대로다.

2026-10-27 자동 보관 MP4 버전 `f4cc08f`로 갱신했다. GitHub에서 가져온 소스로 빌드한 이미지의 전체204개 시험을 통과한 뒤, `DAILY_ARCHIVE_ENABLED=false`인 일시적 Compose override로 먼저 배포·백엔드 연결을 점검했다. 기존 환경 파일·인증 정보·포트·네트워크·자원 한도를 보존했다. 당시에는 자동 전환을 켠 뒤 원래 Compose 파일만 사용했다. 현재 SD 전환 후에는 반드시 SD override도 함께 사용한다. 검증된 MP4로 TS를 정리한 후에는 TS 전용 이전 이미지로 되돌리지 않는다. MP4를 지원하는 이미지를 유지한 상태에서 자동 정리를 일시 중지해야 한다.

## 백엔드 담당자에게 전달할 값

2026-11-03 main의 `ajin-camera-media-service:8d5d584`를 운영 배포했다. 실제 SD 신규 녹화·기존 ID·재시작·메타데이터 영속 조회, 실제 백엔드 컨테이너의 미디어 API와 전체 MP4/구간 다운로드를 확인했다. 현재 설정·성능 관찰·남은 제한은 [VALIDATION.md](VALIDATION.md)의 최신 절을 따른다. 기존 이벤트/날짜 보관30일은 현재 적용값이며 새 보관 기간 합의가 아니다.

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

이벤트 요청·응답 JSON, 오류 코드와 중복 처리는 [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md), 날짜별 조회·HLS·MP4·삭제는 [DAILY_RECORDING_API_GUIDE.md](DAILY_RECORDING_API_GUIDE.md)를 따른다. 실시간 영상은 MJPEG다. 브라우저에는 백엔드/프록시가 인증된 미디어 접근 경로를 제공한다. 백엔드가 영상 바이트를 직접 중계해야 한다는 뜻은 아니다.

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
| 이미지 | `ajin-camera-media-service:8d5d584` |
| 배포 파일 | `/srv/scrap-monitoring/development/main-refresh-20261015/compose.media.yaml` + `/home/kyuncho/ajin-camera-media-service/deploy-20261103-sd/compose.sd.runtime.yaml` |
| 실행 환경 파일 | `/home/kyuncho/ajin-camera-media-service/runtime.env` (비공개 파일, 이번 배포에서 내용 유지) |
| 서버의 영구 저장 경로 | SD `/srv/ajin-camera-storage/sd/camera-media` → 컨테이너 `/var/lib/camera-media`. 이전 SSD `/var/lib/ajin-camera-media`는 보존본 |
| 저장 경로 소유자 | UID/GID `10002:10002`, 디렉터리 권한 `0750` |
| 저장 내용 | `buffer/` JPEG, `clips/` 이벤트 MP4, 오늘 TS/보관 원본 MP4/제한된 HLS 임시 캐시, SQLite |
| 자동 재시작 | SD 사전 확인/미디어 systemd 단위 enabled·active, Docker `unless-stopped`. healthy까지 시작 검증 |
| 자원 제한 | CPU 2개, 메모리 3 GiB |
| 버퍼·작업 한도 | JPEG 버퍼 2 GiB, 미완료 녹화 작업 최대 32개 |
| 날짜 녹화 | 활성, 한국 자정 전환, H.264 1920×1080/15fps, 10초 조각, 새 녹화의 키프레임 간격 1초 |
| 저장 예산 | 전체48GiB·날짜42GiB·MP4 작업 공간10GiB·최소 여유5GiB. JPEG·클립·임시 파일·DB도 전체 합계에 포함 |
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
sudo systemctl restart ajin-camera-media.service

# 디스크 사용량
sudo du -sh /srv/ajin-camera-storage/sd/camera-media
df -h /srv/ajin-camera-storage/sd/camera-media
```


`ajin-camera-media-storage.service`의 preflight는 실제 UUID/장치/권한과 전용 공유 부모·SD 하위 마운트를 확인하며 Docker보다 먼저 실행된다. `ajin-camera-media.service`의 ExecStartPre에서도 재확인한다. SD 단위 실패가 다른 애플리케이션의 Docker 전체를 중지하도록 구성하지 않았다. 시작은 두 Compose 파일의 `up -d --no-deps --wait --wait-timeout 120 camera-media`이며 서비스 단위 active는 healthy 확인 이후다.

preflight는 `/usr/local/libexec/ajin-camera-media-storage-preflight`, 고정 검증 도구는 `/home/kyuncho/ajin-camera-media-service/pr-media-sd-20261103-1923d83/tools/media_storage_migrate.py`를 사용한다. 이 체크아웃을 운영 의존성 확인 없이 삭제하지 않는다. 설정 파일·환경 파일은 비공개이며 토큰을 문서/Git에 넣지 않는다. 서버 전체 재부팅은 미실시다. SSD 보존본의 오래된 DB를 현재 SD 위에 덮어쓰지 않는다.

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

후속 배포 증거와 복구 정보는 서버 `/home/kyuncho/ajin-camera-media-service/deploy-20261031-archivefix/`에 보관한다. 이전1bcce51로 복구할 때는 그 버전의 임시 아카이브 중지 override를 함께 적용해야 같은 공간 계산 문제가 재발하지 않는다. 백업 SQLite를 새 녹화 데이터 위에 덮어쓰지 않는다.
