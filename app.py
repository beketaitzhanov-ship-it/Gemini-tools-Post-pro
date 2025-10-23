import os
import re
import json
import logging
import psycopg2
import psycopg2.pool
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import google.generativeai as genai
from google.generativeai.types import GenerationConfig
from dotenv import load_dotenv

# --- 1. Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. Загрузка API ключей ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv('DATABASE_URL') # Загружаем URL базы данных из .env
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'postpro-secret-key-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = 1800

# --- 3. Подключение к PostgreSQL (Новый блок) ---
pool = None
try:
    if DATABASE_URL:
        pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL)
        logger.info(">>> Успешное подключение к PostgreSQL (пул создан).")
    else:
        logger.error("!!! КРИТИЧЕСКАЯ ОШИБКА: DATABASE_URL не найден в .env")
except Exception as e:
    logger.error(f"!!! КРИТИЧЕСКАЯ ОШИБКА: Не удалось подключиться к PostgreSQL: {e}")

# --- 4. Вспомогательные функции для работы с БД ---
def get_db_conn():
    """Берет соединение из пула"""
    if not pool:
        logger.error("Пул соединений не инициализирован.")
        return None
    try:
        return pool.getconn()
    except Exception as e:
        logger.error(f"Ошибка получения соединения из пула: {e}")
        return None

def release_db_conn(conn):
    """Возвращает соединение в пул"""
    if pool and conn:
        pool.putconn(conn)

def query_db(sql, params=None, fetch_one=False):
    """Универсальная функция для выполнения SQL-запросов (ЧТЕНИЕ)"""
    conn = get_db_conn()
    if not conn:
        return None
    
    result = None
    try:
        # Используем 'with' для автоматического закрытия курсора
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            if fetch_one:
                result = cursor.fetchone()
            else:
                result = cursor.fetchall()
    except Exception as e:
        logger.error(f"Ошибка SQL-запроса (ЧТЕНИЕ): {e} | SQL: {sql} | Params: {params}")
    finally:
        release_db_conn(conn)
    return result

def execute_db(sql, params=None):
    """Универсальная функция для выполнения SQL-запросов (ЗАПИСЬ/ИЗМЕНЕНИЕ)"""
    conn = get_db_conn()
    if not conn:
        return False
        
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit() # <-- Сохраняем изменения
        return True
    except Exception as e:
        logger.error(f"Ошибка SQL-запроса (ЗАПИСЬ): {e} | SQL: {sql} | Params: {params}")
        if conn:
            conn.rollback() # Откатываем в случае ошибки
        return False
    finally:
        release_db_conn(conn)

# --- 5. Загрузка промптов (Остается без изменений) ---
def load_personality_prompt():
    """Загружает промпт личности из файла personality_prompt.txt."""
    try:
        with open('personality_prompt.txt', 'r', encoding='utf-8') as f:
            prompt_text = f.read()
            logger.info(">>> Файл personality_prompt.txt успешно загружен.")
            return prompt_text
    except FileNotFoundError:
        logger.error("!!! Файл personality_prompt.txt не найден! Бот будет отвечать стандартно.")
        return "Ты — дружелюбный и профессиональный ассистент логистической компании Post Pro. Общайся вежливо, с лёгким позитивом и эмодзи, как живой человек."

PERSONALITY_PROMPT = load_personality_prompt()

# ВАЖНО: Скопируйте сюда ВЕСЬ ваш SYSTEM_INSTRUCTION из старого app.py
SYSTEM_INSTRUCTION = """
Ты — умный ассистент компании PostPro. Твоя главная цель — помочь клиенту рассчитать стоимость доставки и оформить заявку.

***ВАЖНЫЕ ПРАВИЛА:***

1. **СКЛАДЫ В КИТАЕ:** У нас только 2 склада - ИУ и Гуанчжоу. Если клиент спрашивает "откуда заберете?" - отвечай: "Уточните у вашего поставщика, какой склад ему ближе - ИУ или Гуанчжоу"
2. **ТАРИФЫ:**... (и т.д.) ...
7. **НЕ УПОМИНАЙ:** другие города Китая кроме ИУ и Гуанчжоу

Всегда будь дружелюбным и профессиональным! 😊
"""

# --- 6. Инициализация Gemini (Остается без изменений) ---
model = None
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.0-flash')
        logger.info(">>> Модель Gemini успешно инициализирован.")
    else:
        logger.error("!!! API ключ не найден")
except Exception as e:
    logger.error(f"!!! Ошибка инициализации Gemini: {e}")

