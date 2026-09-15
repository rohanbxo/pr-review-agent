FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

COPY --chown=app:app . .

USER app

EXPOSE 8000

# Migrations first, then the server.
#
# --no-proxy-headers: uvicorn must NOT rewrite request.client from X-Forwarded-For. The app
# resolves the client IP itself (app/netutil.py) and only believes X-Forwarded-For when the TCP
# peer is in TRUSTED_PROXIES (CIDR-aware). Letting uvicorn do it too would mean two trust
# policies, and uvicorn's would replace the peer address our check depends on.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
