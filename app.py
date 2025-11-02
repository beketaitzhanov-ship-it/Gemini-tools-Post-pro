# -*- coding: utf-8 -*-
import os
import json
import logging
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from flask_session import Session  
import redis                     
import google.generativeai as genai
import google.generativeai.types as genai_types
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")

app = Flask(__name__)
# ===== НАСТРОЙКА СЕРВЕРНОЙ СЕССИИ (REDIS) =====
# 1. Загружаем URL Redis из переменных окружения
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')

# 2. Конфигурация сессии
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30) #
app.config['SESSION_USE_SIGNER'] = True # Подписываем сессии для безопасности
app.config['SESSION_REDIS'] = redis.from_url(REDIS_URL)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'postpro-secret-key-2024') #

# 3. Инициализируем Flask-Session
Session(app)
# ===== КОНЕЦ НАСТРОЙКИ СЕССИИ =====

# ===== СИСТЕМА ЗАГРУЗКИ С МНОГОУРОВНЕВОЙ ОТКАЗОУСТОЙЧИВОСТЬЮ =====

class ConfigLoader:
    """Класс для безопасной загрузки конфигурации с полной обработкой ошибок"""
    
    @staticmethod
    def load_config():
        """Загружает конфигурацию с проверкой существования файла и валидацией JSON"""
        try:
            if not os.path.exists('config.json'):
                logger.warning("⚠️ Файл config.json не найден, используются значения по умолчанию")
                return None
            
            with open('config.json', 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                logger.info("✅ Файл config.json успешно загружен")
                return config_data
                
        except json.JSONDecodeError as e:
            logger.error(f"❌ Ошибка парсинга config.json: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Критическая ошибка загрузки config.json: {e}")
            return None

    @staticmethod
    def load_prompt_file(filename, description):
        """Универсальный метод загрузки текстовых файлов с полной обработкой ошибок"""
        try:
            if not os.path.exists(filename):
                logger.warning(f"⚠️ Файл {filename} не найден ({description})")
                return ""
            
            with open(filename, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    logger.warning(f"⚠️ Файл {filename} пустой ({description})")
                    return ""
                
                logger.info(f"✅ {description} загружен")
                return content
                
        except UnicodeDecodeError as e:
            logger.error(f"❌ Ошибка кодировки в {filename}: {e}")
            return ""
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return ""

# Загружаем все конфигурации с защитой от ошибок
config = ConfigLoader.load_config()

# Загружаем промпты с гарантией не-None значений
PERSONALITY_PROMPT = ConfigLoader.load_prompt_file(
    'personality_prompt.txt', 
    'Промпт личности Айсулу'
)

CALCULATION_PROMPT = ConfigLoader.load_prompt_file(
    'calculation_prompt.txt', 
    'Промпт расчетов доставки'
)

# Создаем отказоустойчивый системный промпт
def create_aisulu_prompt():
    """Создает финальный промпт с защитой от всех возможных ошибок"""
    base_prompt = ""
    
    # Добавляем personality_prompt если он есть
    if PERSONALITY_PROMPT:
        base_prompt += PERSONALITY_PROMPT + "\n\n"
    
    # Добавляем calculation_prompt если он есть
    if CALCULATION_PROMPT:
        base_prompt += CALCULATION_PROMPT + "\n\n"
    
    # Минимальный fallback промпт если оба файла отсутствуют
    if not base_prompt.strip():
        base_prompt = """Ты - Айсулу, помощник по доставке грузов из Китая в Казахстан.
Используй функцию calculate_delivery_cost для расчетов стоимости доставки."""
        logger.warning("⚠️ Оба промпта отсутствуют, используется fallback промпт")
    
    # Добавляем финальные инструкции
    base_prompt += """
# 🎯 ФИНАЛЬНЫЕ ИНСТРУКЦИИ:

Ты - Айсулу, профессиональный помощник по доставке грузов компании Post Pro.
Следуй правилам из загруженных промптов.
При получении полных данных о доставке НЕМЕДЛЕННО вызывай функцию calculate_delivery_cost.
Всегда указывай информацию о складах Гуанчжоу и Иу с сроками доставки.
"""
    
    return base_prompt

AISULU_PROMPT = create_aisulu_prompt()

# Инициализация конфигурационных переменных с защитой от None
try:
    if config:
        EXCHANGE_RATE = config.get("EXCHANGE_RATE", {}).get("rate", 550)
        DESTINATION_ZONES = config.get("DESTINATION_ZONES", {})
        T1_RATES_DENSITY = config.get("T1_RATES_DENSITY", {})
        T2_RATES = config.get("T2_RATES", {})
        T2_RATES_DETAILED = config.get("T2_RATES_DETAILED", {})
        PRODUCT_CATEGORIES = config.get("PRODUCT_CATEGORIES", {})
    else:
        # Значения по умолчанию с логированием
        logger.warning("⚠️ Конфигурация не загружена, используются значения по умолчанию")
        EXCHANGE_RATE, DESTINATION_ZONES, T1_RATES_DENSITY, T2_RATES, T2_RATES_DETAILED, PRODUCT_CATEGORIES = 550, {}, {}, {}, {}, {}
        
except Exception as e:
    logger.error(f"❌ Ошибка инициализации конфигурационных переменных: {e}")
    # Fallback значения
    EXCHANGE_RATE, DESTINATION_ZONES, T1_RATES_DENSITY, T2_RATES, T2_RATES_DETAILED, PRODUCT_CATEGORIES = 550, {}, {}, {}, {}, {}

# ===== ИНСТРУМЕНТЫ ДЛЯ GEMINI =====
tools = [
    {
        "function_declarations": [
            {
                "name": "calculate_delivery_cost",
                "description": "Рассчитать стоимость доставки из Китая в Казахстан по нашим тарифам",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "weight_kg": {
                            "type": "NUMBER",
                            "description": "Общий вес груза в килограммах"
                        },
                        "city": {
                            "type": "STRING", 
                            "description": "Город доставки в Казахстане: Алматы, Астана, Шымкент и др."
                        },
                        "product_type": {
                            "type": "STRING",
                            "description": "Тип товара: одежда, мебель, техника, косметика, автозапчасти и т.д."
                        },
                        "volume_m3": {
                            "type": "NUMBER",
                            "description": "Объем груза в кубических метрах"
                        },
                        "length_m": {
                            "type": "NUMBER",
                            "description": "Длина груза в метрах"
                        },
                        "width_m": {
                            "type": "NUMBER",
                            "description": "Ширина груза в метрах"
                        },
                        "height_m": {
                            "type": "NUMBER",
                            "description": "Высота груза в метрах"
                        }
                    },
                    "required": ["weight_kg", "city", "product_type"]
                }
            },
            {
                "name": "track_shipment",
                "description": "Отследить статус груза по трек-номеру",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "tracking_number": {
                            "type": "STRING",
                            "description": "Трек-номер груза (начинается с GZ, IY, SZ)"
                        }
                    },
                    "required": ["tracking_number"]
                }
            },
            {
                "name": "get_delivery_terms",
                "description": "Получить информацию о сроках доставки"
            },
            {
                "name": "get_payment_methods", 
                "description": "Получить список доступных способов оплаты"
            },
            {
                "name": "save_customer_application",
                "description": "Сохранить заявку клиента для обратного звонка",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {
                            "type": "STRING",
                            "description": "Имя клиента"
                        },
                        "phone": {
                            "type": "STRING",
                            "description": "Телефон клиента (10-11 цифр)"
                        },
                        "details": {
                            "type": "STRING", 
                            "description": "Дополнительная информация о заявке"
                        }
                    },
                    "required": ["name", "phone"]
                }
            }
        ]
    }
]

