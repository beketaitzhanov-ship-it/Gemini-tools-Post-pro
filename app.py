# -*- coding: utf-8 -*-
import os
import json
import logging
import re
import psycopg2
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from flask_session import Session  
import redis                     
import google.generativeai as genai
import google.generativeai.types as genai_types
from dotenv import load_dotenv

# 👇 ИМПОРТ НОВОГО КАЛЬКУЛЯТОРА
from calculator import LogisticsCalculator

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")

app = Flask(__name__)

# ===== НАСТРОЙКА СЕССИИ (REDIS) =====
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_REDIS'] = redis.from_url(REDIS_URL)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'postpro-secret-key-2024')
Session(app)

# ===== ЗАГРУЗЧИК КОНФИГУРАЦИИ =====
class ConfigLoader:
    @staticmethod
    def load_prompt_file(filename, description):
        try:
            if not os.path.exists(filename):
                logger.warning(f"⚠️ Файл {filename} не найден")
                return ""
            with open(filename, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return ""

# Загружаем промпты
PERSONALITY_PROMPT = ConfigLoader.load_prompt_file('personality_prompt.txt', 'Личность')
CALCULATION_PROMPT = ConfigLoader.load_prompt_file('calculation_prompt.txt', 'Расчеты')

def create_aisulu_prompt():
    base_prompt = ""
    if PERSONALITY_PROMPT: base_prompt += PERSONALITY_PROMPT + "\n\n"
    if CALCULATION_PROMPT: base_prompt += CALCULATION_PROMPT + "\n\n"
    if not base_prompt.strip():
        base_prompt = "Ты - Айсулу, помощник Post Pro."
    return base_prompt

AISULU_PROMPT = create_aisulu_prompt()

# ===== ИНСТРУМЕНТЫ GEMINI =====
tools = [
    {
        "function_declarations": [
            {
                "name": "calculate_delivery_cost",
                "description": "Рассчитать стоимость доставки (T1+T2) с учетом плотности и категории",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "weight_kg": {"type": "NUMBER", "description": "Вес в кг"},
                        "city": {"type": "STRING", "description": "Город назначения"},
                        "product_type": {"type": "STRING", "description": "Тип товара"},
                        "volume_m3": {"type": "NUMBER", "description": "Объем в м3"},
                        "length_m": {"type": "NUMBER"},
                        "width_m": {"type": "NUMBER"},
                        "height_m": {"type": "NUMBER"}
                    },
                    "required": ["weight_kg", "city", "product_type"]
                }
            },
            {
                "name": "track_shipment",
                "description": "Отследить груз по трек-номеру (GZ...)",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"tracking_number": {"type": "STRING"}},
                    "required": ["tracking_number"]
                }
            },
            {
                "name": "save_customer_application",
                "description": "Сохранить заявку клиента",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "phone": {"type": "STRING"},
                        "details": {"type": "STRING"}
                    },
                    "required": ["name", "phone"]
                }
            }
        ]
    }
]

# Инициализация Gemini
model = None
gemini_available = False
try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.0-flash', tools=tools)
        gemini_available = True
        logger.info("✅ Gemini запущена")
except Exception as e:
    logger.error(f"❌ Ошибка Gemini: {e}")

# ===== ФУНКЦИИ (ТЕПЕРЬ ЧЕРЕЗ DB И CALCULATOR) =====

def format_calculation_result(result):
    """Формирует красивый ответ клиенту с ПРЕДУПРЕЖДЕНИЕМ"""
    if not result.get('success'):
        return f"❌ {result.get('error', 'Ошибка расчета')}"
    
    return f"""
📊 **Предварительный расчет доставки (Post Pro):**

🏷 **Параметры:**
• Груз: {result.get('product_type', 'Товар')}
• Вес: {result.get('weight')} кг
• Объем: {result.get('volume')} м³
• Город: {result.get('city')}

💰 **Стоимость:**
• Тариф Китай-Алматы: ${result.get('t1_usd')} (по курсу {result.get('exchange_rate')})
• Доставка по РК: {result.get('t2_kzt'):,} ₸
• **ИТОГО: ~{result.get('total_kzt'):,} ₸**

⚠️ **ВАЖНО:**
Это предварительный расчет.
**Точная стоимость будет зафиксирована по факту прибытия груза на сортировочный склад в г. Алматы.**
Дополнительные услуги (упаковка, страховка) рассчитываются отдельно.

⏱ **Сроки:** Гуанчжоу 12-15 дней | Иу 8-12 дней
"""

def process_tracking_request(tracking_number):
    """Отслеживание через Базу Данных"""
    try:
        if not tracking_number: return {"error": "Нет номера"}
        tracking_number = tracking_number.strip().upper()
        
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT fio, product, weight, status, route_progress 
            FROM shipments WHERE track_number = %s
        """, (tracking_number,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            fio, product, weight, status, progress = row
            
            # Карта маршрута
            route = [
                {"city": "🏭 Гуанчжоу", "progress": 0},
                {"city": "📍 Урумчи", "progress": 76},
                {"city": "🛃 Хоргос", "progress": 85},
                {"city": "🏙️ Алматы", "progress": 100}
            ]
            map_text = ""
            for point in route:
                map_text += f"✅ {point['city']}\n" if progress >= point['progress'] else f"⏳ {point['city']}\n"

            return f"""
