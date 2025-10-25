# -*- coding: utf-8 -*-
import os
import re
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import google.generativeai as genai
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
app.secret_key = os.getenv('SECRET_KEY', 'postpro-secret-key-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)

# ===== ЗАГРУЗКА КОНФИГУРАЦИИ =====
def load_config():
    """Загружает конфигурацию из файла config.json."""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            logger.info("✅ Файл config.json успешно загружен")
            return config_data
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки config.json: {e}")
        return None

config = load_config()

if config:
    EXCHANGE_RATE = config.get("EXCHANGE_RATE", {}).get("rate", 550)
    DESTINATION_ZONES = config.get("DESTINATION_ZONES", {})
    T1_RATES_DENSITY = config.get("T1_RATES_DENSITY", {})
    T2_RATES = config.get("T2_RATES", {})
    T2_RATES_DETAILED = config.get("T2_RATES_DETAILED", {})
    PRODUCT_CATEGORIES = config.get("PRODUCT_CATEGORIES", {})
    GREETINGS = config.get("GREETINGS", [])
else:
    logger.error("⚠️ Приложение запускается с значениями по умолчанию")
    EXCHANGE_RATE, DESTINATION_ZONES, T1_RATES_DENSITY, T2_RATES, T2_RATES_DETAILED, PRODUCT_CATEGORIES, GREETINGS = 550, {}, {}, {}, {}, {}, []

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def find_product_category(text):
    """Находит категорию товара по тексту"""
    if not text:
        return "общие"
    
    text_lower = text.lower()
    for category, data in PRODUCT_CATEGORIES.items():
        for keyword in data["keywords"]:
            if keyword in text_lower:
                return category
    return "общие"

def find_destination_zone(city_name):
    """Находит зону назначения по городу"""
    if not city_name:
        return "5"
    
    city_lower = city_name.lower()
    
    # Прямой поиск
    if city_lower in DESTINATION_ZONES:
        return DESTINATION_ZONES[city_lower]
    
    # Поиск по вхождению
    for city, zone in DESTINATION_ZONES.items():
        if city in city_lower or city_lower in city:
            return zone
    
    return "5"  # зона по умолчанию

def extract_dimensions(text):
    """Извлекает габариты из текста"""
    patterns = [
        r'(\d+(?:[.,]\d+)?)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)',
        r'(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m)\s*[xх*×]\s*(\d+(?:[.,]\d+)?)\s*(?:см|cm|м|m)'
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                l, w, h = [float(x.replace(',', '.')) for x in match.groups()]
                # Конвертация см в метры если нужно
                if l > 10 or w > 10 or h > 10:
                    l, w, h = l/100, w/100, h/100
                return l, w, h
            except:
                continue
    return None, None, None

def extract_volume(text):
    """Извлекает объем из текста"""
    patterns = [
        r'(\d+(?:[.,]\d+)?)\s*(?:куб|м³|м3|м\^3)',
        r'объем\w*\s*(\d+(?:[.,]\d+)?)'
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                return float(match.group(1).replace(',', '.'))
            except:
                continue
    return None

def extract_weight(text):
    """Извлекает вес из текста"""
    patterns = [
        r'(\d+(?:[.,]\d+)?)\s*(?:кг|kg|килограмм)',
        r'вес\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:кг)?',
        r'(\d+(?:[.,]\d+)?)\s*(?:т|тонн|тонны)'
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                weight = float(match.group(1).replace(',', '.'))
                # Конвертация тонн в кг
                if 'т' in pattern or 'тонн' in pattern:
                    weight *= 1000
                return weight
            except:
                continue
    return None

def extract_city(text):
    """Извлекает город из текста"""
    text_lower = text.lower()
    for city in DESTINATION_ZONES.keys():
        if city in text_lower:
            return city
    return None

def extract_boxes_from_message(message):
    """Извлекает информацию о коробках"""
    boxes = []
    try:
        text_lower = message.lower()
        
        # Паттерн: "N коробок по X кг"
        pattern = r'(\d+)\s*(?:коробк|посылк|упаковк|шт|штук)\w*\s+по\s+(\d+(?:[.,]\d+)?)\s*кг'
        matches = re.findall(pattern, text_lower)
        
        for count, weight in matches:
            box_count = int(count)
            box_weight = float(weight.replace(',', '.'))
            
            for i in range(box_count):
                boxes.append({
                    'weight': box_weight,
                    'product_type': None,
                    'volume': None,
                    'description': f"Коробка {i+1}"
                })
        
        return boxes
    except Exception as e:
        logger.error(f"Ошибка извлечения коробок: {e}")
        return []

def extract_pallets_from_message(message):
    """Извлекает информацию о паллетах"""
    try:
        text_lower = message.lower()
        
        # Паттерн: "N паллет"
        pallet_match = re.search(r'(\d+)\s*паллет\w*', text_lower)
        if pallet_match:
            pallet_count = int(pallet_match.group(1))
            
            # Стандартные параметры паллета
            STANDARD_PALLET = {
                'weight': 500,  # кг
                'volume': 1.2,  # м³
                'description': 'Стандартная паллета'
            }
            
            pallets = []
            for i in range(pallet_count):
                pallets.append({
                    'weight': STANDARD_PALLET['weight'],
                    'volume': STANDARD_PALLET['volume'],
                    'product_type': 'мебель',  # по умолчанию для паллет
                    'description': f'Паллета {i+1}'
                })
            
            return pallets
        
        return []
    except Exception as e:
        logger.error(f"Ошибка извлечения паллет: {e}")
        return []

# ===== ФУНКЦИИ РАСЧЕТА СТОИМОСТИ =====
def get_t1_rate_from_db(product_type, weight, volume):
    """Получает тариф Т1 из конфига"""
    if not volume or volume <= 0:
        return None, 0
    
    density = weight / volume
    category = find_product_category(product_type)
    rules = T1_RATES_DENSITY.get(category, T1_RATES_DENSITY.get("общие", []))
    
    if not rules:
        return None, density
    
    for rule in sorted(rules, key=lambda x: x['min_density'], reverse=True):
        if density >= rule['min_density']:
            return rule, density
    
    return None, density

def get_t2_cost_from_db(weight, zone):
    """Рассчитывает стоимость Т2 из конфига"""
    try:
        if zone == "алматы":
            return weight * T2_RATES.get("алматы", 250)
        
        # Используем детальные тарифы если доступны
        t2_detailed = T2_RATES_DETAILED.get("large_parcel", {})
        weight_ranges = t2_detailed.get("weight_ranges", [])
        extra_rates = t2_detailed.get("extra_kg_rate", {})
        
        if weight_ranges and extra_rates:
            extra_rate = extra_rates.get(zone, 300)
            
            # Находим базовую стоимость
            base_cost = 0
            remaining_weight = weight
            
            for weight_range in weight_ranges:
                if weight <= weight_range["max"]:
                    base_cost = weight_range["zones"][zone]
                    remaining_weight = 0
                    break
                elif weight > 20 and weight_range["max"] == 20:
                    base_cost = weight_range["zones"][zone]
                    remaining_weight = weight - 20
            
            # Добавляем стоимость дополнительных кг
            if remaining_weight > 0:
                base_cost += remaining_weight * extra_rate
            
            return base_cost
        else:
            # Резервный расчет
            return weight * T2_RATES.get(zone, 300)
            
    except Exception as e:
        logger.error(f"Ошибка расчета Т2: {e}")
        return weight * 300

def calculate_quick_cost(weight, product_type, city, volume=None, length=None, width=None, height=None):
    """Основная функция расчета стоимости"""
    try:
        # Расчет объема если предоставлены габариты
        if not volume and length and width and height:
            volume = length * width * height
        
        if not volume or volume <= 0:
            return None
        
        # Получаем тариф Т1
        rule, density = get_t1_rate_from_db(product_type, weight, volume)
        if not rule:
            return None
        
        price = rule['price']
        unit = rule['unit']
        
        # Расчет стоимости Т1
        if unit == "kg":
            cost_usd = price * weight
        else:  # m3
            cost_usd = price * volume
        
        t1_cost_kzt = cost_usd * EXCHANGE_RATE
        
        # Получаем зону и рассчитываем Т2
        zone = find_destination_zone(city)
        if not zone:
            return None
        
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone))
        
        # Итоговая стоимость с комиссией 20%
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20
        
        return {
            't1_cost': t1_cost_kzt,
            't2_cost': t2_cost_kzt,
            'total': total_cost,
            'zone': f"зона {zone}" if zone != "алматы" else "алматы",
            'volume': volume,
            'density': density,
            'rule': rule,
            't1_cost_usd': cost_usd,
            'product_type': product_type,
            'city': city,
            'weight': weight
        }
        
    except Exception as e:
        logger.error(f"Ошибка расчета стоимости: {e}")
        return None

def calculate_detailed_cost(quick_cost, weight, product_type, city):
    """Детальный расчет с разбивкой"""
    if not quick_cost:
        return "❌ Ошибка расчета"
    
    t1_cost = quick_cost['t1_cost']
    t2_cost = quick_cost['t2_cost']
    zone = quick_cost['zone']
    volume = quick_cost['volume']
    density = quick_cost['density']
    rule = quick_cost['rule']
    t1_cost_usd = quick_cost['t1_cost_usd']
    
    price = rule['price']
    unit = rule['unit']
    
    if unit == "kg":
        calculation_text = f"${price}/кг × {weight} кг = ${t1_cost_usd:.2f} USD"
    else:
        calculation_text = f"${price}/м³ × {volume:.3f} м³ = ${t1_cost_usd:.2f} USD"
    
    response = f"""
📊 **Детальный расчет для {weight} кг «{product_type}» в г. {city.capitalize()}:**

**Т1: Доставка из Китая до Алматы**
• Плотность вашего груза: **{density:.1f} кг/м³**
• Применен тариф Т1: **${price} за {unit}**
• Расчет: {calculation_text}
• По курсу {EXCHANGE_RATE} тенге/$ = **{t1_cost:.0f} тенге**

**Т2: Доставка до двери ({zone})**
• Прогрессивный тариф для {weight} кг = **{t2_cost:.0f} тенге**

**Комиссия компании (20%):**
• ({t1_cost:.0f} + {t2_cost:.0f}) × 20% = **{(t1_cost + t2_cost) * 0.20:.0f} тенге**

------------------------------------
💰 **ИТОГО с доставкой до двери:** ≈ **{quick_cost['total']:,.0f} тенге**

💡 **Страхование:** дополнительно 1% от стоимости груза
💳 **Оплата:** пост-оплата при получении

✅ **Оставить заявку?** Напишите ваше имя и телефон!
🔄 **Новый расчет?** Напишите **Старт**
    """
    
    return response

# ===== GEMINI TOOLS ИНИЦИАЛИЗАЦИЯ =====
base_model = None
model_with_tools = None

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # Базовая модель
        base_model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Функции для инструментов
        function_declarations = [
            genai.FunctionDeclaration(
                name="calculate_delivery_cost",
                description="Рассчитать стоимость доставки груза из Китая в Казахстан",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "weight": {
                            "type": "NUMBER", 
                            "description": "Вес груза в килограммах"
                        },
                        "product_type": {
                            "type": "STRING",
                            "description": "Тип товара: мебель, техника, одежда, косметика, автозапчасти, общие"
                        },
                        "city": {
                            "type": "STRING", 
                            "description": "Город доставки в Казахстане: алматы, астана, караганда и др."
                        },
                        "volume": {
                            "type": "NUMBER",
                            "description": "Объем груза в кубических метрах"
                        },
                        "length": {
                            "type": "NUMBER",
                            "description": "Длина груза в метрах"
                        },
                        "width": {
                            "type": "NUMBER",
                            "description": "Ширина груза в метрах" 
                        },
                        "height": {
                            "type": "NUMBER",
                            "description": "Высота груза в метрах"
                        }
                    },
                    "required": ["weight", "product_type", "city"]
                }
            ),
            genai.FunctionDeclaration(
                name="track_shipment",
                description="Отследить статус груза по трек-номеру",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "track_number": {
                            "type": "STRING",
                            "description": "Трек-номер груза в формате GZ123456, IY789012 и т.д."
                        }
                    },
                    "required": ["track_number"]
                }
            ),
            genai.FunctionDeclaration(
                name="save_application", 
                description="Сохранить заявку на доставку для связи менеджера с клиентом",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "name": {
                            "type": "STRING",
                            "description": "Имя клиента"
                        },
                        "phone": {
                            "type": "STRING", 
                            "description": "Номер телефона клиента"
                        },
                        "details": {
                            "type": "STRING",
                            "description": "Детали заявки: вес, товар, город и т.д."
                        }
                    },
                    "required": ["name", "phone"]
                }
            ),
            genai.FunctionDeclaration(
                name="get_static_info",
                description="Предоставить информацию о тарифах, оплате, процедуре доставки",
                parameters={
                    "type": "OBJECT", 
                    "properties": {
                        "info_type": {
                            "type": "STRING",
                            "description": "Тип информации: тарифы, оплата, процедура, контакты"
                        }
                    },
                    "required": ["info_type"]
                }
            )
        ]
        
        # Модель с инструментами
        model_with_tools = genai.GenerativeModel(
            model_name='gemini-1.5-flash',
            tools=function_declarations
        )
        
        logger.info("✅ Модель Gemini с инструментами инициализирована")
        
