# 검증 범위

Python 3.13과 잠금 파일의 의존성으로 검사한다.

```shell
uv sync --frozen --extra dev
uv run pytest -q -p no:cacheprovider
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

검사 대상은 설정, 인증, WebSocket 경로, JPEG 경계·크기 검증, 세션 교체 및 최신 프레임 관리다.
Windows 독립 환경 테스트와 wheel/sdist 빌드를 확인했다.
CI 결과는 해당 커밋의 GitHub Actions 실행에서 확인한다.

실카메라·운영 TLS·장시간 부하 인수는 별도다. 브라우저 전달, 녹화, 클립 조회는 미구현이며
테스트 통과가 해당 기능의 완성을 뜻하지 않는다.
[아키텍처](ARCHITECTURE.md)와 [남은 작업](REMAINING_WORK.md)에 완료 조건을 구분한다.
