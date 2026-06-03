FROM python:3.13-alpine AS builder
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.13-alpine
RUN adduser -D -u 1000 appuser
WORKDIR /app
COPY --from=builder /venv /venv
COPY envoy2mqtt4trydan.py .
ENV PATH="/venv/bin:$PATH"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"]
USER appuser
ENTRYPOINT ["python", "-u", "envoy2mqtt4trydan.py"]
