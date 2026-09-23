import os, uuid, json, threading, subprocess, math, re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from openai import OpenAI

BASE=Path(__file__).resolve().parent
JOBS=BASE/"jobs"; JOBS.mkdir(exist_ok=True)
app=Flask(__name__)

def save(job,state):
    (JOBS/job/"state.json").write_text(json.dumps(state,ensure_ascii=False),encoding="utf-8")

def cmd(c):
    print("EXECUTANDO:", " ".join(map(str, c)), flush=True)

    result = subprocess.run(
        c,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print(result.stdout, flush=True)

    if result.returncode != 0:
        raise RuntimeError(
            "Comando falhou (exit %s):\n%s"
            % (result.returncode, result.stdout[-8000:])
        )

    return result.stdout

def transcribe(client,audio):
    with open(audio,"rb") as f:
        r=client.audio.transcriptions.create(
            model=os.getenv("TRANSCRIBE_MODEL","gpt-4o-mini-transcribe"),
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
    return [{"start":s.start,"end":s.end,"text":s.text.strip()} for s in r.segments]

def choose_moments(client,segments,count,duration):
    transcript="\n".join(f"[{x['start']:.1f}-{x['end']:.1f}] {x['text']}" for x in segments)
    prompt=f"""Você é editor de cortes virais para Shorts/Reels/TikTok.
Analise a transcrição abaixo e escolha até {count} trechos fortes.
Cada trecho deve ter entre 30 e {duration} segundos, começar e terminar em pontos naturais,
e priorizar: gancho forte, opinião polêmica sem inventar fatos, história, surpresa,
dica prática, pergunta/resposta ou momento engraçado.
Retorne SOMENTE JSON no formato:
{{"clips":[{{"start":0,"end":60,"title":"...","hook":"..."}}]}}
Não invente falas. Use apenas timestamps da transcrição.
TRANSCRIÇÃO:
{transcript[:180000]}"""
    r=client.responses.create(
        model=os.getenv("ANALYSIS_MODEL","gpt-5.6-luna"),
        input=prompt
    )
    text=r.output_text
    m=re.search(r'\{.*\}',text,re.S)
    if not m: raise ValueError("A IA não retornou JSON válido.")
    data=json.loads(m.group(0))
    return data.get("clips",[])[:count]

def make_srt(segments,start,end,path):
    rows=[]; n=1
    for s in segments:
        a=max(s["start"],start); b=min(s["end"],end)
        if b<=a: continue
        def ts(v):
            ms=int((v-int(v))*1000); sec=int(v); h=sec//3600; sec%=3600; mi=sec//60; sec%=60
            return f"{h:02d}:{mi:02d}:{sec:02d},{ms:03d}"
        rows.append(f"{n}\n{ts(a-start)} --> {ts(b-start)}\n{s['text']}\n")
        n+=1
    path.write_text("\n".join(rows),encoding="utf-8")

def worker(jobid,url,count,duration):
    job=JOBS/jobid; job.mkdir(exist_ok=True)
    state={"status":"iniciando","progress":1,"message":"Preparando IA...","clips":[]}
    save(job,state)
    try:
        key=os.getenv("OPENAI_API_KEY")
        if not key: raise RuntimeError("Defina OPENAI_API_KEY no servidor.")
        client=OpenAI(api_key=key)

        video=job/"source.mp4"
        cmd(["yt-dlp","--no-playlist","-f","bv*[height<=720]+ba/b[height<=720]",
             "--merge-output-format","mp4","-o",str(video),url])
        state.update(status="audio",progress=20,message="Extraindo áudio para transcrição...")
        save(job,state)

        audio=job/"audio.mp3"
        cmd(["ffmpeg","-y","-i",str(video),"-vn","-ac","1","-ar","16000","-b:a","64k",str(audio)])
        state.update(status="transcricao",progress=35,message="Transcrevendo a live com IA...")
        save(job,state)
        segments=transcribe(client,audio)
        (job/"transcript.json").write_text(json.dumps(segments,ensure_ascii=False),encoding="utf-8")

        state.update(status="selecao",progress=55,message="IA escolhendo os melhores momentos...")
        save(job,state)
        clips=choose_moments(client,segments,count,duration)

        state.update(status="render",progress=65,message="Renderizando cortes e legendas...")
        save(job,state)
        outclips=[]
        for i,c in enumerate(clips,1):
            start=max(0,float(c["start"])); end=min(start+duration,float(c["end"]))
            if end-start<15: continue
            srt=job/f"clip_{i:02d}.srt"; out=job/f"corte_{i:02d}.mp4"
            make_srt(segments,start,end,srt)
            vf=("scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1,"
                "subtitles="+str(srt).replace("\\","/")+":force_style="
                "'FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
                "Alignment=2,MarginV=100'")
            cmd(["ffmpeg","-y","-ss",str(start),"-i",str(video),"-t",str(end-start),
                 "-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","23",
                 "-c:a","aac","-b:a","128k",str(out)])
            outclips.append({"file":f"/download/{jobid}/{out.name}",
                             "title":c.get("title","Corte"),"hook":c.get("hook","")})
            state["progress"]=65+int(35*i/max(1,len(clips)))
            state["clips"]=outclips; save(job,state)

        state.update(status="concluido",progress=100,message="Tudo pronto!",clips=outclips)
        save(job,state)
    except Exception as e:
        state.update(status="erro",progress=100,message=str(e)); save(job,state)

@app.get("/")
def home(): return render_template("index.html")

@app.post("/api/start")
def start():
    d=request.get_json(force=True); url=(d.get("url") or "").strip()
    if not url: return jsonify(error="Cole um link do YouTube."),400
    count=max(1,min(int(d.get("count",5)),15)); duration=max(30,min(int(d.get("duration",60)),120))
    job=uuid.uuid4().hex[:12]
    threading.Thread(target=worker,args=(job,url,count,duration),daemon=True).start()
    return jsonify(job_id=job)

@app.get("/api/status/<job>")
def status(job):
    p=JOBS/job/"state.json"
    if not p.exists(): return jsonify(error="Job não encontrado"),404
    return jsonify(json.loads(p.read_text(encoding="utf-8")))

@app.get("/download/<job>/<filename>")
def download(job,filename): return send_from_directory(JOBS/job,filename,as_attachment=True)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)))
