from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Union
from datetime import date, datetime # Cambié datetime por date para que sea más limpio con el input de HTML

# 1. ESQUEMAS DE CITAS (Deben ir primero)
class AppointmentBase(BaseModel):
    date: str
    time: str
    reason: str
    status: Optional[str] = "Pendiente"
    pet_id: int # Cambiado a int para que coincida con el ID de la mascota en la DB
    prescription_text: Optional[str] = None

class AppointmentCreate(AppointmentBase):
    pass

class Appointment(AppointmentBase):
    id: int
    class Config:
        from_attributes = True

# 2. ESQUEMAS DE MASCOTAS (Dependen de Appointment)
class PetBase(BaseModel):
    name: str
    species: str
    breed: Optional[str] = None
    birth_date: Optional[Union[date, datetime, str]] = None # Lo dejamos como str para facilitar el manejo con el input de React
    sex : Optional[str]=  None
    color : Optional[str]= None

class PetCreate(PetBase):
    owner_id: int

class Pet(PetBase):
    id: int
    owner_id: int
    appointments: List[Appointment] = [] # Ahora sí conoce qué es Appointment
    class Config:
        from_attributes = True 

# 3. ESQUEMAS DE DUEÑOS (Dependen de Pet)
class OwnerCreate(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None

class Owner(BaseModel):
    id: int
    name: str
    email: str
    phone: Optional[str] = None
    pets: List[Pet] = [] # Ahora sí conoce qué es Pet
 
    class Config:
        from_attributes = True

class ProductBase(BaseModel):
    category: str
    name: str
    quantity: float
    unit: str
    price: float
    min_stock: float
    expiration_date: Optional[date] = None

class ProductCreate(ProductBase):
    pass

class Product(ProductBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


class SaleBase(BaseModel):
    product_id: int
    quantity: float
    payment_method: str
    total_price: Optional[float] = None

class SaleCreate(SaleBase):
    pass

class Sale(SaleBase):
    id: int
    total_price: float
    sale_date: datetime

    model_config = ConfigDict(from_attributes=True)

# ---Esquemas de seguridad ---
class UserCreate(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str
