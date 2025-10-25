# -*- coding: utf-8 -*-
# Model: models/gemini-2.0-flash
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

# ===== ЗАГРУЗКА КОНФИГУРАЦИИ И ПРОМПТА =====
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
    GREETINGS = config.get("GREETINGS", [])
else:
    logger.error("⚠️ Приложение запускается с значениями по умолчанию")
    EXCHANGE_RATE, DESTINATION_ZONES, T1_RATES_DENSITY, T2_RATES, T2_RATES_DETAILED, PRODUCT_CATEGORIES, GREETINGS = 550, {}, {}, {}, {}, {}, []

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def detect_delivery_mode(message):
    """Определяет, нужно ли переключиться в режим доставки"""
    text_lower = message.lower()
    
    # Признаки режима доставки
    delivery_keywords = ['доставка', 'груз', 'посчитай', 'расчёт', 'тариф', 'логистика', 'стоимость', 'сколько стоит']
    has_delivery_keywords = any(keyword in text_lower for keyword in delivery_keywords)
    
    # Цифры с единицами измерения
    has_measurements = bool(re.search(r'\d+\s*(?:кг|kg|м|m|см|cm|куб|м³|×|х|x)', text_lower))
    
    # Города Казахстана
    has_cities = any(city in text_lower for city in DESTINATION_ZONES.keys())
    
    return has_delivery_keywords or has_measurements or has_cities

def get_aisulu_prompt(user_message, context=""):
    """Создает промпт для Айсулу с учетом режима"""
    delivery_mode = detect_delivery_mode(user_message)
    
    base_prompt = AISULU_PROMPT or """
Ты - Айсулу, весёлый и энергичный ИИ-помощник из Казахстана. 
Отвечай дружелюбно, с казахским колоритом, используй эмодзи.
"""
    
    prompt = f"""
{base_prompt}

Текущий режим: {"📦 РЕЖИМ ДОСТАВКИ" if delivery_mode else "💬 ОБЫЧНОЕ ОБЩЕНИЕ"}

Контекст предыдущего разговора:
{context}

Сообщение пользователя: {user_message}

Помни: Ты Айсулу - настоящая казахская девушка с большим сердцем! 
Отвечай соответственно своему характеру и текущему режиму.
"""
    return prompt

# [ОСТАЛЬНЫЕ ФУНКЦИИ ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ - extract_weight, extract_city, find_product_category и т.д.]

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
🎯 **Айсулу рассчитала доставку для {weight} кг «{product_type}» в {city.capitalize()}!** 🌸

📊 **Детальный расчет:**

**🚛 Т1: Доставка из Китая до Алматы**
• Плотность груза: **{density:.1f} кг/м³**
• Применен тариф: **${price} за {unit}**
• Расчет: {calculation_text}
• В тенге: **{t1_cost:.0f} ₸**

**🚚 Т2: Доставка до двери ({zone})**
• Прогрессивный тариф = **{t2_cost:.0f} ₸**

**💼 Комиссия компании (20%):**
• ({t1_cost:.0f} + {t2_cost:.0f}) × 20% = **{(t1_cost + t2_cost) * 0.20:.0f} ₸**

------------------------------------
💰 **ИТОГО с доставкой до двери:** ≈ **{quick_cost['total']:,.0f} ₸**

💡 *Ваш груз помчится через Хоргос быстрее, чем новости по аулу!* 😄

