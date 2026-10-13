# BOTLAB Bot Log Auditor - web app image (build context: repo root)
#   docker build -f vultr/Dockerfile.app -t botlab-auditor-app .
# Node is copied in only for the sandboxed cross-check of the original gates.js (trusted vendor code).
# Model-written Python does NOT run here: it goes to the separate runner container (RUNNER_URL).
FROM node:24-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 botlab \
 && useradd --system --uid 10001 --gid botlab --no-create-home --shell /usr/sbin/nologin botlab \
 && mkdir -p /run/botlab && chown botlab:botlab /run/botlab
COPY --from=node /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY app.py agent.py agent_loop.py csv_normalize.py gates.py llm.py payments_paypal.py pdf_export.py \
     runner_client.py sandbox.py LICENSE ./
COPY static ./static
COPY samples ./samples
COPY vendor ./vendor
USER botlab
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
CMD ["python", "app.py"]
