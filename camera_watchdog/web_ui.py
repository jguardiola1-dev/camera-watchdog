"""Presentation only. Camera processing and alarm decisions live in app.py."""
import html
import time
from urllib.parse import quote
from reference_catalog import REFERENCE_KINDS, REFERENCE_GROUPS, reference_label


def esc(value):
    return html.escape(str(value), quote=True)


def icon(name, size=20):
    paths = {
        'camera': '<rect x="3" y="6" width="13" height="12" rx="3"/><path d="m16 10 5-3v10l-5-3"/>',
        'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        'shield': '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
        'plus': '<path d="M12 5v14M5 12h14"/>',
        'arrow': '<path d="M5 12h14m-5-5 5 5-5 5"/>',
        'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
        'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
        'moon': '<path d="M20 14A9 9 0 0 1 10 4a9 9 0 1 0 10 10Z"/>',
    }
    return f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths.get(name, paths["camera"])}</svg>'


def header(title, prefix, collection_only):
    mode = ('<span class="mode-pill"><span class="dot"></span>Solo observación</span>'
            if collection_only else '<span class="mode-pill"><span class="dot"></span>Avisos de cámaras activos</span>')
    notice = ('<aside class="observation-note">' + icon('shield') +
              '<div><strong>Recogida de datos sin alarmas</strong>'
              '<span>Las cámaras capturan y registran cambios. Las alarmas están bloqueadas durante la prueba.</span></div>'
              '<span class="note-tag">Prueba en curso</span></aside>') if collection_only else (
              '<aside class="observation-note">' + icon('shield') +
              '<div><strong>Avisos de cambio de escena activos</strong>'
              '<span>Census compara referencias validadas. La pérdida persistente de señal avisa en las cámaras con ese check activado. '
              'Una imagen sin textura se muestra como incidencia de diagnóstico.</span>'
              '<span id="publication-status" role="status">Comprobando conexión con Home Assistant…</span></div></aside>')
    form_heading = (f'<section class="page-heading"><div><span class="eyebrow">CONFIGURACIÓN</span><h1>{esc(title)}</h1><p>Los umbrales y el intervalo de Census se ajustan en las opciones de Camera Watchdog en Home Assistant. Los ajustes del detector anterior se conservan y no intervienen en los avisos actuales.</p></div></section>'
                    if title == 'Añadir cámara' or title.startswith('Editar ') else '')
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{esc(title)} · Camera Watchdog</title>
<link rel="stylesheet" href="{esc(prefix)}/static/watchdog.css?v=3.2.1-signal-loss1">
<script defer src="{esc(prefix)}/static/watchdog.js?v=3.2.1-signal-loss1"></script></head>
<body data-prefix="{esc(prefix)}"><a class="skip-link" href="#content">Saltar al contenido</a>
<header class="topbar"><div class="topbar-inner">
<a class="brand" href="{esc(prefix)}/"><span class="brand-mark">{icon('camera', 23)}</span>
<span>Camera <strong>Watchdog</strong><small>SUPERVISIÓN DE CÁMARAS</small></span></a>
<nav aria-label="Navegación principal"><a class="nav-link" href="{esc(prefix)}/">{icon('grid', 17)}<span>Cámaras</span></a><a class="nav-link nav-history" href="{esc(prefix)}/sample-history"{' aria-current="page"' if title == 'Histórico' else ''}>{icon('clock', 17)}<span>Histórico</span></a>{mode}</nav>
</div></header><main id="content">{notice}{form_heading}'''


def footer():
    return '<footer class="page-footer"><span>Camera Watchdog</span><span>Supervisión y registro de escenas</span></footer></main></body></html>'


def presentation(slug, state):
    result = state.get('census') or {}
    if state.get('signal_loss_enabled') and (state.get('signal_loss') or {}).get('confirmed'):
        return 'Posible sabotaje · pérdida de señal', 'danger alarm'
    if state.get('alarms_enabled') is not False and (state.get('changed') or result.get('observed_scene_loss')):
        return 'Posible sabotaje · cambio persistente', 'danger alarm'
    if state.get('offline') or result.get('status') == 'offline':
        if state.get('signal_loss_enabled') and (state.get('signal_loss') or {}).get('pending'):
            return 'Sin conexión · aviso en espera', 'warning'
        return 'Sin conexión', 'danger'
    if not state.get('last_ok'):
        return 'Esperando captura', 'neutral'
    if state.get('alarms_enabled') is False:
        if state.get('signal_loss_enabled'):
            return 'Solo aviso por pérdida de señal', 'neutral'
        return 'Avisos desactivados', 'neutral'
    if state.get('changed') or result.get('observed_scene_loss'):
        return 'Posible sabotaje · cambio persistente', 'danger alarm'
    return {
        'stable': ('Escena estable', 'good'),
        'scene_change': ('Cambio en evaluación', 'warning'),
        'inconclusive': ('Por revisar', 'warning'),
        'initialized': ('Referencia inicial', 'neutral'),
    }.get(result.get('status'), ('Pendiente de análisis', 'neutral'))


def percent(value):
    return '—' if value is None else f'{float(value) * 100:.1f} %'


def sabotage_status(camera, state, collection_only, references, publication, now=None):
    """Display readiness independently of capture/scene stability; never alter alarms."""
    now = time.time() if now is None else now
    result = state.get('census') or {}
    reason = None
    if collection_only:
        reason = 'Avisos globales desactivados'
    elif not camera.get('enabled', True):
        reason = 'Avisos Census desactivados en esta cámara'
    elif not any(references.get(kind) for kind in REFERENCE_KINDS):
        reason = 'Sin referencias guardadas'
    elif state.get('offline') or result.get('status') == 'offline':
        reason = 'Sin captura válida'
    elif not state.get('last_ok') or now - state['last_ok'] > 180:
        reason = 'Esperando una captura reciente'
    elif (result.get('comparison_mode') != 'day_night_references'
          or not state.get('alarm_ready')
          or not all((result.get('reference_comparisons') or {}).get(k, {}).get('reference_id')
                     for k in REFERENCE_KINDS if references.get(k))):
        reason = 'Referencias pendientes de validación'
    elif not result.get('reliable'):
        reason = 'Escena sin evaluación fiable en esta captura'
    elif publication.get('reason') in ('homeassistant_api_error', 'token_unavailable', 'alarm_state_error'):
        reason = 'No se pueden publicar avisos en Home Assistant'
    return {'operational': reason is None,
            'label': 'Sabotaje operativo' if reason is None else 'Sabotaje no operativo',
            'reason': reason or 'Referencias validadas y detección habilitada'}


def camera_card(camera, state, prefix, census_text=None, orb_text=None, show_actions=True):
    slug = camera['slug']
    key, url = esc(slug), esc(prefix + '/camera/' + quote(slug, safe=''))
    result = state.get('census') or {}
    label, tone = presentation(slug, state)
    sabotage = state.get('sabotage') or {'operational': False, 'label': 'Sabotaje no operativo', 'reason': 'Comprobando disponibilidad'}
    captured_at = state.get('last_capture_at', state.get('last_ok', 0))
    last = time.strftime('%H:%M:%S', time.localtime(captured_at)) if captured_at else '—'
    signal = state.get('signal_loss') or {}
    signal_text = ('Aviso por pérdida de señal desactivado' if not state.get('signal_loss_enabled') else
                   'Pérdida de señal confirmada' if signal.get('confirmed') else
                   f"Sin señal: {signal.get('elapsed_seconds', 0)} / {signal.get('timeout_seconds', 120)} s · {signal.get('failures', 0)} capturas fallidas" if signal.get('pending') else
                   'Aviso por pérdida de señal activado' if state.get('signal_loss_enabled') else
                   'Aviso por pérdida de señal desactivado')
    ref = reference_label(result.get('reference'))
    actions = (f'<div class="camera-actions"><a class="text-link" href="{url}">Ver cámara {icon("arrow",16)}</a><a class="icon-button" href="{url}/edit" aria-label="Configurar {esc(camera["name"])}" title="Configuración">{icon("settings",18)}</a></div>'
               if show_actions else '')
    return f'''<article class="camera-card" id="card-{key}">
