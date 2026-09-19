FROM python:3.13-slim AS builder

WORKDIR /build
COPY setup.py README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim

ARG GID=1012
ARG UID=1012
LABEL org.opencontainers.image.source="https://github.com/knight9/vuegraf" \
      org.opencontainers.image.description="VueGraf with per-channel electrical telemetry and history"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels vuegraf \
    && rm -rf /wheels \
    && groupadd --gid "$GID" vuegraf \
    && useradd --uid "$UID" --gid "$GID" --home-dir /opt/vuegraf --create-home vuegraf \
    && mkdir -p /opt/vuegraf/conf \
    && chown vuegraf:vuegraf /opt/vuegraf/conf

WORKDIR /opt/vuegraf

USER ${UID}:${GID}

ENTRYPOINT ["vuegraf" ]
CMD ["/opt/vuegraf/conf/vuegraf.json"]
