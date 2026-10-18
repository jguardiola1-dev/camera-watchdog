# Primera publicación

1. Sube la estructura completa a la raíz de `jguardiola1-dev/camera-watchdog`.
   Deben aparecer `repository.yaml`, `camera_watchdog/`, `.github/` y `scripts/`.
2. Revisa **Actions → Build and publish**. Deben terminar correctamente las
   pruebas, las comprobaciones nativas y la publicación de la imagen conjunta.
3. En los ajustes de los paquetes de GitHub Container Registry, establece
   visibilidad **Public** para `aarch64-camera-watchdog`,
   `amd64-camera-watchdog` y `camera-watchdog`. La primera publicación de un
   paquete puede ser privada aunque el repositorio de código sea público.
4. Comprueba desde una sesión sin autenticar que se descarga la etiqueta
   indicada en `camera_watchdog/config.yaml`.
5. Añade el repositorio a una instalación de prueba de Home Assistant.
   Valida las capturas, referencias, sensor e históricos antes de migrar una
   instalación que ya esté vigilando cámaras.

Las acciones utilizan `GITHUB_TOKEN`, que GitHub proporciona automáticamente.
No añadas contraseñas ni tokens personales a los archivos del repositorio.

## Versiones siguientes

Incrementa la versión antes de publicar código diferente. Las acciones evitan
sobrescribir una etiqueta ya existente. Si la construcción o las pruebas
fallan, corrige el problema y usa una versión nueva si ya se publicó alguna
de las imágenes de esa versión.

## Licencia

La licencia debe decidirla el autor antes de anunciar la aplicación para uso
y modificación por terceros. Hasta que se incorpore una licencia, este paquete
no concede permisos adicionales de redistribución o modificación.
