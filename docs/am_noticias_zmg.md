# Las 8 estaciones AM de noticias/hablado de mayor audiencia en la ZMG

**Fecha de la investigación:** 2026-08-11

## Metodología

1. **Reconstrucción del dial AM de la ZMG**: se cruzó Wikipedia (en/es), enmedios.com, el directorio del Observatorio ITESO (`observatorioiteso2012.wordpress.com`), Círculo de Editores de Jalisco, y búsquedas dirigidas por frecuencia/distintivo (XE-) para levantar el dial completo de AM de Guadalajara — 24 a 26 frecuencias entre 580 y 1480 kHz. Un hallazgo importante: **la mayoría de las estaciones AM históricamente noticiosas de la ZMG migraron a FM entre 2017 y 2021** (programa de migración AM→FM del IFT) y su frecuencia AM quedó silenciada, revendida a otro formato, o convertida en repetidora de una estación musical. Esto redujo considerablemente el universo real de AM de noticias/hablado todavía activas en 2026 respecto a lo que sugieren los directorios desactualizados.
2. **Determinación de audiencia**: se buscó primero un rating numérico duro. Se encontró: **Massive Caller, ranking de audiencia de Guadalajara, mayo 2022** (vía radioNOTAS y deRadios.com), que mide puntos de rating y audiencia acumulada por estación. Es la única fuente con cifras duras localizadas — de las AM, únicamente **Radio Metrópoli** (6.2 puntos, 326,814 de audiencia acumulada — **líder de todo el mercado de Guadalajara, FM y AM combinadas**) y **Radio María** (1.50 puntos, 79,228 de audiencia acumulada) aparecieron con cifra propia en esa medición. Para las 6 estaciones restantes **no se localizó rating numérico duro y actualizado**, así que se usó como proxy: (a) pertenencia a un grupo radiofónico nacional grande (Radio Fórmula, Grupo Radio Centro, Grupo Radio Cañón/Radiópolis, Radiorama de Occidente, Grupo Unidifusión/Notisistema), (b) antigüedad/trayectoria de la marca, (c) potencia de transmisión (kW), y (d) el hecho de que la marca en cuestión sigue viva y transmitiendo en 2026 (a diferencia de la mayoría de sus pares AM que ya desaparecieron o migraron). Esto se declara explícitamente en la tabla.
3. **Verificación de streaming real**: para cada estación se buscó el stream en su sitio oficial, agregadores (Zeno.fm, TuneIn, Radio Garden, Streema, OnlineRadioBox) y sobre todo en **Radio-Browser** (`api.radio-browser.info`), que indexa y valida streams automáticamente. Cada URL candidata se probó **en vivo con `curl`** (incluyendo `-L` para seguir redirecciones en radiojar/zeno.fm/streamtheworld, que usan capas de redirección con token): se verificaron encabezados `icy-name` / `icy-genre` / `Content-Type` y se confirmó con el comando `file` (y en un caso `ffprobe`) que el cuerpo descargado fueran bytes de audio real (MP3 o AAC), no HTML.
4. Se documenta explícitamente cualquier estación que no se pudo verificar con streaming funcional — no se fuerza ningún resultado.

## Resultado general

Se identificaron y verificaron con audio real las **8 estaciones AM de noticias/hablado** de mayor audiencia (medida o por proxy) de la ZMG. **Las 8 tienen streaming funcional confirmado.** No hubo ninguna sin verificar dentro de las 8 finales — sin embargo, se descartaron varias estaciones AM que en directorios aparecen como "de noticias" pero que en 2026 **ya no lo son** (ver sección de descartes más abajo), lo cual es un hallazgo relevante en sí mismo para no integrarlas por error.

## Tabla: las 8 estaciones AM de noticias/hablado de mayor audiencia

