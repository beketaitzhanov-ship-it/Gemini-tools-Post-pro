# -*- coding: utf-8 -*-
import os
import re
import json
import logging
import psycopg2
import psycopg2.pool
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify # Убираем 'session' - больше не нужен
import google.generativeai as genai
from google.generativeai.types import GenerationConfig, HarmCategory, HarmBlockThreshold
from dotenv import load_dotenv

# --- 1. Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. Загрузка API ключей ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv('DATABASE_URL') # Загружаем URL базы данных из .env
app = Flask(__name__)
# app.secret_key = os.getenv('SECRET_KEY', 'postpro-secret-key-2024') # Больше не нужен для session

# --- 3. Подключение к PostgreSQL ---
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
# (Функции get_db_conn, release_db_conn, query_db, execute_db ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ)
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
    if not conn: return None
    result = None
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            if fetch_one: result = cursor.fetchone()
            else: result = cursor.fetchall()
    except Exception as e:
        logger.error(f"Ошибка SQL-запроса (ЧТЕНИЕ): {e} | SQL: {sql} | Params: {params}")
    finally: release_db_conn(conn)
    return result

def execute_db(sql, params=None):
    """Универсальная функция для выполнения SQL-запросов (ЗАПИСЬ/ИЗМЕНЕНИЕ)"""
    conn = get_db_conn()
    if not conn: return False
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка SQL-запроса (ЗАПИСЬ): {e} | SQL: {sql} | Params: {params}")
        if conn: conn.rollback()
        return False
    finally: release_db_conn(conn)

# --- 5. Загрузка промптов (Остается без изменений) ---
# (Функция load_personality_prompt ОСТАЕТСЯ БЕЗ ИЗМЕНЕНИЙ)
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
SYSTEM_INSTRUCTION = """
Ты — умный ИИ-агент компании PostPro Logistics. Твоя главная цель — помочь клиенту **рассчитать стоимость доставки** груза из Китая (склады Гуанчжоу, ИУ) в Казахстан и **отследить** уже отправленный груз. Если клиент хочет **оформить заявку**, помоги ему с этим. Отвечай на **общие вопросы** о компании (оплата, процедура) вежливо и кратко.

***ВАЖНЫЕ ПРАВИЛА АГЕНТА:***

1.  **ИНСТРУМЕНТЫ:** У тебя есть Инструменты (Tools) для:
    * `calculate_delivery_cost`: Расчет стоимости. Вызывай его, ТОЛЬКО когда известны **вес, тип товара, город И (объем ИЛИ габариты)**. Если чего-то не хватает - ЗАПРАШИВАЙ недостающее у клиента вежливо.
    * `track_shipment`: Отслеживание груза. Вызывай его, ТОЛЬКО когда клиент указал **трек-номер** (GZ..., IY...). Если номера нет, но спрашивают про груз - ЗАПРАШИВАЙ номер.
    * `save_application`: Сохранение заявки. Вызывай его, ТОЛЬКО когда клиент ЯВНО согласился оформить заявку **ПОСЛЕ** расчета и предоставил **имя И телефон**.
    * `get_static_info`: Получение информации об оплате, тарифах, процедуре. Вызывай, если клиент спрашивает об этом.
2.  **СКЛАДЫ В КИТАЕ:** ТОЛЬКО Гуанчжоу и ИУ. Если клиент упоминает другой город Китая (Шэньчжэнь и т.д.) - вежливо уточни, на какой из НАШИХ складов (Гуанчжоу или ИУ) ему удобнее отправить. Не пытайся считать из других городов!
3.  **ОПЛАТА:** Всегда пост-оплата при получении (наличные, Kaspi, Halyk, Freedom, безнал). Используй `get_static_info` для деталей.
4.  **ЯЗЫК:** Отвечай на том языке, на котором пишет клиент (русский, казахский, английский, китайский). Понимай числа и параметры на любом из них.
5.  **СТИЛЬ:** Дружелюбный, профессиональный, с эмодзи 😊📦🚚.

**ТВОЯ ЗАДАЧА:** Веди диалог естественно. Анализируй запрос клиента. Если данных для Инструмента хватает - используй его. Если нет - задавай уточняющие вопросы. Если вопрос общий - отвечай сам или используй `get_static_info`.
"""

# --- 6. Инициализация Gemini (БЕЗ ИЗМЕНЕНИЙ) ---
base_model = None # Переименовали, чтобы не путать с model_with_tools
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        base_model = genai.GenerativeModel('models/gemini-2.0-flash') # Или какая у вас модель
        logger.info(">>> Базовая модель Gemini успешно инициализирована.")
    else:
        logger.error("!!! API ключ не найден")
except Exception as e:
    logger.error(f"!!! Ошибка инициализации Gemini: {e}")

