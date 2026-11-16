FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# The Tsinghua mirror is used so builds are reproducible inside the target
# environment; override with --build-arg PIP_INDEX_URL=... if needed.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt ./
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app
COPY demo ./demo
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV AUDIENCE_DB_SCHEMA=audience \
    AUDIENCE_SERVICE_TZ=Asia/Shanghai \
    AUDIENCE_ADMIN_API_KEY=dev-admin-key \
    AUDIENCE_ANALYST_API_KEY=dev-analyst-key

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
