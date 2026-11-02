# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Audiobind" \
      org.opencontainers.image.description="Merge, convert and correctly tag audiobook libraries" \
      org.opencontainers.image.url="https://github.com/stinger303/audiobind" \
      org.opencontainers.image.source="https://github.com/stinger303/audiobind" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LIBRARY_ROOT=/library \
    OUTPUT_ROOT=/output \
    STATE_DIR=/config \
    WEB_PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# fixed uid/gid; the entrypoint remaps them to PUID/PGID at start
RUN groupadd -g 99 audiobind && useradd -m -u 99 -g 99 -s /bin/bash audiobind

WORKDIR /app
COPY audiobind/ /app/audiobind/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && printf '#!/bin/sh\nexec python3 -m audiobind "$@"\n' > /usr/local/bin/audiobind \
 && chmod +x /usr/local/bin/audiobind

VOLUME ["/library", "/output", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
  CMD audiobind health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["daemon"]
