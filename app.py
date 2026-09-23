import os
import json
import uuid
import threading
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from openai import OpenAI


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.getenv("WORK_DIR", BASE_DIR / "work"))

JOBS_DIR = WORK_DIR / "jobs"
OUTPUT_DIR = WORK_DIR / "outputs"
UPLOAD_DIR = WORK_DIR / "uploads"

for directory in (JOBS_DIR, OUTPUT_DIR, UPLOAD_DIR):
    directory.mkdir(parents=True, exist_ok=True)


PORT = int(os.getenv("PORT", "10000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

TRANSCRIPTION_MODEL = os.getenv(
    "TRANSCRIPTION_MODEL",
    "gpt-4o-mini-transcribe"
)

ANALYSIS_MODEL = os.getenv(
    "ANALYSIS_MODEL",
    "gpt-5.6"
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024


# ============================================================
# OPENAI
# ============================================================

client = None

if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================
# JOBS
# ============================================================

JOBS = {}
JOBS_LOCK = threading.Lock()


def create_job():
    job_id = uuid.uuid4().hex

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Aguardando processamento...",
            "clips": [],
            "error": None,
        }

    return job_id


def update_job(job_id, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def get_job(job_id):
    with JOBS_LOCK:
        if job_id not in JOBS:
            return None

        return dict(JOBS[job_id])


# ============================================================
# COMANDOS
# ============================================================

def run_command(command, timeout=3600):
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    if process.returncode != 0:
        error = process.stderr.strip()

        if not error:
            error = "O comando terminou com erro."

        raise RuntimeError(error[-5000:])

    return process.stdout


# ============================================================
# YT-DLP
# ============================================================

def download_video(url, output_path):

    update_job(
        CURRENT_JOB,
        progress=5,
        message="Baixando vídeo..."
    )

    command = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/b",
        "-o",
        str(output_path),
        url,
    ]

    run_command(command, timeout=7200)

    if not output_path.exists():

        candidates = list(
            output_path.parent.glob("*")
        )

        videos = [
            file for file in candidates
            if file.suffix.lower() in {
                ".mp4",
                ".mkv",
                ".webm",
                ".mov"
            }
        ]

        if not videos:
            raise RuntimeError(
                "O yt-dlp terminou, mas nenhum vídeo foi encontrado."
            )

        videos.sort(
            key=lambda file: file.stat().st_mtime,
            reverse=True
        )

        videos[0].rename(output_path)


# ============================================================
# FFMPEG
# ============================================================

def get_duration(video):

    result = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])

    return float(result.strip())


def extract_audio(video, audio):

    update_job(
        CURRENT_JOB,
        progress=20,
        message="Extraindo áudio..."
    )

    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "mp3",
        "-b:a",
        "64k",
        str(audio),
    ], timeout=7200)


# ============================================================
# TRANSCRIÇÃO
# ============================================================

def transcribe_audio(audio):

    if client is None:
        raise RuntimeError(
            "OPENAI_API_KEY não configurada."
        )

    update_job(
        CURRENT_JOB,
        progress=35,
        message="Transcrevendo áudio com IA..."
    )

    with open(audio, "rb") as file:

        response = client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=file,
            response_format="verbose_json",
        )

    segments = []

    raw_segments = getattr(
        response,
        "segments",
        None
    )

    if raw_segments:

        for segment in raw_segments:

            if isinstance(segment, dict):

                start = float(segment.get("start", 0))
                end = float(segment.get("end", start))
                text = str(segment.get("text", "")).strip()

            else:

                start = float(getattr(segment, "start", 0))
                end = float(
                    getattr(segment, "end", start)
                )
                text = str(
                    getattr(segment, "text", "")
                ).strip()

            if text:

                segments.append({
                    "start": start,
                    "end": end,
                    "text": text,
                })

    if not segments:

        text = getattr(
            response,
            "text",
            ""
        )

        if text:

            segments.append({
                "start": 0,
                "end": 999999,
                "text": text.strip(),
            })

    return segments


# ============================================================
# ESCOLHA DOS CORTES
# ============================================================

