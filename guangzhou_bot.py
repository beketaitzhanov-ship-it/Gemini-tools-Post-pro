import os
import logging
import random
import psycopg2
import requests
import json
import time
from datetime import datetime
# FIX: Добавлен ReplyKeyboardRemove в импорты
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK') 
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')   

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    T1_RATES = CONFIG.get('T1_RATES_DENSITY', {})
except:
    T1_RATES = {}

WAREHOUSE_NAMES = {"GZ": "Гуанчжоу", "FS": "Фошань", "IW": "Иу"}

# --- КАТЕГОРИИ ---
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
(NEW_FIO, NEW_WH, NEW_PROD, NEW_WEIGHT, NEW_VOLUME, NEW_COST, NEW_MEDIA) = range(6, 13)

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

def calculate_t1_full(weight, volume, category_key, warehouse_code, agreed_rate_min=0):
    rates = T1_RATES.get(warehouse_code, T1_RATES.get('GZ', {}))
    cat_rates = rates.get(category_key, rates.get('obshhie'))
    
    density = weight / volume if volume > 0 else 9999.0
    base_price = 0
    
    if cat_rates:
        for r in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= r.get('min_density', 0):
                base_price = r.get('price', 0); break
        if base_price == 0: base_price = cat_rates[-1].get('price', 0)
    
    calculated_rate = base_price * 1.30
    final_rate_unit = max(calculated_rate, agreed_rate_min)
    
    is_cbm = final_rate_unit > 50 
    cost = (final_rate_unit * volume) if is_cbm else (final_rate_unit * weight)
    
    return round(cost, 2), round(final_rate_unit, 2), round(density, 0), is_cbm

# --- СБРОС БАЗЫ ДАННЫХ ---
async def reset_database(u, c):
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM shipments") # Полная очистка таблицы
        conn.commit()
        conn.close()
        await u.message.reply_text("🗑 <b>ВСЕ ДАННЫЕ УДАЛЕНЫ!</b>\nБаза бота полностью очищена.", parse_mode='HTML')
    else:
        await u.message.reply_text("Ошибка подключения к БД.")

