FROM python:3.12-slim

WORKDIR /app

# Sistema + FFmpeg
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# DENO
# ============================================================

ARG DENO_VERSION=2.8.0

RUN curl -fL \
    "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /tmp/deno \
    && install -m 0755 /tmp/deno/deno /usr/local/bin/deno \
    && rm -rf /tmp/deno /tmp/deno.zip \
    && deno --version

# ============================================================
# PYTHON
# ============================================================

COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# ============================================================
# APLICAÇÃO
# ============================================================

COPY . .

RUN mkdir -p /app/jobs

ENV PATH="/usr/local/bin:${PATH}"

# ============================================================
# GUNICORN
# ============================================================

CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "7200", "-b", "0.0.0.0:10000", "app:app"]
