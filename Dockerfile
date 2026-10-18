FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
# Runtime UID comes from compose (PUID/PGID). Fix permissions inside IMAGE only.
RUN umask 022 \
    && pip install --no-cache-dir -r requirements.txt \
    && chmod -R a+rX /usr/local/lib/python3.12/site-packages \
    && useradd -r -u 10001 -m -d /nonexistent -s /usr/sbin/nologin bridge
COPY app /app/app
RUN chmod a+rx /app && chmod -R a+rX /app/app
USER 10001:10001
# Smoke-test all modules without creating real persistent state.
# Runtime bootstrap is forbidden; real state is initialized separately.
RUN python -m compileall -q /app/app && python -c 'import app.store, app.sync; print("Bridge modules import OK")'
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
