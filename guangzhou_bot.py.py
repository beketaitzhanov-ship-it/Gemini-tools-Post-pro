import os
import logging
import random
import psycopg2
import requests
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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

WAREHOUSE_CONFIGS = {
    "GZ": {"name": "Гуанчжоу", "prefix": "GZ"},
    "FS": {"name": "Фошань", "prefix": "FS"},
    "IW": {"name": "Иу", "prefix": "IW"}
}

# Состояния
WAITING_ACTUAL_WEIGHT, WAITING_ACTUAL_VOLUME, WAITING_ADDITIONAL_COST, WAITING_MEDIA = range(4)
WAITING_STATUS_TRACK = 5

def clean_number(text):
    return text.replace(',', '.').strip()

# 🔥 СБРОС ВЕБХУКА
def force_delete_webhook(token):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=True")
    except: pass

# --- КАЛЬКУЛЯТОР (СКОПИРОВАН ИЗ ADMIN BOT, ЧИТАЕТ CONFIG.JSON) ---
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

def calculate_t1_only(weight, volume, product_type, warehouse_code="GZ"):
    # Только T1 с наценкой 30%
    raw_t1_usd, raw_rate, density = get_t1_cost(weight, volume, product_type, warehouse_code)
    client_t1_usd = raw_t1_usd * 1.30
    client_rate = raw_rate * 1.30
    return {"tariff_rate": round(client_rate, 2), "total_usd": round(client_t1_usd, 2)}

# --- БАЗА ДАННЫХ ---
def get_db_connection(self):
    try: return psycopg2.connect(DATABASE_URL)
    except Exception: return None

def notify_make(self, event_type, data):
    if not MAKE_WAREHOUSE_WEBHOOK: return
    # ... (код notify_make без изменений) ...
    payload = {
        "event": event_type, "track": data.get('track_number'), "contract_num": data.get('contract_num'),
        "fio": data.get('fio'), "phone": data.get('phone'), "weight": data.get('actual_weight'),
        "volume": data.get('actual_volume'), "final_price": data.get('final_price', 0),
        "additional_cost": data.get('additional_cost', 0), "status": data.get('status'),
        "manager": data.get('manager'), "file_id": data.get('file_id'), "media_type": data.get('media_type'),
        "timestamp": datetime.now().isoformat()
    }
    try: requests.post(MAKE_WAREHOUSE_WEBHOOK, json=payload, timeout=2)
    except: pass

# --- СЦЕНАРИЙ 1: ПРИЕМКА (КНОПКИ) ---
async def show_expected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection(None)
    if not conn: return
    cur = conn.cursor()
    
    # 🔥 ИСПРАВЛЕНИЕ: ILIKE (неважен регистр)
    cur.execute("SELECT contract_num, fio, product, declared_weight FROM shipments WHERE status ILIKE 'оформлен' ORDER BY created_at DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📋 **Список пуст.** Нет ожидаемых грузов.")
        return

    # 🔥 СОЗДАЕМ КНОПКИ (Удобный интерфейс)
    keyboard = []
    for row in rows:
        text = f"{row[0]} — {row[1]} ({row[2]})"
        # В callback_data мы передаем сам номер договора
        keyboard.append([InlineKeyboardButton(text, callback_data=f"accept_{row[0]}")])
    
    await update.message.reply_text("📋 **ОЖИДАЮТСЯ НА СКЛАДЕ:**\nНажми на груз, чтобы принять:", reply_markup=InlineKeyboardMarkup(keyboard))