# --- 7. НОВЫЙ MultiWarehouseTracker (Работает с БД) ---
class MultiWarehouseTracker:
    def __init__(self):
        # Эта конфигурация маршрутов ОСТАЕТСЯ в коде
        self.warehouses = {
            "GZ": {
                "route": [
                    {"city": "🏭 Гуанчжоу", "day": 0, "progress": 0},
                    {"city": "🚚 Наньчан", "day": 2, "progress": 15},
                    {"city": "🚚 Ухань", "day": 4, "progress": 30},
                    {"city": "🚚 Сиань", "day": 6, "progress": 46},
                    {"city": "🚚 Ланьчжоу", "day": 8, "progress": 61},
                    {"city": "🚚 Урумчи", "day": 10, "progress": 76},
                    {"city": "🛃 Хоргос (граница)", "day": 12, "progress": 85},
                    {"city": "✅ Алматы", "day": 15, "progress": 100}
                ]
            },
            "IY": {
                 "route": [
                    {"city": "🏭 ИУ", "day": 0, "progress": 0},
                    {"city": "🚚 Шанхай", "day": 1, "progress": 25},
                    # ... (и т.д. для ИУ)
                    {"city": "✅ Алматы", "day": 10, "progress": 100}
                ]
            }
        }

    def get_shipment_data_from_db(self, track_number):
        """Загрузка данных груза из PostgreSQL"""
        sql = "SELECT track_number, fio, phone, product, weight, volume, status, route_progress, warehouse_code, manager, created_at FROM shipments WHERE track_number = %s"
        data = query_db(sql, (track_number.upper(),), fetch_one=True)
        
        if not data:
            return None
        
        # Преобразуем кортеж (tuple) в словарь (dict) для удобства
        shipment = {
            "track_number": data[0], "fio": data[1], "phone": data[2],
            "product": data[3], "weight": data[4], "volume": data[5],
            "status": data[6], "route_progress": data[7], 
            "warehouse_code": data[8], "manager": data[9], "created_at": data[10]
        }
        return shipment

    # (Функция create_route_visualization остается ТОЧНО ТАКОЙ ЖЕ, как в app.py.py)
    def create_route_visualization(self, warehouse_code, progress):
        """Создание визуализации маршрута (логика та же)"""
        # Используем 'GZ' как резервный, если код склада не найден
        route_config = self.warehouses.get(warehouse_code, self.warehouses["GZ"])
        route = route_config["route"]
        
        visualization = "🛣️ **МАРШРУТ:**\n\n"
        for point in route:
            if point['progress'] <= progress:
                visualization += f"✅ {point['city']} - день {point['day']}\n"
            else:
                visualization += f"⏳ {point['city']} - день {point['day']}\n"
        
        bars = 20
        filled = int(progress / 100 * bars)
        progress_bar = "🟢" * filled + "⚪" * (bars - filled)
        visualization += f"\n📊 Прогресс: {progress}%\n{progress_bar}\n"
        return visualization

    # (Функция calculate_estimated_arrival остается ТОЧНО ТАКОЙ ЖЕ, как в app.py.py)
    def calculate_estimated_arrival(self, shipment):
        """Расчет примерного времени прибытия"""
        created_at = shipment.get('created_at', datetime.now())
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        if shipment.get('status') == 'доставлен':
            return "✅ Груз уже доставлен"

        current_progress = shipment.get('route_progress', 0)
        total_days = 15  # максимальное время (можно брать из конфига)

        if current_progress >= 100:
            return "🕒 Доставка завершается"

        days_passed = (datetime.now() - created_at).days
        if days_passed >= total_days:
            return "🕒 Скоро прибытие"

        days_left = max(1, total_days - days_passed) # Показываем хотя бы 1 день
        estimated_date = datetime.now() + timedelta(days=days_left)

        return f"📅 {estimated_date.strftime('%d.%m.%Y')} (около {days_left} дней)"

    def get_shipment_info(self, track_number):
        """Получение полной информации о грузе (теперь из БД)"""
        shipment = self.get_shipment_data_from_db(track_number)
        if not shipment:
            return None # Груз не найден

        status_emoji = {
            "принят на складе": "🏭", "в пути до границы": "🚚", "на границе": "🛃",
            "в пути до алматы": "🚛", "прибыл в алматы": "🏙️", "доставлен": "✅"
        }.get(shipment['status'], '📦')

        response = f"📦 **ИНФОРМАЦИЯ О ВАШЕМ ГРУЗЕ**\n\n"
        response += f"🔢 **Трек-номер:** {shipment['track_number']}\n"
        response += f"👤 **Получатель:** {shipment.get('fio', 'Не указано')}\n"
        response += f"📦 **Товар:** {shipment.get('product', 'Не указано')}\n"
        response += f"⚖️ **Вес:** {shipment.get('weight', 0)} кг\n"
        response += f"📏 **Объем:** {shipment.get('volume', 0)} м³\n\n"
        response += f"🔄 **Статус:** {status_emoji} {shipment['status']}\n\n"

        progress = shipment.get('route_progress', 0)
        warehouse_code = shipment.get('warehouse_code', 'GZ')
        response += self.create_route_visualization(warehouse_code, progress)
        response += "\n"
        
        eta = self.calculate_estimated_arrival(shipment) 
        response += f"⏰ **Примерное прибытие:** {eta}\n\n"
        
        response += "💡 _Для уточнений обращайтесь к вашему менеджеру_"
        return response

tracker = MultiWarehouseTracker()

# --- 8. ФУНКЦИИ-ПАРСЕРЫ (Остаются без изменений) ---
# (Скопируйте сюда все ваши extract_... функции из старого app.py)

def extract_dimensions(text):
    """Извлекает габариты (длина, ширина, высота) из текста в любом формате."""
    # (Ваш код extract_dimensions...)
    patterns = [
        r'(?:габарит\w*|размер\w*|дшв|длш|разм)?\s*'
        r'(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?\s*'
        r'[xх*×на\s\-]+\s*'
        r'(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?\s*'
        r'[xх*×на\s\-]+\s*'
        r'(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?'
    ]
    text_lower = text.lower()
    for pattern in patterns:
        matches = re.finditer(pattern, text_lower)
        for match in matches:
            try:
                l, w, h = [float(val.replace(',', '.')) for val in match.groups()]
                match_text = match.group(0).lower()
                has_explicit_m = any(word in match_text for word in ['м', 'm', 'метр'])
                is_cm = 'см' in match_text or 'cm' in match_text or (l > 5 or w > 5 or h > 5) and not has_explicit_m
                if is_cm:
                    l, w, h = l / 100, w / 100, h / 100
                logger.info(f"Извлечены габариты: {l:.3f}x{w:.3f}x{h:.3f} м")
                return l, w, h
            except Exception:
                continue
    return None, None, None # Возвращаем None, если ничего не найдено

def extract_volume(text):
    """Извлекает готовый объем из текста в любом формате."""
    # (Ваш код extract_volume...)
    patterns = [
        r'(\d+(?:[.,]\d+)?)\s*(?:куб\.?\s*м|м³|м3|куб\.?|кубическ\w+\s*метр\w*|кубометр\w*)',
        r'(?:объем|volume)\w*\s*(\d+(?:[.,]\d+)?)\s*(?:куб\.?\s*м|м³|м3|куб\.?)?',
    ]
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                volume = float(match.group(1).replace(',', '.'))
                logger.info(f"Извлечен объем: {volume} м³")
                return volume
            except Exception:
                continue
    return None

def extract_boxes_from_message(message):
    """Извлекает коробки из сообщения."""
    # (Ваш код extract_boxes_from_message...)
    boxes = []
    try:
        text_lower = message.lower().strip()
        pattern_main = r'(\d+)\s*(?:коробк|посылк|упаковк|шт|штук)\w*\s+по\s+(\d+(?:[.,]\d+)?)\s*кг'
        matches = re.findall(pattern_main, text_lower)
        for count, weight in matches:
            for _ in range(int(count)):
                boxes.append({'weight': float(weight.replace(',', '.'))})
        if boxes:
             logger.info(f"📦 Найдено по паттерну 1: {len(boxes)} кор.")
             return boxes
        # (Можно добавить другие паттерны из app.py.py)
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения коробок: {e}")
    return boxes