# ===== ИНИЦИАЛИЗАЦИЯ GEMINI С ОТКАЗОУСТОЙЧИВОСТЬЮ =====
model = None
gemini_available = False

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.0-flash', tools=tools)
        gemini_available = True
        logger.info("✅ Модель Gemini инициализирована с инструментами")
    else:
        logger.warning("⚠️ GEMINI_API_KEY не найден в переменных окружения")
        
except Exception as e:
    logger.error(f"❌ Критическая ошибка инициализации Gemini: {e}")
    gemini_available = False

# ===== ФУНКЦИИ-ОБРАБОТЧИКИ С УСИЛЕННОЙ ОБРАБОТКОЙ ОШИБОК =====

def find_product_category(text):
    """Поиск категории товара с защитой от ошибок"""
    try:
        if not text or not PRODUCT_CATEGORIES:
            return "общие"
        
        text_lower = text.lower()
        for category, data in PRODUCT_CATEGORIES.items():
            keywords = data.get("keywords", [])
            for keyword in keywords:
                if keyword in text_lower:
                    return category
        return "общие"
        
    except Exception as e:
        logger.error(f"Ошибка определения категории товара: {e}")
        return "общие"

def find_destination_zone(city_name):
    """Определение зоны доставки с защитой от ошибок"""
    try:
        if not city_name or not DESTINATION_ZONES:
            return "5"
        
        city_lower = city_name.lower()
        if city_lower in DESTINATION_ZONES:
            return DESTINATION_ZONES[city_lower]
            
        for city, zone in DESTINATION_ZONES.items():
            if city in city_lower or city_lower in city:
                return zone
        return "5"
        
    except Exception as e:
        logger.error(f"Ошибка определения зоны доставки: {e}")
        return "5"

