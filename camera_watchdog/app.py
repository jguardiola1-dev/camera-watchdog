#!/usr/bin/env python3

import html
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error

from io import BytesIO
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from flask import Flask, Response, redirect, request
import web_ui
from reference_catalog import REFERENCE_KINDS, REFERENCE_FILES, REFERENCE_GROUPS, saved_reference_paths
from alarm_policy import AlarmLatches
from PIL import Image, ImageChops, ImageFilter, ImageStat
from observation_store import ObservationStore

try:
    import cv2
    from census_detector import SceneObserver, ReferenceSceneObserver, CensusSettings
except ImportError:
    SceneObserver = None

census_observers = {}
census_pending_evidence = {}
census_lock = threading.RLock()

try:
    from orb_detector import OrbObserver, Settings, empty_result
except ImportError:
    OrbObserver = None

orb_observer = OrbObserver() if OrbObserver is not None else None
# Publishing remains opt-in, including when upgrading from saved 3.0 options.
alarm_latches = None
alarm_latches_lock = threading.RLock()
publication_lock = threading.RLock()
last_ha_publication = {}
last_ha_signature = None
last_ha_time = 0
observation_store = None
observation_store_lock = threading.RLock()


DATA_DIR = Path("/data")
LOG_FILE = DATA_DIR / "watchdog.log"
CAMERAS_FILE = DATA_DIR / "cameras.json"
OPTIONS_FILE = DATA_DIR / "options.json"
IMAGES_DIR = DATA_DIR / "images"

IMAGES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CAMERA = {
    "name": "",
    "slug": "",
    "enabled": True,
    "rtsp_url": "",
    "username": "",
    "password": "",
    "interval": 10,
    "threshold": 45.0,
    "canny_threshold": 4.0,
    "confirmations": 2,
    "capture_timeout": 12,
}

runtime = {}
workers = {}

runtime_lock = threading.RLock()
config_lock = threading.RLock()
workers_lock = threading.Lock()

stop_event = threading.Event()
camera_operations = {}
camera_operations_lock = threading.Lock()
watchdog_log_lock = threading.Lock()
WATCHDOG_LOG_MAX_BYTES = 2 * 1024 * 1024
WATCHDOG_LOG_BACKUPS = 3

app = Flask(__name__)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    os.replace(tmp, path)


def load_options():
    return load_json(OPTIONS_FILE, {})


def collection_only():
    return load_options().get("census_alarms_enabled", False) is not True


def get_alarm_latches():
    global alarm_latches
    with alarm_latches_lock:
        if alarm_latches is None:
            alarm_latches = AlarmLatches(DATA_DIR / 'census_alarm_state.json')
        return alarm_latches

def write_watchdog_log(line):
    try:
        # One writer/rotation at a time. No full-file reads or line rewrites.
        with watchdog_log_lock:
            handler = RotatingFileHandler(LOG_FILE, maxBytes=WATCHDOG_LOG_MAX_BYTES,
                                          backupCount=WATCHDOG_LOG_BACKUPS,
                                          encoding="utf-8", delay=True)
            try:
                handler.emit(logging.LogRecord("camera_watchdog", logging.INFO,
                                               "", 0, str(line), (), None))
            finally:
                handler.close()

    except Exception as error:
        print(
            f"ERROR escribiendo watchdog.log: {error}"
        )

def load_cameras():
    with config_lock:
        data = load_json(CAMERAS_FILE, [])

        if isinstance(data, list):
            return data

        return []


def save_cameras(cameras):
    with config_lock:
        atomic_write_json(CAMERAS_FILE, cameras)


def slugify(value):
    value = (value or "").strip().lower()

    value = re.sub(
        r"[^a-z0-9_-]+",
        "_",
        value,
    )

    value = re.sub(
        r"_+",
        "_",
        value,
    ).strip("_")

    if not value:
        value = f"camara_{int(time.time())}"

    return value


def camera_dir(slug):
    path = IMAGES_DIR / slug

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def image_path(slug, kind):
    names = {
        "current": "current.jpg",
        "previous": "previous.jpg",
        "day": "reference_day.jpg",
        "night": "reference_night.jpg",
        "night_alt": "reference_night_alt.jpg",
        "fixed": "reference_fixed.jpg",
        **REFERENCE_FILES,
    }

    return camera_dir(slug) / names[kind]