# --- 7. MultiWarehouseTracker (ОСТАЕТСЯ БЕЗ ИЗМЕНЕНИЙ) ---
# (Класс MultiWarehouseTracker и его методы get_shipment_data_from_db,
# create_route_visualization, calculate_estimated_arrival, get_shipment_info
# ОСТАЮТСЯ ТОЧНО ТАКИМИ ЖЕ, как в предыдущей версии app.py)
class MultiWarehouseTracker:
    def __init__(self):
        # Эта конфигурация маршрутов ОСТАЕТСЯ в коде
        self.warehouses = {
            "GZ": {"route": [{"city": "🏭 Гуанчжоу", "day": 0, "progress": 0}, {"city": "🚚 Наньчан", "day": 2, "progress": 15}, {"city": "🚚 Ухань", "day": 4, "progress": 30}, {"city": "🚚 Сиань", "day": 6, "progress": 46}, {"city": "🚚 Ланьчжоу", "day": 8, "progress": 61}, {"city": "🚚 Урумчи", "day": 10, "progress": 76}, {"city": "🛃 Хоргос (граница)", "day": 12, "progress": 85}, {"city": "✅ Алматы", "day": 15, "progress": 100}]},
            "IY": {"route": [{"city": "🏭 ИУ", "day": 0, "progress": 0}, {"city": "🚚 Шанхай", "day": 1, "progress": 25}, {"city": "🚚 Нанкин", "day": 2, "progress": 45}, {"city": "🚚 Сиань", "day": 4, "progress": 65}, {"city": "🚚 Ланьчжоу", "day": 6, "progress": 80}, {"city": "🛃 Хоргос (граница)", "day": 8, "progress": 92}, {"city": "✅ Алматы", "day": 10, "progress": 100}]}
            # Добавить SZ позже
        }
    def get_shipment_data_from_db(self, track_number):
        sql = "SELECT track_number, fio, phone, product, weight, volume, status, route_progress, warehouse_code, manager, created_at FROM shipments WHERE track_number = %s"
        data = query_db(sql, (track_number.upper(),), fetch_one=True)
        if not data: return None
        shipment = {"track_number": data[0], "fio": data[1], "phone": data[2], "product": data[3], "weight": data[4], "volume": data[5], "status": data[6], "route_progress": data[7], "warehouse_code": data[8], "manager": data[9], "created_at": data[10]}
        return shipment
    def create_route_visualization(self, warehouse_code, progress):
        route_config = self.warehouses.get(warehouse_code or "GZ", self.warehouses["GZ"]) # Добавил 'or "GZ"'
        route = route_config["route"]
        visualization = "🛣️ **МАРШРУТ:**\n\n"; bars = 20
        for point in route: visualization += f"✅ {point['city']} - день {point['day']}\n" if point['progress'] <= progress else f"⏳ {point['city']} - день {point['day']}\n"
        filled = int(progress / 100 * bars); progress_bar = "🟢" * filled + "⚪" * (bars - filled)
        visualization += f"\n📊 Прогресс: {progress}%\n{progress_bar}\n"
        return visualization
    def calculate_estimated_arrival(self, shipment):
        created_at = shipment.get('created_at', datetime.now())
        if isinstance(created_at, str): created_at = datetime.fromisoformat(created_at)
        if shipment.get('status') == 'доставлен': return "✅ Груз уже доставлен"
        current_progress = shipment.get('route_progress', 0); total_days = 15 # Макс. время
        if current_progress >= 100: return "🕒 Доставка завершается"
        days_passed = (datetime.now() - created_at).days
        if days_passed >= total_days: return "🕒 Скоро прибытие"
        days_left = max(1, total_days - days_passed); estimated_date = datetime.now() + timedelta(days=days_left)
        return f"📅 {estimated_date.strftime('%d.%m.%Y')} (около {days_left} дней)"
    def get_shipment_info(self, track_number):
        shipment = self.get_shipment_data_from_db(track_number)
        if not shipment: return None
        status_emoji = {"принят на складе": "🏭", "в пути до границы": "🚚", "на границе": "🛃", "в пути до алматы": "🚛", "прибыл в алматы": "🏙️", "доставлен": "✅"}.get(shipment['status'], '📦')
        response = f"📦 **ИНФОРМАЦИЯ О ВАШЕМ ГРУЗЕ**\n\n🔢 **Трек-номер:** {shipment['track_number']}\n👤 **Получатель:** {shipment.get('fio', 'Не указано')}\n📦 **Товар:** {shipment.get('product', 'Не указано')}\n⚖️ **Вес:** {shipment.get('weight', 0)} кг\n📏 **Объем:** {shipment.get('volume', 0)} м³\n\n🔄 **Статус:** {status_emoji} {shipment['status']}\n\n"
        progress = shipment.get('route_progress', 0); warehouse_code = shipment.get('warehouse_code', 'GZ')
        response += self.create_route_visualization(warehouse_code, progress) + "\n"
        eta = self.calculate_estimated_arrival(shipment); response += f"⏰ **Примерное прибытие:** {eta}\n\n"
        response += "💡 _Для уточнений обращайтесь к вашему менеджеру_"
        return response
tracker = MultiWarehouseTracker()

# --- 8. ФУНКЦИИ-ПАРСЕРЫ (ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ) ---
# (Функции extract_dimensions, extract_volume, extract_contact_info, check_dimensions_exceeded
# ОСТАЮТСЯ ТОЧНО ТАКИМИ ЖЕ, как в предыдущей версии app.py)
def extract_dimensions(text):
    patterns = [r'(?:габарит\w*|размер\w*|дшв|длш|разм)?\s*(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?\s*[xх*×на\s\-]+\s*(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?\s*[xх*×на\s\-]+\s*(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m|сантиметр\w*|метр\w*)?']
    text_lower = text.lower()
    for pattern in patterns:
        matches = re.finditer(pattern, text_lower)
        for match in matches:
            try:
                l, w, h = [float(val.replace(',', '.')) for val in match.groups()]
                match_text = match.group(0).lower(); has_explicit_m = any(word in match_text for word in ['м', 'm', 'метр'])
                is_cm = 'см' in match_text or 'cm' in match_text or (l > 5 or w > 5 or h > 5) and not has_explicit_m
                if is_cm: l, w, h = l / 100, w / 100, h / 100
                logger.info(f"Извлечены габариты: {l:.3f}x{w:.3f}x{h:.3f} м")
                return l, w, h
            except Exception: continue
    return None, None, None
