# 서버 A 상시 배포 및 백엔드 연결 안내

2026-10-04에 서버 A `100.100.27.99`에 GitHub 커밋 `d84e659e016bc765ff3605f2ff26cc95fa164553`를 직접 내려받아 배포했다. 실행 상태와 배포 검증 결과는 VALIDATION.md를 참조한다.

## 백엔드 담당자에게 전달할 값

| 항목 | 값 |
| --- | --- |
| 호스트에서 실행하는 백엔드의 API 기본 주소 | `http://127.0.0.1:8889/api/v1` |
| 공유 Docker 네트워크에서 실행하는 백엔드의 API 기본 주소 | `http://camera-media-service:8889/api/v1` |
| 공유 Docker 네트워크 이름 | `ajin-media` |
| 카메라 ID | `camera-main` |
| 실시간 영상 | `GET /cameras/camera-main/live` (HTTP MJPEG) |
| 녹화 설정 | `GET / PUT /cameras/camera-main/recording-policy` |
| 이벤트 저장 | `POST /clips`, JSON `{"camera_id":"camera-main","event_id":"고유 이벤트 ID"}` |
| 목록 | `GET /clips?camera_id=camera-main` |
| 상태 | `GET /clips/{clip_id}` |
| 재생·다운로드 | `GET /clips/{clip_id}/content` (H.264 MP4, Range 지원) |
| 삭제 | `DELETE /clips/{clip_id}` |
| API 인증 | 별도 토큰 없음. 호스트 공개 포트는 loopback에 제한 |
| 초기 녹화 설정 | 이벤트 전 30초 / 후 30초, 보관 30일 |

상세 요청·응답 JSON, 오류 코드와 중복 이벤트 처리는 [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md)를 따른다. 기존 WHEP/WebRTC 중개는 이 서버의 MJPEG 규격에 맞춰 백엔드 담당자가 수정해야 한다. 브라우저는 백엔드의 영상 중개 경로를 사용한다.

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
| 이미지 | `ajin-camera-media-service:d84e659` |
| 배포 파일 | `/home/kyuncho/ajin-camera-media-service/compose.server.yaml` |
| 실행 환경 파일 | 같은 디렉터리의 `runtime.env` (권한 `0600`) |
| 서버의 영구 저장 경로 | `/var/lib/ajin-camera-media` |
| 저장 경로 소유자 | UID/GID `10002:10002`, 디렉터리 권한 `0750` |
| 저장 내용 | `buffer/` JPEG, `clips/` MP4, `catalog.sqlite3` 및 SQLite 보조 파일 |
| 자동 재시작 | Docker `unless-stopped`, Docker 서비스 자동 시작 활성화 |
| 자원 제한 | CPU 2개, 메모리 3 GiB |
| 버퍼·작업 한도 | JPEG 버퍼 2 GiB, 미완료 녹화 작업 최대 32개 |
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
sudo docker compose -p ajin-camera-media \
  -f /home/kyuncho/ajin-camera-media-service/compose.server.yaml restart

# 디스크 사용량
sudo du -sh /var/lib/ajin-camera-media
df -h /var/lib/ajin-camera-media
```

Docker 상태 검사 실패는 상태 표시이며, 그 자체로 컨테이너를 재시작하지 않는다. `unless-stopped`는 프로세스 종료와 Docker 재시작에 적용된다. 서버 전체 재부팅 시험은 기존 서비스에 영향이 있어 이번 배포 검증에 포함하지 않았다.

전체 저장소 하드 쿼터와 오래된 메타데이터 자동 정리는 아직 없다. 완료 영상은 카메라 설정의 보관 기간으로 만료·삭제하며 디스크 여유 공간을 별도로 확인해야 한다.
