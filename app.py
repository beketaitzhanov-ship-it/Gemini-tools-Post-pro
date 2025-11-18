import os
import logging
import requests
import json
import psycopg2
import re
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# Вебхуки Make.com
MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK') # Сценарий 3 (Категория)
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')     # Сценарий 1 (Контракт)
MAKE_AI_CHAT_WEBHOOK = os.getenv('MAKE_AI_CHAT_WEBHOOK')       # Сценарий 4 (Болталка)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗАГРУЗКА КОНФИГА ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    EXCHANGE_RATE = CONFIG.get('EXCHANGE_RATE', {}).get('rate', 500)
except:
    CONFIG = {}
    EXCHANGE_RATE = 500

WAREHOUSE_NAMES = {"GZ": "Гуанчжоу", "FS": "Фошань", "IW": "Иу"}

# --- СОСТОЯНИЯ ---
# Клиент
(CLIENT_CITY, CLIENT_WAREHOUSE, CLIENT_PRODUCT, CLIENT_WEIGHT, 
 CLIENT_VOLUME, CLIENT_ADD_MORE, CLIENT_DECISION, CLIENT_NAME, CLIENT_PHONE) = range(9)

# Админ
(ADM_NAME, ADM_PHONE, ADM_CITY, ADM_WAREHOUSE, ADM_PRODUCT, 
 ADM_WEIGHT, ADM_VOLUME, ADM_RATE) = range(9, 17)

# --- ФУНКЦИИ ---

def get_db_connection():
    try: return psycopg2.connect(DATABASE_URL)
    except: return None

def clean_number(text):
    if not text: return 0.0
    try: return float(text.replace(',', '.').strip())
    except: return 0.0

def parse_volume_input(text):
    """Парсер объема: '60*50*40' -> м3"""
    text = text.lower().replace('х', 'x').replace('*', 'x')
    try:
        val = float(text.replace(',', '.'))
        if val < 20: return val 
    except: pass
    
    dims = re.findall(r'(\d+[.,]?\d*)', text)
    if len(dims) >= 3:
        l = float(dims[-3].replace(',', '.'))
        w = float(dims[-2].replace(',', '.'))
        h = float(dims[-1].replace(',', '.'))
        count = 1
        if len(dims) >= 4: count = float(dims[-4].replace(',', '.'))
        return round((l * w * h / 1000000) * count, 4)
    return 0.0

def get_product_category_from_ai(text):
    """Сценарий 3: Gemini Categorizer"""
    if not MAKE_CATEGORIZER_WEBHOOK: return "obshhie"
    try:
        resp = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': text}, timeout=10)
        key = resp.json().get('category_key')
        return key.lower() if key else "obshhie"
    except: return "obshhie"

def calculate_t1_line_item(weight, volume, category_key, warehouse):
    """Расчет одной позиции"""
    rates = CONFIG.get('T1_RATES_DENSITY', {}).get(warehouse, CONFIG.get('T1_RATES_DENSITY', {}).get('GZ', {}))
    cat_rates = rates.get(category_key, rates.get('obshhie'))
    
    density = weight / volume if volume > 0 else 0
    base_price = 0
    
    if cat_rates:
        for r in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= r.get('min_density', 0):
                base_price = r.get('price', 0); break
        if base_price == 0: base_price = cat_rates[-1].get('price', 0)

    client_rate = base_price * 1.30
    is_cbm = client_rate > 50
    cost = (client_rate * volume) if is_cbm else (client_rate * weight)
    
    return round(cost, 2), round(client_rate, 2), round(density, 2), is_cbm

def calculate_t2_total(total_weight, city_name):
    zone = "5"
    if CONFIG and 'DESTINATION_ZONES' in CONFIG:
        for k, v in CONFIG['DESTINATION_ZONES'].items():
            if k in city_name.lower(): zone = v; break
    if zone == "алматы": return 0, 0
    
    rate = {"1": 0.4, "2": 0.5, "3": 0.6, "4": 0.7, "5": 0.8}.get(str(zone), 0.8)
    return int(total_weight * rate * EXCHANGE_RATE), rate

