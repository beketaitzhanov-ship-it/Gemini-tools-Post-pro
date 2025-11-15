import os
import logging
import requests
import psycopg2
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from dotenv import load_dotenv

# Подключаем калькулятор
try:
    from calculator import LogisticsCalculator
except ImportError:
    LogisticsCalculator = None

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('ADMIN_BOT_TOKEN') 
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')
DATABASE_URL = os.getenv('DATABASE_URL')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния
ASK_NAME, ASK_PHONE, ASK_CITY, ASK_CARGO, ASK_WEIGHT, ASK_VOLUME, ASK_CONFIRM_CALC, ASK_MANUAL_RATE, CONFIRM = range(9)

def clean_number(text):
    return text.replace(',', '.').strip()

# 🔥 ФУНКЦИЯ: ПРИНУДИТЕЛЬНОЕ УДАЛЕНИЕ ВЕБХУКА (ЧТОБЫ НЕ БЫЛО КОНФЛИКТОВ)
def force_delete_webhook(token):
    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=True"
        requests.get(url)
        logger.info("♻️ Вебхук успешно сброшен. Переходим в режим Polling.")
    except Exception as e:
        logger.error(f"⚠️ Не удалось сбросить вебхук: {e}")

# --- СОХРАНЕНИЕ В БАЗУ ---
def save_contract_to_db(data):
    if not DATABASE_URL: return False
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        sql = """
        INSERT INTO shipments (
            contract_num, track_number, fio, phone, 
            product, declared_weight, declared_volume, 
            client_city, agreed_rate, total_price_final, 
            status, created_at, manager
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (track_number) DO UPDATE SET fio = EXCLUDED.fio;
        """
        track_temp = f"DOC-{data['contract_num']}" 
        cursor.execute(sql, (
            data['contract_num'], track_temp, data['client_name'], data['client_phone'],
            data['cargo_name'], float(data['weight']), float(data['volume']),
            data['city'], float(data['rate']), float(data['clean_total']),
            "Оформлен", "Manager_Bot"
        ))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")
        return False
    finally:
        if conn: conn.close()

# --- АНАЛИТИКА ---
def get_financial_report():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Доходы
        cur.execute("""
            SELECT COUNT(*), SUM(CASE WHEN total_price_final > 0 THEN total_price_final ELSE agreed_rate * declared_weight END), SUM(actual_weight)
            FROM shipments
        """)
        deals, revenue, kg = cur.fetchone()
        # Расходы
        cur.execute("SELECT SUM(amount) FROM expenses")
        res = cur.fetchone()
        expenses = res[0] if res and res[0] else 0
        # Лиды
        cur.execute("SELECT COUNT(*) FROM applications")
        leads = cur.fetchone()[0]

        return {"deals": deals or 0, "revenue": round(revenue or 0, 2), "expenses": round(expenses, 2), "profit": round((revenue or 0) - expenses, 2), "kg": round(kg or 0, 2), "leads": leads or 0}
    except Exception: return None
    finally: 
        if conn: conn.close()

