FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p jobs
CMD ["gunicorn","-w","1","--threads","4","--timeout","7200","-b","0.0.0.0:10000","app:app"]