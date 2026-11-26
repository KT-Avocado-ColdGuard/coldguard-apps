FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates && update-ca-certificates
RUN pip install --no-cache-dir -r requirements.txt
RUN addgroup -S -g 10001 app && adduser -S -D -u 10001 -G app app
COPY common ./common
COPY collector ./collector
COPY controller ./controller
COPY field_web ./field_web
COPY central_worker ./central_worker
COPY control_api ./control_api
COPY control_ui ./control_ui
COPY image ./image
RUN mkdir -p /app/state && chown app:app /app/state
USER app
CMD ["python", "-m", "field_web.field_web"]
