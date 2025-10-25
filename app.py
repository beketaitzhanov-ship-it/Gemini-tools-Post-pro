# -*- coding: utf-8 -*-
import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
import google.generativeai as genai
# ИСПРАВЛЕНИЕ: Добавляем импорт protos
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
# ИСПРАВЛЕНИЕ: используем правильный формат через genai.protos с type_
tools = [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="calculate_delivery_cost",
                description="Рассчитать стоимость доставки из Китая в Казахстан по нашим тарифам",
                parameters=genai.protos.Schema(
                    type_=genai.protos.Type.OBJECT,
                    properties={
                        "weight_kg": genai.protos.Schema(type_=genai.protos.Type.NUMBER, description="Общий вес груза в килограммах"),
                        "city": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Город доставки в Казахстане: Алматы, Астана, Шымкент и др."),
                        "product_type": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Тип товара: одежда, мебель, техника, косметика, автозапчасти и т.д."),
                        "volume_m3": genai.protos.Schema(type_=genai.protos.Type.NUMBER, description="Объем груза в кубических метрах"),
                        "length_m": genai.protos.Schema(type_=genai.protos.Type.NUMBER, description="Длина груза в метрах"),
                        "width_m": genai.protos.Schema(type_=genai.protos.Type.NUMBER, description="Ширина груза в метрах"),
                        "height_m": genai.protos.Schema(type_=genai.protos.Type.NUMBER, description="Высота груза в метрах")
                    },
                    required=["weight_kg", "city", "product_type"]
                )
            ),
            genai.protos.FunctionDeclaration(
                name="track_shipment",
                description="Отследить статус груза по трек-номеру",
                parameters=genai.protos.Schema(
                    type_=genai.protos.Type.OBJECT,
                    properties={
                        "tracking_number": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Трек-номер груза (начинается с GZ, IY, SZ)")
                    },
                    required=["tracking_number"]
                )
            ),
            genai.protos.FunctionDeclaration(
                name="get_delivery_terms",
                description="Получить информацию о сроках доставки"
            ),
            genai.protos.FunctionDeclaration(
                name="get_payment_methods", 
                description="Получить список доступных способов оплаты"
            ),
            genai.protos.FunctionDeclaration(
                name="save_customer_application",
                description="Сохранить заявку клиента для обратного звонка",
                parameters=genai.protos.Schema(
                    type_=genai.protos.Type.OBJECT,
                    properties={
                        "name": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Имя клиента"),
                        "phone": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Телефон клиента (10-11 цифр)"),
                        "details": genai.protos.Schema(type_=genai.protos.Type.STRING, description="Дополнительная информация о заявке")
                    },
                    required=["name", "phone"]
                )
            )
        ]
    )
]

# ===== ИНИЦИАЛИЗАЦИЯ GEMINI =====
model = None

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # ИСПРАВЛЕНИЕ: Инициализируем модель с правильным именем
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
# (Ваши функции-обработчики не изменились)
def find_product_category(text):
    if not text: return "общие"
    text_lower = text.lower()
    for category, data in PRODUCT_CATEGORIES.items():
        for keyword in data["keywords"]:
            if keyword in text_lower: return category
    return "общие"

def find_destination_zone(city_name):
    if not city_name: return "5"
    city_lower = city_name.lower()
    if city_lower in DESTINATION_ZONES: return DESTINATION_ZONES[city_lower]
    for city, zone in DESTINATION_ZONES.items():
        if city in city_lower or city_lower in city: return zone
    return "5"

def get_t1_rate_from_db(product_type, weight, volume):
    if not volume or volume <= 0: return None, 0
    density = weight / volume
    category = find_product_category(product_type)
    rules = T1_RATES_DENSITY.get(category, T1_RATES_DENSITY.get("общие", []))
    if not rules: return None, density
    for rule in sorted(rules, key=lambda x: x['min_density'], reverse=True):
        if density >= rule['min_density']: return rule, density
    return None, density

def get_t2_cost_from_db(weight, zone):
    try:
        if zone == "алматы": return weight * T2_RATES.get("алматы", 250)
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
        else: return weight * T2_RATES.get(zone, 300)
    except Exception as e:
        logger.error(f"Ошибка расчета Т2: {e}")
        return weight * 300

