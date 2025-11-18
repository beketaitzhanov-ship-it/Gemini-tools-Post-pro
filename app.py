import os
import logging
import requests
import json
import psycopg2
import time
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ (ENVIRONMENT) ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 
MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK') # Сценарий 3 (Gemini)
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK') # Сценарий 1 (Таблица/PDF)

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗАГРУЗКА КОНФИГУРАЦИИ ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
except Exception as e:
    logger.error(f"Ошибка загрузки config.json: {e}")
    CONFIG = {}

# --- КОНСТАНТЫ ---
WAREHOUSE_INFO = {
    "GZ": {"name": "Гуанчжоу", "days": 12, "flag": "🇨🇳"},
    "FS": {"name": "Фошань", "days": 12, "flag": "🇨🇳"},
    "IW": {"name": "Иу", "days": 11, "flag": "🇨🇳"}
}

# --- СОСТОЯНИЯ (АЙСУЛУ) ---
CLIENT_NAME, CLIENT_CITY, CLIENT_PRODUCT, CLIENT_WEIGHT, CLIENT_VOLUME, CLIENT_PHONE = range(6)

# --- СОСТОЯНИЯ (АДМИНКА) ---
ADM_NAME, ADM_PHONE, ADM_CITY, ADM_WAREHOUSE, ADM_PRODUCT, ADM_WEIGHT, ADM_VOLUME, ADM_RATE = range(6, 14)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")
        return None

def clean_number(text):
    if not text: return 0.0
    try: return float(text.replace(',', '.').strip())
    except: return 0.0

def get_product_category_from_ai(product_text: str) -> str:
    """Gemini (Сценарий 3)"""
    if not MAKE_CATEGORIZER_WEBHOOK: return "obshhie"
    try:
        response = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': product_text}, timeout=15)
        response.raise_for_status()
        key = response.json().get('category_key')
        return key.lower() if key else "obshhie"
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "obshhie"

# --- ФУНКЦИИ ОТСЛЕЖИВАНИЯ (КАРТА) ---

def generate_vertical_map(status, progress, warehouse_code="GZ", city_to="Алматы"):
    start_city = "Гуанчжоу"
    if warehouse_code == "IW": start_city = "Иу"
    elif warehouse_code == "FS": start_city = "Фошань"

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
        if i < pos:
            map_lines.append(f"✅ {city}")
            map_lines.append("      ⬇️")
        elif i == pos:
            map_lines.append(f"🚚 <b>{city.upper()}</b> 📍")
            if i != len(route) - 1: map_lines.append("      ⬇️")
        else:
            map_lines.append(f"⬜️ {city}")
            if i != len(route) - 1: map_lines.append("      ⬇️")
                
    return "\n".join(map_lines)