def build_rtsp(camera):
    url = (
        camera.get("rtsp_url")
        or ""
    ).strip()

    if not url:
        raise ValueError(
            "Falta la URL RTSP"
        )

    parts = urlsplit(url)

    if "@" in parts.netloc:
        return url

    user = (
        camera.get("username")
        or ""
    )

    password = (
        camera.get("password")
        or ""
    )

    if not user:
        return url

    host = parts.hostname or ""

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = (
        f":{parts.port}"
        if parts.port
        else ""
    )

    netloc = (
        f"{quote(user, safe='')}:"
        f"{quote(password, safe='')}@"
        f"{host}{port}"
    )

    return urlunsplit(
        (
            parts.scheme,
            netloc,
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def camera_operation_lock(slug):
    with camera_operations_lock:
        return camera_operations.setdefault(slug, threading.RLock())


def serialized_camera_operation(function):
    @wraps(function)
    def wrapped(camera, *args, **kwargs):
        with camera_operation_lock(camera['slug']):
            return function(camera, *args, **kwargs)
    return wrapped


@serialized_camera_operation
def capture_frame(camera, dest):
    tmp = dest.with_suffix(
        ".tmp.jpg"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        build_rtsp(camera),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-q:v",
        "4",
        "-y",
        str(tmp),
    ]

    try:
        process = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=(
                int(
                    camera.get(
                        "capture_timeout",
                        12,
                    )
                )
                + 5
            ),
        )

        if process.returncode != 0:
            message = process.stderr.decode(
                "utf-8",
                "ignore",
            ).strip()

            raise RuntimeError(
                message
                or "Error FFmpeg"
            )

        os.replace(
            tmp,
            dest,
        )
        set_rt(camera['slug'], last_capture_at=time.time())

    finally:
        if tmp.exists():
            tmp.unlink(
                missing_ok=True
            )


def load_small(
    path,
    size=(160, 90),
):
    raw = Path(path).read_bytes()

    image = Image.open(
        BytesIO(raw)
    )

    image.load()

    return image.convert(
        "L"
    ).resize(size)


def direct_difference(
    image_a,
    image_b,
):
    difference = ImageChops.difference(
        load_small(image_a),
        load_small(image_b),
    )

    stat = ImageStat.Stat(
        difference
    )

    return (
        stat.mean[0]
        / 255.0
        * 100.0
    )

def canny_difference(image_a, image_b):
    edge_a = Path(str(image_a) + ".edge.png")
    edge_b = Path(str(image_b) + ".edge.png")

    try:
        command_a = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(image_a),
            "-vf",
            "scale=160:90,edgedetect=low=0.1:high=0.4",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-y",
            str(edge_a),
        ]

        command_b = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(image_b),
            "-vf",
            "scale=160:90,edgedetect=low=0.1:high=0.4",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-y",
            str(edge_b),
        ]

        subprocess.run(
            command_a,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )

        subprocess.run(
            command_b,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )

        img_a = load_small(edge_a)
        img_b = load_small(edge_b)

        diff = ImageChops.difference(
            img_a,
            img_b,
        )

        stat = ImageStat.Stat(diff)

        return (
            stat.mean[0]
            / 255.0
            * 100.0
        )

    finally:
        edge_a.unlink(missing_ok=True)
        edge_b.unlink(missing_ok=True)



def image_stats(path):
    image = load_small(path)

    stat = ImageStat.Stat(
        image
    )

    edges = image.filter(
        ImageFilter.FIND_EDGES
    )

    edge_stat = ImageStat.Stat(
        edges
    )

    return (
        stat.mean[0],
        stat.stddev[0],
        edge_stat.mean[0],
    )


def covered_from_stats(
    brightness,
    contrast,
    detail,
):
    return (
        brightness < 18
        or brightness > 238
        or contrast < 10
        or detail < 6
    )


def get_rt(slug):
    with runtime_lock:
        return dict(
            runtime.get(
                slug,
                {},
            )
        )


def set_rt(
    slug,
    **values,
):
    with runtime_lock:
        runtime.setdefault(
            slug,
            {},
        ).update(values)


def observe_geometry(camera, current):
    """Diagnostics only. Never mutate the existing alarm or RTSP configuration."""
    slug = camera["slug"]
    options = load_options()
    if not options.get("orb_observation", True):
        if orb_observer is not None:
            orb_observer.reset(slug)
        result = {"mode": "observation", "status": "disabled",
                  "reason": "observation_disabled", "observed_movement": False,
                  "pending": 0, "required": 0}
    elif orb_observer is None:
        result = {"mode": "observation", "status": "unavailable",
                  "reason": "opencv_unavailable", "observed_movement": False,
                  "pending": 0, "required": 0}
    else:
        try:
            settings = Settings.from_options(options)
            references = {'fixed': image_path(slug, 'fixed'),
                          **saved_reference_paths(IMAGES_DIR / slug)}
            result = orb_observer.observe(slug, current, references, settings)
        except Exception:
            # Do not let a failed diagnostic become a legacy offline alarm.
            # Exception messages may contain paths or credentials: do not log them.
            orb_observer.reset(slug)
            result = empty_result("diagnostic_error")
    result["checked_at"] = time.time()
    set_rt(slug, orb=result)
    if diagnostic_history_enabled() and options.get("detailed_log", True) and options.get("orb_observation", True):
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(slug))
        line = (f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] "
                f"[{safe_slug}] ORB_OBSERVACION "
                + json.dumps(result, ensure_ascii=False, allow_nan=False))
        # Logging is part of observation and must not affect alarm processing.
        try:
            print(line)
            write_watchdog_log(line)
        except Exception:
            pass
    return result


def get_observation_store():
    global observation_store
    with observation_store_lock:
        if observation_store is None:
            options = load_options()
            observation_store = ObservationStore(
                DATA_DIR / 'observations',
                max_bytes=int(options.get('observation_max_mb', 512)) * 1024 * 1024,
                retention_days=int(options.get('observation_retention_days', 7)),
                snapshot_seconds=int(options.get('observation_snapshot_seconds', 60)))
        return observation_store


def sample_history_enabled():
    return load_json(DATA_DIR / 'sample_history.json', {}).get('images_enabled', True) is True


def diagnostic_history_enabled():
    return load_json(DATA_DIR / 'sample_history.json', {}).get('diagnostics_enabled', True) is True


def sample_history_status():
    try:
        with observation_store_lock:
            return dict(get_observation_store().stats(), images_enabled=sample_history_enabled(),
                        diagnostics_enabled=diagnostic_history_enabled(), available=True)
    except (OSError, ValueError):
        return dict(available=False, images_enabled=sample_history_enabled(),
                    diagnostics_enabled=diagnostic_history_enabled())