def analyze_best_clips(segments, duration, clip_count):

    if not segments:
        return fallback_clips(
            duration,
            clip_count
        )

    transcript = "\n".join(
        f"[{segment['start']:.2f}-{segment['end']:.2f}] "
        f"{segment['text']}"
        for segment in segments
    )

    prompt = f"""
Você é um editor profissional de vídeos curtos.

Analise a transcrição abaixo e escolha os melhores momentos
para vídeos curtos.

Duração total do vídeo:
{duration:.2f} segundos

Quantidade desejada:
{clip_count}

Escolha momentos que tenham potencial de prender atenção,
como histórias interessantes, opiniões fortes, explicações,
frases marcantes, humor, surpresa ou informações úteis.

REGRAS:

- Não invente timestamps.
- Use somente timestamps existentes na transcrição.
- Cada corte deve ter entre 30 e 90 segundos quando possível.
- Evite cortes muito parecidos.
- Evite começar no meio de uma frase.
- Evite terminar no meio de uma frase.

Retorne SOMENTE JSON válido neste formato:

{{
  "clips": [
    {{
      "start": 120.5,
      "end": 178.2,
      "title": "Título curto"
    }}
  ]
}}

TRANSCRIÇÃO:

{transcript}
"""

    response = client.responses.create(
        model=ANALYSIS_MODEL,
        input=prompt,
    )

    text = response.output_text.strip()

    text = text.replace(
        "```json",
        ""
    ).replace(
        "```",
        ""
    ).strip()

    try:
        data = json.loads(text)

    except json.JSONDecodeError:

        return fallback_clips(
            duration,
            clip_count
        )

    clips = []

    for item in data.get("clips", []):

        try:

            start = float(item["start"])
            end = float(item["end"])

            start = max(
                0,
                min(start, duration)
            )

            end = max(
                start + 1,
                min(end, duration)
            )

            if end > start:

                clips.append({
                    "start": start,
                    "end": end,
                    "title": str(
                        item.get(
                            "title",
                            f"Corte {len(clips) + 1}"
                        )
                    ),
                })

        except Exception:
            continue

    if not clips:

        return fallback_clips(
            duration,
            clip_count
        )

    return clips[:clip_count]


def fallback_clips(duration, count):

    clips = []

    if duration <= 30:
        return [{
            "start": 0,
            "end": duration,
            "title": "Corte"
        }]

    clip_duration = min(
        60,
        duration
    )

    available = max(
        0,
        duration - clip_duration
    )

    if count <= 1:
        positions = [available / 2]

    else:

        positions = [
            available * i / (count - 1)
            for i in range(count)
        ]

    for index, start in enumerate(
        positions,
        start=1
    ):

        clips.append({
            "start": round(start, 2),
            "end": round(
                min(
                    start + clip_duration,
                    duration
                ),
                2
            ),
            "title": f"Corte {index}",
        })

    return clips


# ============================================================
# CRIAÇÃO DO VÍDEO
# ============================================================

def create_clip(
    source,
    destination,
    start,
    end
):

    duration = max(
        1,
        end - start
    )

    video_filter = (
        "scale=720:1280:"
        "force_original_aspect_ratio=increase,"
        "crop=720:1280"
    )

    run_command([
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(destination),
    ], timeout=7200)


# ============================================================
# SRT
# ============================================================

def seconds_to_srt(seconds):

    seconds = max(
        0,
        float(seconds)
    )

    milliseconds = int(
        round(
            (seconds - int(seconds)) * 1000
        )
    )

    total = int(seconds)

    hours = total // 3600

    total %= 3600

    minutes = total // 60

    seconds = total % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{seconds:02d},"
        f"{milliseconds:03d}"
    )


def create_srt(
    segments,
    clip_start,
    clip_end,
    output
):

    lines = []

    counter = 1

    for segment in segments:

        start = max(
            segment["start"],
            clip_start
        )

        end = min(
            segment["end"],
            clip_end
        )

        if end <= start:
            continue

        local_start = start - clip_start
        local_end = end - clip_start

        lines.append(
            f"{counter}\n"
            f"{seconds_to_srt(local_start)} --> "
            f"{seconds_to_srt(local_end)}\n"
            f"{segment['text']}\n"
        )

        counter += 1

    output.write_text(
        "\n".join(lines),
        encoding="utf-8"
    )