# --- ЖИВОЕ ОБЩЕНИЕ (Сценарий 4) ---
async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    # Если это трек-номер -> Отследить
    if re.match(r'^[A-Za-z0-9-]{5,}$', user_text) and len(user_text) < 20:
        return await track_cargo(update, context)

    # Если нет вебхука или нажали кнопку в меню (которая не сработала как команда)
    if not MAKE_AI_CHAT_WEBHOOK or user_text in ["🚚 Калькулятор", "🔎 Отследить груз"]:
        await start(update, context)
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        resp = requests.post(MAKE_AI_CHAT_WEBHOOK, json={'text_message': user_text}, timeout=20)
        await update.message.reply_text(resp.text)
    except:
        # Если AI не ответил, просто показываем меню
        await start(update, context)

# --- КАРТА ---
def generate_vertical_map(status, progress, warehouse_code="GZ", city_to="Алматы"):
    start_city = WAREHOUSE_NAMES.get(warehouse_code, "Гуанчжоу")
    route = [start_city, "Чанша", "Сиань", "Ланьчжоу", "Урумчи", "Хоргос (Граница)", city_to]
    pos = 0
    if progress >= 100: pos = 6
    elif progress >= 90: pos = 5
    elif progress >= 70: pos = 4
    elif progress >= 50: pos = 3
    elif progress >= 30: pos = 2
    elif progress >= 15: pos = 1
    
    map_lines = []
    for i, city in enumerate(route):
        if i < pos: map_lines.append(f"✅ {city}\n      ⬇️")
        elif i == pos: map_lines.append(f"🚚 <b>{city.upper()}</b> 📍" + ("\n      ⬇️" if i != 6 else ""))
        else: map_lines.append(f"⬜️ {city}" + ("\n      ⬇️" if i != 6 else ""))
    return "\n".join(map_lines)

async def track_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track = update.message.text.strip().upper()
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT status, actual_weight, product, warehouse_code, client_city, route_progress FROM shipments WHERE track_number = %s OR contract_num = %s", (track, track))
    row = cur.fetchone()
    conn.close()

    if row:
        status, weight, product, wh_code, city, progress = row
        if not wh_code: wh_code = "GZ"
        if not city: city = "Алматы"
        progress = progress if progress is not None else 10
        visual = generate_vertical_map(status, progress, wh_code, city)
        await update.message.reply_text(f"📦 <b>ГРУЗ НАЙДЕН!</b>\n🆔 {track}\n📄 {product}\n⚖️ {weight} кг\n📍 <b>{status}</b>\n\n{visual}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Груз не найден. Проверьте трек.")

# --- КЛИЕНТСКИЙ КАЛЬКУЛЯТОР ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🚚 Калькулятор")], [KeyboardButton("🔎 Отследить груз")]]
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я — Айсулу, ИИ-менеджер Post Pro.</b>\n"
        "Вы можете написать мне любой вопрос или выбрать действие:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML'
    )
    return ConversationHandler.END

async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cart'] = []
    await update.message.reply_text("🏙 Введите <b>Город доставки</b> (в Казахстане):", parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
    return CLIENT_CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['city'] = update.message.text
    kb = [[KeyboardButton("🇨🇳 Гуанчжоу"), KeyboardButton("🇨🇳 Фошань")], [KeyboardButton("🇨🇳 Иу")]]
    await update.message.reply_text("🏭 <b>С какого склада отправка?</b>", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True), parse_mode='HTML')
    return CLIENT_WAREHOUSE

async def get_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    code = "GZ"
    if "Фошань" in text: code = "FS"
    elif "Иу" in text: code = "IW"
    context.user_data['wh_code'] = code
    context.user_data['wh_name'] = WAREHOUSE_NAMES.get(code, "Гуанчжоу")
    await update.message.reply_text(f"✅ Склад: {context.user_data['wh_name']}\n📦 <b>Напишите товар</b> (например: 'ткани'):", reply_markup=ReplyKeyboardRemove(), parse_mode='HTML')
    return CLIENT_PRODUCT