def parse_product_assignments(message, total_boxes):
    """Парсит распределение товаров по коробкам."""
    # (Ваш код parse_product_assignments...)
    assignments = {}
    try:
        text_lower = message.lower().strip()
        
        # Простой вариант: один товар для всех
        if not any(char.isdigit() for char in text_lower):
            product_type = find_product_category_from_db(text_lower) or text_lower
            logger.info(f"📦 Простой товар для всех коробок: {product_type}")
            for i in range(total_boxes):
                assignments[i] = product_type
            return assignments

        # (Можно добавить другие паттерны из app.py.py)
        # Паттерн "все: товар"
        if 'все:' in text_lower or 'all:' in text_lower:
            product_match = re.search(r'(?:все|all)\s*:\s*([а-яa-z\s]+)', text_lower)
            if product_match:
                product = product_match.group(1).strip()
                logger.info(f"📦 Все коробки: {product}")
                for i in range(total_boxes):
                    assignments[i] = product
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга распределения товаров: {e}")
    return assignments

def extract_contact_info(text):
    """Умное извлечение контактных данных."""
    # (Ваш код extract_contact_info...)
    name, phone = None, None
    clean_text = re.sub(r'\s+', ' ', text.strip()).lower()
    name_match = re.search(r'(?:имя|меня зовут|зовут)\s*[:\-]?\s*([а-яa-z]{2,})', clean_text)
    if name_match:
        name = name_match.group(1).capitalize()
    
    phone_match = re.search(r'(\d{10,11})', clean_text.replace(r'\D', ''))
    if phone_match:
        phone_num = phone_match.group(1)
        if phone_num.startswith('8'): phone_num = '7' + phone_num[1:]
        if len(phone_num) == 10: phone_num = '7' + phone_num
        if len(phone_num) == 11: phone = phone_num

    if not name and phone: # Пытаемся найти имя, если есть только телефон
        name_guess = clean_text.split(phone_match.group(1))[0].strip(' ,')
        if name_guess and not name_guess.isdigit():
             name = name_guess.capitalize()
             
    if not name and not phone: # Простой поиск "Имя Телефон"
        match = re.search(r'([а-яa-z]{2,})\s+(\d{10,11})', clean_text)
        if match:
            name = match.group(1).capitalize()
            phone_num = match.group(2).replace(r'\D', '')
            if phone_num.startswith('8'): phone_num = '7' + phone_num[1:]
            if len(phone_num) == 10: phone_num = '7' + phone_num
            if len(phone_num) == 11: phone = phone_num

    return name, phone

def check_dimensions_exceeded(length, width, height):
    """Проверка габаритов (логика та же)"""
    # (Ваш код check_dimensions_exceeded...)
    if not length or not width or not height:
        return False
    return (length > MAX_DIMENSIONS['length'] or
            width > MAX_DIMENSIONS['width'] or
            height > MAX_DIMENSIONS['height'])

# --- 9. НОВЫЕ ФУНКЦИИ РАСЧЕТА (Работают с БД) ---
MAX_DIMENSIONS = {'length': 2.3, 'width': 1.8, 'height': 1.1} # (Из старого app.py)

def get_exchange_rate_from_db():
    """Получает курс валют из БД"""
    data = query_db("SELECT value FROM settings WHERE key = 'exchange_rate'", fetch_one=True)
    return data[0] if data else 550 # 550 - резервный курс

def get_destination_zone_from_db(city_name):
    """Находит зону в БД по имени города"""
    # Ищем точное совпадение
    sql = "SELECT zone FROM cities WHERE city_name = %s"
    data = query_db(sql, (city_name.lower(),), fetch_one=True)
    if data:
        return data[0]
    
    # Ищем частичное совпадение (если точного нет)
    sql_like = "SELECT zone FROM cities WHERE %s LIKE '%' || city_name || '%'"
    data_like = query_db(sql_like, (city_name.lower(),), fetch_one=True)
    if data_like:
        logger.info(f"Найдена зона (LIKE): {data_like[0]} для {city_name}")
        return data_like[0]
        
    logger.warning(f"Зона для {city_name} не найдена в БД.")
    return None

def find_product_category_from_db(text):
    """Находит категорию товара по тексту (ЗАМЕНА ДЛЯ find_product_category)"""
    # (Эта функция заменяет старую. Ей нужна таблица 'category_keywords')
    # Мы не создали ее в Шаге 1Б, поэтому пока используем ЗАГЛУШКУ
    logger.warning("Используется ЗАГЛУШКА для find_product_category_from_db")
    text_lower = text.lower()
    if any(k in text_lower for k in ["мебель", "диван", "шкаф"]): return "мебель"
    if any(k in text_lower for k in ["техника", "телефон", "ноутбук"]): return "техника"
    if any(k in text_lower for k in ["ткани", "одежда", "вещи", "куртки"]): return "ткани"
    if any(k in text_lower for k in ["косметика", "духи", "крем"]): return "косметика"
    if any(k in text_lower for k in ["автозапчасти", "запчасти", "шина"]): return "автозапчасти"
    return "общие" # Категория по умолчанию

def get_t1_rate_from_db(product_type, weight, volume):
    """Получает тариф Т1 из БД на основе плотности"""
    if not volume or volume <= 0:
        logger.warning("Объем 0, расчет T1 невозможен.")
        return None, 0
        
    density = weight / volume
    
    # Используем новую функцию, работающую (пока) с заглушкой
    category = find_product_category_from_db(product_type) 
    
    sql = """
    SELECT price, unit 
    FROM t1_rates 
    WHERE category_name = %s AND min_density <= %s
    ORDER BY min_density DESC 
    LIMIT 1
    """
    rule = query_db(sql, (category, density), fetch_one=True)
    
    if rule:
        logger.info(f"Найден T1 тариф (SQL): {rule[0]} {rule[1]} для плотности {density:.1f}")
        return {"price": rule[0], "unit": rule[1]}, density
    else:
        logger.warning(f"Не найден T1 тариф (SQL) для {category} / {density:.1f}")
        # Ищем тариф "общие" как резервный
        rule_common = query_db(sql, ("общие", density), fetch_one=True)
        if rule_common:
            logger.info(f"Найден T1 тариф (SQL, Резервный 'общие'): {rule_common[0]} {rule_common[1]}")
            return {"price": rule_common[0], "unit": rule_common[1]}, density
        return None, density