except Exception as e:
    logger.error(f"❌ Ошибка инициализации Gemini: {e}")

# ===== РЕАЛИЗАЦИИ ФУНКЦИЙ ДЛЯ GEMINI TOOLS =====
def calculate_delivery_cost_impl(weight, product_type, city, volume=None, length=None, width=None, height=None):
    """Реализация расчета стоимости для Gemini Tools"""
    try:
        logger.info(f"🔄 Расчет: {weight}кг, {product_type}, {city}")
        
        # Используем нашу основную функцию расчета
        quick_cost = calculate_quick_cost(weight, product_type, city, volume, length, width, height)
        
        if quick_cost:
            detailed_response = calculate_detailed_cost(quick_cost, weight, product_type, city)
            return {
                "success": True,
                "calculation": detailed_response,
                "total_cost": quick_cost['total'],
                "currency": "тенге"
            }
        else:
            return {"error": "Не удалось рассчитать стоимость. Проверьте данные."}
            
    except Exception as e:
        logger.error(f"Ошибка расчета: {e}")
        return {"error": f"Ошибка расчета: {str(e)}"}

def track_shipment_impl(track_number):
    """Реализация отслеживания для Gemini Tools"""
    try:
        # Загрузка данных отслеживания
        track_data = {}
        try:
            with open('guangzhou_track_data.json', 'r', encoding='utf-8') as f:
                track_data = json.load(f)
        except:
            pass
        
        shipment = track_data.get(track_number.upper())
        if shipment:
            return {
                "success": True,
                "track_number": track_number,
                "status": shipment.get('status', 'неизвестен'),
                "location": shipment.get('warehouse', 'Гуанчжоу'),
                "progress": shipment.get('route_progress', 0),
                "description": f"Груз {track_number} - {shipment.get('status', 'в обработке')}"
            }
        else:
            return {"error": f"Груз {track_number} не найден"}
            
    except Exception as e:
        return {"error": f"Ошибка отслеживания: {str(e)}"}

