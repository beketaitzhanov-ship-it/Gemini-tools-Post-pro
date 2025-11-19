import os
import logging
import random
import psycopg2
import requests
import json
import time
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK') # Обновление (Сценарий 2)
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')   # Создание (Сценарий 1)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    T1_RATES = CONFIG.get('T1_RATES_DENSITY', {})
except:
    T1_RATES = {}

# --- КАТЕГОРИИ ДЛЯ КНОПОК ---
CATEGORY_BUTTONS = {
    "odezhda": "👕 Одежда", "obuv": "👟 Обувь", "sumki": "👜 Сумки",
    "tovary_dlja_doma": "🏠 Хозтовары", "igrushki": "🧸 Игрушки", "mebel": "🛋 Мебель",
    "elektronika": "💻 Электроника", "telefony": "📱 Телефоны", "avtozapchasti": "🚗 Автозапчасти",
    "santehnika": "🚿 Сантехника", "oborudovanie": "⚙️ Оборудование", "strojmaterialy": "🧱 Строймат.",
    "tovary_dlja_zhivotnyh": "🐾 Зоотовары", "obshhie": "📦 Прочее"
}

# --- СОСТОЯНИЯ ---
(WAITING_ACTUAL_WEIGHT, WAITING_ACTUAL_VOLUME, WAITING_ADDITIONAL_COST, WAITING_MEDIA) = range(4)
WAITING_STATUS_TRACK = 5

# Для "Нового Груза"
(NEW_FIO, NEW_PROD, NEW_WEIGHT, NEW_VOLUME, NEW_COST, NEW_MEDIA) = range(6, 12)

# --- ФУНКЦИИ ---

def get_db_connection():
    try: return psycopg2.connect(DATABASE_URL)
    except: return None

def clean_number(text):
    if not text: return 0.0
    try: return float(text.replace(',', '.').strip())
    except: return 0.0

def notify_make_update(payload):
    if not MAKE_WAREHOUSE_WEBHOOK: return
    try: requests.post(MAKE_WAREHOUSE_WEBHOOK, json=payload, timeout=3)
    except: pass

def notify_make_create(payload):
    if not MAKE_CONTRACT_WEBHOOK: return
    try: requests.post(MAKE_CONTRACT_WEBHOOK, json=payload, timeout=5)
    except: pass

def calculate_t1_compare(weight, volume, product_type, warehouse_code, agreed_rate):
    """Сравнивает расчетный тариф с админским и берет MAX"""
    rates = T1_RATES.get(warehouse_code, T1_RATES.get('GZ', {}))
    cat_rates = rates.get(product_type, rates.get('obshhie'))
    
    density = weight / volume if volume > 0 else 0
    base_price = 0
    
    if cat_rates:
        for r in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= r.get('min_density', 0):
                base_price = r.get('price', 0); break
        if base_price == 0: base_price = cat_rates[-1].get('price', 0)
    
    calculated_rate = base_price * 1.30
    
    # Логика защиты: Если админ дал цену ВЫШЕ расчетной, оставляем её.
    # Если админ ошибся и дал слишком низкую, или тариф 0 -> берем расчетную.
    final_rate = max(calculated_rate, agreed_rate)
    return round(final_rate, 2)

