import os
import psycopg2
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')

# SQL: Создание таблицы расходов
CREATE_EXPENSES_SQL = """
CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    date DATE DEFAULT CURRENT_DATE,
    category TEXT,
    amount REAL,
    description TEXT
);
"""

# SQL: Добавление недостающих колонок
ALTER_TABLES_SQL = [
    "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'obshhie';",
    "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'Direct';",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS city TEXT;",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS total_weight REAL;", 
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS total_volume REAL;",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS calculated_cost REAL;"
]

# Фиксированные расходы (в месяц)
FIXED_COSTS = [
    ('it', 14.0, 'Hostinger (Сайт)'),
    ('it', 100.0, 'Render (Сервер + БД)'),
    ('it', 20.0, 'Make (Тариф Core)'),
    ('content', 200.0, 'Создание роликов (Veo3/Content)')
]

def update_stats_db():
    if not DATABASE_URL:
        print("❌ Ошибка: Не задан DATABASE_URL.")
        return

    conn = None
    try:
        print("⏳ Подключаюсь к базе...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Создаем таблицу расходов если её нет
        cur.execute(CREATE_EXPENSES_SQL)
        print("✅ Таблица expenses создана/проверена.")

        # 2. Обновляем структуру таблиц
        print("🔄 Обновляю структуру таблиц...")
        for alter_sql in ALTER_TABLES_SQL:
            try:
                cur.execute(alter_sql)
                print(f"✅ Выполнено: {alter_sql[:50]}...")
            except Exception as e:
                print(f"⚠️ Предупреждение: {e}")

        # 3. Вносим фиксированные расходы (если их еще нет)
        print("💸 Проверяю фиксированные расходы...")
        for category, amount, desc in FIXED_COSTS:
            # Проверяем, есть ли уже такая запись за текущий месяц
            cur.execute("""
                SELECT id FROM expenses 
                WHERE category = %s AND amount = %s AND description = %s 
                AND date >= DATE_TRUNC('month', CURRENT_DATE)
            """, (category, amount, desc))
            
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO expenses (category, amount, description, date)
                    VALUES (%s, %s, %s, CURRENT_DATE)
                """, (category, amount, desc))
                print(f"✅ Добавлен расход: {desc} - ${amount}")
        
        conn.commit()
        print("🎉 БАЗА ДАННЫХ ОБНОВЛЕНА И ГОТОВА К РАБОТЕ!")
        
        # 4. Показываем статистику
        print("\n📊 ТЕКУЩАЯ СТАТИСТИКА:")
        
        # Количество грузов
        cur.execute("SELECT COUNT(*) FROM shipments")
        shipments_count = cur.fetchone()[0]
        print(f"📦 Грузов в базе: {shipments_count}")
        
        # Количество заявок
        cur.execute("SELECT COUNT(*) FROM applications") 
        apps_count = cur.fetchone()[0]
        print(f"📝 Заявок в базе: {apps_count}")
        
        # Статусы грузов
        cur.execute("SELECT status, COUNT(*) FROM shipments GROUP BY status")
        status_stats = cur.fetchall()
        print("🚚 Статусы грузов:")
        for status, count in status_stats:
            print(f"  - {status}: {count}")

    except Exception as e:
        print(f"❌ Ошибка SQL: {e}")
        if conn: 
            conn.rollback()
    finally:
        if conn: 
            conn.close()

if __name__ == '__main__':
    update_stats_db()