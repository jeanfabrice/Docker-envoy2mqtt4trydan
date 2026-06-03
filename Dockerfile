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
USER appuser
ENTRYPOINT ["python", "envoy2mqtt4trydan.py"]