📞 **Оставить заявку?** Напишите ваше имя и телефон!
🔄 **Новый расчет?** Напишите **"Старт"**
    """
    
    return response

# ===== GEMINI ИНИЦИАЛИЗАЦИЯ =====
model = None

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.0-flash')
        logger.info("✅ Модель Gemini инициализирована")
        
except Exception as e:
    logger.error(f"❌ Ошибка инициализации Gemini: {e}")

def get_gemini_response(user_message, context=""):
    """Получает ответ от Gemini с личностью Айсулу"""
    if not model:
        return "🤖 Сервис временно недоступен."
    
    try:
        prompt = get_aisulu_prompt(user_message, context)
        
        response = model.generate_content(prompt)
        return response.text if response.text else "Ой, что-то пошло не так! 😅 Попробуйте еще раз."
        
    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}")
        return "⚠️ Ой, произошла ошибка! Попробуйте еще раз. 🌸"

# ===== ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ =====
def process_delivery_request(user_message):
    """Обрабатывает запросы на доставку с личностью Айсулу"""
    try:
        # Извлекаем параметры
        weight = extract_weight(user_message)
        city = extract_city(user_message)
        product_type = find_product_category(user_message)
        length, width, height = extract_dimensions(user_message)
        volume = extract_volume(user_message)
        
        # Проверяем наличие всех данных
        if not weight:
            return "🌸 Сәлеметсіз бе! 📊 Чтобы рассчитать доставку, укажите вес груза в кг (например: 50 кг)"
        
        if not product_type:
            return "📦 Ой, а что за товар будем отправлять? Укажите тип (мебель, техника, одежда и т.д.) 😊"
        
        if not city:
            return "🏙️ А в какой город Казахстана нужно доставить? Напишите название города 🌸"
        
        if not volume and not (length and width and height):
            return "📐 Чтобы расчет был точным, укажите габариты (например: 1.2×0.8×0.5 м) или объем груза 💫"
        
        # Расчет объема если предоставлены габариты
        if not volume and length and width and height:
            volume = length * width * height
        
        # Производим расчет
        quick_cost = calculate_quick_cost(weight, product_type, city, volume, length, width, height)
        
        if quick_cost:
            return calculate_detailed_cost(quick_cost, weight, product_type, city)
        else:
            return "❌ Ой, не удалось рассчитать стоимость! Проверьте данные и попробуйте еще раз. 🌸"
            
    except Exception as e:
        logger.error(f"Ошибка обработки доставки: {e}")
        return "⚠️ Ой, ошибка расчета! Попробуйте еще раз или напишите 'Старт'. 🌸"

def process_tracking_request(user_message):
    """Обрабатывает запросы на отслеживание с личностью Айсулу"""
    try:
        # Ищем трек-номер
        track_match = re.search(r'\b(GZ|IY|SZ)[a-zA-Z0-9]{6,18}\b', user_message.upper())
        if track_match:
            track_number = track_match.group(0)
            
            # Загрузка данных отслеживания
            track_data = {}
            try:
                with open('guangzhou_track_data.json', 'r', encoding='utf-8') as f:
                    track_data = json.load(f)
            except:
                pass
            
            shipment = track_data.get(track_number)
            if shipment:
                status_emoji = {
                    "принят на складе": "🏭",
                    "в пути до границы": "🚚", 
                    "на границе": "🛃",
                    "в пути до алматы": "🚛",
                    "прибыл в алматы": "🏙️",
                    "доставлен": "✅"
                }.get(shipment.get('status'), '📦')
                
                return f"""
📦 **Айсулу нашла ваш груз {track_number}!** 🌸

👤 **Получатель:** {shipment.get('fio', 'Не указано')}
📦 **Товар:** {shipment.get('product', 'Не указано')}  
⚖️ **Вес:** {shipment.get('weight', 0)} кг
📏 **Объем:** {shipment.get('volume', 0)} м³

🔄 **Статус:** {status_emoji} {shipment.get('status', 'В обработке')}
📊 **Прогресс:** {shipment.get('route_progress', 0)}%