def extract_volume(text):
    patterns = [r'(\d+(?:[.,]\d+)?)\s*(?:куб\.?\s*м|м³|м3|куб\.?|кубическ\w+\s*метр\w*|кубометр\w*)', r'(?:объем|volume)\w*\s*(\d+(?:[.,]\d+)?)\s*(?:куб\.?\s*м|м³|м3|куб\.?)?']
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                volume = float(match.group(1).replace(',', '.'))
                logger.info(f"Извлечен объем: {volume} м³")
                return volume
            except Exception: continue
    return None
def extract_contact_info(text):
    name, phone = None, None; clean_text = re.sub(r'\s+', ' ', text.strip()).lower()
    name_match = re.search(r'(?:имя|меня зовут|зовут)\s*[:\-]?\s*([а-яa-z]{2,})', clean_text)
    if name_match: name = name_match.group(1).capitalize()
    phone_match = re.search(r'(\d{10,11})', clean_text.replace(r'\D', ''))
    if phone_match:
        phone_num = phone_match.group(1)
        if phone_num.startswith('8'): phone_num = '7' + phone_num[1:]
        if len(phone_num) == 10: phone_num = '7' + phone_num
        if len(phone_num) == 11: phone = phone_num
    if not name and phone: name_guess = clean_text.split(phone_match.group(1))[0].strip(' ,'); name = name_guess.capitalize() if name_guess and not name_guess.isdigit() else name
    if not name and not phone: match = re.search(r'([а-яa-z]{2,})\s+(\d{10,11})', clean_text);
    if match: name = match.group(1).capitalize(); phone_num = match.group(2).replace(r'\D', ''); phone = ('7' + phone_num[1:] if phone_num.startswith('8') else ('7' + phone_num if len(phone_num) == 10 else (phone_num if len(phone_num) == 11 else phone)))
    return name, phone
MAX_DIMENSIONS = {'length': 2.3, 'width': 1.8, 'height': 1.1}
def check_dimensions_exceeded(length, width, height):
    if not length or not width or not height: return False
    return (length > MAX_DIMENSIONS['length'] or width > MAX_DIMENSIONS['width'] or height > MAX_DIMENSIONS['height'])

# --- 9. ФУНКЦИИ РАСЧЕТА (ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ, т.к. уже работают с БД) ---
# (Функции get_exchange_rate_from_db, get_destination_zone_from_db,
# find_product_category_from_db (с ЗАГЛУШКОЙ), get_t1_rate_from_db, get_t2_cost_from_db,
# calculate_quick_cost, calculate_detailed_cost ОСТАЮТСЯ ТОЧНО ТАКИМИ ЖЕ,
# как в предыдущей версии app.py)
def get_exchange_rate_from_db():
    data = query_db("SELECT value FROM settings WHERE key = 'exchange_rate'", fetch_one=True)
    return data[0] if data else 550
def get_destination_zone_from_db(city_name):
    sql = "SELECT zone FROM cities WHERE city_name = %s"; data = query_db(sql, (city_name.lower(),), fetch_one=True)
    if data: return data[0]
    sql_like = "SELECT zone FROM cities WHERE %s LIKE '%' || city_name || '%'" # Проверяем вхождение
    data_like = query_db(sql_like, (city_name.lower(),), fetch_one=True)
    if data_like: logger.info(f"Найдена зона (LIKE): {data_like[0]} для {city_name}"); return data_like[0]
    logger.warning(f"Зона для {city_name} не найдена в БД."); return None
def find_product_category_from_db(text): # ЗАГЛУШКА
    logger.warning("Используется ЗАГЛУШКА для find_product_category_from_db")
    text_lower = text.lower();
    if any(k in text_lower for k in ["мебель", "диван", "шкаф"]): return "мебель"
    if any(k in text_lower for k in ["техника", "телефон", "ноутбук"]): return "техника"
    if any(k in text_lower for k in ["ткани", "одежда", "вещи", "куртки"]): return "ткани"
    if any(k in text_lower for k in ["косметика", "духи", "крем"]): return "косметика"
    if any(k in text_lower for k in ["автозапчасти", "запчасти", "шина"]): return "автозапчасти"
    return "общие"
def get_t1_rate_from_db(product_type, weight, volume):
    if not volume or volume <= 0: logger.warning("Объем 0, расчет T1 невозможен."); return None, 0
    density = weight / volume; category = find_product_category_from_db(product_type)
    sql = "SELECT price, unit FROM t1_rates WHERE category_name = %s AND min_density <= %s ORDER BY min_density DESC LIMIT 1"
    rule = query_db(sql, (category, density), fetch_one=True)
    if rule: logger.info(f"Найден T1 тариф (SQL): {rule[0]} {rule[1]} для плотности {density:.1f}"); return {"price": rule[0], "unit": rule[1]}, density
    else: logger.warning(f"Не найден T1 тариф (SQL) для {category} / {density:.1f}"); rule_common = query_db(sql, ("общие", density), fetch_one=True);
    if rule_common: logger.info(f"Найден T1 тариф (SQL, Резервный 'общие'): {rule_common[0]} {rule_common[1]}"); return {"price": rule_common[0], "unit": rule_common[1]}, density
    return None, density
