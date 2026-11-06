from fastapi import FastAPI, APIRouter, Depends, HTTPException, status, BackgroundTasks, Request
from sqlalchemy.orm import Session
from . import models, schemas, database
from .migraciones import aplicar_migraciones
from . import notificaciones
from typing import List
from fastapi.middleware.cors import CORSMiddleware # importación obligatoria
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import func, text
import pandas as pd
from fastapi.responses import StreamingResponse, FileResponse
import io
import uuid
import threading
import time
from collections import deque
from sqlalchemy.orm import selectinload

#Importaciones de seguridad
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, date, timezone, time as dtime
from zoneinfo import ZoneInfo
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from sqlalchemy.exc import IntegrityError
from fastapi.middleware.cors import CORSMiddleware

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import os # Aseguramos que os esté importado para leer el .env
from dotenv import load_dotenv

load_dotenv()

ZONA_MX = ZoneInfo("America/Mexico_City")  # hora oficial de la clínica



app = FastAPI(
    title="Dogs & Cats",
    description="Backend robusto para gestion de mascotas y citas",
    version="1.0.0"
)

# 2. Configuración
# Solo nuestra página (y el entorno local) pueden llamar a la API desde el navegador.
# Si algún día cambia el dominio, se agrega en la variable CORS_ORIGINS (separado por comas).
ORIGENES_PERMITIDOS = [
    "https://dogs-and-cats-app.onrender.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
] + [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_credentials=False,  # la sesión viaja en el encabezado Authorization, no en cookies
    allow_methods=["*"],
    allow_headers=["*"],
)
# Comprime las respuestas grandes (lista de dueños, ventas): ~10 veces menos datos
# por cada actualización automática, importante cuando se usa con datos del celular.
app.add_middleware(GZipMiddleware, minimum_size=1000)

models.Base.metadata.create_all(bind=database.engine)   # crea tablas nuevas
aplicar_migraciones(database.engine)                     # agrega columnas nuevas (ver migraciones.py)


@app.on_event("startup")
def _arrancar_programador():
    notificaciones.iniciar_programador()   # resumen de la mañana (notificación al teléfono)

# --- Condiguracion de seguridad JWT ---
# La llave maestra para firmar las sesiones. Vive en el .env (local) y en las
# variables de entorno de Render (producción). NUNCA escribirla en el código.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("Falta la variable de entorno SECRET_KEY (agrégala al .env o a Render)")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 720 # El token durará 12 horas

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# Hash de relleno: si el usuario no existe se compara igual contra algo, para que
# la respuesta tarde lo mismo y no se pueda adivinar qué usuarios existen.
HASH_RELLENO = pwd_context.hash("usuario-inexistente-relleno")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def enviar_aviso_doctora(dueño, mascota, fecha, hora, motivo):
    if not os.getenv("EMAIL_PASSWORD"):
        return  # sin contraseña de correo configurada no se intenta enviar
    
    # --- CONFIGURACIÓN ---
    correo_emisor = "mdogsandcats28@gmail.com" 
    password_emisor = os.getenv("EMAIL_PASSWORD")
    
    # LISTA DE DOCTORAS
    correos_destino = [
        "marucha28@me.com", 
        "ofeliadogsandcats@gmail.com"
    ]
    
    # ✨ LA MAGIA: Convertimos la lista en un solo texto separado por comas
    destinatarios_texto = ", ".join(correos_destino)

    # Crear el mensaje
    msg = MIMEMultipart()
    msg['From'] = correo_emisor
    msg['To'] = destinatarios_texto # <--- Le pasamos el texto, no la lista
    msg['Subject'] = f"📅 Nueva Cita Agendada: {' '.join(str(mascota).split())}"

    cuerpo = f"""
    Hola Doctoras,
    
    Se ha registrado una nueva cita en el sistema:
    
    🐾 Mascota: {mascota}
    👤 Dueño: {dueño}
    📅 Fecha: {fecha}
    ⏰ Hora: {hora}
    🩺 Motivo: {motivo}
    
    Saludos, 
    Sistema de Gestión Dogs & Cats.
    """
    msg.attach(MIMEText(cuerpo, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=20)
        server.starttls()
        server.login(correo_emisor, password_emisor)
        server.send_message(msg)
        server.quit()
        print("✅ Aviso enviado con éxito a todas las doctoras.")
    except Exception as e:
        print(f"❌ Error al enviar aviso: {e}")

# =====================================================================
# --- LÍMITE DE INTENTOS (contra adivinar contraseñas y contra spam) ---
# En memoria del servidor: se reinicia si Render reinicia el servicio (suficiente aquí).
# =====================================================================
class Limitador:
    def __init__(self, maximo: int, ventana_seg: int):
        self.maximo, self.ventana = maximo, ventana_seg
        self.eventos: dict = {}
        self.lock = threading.Lock()

    def _limpiar(self, clave, ahora):
        q = self.eventos.get(clave)
        while q and ahora - q[0] > self.ventana:
            q.popleft()
        if q is not None and not q:
            del self.eventos[clave]

    def bloqueado(self, clave) -> bool:
        with self.lock:
            ahora = time.monotonic()
            self._limpiar(clave, ahora)
            return len(self.eventos.get(clave, ())) >= self.maximo

    def registrar(self, clave):
        with self.lock:
            ahora = time.monotonic()
            self._limpiar(clave, ahora)
            self.eventos.setdefault(clave, deque()).append(ahora)
            if len(self.eventos) > 10000:  # protección de memoria
                self.eventos.clear()

    def limpiar(self, clave):
        with self.lock:
            self.eventos.pop(clave, None)


LOGIN_FALLIDOS = Limitador(maximo=6, ventana_seg=15 * 60)        # 6 errores en 15 min → espera
REGISTRO_POR_IP = Limitador(maximo=20, ventana_seg=10 * 60)      # Sala de Espera: 20 por 10 min por red
REGISTRO_GLOBAL = Limitador(maximo=80, ventana_seg=10 * 60)      # y 80 en total por 10 min


def ip_cliente(request: Request) -> str:
    for encabezado in ("cf-connecting-ip", "true-client-ip"):
        if request.headers.get(encabezado):
            return request.headers[encabezado].strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# El Guardia de Seguridad: verifica que el token sea válido
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas o sesión expirada",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

# 🔒 Todas las rutas de "privado" exigen una sesión válida (token).
# Las únicas públicas son: "/", "/login/" y "/public/registro" (Sala de Espera).
privado = APIRouter(dependencies=[Depends(get_current_user)])

# =====================================================================
# --- ADMINISTRACIÓN DE USUARIOS (solo la administradora: Maru) ---
# Quién es administrador se define en la variable ADMIN_USERNAMES (separados
# por comas). No depende de la pantalla: aunque alguien modifique el navegador,
# el backend vuelve a revisar en cada petición.
# =====================================================================
ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "maru peña,maru pena").split(",")
    if u.strip()
}