💡 *Для уточнений обращайтесь к вашему менеджеру!* 😊
                """
            else:
                return f"❌ Ой, груз с трек-номером {track_number} не найден! Проверьте номер. 🌸"
        else:
            return "📦 Для отслеживания укажите трек-номер (например: GZ123456) 🌸"
            
    except Exception as e:
        logger.error(f"Ошибка отслеживания: {e}")
        return "⚠️ Ой, ошибка при поиске груза! Попробуйте еще раз. 🌸"

def save_application(name, phone, details=None):
    """Сохраняет заявку"""
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
        
        return f"✅ **Рахамет, {name}!** 🌸\nЗаявка успешно сохранена! Менеджер свяжется с вами в течение часа по номеру {phone}."
        
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")
        return "❌ Ой, ошибка при сохранении заявки! Попробуйте еще раз. 🌸"

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

        # ==== ВСТАВЬ КОНТЕКСТНУЮ ПАМЯТЬ ЗДЕСЬ ====
        # КОНТЕКСТНАЯ ПАМЯТЬ - сохраняем предыдущие данные
        if 'context' not in session:
            session['context'] = {
                'weight': None,
                'city': None, 
                'product_type': None,
                'dimensions': None,
                'boxes_count': None
            }
        
        # АВТОМАТИЧЕСКОЕ ИЗВЛЕЧЕНИЕ И СОХРАНЕНИЕ ДАННЫХ
        current_weight = extract_weight(user_message)
        current_city = extract_city(user_message)  
        current_product = find_product_category(user_message)
        current_dims = extract_dimensions(user_message)
        current_boxes = extract_boxes_from_message(user_message)
        
        # ОБНОВЛЯЕМ КОНТЕКСТ если нашли новые данные
        if current_weight: session['context']['weight'] = current_weight
        if current_city: session['context']['city'] = current_city
        if current_product: session['context']['product_type'] = current_product  
        if current_dims != (None, None, None): session['context']['dimensions'] = current_dims
        if current_boxes: session['context']['boxes_count'] = len(current_boxes)
        
        context = session['context']
        
        # УМНАЯ ОБРАБОТКА С УЧЕТОМ КОНТЕКСТА
        has_weight = context['weight'] or current_weight
        has_city = context['city'] or current_city  
        has_product = context['product_type'] or current_product
        has_dims = context['dimensions'] or (current_dims != (None, None, None))
        
        # ЕСЛИ ЕСТЬ ВСЕ ДАННЫЕ ДЛЯ РАСЧЕТА - СЧИТАЕМ АВТОМАТИЧЕСКИ
        if has_weight and has_city and has_product and has_dims:
            weight = context['weight'] or current_weight
            city = context['city'] or current_city
            product_type = context['product_type'] or current_product
            dims = context['dimensions'] or current_dims
            
            # РАСЧЕТ С УЧЕТОМ КОРОБОК
            if context['boxes_count'] and context['boxes_count'] > 1:
                total_weight = weight * context['boxes_count']
                volume_per_box = dims[0] * dims[1] * dims[2] if dims[0] else None
                total_volume = volume_per_box * context['boxes_count'] if volume_per_box else None
                
                quick_cost = calculate_quick_cost(total_weight, product_type, city, total_volume, dims[0], dims[1], dims[2])
                if quick_cost:
                    response = f"""
🎯 **Айсулу всё поняла! Рассчитываю доставку...** 🌸

📦 **Ваш заказ:**
• {context['boxes_count']} коробок {product_type}
• Вес каждой: {weight} кг
• Размер: {dims[0]*100 if dims[0] else '?'}×{dims[1]*100 if dims[1] else '?'}×{dims[2]*100 if dims[2] else '?'} см
• Общий вес: {total_weight} кг

