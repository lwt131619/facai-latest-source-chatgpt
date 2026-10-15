FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FACAI_DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi \
    && useradd --uid 10001 --create-home facai
COPY facai ./facai
COPY docs ./docs
RUN chmod -R a+rX /app/facai /app/docs \
    && mkdir -p /app/data && chown -R facai:facai /app/data
USER facai
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health',timeout=3)"
CMD ["python", "-m", "uvicorn", "facai.api:app", "--host", "0.0.0.0", "--port", "8080"]
