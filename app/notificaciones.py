"""
NOTIFICACIONES
==============
1) Notificaciones al teléfono (Web Push) para Maru y el personal.
   - Estándar Web Push (RFC 8291 + VAPID RFC 8292), funciona con iPhone (Apple) y Android (Google).
   - Programado con la librería `cryptography` que ya usa el proyecto: sin dependencias nuevas.
   - En iPhone/iPad solo funciona si la app está "Agregada a inicio" (iOS 16.4+).
2) Centro de avisos para los dueños (WhatsApp de un toque, sin costo):
   confirmaciones de citas nuevas y recordatorios de citas próximas.
3) Resumen de cada mañana (lunes a sábado, 8:30 hora de México).
"""
import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sqlalchemy.orm import selectinload

from . import models, database

ZONA_MX = ZoneInfo("America/Mexico_City")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:mdogsandcats28@gmail.com")

HORA_RESUMEN = (8, 30)        # resumen de la mañana
DIAS_RECORDATORIO = (1, 2)    # recordatorio a dueños: citas de mañana y pasado mañana
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def push_disponible() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


# ---------------------------------------------------------------------------
# Web Push: cifrado del mensaje (RFC 8291, aes128gcm) y firma VAPID (RFC 8292)
# ---------------------------------------------------------------------------
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _hmac(clave: bytes, datos: bytes) -> bytes:
    h = hmac.HMAC(clave, hashes.SHA256())
    h.update(datos)
    return h.finalize()


def _cifrar(payload: bytes, p256dh: str, auth: str) -> bytes:
    ua_public = _b64d(p256dh)
    auth_secret = _b64d(auth)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_public = as_key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_key.exchange(ec.ECDH(), ua_key)

    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    salt = os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    cifrado = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02 = último registro
    encabezado = salt + struct.pack("!L", 4096) + bytes([len(as_public)]) + as_public
    return encabezado + cifrado


def _jwt_vapid(endpoint: str) -> str:
    u = urlparse(endpoint)
    encabezado = _b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    datos = _b64e(json.dumps({
        "aud": f"{u.scheme}://{u.netloc}",
        "exp": int(time.time()) + 3600,  # 1 hora (Apple pide vigencias cortas)
        "sub": VAPID_SUBJECT,
    }, separators=(",", ":")).encode())
    firmar = f"{encabezado}.{datos}".encode()
    clave = ec.derive_private_key(int.from_bytes(_b64d(VAPID_PRIVATE_KEY), "big"), ec.SECP256R1())
    r, s = decode_dss_signature(clave.sign(firmar, ec.ECDSA(hashes.SHA256())))
    return f"{encabezado}.{datos}.{_b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # un servicio de push nunca redirige: si lo hace, no se sigue


_abridor = urllib.request.build_opener(_SinRedirecciones)


