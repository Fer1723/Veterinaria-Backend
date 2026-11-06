# app/models.py
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Float, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base
from datetime import datetime

class Owner(Base):
    __tablename__ = "owners"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    address = Column(String, index=True)
    phone = Column(String)
    
    pets = relationship("Pet", back_populates="owner", cascade="all, delete-orphan")

class Pet(Base):
    __tablename__ = "pets"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    species = Column(String)
    breed = Column(String)
    birth_date = Column(DateTime, nullable=True)
    sex = Column(String, nullable=True)
    color = Column(String, nullable=True)
    
    owner_id = Column(Integer, ForeignKey("owners.id"))

    registration_date = Column(String, default=lambda: datetime.now().strftime("%d/%m/%Y"))

    owner = relationship("Owner", back_populates="pets")

    appointments = relationship("Appointment", back_populates="pet", cascade="all , delete-orphan")
    preventivos = relationship("PreventiveRecord", back_populates="pet", cascade="all, delete-orphan",
                               order_by="PreventiveRecord.applied_date.desc()")

class Appointment(Base):
    __tablename__= "appointments"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String)
    time = Column(String)
    reason = Column(String)
    status = Column(String, default="Pendiente")
    pet_id = Column(Integer, ForeignKey("pets.id"))
    prescription_text = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # cuándo se agendó
    confirm_sent_at = Column(DateTime(timezone=True), nullable=True)   # WhatsApp de confirmación enviado
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)  # WhatsApp de recordatorio enviado

    pet = relationship("Pet", back_populates="appointments")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key= True, index= True)
    category = Column(String, index=True)
    name = Column(String, unique=True, index=True)
    quantity = Column(Float)
    unit = Column(String)
    price = Column(Float)
    application_price = Column(Float, nullable=True)  # precio fijo por aplicación (vacunas, etc.)
    area = Column(String, nullable=True)  # "medico" | "estetica" | "tienda" (vacío = según categoría)
    min_stock = Column(Float)
    expiration_date = Column(Date, nullable=True)

class Sale(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id")) # Relación con el producto
    quantity = Column(Float)
    total_price = Column(Float)
    payment_method = Column(String)
    sale_date = Column(DateTime(timezone=True), server_default=func.now()) # Fecha y hora en automático-
    ticket_id = Column(String, nullable=True, index=True)          # agrupa los renglones de un mismo cobro
    cancelled_at = Column(DateTime(timezone=True), nullable=True)  # si tiene fecha, la venta fue cancelada
    cancel_reason = Column(String, nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)


# Teléfonos/tablets que activaron las notificaciones (una fila por dispositivo)
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint = Column(String, unique=True, nullable=False)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# Pequeña tabla de "memoria" del sistema (ej. qué día se mandó el resumen de la mañana)
class AppState(Base):
    __tablename__ = "app_state"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)


# Cartilla: vacunas, desparasitaciones y otros tratamientos preventivos con su próxima dosis
class PreventiveRecord(Base):
    __tablename__ = "preventive_records"

    id = Column(Integer, primary_key=True, index=True)
    pet_id = Column(Integer, ForeignKey("pets.id", ondelete="CASCADE"), index=True, nullable=False)
    tipo = Column(String, nullable=False, default="vacuna")   # vacuna | desparasitacion | otro
    nombre = Column(String, nullable=False)                   # ej. "Rabia", "Séxtuple", "Desparasitante interno"
    applied_date = Column(Date, nullable=False)               # cuándo se aplicó
    next_due_date = Column(Date, nullable=True)               # cuándo toca la siguiente (vacío = no se repite)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    notes = Column(String, nullable=True)
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)  # WhatsApp de recordatorio enviado
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    pet = relationship("Pet", back_populates="preventivos")
