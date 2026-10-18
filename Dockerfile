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
# Parse Python sources without creating __pycache__ directories. Build USER is
# deliberately unprivileged and /app/app must remain non-writable.
# Runtime bootstrap is forbidden; real state is initialized separately.
RUN python -B -c 'import ast, pathlib; [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in pathlib.Path("/app/app").rglob("*.py")]; import app.store, app.sync; print("Bridge modules import OK (read-only syntax check)")'
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