def _enviar_a(sub: "models.PushSubscription", payload: dict) -> int:
    """Envía a UNA suscripción. Regresa el código HTTP (201 = entregado al servicio de push)."""
    from .schemas import endpoint_push_valido
    if not endpoint_push_valido(sub.endpoint):
        return 410   # dirección no permitida (p. ej. guardada antes de esta regla): se borra
    cuerpo = _cifrar(json.dumps(payload, ensure_ascii=False).encode(), sub.p256dh, sub.auth)
    req = urllib.request.Request(sub.endpoint, data=cuerpo, method="POST", headers={
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "high",
        "Authorization": f"vapid t={_jwt_vapid(sub.endpoint)}, k={VAPID_PUBLIC_KEY}",
    })
    try:
        with _abridor.open(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # sin internet, DNS, etc.
        print(f"❌ Push no enviado: {e}")
        return 0


def enviar_push(titulo: str, cuerpo: str, url: str = "/", tag: str | None = None,
                user_ids: list | None = None) -> dict:
    """Envía una notificación a todos los dispositivos suscritos (o solo a ciertos usuarios).
    Abre su propia sesión de BD para poder correr en segundo plano."""
    if not push_disponible():
        return {"enviadas": 0, "fallidas": 0, "motivo": "Faltan las llaves VAPID"}
    payload = {"title": titulo, "body": cuerpo, "url": url, "tag": tag or "dogs-cats"}
    enviadas = fallidas = 0
    db = database.SessionLocal()
    try:
        q = db.query(models.PushSubscription)
        if user_ids:
            q = q.filter(models.PushSubscription.user_id.in_(user_ids))
        for sub in q.all():
            codigo = _enviar_a(sub, payload)
            if 200 <= codigo < 300:
                enviadas += 1
            else:
                fallidas += 1
                if codigo in (404, 410):  # el teléfono canceló el permiso o desinstaló: se borra
                    db.delete(sub)
                else:
                    print(f"⚠️ Push rechazado ({codigo}) para {sub.endpoint[:60]}...")
        db.commit()
    finally:
        db.close()
    return {"enviadas": enviadas, "fallidas": fallidas}


# ---------------------------------------------------------------------------
# Centro de avisos para dueños (WhatsApp de un toque)
# ---------------------------------------------------------------------------
def fecha_larga(fecha_iso: str) -> str:
    try:
        d = datetime.strptime(fecha_iso[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return fecha_iso or ""
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def telefono_whatsapp(tel: str | None) -> str | None:
    if not tel:
        return None
    dig = "".join(c for c in tel if c.isdigit())
    if len(dig) == 10:
        return "52" + dig   # México
    return dig or None


def avisos_pendientes(db) -> list:
    hoy = datetime.now(ZONA_MX).date()
    limite = hoy + timedelta(days=max(DIAS_RECORDATORIO))
    citas = (
        db.query(models.Appointment)
        .filter(models.Appointment.status == "Pendiente",
                models.Appointment.date >= hoy.isoformat(),
                models.Appointment.date <= (hoy + timedelta(days=60)).isoformat())
        .order_by(models.Appointment.date, models.Appointment.time)
        .all()
    )
    avisos = []
    for a in citas:
        try:
            dia = datetime.strptime(a.date[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        mascota, dueño = a.pet, (a.pet.owner if a.pet else None)
        if not mascota or not dueño:
            continue
        hora = (a.time or "")[:5]
        datos = {
            "appt_id": a.id, "fecha": a.date, "hora": hora, "motivo": a.reason,
            "mascota": mascota.name, "dueño": dueño.name,
            "telefono": telefono_whatsapp(dueño.phone),
        }
        if a.created_at is not None and a.confirm_sent_at is None:
            # Sin emojis: el enlace de WhatsApp los daña ("�"). Las *negritas* sí funcionan.
            avisos.append({**datos, "tipo": "confirmacion", "mensaje": (
                f"Hola {dueño.name}, te confirmamos la cita de *{mascota.name}* en *Dogs & Cats* "
                f"para el *{fecha_larga(a.date)}* a las *{hora}*. Motivo: {a.reason}. "
                "Si necesitas cambiarla, respóndenos por aquí. ¡Te esperamos!")})
        elif a.reminder_sent_at is None and dia <= limite and (dia - hoy).days in DIAS_RECORDATORIO:
            cuando = "mañana" if (dia - hoy).days == 1 else "pasado mañana"
            avisos.append({**datos, "tipo": "recordatorio", "mensaje": (
                f"Hola {dueño.name}, te recordamos que *{mascota.name}* tiene cita en *Dogs & Cats* "
                f"{cuando}, *{fecha_larga(a.date)}*, a las *{hora}*. Motivo: {a.reason}. "
                "Si no puedes asistir, avísanos por aquí. ¡Gracias!")})
    return avisos + avisos_preventivos(db)


NOMBRE_TIPO = {"vacuna": "vacuna", "desparasitacion": "desparasitación", "otro": "tratamiento"}
DIAS_AVISO_VACUNA = 7      # avisar al dueño 7 días antes
DIAS_VENCIDA_MAX = 30      # y hasta 30 días después de vencida


def cartilla_vigente(db) -> list:
    """Último registro de cada tratamiento (por mascota y nombre) que tiene próxima dosis."""
    registros = (db.query(models.PreventiveRecord)
                 .options(selectinload(models.PreventiveRecord.pet).selectinload(models.Pet.owner))
                 .order_by(models.PreventiveRecord.applied_date.desc(), models.PreventiveRecord.id.desc())
                 .all())
    vistos, ultimos = set(), []
    for r in registros:
        clave = (r.pet_id, (r.nombre or "").strip().lower())
        if clave in vistos:
            continue
        vistos.add(clave)
        if r.next_due_date:
            ultimos.append(r)
    return ultimos


def estado_preventivo(fecha) -> str:
    hoy = datetime.now(ZONA_MX).date()
    if fecha is None:
        return "sin-refuerzo"
    if fecha < hoy:
        return "vencida"
    if fecha <= hoy + timedelta(days=30):
        return "proxima"
    return "al-dia"


def preventivos_proximos(db, dias: int = 30) -> list:
    """Vacunas/desparasitaciones que vencen en los próximos `dias` o ya vencieron (hasta 60 días)."""
    hoy = datetime.now(ZONA_MX).date()
    salida = []
    for r in cartilla_vigente(db):
        if not (hoy - timedelta(days=60) <= r.next_due_date <= hoy + timedelta(days=dias)):
            continue
        mascota = r.pet
        dueño = mascota.owner if mascota else None
        if not mascota or not dueño:
            continue
        salida.append({
            "id": r.id, "pet_id": mascota.id, "owner_id": dueño.id, "tipo": r.tipo, "nombre": r.nombre,
            "mascota": mascota.name, "especie": mascota.species, "dueño": dueño.name,
            "telefono": telefono_whatsapp(dueño.phone),
            "aplicada": r.applied_date.isoformat(), "proxima": r.next_due_date.isoformat(),
            "estado": estado_preventivo(r.next_due_date),
            "dias": (r.next_due_date - hoy).days,
            "aviso_enviado": r.reminder_sent_at is not None,
        })
    return sorted(salida, key=lambda x: x["proxima"])


def avisos_preventivos(db) -> list:
    hoy = datetime.now(ZONA_MX).date()
    avisos = []
    for p in preventivos_proximos(db, DIAS_AVISO_VACUNA):
        if p["aviso_enviado"] or p["dias"] < -DIAS_VENCIDA_MAX:
            continue
        que = f"{NOMBRE_TIPO.get(p['tipo'], 'tratamiento')} *{p['nombre']}*"
        if p["dias"] < 0:
            cuando = f"ya le toca su {que} (venció el *{fecha_larga(p['proxima'])}*)"
        elif p["dias"] == 0:
            cuando = f"le toca su {que} *hoy*"
        else:
            cuando = f"le toca su {que} el *{fecha_larga(p['proxima'])}*"
        avisos.append({
            "tipo": "preventivo", "record_id": p["id"], "appt_id": None,
            "fecha": p["proxima"], "hora": "", "motivo": f"{p['nombre']} ({NOMBRE_TIPO.get(p['tipo'])})",
            "mascota": p["mascota"], "dueño": p["dueño"], "telefono": p["telefono"],
            "mensaje": (f"Hola {p['dueño']}, te recordamos que a *{p['mascota']}* {cuando}. "
                        "Respóndenos por aquí para agendar su cita en *Dogs & Cats*. ¡Gracias!"),
        })
    return avisos


def marcar_aviso_enviado(db, appt: "models.Appointment", tipo: str):
    ahora = datetime.now(timezone.utc)
    if tipo == "confirmacion":
        appt.confirm_sent_at = ahora
        # si la cita ya es en los próximos días, la confirmación también cuenta como recordatorio
        try:
            dia = datetime.strptime(appt.date[:10], "%Y-%m-%d").date()
            if (dia - datetime.now(ZONA_MX).date()).days <= max(DIAS_RECORDATORIO):
                appt.reminder_sent_at = ahora
        except (ValueError, TypeError):
            pass
    else:
        appt.reminder_sent_at = ahora
    db.commit()


# ---------------------------------------------------------------------------
# Resumen de la mañana (hilo en segundo plano, revisa cada minuto)
# ---------------------------------------------------------------------------
def contenido_resumen(db) -> tuple:
    """Título y texto del resumen de la mañana (citas de hoy + avisos por mandar)."""
    hoy = datetime.now(ZONA_MX).date().isoformat()
    citas = (db.query(models.Appointment)
             .filter(models.Appointment.date == hoy, models.Appointment.status == "Pendiente")
             .order_by(models.Appointment.time).all())
    pendientes = len(avisos_pendientes(db))
    if citas:
        titulo = f"☀️ Buenos días: hoy hay {len(citas)} cita{'s' if len(citas) != 1 else ''}"
        cuerpo = f"La primera es a las {(citas[0].time or '')[:5]}."
    else:
        titulo = "☀️ Buenos días: hoy no hay citas agendadas"
        cuerpo = ""
    vacunas = [p for p in preventivos_proximos(db, 7) if p["dias"] >= -DIAS_VENCIDA_MAX]
    if vacunas:
        cuerpo += f" 💉 {len(vacunas)} vacuna{'s' if len(vacunas) != 1 else ''}/desparasitaci{'ones' if len(vacunas) != 1 else 'ón'} por vencer esta semana."
    if pendientes:
        cuerpo += f" Tienes {pendientes} aviso{'s' if pendientes != 1 else ''} por mandar a los dueños."
    return titulo, (cuerpo.strip() or "Que tengan un excelente día 🐾")


def _resumen_de_hoy():
    db = database.SessionLocal()
    try:
        ahora = datetime.now(ZONA_MX)
        hoy = ahora.date().isoformat()
        if ahora.weekday() == 6 or (ahora.hour, ahora.minute) < HORA_RESUMEN or ahora.hour >= 14:
            return  # domingo, antes de la hora, o ya muy tarde para "buenos días"
        estado = db.query(models.AppState).filter(models.AppState.key == "resumen_diario").first()
        if estado and estado.value == hoy:
            return  # ya se mandó hoy
        if not estado:
            estado = models.AppState(key="resumen_diario", value=hoy)
            db.add(estado)
        else:
            estado.value = hoy
        db.commit()

        titulo, cuerpo = contenido_resumen(db)
        enviar_push(titulo, cuerpo, url="/?avisos=1", tag="resumen")
        print(f"✔ Resumen de la mañana enviado ({hoy})")
    except Exception as e:
        print(f"❌ Error en el resumen de la mañana: {e}")
    finally:
        db.close()


_programador_iniciado = False


def iniciar_programador():
    global _programador_iniciado
    if _programador_iniciado or not push_disponible():
        return
    _programador_iniciado = True

    def ciclo():
        while True:
            _resumen_de_hoy()
            time.sleep(60)

    threading.Thread(target=ciclo, name="resumen-diario", daemon=True).start()
    print("✔ Programador de notificaciones iniciado")
