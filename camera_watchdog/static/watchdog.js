(() => {
  'use strict';
  const prefix = document.body.dataset.prefix || '';
  const setText = (id, text) => { const node = document.getElementById(id); if (node) node.textContent = text; };
  const percent = value => value == null ? '—' : (value * 100).toFixed(1) + ' %';
  const referenceLabel = key => {
    if (key === 'day') return 'Día 1';
    if (key === 'night') return 'Noche 1';
    if (key === 'night_alt') return 'Noche 2';
    const match = /^(day|night)_([2-5])$/.exec(key || '');
    return match ? (match[1] === 'day' ? 'Día ' : 'Noche ') + match[2] : 'Escena anterior';
  };
  const formatClock = seconds => new Date(seconds * 1000).toLocaleTimeString('es-ES', {hour12: false});
  document.addEventListener('submit', event => {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
  document.querySelectorAll('.camera-preview img').forEach(img => {
    const toggle = () => img.parentElement.classList.toggle('image-error', !img.naturalWidth);
    img.addEventListener('error', toggle); img.addEventListener('load', toggle);
    if (img.complete) toggle();
  });
  document.querySelectorAll('label').forEach((label, index) => {
    const input = label.nextElementSibling;
    if (!label.htmlFor && input && input.tagName === 'INPUT') {
      if (!input.id) input.id = 'field-' + index;
      label.htmlFor = input.id;
    }
  });
  if (!document.querySelector('[data-dashboard], .camera-card, #publication-status, #sample-history')) return;
  let busy = false;
  async function refreshStatus() {
    if (busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(prefix + '/api/status', {cache:'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('status');
      const data = await response.json();
      const history = data.sample_history;
      if (history) {
        setText('sample-history-state', history.images_enabled ? 'Guardando muestras' : 'Guardado de muestras pausado');
        setText('sample-history-size', history.available
          ? Number(history.image_count).toLocaleString('es-ES') + ' imágenes · ' + (history.image_bytes / 1048576).toFixed(1) + ' MiB'
          : 'No se pudo consultar el almacenamiento');
        setText('sample-history-diagnostics', history.available ? (history.diagnostic_bytes / 1048576).toFixed(1) + ' MiB de datos de diagnóstico' : 'No se pudo consultar el almacenamiento');
        setText('diagnostic-history-state', history.diagnostics_enabled ? 'Guardando diagnóstico' : 'Guardado de diagnóstico pausado');
        const diagnosticToggle = document.getElementById('diagnostic-history-toggle');
        if (diagnosticToggle) diagnosticToggle.action = prefix + '/sample-history/' + (history.diagnostics_enabled ? 'pause-diagnostics' : 'resume-diagnostics');
        setText('diagnostic-history-toggle-button', history.diagnostics_enabled ? 'Pausar diagnóstico' : 'Reanudar diagnóstico');
        const toggle = document.getElementById('sample-history-toggle');
        if (toggle) toggle.action = prefix + '/sample-history/' + (history.images_enabled ? 'pause' : 'resume');
        setText('sample-history-toggle-button', history.images_enabled ? 'Pausar guardado de muestras' : 'Reanudar guardado de muestras');
      }
      const publication = data.alarm_publication || {};
      const pubMessage = publication.reason === 'awaiting_reference_validation'
        ? 'Esperando validar las referencias antes de publicar.'
        : ['homeassistant_api_error','token_unavailable','alarm_state_error'].includes(publication.reason)
        ? 'No se pueden publicar avisos en Home Assistant. Revisa la conexión y los permisos.'
        : publication.state === 'on' ? 'Home Assistant conectado · aviso de cambio activo.'
        : publication.state === 'off' ? 'Home Assistant conectado · sensor en reposo.'
        : 'Publicación en Home Assistant pendiente.';
      setText('publication-status', pubMessage);
      let online = 0, stable = 0;
      for (const camera of data.cameras) {
        const slug = camera.slug, census = camera.census || {};
        const sabotage = camera.sabotage || {operational:false,label:'Sabotaje no operativo',reason:'Comprobando disponibilidad'};
        const protection = document.getElementById('sabotage-' + slug);
        if (protection) protection.className = 'sabotage-status ' + (sabotage.operational ? 'good' : 'danger');
        setText('sabotage-mark-' + slug, sabotage.operational ? '✓' : '✕');
        setText('sabotage-label-' + slug, sabotage.label);
        setText('sabotage-reason-' + slug, sabotage.reason);
        const hasCapture = camera.last_capture !== '—';
        const offline = census.status === 'offline' || camera.status === 'Sin imagen';
        if (hasCapture && !offline) online++;
        let label = 'Pendiente de análisis', tone = 'neutral';
        if (offline) { label = 'Sin conexión'; tone = 'danger'; }
        else if (!hasCapture) label = 'Esperando captura';
        else if (camera.alarms_enabled === false) label = 'Avisos desactivados';
        else if (camera.changed || census.observed_scene_loss) { label = 'Posible sabotaje · cambio persistente'; tone = 'danger alarm'; }
        else if (census.status === 'stable') { label = 'Escena estable'; tone = 'good'; stable++; }
        else if (census.status === 'scene_change') { label = 'Cambio en evaluación'; tone = 'warning'; }
        else if (census.status === 'inconclusive') { label = 'Por revisar'; tone = 'warning'; }
        else if (census.status === 'initialized') label = 'Referencia inicial';
        const badge = document.getElementById('status-' + slug);
        if (badge) { badge.className = 'badge ' + tone; badge.replaceChildren(); const dot = document.createElement('span'); dot.className = 'dot'; badge.append(dot, document.createTextNode(label)); }
        setText('last-' + slug, camera.last_capture);
        setText('ratio-' + slug, percent(census.changed_block_ratio));
        setText('reference-' + slug, referenceLabel(census.reference));
        setText('confirm-' + slug, (census.pending || 0) + ' / ' + (census.required || 2));
        setText('census-' + slug, camera.census_summary);
        setText('orb-' + slug, camera.orb_summary);
        const img = document.getElementById('img-' + slug);
        if (img && hasCapture) img.src = prefix + '/image/' + encodeURIComponent(slug) + '/current?ts=' + data.timestamp;
      }
      setText('total-cameras', String(data.cameras.length).padStart(2,'0'));
      setText('online-cameras', String(online).padStart(2,'0'));
      setText('stable-cameras', String(stable).padStart(2,'0'));
      setText('sync-status', 'Actualizado ' + formatClock(data.timestamp));
    } catch (_) {
      document.querySelectorAll('.sabotage-status').forEach(node => {
        const slug = node.id.slice('sabotage-'.length);
        node.className = 'sabotage-status danger';
        setText('sabotage-mark-' + slug, '✕');
        setText('sabotage-label-' + slug, 'Estado de sabotaje sin verificar');
        setText('sabotage-reason-' + slug, 'Sin conexión con la supervisión; reintentando');
      });
      setText('sync-status', 'Sin actualizar · reintentando conexión');
    } finally { clearTimeout(timeout); busy = false; }
  }
  setInterval(refreshStatus, 10000);
  refreshStatus();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
})();