def get_t2_cost_from_db(weight: float, zone: str):
    """Расчет стоимости Т2 по тарифам из БД"""
    if zone == "алматы":
        # (Заглушка, т.к. в T2_RATES_DETAILED нет зоны "алматы")
        logger.warning("Используется резервный тариф Т2 для Алматы (250 тг/кг)")
        return weight * 250 # Временный резервный тариф

    # 1. Находим тариф за доп. кг
    extra_rate_data = query_db("SELECT extra_kg_rate FROM t2_rates_extra WHERE zone = %s", (zone,), fetch_one=True)
    extra_rate = extra_rate_data[0] if extra_rate_data else 300 # Резерв
    
    # 2. Ищем базовый тариф в диапазоне
    cost_column = f"zone_{zone}_cost" # e.g., zone_3_cost
    sql_base = f"""
    SELECT {cost_column} 
    FROM t2_rates 
    WHERE max_weight >= %s 
    ORDER BY max_weight ASC 
    LIMIT 1
    """
    
    base_cost_data = query_db(sql_base, (weight,), fetch_one=True)

    if base_cost_data and weight <= 20:
        # Вес в пределах 20 кг, используем базовую стоимость
        return base_cost_data[0]
    elif weight > 20:
        # Вес больше 20 кг. Берем стоимость за 20 кг и добавляем доп. вес
        sql_20kg = f"SELECT {cost_column} FROM t2_rates WHERE max_weight = 20"
        base_20kg_data = query_db(sql_20kg, fetch_one=True)
        
        # Резерв, если в таблице нет ровно 20 кг
        base_20kg_cost = base_20kg_data[0] if base_20kg_data else (20 * extra_rate) 
        
        remaining_weight = weight - 20
        total_t2_cost = base_20kg_cost + (remaining_weight * extra_rate)
        logger.info(f"Расчет T2 (Зона {zone}): {base_20kg_cost} (база 20кг) + {remaining_weight}кг * {extra_rate} = {total_t2_cost}")
        return total_t2_cost
    else:
        # Если вес превышает максимальный в таблице (e.g. > 20), считаем по доп. тарифу
        return weight * extra_rate

