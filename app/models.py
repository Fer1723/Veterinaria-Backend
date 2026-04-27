# app/models.py
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Float, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

class Owner(Base):
    __tablename__ = "owners"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String)
    email = Column(String, unique=True, index=True)
    
    # El nombre aquí debe coincidir con el atributo en Pet
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
    
    # ForeignKey apunta al nombre de la TABLA ("owners.id")
    owner_id = Column(Integer, ForeignKey("owners.id"))

    # back_populates apunta al NOMBRE DEL ATRIBUTO en la otra clase ("pets")
    owner = relationship("Owner", back_populates="pets")

    appointments = relationship("Appointment", back_populates="pet", cascade="all , delete-orphan")

class Appointment(Base):
    __tablename__= "appointments"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String)
    time = Column(String)
    reason = Column(String)
    status = Column(String, default="Pendiente")
    pet_id = Column(Integer, ForeignKey("pets.id"))
    prescription_text = Column(String, nullable=True)

    pet = relationship("Pet", back_populates="appointments")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key= True, index= True)
    category = Column(String, index=True)
    name = Column(String, unique=True, index=True)
    quantity = Column(Float)
    unit = Column(String)
    price = Column(Float)
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

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)