async def get_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    msg = await update.message.reply_text("⏳ <i>Смотрю категорию...</i>", parse_mode='HTML')
    key = get_product_category_from_ai(text)
    context.user_data['current_item'] = {'name': text, 'category': key}
    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"📦 {text} ({key})\n⚖️ <b>Вес (кг):</b>", parse_mode='HTML')
    return CLIENT_WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = clean_number(update.message.text)
    if w <= 0:
        await update.message.reply_text("🔢 Число:")
        return CLIENT_WEIGHT
    context.user_data['current_item']['weight'] = w
    await update.message.reply_text("📦 <b>Объем (м³)</b> или габариты (60*50*40):", parse_mode='HTML')
    return CLIENT_VOLUME

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vol = parse_volume_input(update.message.text)
    if vol <= 0: vol = context.user_data['current_item']['weight'] / 200
    context.user_data['current_item']['volume'] = vol
    context.user_data['cart'].append(context.user_data['current_item'])
    
    kb = [[KeyboardButton("➕ Добавить еще товар")], [KeyboardButton("🏁 Рассчитать итог")]]
    await update.message.reply_text(f"✅ В списке: {len(context.user_data['cart'])} поз.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return CLIENT_ADD_MORE

async def handle_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Добавить" in update.message.text:
        await update.message.reply_text("📦 Название следующего товара:", reply_markup=ReplyKeyboardRemove())
        return CLIENT_PRODUCT
    return await show_final_report(update, context)

async def show_final_report(update, context):
    d = context.user_data
    total_w = sum(i['weight'] for i in d['cart'])
    total_v = sum(i['volume'] for i in d['cart'])
    
    t1_total = 0
    details = ""
    
    for item in d['cart']:
        cost, rate, dens, is_cbm = calculate_t1_line_item(item['weight'], item['volume'], item['category'], d['wh_code'])
        t1_total += cost
        unit = "м³" if is_cbm else "кг"
        details += f"• {item['name']}: {item['weight']}кг -> <b>${cost:.2f}</b> (${rate}/{unit})\n"

    t2_kzt, _ = calculate_t2_total(total_w, d['city'])
    
    report = (
        f"📊 <b>Детальный расчет для г. {d['city']}:</b>\n"
        f"Склад: {d['wh_name']} | Вес: {total_w} кг | Объем: {total_v:.2f} м³\n\n"
        f"<b>Т1: Китай -> Алматы</b>\n{details}"
        f"⭐️ <b>Итого Т1: ${t1_total:.2f} USD</b>\n"
        f"<i>Оплата в тенге по курсу дня.</i>\n\n"
        f"<b>*Доставка по РК</b>\n• Тариф: <b>~{t2_kzt} ₸</b>\n\n"
        f"💡 Страховка: 1% | 💳 Оплата при получении"
    )
    
    kb = [[KeyboardButton("✅ Оставить заявку")], [KeyboardButton("🔄 Новый расчет")]]
    await update.message.reply_text(report, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    return CLIENT_DECISION

async def client_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Оставить" in update.message.text:
        await update.message.reply_text("👤 Как к вам обращаться? (Имя):", reply_markup=ReplyKeyboardRemove())
        return CLIENT_NAME
    return await calc_start(update, context)

async def client_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['client_name'] = update.message.text
    await update.message.reply_text("📱 Ваш телефон:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Контакт", request_contact=True)]], resize_keyboard=True))
    return CLIENT_PHONE