async def track_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_number = update.message.text.strip().upper()
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT status, actual_weight, product, warehouse_code, client_city, route_progress FROM shipments WHERE track_number = %s OR contract_num = %s", (track_number, track_number))
    row = cur.fetchone()
    conn.close()

    if row:
        status, weight, product, wh_code, city, progress_db = row
        if not wh_code: wh_code = "GZ"
        if not city: city = "Алматы"
        progress = progress_db if progress_db is not None else 10 # Дефолт
        
        visual_map = generate_vertical_map(status, progress, wh_code, city)
        
        await update.message.reply_text(
            f"📦 <b>Груз найден!</b>\n🆔 {track_number}\n📄 {product}\n📍 <b>{status}</b>\n\n{visual_map}",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Груз не найден. Проверьте трек.")

# ==========================================
# 1. ЛОГИКА АДМИНА (СЕКРЕТНАЯ ДВЕРЬ)
# ==========================================

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # ПРОВЕРКА НА АДМИНА
    if user_id != str(ADMIN_CHAT_ID):
        await update.message.reply_text("⛔️ У вас нет доступа к админ-панели.")
        return ConversationHandler.END

    kb = [[KeyboardButton("📝 Создать контракт")], [KeyboardButton("🔙 Выход в режим клиента")]]
    await update.message.reply_text(
        "👨‍💻 <b>РЕЖИМ АДМИНИСТРАТОРА</b>\n\nЗдесь вы можете создавать контракты, которые увидит склад.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def admin_create_contract_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка админа (на всякий случай)
    if str(update.effective_user.id) != str(ADMIN_CHAT_ID): return ConversationHandler.END
    
    await update.message.reply_text("👤 Введите <b>ФИО Клиента</b>:", parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
    return ADM_NAME

async def admin_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['adm_name'] = update.message.text
    await update.message.reply_text("📱 Введите <b>Телефон</b>:", parse_mode='HTML')
    return ADM_PHONE

async def admin_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['adm_phone'] = update.message.text
    await update.message.reply_text("🏙 Введите <b>Город клиента</b>:", parse_mode='HTML')
    return ADM_CITY

async def admin_get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['adm_city'] = update.message.text
    kb = [[KeyboardButton("GZ (Гуанчжоу)"), KeyboardButton("IW (Иу)")], [KeyboardButton("FS (Фошань)")]]
    await update.message.reply_text("🏭 Выберите <b>Склад приема</b>:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True), parse_mode='HTML')
    return ADM_WAREHOUSE

async def admin_get_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    code = "GZ"
    if "IW" in text: code = "IW"
    elif "FS" in text: code = "FS"
    context.user_data['adm_wh'] = code
    
    await update.message.reply_text(f"✅ Склад: {code}\n📦 Введите <b>Название товара</b> (например: 'кроссовки'):", reply_markup=ReplyKeyboardRemove(), parse_mode='HTML')
    return ADM_PRODUCT

async def admin_get_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_prod = update.message.text
    # ТУТ МЫ ИСПОЛЬЗУЕМ GEMINI, ЧТОБЫ СКЛАД ПОНИМАЛ ТОВАР!
    msg = await update.message.reply_text("⏳ Определяю категорию для склада...")
    cat_key = get_product_category_from_ai(raw_prod)
    context.user_data['adm_prod'] = cat_key # Сохраняем ключ (odezhda)
    
    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"Категория: <b>{cat_key}</b>\n⚖️ Введите <b>План Вес (кг)</b>:", parse_mode='HTML')
    return ADM_WEIGHT

async def admin_get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['adm_weight'] = clean_number(update.message.text)
    await update.message.reply_text("📦 Введите <b>План Объем (м³)</b>:", parse_mode='HTML')
    return ADM_VOLUME

async def admin_get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['adm_vol'] = clean_number(update.message.text)
    await update.message.reply_text("💰 Введите <b>Тариф ($/кг)</b> (или 0, чтобы посчитать потом):", parse_mode='HTML')
    return ADM_RATE

async def admin_finish_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rate = clean_number(update.message.text)
    d = context.user_data
    
    # ГЕНЕРАЦИЯ НОМЕРА
    contract_num = f"CN-{int(datetime.now().timestamp())}"
    
    # СОХРАНЕНИЕ В БД (ДЛЯ СКЛАДА)
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        # Статус 'оформлен' - это то, что ищет Бот Склада!
        cur.execute("""
            INSERT INTO shipments (
                contract_num, fio, phone, client_city, warehouse_code, 
                product, declared_weight, declared_volume, agreed_rate, 
                status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'оформлен', NOW())
        """, (contract_num, d['adm_name'], d['adm_phone'], d['adm_city'], d['adm_wh'], d['adm_prod'], d['adm_weight'], d['adm_vol'], rate))
        conn.commit()
        conn.close()
        
    # ОТПРАВКА В MAKE (СЦЕНАРИЙ 1 - ТАБЛИЦА)
    if MAKE_CONTRACT_WEBHOOK:
        try:
            payload = {
                "action": "create",
                "contract_num": contract_num,
                "fio": d['adm_name'],
                "phone": d['adm_phone'],
                "warehouse_code": d['adm_wh'],
                "product": d['adm_prod'], # Отправляем ключ
                "declared_weight": d['adm_weight'],
                "declared_volume": d['adm_vol'],
                "rate": rate,
                "created_at": datetime.now().isoformat()
            }
            requests.post(MAKE_CONTRACT_WEBHOOK, json=payload, timeout=5)
        except: pass

    await update.message.reply_text(
        f"✅ <b>Контракт создан!</b>\n\n🆔 Номер: <code>{contract_num}</code>\n📦 Товар: {d['adm_prod']}\n🏭 Склад: {d['adm_wh']}\n\nБот склада теперь видит этот груз.",
        parse_mode='HTML'
    )
    # Возвращаем в админ меню
    return await admin_start(update, context)

# ==========================================
# 2. ЛОГИКА АЙСУЛУ (КЛИЕНТСКАЯ ЧАСТЬ)
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🚚 Рассчитать стоимость")], [KeyboardButton("🔎 Отследить груз")]]
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я — Айсулу, ваш менеджер.</b>\nЧем могу помочь?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML'
    )
    return ConversationHandler.END

async def client_calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Как к вам обращаться?")
    return CLIENT_NAME

async def client_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("🏙 Из какого вы города?")
    return CLIENT_CITY

async def client_get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['city'] = update.message.text
    await update.message.reply_text("📦 Что везем? (Например: 'автозапчасти')")
    return CLIENT_PRODUCT

async def client_get_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['product_text'] = text
    msg = await update.message.reply_text("⏳ Уточняю категорию...")
    key = get_product_category_from_ai(text)
    context.user_data['category_key'] = key
    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"Категория: <b>{key}</b>\n⚖️ Вес (кг):", parse_mode='HTML')
    return CLIENT_WEIGHT

async def client_get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['weight'] = clean_number(update.message.text)
    await update.message.reply_text("📦 Объем (м³):")
    return CLIENT_VOLUME

async def client_get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['volume'] = clean_number(update.message.text) or (context.user_data['weight']/200)
    await update.message.reply_text("📱 Ваш телефон:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Контакт", request_contact=True)]], one_time_keyboard=True, resize_keyboard=True))
    return CLIENT_PHONE