def get_t1_rate_from_db(product_type, weight, volume):
    """Получение тарифа T1 с полной обработкой ошибок"""
    try:
        if not volume or volume <= 0:
            return None, 0
            
        density = weight / volume
        category = find_product_category(product_type)
        rules = T1_RATES_DENSITY.get(category, T1_RATES_DENSITY.get("общие", []))
        
        if not rules:
            return None, density
            
        for rule in sorted(rules, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= rule.get('min_density', 0):
                return rule, density
        return None, density
        
    except Exception as e:
        logger.error(f"Ошибка получения тарифа T1: {e}")
        return None, 0

def get_t2_cost_from_db(weight, zone):
    """Расчет стоимости T2 с защитой от ошибок"""
    try:
        if not weight or weight <= 0:
            return 0
            
        if zone == "алматы":
            return weight * T2_RATES.get("алматы", 250)
            
        t2_detailed = T2_RATES_DETAILED.get("large_parcel", {})
        weight_ranges = t2_detailed.get("weight_ranges", [])
        extra_rates = t2_detailed.get("extra_kg_rate", {})
        
        if weight_ranges and extra_rates:
            extra_rate = extra_rates.get(zone, 300)
            base_cost = 0
            remaining_weight = weight
            
            for weight_range in weight_ranges:
                max_weight = weight_range.get("max", 0)
                zones = weight_range.get("zones", {})
                
                if weight <= max_weight:
                    base_cost = zones.get(zone, 3000)
                    remaining_weight = 0
                    break
                elif weight > 20 and max_weight == 20:
                    base_cost = zones.get(zone, 4200)
                    remaining_weight = weight - 20
                    
            if remaining_weight > 0:
                base_cost += remaining_weight * extra_rate
                
            return base_cost
        else:
            return weight * T2_RATES.get(zone, 300)
            
    except Exception as e:
        logger.error(f"Ошибка расчета Т2: {e}")
        return weight * 300

def calculate_quick_cost(weight, product_type, city, volume=None, length=None, width=None, height=None):
    """Основная функция расчета с комплексной обработкой ошибок"""
    try:
        # Валидация входных данных
        if not weight or weight <= 0:
            return {"error": "❌ Неверный вес груза"}
        if not product_type:
            return {"error": "❌ Не указан тип товара"}
        if not city:
            return {"error": "❌ Не указан город доставки"}
        
        # Расчет объема
        calculated_volume = volume
        if not calculated_volume and length and width and height:
            if length > 0 and width > 0 and height > 0:
                calculated_volume = length * width * height
        
        if not calculated_volume or calculated_volume <= 0:
            return {"error": "❌ Не удалось рассчитать объем. Укажите объем или размеры (длина, ширина, высота)."}
        
        # Получение тарифа T1
        rule, density = get_t1_rate_from_db(product_type, weight, calculated_volume)
        if not rule:
            rule, density = get_t1_rate_from_db("общие", weight, calculated_volume)
            if not rule:
                return {"error": f"❌ Не найден подходящий тариф для плотности {density:.2f} кг/м³ и категории '{product_type}'."}
        
        # Расчет стоимости T1
        price = rule.get('price', 0)
        unit = rule.get('unit', 'kg')
        
        if unit == "kg":
            cost_usd = price * weight
        else:
            cost_usd = price * calculated_volume
            
        current_rate = EXCHANGE_RATE
        t1_cost_kzt = cost_usd * current_rate
        
        # Расчет стоимости T2
        zone = find_destination_zone(city)
        if not zone:
            return {"error": "❌ Город не найден в зонах доставки"}
            
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone))
        
        # Итоговая стоимость с надбавкой 20%
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20
        
        return {
            'success': True,
            't1_cost_kzt': round(t1_cost_kzt),
            't2_cost_kzt': round(t2_cost_kzt),
            'total_cost_kzt': round(total_cost),
            'zone': f"зона {zone}" if zone != "алматы" else "алматы",
            'volume_m3': round(calculated_volume, 3),
            'density_kg_m3': round(density, 2),
            't1_cost_usd': round(cost_usd, 2),
            'product_type': product_type,
            'city': city,
            'weight_kg': weight
        }
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка расчета стоимости: {e}")
        return {"error": f"❌ Внутренняя ошибка расчета: {str(e)}"}

