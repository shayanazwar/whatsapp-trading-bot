# Convenience image for local all-in-one testing. Render uses Dockerfile.web,
# Dockerfile.market and Dockerfile.compute to isolate resource-heavy roles.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/app
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