# --- ЛОГИКА ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📝 Создать Договор", callback_data='create_contract')],
                [InlineKeyboardButton("📊 ФИНАНСОВЫЙ ОТЧЕТ", callback_data='show_stats')]]
    await update.message.reply_text("🏭 **POST PRO ADMIN**\nЦентр управления.", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = get_financial_report()
    if not data:
        await query.edit_message_text("⚠️ Нет данных.")
        return
    text = (f"📊 **ОТЧЕТ**\n💵 ВЫРУЧКА: ${data['revenue']:,}\n💸 РАСХОДЫ: -${data['expenses']:,}\n🏆 **ПРИБЫЛЬ: ${data['profit']:,}**\n\n📦 Сделок: {data['deals']} | ⚖️ {data['kg']} кг")
    kb = [[InlineKeyboardButton("🔙 Меню", callback_data='back_to_menu')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update.message if update.message else update.callback_query.message, context)

# --- ПРОЦЕСС ДОГОВОРА ---
async def start_contract_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 **Новый Договор**\n\n1️⃣ ФИО Клиента:")
    return ASK_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_name'] = update.message.text
    await update.message.reply_text("2️⃣ Телефон:")
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_phone'] = update.message.text
    keyboard = [[InlineKeyboardButton("🏭 Гуанчжоу", callback_data='city_Гуанчжоу'), InlineKeyboardButton("🏗 Иу", callback_data='city_Иу'), InlineKeyboardButton("🛋 Фошань", callback_data='city_Фошань')]]
    await update.message.reply_text("3️⃣ Город отправки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_CITY

async def get_city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['c_city'] = query.data.replace("city_", "")
    await query.edit_message_text(f"✅ Город: {context.user_data['c_city']}\n\n4️⃣ Груз:")
    return ASK_CARGO

async def get_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_cargo'] = update.message.text
    await update.message.reply_text("5️⃣ Вес (кг):")
    return ASK_WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['c_weight'] = float(clean_number(update.message.text))
        await update.message.reply_text("6️⃣ Объем (м³):")
        return ASK_VOLUME
    except: await update.message.reply_text("❌ Число!")

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['c_volume'] = float(clean_number(update.message.text))
        if LogisticsCalculator:
            calc = LogisticsCalculator()
            res = calc.calculate_all(context.user_data['c_weight'], context.user_data['c_volume'], context.user_data['c_cargo'], "Алматы")
            context.user_data['calc_rate'] = res['tariff_rate']
            context.user_data['calc_total'] = res['total_usd']
            msg = f"📊 **РАСЧЕТ:**\nТариф: **${res['tariff_rate']}**\nИтого: **${res['total_usd']}**\n\nПрименить?"
            kb = [[InlineKeyboardButton(f"✅ Да", callback_data='use_auto'), InlineKeyboardButton("✏️ Нет", callback_data='use_manual')]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb))
            return ASK_CONFIRM_CALC
        else:
            await update.message.reply_text("7️⃣ Тариф ($):")
            return ASK_MANUAL_RATE
    except: await update.message.reply_text("❌ Число!")

async def use_auto_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['final_rate'] = context.user_data['calc_rate']
    context.user_data['final_total'] = context.user_data['calc_total']
    await show_summary(query, context)
    return CONFIRM

async def use_manual_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✏️ Тариф ($):")
    return ASK_MANUAL_RATE

async def get_manual_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(clean_number(update.message.text))
        context.user_data['final_rate'] = rate
        context.user_data['final_total'] = round(rate * context.user_data['c_weight'], 2)
        await show_summary(update.message, context)
        return CONFIRM
    except: await update.message.reply_text("❌ Число!")

async def show_summary(message_obj, context):
    text = (f"📑 **ИТОГ:**\n👤 {context.user_data['c_name']}\n📦 {context.user_data['c_cargo']}\n💰 ${context.user_data['final_total']} (Предв.)\n\nГенерируем?")
    kb = [[InlineKeyboardButton("✅ Да", callback_data='generate_yes'), InlineKeyboardButton("❌ Нет", callback_data='generate_no')]]
    if hasattr(message_obj, 'reply_text'): await message_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else: await message_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def generate_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'generate_no':
        await query.edit_message_text("❌ Отмена.")
        return ConversationHandler.END
    
    await query.edit_message_text("⏳ **Печатаю...**")
    cn = f"CN-{datetime.now().strftime('%m%d%H')}"
    
    payload = {
        "contract_num": cn,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "client_name": context.user_data['c_name'],
        "client_phone": context.user_data['c_phone'],
        "city": context.user_data['c_city'],
        "cargo_name": context.user_data['c_cargo'],
        "weight": context.user_data['c_weight'],
        "volume": context.user_data['c_volume'],
        "density": 0,
        "rate": str(context.user_data['final_rate']),
        "total_sum": f"{context.user_data['final_total']} (Предварительно)",
        "clean_total": context.user_data['final_total'],
        "additional_services": "По факту",
        "manager_id": query.from_user.id
    }
    
    save_contract_to_db(payload)
    try:
        requests.post(MAKE_CONTRACT_WEBHOOK, json=payload)
        await query.message.reply_text(f"✅ **Договор {cn} создан!**")
    except: pass
    return ConversationHandler.END

def main():
    # 🔥 СНАЧАЛА СБРАСЫВАЕМ ВЕБХУК!
    force_delete_webhook(TOKEN)

    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_contract_process, pattern='^create_contract$')],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT, get_name)],
            ASK_PHONE: [MessageHandler(filters.TEXT, get_phone)],
            ASK_CITY: [CallbackQueryHandler(get_city_callback, pattern='^city_')],
            ASK_CARGO: [MessageHandler(filters.TEXT, get_cargo)],
            ASK_WEIGHT: [MessageHandler(filters.TEXT, get_weight)],
            ASK_VOLUME: [MessageHandler(filters.TEXT, get_volume)],
            ASK_CONFIRM_CALC: [CallbackQueryHandler(use_auto_calc, pattern='^use_auto$'), CallbackQueryHandler(use_manual_calc, pattern='^use_manual$')],
            ASK_MANUAL_RATE: [MessageHandler(filters.TEXT, get_manual_rate)],
            CONFIRM: [CallbackQueryHandler(generate_contract)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_stats_callback, pattern='^show_stats$'))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(conv)
    
    logger.info("🚀 Admin Bot запущен (Webhook killed).")
    app.run_polling()

if __name__ == '__main__':
    main()