def process_tracking_request(tracking_number):
    """Обработка запроса отслеживания с обработкой ошибок"""
    try:
        if not tracking_number:
            return {"error": "❌ Не указан трек-номер"}
        
        track_data = {}
        try:
            if os.path.exists('guangzhou_track_data.json'):
                with open('guangzhou_track_data.json', 'r', encoding='utf-8') as f:
                    track_data = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки данных отслеживания: {e}")
        
        shipment = track_data.get(tracking_number)
        if shipment:
            status_emoji = {
                "принят на складе": "🏭",
                "в пути до границы": "🚚", 
                "на границе": "🛃",
                "в пути до алматы": "🚛",
                "прибыл в алматы": "🏙️",
                "доставлен": "✅"
            }.get(shipment.get('status'), '📦')
            
            return {
                'success': True,
                'tracking_number': tracking_number,
                'recipient': shipment.get('fio', 'Не указано'),
                'product': shipment.get('product', 'Не указано'),
                'weight_kg': shipment.get('weight', 0),
                'volume_m3': shipment.get('volume', 0),
                'status': shipment.get('status', 'В обработке'),
                'status_emoji': status_emoji,
                'progress_percent': shipment.get('route_progress', 0)
            }
        else:
            return {"error": f"❌ Груз с трек-номером {tracking_number} не найден"}
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки отслеживания: {e}")
        return {"error": f"❌ Ошибка при поиске груза: {str(e)}"}

