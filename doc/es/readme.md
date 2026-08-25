# Cloud Uploader

Sube cualquier archivo a la nube y comparte el enlace de descarga, todo desde el teclado. No necesitas cuenta ni clave de API.

## Uso

Todo comienza con un solo atajo: **NVDA+alt+o**. Se abre un menú con estas opciones:

- **&Subir un archivo** — elige un archivo del disco y luego selecciona el servidor y el tiempo de expiración.
- **&Grabar** — graba un clip nuevo (micrófono, audio de la computadora, o ambos) y luego selecciona servidor y expiración.
- **&Grabación en segundo plano** — inicia una grabación sin abrir ninguna ventana (usa tu fuente y dispositivos predeterminados). Presiona NVDA+alt+o de nuevo para detenerla; se abrirá el cuadro de grabación habitual, donde podrás escuchar, editar y subir.
- **&Historial** — consulta todo lo que has subido. Enter muestra las opciones de Copiar, Abrir o Eliminar; Control+C copia el enlace directamente; Suprimir elimina una entrada. Los enlaces vencidos se quitan automáticamente de la lista.

Una vez abierto el menú, puedes ir directo a cualquier opción con su letra subrayada (U, R, B, H), igual que en cualquier otro menú de Windows. Escape lo cierra sin hacer nada.

Solo NVDA+alt+o viene asignado de forma predeterminada, para evitar conflictos con los comandos de NVDA o de otros complementos. Sin embargo, cada opción del menú también puede asignarse como atajo independiente desde el cuadro de diálogo de gestos de entrada de NVDA (NVDA+N → Preferencias → Gestos de entrada → Cloud Uploader), incluyendo "Grabar" por sí solo, para ir directo a grabar sin pasar por el menú.

También puedes ocultar "Grabar," "Grabación en segundo plano" o "Historial" del menú, de forma individual, desde Configuración — útil si ya le asignaste su propio atajo a alguno y no lo necesitas en el menú, o simplemente no lo usas. Si ocultas los tres, NVDA+alt+o deja de abrir el menú y te lleva directamente a elegir un archivo.

### Grabación

Puedes capturar el micrófono, el audio de la computadora, o ambos a la vez, con vista previa, deshacer/rehacer, eliminación de silencios, reducción de ruido, normalización de volumen y, si grabas ambas fuentes, controles de volumen independientes para cada una. Antes de subirse, la grabación se codifica a MP3, WAV o FLAC (mediante ffmpeg), según tu configuración.

## Servidores de subida

Los archivos se suben de forma anónima al servidor que elijas:

| Servidor | Tipo de enlace | Retención |
|---|---|---|
| Litterbox (catbox.moe) | Descarga directa | de 1 hora a 3 días, tú decides |
| Gofile | Página de descarga | ~10 días |
| Catbox (catbox.moe) | Descarga directa | Permanente |
| 0x0.st | Descarga directa | de 30 días a 1 año, según el tamaño |
| Filebin | Página de descarga | ~6 días |
| Uguu | Descarga directa | ~48 horas |

## Términos de servicio y uso aceptable

**Cada uno de estos servidores es un servicio gratuito e independiente, no algo administrado por este complemento.** Cada uno tiene sus propias reglas sobre el tamaño de archivo permitido, el contenido aceptado y qué ocurre si se incumplen esas reglas. Cloud Uploader no revisa el contenido que subes ni hace cumplir estas reglas en tu nombre — es tu responsabilidad respetar los términos de cada servidor. Un resumen, vigente en 2026:

- **Litterbox / Catbox** (catbox.moe): Catbox limita los archivos a 200 MB; Litterbox (temporal) permite hasta 1 GB. Ninguno de los dos acepta archivos `.exe`, `.scr`, `.cpl`, `.doc*` ni `.jar`, y ambos prohíben material de abuso sexual infantil, malware, episodios completos de series o anime pirateados, y contenido con violencia gráfica extrema. El uso comercial requiere aprobación previa. Incumplir sus reglas resulta en la eliminación del archivo y **el bloqueo de tu dirección IP**.
- **Gofile**: no publican un límite oficial de tamaño por archivo, pero las cuentas gratuitas tienen un límite de tráfico (históricamente, alrededor de 100 GB al mes) y una limitación en la cantidad de solicitudes — superar estos límites puede generar errores o un **bloqueo temporal de IP**. Los archivos gratuitos suelen conservarse unos 10 días si no se descargan; el contenido que incumpla sus términos puede eliminarse y la cuenta puede quedar restringida.
- **0x0.st**: tamaño máximo de 512 MiB. Sus términos prohíben explícitamente la piratería, la pornografía o violencia gráfica, material extremista o terrorista, malware, filtraciones de datos personales, spam generado por inteligencia artificial, subidas automatizadas masivas, y cualquier contenido ilegal según la ley alemana (jurisdicción del servidor). Incumplir sus reglas provoca la eliminación del archivo y **puede bloquear tu IP** para futuras subidas.
- **Filebin**: no tiene un límite fijo por archivo, pero sí un límite total de almacenamiento, y dejará de aceptar subidas cuando se alcance. Las direcciones IP se registran para el manejo de abusos y **pueden compartirse con las autoridades si se solicita**; las IPs que suban contenido malicioso son bloqueadas.
- **Uguu**: tamaño máximo de 128 MiB en su instancia oficial, con una expiración automática breve (de algunas horas a unos días). El malware está explícitamente prohibido. Las solicitudes de retiro por derechos de autor se gestionan a través de abuse@pomf.se.

**En resumen:** sube únicamente contenido razonable, legal y de un tamaño adecuado, y no dependas de ninguno de estos servidores para nada sensible, permanente o de gran volumen. Si un servidor bloquea tu IP por incumplir sus términos, ese bloqueo lo aplica el propio servidor — Cloud Uploader no tiene forma de apelarlo ni de evitarlo en tu nombre.

La primera vez que inicies NVDA después de instalar este complemento, aparecerá un resumen de este aviso en un cuadro de diálogo. Deberás marcar "He leído y entiendo lo anterior" antes de que el botón Aceptar esté disponible. Elegir Rechazar, o cerrar el cuadro con Escape, no registra la aceptación, por lo que el aviso volverá a aparecer la próxima vez que inicies NVDA. Una vez aceptado, no volverá a mostrarse, salvo que el contenido de este aviso cambie de manera significativa en una versión futura; las actualizaciones habituales (nuevas funciones, corrección de errores) no lo hacen reaparecer.

## Configuración

Disponible en NVDA+control+g → Cloud Uploader: servidor predeterminado, copia automática del enlace al subir, tamaño del historial, opciones de grabación (formato, calidad, dispositivo, canales, inicio automático de grabación, ruta de ffmpeg), y qué elementos aparecen en el menú de NVDA+alt+o.

## Notas

- Solo puede realizarse una subida a la vez.
- Subir archivos, consultar el historial y grabar en segundo plano permanecen deshabilitados hasta que aceptes el aviso de términos de servicio mencionado arriba. Si lo cierras sin aceptar, reinicia NVDA para que vuelva a aparecer.
- Todos los atajos pueden reasignarse desde el cuadro de diálogo de gestos de entrada de NVDA (NVDA+N → Preferencias → Gestos de entrada → Cloud Uploader).

## Apoyo

Si Cloud Uploader te ha sido útil, encontrarás un botón de "Donar para apoyar el desarrollo"
en Configuración, o puedes visitar directamente
[ko-fi.com/naday](https://ko-fi.com/naday).

## Contacto

Preguntas, reportes de errores o sugerencias de funciones: dianarl0206@gmail.com