# ============================================================
# PROCESSAMENTO
# ============================================================

CURRENT_JOB = None


def process_job(
    job_id,
    url,
    clip_count
):

    global CURRENT_JOB

    CURRENT_JOB = job_id

    job_dir = JOBS_DIR / job_id
    output_dir = OUTPUT_DIR / job_id

    job_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    video = job_dir / "source.mp4"
    audio = job_dir / "audio.mp3"

    try:

        update_job(
            job_id,
            status="processing",
            progress=1,
            message="Iniciando..."
        )

        download_video(
            url,
            video
        )

        update_job(
            job_id,
            progress=15,
            message="Verificando vídeo..."
        )

        duration = get_duration(video)

        extract_audio(
            video,
            audio
        )

        segments = transcribe_audio(
            audio
        )

        update_job(
            job_id,
            progress=50,
            message="Encontrando os melhores momentos..."
        )

        clips = analyze_best_clips(
            segments,
            duration,
            clip_count
        )

        result = []

        total = len(clips)

        for index, clip in enumerate(
            clips,
            start=1
        ):

            output_video = (
                output_dir /
                f"corte_{index:02d}.mp4"
            )

            output_srt = (
                output_dir /
                f"corte_{index:02d}.srt"
            )

            create_clip(
                video,
                output_video,
                clip["start"],
                clip["end"]
            )

            create_srt(
                segments,
                clip["start"],
                clip["end"],
                output_srt
            )

            progress = (
                50 +
                int(
                    (index / total) * 45
                )
            )

            update_job(
                job_id,
                progress=progress,
                message=(
                    f"Gerando corte "
                    f"{index}/{total}..."
                )
            )

            result.append({
                "index": index,
                "title": clip["title"],
                "start": clip["start"],
                "end": clip["end"],
                "video": (
                    f"/media/{job_id}/"
                    f"corte_{index:02d}.mp4"
                ),
                "subtitle": (
                    f"/media/{job_id}/"
                    f"corte_{index:02d}.srt"
                ),
            })

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="Cortes prontos!",
            clips=result,
        )

    except Exception as error:

        update_job(
            job_id,
            status="error",
            progress=100,
            message="Erro durante o processamento.",
            error=str(error),
        )


# ============================================================
# ROTAS
# ============================================================

@app.get("/")
def home():

    return render_template(
        "index.html"
    )


@app.get("/health")
def health():

    return jsonify({
        "status": "ok"
    })


@app.post("/api/start")
def start():

    data = request.get_json(
        silent=True
    ) or {}

    url = str(
        data.get(
            "url",
            ""
        )
    ).strip()

    if not url:

        return jsonify({
            "error": "Informe o link do vídeo."
        }), 400

    if not url.startswith(
        ("http://", "https://")
    ):

        return jsonify({
            "error": "Informe uma URL válida."
        }), 400

    try:

        clip_count = int(
            data.get(
                "clip_count",
                5
            )
        )

    except Exception:

        clip_count = 5

    clip_count = max(
        1,
        min(
            clip_count,
            20
        )
    )

    job_id = create_job()

    thread = threading.Thread(
        target=process_job,
        args=(
            job_id,
            url,
            clip_count,
        ),
        daemon=True,
    )

    thread.start()

    return jsonify({
        "success": True,
        "job_id": job_id,
    })


@app.get("/api/status/<job_id>")
def status(job_id):

    job = get_job(job_id)

    if job is None:

        return jsonify({
            "error": "Processamento não encontrado."
        }), 404

    return jsonify(job)


@app.get("/media/<job_id>/<path:filename>")
def media(
    job_id,
    filename
):

    directory = OUTPUT_DIR / job_id

    return send_from_directory(
        directory,
        filename,
        as_attachment=False
    )


# ============================================================
# ERROS
# ============================================================

@app.errorhandler(413)
def too_large(error):

    return jsonify({
        "error": "O arquivo enviado é muito grande."
    }), 413


@app.errorhandler(500)
def server_error(error):

    return jsonify({
        "error": "Erro interno do servidor."
    }), 500


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
