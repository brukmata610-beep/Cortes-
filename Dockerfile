FROM python:3.12-slim

WORKDIR /app

# FFmpeg + ferramentas necessárias
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       curl \
       unzip \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Instala Deno para o suporte EJS do yt-dlp/YouTube
RUN curl -fsSL https://deno.land/install.sh | sh \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && chmod +x /usr/local/bin/deno

# Dependências Python
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY . .

RUN mkdir -p jobs

# Inicia Flask/Gunicorn
CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "7200", "-b", "0.0.0.0:10000", "app:app"]
