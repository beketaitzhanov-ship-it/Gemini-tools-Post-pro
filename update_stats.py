import os
import psycopg2
from datetime import datetime
from dotenv import load_dotenv

# Загружаем настройки
load_dotenv()

# 👇 ВСТАВЬ СВОЮ ССЫЛКУ НА БАЗУ СЮДА (если запускаешь на компьютере)
DATABASE_URL = os.getenv('DATABASE_URL') 
# Или жестко: DATABASE_URL = "postgresql://postpro_user:..."

# SQL: Создание таблицы расходов
CREATE_EXPENSES_SQL = """
CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    date DATE DEFAULT CURRENT_DATE,
    category TEXT, -- 'marketing', 'it', 'content', 'office'
    amount REAL,   -- Сумма в $
    description TEXT
);
"""

# SQL: Добавление колонки "Источник" в сделки (Откуда пришел клиент?)
ALTER_SHIPMENTS_SQL = """
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'Direct';
"""

# Твои фиксированные расходы (в месяц)
FIXED_COSTS = [
    ('it', 14.0, 'Hostinger (Сайт)'),
    ('it', 100.0, 'Render (Сервер + БД)'),
    ('it', 20.0, 'Make (Тариф Core)'),
    ('content', 200.0, 'Создание роликов (Veo3/Content)')
]

def update_stats_db():
    if not DATABASE_URL:
        print("❌ Ошибка: Не задан DATABASE_URL. Вставь ссылку в код!")
        return

    conn = None
    try:
        print("⏳ Подключаюсь к базе...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Создаем таблицу расходов
        cur.execute(CREATE_EXPENSES_SQL)
        print("✅ Таблица expenses создана.")

        # 2. Обновляем таблицу грузов (добавляем источник)
        cur.execute(ALTER_SHIPMENTS_SQL)
        print("✅ Таблица shipments обновлена (поле source).")

        # 3. Вносим фиксированные расходы (чтобы статистика не была пустой)
        print(f"💸 Вношу фиксированные расходы ($334)...")
        for category, amount, desc in FIXED_COSTS:
            # Добавляем запись текущей датой
            cur.execute("""
                INSERT INTO expenses (category, amount, description, date)
                VALUES (%s, %s, %s, CURRENT_DATE)
            """, (category, amount, desc))
        
        conn.commit()
        print(f"🎉 Успех! Теперь база готова считать Чистую Прибыль.")

    except Exception as e:
        print(f"❌ Ошибка SQL: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

if __name__ == '__main__':
    update_stats_db()