<div class="camera-heading"><div><span class="eyebrow">SUPERVISIÓN DE ESCENA</span>
<h2><a href="{url}">{esc(camera['name'])}</a></h2></div>
<span class="badge {tone}" id="status-{key}"><span class="dot"></span>{label}</span></div>
<a class="camera-preview" href="{url}" aria-label="Abrir {esc(camera['name'])}">
<img class="cam" id="img-{key}" src="{esc(prefix)}/image/{esc(quote(slug, safe=''))}/current?ts={int(time.time())}" alt="Última captura de {esc(camera['name'])}">
<span class="image-placeholder">{icon('camera',32)}<span>Imagen no disponible</span></span>
<span class="image-caption">Última captura · <time id="last-{key}">{last}</time></span></a>
<div class="camera-body"><div class="sabotage-status {'good' if sabotage['operational'] else 'danger'}" id="sabotage-{key}" role="status"><strong><span id="sabotage-mark-{key}" aria-hidden="true">{'✓' if sabotage['operational'] else '✕'}</span> <span id="sabotage-label-{key}">{esc(sabotage['label'])}</span></strong><span id="sabotage-reason-{key}">{esc(sabotage['reason'])}</span></div><div class="camera-metrics">
<div><span>Cambio de escena</span><strong id="ratio-{key}">{percent(result.get('changed_block_ratio'))}</strong></div>
<div><span>Referencia</span><strong id="reference-{key}">{ref}</strong></div>
<div><span>Confirmaciones</span><strong id="confirm-{key}">{result.get('pending',0)}<small> / {result.get('required',2)}</small></strong></div></div>
<p class="camera-context">{'Comparación con las referencias de día y noche.' if result.get('comparison_mode') == 'day_night_references' else 'Comparación con la última escena estable.'}<br><span id="signal-{key}">{esc(signal_text)}</span></p>
<details class="diagnostic"><summary>Diagnóstico de la captura</summary><p id="census-{key}">{esc(census_text or label)}</p><p id="orb-{key}">{esc(orb_text or '')}</p></details>
{actions}</div></article>'''


def sample_history_controls(history, prefix):
    enabled = history.get('images_enabled', True)
    action = 'pause' if enabled else 'resume'
    button = 'Pausar guardado de muestras' if enabled else 'Reanudar guardado de muestras'
    status = 'Guardando muestras' if enabled else 'Guardado de muestras pausado'
    sizes = (f"{history.get('image_count', 0):,} imágenes · {history.get('image_bytes', 0) / 1048576:.1f} MiB"
             if history.get('available') else 'No se pudo consultar el almacenamiento')
    diagnostics = f"{history.get('diagnostic_bytes', 0) / 1048576:.1f} MiB de datos de diagnóstico"
    diagnostics_enabled = history.get('diagnostics_enabled', True)
    diagnostic_action = 'pause-diagnostics' if diagnostics_enabled else 'resume-diagnostics'
    diagnostic_button = 'Pausar diagnóstico' if diagnostics_enabled else 'Reanudar diagnóstico'
    diagnostic_state = 'Guardando diagnóstico' if diagnostics_enabled else 'Guardado de diagnóstico pausado'
    return f'''<section class="card sample-history" id="sample-history" aria-labelledby="sample-history-title">
