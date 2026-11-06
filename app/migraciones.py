"""
MIGRACIONES SENCILLAS (opción B)
================================
`create_all` crea las tablas que faltan, pero NO agrega columnas nuevas a tablas
que ya existen. Para eso está este archivo: cada vez que el backend arranca
(en tu PC y en Render) revisa esta lista y aplica lo que falte.

REGLAS PARA AGREGAR UNA MIGRACIÓN:
  1. Solo se AGREGA al final de la lista; nunca se borra ni se edita una vieja.
  2. Cada comando debe poder correr muchas veces sin efecto extra
     (usar siempre "IF NOT EXISTS").
  3. Nunca borrar columnas ni datos desde aquí.
"""
from sqlalchemy import text

MIGRACIONES = [
    (
        "2026-11-01 · Precio por aplicación en productos (vacunas/medicamentos)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS application_price DOUBLE PRECISION",
    ),
    (
        "2026-11-03 · Ventas: número de ticket (para cancelar un ticket completo)",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS ticket_id VARCHAR",
    ),
    (
        "2026-11-03 · Ventas: fecha de cancelación",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP WITH TIME ZONE",
    ),
    (
        "2026-11-03 · Ventas: motivo de cancelación",
        "ALTER TABLE sales ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR",
    ),
    (
        "2026-11-03 · Productos: área de negocio (médico, estética, tienda)",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS area VARCHAR",
    ),
    (
        "2026-11-04 · Citas: fecha en que se agendaron (las viejas quedan vacías)",
        "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE",
    ),
    (
        "2026-11-04 · Citas: las nuevas guardan su fecha de creación automáticamente",
        "ALTER TABLE appointments ALTER COLUMN created_at SET DEFAULT now()",
    ),
    (
        "2026-11-04 · Citas: WhatsApp de confirmación enviado",
        "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS confirm_sent_at TIMESTAMP WITH TIME ZONE",
    ),
    (
        "2026-11-04 · Citas: WhatsApp de recordatorio enviado",
        "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMP WITH TIME ZONE",
    ),
]


def aplicar_migraciones(engine):
    """Aplica todas las migraciones en una sola transacción: o se aplican todas o ninguna."""
    with engine.begin() as conn:
        for descripcion, sql in MIGRACIONES:
            conn.execute(text(sql))
            print(f"✔ Migración revisada: {descripcion}")