def record_observation(camera, current=None, **metrics):
    slug = camera["slug"]
    try:
        with observation_store_lock:
            store = get_observation_store()
            with census_lock:
                evidence = census_pending_evidence.pop(slug, {})
            storage = store.record(slug, metrics, current, extra_images=evidence,
                                   images_enabled=sample_history_enabled(),
                                   diagnostics_enabled=diagnostic_history_enabled())
        set_rt(slug, observation_storage=storage)
    except Exception:
        set_rt(slug, observation_storage={"recorded": False, "reason": "storage_error"})
        print(f"[{re.sub(r'[^a-zA-Z0-9_-]', '_', str(slug))}] No se pudo guardar la observación")


def observe_census(camera, current):
    slug = camera["slug"]
    options = load_options()
    now = time.time()
    result = {"status": "inconclusive", "reason": "diagnostic_error", "pending": 0,
              "required": 0, "observed_scene_loss": False, "alarms_published": False}
    if not options.get("census_observation", True):
        with census_lock:
            census_observers.pop(slug, None)
            census_pending_evidence.pop(slug, None)
        result.update(status="disabled", reason="observation_disabled")
    elif SceneObserver is None:
        result.update(status="unavailable", reason="opencv_unavailable")
    else:
        try:
            settings = CensusSettings.from_options(options)
            frame = cv2.imread(str(current), cv2.IMREAD_GRAYSCALE)
            if frame is None:
                raise ValueError("Invalid image")
            capture_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(slug)) + "-" + str(int(now * 1000000))
            # Census and ORB share the same manually selected day/night files.
            # Never learn replacements from periodic captures.
            paths = saved_reference_paths(IMAGES_DIR / slug)
            use_references = bool(paths)
            references = {}
            if use_references:
                for kind, path in paths.items():
                    if path.is_file():
                        content = path.read_bytes()
                        reference = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                        references[kind] = (kind + "-" + hashlib.sha256(content).hexdigest(), reference)
            with census_lock:
                observer = census_observers.get(slug)
                observer_type = ReferenceSceneObserver if use_references else SceneObserver
                if observer is None or type(observer) is not observer_type:
                    observer = observer_type(settings)
                    census_observers[slug] = observer
                elif observer.settings != settings:
                    # Keep the pre-change image; changing options cannot approve
                    # a moved camera. Only the diagnostic confirmation is reset.
                    observer.settings = settings
                    observer.pending = 0
                result = (observer.observe(frame, capture_id, now, references) if use_references
                          else observer.observe(frame, capture_id, now))
                if (result.get("scene_change") and result.get("first_changed_id") == capture_id
                        and observer.anchor is not None):
                    before_ok, before = cv2.imencode(".jpg", observer.anchor)
                    after_ok, after = cv2.imencode(".jpg", frame)
                    if before_ok and after_ok:
                        census_pending_evidence[slug] = {"census_before": before.tobytes(),
                                                         "census_first_change": after.tobytes()}
                        result["evidence_requested"] = True
        except Exception:
            with census_lock:
                if slug in census_observers:
                    census_observers[slug].pending = 0
    result.update(mode="observation", checked_at=now,
                  camera_role="fixed_camera",
                  alarms_published=False)
    set_rt(slug, census=result)
    if diagnostic_history_enabled() and options.get("detailed_log", True) and options.get("census_observation", True):
        safe_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(slug))
        compact = {k: v for k, v in result.items() if k != "blocks"}
        try:
            line = (f"[{time.strftime('%d/%m/%Y %H:%M:%S')}] [{safe_slug}] "
                    "CENSUS_OBSERVACION " + json.dumps(compact, ensure_ascii=False, allow_nan=False))
            print(line)
            write_watchdog_log(line)
        except Exception:
            pass
    return result


def census_summary(result):
    if not result:
        return "Census: pendiente de captura"
    labels = {"initialized": "escena inicial de prueba", "stable": "escena estable",
              "scene_change": "cambio de escena", "inconclusive": "no concluyente",
              "offline": "sin captura válida", "disabled": "desactivado", "unavailable": "no disponible"}
    text = labels.get(result.get("status"), "no concluyente")
    ratio = result.get("changed_block_ratio")
    if ratio is not None:
        text += f" · {ratio * 100:.1f}% de bloques cambiados"
    if result.get("observed_scene_loss"):
        text += " · persistente"
    suffix = "(sin alarmas)" if collection_only() else "(diagnóstico Census)"
    return f"Census: {text} · {result.get('pending', 0)}/{result.get('required', 0)} {suffix}"


def census_details(result):
    result = result or {}
    reasons = {"initial_scene_not_validated": "Primera escena con textura suficiente; no valida la posición física",
               "structure_preserved": "La estructura coincide con la última escena estable",
               "widespread_structure_change": "Cambio extendido de estructura; se conserva la escena anterior",
               "partial_structure_change": "Cambio parcial; se conserva la escena anterior",
               "insufficient_texture": "Textura insuficiente: oscuridad, obstrucción u otra causa",
               "insufficient_spatial_texture": "No hay suficientes bloques con textura",
               "diagnostic_error": "No se pudo completar el diagnóstico",
               "no_valid_capture": "No hay captura válida", "observation_disabled": "Diagnóstico desactivado",
               "opencv_unavailable": "OpenCV no está disponible"}
    reasons.update(saved_reference_match="La escena coincide con una referencia de día o noche",
                   both_references_changed="Cambio extendido respecto a ambas referencias",
                   all_references_changed="Cambio extendido respecto a todas las referencias",
                   no_reference_match="Ninguna referencia confirma la escena; resultado no concluyente")
    reason = html.escape(reasons.get(result.get("reason"), "Pendiente de evaluación"))
    role = "Supervisión de escena fija"
    comparison = ("Comparación con referencias de día y noche, sin actualizarlas automáticamente"
                  if result.get("comparison_mode") == "day_night_references"
                  else "Comparación con la escena anterior, sin modificar referencias guardadas")
    return (f'<div class="card"><h3>Diagnóstico Census</h3><p>{html.escape(census_summary(result))}</p>'
            f'<p>{role}. {comparison}.</p>'
            f'<p><b>Bloques válidos:</b> {int(result.get("valid_blocks", 0))} · '
            f'<b>Bloques cambiados:</b> {int(result.get("changed_blocks", 0))}</p>'
            f'<p>{reason}</p></div>')


