import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise Exception("Ошибка: DATABASE_URL не найден.")

# SQL: Только таблицы для СДЕЛОК, ЛИДОВ и РАСХОДОВ.
# Тарифы (t1_rates, t2_rates, cities, settings) удалены, т.к. они в config.json
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS shipments (
    contract_num TEXT PRIMARY KEY,       -- Номер договора (CN-...)
    track_number TEXT UNIQUE,            -- Трек-номер склада (GZ/IY/SZ...)
    fio TEXT,
    phone TEXT,
    product TEXT,
    status TEXT,
    route_progress INTEGER DEFAULT 0,
    warehouse_code TEXT,                 -- GZ, FS, или IW
    manager TEXT,
    created_at TIMESTAMP,
    client_city TEXT,
    agreed_rate REAL,                    -- Тариф, зафиксированный в договоре
    declared_weight REAL,
    declared_volume REAL,
    total_price_final REAL,              -- Финальная цена (Факт * Тариф + Допы)
    actual_weight REAL,                  -- Факт. вес
    actual_volume REAL,                  -- Факт. объем
    additional_cost REAL,                -- Доп. услуги ($)
    media_link TEXT                      -- Ссылка на Google Drive
);

CREATE TABLE IF NOT EXISTS applications (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP,
    name TEXT,
    phone TEXT,
    details TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    date DATE,
    category TEXT,
    amount REAL,
    description TEXT
);
"""

conn = None
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("✅ [Migrate] Подключено к базе данных Render...")
    cursor.execute(CREATE_TABLES_SQL)
    conn.commit()
    print("🎉 УСПЕХ! Таблицы (shipments, applications, expenses) созданы.")
except Exception as e:
    print(f"❌❌❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
finally:
    if conn:
        cursor.close()
        conn.close()