| # | Estación | Frecuencia | Distintivo | Grupo propietario | Por qué se considera de alta audiencia | Stream verificado |
|---|---|---|---|---|---|---|
| 1 | Radio Metrópoli | 1150 AM | XEAD-AM | Grupo Unidifusión (red Notisistema) | **Dato duro**: líder de *todo* el mercado radiofónico de Guadalajara (FM y AM combinadas) según Massive Caller, mayo 2022 — 6.2 puntos de rating, 326,814 de audiencia acumulada. Fundada en 1936, con 50 kW de día. | `https://s2.yesstreaming.net:9088/stream` |
| 2 | Radio María México | 920 AM | XELT-AM | Radio María (red católica nacional) | **Dato duro**: segunda AM con mayor audiencia medida en Massive Caller mayo 2022 — 1.50 puntos, 79,228 de audiencia acumulada. Formato hablado no musical (educación católica, charlas, oración), no es "noticias" en sentido estricto pero sí es la AM hablada #2 por rating real. Se documenta la salvedad. | `http://dreamsiteradiocp.com:8086/stream` (feed nacional de Radio María México; XELT-AM Guadalajara es repetidora de este mismo contenido) |
| 3 | Radio Fórmula (Primera Cadena) | 790 AM | XEGAJ-AM | Organización Radio Fórmula | Proxy: marca nacional de noticias/talk más reconocida de México, con cadena propia en Guadalajara desde 2000; sin rating numérico local actualizado localizado. | `https://stream.radiojar.com/8a7k0514xp8uv` |
| 4 | Universal / Red AM 700 | 700 AM | XEDKR-AM | Grupo Radio Centro | Proxy: al aire desde 1953, uno de los grupos radiofónicos nacionales más grandes de México; `icy-genre: News` confirmado en el propio stream. Sin rating numérico local actualizado localizado. | `https://playerservices.streamtheworld.com/api/livestream-redirect/XEDKR_AMAAC.aac` |
| 5 | W Deportes | 1010 AM | XEHL-AM | Grupo Radio Cañón / Radiópolis (marca "W") | Proxy: marca "W" (Radiópolis/Televisa) tiene fuerte reconocimiento nacional; formato de deportes hablado (debate, análisis, entrevistas) en una plaza con enorme afición futbolera (Atlas, Chivas). Sin rating numérico local actualizado localizado. | `https://streamingcwsradio30.com/8294/` |
| 6 | DK 1250 | 1250 AM | XEDK-AM | Radiorama de Occidente | Proxy: uno de los 6 grupos más grandes de AM en la ZMG (Radiorama de Occidente); formato hablado/noticias etiquetado explícitamente como tal en Radio-Browser (`news talk`, `noticias locales`). Sin rating numérico local actualizado localizado. | `https://sp2.servidorrprivado.com:10947/` |
| 7 | Radiorama Frecuencia Deportiva | 1340 AM | XEDKT-AM | Radiorama de Occidente | Proxy: mismo grupo (Radiorama de Occidente); formato deportivo hablado con entrevistas en vivo. Sin rating numérico local actualizado localizado. | `https://stream.zeno.fm/r1540b8e408uv` (redirige a `stream-287.surfernetwork.com`; verificar con `curl -L`) |
| 8 | Radio Vital | 1310 AM | XETIA-AM | Grupo Unidifusión (red Notisistema) | Proxy: mismo grupo que el líder de mercado (Radio Metrópoli), con marca Notisistema; formato hablado de salud, no es noticias generalistas — es la opción más débil de las 8, incluida para completar el número por pertenecer a un grupo mayor consolidado. Sin rating numérico local actualizado localizado. | `https://s2.yesstreaming.net:9097/stream` |

### Evidencia de verificación (resumen técnico)

| Estación | `Content-Type` / `icy-genre` observado | Formato de audio confirmado |
|---|---|---|
| Radio Metrópoli | `audio/mpeg`, `icy-genre: News`, `icy-name: Radio Metropoli`, `icy-description: La Estacion de las Noticias` | MP3 real (confirmado con `file`) |
| Radio María México | `audio/aacp`, `icy-genre: Talk`, `icy-name: RADIO MARIA MEXICO` | AAC real (confirmado con `file`) |
| Radio Fórmula 790 | `audio/aac` (tras redirección) | AAC real (confirmado con `file`) |
| Universal / Red AM 700 | `audio/aacp`, `icy-genre: News`, `icy-name: Red AM 700` | AAC real (confirmado con `file`) |
| W Deportes 1010 | `audio/mpeg` (icy-name genérico "No Name" — servidor sin metadatos personalizados) | MP3 real (confirmado con `ffprobe`, formato mp3, ~25s de audio recibido) |
| DK 1250 | `audio/aacp`, `icy-name: XEDK` | AAC real (confirmado con `file`) |
| Radiorama Frecuencia Deportiva | `audio/mpeg`, `icy-name: Frecuencia Deportiva 1340 AM - XEDKT` (tras redirección) | MP3 real (confirmado con `file`) |
| Radio Vital | `audio/mpeg`, `icy-name: Radio Vital`, `icy-description: Radio Vital en Vivo` | MP3 real (confirmado con `file`) |

## Formato listo para integración futura (estilo `TV audio.m3u`)