<div class="section-heading"><h2 id="sample-history-title">Histórico de muestras</h2><span id="sample-history-state" role="status">{status}</span></div>
<p class="muted">Imágenes para analizar la evolución de las cámaras, retenidas hasta {history.get('retention_days', 7):g} días. Las capturas actuales, las referencias y la detección de sabotaje siguen activas al pausar o borrar este histórico.</p>
<p><strong id="sample-history-size">{sizes}</strong></p>
<div class="actions"><form id="sample-history-toggle" method="post" action="{esc(prefix)}/sample-history/{action}"><button class="btn2" id="sample-history-toggle-button">{button}</button></form>
<form method="post" action="{esc(prefix)}/sample-history/clear" data-confirm="¿Borrar todas las imágenes del histórico de muestras? No se pueden recuperar. Se conservan las referencias, las capturas actuales y los datos de diagnóstico. Si el guardado sigue activo, se recogerán nuevas muestras."><button class="danger">Borrar imágenes de muestras</button></form></div>
<div class="history-diagnostics"><div class="section-heading"><h3>Datos de diagnóstico</h3><span id="diagnostic-history-state" role="status">{diagnostic_state}</span></div>
<p><strong id="sample-history-diagnostics">{diagnostics}</strong></p>
<p class="muted">Puedes detener el archivo de métricas y los registros detallados de Census y ORB. La supervisión en directo y los avisos de sabotaje siguen funcionando.</p>
<div class="actions"><form id="diagnostic-history-toggle" method="post" action="{esc(prefix)}/sample-history/{diagnostic_action}"><button class="btn2" id="diagnostic-history-toggle-button">{diagnostic_button}</button></form>
<form method="post" action="{esc(prefix)}/sample-history/clear-diagnostics" data-confirm="¿Borrar los datos de diagnóstico del histórico? No se pueden recuperar. Se conservan las imágenes de muestras, las referencias y las capturas actuales. Si su guardado sigue activo, se recogerán nuevos datos."><button class="danger">Borrar datos de diagnóstico</button></form></div></div></section>'''


def history_page(history, prefix, collection_only):
    return header('Histórico', prefix, collection_only) + f'''