def get_t2_cost_from_db(weight: float, zone: str):
    if zone == "алматы": logger.warning("Используется резервный тариф Т2 для Алматы (250 тг/кг)"); return weight * 250
    extra_rate_data = query_db("SELECT extra_kg_rate FROM t2_rates_extra WHERE zone = %s", (zone,), fetch_one=True); extra_rate = extra_rate_data[0] if extra_rate_data else 300
    cost_column = f"zone_{zone}_cost"; sql_base = f"SELECT {cost_column} FROM t2_rates WHERE max_weight >= %s ORDER BY max_weight ASC LIMIT 1"
    base_cost_data = query_db(sql_base, (weight,), fetch_one=True)
    if base_cost_data and weight <= 20: return base_cost_data[0]
    elif weight > 20: sql_20kg = f"SELECT {cost_column} FROM t2_rates WHERE max_weight = 20"; base_20kg_data = query_db(sql_20kg, fetch_one=True); base_20kg_cost = base_20kg_data[0] if base_20kg_data else (20 * extra_rate); remaining_weight = weight - 20; total_t2_cost = base_20kg_cost + (remaining_weight * extra_rate); logger.info(f"Расчет T2 (Зона {zone}): {base_20kg_cost} (база 20кг) + {remaining_weight}кг * {extra_rate} = {total_t2_cost}"); return total_t2_cost
    else: return weight * extra_rate
def calculate_quick_cost(weight: float, product_type: str, city: str, volume: float = None, length: float = None, width: float = None, height: float = None):
    try:
        if not volume and length and width and height: volume = length * width * height
        if not volume or volume <= 0 or not weight or weight <= 0: logger.error("Вес или объем не указаны, расчет невозможен."); return None
        EXCHANGE_RATE = get_exchange_rate_from_db(); rule, density = get_t1_rate_from_db(product_type, weight, volume)
        if not rule: logger.error(f"Не удалось получить правило T1 из БД для {product_type}"); return None
        price = rule['price']; unit = rule['unit']; cost_usd = price * weight if unit == "kg" else price * volume; t1_cost_kzt = cost_usd * EXCHANGE_RATE
        zone = get_destination_zone_from_db(city)
        if not zone: logger.error(f"Не удалось найти зону для города: {city}"); return None
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone)); zone_name = f"зона {zone}" if zone != "алматы" else "алматы"
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20
        return {'t1_cost': t1_cost_kzt, 't2_cost': t2_cost_kzt, 'total': total_cost, 'zone': zone_name, 'volume': volume, 'density': density, 'rule': rule, 't1_cost_usd': cost_usd, 'length': length, 'width': width, 'height': height, 'EXCHANGE_RATE': EXCHANGE_RATE, 'product_type': product_type, 'city': city, 'weight': weight} # Добавили product_type, city, weight
    except Exception as e: logger.error(f"Критическая ошибка в calculate_quick_cost (SQL): {e}"); import traceback; logger.error(traceback.format_exc()); return None
# (Функция calculate_detailed_cost ОСТАЕТСЯ ТОЧНО ТАКОЙ ЖЕ, как в предыдущей версии app.py)
def calculate_detailed_cost(quick_cost, weight: float, product_type: str, city: str):
    if not quick_cost: return "Ошибка расчета"
    t1_cost = quick_cost['t1_cost']; t2_cost = quick_cost['t2_cost']; zone = quick_cost['zone']; volume = quick_cost['volume']; density = quick_cost['density']; rule = quick_cost['rule']; t1_cost_usd = quick_cost['t1_cost_usd']; EXCHANGE_RATE = quick_cost['EXCHANGE_RATE']
    price = rule['price']; unit = rule['unit']; calculation_text = f"${price}/кг × {weight} кг = ${t1_cost_usd:.2f} USD" if unit == "kg" else f"${price}/м³ × {volume:.3f} м³ = ${t1_cost_usd:.2f} USD"; city_name = city.capitalize()
    length = quick_cost.get('length'); width = quick_cost.get('width'); height = quick_cost.get('height')
    if check_dimensions_exceeded(length, width, height): t2_explanation = f"❌ **Ваш груз превышает максимальный размер посылки 230×180×110 см**\n• Доставка только до склада Алматы (самовывоз)"; t2_cost = 0; zone_text = "только самовывоз"; comparison_text = f"💡 **Самовывоз со склада в Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"; total_cost = t1_cost * 1.20
    else:
        if zone == "алматы": t2_explanation = f"• Доставка по городу Алматы до вашего адреса"; zone_text = "город Алматы"; comparison_text = f"💡 **Если самовывоз со склада в Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"
        else: t2_explanation = f"• Доставка до вашего адреса в {city_name}"; zone_text = f"{zone}"; comparison_text = f"💡 **Если самовывоз из Алматы:** {t1_cost * 1.20:.0f} тенге (включая комиссию 20%)"
        total_cost = (t1_cost + t2_cost) * 1.20
    response = (f"📊 **Детальный расчет для {weight} кг «{product_type}» в г. {city_name}:**\n\n**Т1: Доставка из Китая до Алматы**\n• Плотность вашего груза: **{density:.1f} кг/м³**\n• Применен тариф Т1: **${price} за {unit}**\n• Расчет: {calculation_text}\n• По курсу {EXCHANGE_RATE} тенге/$ = **{t1_cost:.0f} тенге**\n\n**Т2: Доставка до двери ({zone_text})**\n{t2_explanation}\n• Прогрессивный тариф для {weight} кг = **{t2_cost:.0f} тенге**\n\n**Комиссия компании (20%):**\n• ({t1_cost:.0f} + {t2_cost:.0f}) × 20% = **{(t1_cost + t2_cost) * 0.20:.0f} тенге**\n\n------------------------------------\n💰 **ИТОГО с доставкой до двери:** ≈ **{total_cost:,.0f} тенге**\n\n{comparison_text}\n\n💡 **Страхование:** дополнительно 1% от стоимости груза\n💳 **Оплата:** пост-оплата при получении\n\n✅ **Оставить заявку?** Напишите ваше имя и телефон!\n🔄 **Новый расчет?** Напишите **Старт**")
    return response