def es_admin(user: models.User) -> bool:
    return (user.username or "").strip().lower() in ADMIN_USERNAMES

def get_current_admin(current_user: models.User = Depends(get_current_user)):
    if not es_admin(current_user):
        raise HTTPException(status_code=403, detail="Solo la administradora puede hacer esto")
    return current_user

# Quién soy (para que la pantalla sepa si mostrar el botón de Usuarios)
@privado.get("/me/", response_model=schemas.UserMe)
def read_me(current_user: models.User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username, "is_admin": es_admin(current_user)}

@privado.get("/users/", response_model=List[schemas.UserMe])
def list_users(admin: models.User = Depends(get_current_admin), db: Session = Depends(database.get_db)):
    users = db.query(models.User).order_by(models.User.username).all()
    return [{"id": u.id, "username": u.username, "is_admin": es_admin(u)} for u in users]

# Crear usuario: exige sesión de administradora Y que vuelva a escribir SU contraseña
@privado.post("/register/", response_model=schemas.UserResponse)
def register_user(user: schemas.UserCreate, admin: models.User = Depends(get_current_admin),
                  db: Session = Depends(database.get_db)):
    if not verify_password(user.admin_password, admin.hashed_password):
        raise HTTPException(status_code=403, detail="Tu contraseña de administradora no es correcta")

    username = user.username.strip().lower()
    if username in ADMIN_USERNAMES:
        raise HTTPException(status_code=400, detail="Ese nombre está reservado para la administradora")
    db_user = db.query(models.User).filter(func.lower(models.User.username) == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="El usuario ya existe")

    new_user = models.User(username=username, hashed_password=get_password_hash(user.password))
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@privado.delete("/users/{user_id}")
def delete_user(user_id: int, admin: models.User = Depends(get_current_admin), db: Session = Depends(database.get_db)):
    db_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if db_user.id == admin.id or es_admin(db_user):
        raise HTTPException(status_code=400, detail="No se puede eliminar a la administradora")
    db.delete(db_user)
    db.commit()
    return {"message": f"Usuario {db_user.username} eliminado"}

# =====================================================================
# --- NOTIFICACIONES AL TELÉFONO (Web Push) ---
# =====================================================================
@privado.get("/push/clave-publica")
def push_clave_publica():
    return {"disponible": notificaciones.push_disponible(), "publicKey": notificaciones.VAPID_PUBLIC_KEY}

@privado.post("/push/suscribir")
def push_suscribir(datos: schemas.PushSubscribe, current_user: models.User = Depends(get_current_user),
                   db: Session = Depends(database.get_db)):
    sub = db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == datos.endpoint).first()
    if not sub:
        sub = models.PushSubscription(endpoint=datos.endpoint)
        db.add(sub)
    sub.user_id = current_user.id
    sub.p256dh = datos.keys.p256dh
    sub.auth = datos.keys.auth
    sub.user_agent = datos.user_agent
    db.commit()
    return {"message": "Notificaciones activadas en este dispositivo"}

@privado.post("/push/desuscribir")
def push_desuscribir(datos: schemas.PushUnsubscribe, db: Session = Depends(database.get_db)):
    db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == datos.endpoint).delete()
    db.commit()
    return {"message": "Notificaciones desactivadas en este dispositivo"}

@privado.post("/push/prueba")
def push_prueba(current_user: models.User = Depends(get_current_user)):
    resultado = notificaciones.enviar_push(
        "🔔 ¡Listo! Las notificaciones funcionan",
        "Así te avisaremos de citas nuevas y del resumen de cada mañana. 🐾",
        "/?avisos=1", "prueba", user_ids=[current_user.id],
    )
    if resultado["enviadas"] == 0:
        raise HTTPException(status_code=400, detail="No se pudo enviar. Activa las notificaciones en este dispositivo primero.")
    return resultado

# Probar el resumen de la mañana a cualquier hora (solo la administradora, solo a sus dispositivos)
@privado.post("/push/prueba-resumen")
def push_prueba_resumen(admin: models.User = Depends(get_current_admin), db: Session = Depends(database.get_db)):
    titulo, cuerpo = notificaciones.contenido_resumen(db)
    resultado = notificaciones.enviar_push(titulo, cuerpo, "/?avisos=1", "resumen", user_ids=[admin.id])
    return {**resultado, "titulo": titulo, "cuerpo": cuerpo}

# =====================================================================
# --- CENTRO DE AVISOS PARA DUEÑOS (WhatsApp de un toque, sin costo) ---
# =====================================================================
@privado.get("/avisos/")
def get_avisos(db: Session = Depends(database.get_db)):
    pendientes = notificaciones.avisos_pendientes(db)
    return {"total": len(pendientes), "pendientes": pendientes}

