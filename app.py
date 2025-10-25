# -*- coding: utf-8 -*-
import os
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

def load_personality_prompt():
    """Загружает промпт личности Айсулу"""
    try:
        with open('personality_prompt.txt', 'r', encoding='utf-8') as f:
            prompt_content = f.read()
            logger.info("✅ Промпт личности Айсулу загружен")
            return prompt_content
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки personality_prompt.txt: {e}")
        return None

config = load_config()
AISULU_PROMPT = load_personality_prompt()

if config:
    EXCHANGE_RATE = config.get("EXCHANGE_RATE", {}).get("rate", 550)
    DESTINATION_ZONES = config.get("DESTINATION_ZONES", {})
    T1_RATES_DENSITY = config.get("T1_RATES_DENSITY", {})
    T2_RATES = config.get("T2_RATES", {})
    T2_RATES_DETAILED = config.get("T2_RATES_DETAILED", {})
    PRODUCT_CATEGORIES = config.get("PRODUCT_CATEGORIES", {})
else:
    logger.error("⚠️ Приложение запускается с значениями по умолчанию")
    EXCHANGE_RATE, DESTINATION_ZONES, T1_RATES_DENSITY, T2_RATES, T2_RATES_DETAILED, PRODUCT_CATEGORIES = 550, {}, {}, {}, {}, {}

# ===== ИНСТРУМЕНТЫ ДЛЯ GEMINI =====
# ИСПРАВЛЕНИЕ: все функции в одном инструменте
tools = [
    {
        "function_declarations": [
            {
                "name": "calculate_delivery_cost",
                "description": "Рассчитать стоимость доставки из Китая в Казахстан по нашим тарифам",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "weight_kg": {
                            "type": "number", 
                            "description": "Общий вес груза в килограммах"
                        },
                        "city": {
                            "type": "string", 
                            "description": "Город доставки в Казахстане: Алматы, Астана, Шымкент и др."
                        },
                        "product_type": {
                            "type": "string", 
                            "description": "Тип товара: одежда, мебель, техника, косметика, автозапчасти и т.д."
                        },
                        "volume_m3": {
                            "type": "number", 
                            "description": "Объем груза в кубических метрах"
                        },
                        "length_m": {
                            "type": "number", 
                            "description": "Длина груза в метрах"
                        },
                        "width_m": {
                            "type": "number", 
                            "description": "Ширина груза в метрах"
                        },
                        "height_m": {
                            "type": "number", 
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
                    "type": "object",
                    "properties": {
                        "tracking_number": {
                            "type": "string", 
                            "description": "Трек-номер груза (начинается с GZ, IY, SZ)"
                        }
                    },
                    "required": ["tracking_number"]
                }
            },
            {
                "name": "get_delivery_terms",
                "description": "Получить информацию о сроках доставки",
                "parameters": {
                    "type": "object", 
                    "properties": {
                        "warehouse": {
                            "type": "string",
                            "description": "Склад отправки: Гуанчжоу, Иу"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "get_payment_methods", 
                "description": "Получить список доступных способов оплаты",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "save_customer_application",
                "description": "Сохранить заявку клиента для обратного звонка",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Имя клиента"
                        },
                        "phone": {
                            "type": "string", 
                            "description": "Телефон клиента (10-11 цифр)"
                        },
                        "details": {
                            "type": "string",
                            "description": "Дополнительная информация о заявке"
                        }
                    },
                    "required": ["name", "phone"]
                }
            }
        ]
    }
]

# ===== ИНИЦИАЛИЗАЦИЯ GEMINI =====
model = None

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # Инициализация модели с инструментами
        model = genai.GenerativeModel(
            'models/gemini-2.0-flash',
            tools=tools
        )
        logger.info("✅ Модель Gemini инициализирована с инструментами")
    else:
        logger.warning("⚠️ GEMINI_API_KEY не найден")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации Gemini: {e}")