# --- 10. ФУНКЦИИ СТАТИЧЕСКОЙ ИНФОРМАЦИИ (ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ) ---
# (Функции explain_tariffs, get_payment_info, get_delivery_procedure ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ)
def explain_tariffs(): return """🚚 **Объяснение тарифов:**\n\n**Т1 - Доставка до склада в Алматы:**\n• ... (Ваш текст)\n\n**Т2 - Доставка до двери:**\n• ... (Ваш текст)"""
def get_payment_info(): return """💳 **Условия оплаты:**\n\n💰 **Пост-оплата:** ... (Ваш текст)"""
def get_delivery_procedure(): return """📦 **Процедура доставки:**\n\n1. **Прием груза в Китае:** ... (Ваш текст)"""

# --- 11. ФУНКЦИЯ СОХРАНЕНИЯ ЗАЯВКИ (ОСТАЕТСЯ БЕЗ ИЗМЕНЕНИЙ, т.к. уже работает с БД) ---
# (Функция save_application_to_db ОСТАЕТСЯ ТОЧНО ТАКОЙ ЖЕ, как в предыдущей версии app.py)
def save_application_to_db(name, phone, details):
    sql = "INSERT INTO applications (timestamp, name, phone, details) VALUES (NOW(), %s, %s, %s)"
    success = execute_db(sql, (name, phone, details))
    if success: logger.info(f"Заявка сохранена в БД: {name}, {phone}")
    else: logger.error(f"Ошибка сохранения заявки в БД: {name}")
    return success

