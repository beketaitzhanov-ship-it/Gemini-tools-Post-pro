import os
import psycopg2
from dotenv import load_dotenv

# Загружаем настройки (на сервере Render они подтянутся сами)
load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    print("⚠️ Внимание: DATABASE_URL не найден. Убедитесь, что он добавлен в Environment Variables на Render.")

# SQL команды для обновления структуры
UPDATE_SQL = """
-- 1. Разделяем вес
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS declared_weight REAL DEFAULT 0;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS actual_weight REAL DEFAULT 0;

-- 2. Разделяем объем
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS declared_volume REAL DEFAULT 0;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS actual_volume REAL DEFAULT 0;

-- 3. Финансы
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS agreed_rate REAL DEFAULT 0;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS additional_cost REAL DEFAULT 0;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS total_price_final REAL DEFAULT 0;

-- 4. Документы и аналитика
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS contract_num TEXT;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS client_city TEXT;

-- 5. Миграция старых данных (если есть)
UPDATE shipments SET actual_weight = weight WHERE actual_weight = 0 AND weight > 0;
UPDATE shipments SET actual_volume = volume WHERE actual_volume = 0 AND volume > 0;
"""

def update_database():
    conn = None
    try:
        if not DATABASE_URL:
            return

        print("⏳ [Deploy] Проверка структуры базы данных...")
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        cursor.execute(UPDATE_SQL)
        conn.commit()

        print("✅ [Deploy] База данных успешно обновлена/проверена.")

    except Exception as e:
        print(f"❌ [Deploy] Ошибка обновления БД: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            cursor.close()
            conn.close()

if __name__ == '__main__':
    update_database()
