# Configuración y uso

## Primera puesta en marcha

1. Instala e inicia Camera Watchdog desde la tienda de aplicaciones de Home Assistant.
2. Abre la interfaz web y añade una cámara con su dirección RTSP y credenciales.
3. Comprueba la captura con la cámara colocada en su posición de vigilancia.
4. Abre **Ver cámara** y guarda las referencias de iluminación que correspondan.
5. Comprueba la evolución con los avisos globales desactivados, que es el valor inicial.
6. Habilita `census_alarms_enabled` en las opciones cuando hayas validado las referencias.

El check de cada cámara controla su participación en los avisos. Para cámaras
que giran o siguen movimiento automáticamente, decide si interesa habilitarlas:
sus cambios de orientación pueden corresponder al funcionamiento previsto.

## Referencias

Cada cámara admite hasta cinco imágenes en el grupo Día y cinco en Noche.
Solo se comparan las imágenes guardadas, aunque haya una sola. Si cualquiera
coincide de forma fiable, la escena se reconoce como normal y se puede recuperar
un aviso anterior. Para confirmar una pérdida de escena deben cambiar todas
las referencias de forma fiable durante las capturas de confirmación configuradas.

Usa **Añadir captura de día/noche** para guardar otra situación de iluminación.
Cada imagen permite actualización o borrado. Las referencias se seleccionan
manualmente: las muestras periódicas no sustituyen las referencias.
Después de un cambio, espera a que la siguiente captura valide las referencias.

La comparación estructural reduce la sensibilidad a variaciones de exposición,
pero no garantiza que cualquier iluminación sea reconocida como normal.
Comprueba especialmente el cambio a infrarrojos, el amanecer y el anochecer.
Una captura oscura, sin textura o perdida se muestra como incidencia de diagnóstico
y por sí sola no confirma sabotaje.

## Sensor de Home Assistant

Con los avisos habilitados, la aplicación publica `binary_sensor.sabotaje_cam`:

- `on`: una cámara habilitada mantiene un cambio de escena confirmado.
- `off`: las cámaras habilitadas han recuperado una escena válida o no hay un aviso activo.

La aplicación no arma Alarmo ni activa sirenas directamente. La respuesta al
sensor se configura en las automatizaciones de cada instalación.
Comprueba **Sabotaje operativo** en cada cámara; recibir imágenes no basta
para indicar que sus avisos estén habilitados y validados.

## Histórico y copias de seguridad

Pulsa **Histórico** en la barra superior. Imágenes de muestras y datos de
diagnóstico disponen de controles independientes para pausar, reanudar y borrar.
Las preferencias se mantienen después de reiniciar. El borrado requiere
confirmación y actúa sobre el grupo seleccionado.

Para detener el crecimiento de ambos históricos, pausa ambos guardados.
Las capturas actuales, referencias y detección siguen funcionando.
La pausa del diagnóstico también detiene los registros detallados de Census y ORB;
el registro operativo básico conserva su rotación.

El presupuesto inicial conjunto del histórico es de 512 MiB, con retención
máxima de siete días. El límite de espacio puede retirar muestras antes de
los siete días. El registro operativo está limitado a cuatro archivos de
aproximadamente 2 MiB cada uno. Las referencias no forman parte de la limpieza
del histórico y se conservan en los datos de la aplicación.

## Opciones principales

| Opción | Función |
| --- | --- |
| `census_alarms_enabled` | Permite publicar los avisos; inicialmente `false`. |
| `observation_interval_seconds` | Espera entre ciclos de captura y análisis. |
| `census_confirmations` | Capturas consecutivas necesarias para confirmar el cambio. |
| `observation_max_mb` | Presupuesto conjunto del histórico. |
| `observation_retention_days` | Antigüedad máxima del histórico. |
| `observation_snapshot_seconds` | Separación mínima entre imágenes periódicas. |
| `detailed_log` | Registro detallado cuando el diagnóstico no está pausado. |

Los valores avanzados de Census determinan la sensibilidad. Valida sus cambios
con las escenas reales de tus cámaras antes de utilizarlos en automatizaciones.
