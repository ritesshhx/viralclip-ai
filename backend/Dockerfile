
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgomp1 curl unzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh

ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp && \
    pip install --no-cache-dir bgutil-ytdlp-pot-provider==2.0.2

RUN git clone --depth 1 --branch 2.0.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /root/bgutil-ytdlp-pot-provider && \
    cd /root/bgutil-ytdlp-pot-provider/server && \
    deno install --allow-scripts=npm:canvas --frozen

COPY main.py .

ENV DATA_DIR=/data

VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]
