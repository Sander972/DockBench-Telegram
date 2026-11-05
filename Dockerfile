# Nothing to compile. The prep stage runs natively and only creates the state dir, so the arm64 stage has no RUN and CI needs no QEMU.
FROM --platform=$BUILDPLATFORM alpine:3.20 AS prep
RUN mkdir -p /out/data

FROM python:3.13-alpine
# GHCR links the package to the repo in `source` and shows `description` on the package page.
LABEL org.opencontainers.image.source=https://github.com/Sander972/DockBench-telegram \
      org.opencontainers.image.description="Telegram bot that drives Claude Code inside DockBench dev containers" \
      org.opencontainers.image.licenses=MIT
COPY --from=prep --chown=1000:1000 /out/data /data
COPY bot.py /app/bot.py
COPY tgbot /app/tgbot
# The code stays root-owned (read-only for uid 1000), so do not try to write bytecode caches next to it.
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
CMD ["python", "/app/bot.py"]