def calculate_quick_cost(weight, product_type, city, volume=None, length=None, width=None, height=None):
    try:
        if not volume and length and width and height:
            volume = length * width * height
        if not weight or not product_type or not city:
             return {"error": "Недостаточно данных: нужны вес, тип товара и город."}
        if not volume or volume <= 0:
             return {"error": "Не удалось рассчитать объем. Укажите объем или размеры (длина, ширина, высота)."}
        rule, density = get_t1_rate_from_db(product_type, weight, volume)
        if not rule:
             rule, density = get_t1_rate_from_db("общие", weight, volume)
             if not rule:
                 return {"error": f"Не найден подходящий тариф Т1 для плотности {density:.2f} кг/м³ и категории '{product_type}'."}
        price = rule['price']
        unit = rule['unit']
        if unit == "kg": cost_usd = price * weight
        else: cost_usd = price * volume
        current_rate = EXCHANGE_RATE
        t1_cost_kzt = cost_usd * current_rate
        zone = find_destination_zone(city)
        if not zone: return {"error": "Город не найден в зонах доставки"}
        t2_cost_kzt = get_t2_cost_from_db(weight, str(zone))
        total_cost = (t1_cost_kzt + t2_cost_kzt) * 1.20
        return {
            'success': True, 't1_cost_kzt': t1_cost_kzt, 't2_cost_kzt': t2_cost_kzt,
            'total_cost_kzt': total_cost, 'zone': f"зона {zone}" if zone != "алматы" else "алматы",
            'volume_m3': volume, 'density_kg_m3': density, 't1_cost_usd': cost_usd,
            'product_type': product_type, 'city': city, 'weight_kg': weight
        }
    except Exception as e:
        logger.error(f"Ошибка расчета стоимости: {e}")
        return {"error": f"Ошибка расчета: {str(e)}"}

def process_tracking_request(tracking_number):
    try:
        track_data = {}
        try:
            with open('guangzhou_track_data.json', 'r', encoding='utf-8') as f:
                track_data = json.load(f)
        except: pass
        shipment = track_data.get(tracking_number)
        if shipment:
            status_emoji = {
                "принят на складе": "🏭", "в пути до границы": "🚚", "на границе": "🛃",
                "в пути до алматы": "🚛", "прибыл в алматы": "🏙️", "доставлен": "✅"
            }.get(shipment.get('status'), '📦')
            return {
                'success': True, 'tracking_number': tracking_number,
                'recipient': shipment.get('fio', 'Не указано'), 'product': shipment.get('product', 'Не указано'),
                'weight_kg': shipment.get('weight', 0), 'volume_m3': shipment.get('volume', 0),
                'status': shipment.get('status', 'В обработке'), 'status_emoji': status_emoji,
                'progress_percent': shipment.get('route_progress', 0)
            }
        else: return {"error": f"Груз с трек-номером {tracking_number} не найден"}
    except Exception as e:
        logger.error(f"Ошибка отслеживания: {e}")
        return {"error": f"Ошибка при поиске груза: {str(e)}"}

def save_application(name, phone, details=None):
    try:
        application_data = {
            'timestamp': datetime.now().isoformat(), 'name': name, 'phone': phone,
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
            'success': True, 'message': f"Заявка от {name} сохранена",
            'application_id': len(applications)
        }
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")
        return {"error": f"Ошибка при сохранении заявки: {str(e)}"}

def get_delivery_terms(warehouse=None):
    try:
        if warehouse and "гуанчжоу" in warehouse.lower():
            return {
                'success': True, 'warehouse': 'Гуанчжоу', 'route': 'Гуанчжоу → Алматы',
                'transit_time_days': '10-14 дней', 'total_time_days': '15-20 дней',
                'border_crossing': 'Хоргос'
            }
        else:
            return {
                'success': True, 'general_terms': 'Доставка из Китая в Казахстан',
                'transit_time_days': '10-20 дней',
                'customs_clearance': '2-3 дня',
                'domestic_delivery': '1-4 дня',
                'warehouses_info': 'У нас есть склады в Гуанчжоу и Иу.'
            }
    except Exception as e:
        logger.error(f"Ошибка получения сроков: {e}")
        return {"error": f"Ошибка получения информации о сроках: {str(e)}"}