def save_application_impl(name, phone, details=None):
    """Реализация сохранения заявки для Gemini Tools"""
    try:
        application_data = {
            'timestamp': datetime.now().isoformat(),
            'name': name,
            'phone': phone,
            'details': details or 'Заявка через чат-бота'
        }
        
        # Сохранение в файл
        try:
            os.makedirs('data', exist_ok=True)
            applications_file = 'data/applications.json'
            applications = []
            
            if os.path.exists(applications_file):
                with open(applications_file, 'r', encoding='utf-8') as f:
                    applications = json.load(f)
            
            applications.append(application_data)
            
            with open(applications_file, 'w', encoding='utf-8') as f:
                json.dump(applications, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения заявки: {e}")
        
        return {
            "success": True,
            "message": "Заявка успешно сохранена! Менеджер свяжется с вами в течение часа."
        }
        
    except Exception as e:
        return {"error": f"Ошибка сохранения: {str(e)}"}

def get_static_info_impl(info_type):
    """Реализация получения информации для Gemini Tools"""
    info_responses = {
        'тарифы': """
🚚 **Тарифы PostPro:**

**Т1 (Китай → Алматы):**
• Расчет по плотности груза
• Мебель: от 80 тг/кг  
• Техника: от 120 тг/кг
• Одежда: от 60 тг/кг
• Чем выше плотность - тем выгоднее!

**Т2 (Алматы → ваш город):**
• Алматы: от 150 тг/кг
• Другие города: прогрессивный тариф
        """,
        'оплата': """
💳 **Условия оплаты:**

💰 **ПОСТ-ОПЛАТА** - платите при получении!

• Наличными курьеру
• Kaspi Bank
• Halyk Bank  
• Freedom Bank
• Безналичный расчет

✅ Без предоплат!
        """,
        'процедура': """
📦 **Процедура доставки:**

1. Прием груза на складе в Китае
2. Взвешивание и фотофиксация  
3. Отправка в путь (15-20 дней)
4. Уведомление о прибытии
5. Доставка и оплата

⏱️ Срок: 15-25 дней
        """,
        'контакты': """
📞 **Контакты PostPro:**

• Телефон: +7 (777) 123-45-67
• WhatsApp: +7 (777) 123-45-67
• Email: info@postpro.kz

🕘 График: Пн-Пт 9:00-19:00
        """
    }
    
    response = info_responses.get(info_type.lower(), "Информация не найдена.")
    return {"info_type": info_type, "content": response}

# ===== ОБРАБОТКА СООБЩЕНИЙ С GEMINI TOOLS =====
def process_with_gemini_tools(user_message):
    """Обработка сообщения с использованием Gemini Tools"""
    if not model_with_tools:
        return "🤖 Сервис временно недоступен. Пожалуйста, попробуйте позже."
    
    try:
        # Системный промпт
        system_prompt = """
Ты — умный ассистент компании PostPro Logistics. Твоя главная цель — помочь клиенту рассчитать стоимость доставки и оформить заявку.

Используй инструменты когда:
- Есть вес, товар и город → calculate_delivery_cost
- Есть трек-номер → track_shipment  
- Клиент предоставил имя и телефон → save_application
- Спрашивают про тарифы/оплату → get_static_info

Будь дружелюбным и профессиональным! 😊
        """
        
        full_message = f"{system_prompt}\n\nСообщение клиента: {user_message}"
        
        chat = model_with_tools.start_chat()
        response = chat.send_message(full_message)
        
        # Проверяем вызов функции
        if (hasattr(response, 'candidates') and response.candidates and
            hasattr(response.candidates[0], 'content') and
            response.candidates[0].content.parts):
            
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    function_call = part.function_call
                    function_name = function_call.name
                    args = function_call.args
                    
                    logger.info(f"🔧 Вызов функции: {function_name} с args: {args}")
                    
                    # Вызываем соответствующую функцию
                    if function_name == "calculate_delivery_cost":
                        result = calculate_delivery_cost_impl(**args)
                    elif function_name == "track_shipment":
                        result = track_shipment_impl(**args)
                    elif function_name == "save_application":
                        result = save_application_impl(**args)
                    elif function_name == "get_static_info":
                        result = get_static_info_impl(**args)
                    else:
                        result = {"error": "Неизвестная функция"}
                    
                    # Отправляем результат обратно
                    try:
                        function_response = genai.types.Part.from_function_response(
                            name=function_name,
                            response=result
                        )
                        final_response = chat.send_message(function_response)
                        return final_response.text if final_response.text else "✅ Запрос обработан успешно!"
                    except Exception as e:
                        logger.error(f"Ошибка отправки ответа функции: {e}")
                        # Возвращаем результат напрямую если есть calculation
                        if function_name == "calculate_delivery_cost" and "calculation" in result:
                            return result["calculation"]
                        return "✅ Запрос обработан успешно!"
        
        # Если не было вызова функций, возвращаем текстовый ответ
        return response.text if response.text else "🤔 Не удалось обработать ваш запрос. Пожалуйста, уточните."
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки с инструментами: {e}")
        return f"⚠️ Произошла ошибка: {str(e)}"

# ===== WEB ЭНДПОИНТЫ =====
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        if not request.json or 'message' not in request.json:
            return jsonify({"response": "❌ Неверный формат запроса"}), 400

        user_message = request.json.get('message', '').strip()
        
        if not user_message:
            return jsonify({"response": "📝 Пожалуйста, введите сообщение."})

        logger.info(f"📨 Получено сообщение: {user_message}")

        # Инициализация сессии
        if 'chat_history' not in session:
            session['chat_history'] = []

        # Обработка команды "Старт"
        if user_message.lower() in ['старт', 'start', 'новый расчет', 'сброс']:
            session.clear()
            session['chat_history'] = []
            return jsonify({"response": """
🔄 **Начинаем новый расчет!**

🤖 **Я помогу вам:**
📊 Рассчитать стоимость доставки
📦 Отследить груз по трек-номеру  
💼 Оформить заявку
❓ Ответить на вопросы

**Просто опишите что вам нужно!**

*Примеры:*
• "50 кг мебели в Алматы"
• "Где мой груз GZ123456?"
• "Расскажите про тарифы и оплату"
• "Хочу оставить заявку"
            """})

        # Проверка на множественные коробки
        boxes = extract_boxes_from_message(user_message)
        if boxes and len(boxes) > 1:
            total_weight = sum(box['weight'] for box in boxes)
            session['multiple_boxes'] = boxes
            
            boxes_list = "\n".join([f"• {i+1}. {box['weight']} кг" for i, box in enumerate(boxes)])
            
            response = f"""
📦 **Обнаружено несколько коробок:**
{boxes_list}

📊 **Общий вес:** {total_weight} кг

🏙️ **Для расчета укажите:**
• Город доставки
• Тип товара  
• Габариты коробок

💡 **Пример:** "в Астану, одежда, коробки 60×40×30 см"
            """
            return jsonify({"response": response})

        # Проверка на паллеты
        pallets = extract_pallets_from_message(user_message)
        if pallets:
            total_weight = sum(pallet['weight'] for pallet in pallets)
            total_volume = sum(pallet['volume'] for pallet in pallets)
            
            response = f"""
🎯 **Обнаружены паллеты:**
• Количество: {len(pallets)} шт
• Общий вес: {total_weight} кг  
• Общий объем: {total_volume:.1f} м³

🏙️ **Для точного расчета укажите:**
• Город доставки
• Тип товара на паллетах

💡 **Пример:** "в Караганду, мебель на паллетах"
            """
            return jsonify({"response": response})

        # Основная обработка с Gemini Tools
        bot_response = process_with_gemini_tools(user_message)

        # Сохраняем в историю
        session['chat_history'].append(f"Клиент: {user_message}")
        session['chat_history'].append(f"Ассистент: {bot_response}")
        
        if len(session['chat_history']) > 10:
            session['chat_history'] = session['chat_history'][-10:]

        return jsonify({"response": bot_response})

    except Exception as e:
        logger.error(f"❌ Ошибка обработки сообщения: {e}")
        return jsonify({"response": "⚠️ Произошла ошибка. Пожалуйста, попробуйте еще раз или напишите 'Старт'."})

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "gemini_configured": GEMINI_API_KEY is not None,
        "config_loaded": config is not None,
        "model_with_tools": model_with_tools is not None
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Запуск приложения на порту {port}")
    app.run(debug=False, host='0.0.0.0', port=port)