def calculate_quick_cost(weight: float, product_type: str, city: str, volume: float = None, length: float = None, width: float = None, height: float = None):
    """Быстрый расчет (теперь работает с БД)"""
    try:
        # 0. Валидация объема
        if not volume and length and width and height:
            volume = length * width * height
        
        if not volume or volume <= 0 or not weight or weight <= 0:
            logger.error("Вес или объем не указаны, расчет невозможен.")
            return None

        # 1. Получаем курс
        EXCHANGE_RATE = get_exchange_rate_from_db()
        
        # 2. Получаем тариф T1
        rule, density = get_t1_rate_from_db(product_type, weight, volume)
        if not rule:
            logger.error(f"Не удалось получить правило T1 из БД для {product_type}")
            return None

        price = rule['price']
        unit = rule['unit']
        if unit == "kg":
            cost_usd = price * weight
        else: # "m3"
            cost_usd = price * volume
        
        t1_cost_kzt = cost_usd * EXCHANGE_RATE

        # 3. Получаем зону
        zone = get_destination_zone_from_db(city)
        if not zone:
            logger.error(f"Не удалось найти зону для города: {city}")
            return None

        # 4. Получаем тариф T2
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone)) # str(zone) на случай, если зона 'алматы'
        zone_name = f"зона {zone}" if zone != "алматы" else "алматы"
        
        # 5. Итого
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20

        return {
            't1_cost': t1_cost_kzt, 't2_cost': t2_cost_kzt, 'total': total_cost,
            'zone': zone_name, 'volume': volume, 'density': density, 'rule': rule,
            't1_cost_usd': cost_usd, 'length': length, 'width': width, 'height': height,
            'EXCHANGE_RATE': EXCHANGE_RATE # Передаем курс в детальный расчет
        }
    except Exception as e:
        logger.error(f"Критическая ошибка в calculate_quick_cost (SQL): {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def calculate_detailed_cost(quick_cost, weight: float, product_type: str, city: str):
    """Детальный расчет (теперь получает EXCHANGE_RATE из quick_cost)"""
    # (Копируем логику из app.py.py)
    
    if not quick_cost:
        return "Ошибка расчета"

    t1_cost = quick_cost['t1_cost']
    t2_cost = quick_cost['t2_cost']
    zone = quick_cost['zone']
    volume = quick_cost['volume']
    density = quick_cost['density']
    rule = quick_cost['rule']
    t1_cost_usd = quick_cost['t1_cost_usd']
    EXCHANGE_RATE = quick_cost['EXCHANGE_RATE'] # <-- Используем курс из расчета

    price = rule['price']
    unit = rule['unit']
    if unit == "kg":
        calculation_text = f"${price}/кг × {weight} кг = ${t1_cost_usd:.2f} USD"
    else: # "m3"
        calculation_text = f"${price}/м³ × {volume:.3f} м³ = ${t1_cost_usd:.2f} USD"

    city_name = city.capitalize()

    length = quick_cost.get('length')
    width = quick_cost.get('width')
    height = quick_cost.get('height')

    if check_dimensions_exceeded(length, width, height):
        # Груз превышает размеры - только самовывоз
        t2_explanation = f"❌ **Ваш груз превышает максимальный размер посылки 230×180×110 см**\n• Доставка только до склада Алматы (самовывоз)"
        t2_cost = 0 # Обнуляем Т2
        zone_text = "только самовывоз"
        comparison_text = f"💡 **Самовывоз со склада в Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"
    else:
        # Груз в пределах размеров
        if zone == "алматы":
            t2_explanation = f"• Доставка по городу Алматы до вашего адреса"
            zone_text = "город Алматы"
            comparison_text = f"💡 **Если самовывоз со склада в Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"
        else:
            t2_explanation = f"• Доставка до вашего адреса в {city_name}"
            zone_text = f"{zone}"
            comparison_text = f"💡 **Если самовывоз из Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"

    # Пересчитываем итоговую стоимость с учетом (или без) Т2
    if check_dimensions_exceeded(length, width, height):
        total_cost = t1_cost * 1.20  # Только Т1 с комиссией
    else:
        total_cost = (t1_cost + t2_cost) * 1.20

    response = (
        f"📊 **Детальный расчет для {weight} кг «{product_type}» в г. {city_name}:**\n\n"

        f"**Т1: Доставка из Китая до Алматы**\n"
        f"• Плотность вашего груза: **{density:.1f} кг/м³**\n"
        f"• Применен тариф Т1: **${price} за {unit}**\n"
        f"• Расчет: {calculation_text}\n"
        f"• По курсу {EXCHANGE_RATE} тенге/$ = **{t1_cost:.0f} тенге**\n\n"

        f"**Т2: Доставка до двери ({zone_text})**\n"
        f"{t2_explanation}\n"
        f"• Прогрессивный тариф для {weight} кг = **{t2_cost:.0f} тенге**\n\n"

        f"**Комиссия компании (20%):**\n"
        f"• ({t1_cost:.0f} + {t2_cost:.0f}) × 20% = **{(t1_cost + t2_cost) * 0.20:.0f} тенге**\n\n"

        f"------------------------------------\n"
        f"💰 **ИТОГО с доставкой до двери:** ≈ **{total_cost:,.0f} тенге**\n\n"

        f"{comparison_text}\n\n"
        f"💡 **Страхование:** дополнительно 1% от стоимости груза\n"
        f"💳 **Оплата:** пост-оплата при получении\n\n"
        f"✅ **Оставить заявку?** Напишите ваше имя и телефон!\n"
        f"🔄 **Новый расчет?** Напишите **Старт**"
    )
    return response


# --- 10. ПРОЧИЕ ФУНКЦИИ (Оплата, Тарифы) ---
# (Эти функции ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ)
def explain_tariffs():
    return """🚚 **Объяснение тарифов:**

**Т1 - Доставка до склада в Алматы:**
• ... (и т.д.)

**Т2 - Доставка до двери:**
• ... (и т.д.)"""

def get_payment_info():
    return """💳 **Условия оплаты:**

💰 **Пост-оплата:** Вы платите при получении груза... (и т.д.)"""

def get_delivery_procedure():
    return """📦 **Процедура доставки:**

1. **Прием груза в Китае:** ... (и т.д.)"""

# --- 11. НОВАЯ ФУНКЦИЯ СОХРАНЕНИЯ ЗАЯВКИ (Работает с БД) ---
def save_application_to_db(name, phone, details):
    """
    Сохраняет заявку в БД.
    Заменяет save_application()
    """
    sql = """
    INSERT INTO applications (timestamp, name, phone, details) 
    VALUES (NOW(), %s, %s, %s)
    """
    success = execute_db(sql, (name, phone, details))
    
    if success:
        logger.info(f"Заявка сохранена в БД: {name}, {phone}")
    else:
        logger.error(f"Ошибка сохранения заявки в БД: {name}")
    return success

# --- 12. ФУНКЦИЯ GEMINI (Остается без изменений) ---
def get_gemini_response(user_message, context=""):
    """Получает ответ от Gemini для общих вопросов."""
    # (Ваш код get_gemini_response...)
    if not model:
        return "Извините, сейчас я могу отвечать только на вопросы по доставке."
    try:
        multilingual_prompt = f"""
        {PERSONALITY_PROMPT}
        **ВАЖНО: Ты должен понимать и отвечать на русском, казахском, английском и китайском языках.**
        Текущий контекст диалога:
        {context}
        Вопрос клиента: {user_message}
        Твой ответ (соответствуй языку вопроса или используй русский):
        """
        response = model.generate_content(
            multilingual_prompt,
            generation_config=GenerationConfig(max_output_tokens=1000, temperature=0.8)
        )
        return response.text
    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}")
        return "Ой, кажется, у меня что-то пошло не так! Давайте вернемся к расчету доставки. 😊"


# --- 13. НОВАЯ ФУНКЦИЯ ИЗВЛЕЧЕНИЯ ДАННЫХ (Работает с БД) ---
def extract_delivery_info_from_db(text):
    """
    Извлечение данных о доставки с поддержкой тонн.
    Работает с БД для городов и категорий.
    """
    weight = None
    product_type = None
    city = None

    try:
        text_lower = text.lower()
        
        # (Логика извлечения веса остается той же)
        weight_patterns = [
            r'(\d+(?:\.\d+)?)\s*(?:т|тонн|тонны|тонна|тонну|t)',
            r'(\d+(?:\.\d+)?)\s*(?:кг|kg|килограмм|кило)',
        ]
        for pattern in weight_patterns:
            match = re.search(pattern, text_lower)
            if match:
                weight_value = float(match.group(1).replace(',', '.'))
                if re.search(r'(?:т|тонн|тонны|тонна|тонну|t)', text_lower[match.start():match.end()]):
                    weight = weight_value * 1000  # конвертируем тонны в кг
                else:
                    weight = weight_value  # уже в кг
                break

        # НОВОЕ: Ищем город в БД
        all_cities = query_db("SELECT city_name FROM cities")
        if all_cities:
            for city_tuple in all_cities:
                city_name = city_tuple[0]
                if city_name in text_lower:
                    city = city_name
                    break
        
        # НОВОЕ: Ищем категорию в БД (используя заглушку)
        product_type = find_product_category_from_db(text)

        return weight, product_type, city
    except Exception as e:
        logger.error(f"Ошибка извлечения данных (SQL): {e}")
        return None, None, None

# --- 14. ПОШАГОВАЯ ЛОГИКА (Остается, но вызывает НОВЫЕ функции) ---
# (Все функции process_..._step и handle_multi_shipment_steps
#  копируются сюда БЕЗ ИЗМЕНЕНИЙ, т.к. они просто собирают данные в session
#  и в конце вызывают НОВЫЙ calculate_quick_cost)