# --- ГЛАВНОЕ МЕНЮ ---
async def start(u, c):
    kb = [
        [KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ"), KeyboardButton("📦 НОВЫЙ ГРУЗ")],
        [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]
    ]
    await u.message.reply_text("🏭 <b>СКЛАД POST PRO</b>\nВыберите действие:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Отмена.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

# --- СЦЕНАРИЙ 1: ПРИЕМКА ОЖИДАЕМОГО ---

async def show_expected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    # Ищем по статусу 'оформлен'
    cur.execute("SELECT contract_num, fio, product FROM shipments WHERE status ILIKE 'оформлен' ORDER BY created_at DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📋 Список пуст.")
        return

    keyboard = []
    for row in rows:
        text = f"{row[0]} | {row[1]} | {row[2]}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"accept_{row[0]}")])
    
    await update.message.reply_text("📋 <b>Нажми на груз для приемки:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def start_contract_receive_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cn = query.data.replace("accept_", "")
    context.user_data['cn'] = cn
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("SELECT fio, agreed_rate, product, warehouse_code FROM shipments WHERE contract_num = %s", (cn,))
        row = cur.fetchone()
        conn.close()
        if row:
            context.user_data.update({'fio': row[0], 'agreed_rate': float(row[1] or 0), 'prod': row[2], 'wh': row[3] or "GZ"})
            await query.edit_message_text(
                f"📥 <b>Приемка: {cn}</b>\n👤 {row[0]}\n📦 {row[2]}\n💰 Тариф (План): ${row[1]}\n\n⚖️ <b>ФАКТ ВЕС (кг):</b>", 
                parse_mode='HTML')
            return WAITING_ACTUAL_WEIGHT
    return ConversationHandler.END

async def get_actual_weight(u, c):
    c.user_data['fact_w'] = clean_number(u.message.text)
    await u.message.reply_text("📏 <b>ФАКТ ОБЪЕМ (м³):</b>", parse_mode='HTML')
    return WAITING_ACTUAL_VOLUME

async def get_actual_volume(u, c):
    v = clean_number(u.message.text) or (c.user_data['fact_w']/200)
    c.user_data['fact_v'] = v
    d = c.user_data
    
    # Сравниваем тарифы
    final_rate = calculate_t1_compare(d['fact_w'], v, d['prod'], d['wh'], d['agreed_rate'])
    c.user_data['final_rate'] = final_rate
    
    await u.message.reply_text(
        f"✅ Вес: {d['fact_w']} | V: {v:.3f}\n💰 Итог тариф: <b>${final_rate}</b>\n\n🛠 <b>Доп. расходы ($):</b>", 
        parse_mode='HTML')
    return WAITING_ADDITIONAL_COST

async def get_additional_cost(u, c):
    c.user_data['add_cost'] = clean_number(u.message.text)
    await u.message.reply_text("📸 <b>ФОТО/ВИДЕО?</b>\n(Отправь или /skip)", parse_mode='HTML')
    return WAITING_MEDIA

async def save_contract_final(u, c):
    media_link = "Без медиа"
    if u.message.photo:
        f = await c.bot.get_file(u.message.photo[-1].file_id)
        # Чистим ссылку от токена для безопасности (или используем как есть для Make)
        media_link = f.file_path
    elif u.message.video:
        f = await c.bot.get_file(u.message.video.file_id)
        media_link = f.file_path

    d = c.user_data
    prefix = d['wh']
    track = f"{prefix}{random.randint(100000, 999999)}"
    total_price = round((d['fact_w'] * d['final_rate']) + d['add_cost'], 2)
    status = f"Принят на складе {prefix}"
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE shipments 
            SET status=%s, track_number=%s, actual_weight=%s, actual_volume=%s, 
                additional_cost=%s, total_price_final=%s, agreed_rate=%s, media_link=%s
            WHERE contract_num=%s
        """, (status, track, d['fact_w'], d['fact_v'], d['add_cost'], total_price, d['final_rate'], media_link, d['cn']))
        conn.commit(); conn.close()
    
    notify_make_update({
        "action": "update", "contract_num": d['cn'], "actual_weight": d['fact_w'], 
        "actual_volume": d['fact_v'], "status": status, "media_link": media_link
    })
    
    await u.message.reply_text(f"✅ <b>ПРИНЯТО!</b>\n🆔 {track}\n💰 ${total_price}", parse_mode='HTML')
    return ConversationHandler.END


# --- СЦЕНАРИЙ 2: НОВЫЙ ГРУЗ (С КНОПКАМИ) ---

async def new_cargo_start(u, c):
    await u.message.reply_text("👤 <b>ФИО Клиента / Код:</b>", reply_markup=ReplyKeyboardRemove(), parse_mode='HTML')
    return NEW_FIO

async def new_cargo_fio(u, c): 
    c.user_data['new_fio'] = u.message.text
    # КНОПКИ КАТЕГОРИЙ
    keyboard = []
    row = []
    for key, name in CATEGORY_BUTTONS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"new_cat_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    
    await u.message.reply_text("📦 <b>ВЫБЕРИТЕ КАТЕГОРИЮ:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return NEW_PROD

async def new_cargo_prod_callback(u, c):
    query = u.callback_query
    await query.answer()
    cat_key = query.data.replace("new_cat_", "")
    c.user_data['new_prod'] = cat_key
    
    await query.edit_message_text(f"📦 Категория: {cat_key}\n⚖️ <b>Вес (кг):</b>", parse_mode='HTML')
    return NEW_WEIGHT

async def new_cargo_weight(u, c): c.user_data['new_w'] = clean_number(u.message.text); await u.message.reply_text("📦 <b>Объем (м³):</b>", parse_mode='HTML'); return NEW_VOLUME
async def new_cargo_vol(u, c): c.user_data['new_v'] = clean_number(u.message.text); await u.message.reply_text("🛠 <b>Доп. расходы ($):</b>", parse_mode='HTML'); return NEW_COST
async def new_cargo_cost(u, c): c.user_data['new_cost'] = clean_number(u.message.text); await u.message.reply_text("📸 <b>Фото:</b>", parse_mode='HTML'); return NEW_MEDIA

async def new_cargo_finish(u, c):
    media_link = "Без медиа"
    if u.message.photo:
        f = await c.bot.get_file(u.message.photo[-1].file_id)
        media_link = f.file_path

    d = c.user_data
    cn_num = f"CN-{int(time.time())}"
    track = f"GZ{random.randint(100000, 999999)}"
    
    # Расчет тарифа для нового груза (только по плотности)
    rate = calculate_t1_compare(d['new_w'], d['new_v'], d['new_prod'], "GZ", 0)
    total = round((d['new_w'] * rate) + d['new_cost'], 2)
    status = "Принят на складе GZ"
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shipments (
                contract_num, track_number, fio, product, status, warehouse_code, 
                actual_weight, actual_volume, additional_cost, total_price_final, 
                media_link, created_at, agreed_rate
            ) VALUES (%s, %s, %s, %s, %s, 'GZ', %s, %s, %s, %s, %s, NOW(), %s)
        """, (cn_num, track, d['new_fio'], d['new_prod'], status, d['new_w'], d['new_v'], d['new_cost'], total, media_link, rate))
        conn.commit(); conn.close()

    # Отправка в Make (как новый контракт)
    notify_make_create({
        "action": "create", "contract_num": cn_num, "fio": d['new_fio'], 
        "warehouse_code": "GZ", "product": d['new_prod'], 
        "declared_weight": d['new_w'], "declared_volume": d['new_v'], 
        "rate": rate, "created_at": str(datetime.now()),
        "actual_weight": d['new_w'], "status": status, "media_link": media_link
    })

    await u.message.reply_text(f"✅ <b>НОВЫЙ ГРУЗ СОЗДАН!</b>\n\n🆔 {cn_num}\n🆔 {track}\n💰 ${total}", parse_mode='HTML')
    return await start(u, c)


# --- СТАТУСЫ ---
async def set_status_mode(u, c): 
    c.user_data['smode'] = u.message.text
    await u.message.reply_text(f"👇 Режим: {u.message.text}\nВведите Трек:")
    return WAITING_STATUS_TRACK

async def update_status(u, c):
    t = u.message.text.strip().upper()
    st = "В пути"
    pr = 0
    mode = c.user_data.get('smode', '')
    if "ОТПРАВЛЕНО" in mode: st = "В пути (Китай)"; pr = 40
    elif "ГРАНИЦЕ" in mode: st = "На границе"; pr = 60
    elif "ДОСТАВЛЕНО" in mode: st = "Прибыл в Алматы"; pr = 100
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("UPDATE shipments SET status=%s, route_progress=%s WHERE track_number=%s OR contract_num=%s", (st, pr, t, t))
        conn.commit(); conn.close()
    await u.message.reply_text(f"✅ {st}: {t}")
    return WAITING_STATUS_TRACK

# --- SETUP ---
def setup_app():
    app = Application.builder().token(TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_contract_receive_button, pattern='^accept_')],
        states={
            WAITING_ACTUAL_WEIGHT: [MessageHandler(filters.TEXT, get_actual_weight)],
            WAITING_ACTUAL_VOLUME: [MessageHandler(filters.TEXT, get_actual_volume)],
            WAITING_ADDITIONAL_COST: [MessageHandler(filters.TEXT, get_additional_cost)],
            WAITING_MEDIA: [MessageHandler(filters.ALL, save_contract_final)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    new_cargo_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📦 НОВЫЙ ГРУЗ$'), new_cargo_start)],
        states={
            NEW_FIO: [MessageHandler(filters.TEXT, new_cargo_fio)],
            NEW_PROD: [CallbackQueryHandler(new_cargo_prod_callback, pattern='^new_cat_')], # Кнопки!
            NEW_WEIGHT: [MessageHandler(filters.TEXT, new_cargo_weight)],
            NEW_VOLUME: [MessageHandler(filters.TEXT, new_cargo_vol)],
            NEW_COST: [MessageHandler(filters.TEXT, new_cargo_cost)],
            NEW_MEDIA: [MessageHandler(filters.ALL, new_cargo_finish)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    stat_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(🚚|🛃|✅)'), set_status_mode)],
        states={WAITING_STATUS_TRACK: [MessageHandler(filters.TEXT, update_status)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex('^📋'), show_expected))
    app.add_handler(conv)
    app.add_handler(new_cargo_conv)
    app.add_handler(stat_conv)
    
    return app

if __name__ == '__main__':
    if not TOKEN: logger.error("NO TOKEN")
    else:
        app = setup_app()
        app.run_polling()