def safe_capture_error(error):
    return re.sub(r"rtsp://[^\s'\"\]]+", "[RTSP oculto]", str(error), flags=re.I)[:500]


def reset_geometry(slug, status, reason):
    if orb_observer is not None:
        orb_observer.reset(slug)
    set_rt(slug, orb={"mode": "observation", "status": status, "reason": reason,
                      "observed_movement": False, "pending": 0, "required": 0,
                      "checked_at": time.time()})
    with census_lock:
        census_pending_evidence.pop(slug, None)
        if slug in census_observers:
            census_observers[slug].pending = 0
    set_rt(slug, census={"mode": "observation", "status": status, "reason": reason,
                         "observed_scene_loss": False, "pending": 0, "required": 0,
                         "alarms_published": False, "checked_at": time.time()})


def orb_summary(result):
    if not result:
        return "ORB: pendiente de captura (solo observación)"
    labels = {"stable": "geometría estable", "movement": "movimiento geométrico",
              "inconclusive": "no concluyente", "unavailable": "no disponible",
              "disabled": "desactivado", "offline": "sin captura válida"}
    label = labels.get(result.get("status"), "no concluyente")
    if result.get("observed_movement"):
        label += " persistente"
    return (f"ORB: {label} · {result.get('pending', 0)}/{result.get('required', 0)} "
            "(solo observación)")


def orb_details(result):
    result = result or {}
    summary = html.escape(orb_summary(result))
    metrics = []
    for label, key in (("Referencia", "reference"), ("Puntos referencia", "keypoints_reference"),
                       ("Puntos captura", "keypoints_current"), ("Coincidencias", "matches"),
                       ("Inliers", "inliers"), ("Proporción inliers", "inlier_ratio"),
                       ("Cobertura", "coverage"), ("Celdas", "grid_cells"),
                       ("Error reproyección (px)", "reprojection_error"),
                       ("Desplazamiento X (%)", "dx_percent"),
                       ("Desplazamiento Y (%)", "dy_percent"),
                       ("Rotación en imagen (°)", "rotation"), ("Escala", "scale")):
        value = result.get(key)
        metrics.append(f"<b>{label}:</b> {html.escape(str(value) if value is not None else '—')}")
    reasons = {
        "pending_capture": "Pendiente de captura",
        "geometry_stable": "La geometría coincide con las referencias",
        "geometric_movement": "Desplazamiento, rotación o escala por encima del umbral de prueba",
        "insufficient_features": "No hay suficientes puntos característicos",
        "insufficient_matches": "No hay suficientes correspondencias fiables",
        "insufficient_inliers": "Las correspondencias no apoyan una transformación fiable",
        "localized_features": "Los puntos están concentrados en una zona de la imagen",
        "low_texture": "Pérdida de textura: posible obstrucción o iluminación insuficiente",
        "references_disagree": "Las referencias guardadas dan resultados contradictorios; revisar su orientación",
        "no_saved_reference": "Guardar una referencia con la cámara en su orientación correcta",
        "opencv_unavailable": "OpenCV no está disponible",
        "diagnostic_error": "No se pudo completar el diagnóstico",
        "observation_disabled": "Diagnóstico desactivado",
        "camera_disabled": "Seguimiento de la cámara desactivado",
        "no_valid_capture": "No hay captura válida disponible",
        "invalid_image": "No se pudo leer una imagen",
        "aspect_ratio_changed": "Ha cambiado la proporción de la imagen",
        "no_geometric_model": "No se pudo estimar una transformación",
        "reprojection_error": "La transformación no explica las correspondencias con suficiente precisión",
        "implausible_scale": "El cambio de escala no es fiable",
    }
    reason = html.escape(reasons.get(result.get("reason", "pending_capture"), "No concluyente"))
    resolution = html.escape(str(result.get("processed_width", "—"))) + " × " + html.escape(str(result.get("processed_height", "—")))
    return (f'<div class="card"><h3>Diagnóstico geométrico</h3><p>{summary}</p>'
            '<p>Estos resultados no activan el sensor de sabotaje. '
            'Los avisos usan Census; el diagnóstico geométrico es informativo. Canny no se ejecuta.</p>'
            f'<p>{" · ".join(metrics)}</p><p>Resolución analizada: {resolution}. '
            'La rotación corresponde a la imagen, no a una orientación 3D calibrada.</p>'
            f'<p><b>Diagnóstico:</b> {reason}</p></div>')

