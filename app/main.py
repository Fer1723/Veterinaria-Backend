from fastapi import FastAPI, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from . import models, schemas, database
from typing import List
from fastapi.middleware.cors import CORSMiddleware # importación obligatoria
from sqlalchemy import func
import pandas as pd
from fastapi.responses import StreamingResponse, FileResponse
import io

#Importaciones de seguridad
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWSError,jwt
from datetime import datetime, timedelta
import tempfile

from sqlalchemy.exc import IntegrityError
from fastapi.middleware.cors import CORSMiddleware

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart



app = FastAPI(
    title="Dogs & Cats",
    description="Backend robusto para gestion de mascotas y citas",
    version="1.0.0"
)

# 2. Configuración
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # El puerto de React
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
models.Base.metadata.create_all(bind=database.engine)

# --- Condiguracion de seguridad JWT ---
SECRET_KEY = "super_secreta_clave_veterinaria_para_produccion" # La llave maestra del servidor
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 720 # El token durará 12 horas

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def enviar_aviso_doctora(dueño, mascota, fecha, hora, motivo):
    # --- CONFIGURACIÓN ---
    # El correo desde donde saldrá el aviso (ej. el de la clínica)
    correo_emisor = "fercoarteaga314@gmail.com" 
    password_emisor = "beng gojx oymr lkgh" # Contraseña creada dentro del el mismo correo
    
    # EL CORREO DE LA DOCTORA (A donde llegará el aviso)
    correo_doctora = "luisfernandoarteaga879@gmail.com" 

    # Crear el mensaje
    msg = MIMEMultipart()
    msg['From'] = correo_emisor
    msg['To'] = correo_doctora
    msg['Subject'] = f"📅 Nueva Cita Agendada: {mascota}"

    cuerpo = f"""
    Hola Doctora,
    
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
        # Si usa Gmail es smtp.gmail.com. Si usa Outlook es smtp.office365.com
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(correo_emisor, password_emisor)
        server.send_message(msg)
        server.quit()
        print("Aviso enviado a la doctora.")
    except Exception as e:
        print(f"Error al enviar aviso: {e}")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
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

# --- RUTAS DE LOGIN Y REGISTRO ---
@app.post("/register/", response_model=schemas.UserResponse)
def register_user(user: schemas.UserCreate, db: Session = Depends(database.get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    
    hashed_password = get_password_hash(user.password)
    new_user = models.User(username=user.username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@app.post("/login/", response_model=schemas.Token)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/")
def read_root():
    return {"message": "Bienvenido a la API de la Veterinaria"}

#Creacion de los dueños
@app.post("/owners/", response_model=schemas.Owner)
def create_owner(owner: schemas.OwnerCreate, db: Session = Depends(database.get_db)):
    try:
        # Creamos el objeto del modelo
        db_owner = models.Owner(
            name=owner.name, 
            email=owner.email, 
            phone=owner.phone
        )
        db.add(db_owner)
        db.commit()
        db.refresh(db_owner)
        return db_owner
    except Exception as e:
        db.rollback() # Revierte si algo falla
        print(f"ERROR DETECTADO: {e}") # Esto saldrá en tu terminal de VS Code
        raise HTTPException(status_code=500, detail=str(e))
    
#Creacion de las mascotas
@app.post("/pets/", response_model=schemas.Pet)
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
@app.get("/owners/", response_model=List[schemas.Owner])
def get_owners(db: Session = Depends(database.get_db)):
    owners = db.query(models.Owner).all()
    if  not owners:
        raise HTTPException(status_code=404, detail="No se encontraron dueños registrados")
    
    return owners

# Muestra de las mascotas 
@app.get("/pets/", response_model=List[schemas.Pet])
def get_pets(db: Session = Depends(database.get_db)):
    pets = db.query(models.Pet).all()
    if not pets:
        raise HTTPException(status_code=404, detail="No hay mascotas en el sistema actualmente")
    return pets

# Mostrar todos los usuarios que existen
@app.get("/stats/")
def get_stats(db: Session = Depends(database.get_db)):
    count_owners = db.query(models.Owner).count()
    count_pets = db.query(models.Pet).count()
    return{
        "total_dueños": count_owners,
        "total_mascotas": count_pets,
        "mensaje": "Sistema listo" if count_owners > 0 else "Base de datos vacia"
    }

# Editar a los dueños
@app.put("/owners/{owner_id}", response_model=schemas.Owner)
def update_owner(owner_id: int, owner_update: schemas.OwnerCreate, db: Session = Depends(database.get_db)):
    db_owner = db.query(models.Owner).filter(models.Owner.id == owner_id).first()
    if not db_owner:
        raise HTTPException(status_code=404, detail="Dueño no encontrado para actualizar")
    
    db_owner.name = owner_update.name
    db_owner.email = owner_update.email
    db_owner.phone = owner_update.phone

    db.commit()
    db.refresh(db_owner)
    return db_owner

# Editar a las mascotas
@app.put("/pets/{pet_id}", response_model=schemas.Pet)
def update_pet(pet_id: int, pet_update: schemas.PetCreate, db: Session = Depends(database.get_db)):
    db_pet = db.query(models.Pet).filter(models.Pet.id == pet_id).first()
    if not db_pet:
        raise HTTPException(status_code=404, detail="Mascota no encontrada")
    
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
@app.delete("/owners/{owner_id}/delete")
def delete_owner(owner_id: int, db: Session = Depends(database.get_db)):
    db_owner = db.query(models.Owner).filter(models.Owner.id == owner_id).first()
    if not db_owner:
        raise HTTPException(status_code= 404, detail="Dueño no encontrado")
    
    db.delete(db_owner)
    db.commit()
    return{"message": f"Dueño {owner_id} y sus mascotas fueron eliminados correctamente"}

# Eliminacion de las mascotas
@app.delete("/pets{pet_id}/delete")
def delete_pet(pet_id: int, db: Session = Depends(database.get_db)):
    db_pet = db.query(models.Pet).filter(models.Pet.id == pet_id).first()
    if not db_pet:
        raise HTTPException(status_code= 404, detail="Mascota no encontrada")
    
    db.delete(db_pet)
    db.commit()
    return {"message": "Mascota eliminada"}

# En app/main.py
@app.post("/appointments/", response_model=schemas.Appointment)
def create_appointment(appointment: schemas.AppointmentCreate,  background_tasks: BackgroundTasks, db: Session = Depends(database.get_db)):
    db_appointment = models.Appointment(**appointment.model_dump())
    db.add(db_appointment)
    db.commit()
    db.refresh(db_appointment)

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
    return db_appointment

@app.get("/appointments/", response_model=List[schemas.Appointment])
def get_appointments(db: Session = Depends(database.get_db)):
    return db.query(models.Appointment).all()

@app.put("/appointments/{appt_id}", response_model=schemas.Appointment)
def update_appointment(appt_id: int, appt_update: schemas.AppointmentCreate, db: Session = Depends(database.get_db)):
    db_appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not db_appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    
    db_appt.date = appt_update.date
    db_appt.time = appt_update.time
    db_appt.reason = appt_update.reason
    db_appt.status = appt_update.status # Permitir cambiar el estado
    if appt_update.prescription_text is not None:
        db_appt.prescription_text = appt_update.prescription_text
    db.commit()
    db.refresh(db_appt)
    return db_appt

@app.delete("/appointments/{appt_id}")
def delete_appointment(appt_id: int, db: Session = Depends(database.get_db)):
    db_appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    db.delete(db_appt)
    db.commit()
    return {"message": "Cita eliminada"}

@app.get("/products/", response_model=list[schemas.Product])
def read_products(db: Session = Depends(database.get_db)):
    return db.query(models.Product).all()

@app.post("/products/", response_model=schemas.Product)
def create_or_update_product(product: schemas.ProductCreate, db: Session = Depends(database.get_db)):
    db_product = db.query(models.Product).filter(models.Product.name.ilike(product.name)).first()

    if db_product:
        db_product.quantity += product.quantity
        db_product.price = product.price
        db_product.expiration_date = product.expiration_date
        db.commit()
        db.refresh(db_product)
        return db_product
    
    new_product = models.Product(**product.model_dump())
    db.add(new_product)
    db.commit()
    db.refresh(new_product)
    return new_product

@app.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(database.get_db)):
    db_product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
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

@app.post("/sales/", response_model=schemas.Sale)
def create_sale(sale: schemas.SaleCreate, db: Session = Depends(database.get_db)):
    db_product = db.query(models.Product).filter(models.Product.id == sale.product_id).first()
    if not db_product:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    # NUEVA LÓGICA: Si React manda el precio exacto de la dosis, lo respetamos.
    if sale.total_price is not None and sale.total_price > 0:
        final_total = sale.total_price
    else:
        # Si no, multiplicamos normal (frasco completo)
        final_total = db_product.price * sale.quantity

    # Restar stock
    if db_product.category != 'Servicio':
        if db_product.quantity < sale.quantity:
            raise HTTPException(status_code=400, detail="Stock insuficiente")
        db_product.quantity -= sale.quantity

    db_sale = models.Sale(
        product_id=sale.product_id,
        quantity=sale.quantity,
        total_price=final_total,
        payment_method=sale.payment_method
    )
    db.add(db_sale)
    db.commit()
    db.refresh(db_sale)
    return db_sale

@app.get("/sales/", response_model=list[schemas.Sale])
def read_sales(db: Session = Depends(database.get_db)):
    # Traemos las ventas ordenadas de la más reciente a la más antigua
    return db.query(models.Sale).order_by(models.Sale.sale_date.desc()).all()

@app.get("/reports/inventory-status/")
def export_inventory(db: Session = Depends(database.get_db)):
    products = db.query(models.Product).all()
    grouped = {}
    
    for p in products:
        base_name = p.name.split("_@@_")[0].strip()
        key = base_name.lower()
        
        if key not in grouped:
            grouped[key] = {
                "Artículo": base_name,
                "Categoría": p.category,
                "Stock Total": p.quantity if p.category != 'Servicio' else 999999,
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
    for g in grouped.values():
        # Convertimos cada fecha a texto (string) antes de unirlas con comas
        fechas_texto = [str(fecha) for fecha in g["Caducidades"]]
        fechas = ", ".join(fechas_texto) if fechas_texto else "N/A"
        
        data.append({
            "Artículo": g["Artículo"],
            "Categoría": g["Categoría"],
            "Stock Total": "Infinito" if g["Categoría"] == 'Servicio' else g["Stock Total"],
            "Unidad": g["Unidad"],
            "Precio": f"${g['Precio']}",
            "Fechas de Caducidad": fechas
        })
        
    df = pd.DataFrame(data)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df.to_excel(temp_file.name, index=False)
    return FileResponse(temp_file.name, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename="Inventario_Consolidado.xlsx")

# --- REPORTE DE EXCEL: VENTAS Semanales ---
@app.get("/reports/sales-weekly/")
def export_weekly_sales(db: Session = Depends(database.get_db)):
    hoy = datetime.now()
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    lunes = inicio_semana.replace(hour=0, minute=0, second=0, microsecond=0)

    ventas_semana = db.query(models.Sale).filter(models.Sale.sale_date >= lunes).all()

    data = []
    for v in ventas_semana:
        producto = db.query(models.Product).filter(models.Product.id == v.product_id).first()
        # Limpiamos el nombre para que el excel
        nombre_limpio = producto.name.split("_@@_")[0].strip() if producto else "Desconocido"
        data.append({
            "Fecha y Hora": v.sale_date.strftime("%Y-%m-%d %H:%M:%S"),
            "Producto/Servicio": nombre_limpio,
            "Cantidad": v.quantity,
            "Total Pagado": f"${v.total_price}",
            "Método de Pago": v.payment_method
        })

    df = pd.DataFrame(data)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    df.to_excel(temp_file.name, index=False)
    return FileResponse(temp_file.name, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=f"Ventas_Semanales_{hoy.strftime('%Y-%m-%d')}.xlsx")