@privado.post("/avisos/preventivo/{record_id}/enviado")
def marcar_aviso_preventivo(record_id: int, db: Session = Depends(database.get_db)):
    r = db.query(models.PreventiveRecord).filter(models.PreventiveRecord.id == record_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    r.reminder_sent_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Aviso marcado como enviado"}

# =====================================================================
# --- CARTILLA: vacunas, desparasitación y tratamientos preventivos ---
# =====================================================================
@privado.get("/pets/{pet_id}/preventivos", response_model=List[schemas.Preventivo])
def list_preventivos(pet_id: int, db: Session = Depends(database.get_db)):
    return (db.query(models.PreventiveRecord).filter(models.PreventiveRecord.pet_id == pet_id)
            .order_by(models.PreventiveRecord.applied_date.desc()).all())

@privado.get("/preventivos/proximos")
def get_preventivos_proximos(dias: int = 30, db: Session = Depends(database.get_db)):
    return notificaciones.preventivos_proximos(db, max(1, min(dias, 365)))

@privado.post("/preventivos/", response_model=schemas.Preventivo)
def create_preventivo(datos: schemas.PreventivoCreate, db: Session = Depends(database.get_db)):
    if not db.query(models.Pet).filter(models.Pet.id == datos.pet_id).first():
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    if datos.next_due_date and datos.next_due_date <= datos.applied_date:
        raise HTTPException(status_code=400, detail="La próxima dosis debe ser después de la fecha de aplicación")
    r = models.PreventiveRecord(**datos.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r

@privado.put("/preventivos/{record_id}", response_model=schemas.Preventivo)
def update_preventivo(record_id: int, datos: schemas.PreventivoUpdate, db: Session = Depends(database.get_db)):
    r = db.query(models.PreventiveRecord).filter(models.PreventiveRecord.id == record_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    if datos.next_due_date and datos.next_due_date <= datos.applied_date:
        raise HTTPException(status_code=400, detail="La próxima dosis debe ser después de la fecha de aplicación")
    if r.next_due_date != datos.next_due_date:
        r.reminder_sent_at = None  # cambió la fecha: hay que volver a avisar
    for campo, valor in datos.model_dump().items():
        setattr(r, campo, valor)
    db.commit()
    db.refresh(r)
    return r

@privado.delete("/preventivos/{record_id}")
def delete_preventivo(record_id: int, db: Session = Depends(database.get_db)):
    r = db.query(models.PreventiveRecord).filter(models.PreventiveRecord.id == record_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    db.delete(r)
    db.commit()
    return {"message": "Registro eliminado"}

@privado.post("/avisos/{appt_id}/enviado")
def marcar_aviso(appt_id: int, datos: schemas.AvisoEnviado, db: Session = Depends(database.get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    notificaciones.marcar_aviso_enviado(db, appt, datos.tipo)
    return {"message": "Aviso marcado como enviado"}

# --- LOGIN ---

@app.post("/login/", response_model=schemas.Token)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_db)):
    clave = (form_data.username or "").strip().lower()
    if LOGIN_FALLIDOS.bloqueado(clave):
        raise HTTPException(status_code=429, detail="Demasiados intentos fallidos. Espera 15 minutos e intenta de nuevo.")
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    correcto = verify_password(form_data.password, user.hashed_password if user else HASH_RELLENO)
    if not user or not correcto:
        LOGIN_FALLIDOS.registrar(clave)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    LOGIN_FALLIDOS.limpiar(clave)
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/")
def read_root():
    return {"message": "Bienvenido a la API de la Veterinaria"}

# --- SALA DE ESPERA (PÚBLICA) ---
# Registra dueño + mascota en un solo paso. A propósito NO devuelve datos del
# dueño ni de sus otras mascotas: así nadie puede consultar expedientes ajenos
# escribiendo un teléfono.
@app.post("/public/registro")
def registro_publico(datos: schemas.RegistroPublico, background_tasks: BackgroundTasks, request: Request,
                     db: Session = Depends(database.get_db)):
    ip = ip_cliente(request)
    if REGISTRO_POR_IP.bloqueado(ip) or REGISTRO_GLOBAL.bloqueado("todos"):
        raise HTTPException(status_code=429, detail="Demasiados registros seguidos. Espera unos minutos o pide ayuda en recepción.")
    REGISTRO_POR_IP.registrar(ip)
    REGISTRO_GLOBAL.registrar("todos")
    telefono = "".join(c for c in datos.owner.phone if c.isdigit())
    if len(telefono) != 10:
        raise HTTPException(status_code=400, detail="El teléfono debe tener 10 dígitos")

    owner = db.query(models.Owner).filter(models.Owner.phone == telefono).first()
    if not owner:
        owner = models.Owner(
            name=datos.owner.name.strip(),
            address=(datos.owner.address or "").strip(),
            phone=telefono,
        )
        db.add(owner)
        db.flush()  # para obtener el id sin cerrar la transacción

    pet = models.Pet(
        name=datos.pet.name.strip(),
        species=datos.pet.species,
        breed=datos.pet.breed,
        birth_date=datos.pet.birth_date,
        sex=datos.pet.sex,
        color=datos.pet.color,
        owner_id=owner.id,
    )
    db.add(pet)
    db.commit()
    db.refresh(pet)
    # 📱 Avisar al personal que alguien se registró en la Sala de Espera
    background_tasks.add_task(
        notificaciones.enviar_push,
        "🐾 Nuevo registro en Sala de Espera",
        f"{pet.name} ({pet.species}) · dueño: {owner.name}",
        "/",
        f"registro-{pet.id}",
    )
    return {"mensaje": "Registro exitoso", "mascota": pet.name, "folio": f"DC-{pet.id:04d}"}

#Creacion de los dueños
@privado.post("/owners/", response_model=schemas.Owner)
def create_owner(owner: schemas.OwnerCreate, db: Session = Depends(database.get_db)):
    
    # 1. Buscamos si ya existe alguien registrado con ese MISMO TELÉFONO
    #    (solo si SÍ trae teléfono; si viene vacío, siempre es un cliente nuevo)
    if owner.phone and owner.phone.strip():
        existing_owner = db.query(models.Owner).filter(models.Owner.phone == owner.phone.strip()).first()

        # 2. Si ya existe, ¡detenemos todo y regresamos al dueño original!
        if existing_owner:
            return existing_owner
        
    # 3. Si no existe, es un cliente nuevo y lo guardamos normalmente
    db_owner = models.Owner(**owner.model_dump())
    db.add(db_owner)
    db.commit()
    db.refresh(db_owner)
    return db_owner
 
#Creacion de las mascotas
@privado.post("/pets/", response_model=schemas.Pet)
def create_pet(pet: schemas.PetCreate, db: Session = Depends(database.get_db)):
    db_owner = db.query(models.Owner).filter(models.Owner.id == pet.owner_id).first()
    if not db_owner:
        raise HTTPException(status_code=404, detail="El dueño no existe")
    
    db_pet = models.Pet(
        name=pet.name,
        species=pet.species,
        breed=pet.breed,
        birth_date=pet.birth_date,
        sex=pet.sex,
        color=pet.color,
        owner_id=pet.owner_id
    )

    db.add(db_pet)
    db.commit()
    db.refresh(db_pet)
    return db_pet

# Muestra de los dueños junto con las mascotas
@privado.get("/owners/", response_model=List[schemas.Owner])
def get_owners(db: Session = Depends(database.get_db)):
    # Se cargan mascotas, citas y cartillas en 4 consultas en lugar de una por cada mascota
    owners = (db.query(models.Owner)
              .options(selectinload(models.Owner.pets).selectinload(models.Pet.appointments),
                       selectinload(models.Owner.pets).selectinload(models.Pet.preventivos))
              .order_by(models.Owner.id).all())
    return owners

# Muestra de las mascotas 
@privado.get("/pets/", response_model=List[schemas.Pet])
def get_pets(db: Session = Depends(database.get_db)):
    pets = (db.query(models.Pet)
            .options(selectinload(models.Pet.appointments), selectinload(models.Pet.preventivos))
            .order_by(models.Pet.id).all())
    return pets

# Mostrar todos los usuarios que existen
@privado.get("/stats/")
def get_stats(db: Session = Depends(database.get_db)):
    count_owners = db.query(models.Owner).count()
    count_pets = db.query(models.Pet).count()
    return{
        "total_dueños": count_owners,
        "total_mascotas": count_pets,
        "mensaje": "Sistema listo" if count_owners > 0 else "Base de datos vacia"
    }

# Editar a los dueños
@privado.put("/owners/{owner_id}", response_model=schemas.Owner)
def update_owner(owner_id: int, owner_update: schemas.OwnerCreate, db: Session = Depends(database.get_db)):
    db_owner = db.query(models.Owner).filter(models.Owner.id == owner_id).first()
    if not db_owner:
        raise HTTPException(status_code=404, detail="Dueño no encontrado para actualizar")
    
    db_owner.name = owner_update.name
    db_owner.address = owner_update.address
    #db_owner.email = owner_update.email
    db_owner.phone = owner_update.phone

    db.commit()
    db.refresh(db_owner)
    return db_owner

# Editar a las mascotas
@privado.put("/pets/{pet_id}", response_model=schemas.Pet)
def update_pet(pet_id: int, pet_update: schemas.PetCreate, db: Session = Depends(database.get_db)):
    db_pet = db.query(models.Pet).filter(models.Pet.id == pet_id).first()
    if not db_pet:
        raise HTTPException(status_code=404, detail="Mascota no encontrada")
    if pet_update.owner_id != db_pet.owner_id and not db.query(models.Owner).filter(models.Owner.id == pet_update.owner_id).first():
        raise HTTPException(status_code=404, detail="El dueño no existe")
    
    db_pet.name = pet_update.name
    db_pet.species = pet_update.species
    db_pet.breed = pet_update.breed
    db_pet.birth_date = pet_update.birth_date
    db_pet.sex = pet_update.sex
    db_pet.color = pet_update.color
    db_pet.owner_id = pet_update.owner_id

    db.commit()
    db.refresh(db_pet)
    return db_pet

# Eliminacion de los dueños
@privado.delete("/owners/{owner_id}/delete")
def delete_owner(owner_id: int, db: Session = Depends(database.get_db)):
    db_owner = db.query(models.Owner).filter(models.Owner.id == owner_id).first()
    if not db_owner:
        raise HTTPException(status_code= 404, detail="Dueño no encontrado")
    
    db.delete(db_owner)
    db.commit()
    return{"message": f"Dueño {owner_id} y sus mascotas fueron eliminados correctamente"}

# Eliminacion de las mascotas
@privado.delete("/pets/{pet_id}/delete")
def delete_pet(pet_id: int, db: Session = Depends(database.get_db)):
    db_pet = db.query(models.Pet).filter(models.Pet.id == pet_id).first()
    if not db_pet:
        raise HTTPException(status_code= 404, detail="Mascota no encontrada")
    
    db.delete(db_pet)
    db.commit()
    return {"message": "Mascota eliminada"}

def _validar_fecha_hora(fecha: str, hora: str):
    """La fecha debe ser AAAA-MM-DD real y la hora HH:MM (00:00 a 23:59)."""
    try:
        date.fromisoformat((fecha or "").strip())
        if len((fecha or "").strip()) != 10:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="La fecha de la cita no es válida (usa AAAA-MM-DD)")
    h = (hora or "").strip()
    try:
        datetime.strptime(h[:5], "%H:%M")
        if len(h) not in (5, 8):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="La hora de la cita no es válida (usa HH:MM)")


def _validar_horario_libre(db: Session, fecha: str, hora: str, estado: str, excluir_id: int | None = None):
    """Una cita PENDIENTE no puede caer en el mismo día y hora que otra cita pendiente.
    Las canceladas no ocupan horario y las recetas rápidas (Realizada) no bloquean."""
    if estado != "Pendiente" or not fecha or not hora:
        return
    hora_norm = hora.strip()[:5]
    q = db.query(models.Appointment).filter(
        models.Appointment.date == fecha,
        models.Appointment.status == "Pendiente",
    )
    if excluir_id is not None:
        q = q.filter(models.Appointment.id != excluir_id)
    if any((a.time or "").strip()[:5] == hora_norm for a in q.all()):
        raise HTTPException(status_code=409, detail=f"Ese horario ya está ocupado ({fecha} a las {hora_norm}). Elige otra hora.")

@privado.post("/appointments/", response_model=schemas.Appointment)
def create_appointment(appointment: schemas.AppointmentCreate,  background_tasks: BackgroundTasks, db: Session = Depends(database.get_db)):
    _validar_fecha_hora(appointment.date, appointment.time)
    if not db.query(models.Pet).filter(models.Pet.id == appointment.pet_id).first():
        raise HTTPException(status_code=404, detail="La mascota no existe")
    _validar_horario_libre(db, appointment.date, appointment.time, appointment.status)
    db_appointment = models.Appointment(**appointment.model_dump())
    db.add(db_appointment)
    db.commit()
    db.refresh(db_appointment)

    # ✨ EL TRUCO: Verificamos que el estado sea "Pendiente" antes de mandar el correo
    if appointment.status == "Pendiente":
        mascota = db.query(models.Pet).filter(models.Pet.id == appointment.pet_id).first()
        dueño = db.query(models.Owner).filter(models.Owner.id == mascota.owner_id).first() if mascota else None
        
        nombre_mascota = mascota.name if mascota else "N/D"
        nombre_dueño = dueño.name if dueño else "N/D"

        background_tasks.add_task(
            enviar_aviso_doctora, 
            nombre_dueño, 
            nombre_mascota, 
            appointment.date, 
            appointment.time, 
            appointment.reason
        )
        # 📱 Notificación al teléfono del personal
        background_tasks.add_task(
            notificaciones.enviar_push,
            "📅 Nueva cita agendada",
            f"{nombre_mascota} ({nombre_dueño}) · {notificaciones.fecha_larga(appointment.date)} "
            f"a las {(appointment.time or '')[:5]} · {appointment.reason}",
            "/?avisos=1",
            f"cita-{db_appointment.id}",
        )
        
    return db_appointment

@privado.get("/appointments/", response_model=List[schemas.Appointment])
def get_appointments(db: Session = Depends(database.get_db)):
    return db.query(models.Appointment).all()

@privado.get("/appointments/booked-times/{date}")
def get_booked_times(date: str, db: Session = Depends(database.get_db)):
    appointments = db.query(models.Appointment).filter(
        models.Appointment.date == date,
        models.Appointment.status != "Cancelada"
    ).all()
    
    booked_times = []
    for appt in appointments:
        if isinstance(appt.time, str):
            booked_times.append(appt.time[:5])
        else:
            booked_times.append(appt.time.strftime('%H:%M'))
            
    return booked_times

@privado.put("/appointments/{appt_id}", response_model=schemas.Appointment)
def update_appointment(appt_id: int, appt_update: schemas.AppointmentCreate, db: Session = Depends(database.get_db)):
    db_appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not db_appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    # Solo se revisa el formato si cambió (para no bloquear la edición de citas viejas)
    if (db_appt.date, db_appt.time) != (appt_update.date, appt_update.time):
        _validar_fecha_hora(appt_update.date, appt_update.time)
    if appt_update.pet_id != db_appt.pet_id and not db.query(models.Pet).filter(models.Pet.id == appt_update.pet_id).first():
        raise HTTPException(status_code=404, detail="La mascota no existe")
    _validar_horario_libre(db, appt_update.date, appt_update.time, appt_update.status, excluir_id=appt_id)
    # Si se cambió la fecha u hora, hay que volver a avisar al dueño (nueva confirmación)
    if (db_appt.date, (db_appt.time or "")[:5]) != (appt_update.date, (appt_update.time or "")[:5]):
        db_appt.confirm_sent_at = None
        db_appt.reminder_sent_at = None
        if db_appt.created_at is None:
            db_appt.created_at = datetime.now(timezone.utc)
    db_appt.date = appt_update.date
    db_appt.time = appt_update.time
    db_appt.reason = appt_update.reason
    db_appt.status = appt_update.status # Permitir cambiar el estado
    if appt_update.prescription_text is not None:
        db_appt.prescription_text = appt_update.prescription_text
    db.commit()
    db.refresh(db_appt)
    return db_appt

@privado.delete("/appointments/{appt_id}")
def delete_appointment(appt_id: int, db: Session = Depends(database.get_db)):
    db_appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not db_appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    db.delete(db_appt)
    db.commit()
    return {"message": "Cita eliminada"}

@privado.get("/products/", response_model=list[schemas.Product])
def read_products(db: Session = Depends(database.get_db)):
    return db.query(models.Product).all()

@privado.post("/products/", response_model=schemas.Product)
def create_or_update_product(product: schemas.ProductCreate, db: Session = Depends(database.get_db)):
    # Comparación exacta sin distinguir mayúsculas (ilike trataba "_" y "%" como comodines)
    db_product = db.query(models.Product).filter(func.lower(models.Product.name) == product.name.lower()).first()

    if db_product:
        db_product.quantity += product.quantity
        db_product.price = product.price
        db_product.application_price = product.application_price
        db_product.area = product.area
        db_product.expiration_date = product.expiration_date
        db.commit()
        db.refresh(db_product)
        return db_product
    
    new_product = models.Product(**product.model_dump())
    db.add(new_product)
    db.commit()
    db.refresh(new_product)
    return new_product

@privado.put("/products/{product_id}", response_model=schemas.Product)
def update_product(product_id: int, product_update: schemas.ProductCreate, db: Session = Depends(database.get_db)):
    db_product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    # Actualizamos solo la información base que aplica a todos los lotes
    db_product.category = product_update.category
    db_product.price = product_update.price
    db_product.application_price = product_update.application_price
    db_product.area = product_update.area
    db_product.unit = product_update.unit
    db_product.min_stock = product_update.min_stock
        
    db.commit()
    db.refresh(db_product)
    return db_product

@privado.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(database.get_db)):
    db_product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    # Revisamos ANTES si tiene ventas (no depende del tipo de error de cada conector de BD)
    if db.query(models.Sale).filter(models.Sale.product_id == product_id).first():
        raise HTTPException(
            status_code=400,
            detail="No puedes eliminar este artículo porque ya tiene ventas registradas en el historial. Por seguridad contable, corrige su cantidad a 0."
        )
    try:
        db.delete(db_product)
        db.commit()
        return {"message": "Producto eliminado exitosamente"}
    except IntegrityError:
        # Si da error de llave foránea, deshacemos el intento de borrado
        db.rollback()
        raise HTTPException(
            status_code=400, 
            detail="No puedes eliminar este artículo porque ya tiene ventas registradas en el historial. Por seguridad contable, edítalo y pon su stock en 0."
        )

# =====================================================================
# --- COBRO DEL TICKET COMPLETO (todo o nada) ---
# Recibe TODO el ticket en una sola petición y lo guarda en UNA transacción:
# si cualquier artículo falla (sin stock, caducado, error), no se guarda nada.
# Reparte cada artículo entre sus lotes: primero el que caduca antes, nunca caducados.
# =====================================================================
@privado.post("/sales/checkout/", response_model=List[schemas.Sale])
def checkout_ticket(ticket: schemas.CheckoutRequest, db: Session = Depends(database.get_db)):
    hoy = datetime.now(ZONA_MX).date()
    ventas = []
    ticket_id = uuid.uuid4().hex[:12]  # mismo número para todos los renglones de este cobro
    try:
        # Bloqueamos los productos mientras cobramos, para que dos cobros
        # simultáneos no vendan el mismo stock.
        # Un cobro a la vez (candado de Postgres que se suelta solo al terminar la transacción)
        # y los renglones siempre en el mismo orden: así dos cajas cobrando al mismo
        # tiempo esperan su turno en lugar de chocar ("deadlock").
        db.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        productos = db.query(models.Product).order_by(models.Product.id).with_for_update().all()
        por_articulo = {}
        for p in productos:
            por_articulo.setdefault(p.name.split("_@@_")[0].strip().lower(), []).append(p)

        for item in ticket.items:
            nombre = item.product_key.strip().lower()
            lotes = [
                p for p in por_articulo.get(nombre, [])
                if p.category == "Servicio"
                or ((p.quantity or 0) > 1e-9 and not (p.expiration_date and p.expiration_date < hoy))
            ]
            if not lotes:
                raise HTTPException(status_code=400, detail=f"'{item.product_key}' no tiene stock vigente")
            lotes.sort(key=lambda p: (p.expiration_date is None, p.expiration_date or date.max))

            es_servicio = lotes[0].category == "Servicio"
            if not es_servicio:
                disponible = sum(p.quantity for p in lotes)
                if disponible + 1e-9 < item.quantity:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Stock insuficiente de '{item.product_key}': hay {round(disponible, 2)} vigente(s)",
                    )

            pendiente, asignado = item.quantity, 0.0
            for lote in lotes:
                if pendiente <= 1e-9:
                    break
                toma = pendiente if es_servicio else min(pendiente, lote.quantity)
                pendiente -= toma
                es_ultimo = pendiente <= 1e-9
                # el total se reparte proporcional; el último lote se queda con el resto exacto
                monto = round(item.total - asignado, 2) if es_ultimo else round(item.total * toma / item.quantity, 2)
                asignado += monto
                if not es_servicio:
                    lote.quantity = round(lote.quantity - toma, 6)
                venta = models.Sale(product_id=lote.id, quantity=round(toma, 6),
                                    total_price=monto, payment_method=item.payment_method,
                                    ticket_id=ticket_id)
                db.add(venta)
                ventas.append(venta)

            # 💉 Si se indicó el paciente, se anota en su cartilla (en la misma transacción)
            if item.pet_id:
                if not db.query(models.Pet).filter(models.Pet.id == item.pet_id).first():
                    raise HTTPException(status_code=400, detail="El paciente elegido ya no existe")
                db.add(models.PreventiveRecord(
                    pet_id=item.pet_id,
                    tipo=item.tipo_preventivo or ("vacuna" if lotes[0].category == "Vacuna" else "otro"),
                    nombre=lotes[0].name.split("_@@_")[0].strip(),
                    applied_date=hoy,
                    next_due_date=item.next_due_date if (item.next_due_date and item.next_due_date > hoy) else None,
                    product_id=lotes[0].id,
                ))

        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error al cobrar ticket: {e}")
        raise HTTPException(status_code=500, detail="No se pudo registrar la venta. No se guardó nada; intenta de nuevo.")

    for v in ventas:
        db.refresh(v)
    return ventas

