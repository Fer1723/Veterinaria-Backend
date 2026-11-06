from pydantic import BaseModel, ConfigDict, Field, field_validator, field_serializer
from typing import List, Optional, Union
from datetime import date, datetime 
from urllib.parse import urlparse

ESTADOS_CITA = ("Pendiente", "Realizada", "Cancelada")

# Servicios de notificaciones reales (Google/Chrome/Android, Apple/iPhone, Firefox, Edge).
# Cualquier otra dirección se rechaza: así nadie puede usar el servidor para
# "tocar" direcciones internas (ataque SSRF).
HOSTS_PUSH = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com")
SUFIJOS_PUSH = (".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com")

def endpoint_push_valido(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        return False
    return host in HOSTS_PUSH or host.endswith(SUFIJOS_PUSH)

def fecha_nacimiento_valida(v):
    """Vacía = sin fecha. Si trae texto, tiene que ser una fecha real (AAAA-MM-DD)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            raise ValueError("La fecha de nacimiento no es válida (usa AAAA-MM-DD)")
    return v

# 1. ESQUEMAS DE CITAS
class AppointmentBase(BaseModel):
    date: str
    time: str
    reason: str
    status: Optional[str] = "Pendiente"
    pet_id: int # int para que coincida con el ID de la mascota en la DB
    prescription_text: Optional[str] = None

class AppointmentCreate(AppointmentBase):
    # Lo que ENTRA se valida; las respuestas no, para que un dato viejo no rompa la lista.
    reason: str = Field(max_length=1000)
    status: Optional[str] = Field(default="Pendiente", pattern="^(Pendiente|Realizada|Cancelada)$")
    prescription_text: Optional[str] = Field(default=None, max_length=20000)

class Appointment(AppointmentBase):
    id: int
    class Config:
        from_attributes = True

# 1b. CARTILLA (vacunas / desparasitación)
TIPOS_PREVENTIVO = ("vacuna", "desparasitacion", "otro")

class PreventivoBase(BaseModel):
    tipo: str = Field(default="vacuna", pattern="^(vacuna|desparasitacion|otro)$")
    nombre: str = Field(min_length=1, max_length=120)
    applied_date: date
    next_due_date: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=300)

    @field_validator("next_due_date", mode="before")
    @classmethod
    def _vacia_es_nula(cls, v):
        return None if (v is None or (isinstance(v, str) and v.strip() == "")) else v

class PreventivoCreate(PreventivoBase):
    pet_id: int
    product_id: Optional[int] = None

class Preventivo(PreventivoBase):
    id: int
    pet_id: int
    product_id: Optional[int] = None
    reminder_sent_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

# 2. ESQUEMAS DE MASCOTAS 
class PetBase(BaseModel):
    name: str
    species: str
    breed: Optional[str] = None
    birth_date: Optional[Union[date, datetime, str]] = None # str para facilitar el manejo con el input de React
    sex : Optional[str]=  None
    color : Optional[str]= None
    registration_date: str | None = None

    # Si el formulario manda la fecha vacía (""), se guarda como "sin fecha".
    # Antes esto provocaba un error 500 al registrar una mascota sin fecha.
    @field_validator("birth_date", mode="before")
    @classmethod
    def _fecha_vacia_es_nula(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        return v

class PetCreate(PetBase):
    owner_id: int
    name: str = Field(min_length=1, max_length=80)
    species: str = Field(min_length=1, max_length=40)
    breed: Optional[str] = Field(default=None, max_length=80)
    sex: Optional[str] = Field(default=None, max_length=20)
    color: Optional[str] = Field(default=None, max_length=60)

    @field_validator("birth_date", mode="before")
    @classmethod
    def _fecha_valida(cls, v):
        return fecha_nacimiento_valida(v)

class Pet(PetBase):
    id: int
    owner_id: int
    appointments: List[Appointment] = []
    preventivos: List[Preventivo] = []

    # La BD guarda fecha y hora, pero la fecha de nacimiento solo necesita el día:
    # se devuelve como "AAAA-MM-DD" (antes salía "AAAA-MM-DDT00:00:00").
    @field_serializer("birth_date")
    def _solo_fecha(self, v):
        if isinstance(v, (datetime, date)):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, str):
            return v[:10]
        return v
    class Config:
        from_attributes = True 

# 3. ESQUEMAS DE DUEÑOS (Dependen de Pet)
class OwnerCreate(BaseModel):
    name: str
    address: str
    #email: str
    phone: Optional[str] = None

class Owner(BaseModel):
    id: int
    name: str
    address: str
    #email: str
    phone: Optional[str] = None
    pets: List[Pet] = []
 
    class Config:
        from_attributes = True

class ProductBase(BaseModel):
    category: str
    name: str
    quantity: float
    unit: str
    price: float
    application_price: Optional[float] = None   # precio fijo por aplicación (opcional)
    area: Optional[str] = None                  # médico / estética / tienda (vacío = según categoría)
    min_stock: float
    expiration_date: Optional[date] = None

# Las reglas van solo en lo que ENTRA (Create). Las respuestas no validan,
# para que un dato viejo raro en la BD no rompa toda la lista.
class ProductCreate(ProductBase):
    quantity: float = Field(ge=0)   # 0 permitido: un lote puede quedar agotado
    price: float = Field(ge=0)
    application_price: Optional[float] = Field(default=None, ge=0)
    min_stock: float = Field(ge=0)

    @field_validator("area", mode="before")
    @classmethod
    def _area_valida(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        if v not in ("medico", "estetica", "tienda"):
            raise ValueError("El área debe ser medico, estetica o tienda")
        return v

class Product(ProductBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


class SaleBase(BaseModel):
    product_id: int
    quantity: float
    payment_method: str
    total_price: Optional[float] = None

class SaleCreate(SaleBase):
    quantity: float = Field(gt=0)   # una venta siempre es de más de 0 (negativa subía el stock)
    total_price: Optional[float] = Field(default=None, ge=0)

# Un renglón del ticket (lo que se cobra en "Confirmar Venta")
class CheckoutItem(BaseModel):
    product_key: str = Field(min_length=1)   # nombre del artículo (sin el sufijo de lote)
    quantity: float = Field(gt=0)            # cantidad a descontar del inventario
    total: float = Field(ge=0)               # lo que se cobra por este renglón
    payment_method: str = Field(min_length=1)
    # Opcional: si es vacuna/medicamento aplicado a un paciente, se anota en su cartilla
    pet_id: Optional[int] = None
    tipo_preventivo: Optional[str] = Field(default=None, pattern="^(vacuna|desparasitacion|otro)$")
    next_due_date: Optional[date] = None

class CheckoutRequest(BaseModel):
    items: List[CheckoutItem] = Field(min_length=1)

class Sale(SaleBase):
    id: int
    total_price: float
    sale_date: datetime
    ticket_id: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# --- Notificaciones ---
class PushKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=10, max_length=100)

class PushSubscribe(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: PushKeys
    user_agent: Optional[str] = Field(default=None, max_length=300)

    @field_validator("endpoint")
    @classmethod
    def _solo_servicios_push(cls, v):
        if not endpoint_push_valido(v):
            raise ValueError("Dirección de notificaciones no permitida")
        return v

class PushUnsubscribe(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)

class PreventivoUpdate(PreventivoBase):
    pass

class AvisoEnviado(BaseModel):
    tipo: str = Field(pattern="^(confirmacion|recordatorio)$")

class CancelRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)

# Corregir un lote capturado por error (cantidad y/o caducidad)
class LoteUpdate(BaseModel):
    quantity: float = Field(ge=0)
    expiration_date: Optional[date] = None

# --- Sala de Espera pública ---
class RegistroDueño(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=10, max_length=20)
    address: Optional[str] = Field(default="", max_length=200)

class RegistroMascota(PetBase):
    name: str = Field(min_length=1, max_length=80)
    species: str = Field(min_length=1, max_length=40)
    breed: Optional[str] = Field(default=None, max_length=80)
    sex: Optional[str] = Field(default=None, max_length=20)
    color: Optional[str] = Field(default=None, max_length=60)
    registration_date: Optional[str] = Field(default=None, max_length=20)

    @field_validator("birth_date", mode="before")
    @classmethod
    def _fecha_valida(cls, v):
        return fecha_nacimiento_valida(v)

class RegistroPublico(BaseModel):
    owner: RegistroDueño
    pet: RegistroMascota

# ---Esquemas de seguridad ---
class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    password: str = Field(min_length=6, max_length=128)
    admin_password: str = Field(min_length=1)   # la contraseña de Maru, para autorizar

class UserMe(BaseModel):
    id: int
    username: str
    is_admin: bool

class UserResponse(BaseModel):
    id: int
    username: str

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str
