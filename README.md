# Cortes IA V3 — versão com IA

Fluxo:
1. recebe URL do YouTube;
2. baixa o vídeo;
3. extrai áudio;
4. transcreve com modelo de speech-to-text da OpenAI;
5. envia a transcrição para um modelo de linguagem escolher os melhores momentos;
6. cria SRT sincronizado;
7. renderiza vídeo vertical 1080x1920 com legenda queimada;
8. mostra títulos/ganchos e botões de download.

## Configuração
Defina:
OPENAI_API_KEY=sua_chave

Opcional:
TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
ANALYSIS_MODEL=gpt-5.6-luna

## Rodar
pip install -r requirements.txt
python app.py

Ou use Docker/Render.

## Observações
Processar lives longas exige bastante CPU, disco e tempo. Hospedagem gratuita pode ter limites de armazenamento/tempo e não é ideal para vídeos muito longos. Para produção, use worker/background job + armazenamento de objetos.