<section class="page-heading"><div><span class="eyebrow">ALMACENAMIENTO</span><h1>Histórico</h1><p>Gestiona las muestras y los datos de diagnóstico guardados.</p></div></section>
{sample_history_controls(history, prefix)}''' + footer()


def dashboard(cameras, states, prefix, collection_only, census_summary, orb_summary):
    online = sum(bool(states[c['slug']].get('last_ok')) and not states[c['slug']].get('offline') for c in cameras)
    stable = sum(presentation(c['slug'], states[c['slug']])[1] == 'good' for c in cameras)
    cards = []
    for camera in cameras:
        state = states[camera['slug']]
        card = camera_card(camera, state, prefix, census_summary(state.get('census')),
                           orb_summary(state.get('orb')))
        cards.append(card)
    empty = f'<section class="card empty-state">{icon("camera",40)}<h2>Tu primera cámara</h2><p>Añade una cámara para empezar a ver sus capturas y el estado de la escena.</p><a class="btn" href="{esc(prefix)}/camera/new">Añadir cámara</a></section>'
    return header('Cámaras', prefix, collection_only) + f'''
<section class="page-heading"><div><span class="eyebrow">PANEL DE SUPERVISIÓN</span><h1>Supervisión de cámaras</h1><p>Capturas recientes y evolución de cada escena.</p></div>
<a class="btn" href="{esc(prefix)}/camera/new">{icon('plus',17)} Añadir cámara</a></section>
<section class="overview" aria-label="Resumen"><div class="overview-item"><span>Cámaras configuradas</span><strong id="total-cameras">{len(cameras):02d}</strong></div>
<div class="overview-item"><span>Con captura disponible</span><strong id="online-cameras">{online:02d}</strong></div>
<div class="overview-item"><span>Escenas fijas estables</span><strong id="stable-cameras">{stable:02d}</strong></div>
<div class="overview-status"><span class="dot"></span><span id="sync-status" role="status">Actualización automática · cada 10 s</span></div></section>
<section class="section-heading"><h2>Cámaras</h2><span>Últimas imágenes recibidas</span></section>
<div class="camera-grid" data-dashboard>{''.join(cards) if cards else empty}</div>''' + footer()


def detail(camera, state, prefix, collection_only, diagnostics, refs, census_refs):
    slug = camera['slug']
    url = esc(prefix + '/camera/' + quote(slug, safe=''))
    def image(kind, title):
        return f'<img class="cam" src="{esc(prefix)}/image/{esc(quote(slug, safe=""))}/{kind}?ts={int(time.time())}" alt="{esc(title)}">'
    def post(action, text, cls='', confirm=None):
        attr = f' data-confirm="{esc(confirm)}"' if confirm else ''
        return f'<form method="post" action="{url}/{action}"{attr}><button class="{cls}">{text}</button></form>'
    reference_groups = ''
    for group, kinds in REFERENCE_GROUPS.items():
        title = 'Día' if group == 'day' else 'Noche'
        present = [kind for kind in kinds if refs.get(kind)]
        cards = ''
        for kind in present:
            name = reference_label(kind)
            controls = post('reference/' + kind, 'Actualizar con captura actual', 'btn2',
                            '¿Actualizar ' + name.lower() + ' con la captura actual? Comprueba la iluminación y el encuadre.')
            controls += post('reference/' + kind + '/delete', 'Borrar imagen', 'danger',
                             '¿Borrar ' + name.lower() + '? Se conservarán y compararán las demás imágenes guardadas.')
            cards += f'<div class="reference-card"><h3>{icon("sun" if group == "day" else "moon",18)}{name}</h3>{image(kind, name)}<div class="actions">{controls}</div></div>'
        add = (post('reference-group/' + group + '/add', 'Añadir captura de ' + title.lower(), 'btn2',
                    '¿Guardar la captura actual como referencia de ' + title.lower() + '? Comprueba que el encuadre es el correcto.')
               if len(present) < 5 else '<p class="muted">Máximo de cinco imágenes guardadas.</p>')
        reference_groups += f'<section class="reference-group" aria-label="Referencias de {title.lower()}"><div class="section-heading"><h3>{title}</h3><span>{len(present)} / 5 imágenes</span></div><div class="reference-grid">{cards}</div>{"<p class=muted>Sin imágenes guardadas en este grupo.</p>" if not present else ""}<div class="actions">{add}</div></section>'
    legacy = ''
    for kind, name in [('fixed','Fija')]:
        body = image(kind, 'Referencia guardada: ' + name) if refs[kind] else '<p class="muted">No guardada</p>'
        controls = post('reference/' + kind, 'Actualizar archivo del detector anterior' if refs[kind] else 'Guardar para el detector anterior', 'btn2')
        if refs[kind]:
            controls += post('reference/' + kind + '/delete', 'Borrar', 'danger', '¿Borrar esta referencia guardada?')
        legacy += f'<div class="reference-card"><h3>{name}</h3>{body}<div class="actions">{controls}</div></div>'
    current_card = camera_card(camera, state, prefix, show_actions=False)
    # Detail page has a larger image, with the same meaningful status metrics.
    return header(camera['name'], prefix, collection_only) + f'''