def get_payment_methods():
    try:
        return {
            'success': True,
            'payment_methods': [
                'Банковский перевод (Kaspi, Halyk, Freedom Bank)', 'Онлайн-оплата картой',
                'Alipay & WeChat Pay', 'Наличные при получении',
                'Безналичный расчет для ИП и юр.лиц', 'Криптовалюты (Bitcoin, USDT)',
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
            return {"error": f"Неизвестный инструмент: {function_name}"}
            
    except Exception as e:
        logger.error(f"❌ Ошибка выполнения инструмента {function_name}: {e}")
        return {"error": f"Ошибка выполнения: {str(e)}"}

# ===== ИСПРАВЛЕНИЕ ОШИБКИ 'function_response' И АМНЕЗИИ: НОВАЯ ФУНКЦИЯ =====
def get_aisulu_response_with_tools(user_message):
    """Основная функция получения ответа от Айсулу с инструментами (С ПАМЯТЬЮ)"""
    if not model:
        return "🤖 Сервис временно недоступен. Пожалуйста, попробуйте позже."
    
    try:
        # Получаем историю диалога
        chat_history_raw = session.get('chat_history', [])
        
        # Создаем структурированные сообщения для Gemini
        messages = []
        
        # 1. ИСПРАВЛЕНИЕ АМНЕЗИИ: ВСЕГДА отправляем системный промпт
        messages.append({
            "role": "user",
            "parts": [{"text": AISULU_PROMPT}]
        })
        # Добавляем " priming" ответ от модели, чтобы она сразу "вошла в роль"
        messages.append({
            "role": "model",
            "parts": [{"text": "Сәлеметсіз бе! Я Айсулу. Чем могу помочь? 🌸"}]
        })
        
        # 2. Добавляем историю диалога в правильном формате
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
        
        # 3. Добавляем текущее сообщение пользователя
        messages.append({
            "role": "user",
            "parts": [{"text": user_message}]
        })

        # ----- ИСПРАВЛЕНИЕ ОШИБКИ .text & function_response -----
        # 4. Отправляем запрос
        response = model.generate_content(
            messages,
            generation_config={'temperature': 0.7}
            # 'tools=tools' уже был передан в 'GenerativeModel' при инициализации
        )
        
        # 5. Проверяем ответ
        if not (hasattr(response, 'candidates') and response.candidates and
                hasattr(response.candidates[0], 'content') and response.candidates[0].content and
                hasattr(response.candidates[0].content, 'parts') and response.candidates[0].content.parts):
            logger.error("❌ Неожиданный ответ от Gemini (нет 'parts')")
            return "Ой, я не смогла cформировать ответ. 😅"

        candidate = response.candidates[0]
        part = candidate.content.parts[0]

        # Сценарий 1: Gemini вызывает инструмент (УМНЫЙ ОТВЕТ)
        if hasattr(part, 'function_call') and part.function_call:
            logger.info("🤖 Gemini вызвал инструмент...")
            function_call = part.function_call
            
            # Выполняем инструмент
            tool_result = execute_tool_function(
                function_call.name,
                dict(function_call.args)
            )
            
            # --- ИСПРАВЛЕНИЕ ОШИБКИ 'Got keys: [function_response]' ---
            # Мы должны передавать список, состоящий ИСКЛЮЧИТЕЛЬНО из СЛОВАРЕЙ.
            
            # 1. Конвертируем 'Content' (proto) от модели в 'dict'
            # (Используем genai_types.to_dict для безопасной конвертации)
            model_request_content = genai_types.to_dict(candidate.content)

            # 2. Создаем наш 'Content' (dict) с ответом
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
            # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
            
            # Добавляем результат функции в историю и делаем финальный запрос
            # Теперь 'updated_messages' - это список, содержащий *только* словари
            updated_messages = messages + [model_request_content, function_response_content]
            
            final_response = model.generate_content(
                updated_messages,
                generation_config={'temperature': 0.7}
            )
            
            # Безопасно извлекаем текст из ФИНАЛЬНОГО ответа
            try:
                final_text = final_response.candidates[0].content.parts[0].text
                return final_text
            except Exception as e:
                logger.error(f"❌ Ошибка извлечения final_text после вызова функции: {e}")
                return "Ой, что-то пошло не так! 😅"

        # Сценарий 2: Gemini отвечает текстом (ПРОСТОЙ ОТВЕТ)
        elif hasattr(part, 'text'):
            logger.info("🤖 Gemini ответил текстом...")
            return part.text

        # Сценарий 3: Не текст и не инструмент (ошибка)
        else:
            logger.error("❌ Ответ Gemini - не текст и не вызов функции")
            return "Ой, я не поняла, что нужно сделать. 🌸"
        # ----- КОНЕЦ ИСПРАВЛЕНИЯ -----
        
    except Exception as e:
        logger.error(f"❌ Ошибка в get_aisulu_response_with_tools: {e}")
        # Добавляем детали ошибки
        if "Could not recognize" in str(e):
             logger.error("❌ (Детали: Ошибка 'Could not recognize'. Неправильный формат 'function_response'.)")
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

        # ИСПРАВЛЕНИЕ: Вызываем новую функцию, которая сама читает из сессии
        response_text = get_aisulu_response_with_tools(user_message)

        # Сохраняем в историю
        session['chat_history'].append(f"Клиент: {user_message}")
        session['chat_history'].append(f"Айсулу: {response_text}")
        
        # Ограничиваем историю, чтобы она не росла бесконечно
        if len(session['chat_history']) > 12: # (6 раундов диалога)
            # Обрезаем
            session['chat_history'] = session['chat_history'][-10:]

        return jsonify({"response": response_text})

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