📦 **Статус груза {tracking_number}**
👤 {fio} | 📦 {product} | ⚖️ {weight} кг
🔄 Статус: **{status}**

{map_text}
📊 Прогресс: {progress}%
"""
        else:
            return "❌ Груз с таким номером не найден в базе."
    except Exception as e:
        logger.error(f"Ошибка DB: {e}")
        return "⚠️ Ошибка сервиса отслеживания."

def save_application(name, phone, details=None):
    """Сохранение заявки в БД + Make"""
    try:
        # 1. В Базу
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("INSERT INTO applications (timestamp, name, phone, details) VALUES (NOW(), %s, %s, %s) RETURNING id", (name, phone, details))
        app_id = cur.fetchone()[0]
        conn.commit()
        conn.close()

        # 2. В Make (для уведомления менеджера)
        if MAKE_WEBHOOK_URL:
            try:
                requests.post(MAKE_WEBHOOK_URL, json={
                    "type": "new_lead",
                    "id": app_id,
                    "name": name,
                    "phone": phone,
                    "details": details
                }, timeout=1)
            except: pass

        return {"success": True, "message": "✅ Заявка принята! Менеджер свяжется с вами."}
    except Exception as e:
        logger.error(f"Ошибка заявки: {e}")
        return {"error": "Ошибка сохранения"}

def execute_tool_function(function_name, parameters):
    """Выполнение инструментов (Калькулятор, Трекинг, Заявка)"""
    try:
        if function_name == "calculate_delivery_cost":
            # 👇 ПОДКЛЮЧАЕМ НОВЫЙ КАЛЬКУЛЯТОР
            try:
                calc = LogisticsCalculator()
                
                # Если дали размеры, считаем объем
                vol = parameters.get('volume_m3')
                if not vol:
                    l = parameters.get('length_m', 0)
                    w = parameters.get('width_m', 0)
                    h = parameters.get('height_m', 0)
                    if l and w and h: vol = l * w * h
                
                result = calc.calculate_all(
                    weight=float(parameters.get('weight_kg', 0)),
                    volume=float(vol or 0),
                    product_type=parameters.get('product_type', 'общие'),
                    city=parameters.get('city', 'Алматы')
                )
                
                # Добавляем названия для форматирования
                result['product_type'] = parameters.get('product_type')
                result['city'] = parameters.get('city')
                
                return format_calculation_result(result)
            except Exception as calc_err:
                logger.error(f"Calc Error: {calc_err}")
                return "⚠️ Ошибка при расчете. Попробуйте позже."

        elif function_name == "track_shipment":
            return process_tracking_request(parameters.get('tracking_number'))
        
        elif function_name == "save_customer_application":
            return save_application(parameters.get('name'), parameters.get('phone'), parameters.get('details'))
            
        return "Функция не найдена"
    except Exception as e:
        logger.error(f"Tool Error: {e}")
        return "Ошибка выполнения операции"

# ===== ОСНОВНОЙ ЧАТ-БОТ =====

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json.get('message', '').strip()
        if not user_message: return jsonify({"response": "..."})

        # 1. Проверка на трек-номер (быстрый поиск)
        track_match = re.search(r'\b(GZ|IY|SZ|DOC-)\d+\b', user_message.upper())
        if track_match:
            return jsonify({"response": process_tracking_request(track_match.group(0))})

        # 2. История чата
        if 'chat_history' not in session: session['chat_history'] = []
        
        messages = [{"role": "user", "parts": [{"text": AISULU_PROMPT}]}]
        # Добавляем историю
        for i in range(0, len(session['chat_history']), 2):
            if i+1 < len(session['chat_history']):
                messages.append({"role": "user", "parts": [{"text": session['chat_history'][i][8:]}]})
                messages.append({"role": "model", "parts": [{"text": session['chat_history'][i+1][8:]}]})
        
        messages.append({"role": "user", "parts": [{"text": user_message}]})

        # 3. Запрос к Gemini
        if gemini_available:
            response = model.generate_content(messages, generation_config={'temperature': 0.7})
            
            final_text = "Извините, я задумалась."
            
            if response.candidates:
                part = response.candidates[0].content.parts[0]
                
                # Если Gemini хочет вызвать функцию
                if hasattr(part, 'function_call') and part.function_call:
                    tool_response = execute_tool_function(part.function_call.name, dict(part.function_call.args))
                    
                    # Отправляем результат функции обратно в Gemini, чтобы она сформулировала ответ
                    # (Или можно вернуть результат напрямую, если это готовый текст)
                    if isinstance(tool_response, str) and ("📊" in tool_response or "📦" in tool_response):
                        final_text = tool_response # Вернуть красивый готовый текст
                    else:
                        # Если функция вернула JSON, пусть Gemini опишет его (упрощенно)
                        final_text = str(tool_response)
                else:
                    final_text = part.text
            
            # Сохраняем в историю
            session['chat_history'].append(f"Клиент: {user_message}")
            session['chat_history'].append(f"Айсулу: {final_text}")
            if len(session['chat_history']) > 10: session['chat_history'] = session['chat_history'][-10:]
            
            return jsonify({"response": final_text})
        else:
            return jsonify({"response": "Сервис временно перегружен."})

    except Exception as e:
        logger.error(f"Chat Error: {e}")
        return jsonify({"response": "Произошла ошибка. Попробуйте еще раз."})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))