# =====================================================================
# --- CANCELAR VENTAS (solo la administradora) ---
# La venta NO se borra: queda marcada como cancelada (fecha y motivo) para que
# el contador vea qué pasó, y el stock regresa al MISMO lote de donde salió.
# =====================================================================
def _cancelar_ventas(db: Session, ventas: list, motivo: str | None):
    activas = [v for v in ventas if v.cancelled_at is None]
    if not activas:
        raise HTTPException(status_code=400, detail="Esa venta ya estaba cancelada")
    try:
        ids = {v.product_id for v in activas}
        db.execute(text("SELECT pg_advisory_xact_lock(424242)"))   # mismo turno que los cobros
        lotes = {p.id: p for p in db.query(models.Product).filter(models.Product.id.in_(ids))
                 .order_by(models.Product.id).with_for_update().all()}
        ahora = datetime.now(timezone.utc)
        for v in activas:
            lote = lotes.get(v.product_id)
            if lote and lote.category != "Servicio":
                lote.quantity = round((lote.quantity or 0) + (v.quantity or 0), 6)
            v.cancelled_at = ahora
            v.cancel_reason = (motivo or "").strip() or None
        db.commit()
    except Exception:
        db.rollback()
        raise
    for v in activas:
        db.refresh(v)
    return activas

