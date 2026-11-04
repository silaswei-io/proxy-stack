# Test-only Debian stock binary. No systemd/host mounts/ports are used.
FROM proxy-stack-tool:v1.0.0
RUN apt-get update -qq && apt-get install -y --no-install-recommends caddy >/dev/null && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["python"]