def process_weight_step(message, session_data):
    """ОБРАБОТКА ШАГА 1: ПОЛУЧЕНИЕ ВЕСА И КОЛИЧЕСТВА"""
    # (Код из app.py.py)
    try:
        boxes = extract_boxes_from_message(message)
        if not boxes:
            return "❌ Не удалось распознать коробки. Укажите в формате: '3 коробки по 20 кг'"
        
        session_data['boxes'] = boxes
        session_data['step'] = 2
        
        box_list = "\n".join([f"• {i+1}. {box['weight']} кг" for i, box in enumerate(boxes)])
        total_weight = sum(box['weight'] for box in boxes)
        
        return f"✅ **Принято {len(boxes)} коробок:**\n{box_list}\n" \
               f"📊 **Общий вес:** {total_weight} кг\n\n" \
               f"📏 **Теперь укажите габариты КАЖДОЙ коробки:**\n" \
               f"_Пример: '60×40×30 см, 50×50×50'_\n" \
               f"💡 **Если все коробки одинаковые:** 'все 60×40×30 см'"
    except Exception as e:
        logger.error(f"❌ Ошибка в process_weight_step: {e}")
        return "❌ Ошибка обработки. Попробуйте еще раз."

def process_dimensions_step(message, session_data):
    """ОБРАБОТКА ШАГА 2: ПОЛУЧЕНИЕ ГАБАРИТОВ"""
    # (Код из app.py.py)
    try:
        boxes = session_data['boxes']
        dimensions_list = [] # Собираем все найденные габариты
        
        # Ищем все комбинации
        pattern = r'(\d+(?:[.,]\d+)?)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)'
        matches = re.findall(pattern, message.lower())
        
        if not matches:
             return "❌ Не удалось распознать габариты. Укажите в формате: '60×40×30 см, 50×50×50'"
             
        for match in matches:
            l, w, h = [float(x.replace(',', '.')) for x in match]
            if l > 10 or w > 10 or h > 10: # Конвертируем см в м
                l, w, h = l/100, w/100, h/100
            dimensions_list.append((l, w, h))

        if len(dimensions_list) != len(boxes) and len(dimensions_list) != 1:
            return f"❌ Указано {len(dimensions_list)} размеров, но нужно {len(boxes)}."
        
        if len(dimensions_list) == 1:
            dims = dimensions_list[0]
            for box in boxes:
                box.update({'length': dims[0], 'width': dims[1], 'height': dims[2], 'volume': dims[0]*dims[1]*dims[2]})
        else:
            for i, box in enumerate(boxes):
                dims = dimensions_list[i]
                box.update({'length': dims[0], 'width': dims[1], 'height': dims[2], 'volume': dims[0]*dims[1]*dims[2]})
        
        session_data['step'] = 3
        boxes_info = "\n".join([f"• {i+1}. {box['weight']} кг - {box['volume']:.3f} м³" for i, box in enumerate(boxes)])
        
        return f"✅ **Габариты установлены:**\n{boxes_info}\n" \
               f"📏 **Общий объем:** {sum(b['volume'] for b in boxes):.3f} м³\n\n" \
               f"📦 **Теперь укажите тип товара для КАЖДОЙ коробки:**\n" \
               f"_Пример: '1-2: одежда, 3: электроника'_\n" \
               f"💡 **Если все коробки одинаковые:** 'все: одежда'"
    except Exception as e:
        logger.error(f"❌ Ошибка в process_dimensions_step: {e}")
        return "❌ Ошибка обработки габаритов."

def process_products_step(message, session_data):
    """ОБРАБОТКА ШАГА 3: ПОЛУЧЕНИЕ ТИПОВ ТОВАРОВ"""
    # (Код из app.py.py)
    try:
        boxes = session_data['boxes']
        product_assignments = parse_product_assignments(message, len(boxes))
        
        if not product_assignments:
            product_type = find_product_category_from_db(message)
            if product_type:
                for i in range(len(boxes)):
                    product_assignments[i] = product_type
            else:
                return "❌ Не удалось распознать типы товаров."
        
        for box_idx, product_type_raw in product_assignments.items():
            if box_idx < len(boxes):
                category = find_product_category_from_db(product_type_raw)
                boxes[box_idx]['product_type'] = category
        
        session_data['step'] = 4
        products_info = "\n".join([f"• {i+1}. {box.get('product_type', 'не указан')}" for i, box in enumerate(boxes)])
        
        return f"✅ **Типы товаров установлены:**\n{products_info}\n\n" \
               f"🏙️ **Теперь укажите город доставки:**\n" \
               f"_Пример: 'Алматы', 'Астана', 'Караганда'_"
    except Exception as e:
        logger.error(f"❌ Ошибка в process_products_step: {e}")
        return "❌ Ошибка обработки типов товаров."

def process_city_step(message, session_data):
    """ОБРАБОТКА ШАГА 4: ПОЛУЧЕНИЕ ГОРОДА И РАСЧЕТ"""
    # (Код из app.py.py, но вызывает НОВЫЙ calculate_quick_cost)
    try:
        boxes = session_data['boxes']
        city = None
        
        # НОВОЕ: Ищем город в БД
        all_cities = query_db("SELECT city_name FROM cities")
        if all_cities:
            for city_tuple in all_cities:
                city_name = city_tuple[0]
                if city_name in message.lower():
                    city = city_name
                    break
        
        if not city:
            return "❌ Город не распознан. Укажите: 'Алматы', 'Астана', 'Караганда' и т.д."
        
        total_cost = 0
        calculations = []
        
        for i, box in enumerate(boxes):
            # ВЫЗЫВАЕМ НОВУЮ ФУНКЦИЮ РАСЧЕТА
            quick_cost = calculate_quick_cost(
                box['weight'],
                box.get('product_type', 'общие'),
                city,
                box.get('volume'),
                box.get('length'),
                box.get('width'), 
                box.get('height')
            )
            
            if quick_cost:
                box_cost = quick_cost['total']
                total_cost += box_cost
                calculations.append({
                    'box_num': i + 1,
                    'description': f"{box['weight']} кг {box.get('product_type', 'общие')}",
                    'cost': box_cost,
                    'volume': box['volume'],
                })
            else:
                logger.error(f"❌ Ошибка расчета (SQL) для коробки {i+1}")
        
        if not calculations:
            return "❌ Не удалось рассчитать стоимость. Проверьте данные и попробуйте снова."
        
        response = f"🎯 **РАСЧЕТ ДЛЯ {len(boxes)} КОРОБОК В {city.upper()}:**\n\n"
        for calc in calculations:
            response += f"📦 **КОРБОКА {calc['box_num']} ({calc['description']}):**\n"
            response += f"   • Объем: {calc['volume']:.3f} м³\n"
            response += f"   • Стоимость: **{calc['cost']:,.0f} ₸**\n\n"
        
        response += f"💰 **ОБЩАЯ СТОИМОСТЬ: {total_cost:,.0f} ₸**\n\n"
        response += "💡 *В стоимость включена доставка до двери и комиссия 20%*\n\n"
        response += "✅ **Оставить заявку? Напишите ваше имя и телефон!**\n"
        response += "🔄 **Новый расчет?** Напишите **Старт**"
        
        session_data['calculation_result_details'] = f"{len(boxes)} коробок в {city}, Общая стоимость: {total_cost:,.0f} ₸"
        session_data['step'] = 0 # Сбрасываем шаг
        session['waiting_for_contacts'] = True # Сразу ждем контакты

        return response

    except Exception as e:
        logger.error(f"❌ Ошибка в process_city_step (SQL): {e}")
        return "❌ Ошибка расчета. Попробуйте начать заново командой 'Старт'."