def update_sabotaje_cam():
    global last_ha_signature, last_ha_time, last_ha_publication
    with publication_lock:
        if collection_only():
            last_ha_signature = None
            return {"published": False, "reason": "observation_only"}
        try:
            configured = json.loads(CAMERAS_FILE.read_text(encoding="utf-8"))
            if not isinstance(configured, list) or any(not isinstance(c, dict) or not c.get('slug') or not c.get('name') for c in configured):
                raise ValueError('Invalid camera configuration')
            latches = get_alarm_latches()
            enabled = [c for c in configured if c.get("enabled", True)]
            # A camera awaiting its initial references cannot block recovery of
            # another camera. Existing confirmed alarms must remain included,
            # even if their references are later removed.
            cameras = [c for c in enabled if latches.get(c['slug'])
                       or saved_reference_paths(IMAGES_DIR / c['slug'])]
            pending = [c['name'] for c in enabled if c not in cameras]
            alarm_cameras = [c["name"] for c in cameras if latches.get(c["slug"])]
        except Exception:
            last_ha_publication = {"published": False, "reason": "alarm_state_error"}
            return dict(last_ha_publication)
        if not alarm_cameras and any(not get_rt(c["slug"]).get("alarm_ready") for c in cameras):
            last_ha_publication = {"published": False, "reason": "awaiting_reference_validation"}
            return dict(last_ha_publication)
        payload = {"state": "on" if alarm_cameras else "off", "attributes": {
            "friendly_name": "SABOTAJE CAM", "device_class": "problem",
            "camaras_en_sabotaje": alarm_cameras, "numero_camaras": len(alarm_cameras),
            "ultima_camara": alarm_cameras[-1] if alarm_cameras else "",
            "motivo": "CAMBIO DE ESCENA CENSUS" if alarm_cameras else "ESCENA ESTABLE",
            "algoritmo": "census_day_night", "camaras_habilitadas": [c["name"] for c in cameras],
            "camaras_pendientes_referencias": pending}}
        signature = json.dumps(payload, sort_keys=True)
        if signature == last_ha_signature and time.monotonic() - last_ha_time < 300:
            return {"published": False, "reason": "unchanged", "state": payload["state"]}
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            last_ha_publication = {"published": False, "reason": "token_unavailable"}
            return dict(last_ha_publication)
        payload["attributes"]["ultima_actualizacion"] = time.strftime("%d/%m/%Y %H:%M:%S")
        req = urllib.request.Request("http://supervisor/core/api/states/binary_sensor.sabotaje_cam",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("Publication rejected")
                response.read()
        except Exception:
            last_ha_publication = {"published": False, "reason": "homeassistant_api_error"}
            return dict(last_ha_publication)
        last_ha_signature, last_ha_time = signature, time.monotonic()
        last_ha_publication = {"published": True, "state": payload["state"], "timestamp": time.time()}
        return dict(last_ha_publication)


def clear_alert(
    camera,
    accept_current=False,
):
    slug = camera["slug"]

    current = image_path(
        slug,
        "current",
    )

    previous = image_path(
        slug,
        "previous",
    )

    if (
        accept_current
        and current.exists()
    ):
        shutil.copy2(
            current,
            previous,
        )

    set_rt(
        slug,
        pending_count=0,
        changed=False,
        status="Normal",
        last_error="",
    )

@serialized_camera_operation
def process_camera(camera):
    slug = camera["slug"]
    current = image_path(slug, "current")
    set_rt(slug, status="Capturando", last_error="")
    capture_frame(camera, current)
    observe_geometry(camera, current)
    census = observe_census(camera, current)
    brightness, contrast, detail = image_stats(current)
    latched, ready = get_alarm_latches().update(slug, census)
    active = not collection_only() and camera.get("enabled", True)
    changed = active and latched
    set_rt(slug, status=("CAMBIO DE ESCENA CONFIRMADO" if changed else
                        "Census activo" if active else "Recogida de datos · sin alarmas"),
           changed=changed, alarm_latched=latched, alarm_ready=ready or get_rt(slug).get("alarm_ready", False),
           offline=False, pending_count=census.get("pending", 0), difference=0.0, canny_difference=0.0,
           brightness=brightness, contrast=contrast, detail=detail,
           covered=covered_from_stats(brightness, contrast, detail), last_ok=time.time(), last_error="")
    publication = update_sabotaje_cam() if not collection_only() else {"published": False}
    record_observation(camera, current, connection="online", brightness=brightness,
                       contrast=contrast, detail=detail, possible_obstruction=get_rt(slug).get("covered"),
                       orb=get_rt(slug).get("orb"), census=census,
                       mode="collection_only" if collection_only() else "operational",
                       alarm_latched=latched, alarm_eligible=active,
                       alarms_published=publication.get("published", False),
                       alarm_state=publication.get("state"))


def camera_worker(slug):
    while not stop_event.is_set():
        camera = next((c for c in load_cameras() if c.get("slug") == slug), None)
        if camera is None:
            return
        try:
            process_camera(camera)
        except Exception as error:
            reset_geometry(slug, "offline", "no_valid_capture")
            set_rt(slug, status="Sin imagen", offline=True, last_error=safe_capture_error(error))
            publication = update_sabotaje_cam() if not collection_only() else {"published": False}
            record_observation(camera, connection="offline", error=safe_capture_error(error),
                               orb=get_rt(slug).get("orb"), census=get_rt(slug).get("census"),
                               mode="collection_only" if collection_only() else "operational",
                               alarms_published=publication.get("published", False),
                               alarm_state=publication.get("state"))
            print(f"[{camera.get('name', slug)}] ERROR: {safe_capture_error(error)}")
        stop_event.wait(max(10, int(load_options().get("observation_interval_seconds", 30))))


def ensure_workers():
    cameras = load_cameras()

    slugs = {
        camera["slug"]
        for camera
        in cameras
    }

    with workers_lock:

        for slug in list(workers):

            if (
                slug not in slugs
                or not workers[
                    slug
                ].is_alive()
            ):
                workers.pop(
                    slug,
                    None,
                )

        for camera in cameras:

            slug = camera["slug"]

            if slug not in workers:
                thread = threading.Thread(
                    target=camera_worker,
                    args=(slug,),
                    daemon=True,
                )

                workers[slug] = thread

                thread.start()


def manager_loop():
    while not stop_event.is_set():
        ensure_workers()
        if not collection_only():
            update_sabotaje_cam()

        stop_event.wait(5)


def base():
    return request.headers.get(
        "X-Ingress-Path",
        "",
    ).rstrip("/")


def header(title):
    return web_ui.header(title, base(), collection_only())


def footer():
    return web_ui.footer()


def supervision_state(camera):
    state = dict(get_rt(camera['slug']))
    # Display the current checkbox immediately, without discarding diagnostic
    # history or waiting for the next capture to overwrite runtime.changed.
    state['alarms_enabled'] = not collection_only() and camera.get('enabled', True)
    state['changed'] = bool(state.get('changed')) and state['alarms_enabled']
    references = {kind: True for kind in saved_reference_paths(IMAGES_DIR / camera['slug'])}
    state['sabotage'] = web_ui.sabotage_status(camera, state, collection_only(), references,
                                             dict(last_ha_publication))
    return state


@app.get("/")
def home():
    cameras = load_cameras()
    states = {camera["slug"]: supervision_state(camera) for camera in cameras}
    return web_ui.dashboard(cameras, states, base(), collection_only(), census_summary, orb_summary)


@app.get('/sample-history')
def sample_history_page():
    return web_ui.history_page(sample_history_status(), base(), collection_only())


@app.post('/sample-history/<action>')
def control_sample_history(action):
    if action not in ('pause', 'resume', 'clear', 'pause-diagnostics', 'resume-diagnostics', 'clear-diagnostics'):
        return 'Acción no válida', 400
    try:
        with observation_store_lock:
            if action == 'clear':
                get_observation_store().clear_images()
            elif action == 'clear-diagnostics':
                get_observation_store().clear_diagnostics()
            else:
                path = DATA_DIR / 'sample_history.json'
                settings = load_json(path, {})
                field = 'diagnostics_enabled' if action.endswith('-diagnostics') else 'images_enabled'
                settings[field] = action.startswith('resume')
                atomic_write_json(path, settings)
    except (OSError, ValueError):
        return 'No se pudo completar la acción sobre las muestras. Revisa el almacenamiento.', 500
    return redirect(base() + '/sample-history')

@app.get("/api/status")
def api_status():
    cameras_data = []

    for camera in load_cameras():
        state = supervision_state(camera)

        last_ok = state.get('last_capture_at', state.get('last_ok', 0))

        if last_ok:
            last_capture = time.strftime(
                "%H:%M:%S",
                time.localtime(last_ok),
            )
        else:
            last_capture = "—"

        cameras_data.append(
            {
                "slug": camera["slug"],
                "name": camera["name"],
                "sabotage": state["sabotage"],
                "alarms_enabled": state["alarms_enabled"],
                "orb": state.get("orb"),
                "orb_summary": orb_summary(state.get("orb")),
                "census": state.get("census"),
                "census_summary": census_summary(state.get("census")),
                "collection_only": collection_only(),
                "observation_storage": state.get("observation_storage"),
                "status": state.get(
                    "status",
                    "Esperando",
                ),
                "last_capture": last_capture,
                "difference": round(
                    float(
                        state.get(
                            "difference",
                            0,
                        )
                    ),
                    2,
                ),
                "canny": round(
                    float(
                        state.get(
                            "canny_difference",
                            0,
                        )
                    ),
                    2,
                ),
                "canny_threshold": float(
                    camera.get(
                        "canny_threshold",
                        4.0,
                    )
                ),
                "pending": int(
                    state.get(
                        "pending_count",
                        0,
                    )
                ),
                "confirmations": int(
                    camera.get(
                        "confirmations",
                        2,
                    )
                ),
                "changed": bool(
                    state.get(
                        "changed",
                        False,
                    )
                ),
            }
        )

    return {
        "cameras": cameras_data,
        "alarm_publication": dict(last_ha_publication),
        "sample_history": sample_history_status(),
        "timestamp": time.time(),
        "collection_only": collection_only(),
    }

def camera_form(
    camera,
    is_new,
):
    prefix = base()

    if is_new:
        password_text = (
            "Contraseña RTSP"
        )
    else:
        password_text = (
            "Dejar en blanco "
            "para conservarla"
        )

    checked = (
        "checked"
        if camera.get(
            "enabled",
            True,
        )
        else ""
    )

    return (
        header(
            "Añadir cámara"
            if is_new
            else (
                f"Editar "
                f"{camera['name']}"
            )
        )
        + f"""
<div class="card">

<form
    method="post"
    action="{prefix}/camera/save"
>

<input
    type="hidden"
    name="original_slug"
    value="{html.escape(camera.get('slug', ''))}"
>

<div class="grid">

<div>
<label>Nombre</label>
<input
    name="name"
    required
    value="{html.escape(camera.get('name', ''))}"
>
</div>

<div>
<label>URL RTSP</label>
<input
    name="rtsp_url"
    required
    value="{html.escape(camera.get('rtsp_url', ''))}"
    placeholder="rtsp://192.0.2.10:554/stream"
>
</div>

<div>
<label>Usuario RTSP</label>
<input
    name="username"
    value="{html.escape(camera.get('username', ''))}"
>
</div>

<div>
<label>Contraseña RTSP</label>
<input
    type="password"
    name="password"
    placeholder="{password_text}"
>
</div>

<div>
<label>Intervalo del detector anterior (s)</label>
<input
    type="number"
    min="5"
    name="interval"
    value="{int(camera.get('interval', 10))}"
>
</div>

<div>
<label>Umbral de cambio del detector anterior (%)</label>
<input
    type="number"
    min="1"
    max="100"
    step="0.1"
    name="threshold"
    value="{float(camera.get('threshold', 45.0))}"
>

</div>

<div>
<label>Umbral Canny anterior (%)</label>
<input
    type="number"
    min="0.1"
    max="100"
    step="0.1"
    name="canny_threshold"
    value="{float(camera.get('canny_threshold', 4.0))}"
>
</div>


<div>
<label>Confirmaciones del detector anterior</label>
<input
    type="number"
    min="1"
    max="10"
    name="confirmations"
    value="{int(camera.get('confirmations', 2))}"
>
</div>

<div>
<label>Timeout captura (s)</label>
<input
    type="number"
    min="5"
    max="60"
    name="capture_timeout"
    value="{int(camera.get('capture_timeout', 12))}"
>
</div>

</div>

<p>
<label>
<input
    style="width:auto"
    type="checkbox"
    name="enabled"
    {checked}
>
Habilitar avisos Census en esta cámara
</label>
</p>

<button>
Guardar
</button>

<a
    class="btn btn2"
    href="{prefix}/"
>
Cancelar
</a>

</form>
</div>
"""
        + footer()
    )


@app.get(
    "/camera/new"
)
def camera_new():
    return camera_form(
        dict(DEFAULT_CAMERA),
        True,
    )


@app.get(
    "/camera/<slug>/edit"
)
def camera_edit(slug):

    camera = next(
        (
            item
            for item
            in load_cameras()
            if item["slug"]
            == slug
        ),
        None,
    )

    if not camera:
        return (
            "No encontrada",
            404,
        )

    return camera_form(
        camera,
        False,
    )


@app.post(
    "/camera/save"
)
def camera_save():

    cameras = load_cameras()

    original = request.form.get(
        "original_slug",
        "",
    ).strip()

    name = request.form.get(
        "name",
        "",
    ).strip()

    slug = (
        original
        or slugify(name)
    )

    old = next(
        (
            item
            for item
            in cameras
            if item["slug"]
            == original
        ),
        None,
    )

    password = request.form.get(
        "password",
        "",
    )

    if (
        old
        and not password
    ):
        password = old.get(
            "password",
            "",
        )

    camera = {
        "name": name,
        "slug": slug,
        "enabled": (
            request.form.get(
                "enabled"
            )
            == "on"
        ),
        "rtsp_url": (
            request.form.get(
                "rtsp_url",
                "",
            ).strip()
        ),
        "username": (
            request.form.get(
                "username",
                "",
            ).strip()
        ),
        "password": password,
        "interval": max(
            5,
            int(
                request.form.get(
                    "interval",
                    "10",
                )
            ),
        ),
        "threshold": float(
            request.form.get(
                "threshold",
                "45",
            )
        ),
        "canny_threshold": float(
            request.form.get(
                "canny_threshold",
                "4.0",
            )
        ),
        "confirmations": max(
            1,
            int(
                request.form.get(
                    "confirmations",
                    "2",
                )
            ),
        ),
        "capture_timeout": max(
            5,
            int(
                request.form.get(
                    "capture_timeout",
                    "12",
                )
            ),
        ),
    }

    if old:
        cameras = [
            camera
            if item["slug"]
            == original
            else item
            for item
            in cameras
        ]

    else:
        cameras.append(
            camera
        )

    save_cameras(
        cameras
    )

    
    return redirect(
        base() + "/"
    )


@app.get(
    "/camera/<slug>"
)
def camera_detail(slug):

    camera = next(
        (
            item
            for item
            in load_cameras()
            if item["slug"]
            == slug
        ),
        None,
    )

    if not camera:
        return (
            "No encontrada",
            404,
        )

    state = supervision_state(camera)
    refs = {kind: True for kind in saved_reference_paths(IMAGES_DIR / slug)}
    refs['fixed'] = image_path(slug, 'fixed').exists()
    return web_ui.detail(camera, state, base(), collection_only(),
                         census_details(state.get("census")) + orb_details(state.get("orb")),
                         refs, refs)

@app.post(
    "/camera/<slug>/capture-now"
)
def capture_now(slug):

    camera = next(
        (
            item
            for item
            in load_cameras()
            if item["slug"]
            == slug
        ),
        None,
    )

    if not camera:
        return (
            "No encontrada",
            404,
        )

    with camera_operation_lock(slug):
        try:
            capture_frame(camera, image_path(slug, "current"))
            set_rt(slug, status="Captura manual realizada", last_error="")
        except Exception as error:
            set_rt(slug, status="Error de captura", last_error=safe_capture_error(error))

    return redirect(
        base()
        + f"/camera/{slug}"
    )


@app.post(
    "/camera/<slug>/accept"
)
def accept_current(slug):

    camera = next(
        (
            item
            for item
            in load_cameras()
            if item["slug"]
            == slug
        ),
        None,
    )

    if not camera:
        return (
            "No encontrada",
            404,
        )

    clear_alert(
        camera,
        True,
    )

    return redirect(
        base()
        + f"/camera/{slug}"
    )


def store_shared_reference(camera, kind, source, origin):
    """Explicit reference selection only; never learn a replacement automatically."""
    if kind not in REFERENCE_KINDS:
        raise ValueError('Referencia inválida')
    content = source.read_bytes()
    with Image.open(BytesIO(content)) as image:
        if image.format != 'JPEG':
            raise ValueError('La referencia debe ser una imagen JPEG válida')
        image.verify()
    directory = camera_dir(camera['slug'])
    with census_lock:
        directory.mkdir(exist_ok=True)
        destination = image_path(camera['slug'], kind)
        temporary = destination.with_suffix('.tmp.jpg')
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        metadata = load_json(directory / 'reference_selection.json', {})
        metadata[kind] = {'origin': origin, 'selected_at': time.time(),
                          'sha256': hashlib.sha256(content).hexdigest()}
        atomic_write_json(directory / 'reference_selection.json', metadata)
        if orb_observer is not None:
            orb_observer.reset(camera['slug'])
        census_observers.pop(camera['slug'], None)
        census_pending_evidence.pop(camera['slug'], None)
        set_rt(camera['slug'], alarm_ready=False, census={
            'status': 'inconclusive', 'reason': 'references_updated', 'reliable': False,
            'comparison_mode': 'day_night_references', 'pending': 0})


@app.post('/camera/<slug>/census-reference/<kind>', defaults={'action': 'save'})
@app.post('/camera/<slug>/census-reference/<kind>/<action>')
def census_reference(slug, kind, action):
    camera = next((c for c in load_cameras() if c['slug'] == slug), None)
    if camera is None:
        return 'Cámara no encontrada', 404
    if kind not in REFERENCE_KINDS or action not in ('save', 'import', 'delete'):
        return 'Acción de referencia inválida', 400
    return modify_reference(camera, kind, action)


@serialized_camera_operation
def modify_reference(camera, kind, action):
    slug = camera['slug']
    if action == 'delete':
        with census_lock:
            image_path(slug, kind).unlink(missing_ok=True)
            if orb_observer is not None:
                orb_observer.reset(slug)
            census_observers.pop(slug, None)
            census_pending_evidence.pop(slug, None)
            set_rt(slug, alarm_ready=False, census={'status': 'inconclusive', 'reliable': False,
                                                  'reason': 'references_updated'})
    else:
        source = image_path(slug, 'current' if action == 'save' else kind)
        if not source.is_file():
            return 'No hay imagen disponible para esta referencia', 400
        state = get_rt(slug)
        captured_at = state.get('last_capture_at', state.get('last_ok', 0))
        if action == 'save' and time.time() - captured_at > 180:
            return 'La captura no es reciente. Pulsa Capturar ahora antes de guardar.', 400
        try:
            store_shared_reference(camera, kind, source,
                                   'current_capture' if action == 'save' else 'selected_saved_reference')
        except (OSError, ValueError):
            return 'No se pudo guardar una referencia válida', 400
    return redirect(base() + '/camera/' + slug)


@app.post('/camera/<slug>/reference-group/<group>/add')
def add_reference(slug, group):
    camera = next((c for c in load_cameras() if c['slug'] == slug), None)
    if camera is None:
        return 'Cámara no encontrada', 404
    if group not in REFERENCE_GROUPS:
        return 'Grupo de referencias inválido', 400
    with camera_operation_lock(slug):
        kind = next((k for k in REFERENCE_GROUPS[group] if not image_path(slug, k).is_file()), None)
        if kind is None:
            return 'Ya hay cinco imágenes en este grupo. Borra o actualiza una para continuar.', 409
        return modify_reference(camera, kind, 'save')


@app.post(
    "/camera/<slug>/reference/<kind>"
)
def save_reference(
    slug,
    kind,
):

    if kind in REFERENCE_KINDS:
        return census_reference(slug, kind, 'save')

    if kind not in (
        "day",
        "night",
        "fixed",
    ):
        return (
            "Referencia inválida",
            400,
        )

    current = image_path(
        slug,
        "current",
    )

    if not current.exists():
        return (
            "No hay captura actual",
            400,
        )

    shutil.copy2(
        current,
        image_path(
            slug,
            kind,
        ),
    )

    return redirect(
        base()
        + f"/camera/{slug}"
    )


@app.post(
    "/camera/<slug>/reference/<kind>/delete"
)
def delete_reference(
    slug,
    kind,
):

    if kind in REFERENCE_KINDS:
        return census_reference(slug, kind, 'delete')

    if kind not in (
        "day",
        "night",
        "fixed",
    ):
        return (
            "Referencia inválida",
            400,
        )

    image_path(
        slug,
        kind,
    ).unlink(
        missing_ok=True
    )

    return redirect(
        base()
        + f"/camera/{slug}"
    )


@app.post(
    "/camera/<slug>/delete"
)
def delete_camera(slug):

    cameras = [
        camera
        for camera
        in load_cameras()
        if camera["slug"]
        != slug
    ]

    save_cameras(
        cameras
    )

    shutil.rmtree(
        camera_dir(slug),
        ignore_errors=True,
    )

    with runtime_lock:
        runtime.pop(
            slug,
            None,
        )

    return redirect(
        base() + "/"
    )


@app.get(
    "/image/<slug>/<kind>"
)
def image(
    slug,
    kind,
):

    if kind not in REFERENCE_KINDS and kind not in (
        "current",
        "previous",
        "day",
        "night",
        "fixed",
        "census-day",
        "night_alt",
        "census-night",
    ):
        return (
            "No encontrada",
            404,
        )

    path = (image_path(slug, kind.removeprefix("census-"))
            if kind in ("census-day", "census-night") else image_path(slug, kind))

    if not path.exists():
        return Response(
            status=404
        )

    return Response(
        path.read_bytes(),
        mimetype="image/jpeg",
        headers={
            "Cache-Control":
                "no-store, max-age=0"
        },
    )


def main():

    print(
        "Camera Watchdog "
        "multicámara iniciando..."
    )

    manager = threading.Thread(
        target=manager_loop,
        daemon=True,
    )

    manager.start()

    app.run(
        host="0.0.0.0",
        port=8099,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
