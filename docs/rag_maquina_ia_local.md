# Preguntas en lenguaje natural sobre AlertaTV — plan de arquitectura

**Fecha:** 2026-08-11

## Arquitectura elegida: dos máquinas, todo local

Esta máquina (transcriber-linux) ya corre 24/7 con la GPU casi al tope
(~80% de VRAM entre Parakeet-TDT y los workers de CTC-ES) y la CPU con picos
altos por la grabación de video. No hay margen seguro para meter además un
LLM de generación aquí sin arriesgar el pipeline de transcripción en vivo.

Por eso el diseño separa dos roles en dos máquinas:

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  ESTA máquina (transcriber)  │  LAN    │  Máquina nueva (a definir)     │
│                               │ ──────► │                                │
│  - transcriptions.db (fuente │  HTTP   │  - LLM local (Ollama)          │
│    de verdad, escrita 24/7   │         │  - Recibe la pregunta del      │
│    por los motores de ASR)   │         │    usuario                     │
│  - rag_index.db (índice      │         │  - Le pide contexto real a     │
│    separado, solo lectura    │ ◄────── │    esta máquina (/search)      │
│    desde transcriptions.db)  │  JSON   │  - Redacta la respuesta final  │
│  - rag_api.py: expone        │         │    con ese contexto            │
│    /search por HTTP          │         │                                │
└─────────────────────────────┘         └──────────────────────────────┘
```

Esta máquina **nunca envía la base de datos completa ni da acceso directo
al archivo** — solo responde con los fragmentos de texto ya relevantes a
cada pregunta puntual, vía un endpoint HTTP con token.

## Lo que ya quedó listo en esta máquina

- `rag_index.py`: indexa `transcriptions.db` (solo lectura, `PRAGMA
  query_only`) hacia `rag_index.db` (archivo separado, nunca toca la base
  de producción). Guarda embeddings (modelo `intfloat/multilingual-e5-small`,
  ligero, buen español, corre en CPU) + índice FTS5 de palabra clave.
- Backfill histórico de ~2.1M transcripciones en curso (corre en segundo
  plano con prioridad baja — `nice 15`/`ionice -c3` — para no competir con
  la transcripción en vivo). Después de eso, `systemd/rag-index.timer`
  corre cada 5 min y solo procesa lo nuevo (segundos, no horas).
- `rag_search.py`: búsqueda híbrida (FTS5 + similitud vectorial) que
  devuelve los fragmentos de transcripción más relevantes a una pregunta,
  con filtros opcionales por canal y rango de fechas.
- `rag_api.py`: expone `rag_search` como endpoint HTTP
  (`POST /search {"q": "..."}`) en `148.201.38.17:8765`, protegido con un
  token compartido (`/home/transcriber/.rag-api-token`, permisos 600).
- `systemd/rag-index.service` + `.timer`, `systemd/rag-api.service`: listos
  para desplegar (mismo patrón de `sudo cp` + `daemon-reload` + `enable
  --now` que el resto de servicios de este proyecto).

## Pendiente: falta la máquina nueva

### Specs recomendadas (por nivel, según presupuesto)

| Nivel | GPU | RAM | Qué modelo de LLM permite | Notas |
|---|---|---|---|---|
| **Mínimo viable** | 8GB VRAM (ej. RTX 4060 8GB, RTX 3060 12GB) | 16GB | Modelos 7-9B cuantizados (Q4) — ej. Qwen2.5-7B-Instruct, Llama-3.1-8B-Instruct | Suficiente para responder bien en español con el contexto recuperado; respuestas en 2-5s típico. |
| **Recomendado** | 16GB VRAM (ej. RTX 4070 Ti Super 16GB, RTX 4080) | 32GB | Modelos 14B cuantizados, o 7-9B sin cuantizar | Mejor calidad de redacción/razonamiento, sigue siendo rápido. |
| **Sin prisa por GPU** | Ninguna (CPU-only, ej. un mini-PC con Ryzen/Intel de gama media) | 32GB+ | Modelos 7-8B cuantizados en CPU vía llama.cpp | Funciona, pero respuestas de 15-40s en vez de 2-5s. Válido si el uso es esporádico (preguntar y esperar un rato es aceptable). |

No hace falta gastar en una GPU de gama alta para este uso específico — el
trabajo pesado (transcribir 64 canales de audio en tiempo real) ya lo hace
esta máquina. La máquina nueva solo necesita generar texto a partir de
contexto ya recuperado, una tarea mucho más liviana.

### Software sugerido (a confirmar viendo las specs reales)

**Ollama** es la recomendación por defecto: instalación de un comando,
maneja la cuantización y la carga del modelo automáticamente, y expone su
propia API HTTP local (`localhost:11434`) — fácil de conectar con un
script que primero le pregunte a `rag_api.py` de esta máquina por contexto,
arme el prompt, y se lo mande a Ollama para la respuesta final. Si alguien
va a usarlo también de forma interactiva directo en esa máquina (no solo
vía la pregunta desde el dashboard), LM Studio da una interfaz gráfica más
amigable arriba de lo mismo.

### Cómo conectar cuando la máquina exista

1. Confirmar specs reales (GPU/VRAM/RAM) → elegir tamaño de modelo de la
   tabla de arriba.
2. Instalar Ollama (o LM Studio) y descargar el modelo elegido.
3. Script puente (a escribir cuando llegue el momento, corto): recibe la
   pregunta del usuario → `POST http://148.201.38.17:8765/search` con
   header `X-RAG-Token: <contenido de .rag-api-token>` → arma un prompt en
   español con los fragmentos devueltos como contexto → se lo pasa al LLM
   local vía Ollama → devuelve la respuesta.
4. Conectar ese script con la UI del dashboard de AlertaTV (pendiente,
   tarea #51 en el tracker de esta sesión) — un cuadro de "preguntar" que
   llame al script puente de la máquina nueva.

## Decisiones de diseño y por qué

- **`rag_index.db` separado de `transcriptions.db`**: para que el índice
  nunca compita por locks/escrituras con los motores de ASR que escriben
  la base de producción 24/7. Se reconstruye leyendo de ahí, nunca escribe
  ahí.
- **Endpoint HTTP en vez de compartir el archivo por red**: SQLite no está
  pensado para abrirse en vivo desde un filesystem de red (riesgo de
  corrupción con locks distribuidos poco confiables). El endpoint HTTP
  evita ese riesgo por completo — la máquina nueva nunca toca el archivo.
- **Token compartido + bind solo a la IP LAN**: el contenido de las
  transcripciones no es información pública, así que el endpoint no queda
  abierto sin autenticar ni expuesto más allá de la red local.
- **Generación de lenguaje en la máquina nueva, no aquí**: la GPU de esta
  máquina ya está al límite del margen seguro (~2.5GB libres de 12GB) tras
  meses de ajustes para sostener transcripción en tiempo real de 64
  canales — no vale la pena arriesgar esa estabilidad por una función que
  puede vivir perfectamente en hardware separado.