async def client_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    d = context.user_data
    
    # Простой расчет для примера (можно усложнить)
    total = d['weight'] * 5 # Заглушка, тут должна быть функция calculate_t1 из прошлого кода
    
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🔥 <b>ЗАЯВКА</b>\n👤 {d['name']} {phone}\n📦 {d['product_text']} ({d['category_key']})", parse_mode='HTML')
        except: pass
    
    await update.message.reply_text(f"✅ Расчет готов! Менеджер свяжется с вами.\nПримерная стоимость: ${total}", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Отмена.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

# --- СБОРКА ПРИЛОЖЕНИЯ ---
def setup_application():
    app = Application.builder().token(TOKEN).build()
    
    # 1. АДМИНСКАЯ ВЕТКА
    admin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📝 Создать контракт$'), admin_create_contract_start)],
        states={
            ADM_NAME: [MessageHandler(filters.TEXT, admin_get_name)],
            ADM_PHONE: [MessageHandler(filters.TEXT, admin_get_phone)],
            ADM_CITY: [MessageHandler(filters.TEXT, admin_get_city)],
            ADM_WAREHOUSE: [MessageHandler(filters.TEXT, admin_get_warehouse)],
            ADM_PRODUCT: [MessageHandler(filters.TEXT, admin_get_product)],
            ADM_WEIGHT: [MessageHandler(filters.TEXT, admin_get_weight)],
            ADM_VOLUME: [MessageHandler(filters.TEXT, admin_get_volume)],
            ADM_RATE: [MessageHandler(filters.TEXT, admin_finish_contract)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # 2. КЛИЕНТСКАЯ ВЕТКА
    client_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🚚 Рассчитать стоимость$'), client_calc_start)],
        states={
            CLIENT_NAME: [MessageHandler(filters.TEXT, client_get_name)],
            CLIENT_CITY: [MessageHandler(filters.TEXT, client_get_city)],
            CLIENT_PRODUCT: [MessageHandler(filters.TEXT, client_get_product)],
            CLIENT_WEIGHT: [MessageHandler(filters.TEXT, client_get_weight)],
            CLIENT_VOLUME: [MessageHandler(filters.TEXT, client_get_volume)],
            CLIENT_PHONE: [MessageHandler(filters.CONTACT | filters.TEXT, client_finish)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # РЕГИСТРАЦИЯ ХЭНДЛЕРОВ
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('admin', admin_start)) # <-- СЕКРЕТНАЯ КОМАНДА
    app.add_handler(MessageHandler(filters.Regex('^🔙 Выход'), start))
    
    app.add_handler(admin_conv)
    app.add_handler(client_conv)
    
    # Отслеживание
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z0-9-]{5,}$') & ~filters.COMMAND, track_cargo))
    app.add_handler(MessageHandler(filters.Regex('^🔎 Отследить груз$'), lambda u,c: u.message.reply_text("✍️ Напишите трек-номер:")))
    
    return app

if __name__ == '__main__':
    try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    except: pass
    if not TOKEN: logger.error("NO TOKEN")
    else:
        app = setup_application()
        app.run_polling()