FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
LABEL org.opencontainers.image.source="https://github.com/silaswei/proxy-stack" \
      org.opencontainers.image.version="0.1.2" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY manager/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY manager/src/ /app/
ENTRYPOINT ["python", "-m", "proxy_stack.cli"]
CMD ["status"]
