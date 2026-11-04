FROM ghcr.io/xtls/xray-core:26.7.11@sha256:a1644183accdb0b5be967093fe34be756fd5de15fe2ee0206e842ae17350967f AS xray
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
LABEL org.opencontainers.image.source="https://github.com/silaswei/proxy-stack" \
      org.opencontainers.image.version="v1.0.0" \
      org.opencontainers.image.licenses="MIT AND MPL-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
WORKDIR /app
COPY manager/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY LICENSE /usr/share/licenses/proxy-stack/LICENSE
COPY manager/licenses/ /usr/share/licenses/proxy-stack/
COPY manager/src/ /app/
ENTRYPOINT ["python", "-m", "proxy_stack.cli"]
CMD ["status"]
