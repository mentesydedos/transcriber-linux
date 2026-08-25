# Estaciones FM de la ZMG sin streaming funcional confirmado

**Fecha de la investigación:** 2026-08-11

## Metodología

1. Se reconstruyó el dial completo de FM de la Zona Metropolitana de Guadalajara (Guadalajara, Zapopan, San Pedro Tlaquepaque, Tonalá y municipios colindantes) cruzando Wikipedia (en/es), worldradiomap.com, enmedios.com, eduardoradiozam.jimdofree.com y búsquedas dirigidas por frecuencia/distintivo (XH-).
2. Para cada estación del dial que **no** está en la lista de 25 ya integradas, se buscó su sitio oficial y su presencia en agregadores (Zeno.fm, TuneIn, Radio Garden, streema, onlineradiobox) y en la base de datos pública **Radio-Browser** (`api.radio-browser.info`), que indexa y verifica automáticamente URLs de streaming.
3. Cada URL candidata se probó **en vivo con `curl`/`ffprobe`** (no solo "se ve bien en una página"): se verificaron encabezados `icy-name`, `Content-Type` de audio, y que el cuerpo de la respuesta fueran bytes de audio real (AAC/MP3), no una página HTML genérica.
4. Se hizo además un escaneo de puertos del servidor `stream.promomedios.com` (SonicPanel) para las estaciones del Grupo Promomedios, ya que sus mounts no se listan por nombre sino por puerto.

## Resultado general

De las estaciones del dial de ZMG que **no** forman parte de las 25 ya integradas, la gran mayoría sí tiene streaming funcional verificable (se confirmó audio real fluyendo) a través de CDNs de terceros (StreamTheWorld, Zeno.fm, RadioJar, YesStreaming, Audiorama, servidorrprivado.com, Promomedios). Solo **2 estaciones** quedaron sin streaming funcional confirmado:

## Tabla: estaciones SIN streaming funcional confirmado

| Estación | Frecuencia | Distintivo | Razón concreta |
|---|---|---|---|
| Jalisco Radio (Sistema Jalisciense de Radio y Televisión, radio pública del estado) | 96.3 FM | XEJB-FM | **Stream roto/caído.** Se localizó el servidor de streaming (`https://sp2.servidorrprivado.com/8136/guadalajarafm`, indexado también en Radio-Browser). Responde HTTP 200, pero el propio servidor devuelve el mensaje explícito *"Stream is Offline — There is no sound on the radio. Start AutoDJ or stream music to the radio."* Se verificó dos veces con ~20 segundos de diferencia y el resultado fue idéntico: el AutoDJ/fuente de audio del operador no está conectado al servidor de streaming en este momento. No es un problema de geobloqueo ni de URL incorrecta — el mount existe pero no está emitiendo. |
| Rock & Soul | 91.9 FM | XHESP-FM (MegaRadio) | **No se pudo confirmar con certeza.** La estación aparece listada en Zeno.fm, TuneIn y Radio Garden, pero esas páginas cargan el reproductor mediante JavaScript del lado del cliente y no exponen ninguna URL de stream en el HTML estático (se descargó y revisó el HTML completo sin éxito). A diferencia de prácticamente todas las demás estaciones de Guadalajara, **no aparece indexada en Radio-Browser** (base de datos que sí valida automáticamente streams). Tampoco se localizó un mount activo al escanear puertos de servidores usados por estaciones hermanas. No se puede afirmar si tiene streaming funcional o no — se documenta como indeterminado en vez de asumir que no existe. |

## Nota sobre una frecuencia vacante

**99.1 FM** llegó a transmitir hacia la ZMG como repetición de *Origen Radio* (XHZAM-FM, concesionada en Mazamitla, Jalisco), pero el IFT le negó la renovación de la concesión el 8 de septiembre de 2021 por trámite extemporáneo, y la concesión venció el 1 de marzo de 2022. Actualmente no hay ninguna estación transmitiendo legalmente hacia la ZMG en esa frecuencia, por lo que no se incluye como "estación existente sin streaming" — sencillamente ya no existe como estación activa.

## Estaciones sin streaming que SÍ se lograron resolver en esta investigación (fuera del alcance original, informativo)

Estas 7 estaciones del dial de ZMG tampoco estaban en la lista de 25, pero durante la investigación se les encontró y verificó un stream funcional (audio real confirmado por `curl`/`ffprobe`), por lo que **no** entran en la tabla de "sin streaming" — se documentan aquí solo como hallazgo colateral útil para una futura integración:

| Estación | Frecuencia | Stream verificado |
|---|---|---|
| Zona Tres (Grupo Promomedios) | 91.5 FM | `https://stream.promomedios.com:8004/stream` |
| Fiesta Mexicana (Grupo Promomedios) | 92.3 FM | `https://stream.promomedios.com:8002/stream` |
| Radio Mujer (Grupo Promomedios) | 92.7 FM | `https://stream.promomedios.com:8008/stream` |
| Milenio Bella Música (Grupo Promomedios) | 105.1 FM | `https://stream.promomedios.com:8010/stream` |
| Heraldo Radio (Heraldo Media Group) | 100.3 FM | `https://stream.radiojar.com/21h1m4cch8nwv` |
| W Radio Guadalajara (Radiópolis) | 101.5 FM | `https://playerservices.streamtheworld.com/api/livestream-redirect/WRADIO_GDLAAC.aac` |
| La Coyotera (radio comunitaria) | 102.3 FM | `https://streaming.servicioswebmx.com:7058/stream` (audio real confirmado, aunque el encabezado `icy-name` del servidor trae el valor genérico sin personalizar "My Station name" — vale la pena una segunda verificación antes de integrarla en producción) |

## Referencia: las 25 estaciones ya integradas (excluidas de esta búsqueda)

1. Arroba FM — 88.7 FM
2. La Bestia Grupera — 89.1 FM
3. Radio Fórmula GDL — 89.5 FM
4. Match FM — 90.3 FM
5. Señal 90 — 90.7 FM
6. Amor 93.1 — 93.1 FM
7. KY 94.7 — 94.7 FM
8. La Mejor GDL — 95.5 FM
9. VOX Radio Hits — 95.9 FM
10. Ke Buena GDL — 97.1 FM
11. Fórmula Melódica — 97.9 FM
12. FM Globo GDL — 98.7 FM
13. Exa FM GDL — 101.1 FM
14. La Buena Onda — 101.9 FM
15. LOS40 GDL — 102.7 FM
16. Radio UDG — 104.3 FM
17. Retro FM GDL — 107.5 FM
18. Radio ITESO — 95.1 FM
19. Imagen Guadalajara — 93.9 FM
20. La Lupe — 99.9 FM
21. Magia Digital — 89.9 FM
22. Éxtasis Digital — 105.9 FM
23. Máxima FM — 106.7 FM
24. La Tapatía — 103.5 FM
25. Romance FM — 99.5 FM