async def start_contract_receive_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает при нажатии кнопки с CN-номером"""
    query = update.callback_query
    await query.answer()
    
    contract_num = query.data.replace("accept_", "")
    context.user_data['receiving_contract_num'] = contract_num
    
    conn = get_db_connection(None)
    if conn:
        cur = conn.cursor()
        # Достаем все нужные данные
        cur.execute("SELECT fio, agreed_rate, product, client_city, warehouse_code FROM shipments WHERE contract_num = %s", (contract_num,))
        row = cur.fetchone()
        conn.close()
        
        if row:
            context.user_data['agreed_rate'] = float(row[1]) if row[1] else 0
            context.user_data['cargo_type'] = row[2]
            context.user_data['cargo_city'] = row[3]
        
            # 🔥 ВАЖНО: Запоминаем код склада (GZ/FS/IW)
            context.user_data['warehouse_code'] = row[4] 
            context.user_data['receiving_fio'] = row[0]
            
            await query.edit_message_text(f"📥 Приемка **{contract_num}**\n👤 {row[0]}\n💲 Тариф: **{row[1]}**\n\n⚖️ **Введите ФАКТ. ВЕС (кг):**")
            return WAITING_ACTUAL_WEIGHT
        else:
            await query.edit_message_text("❌ Ошибка. Договор не найден.")
            return ConversationHandler.END

async def get_actual_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['fact_weight'] = float(update.message.text.replace(',', '.'))
        await update.message.reply_text("📏 **ФАКТ. ОБЪЕМ (м³):**")
        return WAITING_ACTUAL_VOLUME
    except: await update.message.reply_text("❌ Число!"); return WAITING_ACTUAL_WEIGHT

async def get_actual_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        vol = float(update.message.text.replace(',', '.'))
        context.user_data['fact_volume'] = vol
        
        # 🔥 АВТО-ПЕРЕСЧЕТ ТАРИФА (на основе факта)
        new_rate = context.user_data['agreed_rate']
        
        # Используем тот же калькулятор, что и Админ
        res = calculate_t1_only(
            context.user_data['fact_weight'], vol,
            context.user_data.get('cargo_type', 'общие'),
            context.user_data.get('warehouse_code', 'GZ')
        )
        new_rate = res['tariff_rate']
        
        if new_rate != context.user_data['agreed_rate']:
            await update.message.reply_text(f"⚠️ **Тариф изменился (плотность)!**\nБыл: {context.user_data['agreed_rate']} -> Стал: **{new_rate}**")
        
        context.user_data['final_rate'] = new_rate
        
        await update.message.reply_text("🛠 **Доп. услуги ($)?**\n(0 если нет):")
        return WAITING_ADDITIONAL_COST
    except: await update.message.reply_text("❌ Число!"); return WAITING_ACTUAL_VOLUME

async def get_additional_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['add_cost'] = float(update.message.text.replace(',', '.'))
        await update.message.reply_text("📸 **ФОТО/ВИДЕО?**\n(/skip если нет)")
        return WAITING_MEDIA
    except: await update.message.reply_text("❌ Число!"); return WAITING_ADDITIONAL_COST

async def save_contract_final_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, media_type = None, None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id; media_type = "photo"
    elif update.message.video:
        file_id = update.message.video.file_id; media_type = "video"
    
    data = context.user_data
    contract_num = data['receiving_contract_num']
    weight = data['fact_weight']
    volume = data['fact_volume']
    add_cost = data['add_cost']
    rate = data['final_rate']
    
    # 🔥 ГЕНЕРАЦИЯ GZ/FS/IW ТРЕКА
    wh_code = data.get('warehouse_code', 'GZ')
    prefix = WAREHOUSE_CONFIGS.get(wh_code, {}).get('prefix', 'GZ')
    gz_track = f"{prefix}{random.randint(100000, 999999)}"
    
    final_price = round((weight * rate) + add_cost, 2)
    
    conn = get_db_connection(None)
    if conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE shipments 
            SET status = 'принят на складе', 
                track_number = %s,           -- 🔥 ПРИСВАИВАЕМ GZ-ТРЕК!
                actual_weight = %s, actual_volume = %s, 
                additional_cost = %s, total_price_final = %s, 
                agreed_rate = %s, created_at = NOW() 
            WHERE contract_num = %s 
            RETURNING fio, phone
        """, (gz_track, weight, volume, add_cost, final_price, rate, contract_num))
        res = cur.fetchone()
        conn.commit()
        conn.close()
        
        notify_make(None, "received_final", {
            "contract_num": contract_num, "track_number": gz_track, "fio": res[0], "phone": res[1],
            "actual_weight": weight, "actual_volume": volume, "final_price": final_price,
            "additional_cost": add_cost, "file_id": file_id, "media_type": media_type, "status": "принят на складе"
        })
        await update.message.reply_text(f"✅ **ПРИНЯТО!**\nГрузу присвоен трек: `{gz_track}`\nИтог: ${final_price}")
    return ConversationHandler.END

# --- СЦЕНАРИЙ 3: СТАТУСЫ ---
async def set_status_mode(self, u, c): 
    c.user_data['smode'] = "sent" if "ОТПРАВЛЕНО" in u.message.text else "border" if "НА ГРАНИЦЕ" in u.message.text else "delivered"
    await u.message.reply_text(f"👇 Трек (GZ... или CN...):")
    return WAITING_STATUS_TRACK
    
async def update_status(self, u, c):
    t = u.message.text.strip().upper()
    if t.startswith("➕") or t.startswith("📋"): return ConversationHandler.END
    
    st = "в пути до границы" if c.user_data['smode'] == "sent" else "на границе" if c.user_data['smode'] == "border" else "доставлен"
    
    cn = get_db_connection(None)
    if cn:
        cr = cn.cursor()
        cr.execute("UPDATE shipments SET status=%s, route_progress=%s WHERE track_number=%s OR contract_num=%s", (st, 50 if st == "на границе" else 15, t, t))
        cn.commit(); cn.close()
        self.notify_make(c.user_data['smode'], {"track_number": t, "status": st})
        await u.message.reply_text(f"✅ {st}: {t}")
    return WAITING_STATUS_TRACK

async def cancel(self, u, c): await u.message.reply_text("Меню."); return ConversationHandler.END
async def start_command(self, u, c):
    # 🔥 УБРАЛИ "НОВЫЙ ГРУЗ" (чтобы не было путаницы с договорами)
    kb = [[KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ")], [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]]
    await u.message.reply_text("🏭 **СКЛАД POST PRO**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

def setup_handlers(self):
    self.application.add_handler(CommandHandler("start", self.start_command))
    self.application.add_handler(MessageHandler(filters.Regex('^(📋 ОЖИДАЕМЫЕ ГРУЗЫ)$'), self.show_expected))
    
    # 🔥 ГЛАВНОЕ: ПРИЕМКА ТЕПЕРЬ ТОЛЬКО ЧЕРЕЗ КНОПКИ
    self.application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(self.start_contract_receive_button, pattern='^accept_CN-')],
        states={
            WAITING_ACTUAL_WEIGHT: [MessageHandler(filters.TEXT, self.get_actual_weight)],
            WAITING_ACTUAL_VOLUME: [MessageHandler(filters.TEXT, self.get_actual_volume)],
            WAITING_ADDITIONAL_COST: [MessageHandler(filters.TEXT, self.get_additional_cost)],
            WAITING_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Regex('/skip'), self.save_contract_final_with_media)]
        },
        fallbacks=[CommandHandler('cancel', self.cancel)]
    ))
    
    # СТАТУСЫ
    self.application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(🚚|🛃|✅)'), self.set_status_mode)],
        states={WAITING_STATUS_TRACK: [MessageHandler(filters.TEXT, self.update_status)]},
        fallbacks=[CommandHandler('cancel', self.cancel)]
    ))

def run(self):
    self.application.run_polling()

if __name__ == '__main__':
    force_delete_webhook(TOKEN)
    bot = GuangzhouBot()
    bot.run()