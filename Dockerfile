FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin cameramedia
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/camera-media \
    && chown cameramedia:cameramedia /var/lib/camera-media

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv
RUN uv sync --frozen --no-dev

USER cameramedia

CMD ["/app/.venv/bin/python", "-m", "camera_media_service.main"]