# --- ГЛАВНОЕ МЕНЮ ---
async def start(u, c):
    kb = [
        [KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ"), KeyboardButton("📦 НОВЫЙ ГРУЗ")],
        [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]
    ]
    await u.message.reply_text(
        "🏭 <b>СКЛАД POST PRO</b>\n"
        "Система управления приемкой и статусами.", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML'
    )
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Отмена.", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

# --- СЦЕНАРИЙ 1: ПРИЕМКА ОЖИДАЕМОГО ---

async def show_expected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT contract_num, fio, product FROM shipments WHERE status ILIKE 'оформлен' ORDER BY created_at DESC LIMIT 15")
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📋 Список пуст. Нет оформленных контрактов.")
        return

    keyboard = []
    for row in rows:
        text = f"{row[1]} | {row[2]}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"accept_{row[0]}")])
    
    await update.message.reply_text("📋 <b>Выберите груз для приемки:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

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
            wh_code = row[3] if row[3] else "GZ"
            context.user_data.update({'fio': row[0], 'agreed_rate': float(row[1] or 0), 'prod': row[2], 'wh': wh_code})
            wh_name = WAREHOUSE_NAMES.get(wh_code, wh_code)
            await query.edit_message_text(f"📥 <b>Приемка: {cn}</b>\n🏭 Склад плана: <b>{wh_name}</b>\n👤 {row[0]}\n📦 {row[2]}\n\n⚖️ <b>Введите ФАКТИЧЕСКИЙ ВЕС (кг):</b>", parse_mode='HTML')
            return WAITING_ACTUAL_WEIGHT
    
    await query.edit_message_text("❌ Ошибка: Контракт не найден.")
    return ConversationHandler.END

async def get_actual_weight(u, c):
    c.user_data['fact_w'] = clean_number(u.message.text)
    await u.message.reply_text("📏 <b>Введите ФАКТИЧЕСКИЙ ОБЪЕМ (м³):</b>\n(Например: 0.5 или 60*40*50)", parse_mode='HTML')
    return WAITING_ACTUAL_VOLUME

async def get_actual_volume(u, c):
    text = u.message.text
    if '*' in text or 'х' in text or 'x' in text:
        try:
            dims = text.replace('х', 'x').replace('*', 'x').split('x')
            v = (float(dims[0]) * float(dims[1]) * float(dims[2])) / 1000000
        except: v = 0.0
    else:
        v = clean_number(text)
        
    if v <= 0: v = c.user_data['fact_w'] / 200
    c.user_data['fact_v'] = v
    d = c.user_data
    cost, final_rate, dens, is_cbm = calculate_t1_full(d['fact_w'], v, d['prod'], d['wh'], d['agreed_rate'])
    c.user_data['final_calc'] = {'cost': cost, 'rate': final_rate, 'is_cbm': is_cbm}
    
    await u.message.reply_text(f"✅ Вес: {d['fact_w']} кг | V: {v:.3f} м³\n💰 База: ${cost}\n\n🛠 <b>Нужны доп. услуги (упаковка/обрешетка)?</b>\n👉 Если да — введите сумму ($)\n👉 Если нет — напишите 0", parse_mode='HTML')
    return WAITING_ADDITIONAL_COST

async def get_additional_cost(u, c):
    c.user_data['add_cost'] = clean_number(u.message.text)
    await u.message.reply_text("📸 <b>Сделайте ФОТО груза:</b>\n(Или нажмите /skip)", parse_mode='HTML')
    return WAITING_MEDIA

async def save_contract_final(u, c):
    media_link = "Без медиа"
    if u.message.photo:
        f = await c.bot.get_file(u.message.photo[-1].file_id)
        media_link = f.file_path
    elif u.message.video:
        f = await c.bot.get_file(u.message.video.file_id)
        media_link = f.file_path

    d = c.user_data
    calc = d['final_calc']
    prefix = d['wh']
    track = f"{prefix}{random.randint(100000, 999999)}"
    total_price = round(calc['cost'] + d['add_cost'], 2)
    status = f"Принят на складе {prefix}"
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE shipments 
            SET status=%s, track_number=%s, actual_weight=%s, actual_volume=%s, 
                additional_cost=%s, total_price_final=%s, agreed_rate=%s, media_link=%s
            WHERE contract_num=%s
        """, (status, track, d['fact_w'], d['fact_v'], d['add_cost'], total_price, calc['rate'], media_link, d['cn']))
        conn.commit(); conn.close()
    
    notify_make_update({"action": "update", "contract_num": d['cn'], "track": track, "actual_weight": d['fact_w'], "actual_volume": d['fact_v'], "total_price": total_price, "status": status, "media_link": media_link})
    
    await u.message.reply_text(f"✅ <b>ГРУЗ ПРИНЯТ!</b>\n🆔 Трек: <code>{track}</code>\n💰 Итого: <b>${total_price}</b>", parse_mode='HTML', reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ"), KeyboardButton("📦 НОВЫЙ ГРУЗ")], [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]], resize_keyboard=True))
    return ConversationHandler.END


# --- СЦЕНАРИЙ 2: НОВЫЙ ГРУЗ ---

async def new_cargo_start(u, c):
    await u.message.reply_text("👤 <b>Введите Имя Клиента (или Код):</b>", reply_markup=ReplyKeyboardRemove(), parse_mode='HTML')
    return NEW_FIO

async def new_cargo_fio(u, c): 
    c.user_data['new_fio'] = u.message.text
    kb = [[KeyboardButton("GZ (Гуанчжоу)"), KeyboardButton("IW (Иу)"), KeyboardButton("FS (Фошань)")]]
    await u.message.reply_text("🏭 <b>Выберите Склад приема:</b>", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True), parse_mode='HTML')
    return NEW_WH

async def new_cargo_wh(u, c):
    text = u.message.text
    if "IW" in text: code = "IW"
    elif "FS" in text: code = "FS"
    else: code = "GZ"
    c.user_data['new_wh'] = code
    
    keyboard = []
    row = []
    for key, name in CATEGORY_BUTTONS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"new_cat_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    
    await u.message.reply_text("📦 <b>Выберите категорию товара:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return NEW_PROD

async def new_cargo_prod_callback(u, c):
    query = u.callback_query
    await query.answer()
    cat_key = query.data.replace("new_cat_", "")
    c.user_data['new_prod'] = cat_key
    await query.edit_message_text(f"📦 Категория: {cat_key}\n⚖️ <b>Введите Вес (кг):</b>", parse_mode='HTML')
    return NEW_WEIGHT

async def new_cargo_weight(u, c): 
    c.user_data['new_w'] = clean_number(u.message.text)
    await u.message.reply_text("📦 <b>Введите Объем (м³):</b>", parse_mode='HTML')
    return NEW_VOLUME

async def new_cargo_vol(u, c): 
    c.user_data['new_v'] = clean_number(u.message.text)
    await u.message.reply_text("🛠 <b>Нужны доп. услуги (упаковка/обрешетка)?</b>\n👉 Если да — введите сумму ($)\n👉 Если нет — напишите 0", parse_mode='HTML')
    return NEW_COST

async def new_cargo_cost(u, c): 
    c.user_data['new_cost'] = clean_number(u.message.text)
    await u.message.reply_text("📸 <b>Фото (или /skip):</b>", parse_mode='HTML')
    return NEW_MEDIA

async def new_cargo_finish(u, c):
    media_link = "Без медиа"
    if u.message.photo:
        f = await c.bot.get_file(u.message.photo[-1].file_id)
        media_link = f.file_path

    d = c.user_data
    cn_num = f"CN-{int(time.time())}"
    track = f"{d['new_wh']}{random.randint(100000, 999999)}"
    cost, rate, dens, is_cbm = calculate_t1_full(d['new_w'], d['new_v'], d['new_prod'], d['new_wh'], 0)
    total = round(cost + d['new_cost'], 2)
    status = f"Принят на складе {d['new_wh']}"
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shipments (
                contract_num, track_number, fio, product, status, warehouse_code, 
                actual_weight, actual_volume, additional_cost, total_price_final, 
                media_link, created_at, agreed_rate
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        """, (cn_num, track, d['new_fio'], d['new_prod'], status, d['new_wh'], d['new_w'], d['new_v'], d['new_cost'], total, media_link, rate))
        conn.commit(); conn.close()

    notify_make_create({
        "action": "create", "contract_num": cn_num, "fio": d['new_fio'], 
        "warehouse_code": d['new_wh'], "product": d['new_prod'], 
        "declared_weight": d['new_w'], "declared_volume": d['new_v'], 
        "rate": rate, "created_at": str(datetime.now()),
        "actual_weight": d['new_w'], "status": status, "media_link": media_link, "track": track
    })

    await u.message.reply_text(f"✅ <b>НОВЫЙ ГРУЗ СОЗДАН!</b>\n\n🆔 Контракт: {cn_num}\n🆔 Трек: <b>{track}</b>\n💰 Итого: <b>${total}</b>\n📍 Склад: {d['new_wh']}", parse_mode='HTML', reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ"), KeyboardButton("📦 НОВЫЙ ГРУЗ")], [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]], resize_keyboard=True))
    return ConversationHandler.END

# --- СТАТУСЫ ---
async def set_status_mode(u, c): 
    c.user_data['smode'] = u.message.text
    await u.message.reply_text(f"👇 Режим: {u.message.text}\nВведите Трек номер (или несколько через пробел):")
    return WAITING_STATUS_TRACK

async def update_status(u, c):
    raw_text = u.message.text.strip().upper()
    tracks = [t.strip() for t in raw_text.replace(',', ' ').split()]
    mode = c.user_data.get('smode', '')
    
    if "ОТПРАВЛЕНО" in mode: st = "В пути (Китай)"; pr = 40 
    elif "ГРАНИЦЕ" in mode: st = "На границе (Хоргос)"; pr = 70
    elif "ДОСТАВЛЕНО" in mode: st = "Прибыл в Алматы"; pr = 100
    else: st = "В пути"; pr = 20
        
    conn = get_db_connection()
    updated_count = 0
    if conn:
        cur = conn.cursor()
        for t in tracks:
            cur.execute("UPDATE shipments SET status=%s, route_progress=%s WHERE track_number=%s OR contract_num=%s", (st, pr, t, t))
            if cur.rowcount > 0: updated_count += 1
        conn.commit(); conn.close()
        
    await u.message.reply_text(f"✅ Обновлено грузов: {updated_count}\nСтатус: {st}")
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
            NEW_WH: [MessageHandler(filters.TEXT, new_cargo_wh)],
            NEW_PROD: [CallbackQueryHandler(new_cargo_prod_callback, pattern='^new_cat_')],
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
    app.add_handler(CommandHandler("reset_db", reset_database)) # ДОБАВЛЕНА КОМАНДА СБРОСА
    app.add_handler(MessageHandler(filters.Regex('^📋'), show_expected))
    app.add_handler(conv)
    app.add_handler(new_cargo_conv)
    app.add_handler(stat_conv)
    
    return app

if __name__ == '__main__':
    try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    except: pass
    if not TOKEN: logger.error("NO TOKEN")
    else:
        app = setup_app()
        app.run_polling()