@privado.post("/sales/tickets/{ticket_id}/cancel", response_model=List[schemas.Sale])
def cancel_ticket(ticket_id: str, datos: schemas.CancelRequest, admin: models.User = Depends(get_current_admin),
                  db: Session = Depends(database.get_db)):
    ventas = db.query(models.Sale).filter(models.Sale.ticket_id == ticket_id).all()
    if not ventas:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    return _cancelar_ventas(db, ventas, datos.reason)

# Para ventas viejas (de antes de que existieran los tickets): se cancelan una por una
@privado.post("/sales/{sale_id}/cancel", response_model=List[schemas.Sale])
def cancel_sale(sale_id: int, datos: schemas.CancelRequest, admin: models.User = Depends(get_current_admin),
                db: Session = Depends(database.get_db)):
    venta = db.query(models.Sale).filter(models.Sale.id == sale_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return _cancelar_ventas(db, [venta], datos.reason)

# --- CORREGIR UN LOTE capturado por error (cantidad y/o caducidad) ---
@privado.patch("/products/{product_id}/lote", response_model=schemas.Product)
def update_lote(product_id: int, datos: schemas.LoteUpdate, db: Session = Depends(database.get_db)):
    lote = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not lote:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if lote.category == "Servicio":
        raise HTTPException(status_code=400, detail="Los servicios no manejan lotes")
    lote.quantity = round(datos.quantity, 6)
    lote.expiration_date = datos.expiration_date
    db.commit()
    db.refresh(lote)
    return lote

# (La ruta vieja POST /sales/ se retiró: cobraba sin repartir por lotes ni número
#  de ticket. Todo cobro pasa por /sales/checkout/.)

@privado.get("/sales/", response_model=list[schemas.Sale])
def read_sales(db: Session = Depends(database.get_db)):
    # Traemos las ventas ordenadas de la más reciente a la más antigua
    return db.query(models.Sale).order_by(models.Sale.sale_date.desc()).all()

# =====================================================================
# --- REPORTES DE EXCEL ---
# Se generan en memoria (sin archivos temporales) y siempre en hora de
# Ciudad de México, aunque el servidor de Render trabaje en UTC.
# Los montos y cantidades van como NÚMEROS para que el contador pueda sumar.
# =====================================================================
FORMATO_DINERO = '"$"#,##0.00'
TIPO_EXCEL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


NOMBRES_AREA = {"medico": "Médico", "estetica": "Estética", "tienda": "Tienda"}

def area_de(p) -> str:
    """Área de negocio del producto. Si no se eligió, se deduce de la categoría."""
    if p is None:
        return "N/D"
    if p.area in NOMBRES_AREA:
        return NOMBRES_AREA[p.area]
    if p.category in ("Medicamento", "Vacuna"):
        return "Médico"
    if p.category == "Servicio":
        return "Estética"
    return "Tienda"


def _hoja_excel(writer, df, nombre_hoja, columnas_dinero=(), columnas_fecha=()):
    """Escribe un DataFrame en una hoja con encabezado de color, filtro,
    columnas ajustadas y formato de dinero/fecha."""
    df.to_excel(writer, sheet_name=nombre_hoja, index=False)
    ws = writer.sheets[nombre_hoja]
    # Seguridad: un texto capturado que empiece con "=" (ej. un nombre de producto)
    # se guarda como TEXTO, nunca como fórmula que Excel ejecute.
    for fila in ws.iter_rows():
        for celda in fila:
            if celda.data_type == "f":
                celda.data_type = "s"
    ws.freeze_panes = "A2"
    if len(df.columns):
        ws.auto_filter.ref = ws.dimensions
    for celda in ws[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="0D9488")
    for idx, col in enumerate(df.columns, start=1):
        valores = [str(col)] + [str(v) for v in df[col].tolist()]
        ws.column_dimensions[get_column_letter(idx)].width = min(max(max(len(v) for v in valores) + 3, 10), 60)
        formato = FORMATO_DINERO if col in columnas_dinero else ("DD/MM/YYYY" if col in columnas_fecha else None)
        if formato:
            for fila in range(2, len(df) + 2):
                ws.cell(row=fila, column=idx).number_format = formato


def _respuesta_excel(buffer, nombre_archivo):
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type=TIPO_EXCEL,
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@privado.get("/reports/inventory-status/")
def export_inventory(db: Session = Depends(database.get_db)):
    products = db.query(models.Product).all()
    grouped = {}

    for p in products:
        base_name = p.name.split("_@@_")[0].strip()
        key = base_name.lower()

        if key not in grouped:
            grouped[key] = {
                "Artículo": base_name,
                "Área": area_de(p),
                "Categoría": p.category,
                "Stock Total": p.quantity if p.category != 'Servicio' else 0,
                "Unidad": p.unit,
                "Precio": p.price,
                "Caducidades": [p.expiration_date] if p.expiration_date and p.quantity > 0 else []
            }
        else:
            if p.category != 'Servicio':
                grouped[key]["Stock Total"] += p.quantity
            if p.expiration_date and p.quantity > 0 and p.expiration_date not in grouped[key]["Caducidades"]:
                grouped[key]["Caducidades"].append(p.expiration_date)

    data = []
    for g in sorted(grouped.values(), key=lambda x: x["Artículo"].lower()):
        fechas = ", ".join(f.strftime("%d/%m/%Y") for f in sorted(g["Caducidades"])) or "N/A"
        data.append({
            "Artículo": g["Artículo"],
            "Área": g["Área"],
            "Categoría": g["Categoría"],
            "Stock Total": "Servicio" if g["Categoría"] == 'Servicio' else round(g["Stock Total"] or 0, 2),
            "Unidad": g["Unidad"],
            "Precio": round(g["Precio"] or 0, 2),
            "Fechas de Caducidad": fechas
        })

    columnas = ["Artículo", "Área", "Categoría", "Stock Total", "Unidad", "Precio", "Fechas de Caducidad"]
    df = pd.DataFrame(data, columns=columnas)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        _hoja_excel(writer, df, "Inventario", columnas_dinero=("Precio",))
    hoy = datetime.now(ZONA_MX).strftime("%Y-%m-%d")
    return _respuesta_excel(buffer, f"Inventario_{hoy}.xlsx")


def _reporte_ventas(db: Session, inicio: date, fin: date):
    """Excel de ventas del día `inicio` al día `fin` (ambos incluidos, hora de México)
    con 4 hojas: Ventas (detalle), Resumen, Por día y Por categoría."""
    desde = datetime.combine(inicio, dtime.min, tzinfo=ZONA_MX)
    hasta = datetime.combine(fin + timedelta(days=1), dtime.min, tzinfo=ZONA_MX)

    ventas = (
        db.query(models.Sale)
        .filter(models.Sale.sale_date >= desde, models.Sale.sale_date < hasta)
        .order_by(models.Sale.sale_date)
        .all()
    )
    productos = {p.id: p for p in db.query(models.Product).all()}

    canceladas = [v for v in ventas if v.cancelled_at is not None]
    ventas = [v for v in ventas if v.cancelled_at is None]   # las canceladas no cuentan en los totales

    filas = []
    for v in ventas:
        producto = productos.get(v.product_id)
        fecha = v.sale_date
        if fecha.tzinfo is None:  # por si la BD regresa la fecha sin zona: se guarda en UTC
            fecha = fecha.replace(tzinfo=timezone.utc)
        fecha = fecha.astimezone(ZONA_MX)
        cantidad = float(v.quantity or 0)
        total = round(float(v.total_price or 0), 2)
        filas.append({
            "Fecha": fecha.date(),
            "Hora": fecha.strftime("%H:%M"),
            "Producto/Servicio": producto.name.split("_@@_")[0].strip() if producto else "Desconocido",
            "Área": area_de(producto),
            "Categoría": producto.category if producto else "N/D",
            "Cantidad": round(cantidad, 3),
            "Unidad": producto.unit if producto else "",
            "Precio Unitario": round(total / cantidad, 2) if cantidad else 0,
            "Total": total,
            "Método de Pago": v.payment_method or "N/D",
        })

    columnas = ["Fecha", "Hora", "Producto/Servicio", "Área", "Categoría", "Cantidad", "Unidad",
                "Precio Unitario", "Total", "Método de Pago"]
    df = pd.DataFrame(filas, columns=columnas)

    # --- Resumen por método de pago ---
    periodo = f"Periodo: {inicio.strftime('%d/%m/%Y')} al {fin.strftime('%d/%m/%Y')}"
    resumen = [{"Concepto": periodo, "Operaciones": None, "Monto": None}]
    for metodo, grupo in df.groupby("Método de Pago"):
        resumen.append({"Concepto": f"Ventas en {metodo}", "Operaciones": len(grupo), "Monto": round(grupo["Total"].sum(), 2)})
    resumen.append({"Concepto": "TOTAL DEL PERIODO", "Operaciones": len(df), "Monto": round(df["Total"].sum(), 2)})
    df_resumen = pd.DataFrame(resumen, columns=["Concepto", "Operaciones", "Monto"])

    # --- Por día (una columna por método de pago) ---
    if len(df):
        df_dia = df.pivot_table(index="Fecha", columns="Método de Pago", values="Total", aggfunc="sum", fill_value=0)
        df_dia["Total del Día"] = df_dia.sum(axis=1)
        df_dia = df_dia.round(2).reset_index()
        df_dia.columns.name = None
    else:
        df_dia = pd.DataFrame(columns=["Fecha", "Total del Día"])

    # --- Por área (médico / estética / tienda) ---
    df_area = (
        df.groupby("Área").agg(Operaciones=("Total", "size"), Total=("Total", "sum")).round(2).reset_index()
        if len(df) else pd.DataFrame(columns=["Área", "Operaciones", "Total"])
    )

    # --- Por categoría ---
    df_cat = (
        df.groupby("Categoría").agg(Operaciones=("Total", "size"), Total=("Total", "sum")).round(2).reset_index()
        if len(df) else pd.DataFrame(columns=["Categoría", "Operaciones", "Total"])
    )

    # --- Canceladas (informativo, no suman) ---
    def _a_mx(f):
        return (f if f.tzinfo else f.replace(tzinfo=timezone.utc)).astimezone(ZONA_MX)
    filas_cancel = []
    for v in canceladas:
        producto = productos.get(v.product_id)
        filas_cancel.append({
            "Fecha de Venta": _a_mx(v.sale_date).strftime("%d/%m/%Y %H:%M"),
            "Producto/Servicio": producto.name.split("_@@_")[0].strip() if producto else "Desconocido",
            "Cantidad": round(float(v.quantity or 0), 3),
            "Total": round(float(v.total_price or 0), 2),
            "Método de Pago": v.payment_method or "N/D",
            "Cancelada el": _a_mx(v.cancelled_at).strftime("%d/%m/%Y %H:%M"),
            "Motivo": v.cancel_reason or "",
        })
    df_cancel = pd.DataFrame(filas_cancel, columns=["Fecha de Venta", "Producto/Servicio", "Cantidad", "Total",
                                                    "Método de Pago", "Cancelada el", "Motivo"])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        _hoja_excel(writer, df, "Ventas", columnas_dinero=("Precio Unitario", "Total"), columnas_fecha=("Fecha",))
        _hoja_excel(writer, df_resumen, "Resumen", columnas_dinero=("Monto",))
        _hoja_excel(writer, df_dia, "Por día", columnas_dinero=tuple(c for c in df_dia.columns if c != "Fecha"), columnas_fecha=("Fecha",))
        _hoja_excel(writer, df_area, "Por área", columnas_dinero=("Total",))
        _hoja_excel(writer, df_cat, "Por categoría", columnas_dinero=("Total",))
        if len(df_cancel):
            _hoja_excel(writer, df_cancel, "Canceladas", columnas_dinero=("Total",))
    return _respuesta_excel(buffer, f"Ventas_{inicio.isoformat()}_al_{fin.isoformat()}.xlsx")


# --- REPORTE DE EXCEL: VENTAS POR PERIODO (para el contador) ---
@privado.get("/reports/sales/")
def export_sales(inicio: date, fin: date, db: Session = Depends(database.get_db)):
    if fin < inicio:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la inicial")
    if (fin - inicio).days > 366:
        raise HTTPException(status_code=400, detail="El periodo máximo es de un año")
    return _reporte_ventas(db, inicio, fin)


# --- REPORTE DE EXCEL: VENTAS DE LA SEMANA ACTUAL (lunes a hoy, hora de México) ---
@privado.get("/reports/sales-weekly/")
def export_weekly_sales(db: Session = Depends(database.get_db)):
    hoy = datetime.now(ZONA_MX).date()
    lunes = hoy - timedelta(days=hoy.weekday())
    return _reporte_ventas(db, lunes, hoy)


# El router privado se registra AL FINAL, cuando ya tiene todas sus rutas.
app.include_router(privado)
