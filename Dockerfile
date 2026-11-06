FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY rslr ./rslr
RUN pip install --no-cache-dir .
RUN mkdir -p /app/charts
EXPOSE 8000
CMD ["uvicorn", "rslr.web:app", "--host", "0.0.0.0", "--port", "8000"]
