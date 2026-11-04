# 썸네일 요청 회신 — 2026-11-05

전달용 회신 초안이다. 실제 이메일·메신저 발송은 하지 않았다. 2026-11-04 요청서의 미디어 담당 범위를 구현하고 격리된 합성 영상으로 검증했다. 운영 배포와 백엔드/게이트웨이 통합은 아래 상태를 구분한다.

## 지원 범위와 결정

- ready 이벤트·요청 구간 MP4와 완료 날짜의 ready 보관 MP4에 실제 JPEG1장을 제공한다. 진행 날짜·TS만 있는 날짜는 이번 범위에서 제외한다.
- 기존 media-info.thumbnail은 unsupported/null 그대로다. 전용 JSON GET, 명시적 준비 POST, 이미지 GET/HEAD를 추가했다. [확정 미디어 계약](THUMBNAIL_API_CONTRACT_2026-11-04.md)에 경로·필드·상태·인증 경계를 정리했다.
- 대표 프레임은 첫 실제 디코딩 프레임이다. 동일 파일과 생성 시각 매핑을 확인한 신규 파일만 녹화 타임라인 시각을 제공한다. 기존 파일 등 근거가 없으면 이미지 ready를 허용하고 representative_at/time_basis는 null, reason_code=TIMELINE_UNAVAILABLE이다. 이 null 규칙을 새 백엔드 스키마에서 수용해야 한다. 카메라 원본 촬영 시각은 추가로 제공하지 않는다.
- 최대640×360, 종횡비 유지·확대 금지,512KiB. 동시 생성1개/대기 포함4개/실행15초 기본값이다. 신규 ready 파일은 제한된 자동 준비를 시도하고 과거 파일은 선택한 ID의 POST로 준비한다. GET·목록·재시작에서 영상 export나 과거 일괄 변환을 시작하지 않는다.
- SD의 별도 clip-thumbnails/daily-thumbnails와 선택 기능 전용 SQLite에 저장한다. 기존 예산에 이미지·임시 파일을 포함하고 보관 정책과 용량 상한은 변경하지 않았다. 파일 교체 무효화·재시작 재사용·삭제/만료410·생성 중 원본 보호·실패 재시도를 구현했다.

## 실제 격리 환경 응답

합성320×240 MP4 한 건으로 확인했다. 아래 UUID는 시험 파일의 실제 ID이며 운영 영상이 아니다.

준비 전 GET200:

```json
{
  "clip_id": "5a54d09a-6608-4d47-a135-e52e847f6af3",
  "camera_id": "camera-main",
  "status": "unavailable",
  "representative_at": null,
  "time_basis": null,
  "content_path": null,
  "width": null,
  "height": null,
  "size_bytes": null,
  "reason_code": "NOT_PREPARED",
  "generated_at": null
}
```

`POST /api/v1/clips/5a54d09a-6608-4d47-a135-e52e847f6af3/thumbnail`, JSON `{}` →202/pending. 후속 정보 GET200:

```json
{
  "clip_id": "5a54d09a-6608-4d47-a135-e52e847f6af3",
  "camera_id": "camera-main",
  "status": "ready",
  "representative_at": null,
  "time_basis": null,
  "content_path": "/api/v1/clips/5a54d09a-6608-4d47-a135-e52e847f6af3/thumbnail",
  "width": 320,
  "height": 240,
  "size_bytes": 10650,
  "reason_code": "TIMELINE_UNAVAILABLE",
  "generated_at": "2026-11-04T15:05:36.413Z"
}
```

이미지 GET200, Content-Type=image/jpeg, Content-Length=10650(실제10650bytes), JPEG 시작ffd8, Cache-Control=no-store, nosniff. HEAD200/Content-Length10650/본문0bytes. 시험 클립 DELETE204 뒤 JSON과 직접 이미지 경로 모두410/CLIP_DELETED/no-store를 확인했다. 기존 media-info.thumbnail은200/unsupported/null을 유지했다.

## 검증 결과

Windows 전체427passed/2skipped, Ruff check와 format check 통과. skip은 Windows 계정의 심볼릭 링크 생성 권한과 Linux ctime 의미 차이로 기존 시험 두 건이다. 별도 썸네일/설정/날짜 export 관련53개도 통과했다.

실제 JPEG 생성·검은 실제 프레임·세로 종횡비·확대 방지·바이트 상한·프레임 없음·OS 자식 프로세스 타임아웃 종료·동일 요청 합치기·대기 상한·실패 재시도·삭제/만료·재시작 재사용·동일 크기/mtime 원본 교체·생성 중 교체·stale GET 경합·삭제 중 재준비·예약 해제·선택 DB 손상 격리를 검증했다. 날짜 TS-only 요청은 새 export를 만들지 않고, 날짜 MP4 자동 완료 후 썸네일 생성 및 삭제 시 실제 JPEG 정리를 확인했다. 실제 운영 원본을 삭제하는 검증은 하지 않았다.

## 배포와 후속 담당

운영 서버 SSH 접속을 재확인했다. 기존 미디어 서비스 healthy, SD 경로 유지와 카메라 정상 수신을 확인했다. 새 버전은 서버의 격리 환경에서 검증한 후 미디어 서비스만 교체할 예정이며, 실제 배포 결과와 commit을 검증 뒤 추가한다.

GitHub connector의 쓰기는403이지만 기존 SSH 키의 GitHub 인증을 확인했다. 해당 인증으로 구현 브랜치 업로드와 PR을 진행한다.

백엔드 담당자는 전용 스키마/준비 POST와 조회, ID·camera_id·고정 내부 경로 검증, 로그인·대상 권한을 적용한 이미지 GET/HEAD 게이트웨이를 구현하면 된다. 브라우저 공개 경로는 백엔드 구현 대상이다. 요청서에 적힌 ‘기존 v1 서버 간 토큰’과 달리 현재 미디어 v1/v2 HTTP API는 토큰을 요구하지 않고 기존 내부망/loopback 경계로 운영한다. MEDIA_API_TOKEN을 새로 추가하지 않았다.

백엔드/프론트 저장소는 수정하지 않았고 그 통합 테스트는 실행하지 않았다. 기존 LC-4 HLS 최초 재생/간헐적 라이브 지연 문제는 이번 썸네일 기능과 별도이며 해결됐다고 보고하지 않는다. 현장 실측·라이다 배치는 범위에서 제외했다.
