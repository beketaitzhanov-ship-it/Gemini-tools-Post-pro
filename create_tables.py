import os
import psycopg2
from dotenv import load_dotenv

# Загружаем DATABASE_URL из .env файла
load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise Exception("Ошибка: DATABASE_URL не найден. Убедитесь, что он есть в .env файле.")

# SQL-команды для создания всех таблиц
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS shipments (
    track_number TEXT PRIMARY KEY,
    fio TEXT,
    phone TEXT,
    product TEXT,
    weight REAL,
    volume REAL,
    status TEXT,
    route_progress INTEGER,
    warehouse_code TEXT,
    manager TEXT,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applications (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP,
    name TEXT,
    phone TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS t1_rates (
    id SERIAL PRIMARY KEY,
    category_name TEXT,
    min_density REAL,
    price REAL,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS cities (
    city_name TEXT PRIMARY KEY,
    zone TEXT
);

CREATE TABLE IF NOT EXISTS t2_rates (
    id SERIAL PRIMARY KEY,
    max_weight REAL,
    zone_1_cost REAL,
    zone_2_cost REAL,
    zone_3_cost REAL,
    zone_4_cost REAL,
    zone_5_cost REAL
);

CREATE TABLE IF NOT EXISTS t2_rates_extra (
    zone TEXT PRIMARY KEY,
    extra_kg_rate REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL
);
"""

conn = None
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("✅ Подключено к базе данных Render...")

    cursor.execute(CREATE_TABLES_SQL)
    conn.commit()

    print("🎉 Успех! Все таблицы созданы (или уже существовали).")

except Exception as e:
    print(f"❌ ОШИБКА: Не удалось создать таблицы: {e}")
    if conn:
        conn.rollback()
finally:
    if conn:
        cursor.close()
        conn.close()
        print("🔌 Соединение с БД закрыто.")