<a class="back-link" href="{esc(prefix)}/">← Todas las cámaras</a>
<section class="page-heading"><div><span class="eyebrow">DETALLE DE CÁMARA</span><h1>{esc(camera['name'])}</h1><p>Estado de la escena y referencias de comparación.</p></div><a class="btn btn2" href="{url}/edit">{icon('settings',17)} Configuración</a></section>
<div class="detail-layout"><section>{current_card}</section><aside class="detail-sidebar">
<section class="card"><span class="eyebrow">CONTROL DE CAPTURA</span><h2>Una imagen más reciente</h2><p>Solicita una captura para comprobar el encuadre actual.</p>{post('capture-now','Capturar ahora')}<p class="muted">La recogida periódica continúa en el servidor.</p></section>
<section class="card"><h3>Calidad de imagen</h3><dl class="quality">{''.join(f'<div><dt>{name}</dt><dd>{float(state.get(key,0)):.1f}</dd></div>' for name,key in [('Brillo','brightness'),('Contraste','contrast'),('Detalle','detail')])}</dl></section></aside></div>
<section class="card"><div class="section-heading"><h2>Referencias de la cámara</h2><span>Hasta 5 de día y 5 de noche</span></div><p class="muted">Ambos detectores comparten las imágenes guardadas. Se comparan todas las disponibles, aunque solo haya una. Reconocer cualquiera permite recuperar el estado normal; confirmar sabotaje requiere cambio frente a todas.</p>{reference_groups}</section>
<details class="card advanced"><summary>Diagnóstico detallado</summary><div class="diagnostic-grid">{diagnostics}</div></details>
<details class="card advanced"><summary>Controles del diagnóstico anterior</summary><p class="muted">Referencia fija opcional y control del seguimiento anterior. Las imágenes de día y noche son las compartidas que aparecen arriba.</p><div class="reference-grid legacy-references">{legacy}</div><div class="legacy-controls">{post('accept','Aceptar imagen actual como normal','btn2','¿Aceptar la imagen para el seguimiento anterior? Las referencias de día y noche se mantienen.')}</div></details>
<details class="card advanced danger-zone"><summary>Administrar cámara</summary><p>Eliminar la cámara detendrá su seguimiento.</p>{post('delete','Eliminar cámara','danger','¿Eliminar esta cámara y su configuración?')}</details>''' + footer()












