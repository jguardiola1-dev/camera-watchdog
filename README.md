# Camera Watchdog

Aplicación para Home Assistant que supervisa cámaras RTSP y detecta cambios
persistentes de escena mediante Census y referencias seleccionadas manualmente.

- Hasta cinco referencias de día y cinco de noche por cámara, compartidas por los detectores.
- Una coincidencia fiable con cualquier referencia reconoce la escena normal.
- El cambio debe persistir frente a todas las referencias para confirmar un aviso.
- Panel de cámaras integrado en Home Assistant mediante Ingress.
- Histórico de imágenes y diagnóstico con pausa y borrado independientes.
- Aviso conjunto en `binary_sensor.sabotaje_cam`, habilitado de forma explícita.

## Instalación

Añade este repositorio desde **Ajustes → Aplicaciones → Instalar aplicación →
Repositorios**:

```text
https://github.com/jguardiola1-dev/camera-watchdog
```

Instala **Camera Watchdog**, inicia la aplicación y abre su interfaz web.
Consulta [la configuración y puesta en marcha](camera_watchdog/DOCS.md).

Las imágenes se preparan para `aarch64` y `amd64`. El flujo de GitHub Actions
ejecuta las pruebas y comprueba cada imagen en un equipo de su arquitectura
antes de publicar la imagen conjunta. La instalación desde el repositorio
requiere que esa publicación termine correctamente y que los paquetes de
GitHub Container Registry sean públicos.

## Desarrollo y publicación

El código de la aplicación está en `camera_watchdog/`.
Las versiones se construyen con las acciones oficiales de Home Assistant Builder.
Los cambios en `main` ejecutan las pruebas, construyen y publican ambas imágenes.
Las solicitudes de cambios ejecutan pruebas y construcciones sin publicar.

Antes de cambiar código que se vaya a distribuir, incrementa `version` en
`camera_watchdog/config.yaml` y actualiza `CHANGELOG.md`. Cada versión publicada
debe conservar su contenido: no reutilices una etiqueta para código diferente.
Para la primera publicación, revisa [la guía de publicación](PUBLISHING.md).

## Migración desde una instalación local

Home Assistant asigna un identificador diferente a la aplicación de este
repositorio. La configuración y las referencias de una instalación local no se
trasladan automáticamente. Haz una copia de seguridad y prepara la migración
antes de cambiar de instalación. Detén la anterior antes de arrancar la nueva,
para evitar que ambas publiquen sobre el mismo sensor.

Este repositorio contiene exclusivamente código, documentación y pruebas
sintéticas. Cada usuario configura sus cámaras y guarda sus propias referencias.
