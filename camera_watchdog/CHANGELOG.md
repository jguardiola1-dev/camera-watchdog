# Cambios

## 3.2.1

- Aviso por pérdida persistente de señal con check independiente por cámara.
- Espera configurable de 120 segundos inicialmente y al menos dos capturas fallidas.
- Recuperación del aviso al recibir una captura válida; los cambios de escena confirmados siguen activos.
- El sensor conjunto identifica las cámaras afectadas y el motivo de cada tipo de aviso.
- Los avisos de señal confirmados se conservan al reiniciar.
- Se conserva la última imagen válida si FFmpeg entrega un archivo no decodificable.

## 3.2.0

- Primera distribución preparada para un repositorio de Home Assistant.
- Construcción y comprobación automática para aarch64 y amd64.
- Hasta cinco referencias diurnas y cinco nocturnas por cámara.
- Referencias compartidas entre Census y ORB.
- Indicación independiente de disponibilidad para sabotaje.
- Página Histórico con pausa y borrado independientes de imágenes y diagnóstico.
- Persistencia del estado de aviso y recuperación al reconocer una referencia válida.
- Avisos globales desactivados inicialmente para facilitar la puesta en marcha.