# ===== ФУНКЦИИ-ОБРАБОТЧИКИ ИНСТРУМЕНТОВ =====
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
    
    if city_lower in DESTINATION_ZONES:
        return DESTINATION_ZONES[city_lower]
    
    for city, zone in DESTINATION_ZONES.items():
        if city in city_lower or city_lower in city:
            return zone
    
    return "5"

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
        
        t2_detailed = T2_RATES_DETAILED.get("large_parcel", {})
        weight_ranges = t2_detailed.get("weight_ranges", [])
        extra_rates = t2_detailed.get("extra_kg_rate", {})
        
        if weight_ranges and extra_rates:
            extra_rate = extra_rates.get(zone, 300)
            
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
            
            if remaining_weight > 0:
                base_cost += remaining_weight * extra_rate
            
            return base_cost
        else:
            return weight * T2_RATES.get(zone, 300)
            
    except Exception as e:
        logger.error(f"Ошибка расчета Т2: {e}")
        return weight * 300

def calculate_quick_cost(weight, product_type, city, volume=None, length=None, width=None, height=None):
    """Основная функция расчета стоимости"""
    try:
        if not volume and length and width and height:
            volume = length * width * height
        
        if not volume or volume <= 0:
            return {"error": "Не удалось рассчитать объем"}
        
        rule, density = get_t1_rate_from_db(product_type, weight, volume)
        if not rule:
            return {"error": "Не найден подходящий тариф"}
        
        price = rule['price']
        unit = rule['unit']
        
        if unit == "kg":
            cost_usd = price * weight
        else:
            cost_usd = price * volume
        
        current_rate = EXCHANGE_RATE
        t1_cost_kzt = cost_usd * current_rate
        
        zone = find_destination_zone(city)
        if not zone:
            return {"error": "Город не найден в зонах доставки"}
        
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone))
        
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20
        
        return {
            'success': True,
            't1_cost_kzt': t1_cost_kzt,
            't2_cost_kzt': t2_cost_kzt,
            'total_cost_kzt': total_cost,
            'zone': f"зона {zone}" if zone != "алматы" else "алматы",
            'volume_m3': volume,
            'density_kg_m3': density,
            't1_cost_usd': cost_usd,
            'product_type': product_type,
            'city': city,
            'weight_kg': weight
        }
        
    except Exception as e:
        logger.error(f"Ошибка расчета стоимости: {e}")
        return {"error": f"Ошибка расчета: {str(e)}"}

def process_tracking_request(tracking_number):
    """Обрабатывает запросы на отслеживание"""
    try:
        track_data = {}
        try:
            with open('guangzhou_track_data.json', 'r', encoding='utf-8') as f:
                track_data = json.load(f)
        except:
            pass
        
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
            return {"error": f"Груз с трек-номером {tracking_number} не найден"}
            
    except Exception as e:
        logger.error(f"Ошибка отслеживания: {e}")
        return {"error": f"Ошибка при поиске груза: {str(e)}"}

def save_application(name, phone, details=None):
    """Сохраняет заявку"""
    try:
        application_data = {
            'timestamp': datetime.now().isoformat(),
            'name': name,
            'phone': phone,
            'details': details or 'Заявка через чат-бота'
        }
        
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
            'success': True,
            'message': f"Заявка от {name} сохранена",
            'application_id': len(applications)
        }
        
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")
        return {"error": f"Ошибка при сохранении заявки: {str(e)}"}

def get_delivery_terms(warehouse=None):
    """Возвращает информацию о сроках доставки"""
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
                'domestic_delivery': '1-4 дня'
            }
    except Exception as e:
        logger.error(f"Ошибка получения сроков: {e}")
        return {"error": f"Ошибка получения информации о сроках: {str(e)}"}

def get_payment_methods():
    """Возвращает список способов оплаты"""
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
        logger.error(f"Ошибка получения способов оплаты: {e}")
        return {"error": f"Ошибка получения способов оплаты: {str(e)}"}

def execute_tool_function(function_name, parameters):
    """Выполняет функцию-инструмент по имени"""
    try:
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
            return get_delivery_terms(parameters.get('warehouse'))
        
        elif function_name == "get_payment_methods":
            return get_payment_methods()
        
        elif function_name == "save_customer_application":
            return save_application(
                name=parameters.get('name'),
                phone=parameters.get('phone'),
                details=parameters.get('details')
            )
        
        else:
            return {"error": f"Неизвестный инструмент: {function_name}"}
            
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения инструмента {function_name}: {e}")
        return {"error": f"Ошибка выполнения: {str(e)}"}

