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
COPY field_web/ ./field_web/
COPY image/ ./image/

RUN addgroup -S -g 10001 appuser \
    && adduser -S -D -u 10001 -G appuser appuser

USER appuser

EXPOSE 8080

CMD ["python", "-m", "field_web.field_web"]