def handle_multi_shipment_steps(user_message, session_data):
    """
    ГЛАВНАЯ ФУНКЦИЯ ОБРАБОТКИ ПОШАГОВОГО СБОРА ДАННЫХ
    (Код из app.py.py)
    """
    try:
        step = session_data.get('step', 0)
        logger.info(f"🔄 Обработка шага {step} для множественных товаров")
        
        if step == 1:
            return process_weight_step(user_message, session_data)
        elif step == 2:
            return process_dimensions_step(user_message, session_data)
        elif step == 3:
            return process_products_step(user_message, session_data)
        elif step == 4:
            return process_city_step(user_message, session_data)
        else:
            session_data['step'] = 0
            return "❌ Ошибка состояния. Начните новый расчет командой 'Старт'"
            
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_multi_shipment_steps: {e}")
        return "❌ Произошла ошибка. Начните новый расчет командой 'Старт'"


# --- 15. ГЛАВНЫЙ ОБРАБОТЧИК /chat (Переписан для БД) ---
@app.route('/chat', methods=['POST'])
def chat():
    try:
        if not request.json or 'message' not in request.json:
            return jsonify({"response": "❌ Неверный формат запроса"}), 400

        user_message = request.json.get('message', '').strip()
        
        # Инициализация сессий (если их нет)
        if 'multi_shipment' not in session:
            session['multi_shipment'] = {'step': 0, 'boxes': []}
        if 'delivery_data' not in session:
            session['delivery_data'] = {}
        if 'chat_history' not in session:
            session['chat_history'] = []
        if 'waiting_for_contacts' not in session:
            session['waiting_for_contacts'] = False
        if 'calculation_shown' not in session:
            session['calculation_shown'] = False
            
        # Команда Сброса / Старт
        if user_message.lower() in ['старт', 'start', 'сброс', 'новый расчет']:
            session['multi_shipment'] = {'step': 0, 'boxes': []}
            session['delivery_data'] = {}
            session['chat_history'] = []
            session['waiting_for_contacts'] = False
            session['calculation_shown'] = False
            session.pop('quick_cost', None) # Удаляем старый расчет
            session.pop('calculation_result_details', None)
            
            return jsonify({"response": "🔄 Начинаем новый расчет!\n\n📦 **Для расчета укажите 4 параметра:**\n• **Вес** (кг)\n• **Тип товара** (мебель, техника...)\n• **Габариты** (Д×Ш×В) или **Объем** (м³)\n• **Город доставки**\n\n💡 *Или* укажите кол-во коробок (e.g. '3 коробки по 20 кг')"})

        if not user_message:
            return jsonify({"response": "📝 Пожалуйста, введите сообщение."})

        logger.info(f"=== НОВЫЙ ЗАПРОС: {user_message} ===")
        
        # --- 1. Логика пошагового сбора ---
        if session['multi_shipment']['step'] > 0:
            logger.info(f"🔄 Продолжаем сбор данных (шаг {session['multi_shipment']['step']})")
            response = handle_multi_shipment_steps(user_message, session['multi_shipment'])
            session.modified = True # Сохраняем изменения в сессии
            return jsonify({"response": response})
        
        # --- 2. Логика начала пошагового сбора ---
        multiple_boxes = extract_boxes_from_message(user_message)
        if multiple_boxes and len(multiple_boxes) > 0:
            logger.info(f"🎯 Начало нового сбора данных: {len(multiple_boxes)} коробок")
            session['multi_shipment'] = {'step': 1, 'boxes': [], 'current_data': {}}
            response = process_weight_step(user_message, session['multi_shipment'])
            session.modified = True
            return jsonify({"response": response})
            
        # --- 3. Логика отслеживания (НОВАЯ, работает с БД) ---
        tracking_keywords = ['трек', 'отследить', 'статус', 'где', 'заказ', 'посылка', 'груз']
        has_tracking_request = any(keyword in user_message.lower() for keyword in tracking_keywords)
        track_match = re.search(r'\b(GZ|IY|SZ)[a-zA-Z0-9]{6,18}\b', user_message.upper())
        track_number = track_match.group(0) if track_match else None

        if has_tracking_request or track_number:
            if not track_number:
                return jsonify({"response": "📦 Для отслеживания груза мне нужен трек-номер. Пожалуйста, укажите его (например: GZ123456)"})
            
            try:
                # ВЫЗЫВАЕМ НОВЫЙ ТРЕКЕР
                shipment_info = tracker.get_shipment_info(track_number)
                if shipment_info:
                    return jsonify({"response": shipment_info})
                else:
                    return jsonify({"response": f"❌ Груз с трек-номером {track_number} не найден в базе данных."})
            except Exception as e:
                logger.error(f"Ошибка при поиске заказа {track_number} (SQL): {e}")
                return jsonify({"response": f"⚠️ Ошибка при поиске заказа. Попробуйте позже."})

        # --- 4. Логика сбора контактов (НОВАЯ, сохраняет в БД) ---
        if session.get('waiting_for_contacts'):
            name, phone = extract_contact_info(user_message)

            if name and phone:
                details = session.get('calculation_result_details', 'Детали не указаны')
                
                # ВЫЗЫВАЕМ НОВУЮ ФУНКЦИЮ СОХРАНЕНИЯ В БД
                save_application_to_db(name, phone, details)

                # Сбрасываем сессию
                session['multi_shipment'] = {'step': 0, 'boxes': []}
                session['delivery_data'] = {}
                session['chat_history'] = []
                session['waiting_for_contacts'] = False
                session['calculation_shown'] = False
                session.pop('quick_cost', None)
                session.pop('calculation_result_details', None)

                return jsonify({"response": "🎉 Спасибо! Ваша заявка принята. Менеджер свяжется с вами в течение часа. 📞⏰"})
            else:
                return jsonify({"response": "❌ Не удалось распознать контакты. Пожалуйста, укажите в формате: 'Аслан, 87001234567'"})

        # --- 5. Логика общих вопросов (Оплата, Тарифы) ---
        if not session.get('calculation_shown'): # Не отвечаем, если уже показали расчет
            if any(word in user_message.lower() for word in ['оплат', 'платеж', 'kaspi', 'halyk']):
                return jsonify({"response": get_payment_info()})
            if any(word in user_message.lower() for word in ['т1', 'т2', 'тариф', 'объясни']):
                return jsonify({"response": explain_tariffs()})
            if any(word in user_message.lower() for word in ['процедур', 'процесс', 'как достав']):
                return jsonify({"response": get_delivery_procedure()})

        # --- 6. Логика одиночного расчета (НОВАЯ, работает с БД) ---
        
        # Извлекаем данные из сообщения
        weight, product_type, city = extract_delivery_info_from_db(user_message)
        length, width, height = extract_dimensions(user_message)
        volume_direct = extract_volume(user_message)
        
        delivery_data = session.get('delivery_data', {})
        data_updated = False
        confirmation_parts = []

        # Обновляем данные в сессии
        if weight: delivery_data['weight'] = weight; data_updated = True; confirmation_parts.append(f"📊 Вес: {weight} кг")
        if product_type: delivery_data['product_type'] = product_type; data_updated = True; confirmation_parts.append(f"📦 Товар: {product_type}")
        if city: delivery_data['city'] = city; data_updated = True; confirmation_parts.append(f"🏙️ Город: {city.capitalize()}")
        
        if volume_direct:
            delivery_data['volume'] = volume_direct; data_updated = True; confirmation_parts.append(f"📏 Объем: {volume_direct:.3f} м³")
        elif length and width and height:
            delivery_data['volume'] = length * width * height; data_updated = True
            delivery_data.update({'length': length, 'width': width, 'height': height})
            confirmation_parts.append(f"📐 Габариты: {length:.2f}×{width:.2f}×{height:.2f} м (Объем: {delivery_data['volume']:.3f} м³)")

        session['delivery_data'] = delivery_data

        # Проверяем, есть ли все данные
        has_all_data = all([
            delivery_data.get('weight'),
            delivery_data.get('product_type'),
            delivery_data.get('city'),
            delivery_data.get('volume')
        ])

        if has_all_data:
            # ВСЕ ДАННЫЕ ЕСТЬ -> СЧИТАЕМ
            logger.info("Все данные для одиночного расчета собраны, вызов calculate_quick_cost (SQL)...")
            quick_cost = calculate_quick_cost(
                delivery_data['weight'],
                delivery_data['product_type'],
                delivery_data['city'],
                delivery_data.get('volume'),
                delivery_data.get('length'),
                delivery_data.get('width'),
                delivery_data.get('height')
            )

            if quick_cost:
                detailed_response = calculate_detailed_cost(
                    quick_cost,
                    delivery_data['weight'],
                    delivery_data['product_type'],
                    delivery_data['city']
                )
                
                session['quick_cost'] = quick_cost
                session['calculation_shown'] = True
                session['waiting_for_contacts'] = True
                session['calculation_result_details'] = f"{delivery_data['weight']} кг {delivery_data['product_type']} в {delivery_data['city']}"
                
                return jsonify({"response": detailed_response})
            else:
                return jsonify({"response": "❌ Не удалось рассчитать стоимость по тарифам. Проверьте тип товара или город."})
        
        elif data_updated:
            # Данные обновились, но их не хватает
            response_message = "✅ **Данные обновлены:**\n" + "\n".join(confirmation_parts) + "\n\n"
            missing_data = []
            if not delivery_data.get('weight'): missing_data.append("вес")
            if not delivery_data.get('product_type'): missing_data.append("тип товара")
            if not delivery_data.get('volume'): missing_data.append("габариты или объем")
            if not delivery_data.get('city'): missing_data.append("город доставки")
            
            response_message += f"📝 **Осталось указать:** {', '.join(missing_data)}"
            return jsonify({"response": response_message})

        # --- 7. Если ничего не подошло -> Свободный диалог Gemini ---
        logger.info("Ни один триггер не сработал, вызов Gemini...")
        chat_history = session.get('chat_history', [])
        context_lines = ["История диалога:"] + chat_history[-6:]
        context = "\n".join(context_lines)
        
        bot_response = get_gemini_response(user_message, context)
        chat_history.append(f"Клиент: {user_message}")
        chat_history.append(f"Ассистент: {bot_response}")
        session['chat_history'] = chat_history[-10:] # Ограничиваем историю
        
        return jsonify({"response": bot_response})

    except Exception as e:
        logger.error(f"Критическая ошибка /chat: {e}")
        import traceback
        logger.error(f"Трассировка: {traceback.format_exc()}")
        return jsonify({"response": "⚠️ Произошла системная ошибка. Пожалуйста, попробуйте еще раз."})

@app.route('/')
def index():
    """Главная страница с чат-интерфейсом"""
    # (Код из app.py.py)
    return render_template('index.html')

@app.route('/health')
def health_check():
    """Проверка работоспособности для Render"""
    # (Код из app.py.py)
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    # (Код из app.py.py)
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port) # Включаем debug=True для локальной отладки