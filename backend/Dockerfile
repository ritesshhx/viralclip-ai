
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"

COPY main.py .

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]
