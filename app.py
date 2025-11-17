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

# --- НАСТРОЙКИ ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL") 

app = Flask(__name__)
# ... (Настройки REDIS и SESSION остаются без изменений) ...
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_REDIS'] = redis.from_url(REDIS_URL)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'postpro-secret-key-2024')
Session(app)

# 🔥 ЗАГРУЖАЕМ ЕДИНЫЙ CONFIG.JSON
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    T1_RATES = CONFIG['T1_RATES_DENSITY']
    T2_RATES = CONFIG['T2_RATES_DETAILED']
    ZONES = CONFIG['DESTINATION_ZONES']
    EXCHANGE_RATE = CONFIG['EXCHANGE_RATE']['rate']
except Exception as e:
    logger.error(f"!!! КРИТИЧЕСКАЯ ОШИБКА: Не могу загрузить config.json: {e}")
    T1_RATES, T2_RATES, ZONES, EXCHANGE_RATE = {}, {}, {}, 550

# ... (Загрузчик Промптов остается без изменений) ...
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
PERSONALITY_PROMPT = ConfigLoader.load_prompt_file('personality_prompt.txt', 'Личность')
CALCULATION_PROMPT = ConfigLoader.load_prompt_file('calculation_prompt.txt', 'Расчеты')
def create_aisulu_prompt():
    base_prompt = ""
    if PERSONALITY_PROMPT: base_prompt += PERSONALITY_PROMPT + "\n\n"
    if CALCULATION_PROMPT: base_prompt += CALCULATION_PROMPT + "\n\n"
    return base_prompt or "Ты - Айсулу, помощник Post Pro."
AISULU_PROMPT = create_aisulu_prompt()

# --- ИНСТРУМЕНТЫ (Добавляем Склад) ---
tools = [
    {
        "function_declarations": [
            {
                "name": "calculate_delivery_cost",
                "description": "Рассчитать стоимость доставки (T1+T2)",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "weight_kg": {"type": "NUMBER"},
                        "city": {"type": "STRING"},
                        "product_type": {"type": "STRING"},
                        "volume_m3": {"type": "NUMBER"},
                        "warehouse_code": {
                            "type": "STRING",
                            "description": "Код склада в Китае: GZ (Гуанчжоу), FS (Фошань), или IW (Иу)"
                        }
                    },
                    "required": ["weight_kg", "city", "product_type", "volume_m3", "warehouse_code"]
                }
            },
            {
                "name": "track_shipment",
                 "parameters": { "type": "OBJECT", "properties": {"tracking_number": {"type": "STRING"}}, "required": ["tracking_number"] }
            },
            {
                "name": "save_customer_application",
                 "parameters": { "type": "OBJECT", "properties": {"name": {"type": "STRING"}, "phone": {"type": "STRING"}, "details": {"type": "STRING"}}, "required": ["name", "phone"] }
            }
        ]
    }
]

# ... (Инициализация Gemini) ...
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