# ===== ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ С ИНСТРУМЕНТАМИ =====
def get_aisulu_response_with_tools(user_message):
    """Основная функция получения ответа от Айсулу с инструментами"""
    if not model:
        return "🤖 Сервис временно недоступен. Пожалуйста, попробуйте позже."
    
    try:
        # Передаем system_instruction в generate_content()
        response = model.generate_content(
            user_message,
            system_instruction=AISULU_PROMPT
        )
        
        # Проверяем, есть ли вызов функции в ответе
        if hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                if hasattr(candidate.content, 'parts') and candidate.content.parts:
                    part = candidate.content.parts[0]
                    
                    # Проверяем, вызвал ли Gemini инструмент
                    if hasattr(part, 'function_call') and part.function_call:
                        function_call = part.function_call
                        
                        # Выполняем инструмент
                        tool_result = execute_tool_function(
                            function_call.name,
                            dict(function_call.args)
                        )
                        
                        # Создаем контент с результатом для обратного вызова
                        function_response_content = {
                            "role": "function",
                            "parts": [{
                                "function_response": {
                                    "name": function_call.name,
                                    "response": tool_result
                                }
                            }]
                        }
                        
                        # Передаем system_instruction во втором вызове тоже
                        final_response = model.generate_content(
                            [
                                user_message,
                                candidate.content,
                                function_response_content
                            ],
                            system_instruction=AISULU_PROMPT
                        )
                        
                        return final_response.text if final_response.text else "Ой, что-то пошло не так! 😅"
        
        # Если вызова функции не было, возвращаем текстовый ответ
        return response.text if response.text else "Ой, не получилось ответить! Попробуйте еще раз. 🌸"
        
    except Exception as e:
        logger.error(f"❌ Ошибка в get_aisulu_response_with_tools: {e}")
        return "⚠️ Ой, произошла ошибка! Пожалуйста, попробуйте еще раз. 🌸"

# ===== WEB ЭНДПОИНТЫ =====
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        if not request.json or 'message' not in request.json:
            return jsonify({"response": "❌ Ой, неверный формат запроса! 🌸"}), 400

        user_message = request.json.get('message', '').strip()
        
        if not user_message:
            return jsonify({"response": "📝 Сәлеметсіз бе! 🌸 Напишите ваше сообщение."})

        logger.info(f"📨 Получено сообщение: {user_message}")

        # Инициализация сессии
        if 'chat_history' not in session:
            session['chat_history'] = []

        # Обработка команды "Старт" 
        if user_message.lower() in ['старт', 'start', 'новый расчет', 'сброс', 'баста']:
            session.clear()
            session['chat_history'] = []
            return jsonify({"response": """
🔄 **Сәлеметсіз бе! Я Айсулу - ваш помощник в доставке!** 🌸

🤖 **Я помогу вам:**
📊 Рассчитать стоимость доставки из Китая
📦 Отследить груз по трек-номеру  
💼 Оформить заявку на доставку
❓ Ответить на вопросы о логистике

**Просто напишите что вам нужно!** 😊
            """})

        # ВСЁ остальное обрабатываем через AI-first архитектуру
        response = get_aisulu_response_with_tools(user_message)

        # Сохраняем в историю
        session['chat_history'].append(f"Клиент: {user_message}")
        session['chat_history'].append(f"Айсулу: {response}")
        
        if len(session['chat_history']) > 10:
            session['chat_history'] = session['chat_history'][-10:]

        return jsonify({"response": response})

    except Exception as e:
        logger.error(f"❌ Ошибка обработки сообщения: {e}")
        return jsonify({"response": "⚠️ Ой, произошла ошибка! Пожалуйста, попробуйте еще раз или напишите 'Старт'. 🌸"})

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "gemini_configured": GEMINI_API_KEY is not None,
        "config_loaded": config is not None,
        "model_initialized": model is not None,
        "aisulu_prompt_loaded": AISULU_PROMPT is not None
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🚀 Запуск приложения на порту {port}")
    app.run(debug=False, host='0.0.0.0', port=port)
