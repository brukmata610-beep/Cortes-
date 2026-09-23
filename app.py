import os
import uuid
import json
import threading
import subprocess
import re
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory
from openai import OpenAI


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE = Path(__file__).resolve().parent
JOBS = BASE / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


# ============================================================
# UTILIDADES
# ============================================================

def save(job, state):
    job.mkdir(parents=True, exist_ok=True)

    temp = job / "state.tmp"
    target = job / "state.json"

    temp.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8"
    )

    temp.replace(target)


def cmd(command):
    """
    Executa um comando externo e mostra o resultado completo
    nos logs do Render.
    """

    command = [str(x) for x in command]

    print(
        "\n==================================================",
        flush=True
    )
    print(
        "EXECUTANDO:",
        " ".join(command),
        flush=True
    )
    print(
        "==================================================",
        flush=True
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    output = result.stdout or ""

    print(output, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            "Comando falhou (exit %s):\n%s"
            % (
                result.returncode,
                output[-12000:]
            )
        )

    return output


def clean_json_text(text):
    """
    Remove possíveis blocos ```json ... ``` retornados pela IA.
    """

    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

    return text.strip()


def clamp_number(value, minimum, maximum, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(value, maximum))


# ============================================================
# TRANSCRIÇÃO
# ============================================================

def transcribe(client, audio):
    print("Iniciando transcrição...", flush=True)

    with open(audio, "rb") as f:
        response = client.audio.transcriptions.create(
            model=os.getenv(
                "TRANSCRIBE_MODEL",
                "gpt-4o-mini-transcribe"
            ),
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )

    segments = []

    for segment in getattr(response, "segments", []) or []:
        start = float(getattr(segment, "start", 0) or 0)
        end = float(getattr(segment, "end", 0) or 0)
        text = str(getattr(segment, "text", "") or "").strip()

        if not text:
            continue

        if end <= start:
            continue

        segments.append({
            "start": start,
            "end": end,
            "text": text
        })

    if not segments:
        raise RuntimeError(
            "A transcrição não retornou segmentos com timestamps."
        )

    print(
        f"Transcrição concluída: {len(segments)} segmentos.",
        flush=True
    )

    return segments


# ============================================================
# ESCOLHA DOS MELHORES MOMENTOS
# ============================================================

def choose_moments(client, segments, count, duration):

    transcript = "\n".join(
        f"[{x['start']:.1f}-{x['end']:.1f}] {x['text']}"
        for x in segments
    )

    # Evita mandar uma quantidade gigantesca de texto para a API.
    transcript = transcript[:180000]

    prompt = f"""
Você é um editor profissional de vídeos curtos para
YouTube Shorts, Instagram Reels e TikTok.

Analise a transcrição abaixo e escolha até {count} dos
melhores momentos.

REGRAS:

- Cada corte deve ter entre 30 e {duration} segundos.
- Não invente nenhuma fala.
- Use somente trechos existentes na transcrição.
- O início e o final devem ser naturais.
- Priorize:
  - gancho forte;
  - opinião interessante;
  - história;
  - surpresa;
  - pergunta e resposta;
  - momento engraçado;
  - informação útil;
  - discussão;
  - frase que desperte curiosidade.
- Evite trechos sem contexto.
- Evite silêncio.
- Evite escolher várias partes praticamente iguais.
- Os timestamps precisam estar dentro da transcrição.

RETORNE SOMENTE JSON VÁLIDO.

Formato obrigatório:

{{
  "clips": [
    {{
      "start": 100.0,
      "end": 155.0,
      "title": "Título curto do corte",
      "hook": "Gancho curto para apresentar o corte"
    }}
  ]
}}

TRANSCRIÇÃO:

{transcript}
"""

    model = os.getenv(
        "ANALYSIS_MODEL",
        "gpt-5.6"
    )

    print(
        f"Analisando transcrição com modelo: {model}",
        flush=True
    )

    response = client.responses.create(
        model=model,
        input=prompt
    )

    text = clean_json_text(
        getattr(response, "output_text", "")
    )

    if not text:
        raise RuntimeError(
            "A IA não retornou nenhum resultado."
        )

    # Procura o primeiro objeto JSON.
    match = re.search(
        r"\{.*\}",
        text,
        flags=re.DOTALL
    )

    if not match:
        raise RuntimeError(
            "A IA não retornou JSON válido.\n"
            + text[:4000]
        )

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "Não foi possível interpretar o JSON da IA: "
            + str(e)
        )

    clips = data.get("clips", [])

    if not isinstance(clips, list):
        raise RuntimeError(
            "A IA retornou um formato de clips inválido."
        )

    valid_clips = []

    video_duration = 0

    if segments:
        video_duration = max(
            float(s["end"])
            for s in segments
        )

    for clip in clips:

        try:
            start = float(clip["start"])
            end = float(clip["end"])
        except (KeyError, TypeError, ValueError):
            continue

        if start < 0:
            start = 0

        if end <= start:
            continue

        if video_duration > 0:
            start = min(start, video_duration)
            end = min(end, video_duration)

        length = end - start

        if length < 30:
            continue

        if length > duration:
            end = start + duration

        if end <= start:
            continue

        valid_clips.append({
            "start": start,
            "end": end,
            "title": str(
                clip.get("title") or "Corte"
            )[:150],
            "hook": str(
                clip.get("hook") or ""
            )[:300]
        })

        if len(valid_clips) >= count:
            break

    if not valid_clips:
        raise RuntimeError(
            "A IA não encontrou cortes válidos "
            "dentro dos limites solicitados."
        )

    print(
        f"Cortes selecionados: {len(valid_clips)}",
        flush=True
    )

    return valid_clips


# ============================================================
# SRT / LEGENDAS
# ============================================================

def make_srt(segments, start, end, path):

    rows = []
    number = 1

    def timestamp(seconds):
        seconds = max(0, float(seconds))

        total_ms = int(round(seconds * 1000))

        hours = total_ms // 3_600_000
        total_ms %= 3_600_000

        minutes = total_ms // 60_000
        total_ms %= 60_000

        secs = total_ms // 1000
        milliseconds = total_ms % 1000

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{secs:02d},"
            f"{milliseconds:03d}"
        )

    for segment in segments:

        seg_start = max(
            float(segment["start"]),
            start
        )

        seg_end = min(
            float(segment["end"]),
            end
        )

        if seg_end <= seg_start:
            continue

        text = str(
            segment.get("text", "")
        ).strip()

        if not text:
            continue

        rows.append(
            f"{number}\n"
            f"{timestamp(seg_start - start)} --> "
            f"{timestamp(seg_end - start)}\n"
            f"{text}\n"
        )

        number += 1

    path.write_text(
        "\n".join(rows),
        encoding="utf-8"
    )


# ============================================================
# PROCESSAMENTO PRINCIPAL
# ============================================================

def worker(jobid, url, count, duration):

    job = JOBS / jobid
    job.mkdir(parents=True, exist_ok=True)

    state =