# --- КАЛЬКУЛЯТОР (ВНУТРИ APP.PY, ЧИТАЕТ CONFIG.JSON) ---
# (Этот код дублирует логику calculator.py, чтобы app.py был независимым)
def get_t1_cost(weight, volume, category_name="общие", warehouse_code="GZ"):
    try:
        density = weight / volume if volume > 0 else 0
        warehouse_rates = T1_RATES.get(warehouse_code, T1_RATES.get("GZ"))
        rules = warehouse_rates.get(category_name, warehouse_rates.get("общие"))
        for rule in sorted(rules, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= rule.get('min_density', 0):
                price = rule.get('price', 0)
                unit = rule.get('unit', 'kg')
                cost_usd = price * volume if unit == 'm3' else price * weight
                return cost_usd, price, density
        return 0, 0, density
    except Exception as e:
        logger.error(f"Ошибка T1: {e}"); return 0, 0, 0

def get_t2_cost(weight, zone):
    try:
        if zone == 'алматы': return 0
        rules = T2_RATES.get('large_parcel', {})
        weight_ranges = rules.get('weight_ranges', [])
        extra_rates = rules.get('extra_kg_rate', {})
        for r in weight_ranges:
            if weight <= r['max']:
                return float(r['zones'].get(zone, 0))
        if weight_ranges:
            max_w = weight_ranges[-1]['max']
            base_cost = float(weight_ranges[-1]['zones'].get(zone, 0))
            extra_rate = float(extra_rates.get(zone, 300))
            return base_cost + ((weight - max_w) * extra_rate)
        return weight * 300
    except Exception as e:
        logger.error(f"Ошибка T2: {e}"); return 0

def calculate_all(weight, volume, product_type, city, warehouse_code="GZ"):
    rate_kzt = EXCHANGE_RATE
    
    # T1 + 30%
    raw_t1_usd, raw_rate, density = get_t1_cost(weight, volume, product_type, warehouse_code)
    client_t1_usd = raw_t1_usd * 1.30
    client_rate = raw_rate * 1.30
    
    # T2 + 20%
    zone = ZONES.get(city.lower(), "5")
    client_t2_kzt = get_t2_cost(weight, zone) * 1.20
    
    total_usd = client_t1_usd # В договор идет T1
    total_kzt_estimate = (client_t1_usd * rate_kzt) + client_t2_kzt

    return {
        "success": True, "weight": weight, "volume": volume, "density": round(density, 2),
        "tariff_rate": round(client_rate, 2), "t1_usd": round(client_t1_usd, 2),
        "t2_kzt": round(client_t2_kzt, 2), "total_usd": round(total_usd, 2),
        "total_kzt": round(total_kzt_estimate), "warehouse_code": warehouse_code
    }
# --- КОНЕЦ КАЛЬКУЛЯТОРА ---

def format_calculation_result(result):
    if not result.get('success'):
        return f"❌ {result.get('error', 'Ошибка расчета')}"
    
    return f"""
📊 **Предварительный расчет (Склад: {result.get('warehouse_code')})**

• Вес: {result.get('weight')} кг
• Объем: {result.get('volume')} м³
• Тариф: ${result.get('tariff_rate')}/кг (или за куб)

• Доставка Китай-Алматы: **${result.get('t1_usd')}**
• Доставка по РК (до г. {result.get('city', '...')}): **{result.get('t2_kzt'):,} ₸** (доп. оплата)

⚠️ **Точный расчет будет по факту приемки на складе.**
"""

def process_tracking_request(tracking_number):
    try:
        if not tracking_number: return {"error": "Нет номера"}
        tracking_number = tracking_number.strip().upper()
        
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT fio, product, status, route_progress FROM shipments WHERE track_number = %s OR contract_num = %s", (tracking_number, tracking_number))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            fio, product, status, progress = row
            map_text = "..." # (Логика карты)
            return f"📦 **Статус {tracking_number}**\n👤 {fio}\n🔄 {status} ({progress}%)"
        else:
            return "❌ Груз не найден."
    except Exception as e:
        logger.error(f"Ошибка DB: {e}"); return "⚠️ Ошибка отслеживания."

def save_application(name, phone, details=None):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("INSERT INTO applications (timestamp, name, phone, details, source) VALUES (NOW(), %s, %s, %s, 'Aisulu Web')", (name, phone, details))
        app_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        if MAKE_WEBHOOK_URL:
            try: requests.post(MAKE_WEBHOOK_URL, json={"type": "new_lead", "id": app_id, "name": name, "phone": phone, "details": details}, timeout=1)
            except: pass
        return {"success": True, "message": "✅ Заявка принята! Менеджер свяжется."}
    except Exception as e:
        logger.error(f"Ошибка заявки: {e}"); return {"error": "Ошибка сохранения"}

def execute_tool_function(function_name, parameters):
    try:
        if function_name == "calculate_delivery_cost":
            # Айсулу теперь тоже читает из config.json
            res = calculate_all(
                weight=float(parameters.get('weight_kg', 0)),
                volume=float(parameters.get('volume_m3') or 0.1),
                product_type=parameters.get('product_type', 'общие'),
                city=parameters.get('city', 'Алматы'),
                warehouse_code=parameters.get('warehouse_code', 'GZ') # GZ по умолчанию
            )
            res['product_type'] = parameters.get('product_type')
            res['city'] = parameters.get('city')
            return format_calculation_result(res)

        elif function_name == "track_shipment":
            return process_tracking_request(parameters.get('tracking_number'))
        
        elif function_name == "save_customer_application":
            return save_application(parameters.get('name'), parameters.get('phone'), parameters.get('details'))
            
        return "Функция не найдена"
    except Exception as e:
        logger.error(f"Tool Error: {e}"); return "Ошибка операции"

# --- (Остальной код Flask @app.route('/') и @app.route('/chat') остается БЕЗ ИЗМЕНЕНИЙ) ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json.get('message', '').strip()
        if not user_message: return jsonify({"response": "..."})

        track_match = re.search(r'\b(GZ|IY|FS|DOC-|CN-)\d+\b', user_message.upper())
        if track_match:
            return jsonify({"response": process_tracking_request(track_match.group(0))})

        if 'chat_history' not in session: session['chat_history'] = []
        
        messages = [{"role": "user", "parts": [{"text": AISULU_PROMPT}]}]
        # ... (Код истории чата)
        for i in range(0, len(session['chat_history']), 2):
            if i+1 < len(session['chat_history']):
                messages.append({"role": "user", "parts": [{"text": session['chat_history'][i][8:]}]})
                messages.append({"role": "model", "parts": [{"text": session['chat_history'][i+1][8:]}]})
        
        messages.append({"role": "user", "parts": [{"text": user_message}]})

        if gemini_available:
            response = model.generate_content(messages, generation_config={'temperature': 0.7})
            
            final_text = "Извините, я задумалась."
            
            if response.candidates:
                part = response.candidates[0].content.parts[0]
                
                if hasattr(part, 'function_call') and part.function_call:
                    tool_response = execute_tool_function(part.function_call.name, dict(part.function_call.args))
                    
                    if isinstance(tool_response, str) and ("📊" in tool_response or "📦" in tool_response):
                        final_text = tool_response
                    else:
                        final_text = str(tool_response)
                else:
                    final_text = part.text
            
            session['chat_history'].append(f"Клиент: {user_message}")
            session['chat_history'].append(f"Айсулу: {final_text}")
            if len(session['chat_history']) > 10: session['chat_history'] = session['chat_history'][-10:]
            
            return jsonify({"response": final_text})
        else:
            return jsonify({"response": "Сервис временно перегружен."})

    except Exception as e:
        logger.error(f"Chat Error: {e}")
        return jsonify({"response": "Произошла ошибка."})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))