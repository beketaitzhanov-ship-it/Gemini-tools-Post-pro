import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise Exception("❌ Ошибка: DATABASE_URL не найден.")

# Обновленная структура таблиц
CREATE_TABLES_SQL = """
-- Таблица грузов с добавленным полем category
CREATE TABLE IF NOT EXISTS shipments (
    contract_num TEXT PRIMARY KEY,       -- Номер договора (CN-...)
    track_number TEXT UNIQUE,            -- Трек-номер склада (GZ/IY/SZ...)
    fio TEXT,
    phone TEXT,
    product TEXT,                        -- Сырое название товара
    category TEXT DEFAULT 'obshhie',     -- 📌 ДОБАВЛЕНО: Категория товара (английский ключ)
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
    media_link TEXT,                     -- Ссылка на Google Drive
    source TEXT DEFAULT 'Direct'         -- Источник заявки
);

-- Таблица заявок от калькулятора
CREATE TABLE IF NOT EXISTS applications (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    name TEXT,
    phone TEXT,
    details TEXT,
    source TEXT,
    city TEXT,
    total_weight REAL,
    total_volume REAL,
    calculated_cost REAL
);

-- Таблица расходов
CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    date DATE DEFAULT CURRENT_DATE,
    category TEXT, -- 'marketing', 'it', 'content', 'office'
    amount REAL,   -- Сумма в $
    description TEXT
);
"""

# SQL для обновления существующей таблицы
ALTER_TABLES_SQL = [
    "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'obshhie';",
    "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'Direct';",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS city TEXT;",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS total_weight REAL;",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS total_volume REAL;",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS calculated_cost REAL;"
]

conn = None
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("✅ Подключено к базе данных...")
    
    # Создаем таблицы
    cursor.execute(CREATE_TABLES_SQL)
    print("✅ Основные таблицы созданы/проверены")
    
    # Обновляем существующие таблицы
    for alter_sql in ALTER_TABLES_SQL:
        try:
            cursor.execute(alter_sql)
            print(f"✅ Выполнено: {alter_sql[:50]}...")
        except Exception as e:
            print(f"⚠️ Предупреждение при выполнении ALTER: {e}")
    
    conn.commit()
    print("🎉 БАЗА ДАННЫХ ГОТОВА К РАБОТЕ!")
    
except Exception as e:
    print(f"❌❌❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
    if conn:
        conn.rollback()
finally:
    if conn:
        cursor.close()
        conn.close()
