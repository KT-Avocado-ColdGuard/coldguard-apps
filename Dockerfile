FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app
COPY common ./common
COPY collector ./collector
COPY controller ./controller
COPY field_web ./field_web
COPY image ./image
RUN mkdir -p /app/state && chown app:app /app/state
USER app
CMD ["python", "-m", "field_web.field_web"]
