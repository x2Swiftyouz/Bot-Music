FROM denoland/deno:bin AS deno

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
ENV PATH="/home/bot/.local/bin:${PATH}" PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["./entrypoint.sh"]
