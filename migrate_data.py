import os
import psycopg2
import json
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise Exception("Ошибка: DATABASE_URL не найден. Убедитесь, что он есть в .env файле.")

def migrate_t1_rates(cursor, t1_rates_data):
    print("🚚 Начинаю миграцию T1_RATES_DENSITY...")
    # Очищаем старые данные
    cursor.execute("DELETE FROM t1_rates")

    sql = "INSERT INTO t1_rates (category_name, min_density, price, unit) VALUES (%s, %s, %s, %s)"
    count = 0
    for category, rules in t1_rates_data.items():
        for rule in rules:
            cursor.execute(sql, (category, rule['min_density'], rule['price'], rule['unit']))
            count += 1
    print(f"✅ T1_RATES: {count} правил тарифа перенесено.")

def migrate_cities(cursor, cities_data):
    print("🚚 Начинаю миграцию DESTINATION_ZONES...")
    cursor.execute("DELETE FROM cities")

    sql = "INSERT INTO cities (city_name, zone) VALUES (%s, %s)"
    count = 0
    for city, zone in cities_data.items():
        cursor.execute(sql, (city, str(zone))) # Приводим зону к строке на всякий случай
        count += 1
    print(f"✅ CITIES: {count} городов перенесено.")

def migrate_t2_rates(cursor, t2_data):
    print("🚚 Начинаю миграцию T2_RATES_DETAILED...")
    cursor.execute("DELETE FROM t2_rates")
    cursor.execute("DELETE FROM t2_rates_extra")

    # Переносим таблицу с диапазонами веса
    sql_t2 = """
    INSERT INTO t2_rates (max_weight, zone_1_cost, zone_2_cost, zone_3_cost, zone_4_cost, zone_5_cost) 
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    ranges = t2_data.get('large_parcel', {}).get('weight_ranges', [])
    count = 0
    for r in ranges:
        z = r['zones']
        cursor.execute(sql_t2, (r['max'], z['1'], z['2'], z['3'], z['4'], z['5']))
        count += 1
    print(f"✅ T2_RATES: {count} весовых диапазонов перенесено.")

    # Переносим тарифы за доп. кг
    sql_extra = "INSERT INTO t2_rates_extra (zone, extra_kg_rate) VALUES (%s, %s)"
    extra = t2_data.get('large_parcel', {}).get('extra_kg_rate', {})
    count = 0
    for zone, rate in extra.items():
        cursor.execute(sql_extra, (zone, rate))
        count += 1
    print(f"✅ T2_RATES_EXTRA: {count} тарифов за доп. кг перенесено.")

def migrate_settings(cursor, exchange_rate_data):
    print("🚚 Начинаю миграцию EXCHANGE_RATE...")
    cursor.execute("DELETE FROM settings WHERE key = 'exchange_rate'")
    sql = "INSERT INTO settings (key, value) VALUES (%s, %s)"
    cursor.execute(sql, ('exchange_rate', exchange_rate_data.get('rate', 550)))
    print(f"✅ SETTINGS: Курс валют {exchange_rate_data.get('rate', 550)} установлен.")

def migrate_shipments(cursor, shipment_file_path):
    print(f"🚚 Начинаю миграцию грузов из {shipment_file_path}...")
    try:
        with open(shipment_file_path, 'r', encoding='utf-8') as f:
            shipment_data = json.load(f)
    except Exception as e:
        print(f"❌ ОШИБКА: Не удалось прочитать файл {shipment_file_path}: {e}")
        return

    sql = """
    INSERT INTO shipments (track_number, fio, phone, product, weight, volume, status, route_progress, warehouse_code, manager, created_at) 
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (track_number) DO UPDATE SET
        fio = EXCLUDED.fio,
        phone = EXCLUDED.phone,
        product = EXCLUDED.product,
        weight = EXCLUDED.weight,
        volume = EXCLUDED.volume,
        status = EXCLUDED.status,
        route_progress = EXCLUDED.route_progress,
        manager = EXCLUDED.manager,
        created_at = EXCLUDED.created_at;
    """
    count = 0
    for track, data in shipment_data.items():
        cursor.execute(sql, (
            track,
            data.get('fio'),
            data.get('phone'),
            data.get('product'),
            data.get('weight'),
            data.get('volume'),
            data.get('status'),
            data.get('route_progress'),
            data.get('warehouse', 'GZ'), # Берем 'GZ' из имени файла
            data.get('manager'),
            data.get('created_at')
        ))
        count += 1
    print(f"✅ SHIPMENTS: {count} грузов перенесено/обновлено.")

# --- ГЛАВНЫЙ ЗАПУСК МИГРАЦИИ ---
conn = None
try:
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    print("✅ [Migrate] Подключено к базе данных Render...")

    # 1. Загружаем config.json
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 2. Выполняем миграцию по частям
    migrate_t1_rates(cursor, config.get("T1_RATES_DENSITY", {}))
    migrate_cities(cursor, config.get("DESTINATION_ZONES", {}))
    migrate_t2_rates(cursor, config.get("T2_RATES_DETAILED", {}))
    migrate_settings(cursor, config.get("EXCHANGE_RATE", {}))

    # 3. Выполняем миграцию грузов
    # !! Убедитесь, что путь к файлу верный !!
    migrate_shipments(cursor, 'guangzhou_track_data.json')
    # (Когда появятся другие склады, мы добавим их сюда же)

    # 4. Сохраняем все изменения
    conn.commit()
    print("🎉🎉🎉 УСПЕХ! Все данные успешно перенесены в PostgreSQL на Render!")

except Exception as e:
    print(f"❌❌❌ КРИТИЧЕСКАЯ ОШИБКА МИГРАЦИИ: {e}")
    if conn:
        conn.rollback()
finally:
    if conn:
        cursor.close()
        conn.close()
        print("🔌 [Migrate] Соединение с БД закрыто.")