async def client_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    d = context.user_data
    if ADMIN_CHAT_ID:
        try: await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🔥 <b>ЗАЯВКА</b>\n👤 {d['client_name']} {phone}\n🏙 {d['city']}", parse_mode='HTML')
        except: pass
    await update.message.reply_text("✅ Заявка принята!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Отмена.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

# --- АДМИНКА (Скрытая) ---
async def admin_start(u, c): 
    if str(u.effective_user.id) != str(ADMIN_CHAT_ID): return ConversationHandler.END
    kb = [[KeyboardButton("📝 Создать контракт")], [KeyboardButton("🔙 Выход")]]
    await u.message.reply_text("👨‍💻 Админка", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return ConversationHandler.END

async def admin_create(u, c):
    if str(u.effective_user.id) != str(ADMIN_CHAT_ID): return ConversationHandler.END
    await u.message.reply_text("👤 ФИО Клиента:", reply_markup=ReplyKeyboardRemove()); return ADM_NAME
async def admin_name(u, c): c.user_data['aname'] = u.message.text; await u.message.reply_text("📱 Телефон:"); return ADM_PHONE
async def admin_phone(u, c): c.user_data['aphone'] = u.message.text; await u.message.reply_text("🏙 Город:"); return ADM_CITY
async def admin_city(u, c): c.user_data['acity'] = u.message.text; await u.message.reply_text("🏭 Склад (GZ/IW/FS):"); return ADM_WAREHOUSE
async def admin_wh(u, c): 
    t = u.message.text; code = "GZ"
    if "IW" in t: code="IW"
    elif "FS" in t: code="FS"
    c.user_data['awh'] = code
    await u.message.reply_text("📦 Товар:"); return ADM_PRODUCT
async def admin_prod(u, c):
    c.user_data['aprod'] = get_product_category_from_ai(u.message.text)
    await u.message.reply_text(f"Кат: {c.user_data['aprod']}\n⚖️ Вес:"); return ADM_WEIGHT
async def admin_w(u, c): c.user_data['aw'] = clean_number(u.message.text); await u.message.reply_text("📦 Объем:"); return ADM_VOLUME
async def admin_v(u, c): c.user_data['av'] = clean_number(u.message.text); await u.message.reply_text("💰 Тариф:"); return ADM_RATE
async def admin_fin(u, c):
    rate = clean_number(u.message.text); d = c.user_data
    num = f"CN-{int(datetime.now().timestamp())}"
    
    # DB
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO shipments (contract_num, fio, phone, client_city, warehouse_code, product, declared_weight, declared_volume, agreed_rate, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'оформлен',NOW())", 
                    (num, d['aname'], d['aphone'], d['acity'], d['awh'], d['aprod'], d['aw'], d['av'], rate))
        conn.commit(); conn.close()
    
    # MAKE
    if MAKE_CONTRACT_WEBHOOK:
        try: requests.post(MAKE_CONTRACT_WEBHOOK, json={"action":"create","contract_num":num,"fio":d['aname'],"phone":d['aphone'],"warehouse_code":d['awh'],"product":d['aprod'],"declared_weight":d['aw'],"declared_volume":d['av'],"rate":rate,"created_at":str(datetime.now())}, timeout=5)
        except: pass
        
    await u.message.reply_text(f"✅ {num} создан!"); return await admin_start(u, c)

# --- SETUP ---
def setup_application():
    app = Application.builder().token(TOKEN).build()
    
    # Клиент
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🚚 Калькулятор$'), calc_start)],
        states={
            CLIENT_CITY: [MessageHandler(filters.TEXT, get_city)],
            CLIENT_WAREHOUSE: [MessageHandler(filters.TEXT, get_warehouse)],
            CLIENT_PRODUCT: [MessageHandler(filters.TEXT, get_product)],
            CLIENT_WEIGHT: [MessageHandler(filters.TEXT, get_weight)],
            CLIENT_VOLUME: [MessageHandler(filters.TEXT, get_volume)],
            CLIENT_ADD_MORE: [MessageHandler(filters.TEXT, handle_add_more)],
            CLIENT_DECISION: [MessageHandler(filters.TEXT, client_decision)],
            CLIENT_NAME: [MessageHandler(filters.TEXT, client_get_name)],
            CLIENT_PHONE: [MessageHandler(filters.CONTACT | filters.TEXT, client_finish)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    # Админ
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📝 Создать контракт$'), admin_create)],
        states={
            ADM_NAME: [MessageHandler(filters.TEXT, admin_name)],
            ADM_PHONE: [MessageHandler(filters.TEXT, admin_phone)],
            ADM_CITY: [MessageHandler(filters.TEXT, admin_city)],
            ADM_WAREHOUSE: [MessageHandler(filters.TEXT, admin_wh)],
            ADM_PRODUCT: [MessageHandler(filters.TEXT, admin_prod)],
            ADM_WEIGHT: [MessageHandler(filters.TEXT, admin_w)],
            ADM_VOLUME: [MessageHandler(filters.TEXT, admin_v)],
            ADM_RATE: [MessageHandler(filters.TEXT, admin_fin)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('admin', admin_start))
    app.add_handler(MessageHandler(filters.Regex('^🔙 Выход'), start))
    app.add_handler(MessageHandler(filters.Regex('^🔎 Отследить груз$'), lambda u,c: u.message.reply_text("Уважаемый клиент, введите трэк номер:")))
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z0-9-]{5,}$') & ~filters.COMMAND, track_cargo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_chat))
    
    return app

if __name__ == '__main__':
    try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    except: pass
    if not TOKEN: logger.error("NO TOKEN")
    else:
        app = setup_application()
        app.run_polling()