def save_application(name, phone, details=None):
    """Сохранение заявки с обработкой ошибок IO"""
    try:
        if not name or not phone:
            return {"error": "❌ Не указаны имя или телефон"}
        
        application_data = {
            'timestamp': datetime.now().isoformat(),
            'name': name.strip(),
            'phone': phone.strip(),
            'details': details.strip() if details else 'Заявка через чат-бота'
        }
        
        try:
            os.makedirs('data', exist_ok=True)
            applications_file = 'data/applications.json'
            applications = []
            
            if os.path.exists(applications_file):
                try:
                    with open(applications_file, 'r', encoding='utf-8') as f:
                        applications = json.load(f)
                except json.JSONDecodeError:
                    logger.warning("Файл applications.json поврежден, создается новый")
                    applications = []
            
            applications.append(application_data)
            
            with open(applications_file, 'w', encoding='utf-8') as f:
                json.dump(applications, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f"Ошибка сохранения заявки в файл: {e}")
            # Продолжаем работу даже при ошибке записи файла
            
        return {
            'success': True,
            'message': f"✅ Заявка от {name} сохранена",
            'application_id': len(applications)
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения заявки: {e}")
        return {"error": f"❌ Ошибка при сохранении заявки: {str(e)}"}

def get_delivery_terms(warehouse=None):
    """Получение условий доставки с fallback"""
    try:
        if warehouse and "гуанчжоу" in warehouse.lower():
            return {
                'success': True,
                'warehouse': 'Гуанчжоу',
                'route': 'Гуанчжоу → Алматы', 
                'transit_time_days': '10-14 дней',
                'total_time_days': '15-20 дней',
                'border_crossing': 'Хоргос'
            }
        else:
            return {
                'success': True,
                'general_terms': 'Доставка из Китая в Казахстан',
                'transit_time_days': '10-20 дней',
                'customs_clearance': '2-3 дня',
                'domestic_delivery': '1-4 дня',
                'warehouses_info': 'У нас есть склады в Гуанчжоу и Иу.'
            }
    except Exception as e:
        logger.error(f"❌ Ошибка получения сроков доставки: {e}")
        return {"error": f"❌ Ошибка получения информации о сроках: {str(e)}"}

def get_payment_methods():
    """Получение способов оплаты с fallback"""
    try:
        return {
            'success': True,
            'payment_methods': [
                'Банковский перевод (Kaspi, Halyk, Freedom Bank)',
                'Онлайн-оплата картой',
                'Alipay & WeChat Pay', 
                'Наличные при получении',
                'Безналичный расчет для ИП и юр.лиц',
                'Криптовалюты (Bitcoin, USDT)',
                'Рассрочка для постоянных клиентов'
            ]
        }
    except Exception as e:
        logger.error(f"❌ Ошибка получения способов оплаты: {e}")
        return {"error": f"❌ Ошибка получения способов оплаты: {str(e)}"}

def execute_tool_function(function_name, parameters):
    """Централизованный исполнитель инструментов с полной обработкой ошибок"""
    try:
        if not function_name:
            return {"error": "❌ Не указано имя функции"}
            
        logger.info(f"🔧 Выполнение инструмента: {function_name} с параметрами: {parameters}")
        
        if function_name == "calculate_delivery_cost":
            return calculate_quick_cost(
                weight=parameters.get('weight_kg'),
                product_type=parameters.get('product_type'),
                city=parameters.get('city'),
                volume=parameters.get('volume_m3'),
                length=parameters.get('length_m'),
                width=parameters.get('width_m'),
                height=parameters.get('height_m')
            )
        
        elif function_name == "track_shipment":
            return process_tracking_request(parameters.get('tracking_number'))
        
        elif function_name == "get_delivery_terms":
            warehouse = parameters.get('warehouse') if parameters else None
            return get_delivery_terms(warehouse)
        
        elif function_name == "get_payment_methods":
            return get_payment_methods()
        
        elif function_name == "save_customer_application":
            return save_application(
                name=parameters.get('name'),
                phone=parameters.get('phone'), 
                details=parameters.get('details')
            )
        
        else:
            return {"error": f"❌ Неизвестный инструмент: {function_name}"}
            
    except Exception as e:
        logger.error(f"❌ Критическая ошибка выполнения инструмента {function_name}: {e}")
        return {"error": f"❌ Внутренняя ошибка выполнения: {str(e)}"}

# ==== ДОБАВЬТЕ ЭТУ ФУНКЦИЮ ЗДЕСЬ ====
def format_calculation_result(result):
    """Красиво форматирует результат расчета"""
    if not result.get('success'):
        return f"❌ {result.get('error', 'Произошла ошибка расчета')}"
    
    return f"""
📊 **Расчет стоимости доставки:**

🏷 **Данные груза:**
• Вес: {result.get('weight_kg')} кг
• Объем: {result.get('volume_m3')} м³  
• Плотность: {result.get('density_kg_m3')} кг/м³
• Тип товара: {result.get('product_type')}
• Город: {result.get('city')} ({result.get('zone')})

💰 **Стоимость:**
• Доставка по Китаю (T1): {result.get('t1_cost_kzt'):,} ₸ (${result.get('t1_cost_usd')})
• Доставка по Казахстану (T2): {result.get('t2_cost_kzt'):,} ₸
• **Итого: {result.get('total_cost_kzt'):,} ₸**

⏱ **Сроки доставки:**
• Склад Гуанчжоу: 15-20 дней
• Склад Иу: 12-18 дней

💎 *Расчет примерный, точную стоимость уточнит менеджер*
"""

# ===== ОСНОВНАЯ ЛОГИКА С УСИЛЕННОЙ ОТКАЗОУСТОЙЧИВОСТЬЮ =====

def get_fallback_response(user_message):
    """Fallback ответы когда Gemini недоступна"""
    message_lower = user_message.lower()
    
    # Определяем интент сообщения для более релевантного ответа
    if any(word in message_lower for word in ['привет', 'здравств', 'салем', 'hello', 'hi']):
        return "Сәлеметсіз бе! 🌸 Я Айсулу, помощник по доставке из Китая в Казахстан. К сожалению, сервис расчетов временно недоступен. Пожалуйста, попробуйте позже или свяжитесь с нами по телефону."
    
    elif any(word in message_lower for word in ['доставк', 'груз', 'посчитай', 'расчет', 'стоимость', 'тариф']):
        return "📦 Для расчета стоимости доставки мне нужны параметры груза: вес, город назначения, тип товара и объем/размеры. В настоящее время сервис расчетов временно недоступен. Пожалуйста, попробуйте позже."
    
    elif any(word in message_lower for word in ['трек', 'отследит', 'номер', 'track']):
        return "📮 Для отслеживания груза нужен трек-номер. В настоящее время сервис отслеживания временно недоступен."
    
    elif any(word in message_lower for word in ['заявк', 'звонок', 'контакт', 'свяж']):
        return "📞 Для оформления заявки нужны ваше имя и телефон. В настоящее время сервис заявок временно недоступен."
    
    else:
        return "🌸 Извините, в настоящее время сервис временно недоступен. Пожалуйста, попробуйте позже или свяжитесь с нами другим способом."

def get_aisulu_response_with_tools(user_message):
    """Основная функция с максимальной отказоустойчивостью"""
    
    # Fallback если Gemini недоступна
    if not gemini_available or not model:
        logger.warning("⚠️ Gemini недоступна, используется fallback режим")
        return get_fallback_response(user_message)
    
    try:
        # Безопасное получение истории диалога
        chat_history_raw = session.get('chat_history', [])
        
        # Создаем структурированные сообщения для Gemini
        messages = []
        
        # 1. Системный промпт с гарантией валидности
        messages.append({
            "role": "user",
            "parts": [{"text": AISULU_PROMPT}]
        })
        
        # Priming ответ для инициализации роли
        messages.append({
            "role": "model", 
            "parts": [{"text": "Сәлеметсіз бе! Я Айсулу. Чем могу помочь? 🌸"}]
        })
        
        # 2. Безопасное добавление истории диалога
        for i in range(0, len(chat_history_raw), 2):
            if i < len(chat_history_raw):
                user_msg = chat_history_raw[i]
                if user_msg.startswith("Клиент: "):
                    messages.append({
                        "role": "user", 
                        "parts": [{"text": user_msg[8:]}] 
                    })
            
            if i + 1 < len(chat_history_raw):
                assistant_msg = chat_history_raw[i + 1]
                if assistant_msg.startswith("Айсулу: "):
                    messages.append({
                        "role": "model",
                        "parts": [{"text": assistant_msg[8:]}]
                    })
        
        # 3. Текущее сообщение пользователя
        messages.append({
            "role": "user",
            "parts": [{"text": user_message}]
        })

        # 4. Безопасный запрос к Gemini
        try:
            response = model.generate_content(
                messages,
                generation_config={'temperature': 0.7}
            )
        except Exception as e:
            logger.error(f"❌ Ошибка запроса к Gemini: {e}")
            return get_fallback_response(user_message)
        
        # 5. Безопасная проверка ответа
        if not (hasattr(response, 'candidates') and response.candidates):
            logger.error("❌ Нет candidates в ответе Gemini")
            return get_fallback_response(user_message)
            
        candidate = response.candidates[0]
        
        if not (hasattr(candidate, 'content') and candidate.content):
            logger.error("❌ Нет content в candidate")
            return get_fallback_response(user_message)
            
        if not (hasattr(candidate.content, 'parts') and candidate.content.parts):
            logger.error("❌ Нет parts в content")
            return get_fallback_response(user_message)
            
        part = candidate.content.parts[0]

        # Сценарий 1: Gemini вызывает инструмент
        if hasattr(part, 'function_call') and part.function_call:
            logger.info("🤖 Gemini вызвал инструмент...")
            function_call = part.function_call
            
            # 1. Безопасное выполнение инструмента
            tool_result = execute_tool_function(
                function_call.name,
                dict(function_call.args) if hasattr(function_call, 'args') else {}
            )
            
            # 2. Конвертируем запрос модели в словарь
            try:
                model_request_dict = genai_types.to_dict(candidate.content)
            except Exception as e:
                logger.warning(f"⚠️ Не удалось конвертировать model_request в dict: {e}")
                model_request_dict = {
                    "role": "model",
                    "parts": [{"function_call": {"name": function_call.name, "args": dict(function_call.args)}}]
                }

            # 3. Создаем ответ функции в виде словаря
            function_response_content = {
                "role": "function",
                "parts": [
                    {
                        "function_response": {
                            "name": function_call.name,
                            "response": tool_result 
                        }
                    }
                ]
            }

            # 4. Собираем обновленную историю
            updated_messages = messages + [model_request_dict, function_response_content]

            # 5. Делаем финальный запрос к Gemini для форматирования
            try:
                final_response = model.generate_content(
                    updated_messages,
                    generation_config={'temperature': 0.7}
                )
                
                # 6. Безопасное извлечение текста
                if (final_response.candidates and 
                    final_response.candidates[0].content and 
                    final_response.candidates[0].content.parts and 
                    final_response.candidates[0].content.parts[0].text):
                    
                    final_text = final_response.candidates[0].content.parts[0].text
                    return final_text
                else:
                    # Fallback: красиво форматируем результат сами
                    return format_calculation_result(tool_result)
                    
            except Exception as e:
                logger.error(f"❌ Ошибка финального запроса: {e}")
                # Fallback: красиво форматируем результат сами
                return format_calculation_result(tool_result)
                    
            except Exception as e:
                logger.error(f"❌ Ошибка финального запроса: {e}")
                # Возвращаем хотя бы результат расчета
                return f"✅ Расчет выполнен! {json.dumps(tool_result, ensure_ascii=False)}"

        # Сценарий 2: Gemini отвечает текстом
        elif hasattr(part, 'text'):
            logger.info("🤖 Gemini ответил текстом...")
            return part.text

        # Сценарий 3: Неизвестный формат ответа
        else:
            logger.error("❌ Неизвестный формат ответа Gemini")
            return get_fallback_response(user_message)
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в get_aisulu_response_with_tools: {e}")
        return get_fallback_response(user_message)

# ===== WEB ЭНДПОИНТЫ С УСИЛЕННОЙ ОБРАБОТКОЙ ОШИБОК =====

@app.route('/')
def index():
    """Главная страница с обработкой ошибок шаблона"""
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки шаблона: {e}")
        return """
        <html>
            <head><title>Айсулу - Помощник по доставке</title></head>
            <body>
                <h1>Айсулу - Помощник по доставке</h1>
                <p>Сервис временно недоступен. Пожалуйста, попробуйте позже.</p>
            </body>
        </html>
        """

@app.route('/chat', methods=['POST'])
def chat():
    """Основной endpoint с максимальной отказоустойчивостью"""
    try:
        # Валидация входных данных
        if not request.json or 'message' not in request.json:
            return jsonify({"response": "❌ Неверный формат запроса"}), 400

        user_message = request.json.get('message', '').strip()
        
        if not user_message:
            return jsonify({"response": "📝 Напишите ваше сообщение"})

        logger.info(f"📨 Получено сообщение: {user_message}")

        # 🚨 ДОБАВЬТЕ ЭТОТ БЛОК ПРЯМО ЗДЕСЬ - ПРИНУДИТЕЛЬНОЕ ОТСЛЕЖИВАНИЕ
        track_number = extract_tracking_number(user_message)
        if track_number:
            logger.info(f"🔍 Обнаружен трек-номер: {track_number}, принудительно отслеживаю")
            tracking_result = process_tracking_request(track_number)
            response_text = format_tracking_for_display(tracking_result)
            
            # Сохраняем в историю
            try:
                if 'chat_history' not in session:
                    session['chat_history'] = []
                session['chat_history'].append(f"Клиент: {user_message}")
                session['chat_history'].append(f"Айсулу: {response_text}")
                
                # Ограничение истории
                if len(session['chat_history']) > 20:
                    session['chat_history'] = session['chat_history'][-16:]
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения истории: {e}")
                
            return jsonify({"response": response_text})
        # 🚨 КОНЕЦ ДОБАВЛЕННОГО БЛОКА

        # Безопасная инициализация сессии
        try:
            if 'chat_history' not in session:
                session['chat_history'] = []
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации сессии: {e}")
            # Продолжаем работу без сессии
            session_backup = []

        # Обработка команды "Старт"
        if user_message.lower() in ['старт', 'start', 'новый расчет', 'сброс', 'баста']:
            try:
                session.clear()
                session['chat_history'] = []
            except Exception as e:
                logger.error(f"❌ Ошибка очистки сессии: {e}")
                
            return jsonify({"response": """
🔄 **Сәлеметсіз бе! Я Айсулу - ваш помощник в доставке!** 🌸

🤖 **Я помогу вам:**
📊 Рассчитать стоимость доставки из Китая
📦 Отследить груз по трек-номеру  
💼 Оформить заявку на доставку
❓ Ответить на вопросы о логистике

**Просто напишите что вам нужно!** 😊
            """})

        # Получение ответа от Айсулу
        response_text = get_aisulu_response_with_tools(user_message)

        # Безопасное сохранение в историю
        try:
            session['chat_history'].append(f"Клиент: {user_message}")
            session['chat_history'].append(f"Айсулу: {response_text}")
            
            # Ограничение истории с защитой от переполнения
            if len(session['chat_history']) > 20:
                session['chat_history'] = session['chat_history'][-16:]
                
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения истории: {e}")

        return jsonify({"response": response_text})

    except Exception as e:
        logger.error(f"❌ Критическая ошибка обработки сообщения: {e}")
        return jsonify({"response": "⚠️ Внутренняя ошибка сервера. Пожалуйста, попробуйте еще раз."}), 500

@app.route('/health')
def health_check():
    """Комплексная проверка здоровья системы"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "components": {
            "gemini_configured": GEMINI_API_KEY is not None,
            "gemini_available": gemini_available,
            "config_loaded": config is not None,
            "model_initialized": model is not None,
            "personality_prompt_loaded": bool(PERSONALITY_PROMPT),
            "calculation_prompt_loaded": bool(CALCULATION_PROMPT),
            "aisulu_prompt_loaded": bool(AISULU_PROMPT.strip())
        },
        "file_checks": {
            "config.json": os.path.exists('config.json'),
            "personality_prompt.txt": os.path.exists('personality_prompt.txt'), 
            "calculation_prompt.txt": os.path.exists('calculation_prompt.txt')
        }
    }
    
    # Определяем общий статус
    critical_components = [health_status["components"]["gemini_configured"]]
    if not all(critical_components):
        health_status["status"] = "degraded"
    
    return jsonify(health_status)

@app.errorhandler(404)
def not_found(error):
    """Обработка 404 ошибок"""
    return jsonify({"error": "Эндпоинт не найден"}), 404

@app.errorhandler(500)
def internal_error(error):
    """Обработка 500 ошибок"""
    logger.error(f"❌ Internal Server Error: {error}")
    return jsonify({"error": "Внутренняя ошибка сервера"}), 500

@app.errorhandler(Exception)
def handle_exception(error):
    """Глобальная обработка исключений"""
    logger.error(f"❌ Необработанное исключение: {error}")
    return jsonify({"error": "Произошла непредвиденная ошибка"}), 500

if __name__ == '__main__':
    # Комплексная проверка перед запуском
    logger.info("🚀 Запуск приложения Айсулу...")
    logger.info(f"📊 Статус компонентов:")
    logger.info(f"  ✅ Gemini API: {'доступен' if gemini_available else 'недоступен'}")
    logger.info(f"  ✅ Конфигурация: {'загружена' if config else 'не загружена'}")
    logger.info(f"  ✅ Промпт личности: {'загружен' if PERSONALITY_PROMPT else 'не загружен'}")
    logger.info(f"  ✅ Промпт расчетов: {'загружен' if CALCULATION_PROMPT else 'не загружен'}")
    logger.info(f"  ✅ Финальный промпт: {'создан' if AISULU_PROMPT.strip() else 'не создан'}")
    
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🌐 Запуск на порту {port}")
    
    try:
        app.run(debug=False, host='0.0.0.0', port=port)
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка запуска приложения: {e}")
