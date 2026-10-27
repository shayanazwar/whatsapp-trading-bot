FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md requirements-runtime.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements-runtime.lock && pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 appuser && mkdir -p /var/data /var/reports && chown -R appuser:appuser /app /var/data /var/reports
USER appuser
ENV DATABASE_PATH=/var/data/mexc_bot.sqlite3 REPORTS_DIR=/var/reports
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