""" + calculate_detailed_cost(quick_cost, total_weight, product_type, city)
                else:
                    response = "❌ Ой, не могу рассчитать! Проверьте данные 🌸"
            else:
                # Расчет для одной коробки
                quick_cost = calculate_quick_cost(weight, product_type, city, None, dims[0], dims[1], dims[2])
                if quick_cost:
                    response = calculate_detailed_cost(quick_cost, weight, product_type, city)
                else:
                    response = "❌ Ой, ошибка расчета! 🌸"
            
            # Очищаем контекст после расчета
            session['context'] = {'weight': None, 'city': None, 'product_type': None, 'dimensions': None, 'boxes_count': None}
            return jsonify({"response": response})
        # ==== КОНЕЦ КОНТЕКСТНОЙ ПАМЯТИ ====

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

*Примеры:*
• "50 кг мебели в Алматы, габариты 2×1×0.5 м"
• "Где мой груз GZ123456?"
• "Расскажите про тарифы и оплату"
• "Хочу оставить заявку на доставку"

**Жарайсың! Давайте начнем!** 💫
            """})

        # Проверка на множественные коробки
        boxes = extract_boxes_from_message(user_message)
        if boxes and len(boxes) > 1:
            total_weight = sum(box['weight'] for box in boxes)
            session['multiple_boxes'] = boxes
            
            boxes_list = "\n".join([f"• {i+1}. {box['weight']} кг" for i, box in enumerate(boxes)])
            
            response = f"""
📦 **Ой, обнаружила несколько коробок!** 🌸
{boxes_list}

📊 **Общий вес:** {total_weight} кг

🏙️ **Для расчета укажите:**
• Город доставки
• Тип товара  
• Габариты коробок

💡 **Пример:** "в Астану, одежда, коробки 60×40×30 см"

**Жарайсың! Продолжаем!** 💫
            """
            return jsonify({"response": response})

        # Проверка на паллеты
        pallets = extract_pallets_from_message(user_message)
        if pallets:
            total_weight = sum(pallet['weight'] for pallet in pallets)
            total_volume = sum(pallet['volume'] for pallet in pallets)
            
            response = f"""
🎯 **Обнаружены паллеты!** 🌸
• Количество: {len(pallets)} шт
• Общий вес: {total_weight} кг  
• Общий объем: {total_volume:.1f} м³

🏙️ **Для точного расчета укажите:**
• Город доставки
• Тип товара на паллетах

💡 **Пример:** "в Караганду, мебель на паллетах"

**Отлично! Почти готово!** 😊
            """
            return jsonify({"response": response})

        # Определяем тип запроса
        text_lower = user_message.lower()
        
        # Отслеживание
        if any(word in text_lower for word in ['трек', 'отследить', 'статус', 'где', 'груз', 'посылка']) or re.search(r'\b(GZ|IY|SZ)[a-zA-Z0-9]', text_lower.upper()):
            response = process_tracking_request(user_message)
        
        # Заявка (есть имя и телефон)
        elif re.search(r'(?:имя|зовут|меня зовут)\s*[:\-]?\s*[а-яa-z]{2,}', text_lower) and re.search(r'\d{10,11}', text_lower):
            name_match = re.search(r'(?:имя|зовут|меня зовут)\s*[:\-]?\s*([а-яa-z]{2,})', text_lower)
            phone_match = re.search(r'(\d{10,11})', text_lower)
            
            if name_match and phone_match:
                name = name_match.group(1).capitalize()
                phone = phone_match.group(1)
                response = save_application(name, phone, user_message)
            else:
                response = "❌ Ой, не удалось распознать контакты! Укажите имя и телефон. 🌸"
        
        # Расчет доставки (есть параметры)
        elif (extract_weight(user_message) and extract_city(user_message)) or any(word in text_lower for word in ['рассчитай', 'посчитай', 'сколько', 'стоимость', 'доставк']):
            response = process_delivery_request(user_message)
        
        # Приветствие
        elif any(greeting in text_lower for greeting in GREETINGS):
            response = "Сәлеметсіз бе! 🌸 Я Айсулу - ваш помощник в доставке из Китая! Чем могу помочь?"
        
        # Общие вопросы - используем Gemini с личностью Айсулу
        else:
            response = get_gemini_response(user_message, session.get('chat_history', []))

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