```
#EXTINF:-1 tvg-chno="1150",Radio Metropoli
https://s2.yesstreaming.net:9088/stream
#EXTINF:-1 tvg-chno="920",Radio Maria Mexico
http://dreamsiteradiocp.com:8086/stream
#EXTINF:-1 tvg-chno="790",Radio Formula GDL AM
https://stream.radiojar.com/8a7k0514xp8uv
#EXTINF:-1 tvg-chno="700",Universal Red AM 700
https://playerservices.streamtheworld.com/api/livestream-redirect/XEDKR_AMAAC.aac
#EXTINF:-1 tvg-chno="1010",W Deportes 1010
https://streamingcwsradio30.com/8294/
#EXTINF:-1 tvg-chno="1250",DK 1250
https://sp2.servidorrprivado.com:10947/
#EXTINF:-1 tvg-chno="1340",Frecuencia Deportiva 1340
https://stream.zeno.fm/r1540b8e408uv
#EXTINF:-1 tvg-chno="1310",Radio Vital
https://s2.yesstreaming.net:9097/stream
```

**Nota técnica para integración**: los streams de `radiojar.com` y `zeno.fm` responden con un HTTP 302 antes de entregar el audio (token de sesión de corta duración); cualquier consumidor (ffmpeg, VLC, el pipeline de transcripción) debe seguir redirecciones automáticamente, lo cual ya hacen ffmpeg/VLC por defecto. El stream de `streamtheworld.com` (Universal 700) hace dos saltos de redirección antes del audio final — también manejado de forma transparente por ffmpeg.

## Estaciones descartadas que aparecen en directorios como "de noticias" pero ya NO lo son en 2026

Este hallazgo es relevante para no integrarlas por error, ya que enmedios.com y otros directorios web tienen información desactualizada:

| Estación (nombre histórico) | Frecuencia | Distintivo | Qué pasó |
|---|---|---|---|
| La Voz de Guadalajara | 960 AM | XEHK-AM | Dejó de transmitir el 21 de marzo de 2026 tras 87 años; Grupo Audiorama la convirtió en repetidora de XHGDA-FM "La Bestia Grupera" (regional mexicano, musical). |
| 850 Noticias | 850 AM | XEMIA-AM | La frecuencia AM se apagó; el formato de noticias migró a FM en 2018 como XHEMIA-FM 90.3, y ese FM cambió después a "Match FM" con formato pop musical. Ya no existe streaming AM de noticias en 850. |
| 1070 Noticias | 1070 AM | XESP-AM | La AM dejó de transmitir el 10 de diciembre de 2019; XHESP-FM 91.9 (su sucesora en FM) cambió a formato rock musical ("Rock & Soul", ya documentado como sin streaming verificable en la investigación de FM previa). |
| W Radio | 1190 AM | XEWK-AM | La AM cesó operaciones el 29 de marzo de 2021; W Radio ahora transmite solo en 101.5 FM (ya integrada en el sistema). |
| ABC Radio | 1440 AM | XEABCJ-AM | Migró a FM en 2019 como XHABCJ-FM 95.9, que hoy es "Vox Radio Hits" (formato musical pop). No hay AM de noticias activa en esta frecuencia. |
| Canal 58 / ABC Radio | 580 AM | XEAV-AM | Hoy es "Radio Cañón 580", simulcast musical de Vox FM — ya no es noticias. |
| Radio Mujer / ESNE Radio | 880 AM / 1040 AM | XEAAA-AM / XEBBB-AM | Intercambiaron formatos en 2018; Radio Mujer migró a FM 92.7 (ya integrada en el sistema como parte de Grupo Promomedios); ESNE Radio (1040 AM, católica) quedó fuera del aire desde finales de febrero de 2026. |
| Radio Fórmula (Segunda Cadena) / "DK Noticias" | 1230 AM | XEDKN-AM | Sigue existiendo según fuentes de 2026 y comparte formato de noticias/talk de Radio Fórmula, pero **no se localizó un stream indexado en Radio-Browser ni un mount propio verificable** (solo páginas agregadoras con reproductor en JavaScript). No se incluyó en la tabla de 8 para evitar duplicar la marca Radio Fórmula (ya cubierta por la Primera Cadena en 790 AM) y por no poder verificar audio real de forma independiente. Se documenta aquí como pendiente, no como "sin streaming confirmado" de las 8 finales — quedó fuera del top 8 por decisión metodológica, no por falla de verificación.

## Referencia: 32 estaciones FM ya integradas (fuera del alcance de esta búsqueda)

Ver la lista completa de 25 + 7 en `docs/fm_zmg_sin_streaming.md`. Ninguna de las 8 estaciones AM de esta tabla se superpone con esas 32 — todas son frecuencias AM activas, distintas de sus posibles "hermanas" FM ya integradas (p. ej. Radio Fórmula GDL FM 89.5 ya está integrada; Radio Fórmula AM 790 es una señal AM independiente de la misma organización, con su propio stream).
