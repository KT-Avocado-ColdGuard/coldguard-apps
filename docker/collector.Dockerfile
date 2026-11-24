FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates \
    && update-ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY collector/ ./collector/

RUN addgroup -S -g 10001 appuser \
    && adduser -S -D -u 10001 -G appuser appuser \
    && mkdir -p /app/state \
    && chown appuser:appuser /app/state

USER appuser

EXPOSE 9201

CMD ["python", "-m", "collector.data_collector"]