# --- 12. ФУНКЦИЯ GEMINI ДЛЯ СВОБОДНОГО ДИАЛОГА (ОСТАЕТСЯ БЕЗ ИЗМЕНЕНИЙ) ---
# (Функция get_gemini_response ОСТАЕТСЯ ТОЧНО ТАКОЙ ЖЕ, как в предыдущей версии app.py)
def get_gemini_response(user_message, history):
    if not base_model: return "Извините, сейчас я могу отвечать только на вопросы по доставке."
    try:
        # Формируем историю для модели
        gemini_history = []
        for i, msg in enumerate(history):
             # Проверяем, есть ли префикс 'Клиент:' или 'Ассистент:'
             if msg.startswith('Клиент: '):
                 gemini_history.append({'role': 'user', 'parts': [msg[len('Клиент: '):]]})
             elif msg.startswith('Ассистент: '):
                 gemini_history.append({'role': 'model', 'parts': [msg[len('Ассистент: '):]]})
             else: # Если префикса нет, предполагаем роль по четности/нечетности
                 gemini_history.append({'role': 'user' if i % 2 == 0 else 'model', 'parts': [msg]})

        # Начинаем чат с историей
        chat = base_model.start_chat(history=gemini_history)
        response = chat.send_message(
            f"{SYSTEM_INSTRUCTION}\n\nВопрос клиента: {user_message}\n\nТвой ответ:", # Добавляем инструкцию к последнему сообщению
            generation_config=GenerationConfig(max_output_tokens=1000, temperature=0.7),
            safety_settings={ # Снижаем порог блокировки
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
        )
        return response.text
    except Exception as e:
        logger.error(f"Ошибка Gemini (свободный диалог): {e}")
        return "Ой, кажется, у меня что-то пошло не так! Давайте вернемся к расчету доставки. 😊"

# --- 13. НОВЫЙ БЛОК: ИНСТРУМЕНТЫ GEMINI И ИХ "ПАСПОРТА" ---
# (Код определения инструментов и их "паспортов" из предыдущего ответа)

# --- Функции-обертки для Инструментов ---
def calculate_delivery_cost_tool_wrapper(weight: float, product_type: str, city: str, volume: float = None, length: float = None, width: float = None, height: float = None):
    """Обертка для calculate_quick_cost, возвращающая JSON-строку с СЛОВАРЕМ РАСЧЕТА."""
    logger.info(f"🤖 Tool вызвал calculate_delivery_cost с: w={weight}, p='{product_type}', c='{city}', v={volume}, l={length}, wi={width}, h={height}")
    try:
        # Коррекция габаритов (если пришли в см)
        if length and length > 5: length /= 100
        if width and width > 5: width /= 100
        if height and height > 5: height /= 100
        
        # Вызываем нашу функцию расчета, работающую с БД
        result_dict = calculate_quick_cost(weight, product_type, city, volume, length, width, height)
        if result_dict:
            # Возвращаем СЛОВАРЬ результата как JSON-строку
            return json.dumps(result_dict, ensure_ascii=False)
        else:
            return json.dumps({"error": "Не удалось рассчитать стоимость. Проверьте данные (город, тип товара). Возможно, город не поддерживается или тип товара не найден."})
    except Exception as e:
        logger.error(f"❌ Ошибка в calculate_delivery_cost_tool_wrapper: {e}"); import traceback; logger.error(traceback.format_exc())
        return json.dumps({"error": f"Внутренняя ошибка расчета: {e}"})

def track_shipment_tool_wrapper(track_number: str):
    """Обертка для tracker.get_shipment_info, возвращающая JSON-строку с ГОТОВЫМ ТЕКСТОМ."""
    logger.info(f"🤖 Tool вызвал track_shipment с: track='{track_number}'")
    try:
        # Вызываем наш трекер, работающий с БД
        result_text = tracker.get_shipment_info(track_number)
        if result_text:
            # Возвращаем ГОТОВЫЙ ТЕКСТ как JSON-строку
            return json.dumps({"shipment_info_text": result_text}, ensure_ascii=False)
        else:
            # Добавляем стандартные префиксы к поиску
            prefixes = ['GZ', 'IY', 'SZ']
            found = False
            for prefix in prefixes:
                 if not track_number.upper().startswith(prefix):
                      test_track = prefix + track_number.upper()
                      result_text = tracker.get_shipment_info(test_track)
                      if result_text:
                          found = True
                          return json.dumps({"shipment_info_text": result_text}, ensure_ascii=False)
            if not found:
                 return json.dumps({"error": f"Груз с трек-номером {track_number} (или с префиксами GZ/IY/SZ) не найден."})
                 
    except Exception as e:
        logger.error(f"❌ Ошибка в track_shipment_tool_wrapper: {e}")
        return json.dumps({"error": f"Внутренняя ошибка отслеживания: {e}"})

def save_application_tool_wrapper(name: str, phone: str, details: str = "Детали из чата"):
    """Обертка для save_application_to_db, возвращающая JSON с подтверждением."""
    logger.info(f"🤖 Tool вызвал save_application с: name='{name}', phone='{phone}', details='{details}'")
    try:
        # Извлекаем имя и телефон еще раз на всякий случай
        extracted_name, extracted_phone = extract_contact_info(f"{name} {phone}")
        if not extracted_name or not extracted_phone:
             logger.warning(f"Не удалось извлечь контакты из: {name} {phone}")
             # Пытаемся использовать то, что передал Gemini
             final_name = name
             final_phone = phone # Оставляем как есть, надеясь на Gemini
        else:
            final_name = extracted_name
            final_phone = extracted_phone

        # Вызываем нашу функцию сохранения в БД
        success = save_application_to_db(final_name, final_phone, details)
        if success:
            return json.dumps({"confirmation_text": f"🎉 Спасибо, {final_name}! Ваша заявка принята. Менеджер свяжется с вами по номеру {final_phone} в рабочее время (9:00-19:00 Астана). 📞⏰"}, ensure_ascii=False)
        else:
            return json.dumps({"error": "Не удалось сохранить заявку в базу данных."})
    except Exception as e:
        logger.error(f"❌ Ошибка в save_application_tool_wrapper: {e}")
        return json.dumps({"error": f"Внутренняя ошибка сохранения заявки: {e}"})

def get_static_info_tool_wrapper(topic: str):
    """Обертка для получения статической информации (оплата, тарифы, процедура)."""
    logger.info(f"🤖 Tool вызвал get_static_info с: topic='{topic}'")
    topic_lower = topic.lower()
    response_text = "Извините, я не нашел информацию по этой теме."
    try:
        if "оплат" in topic_lower or "платеж" in topic_lower:
            response_text = get_payment_info()
        elif "тариф" in topic_lower or "т1" in topic_lower or "т2" in topic_lower:
            response_text = explain_tariffs()
        elif "процедур" in topic_lower or "процесс" in topic_lower or "доставк" in topic_lower:
            response_text = get_delivery_procedure()
        
        return json.dumps({"info_text": response_text}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Ошибка в get_static_info_tool_wrapper: {e}")
        return json.dumps({"error": f"Внутренняя ошибка получения информации: {e}"})

# --- Словарь и "Паспорта" Инструментов ---
available_tools = {
    "calculate_delivery_cost": calculate_delivery_cost_tool_wrapper,
    "track_shipment": track_shipment_tool_wrapper,
    "save_application": save_application_tool_wrapper,
    "get_static_info": get_static_info_tool_wrapper
}

tools_declaration = [
    genai.Tool(
        function_declarations=[
            # Паспорт 1: Расчет Стоимости
            genai.FunctionDeclaration(
                name="calculate_delivery_cost",
                description="Рассчитать точную стоимость доставки груза из Китая (Гуанчжоу/ИУ) в город Казахстана. Требует вес, тип товара, город И (объем ИЛИ габариты д*ш*в). Не вызывай, если чего-то не хватает!",
                parameters=genai.Schema(
                    type=genai.Type.OBJECT,
                    properties={
                        "weight": genai.Schema(type=genai.Type.NUMBER, description="Общий вес груза в КГ (обязательно)."),
                        "product_type": genai.Schema(type=genai.Type.STRING, description="Категория товара (обязательно), например 'мебель', 'одежда', 'техника'. Используй 'общие', если неясно."),
                        "city": genai.Schema(type=genai.Type.STRING, description="Город доставки в Казахстане (обязательно), например 'Астана', 'Алматы', 'Караганда'."),
                        "volume": genai.Schema(type=genai.Type.NUMBER, description="Общий объем груза в м³ (кубических метрах). Указывать ТОЛЬКО если ИЗВЕСТЕН точный объем."),
                        "length": genai.Schema(type=genai.Type.NUMBER, description="Длина ОДНОГО места груза в метрах ИЛИ сантиметрах (если объем неизвестен). Указывать ТОЛЬКО если volume не указан."),
                        "width": genai.Schema(type=genai.Type.NUMBER, description="Ширина ОДНОГО места груза в метрах ИЛИ сантиметрах (если объем неизвестен). Указывать ТОЛЬКО если volume не указан."),
                        "height": genai.Schema(type=genai.Type.NUMBER, description="Высота ОДНОГО места груза в метрах ИЛИ сантиметрах (если объем неизвестен). Указывать ТОЛЬКО если volume не указан.")
                    },
                    required=["weight", "product_type", "city"] # Объем/габариты Gemini должен извлечь и передать, если они есть
                )
            ),
            # Паспорт 2: Отслеживание Груза
            genai.FunctionDeclaration(
                name="track_shipment",
                description="Отследить груз по трек-номеру и получить его текущий статус, местоположение и маршрут. Требует трек-номер.",
                parameters=genai.Schema(
                    type=genai.Type.OBJECT,
                    properties={ "track_number": genai.Schema(type=genai.Type.STRING, description="Трек-номер груза (обязательно), например GZ123456, IY789012.") },
                    required=["track_number"]
                )
            ),
             # Паспорт 3: Сохранение Заявки
            genai.FunctionDeclaration(
                name="save_application",
                description="Сохранить заявку клиента в базу данных ПОСЛЕ успешного расчета и ЯВНОГО согласия клиента. Требует имя и телефон.",
                parameters=genai.Schema(
                    type=genai.Type.OBJECT,
                    properties={
                        "name": genai.Schema(type=genai.Type.STRING, description="Имя клиента (обязательно)."),
                        "phone": genai.Schema(type=genai.Type.STRING, description="Номер телефона клиента (обязательно)."),
                        "details": genai.Schema(type=genai.Type.STRING, description="Краткие детали расчета (опционально, например '50кг мебель в Астану').")
                    },
                    required=["name", "phone"]
                )
            ),
            # Паспорт 4: Статическая Информация
            genai.FunctionDeclaration(
                name="get_static_info",
                description="Получить информацию об условиях оплаты, процедуре доставки или объяснении тарифов.",
                parameters=genai.Schema(
                    type=genai.Type.OBJECT,
                    properties={
                        "topic": genai.Schema(type=genai.Type.STRING, description="Тема запроса (обязательно): 'оплата', 'тарифы' или 'процедура'.")
                    },
                    required=["topic"]
                )
            ),
        ]
    )
]

# --- 14. ИНИЦИАЛИЗАЦИЯ МОДЕЛИ С ИНСТРУМЕНТАМИ ---
model_with_tools = None
try:
    if base_model: # Если базовая модель загрузилась
        model_with_tools = genai.GenerativeModel(
            # Используем ту же базовую модель
            model_name=base_model.model_name,
            # Добавляем инструкции и инструменты
            system_instruction=SYSTEM_INSTRUCTION,
            tools=tools_declaration,
            safety_settings={ # Снижаем порог блокировки для модели с инструментами тоже
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
        )
        logger.info(">>> Модель Gemini С ИНСТРУМЕНТАМИ успешно инициализирована.")
    else:
        logger.error("!!! Базовая модель Gemini не загружена, инструменты не подключены.")
except Exception as e:
    logger.error(f"!!! Ошибка инициализации Gemini с инструментами: {e}")


# --- 15. НОВЫЙ ОБРАБОТЧИК /chat (ИСПОЛЬЗУЕТ TOOLS) ---
@app.route('/chat', methods=['POST'])
def chat():
    # Проверяем инициализацию модели
    if not model_with_tools:
        return jsonify({"response": "⚠️ Ошибка: Модель ИИ (Tools) не инициализирована."}), 500
        
    try:
        user_message = request.json.get('message', '').strip()
        if not user_message:
            return jsonify({"response": "📝 Пожалуйста, введите сообщение."})

        # Команда Сброса / Старт (ОСТАВЛЯЕМ ДЛЯ УДОБСТВА)
        if user_message.lower() in ['старт', 'start', 'сброс', 'новый расчет']:
            logger.info(">>> Получена команда СТАРТ/СБРОС.")
            # Просто возвращаем приветствие, Gemini сам начнет диалог заново
            return jsonify({"response": "🔄 Начинаем новый расчет!\n\n📦 **Для расчета укажите 4 параметра:**\n• **Вес** (кг)\n• **Тип товара** (мебель, техника...)\n• **Габариты** (Д×Ш×В) или **Объем** (м³)\n• **Город доставки**\n\n💡 *Или* просто опишите ваш груз."})

        logger.info(f"=== НОВЫЙ ЗАПРОС (TOOLS): {user_message} ===")
        
        # --- ИСТОРИЯ ЧАТА (Новая логика, пока простая) ---
        # Мы НЕ используем Flask session. Историю нужно будет хранить в БД или передавать каждый раз.
        # Для ПРОСТОТЫ пока будем работать БЕЗ истории между запросами.
        # TODO: Реализовать хранение истории чата в БД по user_id (из WhatsApp/Telegram).
        chat_history_for_gemini = [] # Пустая история для каждого запроса

        # --- ЗАПУСК АГЕНТА ---
        chat_session = model_with_tools.start_chat(
             history=chat_history_for_gemini
             # enable_automatic_function_calling=True # Можно включить
        )
        last_successful_tool_result = None # Сохраняем результат последнего инструмента
        last_tool_name = None

        # Отправляем сообщение клиента модели
        response = chat_session.send_message(user_message)

        # --- ГЛАВНЫЙ ЦИКЛ ОБРАБОТКИ ВЫЗОВОВ ФУНКЦИЙ ---
        while response.candidates[0].content.parts[0].function_call:
            function_call = response.candidates[0].content.parts[0].function_call
            function_name = function_call.name
            args_dict = {key: value for key, value in function_call.args.items()}
            logger.info(f"🤖 Агент решил вызвать: {function_name} с аргументами: {args_dict}")

            # Безопасно вызываем НАШУ функцию-обертку
            if function_name in available_tools:
                function_to_call = available_tools[function_name]
                try:
                    api_response_json_str = function_to_call(**args_dict)
                    logger.info(f"✅ Результат JSON от {function_name}: {api_response_json_str}")
                    # Сохраняем результат УСПЕШНОГО вызова
                    last_successful_tool_result = json.loads(api_response_json_str) # Парсим JSON
                    last_tool_name = function_name
                except TypeError as te:
                     logger.error(f"❌ Ошибка TypeError при вызове {function_name}: {te}. Args: {args_dict}")
                     api_response_json_str = json.dumps({"error": f"Ошибка: неверные аргументы для {function_name}. Проверьте тип данных."})
                     last_successful_tool_result = None # Сбрасываем при ошибке
                     last_tool_name = None
                except Exception as e:
                    logger.error(f"❌ Ошибка выполнения {function_name}: {e}"); import traceback; logger.error(traceback.format_exc())
                    api_response_json_str = json.dumps({"error": f"Внутренняя ошибка при выполнении {function_name}."})
                    last_successful_tool_result = None
                    last_tool_name = None
            else:
                logger.error(f"⚠️ Модель попыталась вызвать неизвестную функцию: {function_name}")
                api_response_json_str = json.dumps({"error": f"Ошибка: неизвестный инструмент {function_name}."})
                last_successful_tool_result = None
                last_tool_name = None

            # Отправляем РЕЗУЛЬТАТ работы нашей функции ОБРАТНО модели
            response = chat_session.send_message(
                genai.Part(
                    function_response=genai.FunctionResponse(
                        name=function_name,
                        response={"result": api_response_json_str} # Отправляем как строку
                    )
                )
            )

        # --- КОНЕЦ ЦИКЛА ---

        # --- ОБРАБОТКА ФИНАЛЬНОГО ОТВЕТА ---
        final_response_text = response.text
        logger.info(f"💬 Финальный ответ Gemini (до обработки): {final_response_text}")

        # --- СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ РАСЧЕТА СТОИМОСТИ ---
        # Если последним успешным инструментом был расчет, ИСПОЛЬЗУЕМ ДЕТАЛЬНЫЙ РАСЧЕТ
        if last_tool_name == "calculate_delivery_cost" and last_successful_tool_result and "error" not in last_successful_tool_result:
             logger.info("🛠️ Последний инструмент был расчетом, вызываем calculate_detailed_cost...")
             # Вызываем нашу функцию детального расчета, передавая ей СЛОВАРЬ quick_cost
             try:
                 # Извлекаем нужные параметры из словаря quick_cost
                 quick_cost_data = last_successful_tool_result
                 # Достаем параметры, которые были переданы в calculate_quick_cost
                 weight = quick_cost_data.get('weight')
                 product_type = quick_cost_data.get('product_type')
                 city = quick_cost_data.get('city')
                 
                 if weight and product_type and city:
                      detailed_text = calculate_detailed_cost(quick_cost_data, weight, product_type, city)
                      final_response_text = detailed_text # ЗАМЕНЯЕМ ответ Gemini на наш детальный
                      logger.info("✅ Детальный расчет успешно сформирован и подставлен.")
                 else:
                      logger.warning("Не хватило данных в результате quick_cost для детального расчета.")
                      # Оставляем как есть ответ Gemini, он должен содержать результат quick_cost
                 
             except Exception as e:
                 logger.error(f"❌ Ошибка при вызове calculate_detailed_cost после Tool: {e}")
                 # Оставляем как есть ответ Gemini, но логгируем ошибку

        # --- СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ ДРУГИХ ИНСТРУМЕНТОВ ---
        # Если последним был трекер, заявка или инфо, берем текст из их результата
        elif last_tool_name == "track_shipment" and last_successful_tool_result and "shipment_info_text" in last_successful_tool_result:
            final_response_text = last_successful_tool_result["shipment_info_text"]
        elif last_tool_name == "save_application" and last_successful_tool_result and "confirmation_text" in last_successful_tool_result:
            final_response_text = last_successful_tool_result["confirmation_text"]
        elif last_tool_name == "get_static_info" and last_successful_tool_result and "info_text" in last_successful_tool_result:
            final_response_text = last_successful_tool_result["info_text"]
        elif last_successful_tool_result and "error" in last_successful_tool_result:
             # Если инструмент вернул ошибку, попросим Gemini ее перефразировать
             error_message = last_successful_tool_result["error"]
             logger.warning(f"Инструмент вернул ошибку: {error_message}")
             # Отправляем ошибку Gemini для перефразирования (как свободный диалог)
             history_for_error = chat_history_for_gemini + [
                 {'role': 'user', 'parts': [user_message]},
                 {'role': 'model', 'parts': [f'Произошла ошибка при обработке: {error_message}. Сообщи об этом клиенту вежливо.']}
             ]
             final_response_text = get_gemini_response(f"Перефразируй ошибку: {error_message}", history_for_error)


        # --- ОТПРАВКА ОТВЕТА КЛИЕНТУ ---
        logger.info(f"✅ Финальный ответ для клиента: {final_response_text}")
        return jsonify({"response": final_response_text})

    except Exception as e:
        logger.error(f"Критическая ошибка /chat (Tools): {e}")
        import traceback
        logger.error(f"Трассировка: {traceback.format_exc()}")
        return jsonify({"response": "⚠️ Произошла системная ошибка (Агент). Пожалуйста, попробуйте еще раз."}), 500

# --- Остальные роуты (@app.route('/'), @app.route('/health')) ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # Убираем debug=True для продакшена на Render
    app.run(host='0.0.0.0', port=port)