import os
import logging
import requests
import psycopg2
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('ADMIN_BOT_TOKEN') 
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')
DATABASE_URL = os.getenv('DATABASE_URL')

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

# Состояния
ASK_NAME, ASK_PHONE, ASK_CITY, ASK_WAREHOUSE, ASK_CARGO, ASK_WEIGHT, ASK_VOLUME, ASK_CONFIRM_CALC, ASK_MANUAL_RATE, CONFIRM = range(10)

def clean_number(text):
    return text.replace(',', '.').strip()

# 🔥 СБРОС ВЕБХУКА
def force_delete_webhook(token):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=True")
    except: pass

# --- КАЛЬКУЛЯТОР (ВНУТРИ БОТА, ЧИТАЕТ CONFIG.JSON) ---
def get_t1_cost(weight, volume, category_name="общие", warehouse_code="GZ"):
    try:
        density = weight / volume if volume > 0 else 0
        
        # 1. Ищем тарифы для склада
        warehouse_rates = T1_RATES.get(warehouse_code, T1_RATES.get("GZ")) # Если нет FS/IW, берем GZ
        
        # 2. Ищем по категории, потом в "общих"
        rules = warehouse_rates.get(category_name, warehouse_rates.get("общие"))
        
        # 3. Ищем по плотности
        for rule in sorted(rules, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= rule.get('min_density', 0):
                price = rule.get('price', 0)
                unit = rule.get('unit', 'kg')
                cost_usd = price * volume if unit == 'm3' else price * weight
                return cost_usd, price, density # (Сумма, Тариф, Плотность)
        
        return 0, 0, density
    except Exception as e:
        logger.error(f"Ошибка T1: {e}"); return 0, 0, 0

def get_t2_cost(weight, zone):
    try:
        if zone == 'алматы': return 0 # T2 до Алматы не нужен
        
        rules = T2_RATES.get('large_parcel', {})
        weight_ranges = rules.get('weight_ranges', [])
        extra_rates = rules.get('extra_kg_rate', {})
        
        for r in weight_ranges:
            if weight <= r['max']:
                return float(r['zones'].get(zone, 0)) # Цена по шагу
        
        # Если вес больше (напр > 20кг)
        if weight_ranges:
            max_w = weight_ranges[-1]['max']
            base_cost = float(weight_ranges[-1]['zones'].get(zone, 0))
            extra_rate = float(extra_rates.get(zone, 300))
            return base_cost + ((weight - max_w) * extra_rate)
            
        return weight * 300
    except Exception as e:
        logger.error(f"Ошибка T2: {e}"); return 0

def calculate_all(weight, volume, product_type, city, warehouse_code="GZ"):
    # 1. T1 (Китай-Алматы) + 30%
    raw_t1_usd, raw_rate, density = get_t1_cost(weight, volume, product_type, warehouse_code)
    client_t1_usd = raw_t1_usd * 1.30
    client_rate = raw_rate * 1.30
    
    # 2. T2 (Алматы-Регион) + 20%
    zone = ZONES.get(city.lower(), "5") # find_zone
    client_t2_kzt = get_t2_cost(weight, zone) * 1.20
    
    # 3. Итог
    total_usd = client_t1_usd # В договор идет только T1
    total_kzt_estimate = (client_t1_usd * EXCHANGE_RATE) + client_t2_kzt

    return {
        "success": True, "density": round(density, 2),
        "tariff_rate": round(client_rate, 2), "t1_usd": round(client_t1_usd, 2),
        "t2_kzt": round(client_t2_kzt, 2), "total_usd": round(total_usd, 2),
        "total_kzt": round(total_kzt_estimate), "warehouse_code": warehouse_code
    }

# --- СОХРАНЕНИЕ В БАЗУ (POSTGRESQL) ---
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
            status, created_at, manager, warehouse_code, source
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, 'Manager')
        ON CONFLICT (contract_num) DO NOTHING;
        """
        
        track_temp = f"DOC-{data['contract_num']}" 

        cursor.execute(sql, (
            data['contract_num'], track_temp, data.get('client_name'), data.get('client_phone'),
            data.get('cargo_name'), float(data.get('weight', 0)), float(data.get('volume', 0)),
            data.get('city'), float(data.get('rate', 0)), float(data.get('clean_total', 0)),
            "Оформлен", "Manager_Bot", data.get('warehouse_code')
        ))
        conn.commit()
        logger.info(f"✅ Договор {data['contract_num']} сохранен в БД.")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка записи в БД: {e}")
        return False
    finally:
        if conn: conn.close()

# --- АНАЛИТИКА ---
def get_financial_report():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(CASE WHEN total_price_final > 0 THEN total_price_final ELSE agreed_rate * declared_weight END), SUM(actual_weight) FROM shipments")
        deals, revenue, kg = cur.fetchone()
        try:
            cur.execute("SELECT SUM(amount) FROM expenses")
            res = cur.fetchone(); expenses = res[0] if res and res[0] else 0
        except: expenses = 0
        cur.execute("SELECT COUNT(*) FROM applications")
        leads = cur.fetchone()[0]
        return {"deals": deals or 0, "revenue": round(revenue or 0, 2), "expenses": round(expenses, 2), "profit": round((revenue or 0) - expenses, 2), "kg": round(kg or 0, 2), "leads": leads or 0}
    except Exception: return None
    finally: 
        if conn: conn.close()

# --- ЛОГИКА БОТА ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📝 Создать Договор", callback_data='create_contract')],
                [InlineKeyboardButton("📊 ФИНАНСОВЫЙ ОТЧЕТ", callback_data='show_stats')]]
    await update.message.reply_text("🏭 **POST PRO ADMIN**\nЦентр управления.", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = get_financial_report()
    if not data: await query.edit_message_text("⚠️ Нет данных."); return
    text = (f"📊 **ОТЧЕТ**\n💵 ВЫРУЧКА: ${data['revenue']:,}\n💸 РАСХОДЫ: -${data['expenses']:,}\n🏆 **ПРИБЫЛЬ: ${data['profit']:,}**\n\n📦 Сделок: {data['deals']} | ⚖️ {data['kg']} кг")
    kb = [[InlineKeyboardButton("🔙 Меню", callback_data='back_to_menu')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update.message if update.message else update.callback_query.message, context)

# --- ПРОЦЕСС ДОГОВОРА ---
async def start_contract_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data.clear()
    await query.edit_message_text("📝 **Новый Договор**\n\n1️⃣ ФИО Клиента:")
    return ASK_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_name'] = update.message.text
    await update.message.reply_text("2️⃣ Телефон:")
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_phone'] = update.message.text
    await update.message.reply_text("3️⃣ Город назначения (в КЗ):")
    return ASK_CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_city'] = update.message.text
    # 🔥 ШАГ 4: ВЫБОР СКЛАДА
    keyboard = [[InlineKeyboardButton("🏭 Гуанчжоу (GZ)", callback_data='wh_GZ')],
                [InlineKeyboardButton("🛋 Фошань (FS)", callback_data='wh_FS')],
                [InlineKeyboardButton("🏗 Иу (IW)", callback_data='wh_IW')]]
    await update.message.reply_text("4️⃣ Склад отправки (в Китае):", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_WAREHOUSE

async def get_warehouse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['c_warehouse'] = query.data.replace("wh_", "") # GZ, FS или IW
    await query.edit_message_text(f"✅ Склад: {context.user_data['c_warehouse']}\n\n5️⃣ Груз (название):")
    return ASK_CARGO

async def get_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_cargo'] = update.message.text
    await update.message.reply_text("6️⃣ Вес (кг):")
    return ASK_WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['c_weight'] = float(clean_number(update.message.text))
        await update.message.reply_text("7️⃣ Объем (м³):")
        return ASK_VOLUME
    except: await update.message.reply_text("❌ Число!")

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        vol = float(clean_number(update.message.text))
        context.user_data['c_volume'] = vol
        
        # 🔥 АВТО-РАСЧЕТ (Читаем config.json)
        res = calculate_all(
            context.user_data['c_weight'], vol, 
            context.user_data['c_cargo'], 
            context.user_data['c_city'],
            context.user_data['c_warehouse'] # GZ, FS или IW
        )
        
        context.user_data['calc_rate'] = res['tariff_rate']
        context.user_data['calc_total'] = res['total_usd']
        context.user_data['c_density'] = res['density']
        
        msg = f"📊 **АВТО-РАСЧЕТ (Склад: {res['warehouse_code']}):**\n⚖️ Плотность: {res['density']}\nТариф: **${res['tariff_rate']}**\nИтого (Т1): **${res['total_usd']}**\n\nПрименить?"
        kb = [[InlineKeyboardButton(f"✅ Да", callback_data='use_auto'), InlineKeyboardButton("✏️ Нет", callback_data='use_manual')]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        return ASK_CONFIRM_CALC
    except: await update.message.reply_text("❌ Число!")

async def use_auto_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['final_rate'] = context.user_data['calc_rate']
    context.user_data['final_total'] = context.user_data['calc_total']
    await show_summary(query, context)
    return CONFIRM

async def use_manual_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("✏️ Введите ТАРИФ ($/кг):")
    return ASK_MANUAL_RATE

async def get_manual_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(clean_number(update.message.text))
        context.user_data['final_rate'] = rate
        context.user_data['final_total'] = round(rate * context.user_data['c_weight'], 2)
        if context.user_data['c_volume'] > 0:
             context.user_data['c_density'] = round(context.user_data['c_weight'] / context.user_data['c_volume'], 2)
        else: context.user_data['c_density'] = 0
        await show_summary(update.message, context)
        return CONFIRM
    except: await update.message.reply_text("❌ Число!")

async def show_summary(message_obj, context):
    text = (f"📑 **ИТОГ:**\n👤 {context.user_data['c_name']}\n📦 {context.user_data['c_cargo']}\n💰 ${context.user_data['final_total']} (Предв.)\n\nГенерируем?")
    kb = [[InlineKeyboardButton("✅ Да", callback_data='generate_yes'), InlineKeyboardButton("❌ Нет", callback_data='generate_no')]]
    if hasattr(message_obj, 'reply_text'): await message_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else: await message_obj.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def generate_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == 'generate_no':
        await query.edit_message_text("❌ Отмена."); return ConversationHandler.END
    
    await query.edit_message_text("⏳ **Печатаю...**")
    
    cn = f"CN-{datetime.now().strftime('%m%d%H%M%S')}"
    data = context.user_data
    
    payload = {
        "contract_num": cn,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "client_name": data.get('c_name'),
        "client_phone": data.get('c_phone'),
        "city": data.get('c_city'),
        "cargo_name": data.get('c_cargo'),
        "weight": data.get('c_weight'),
        "volume": data.get('c_volume'),
        "density": data.get('c_density', 0),
        "rate": str(data.get('final_rate')),
        "total_sum": f"{data.get('final_total')} (Предварительно)",
        "clean_total": data.get('final_total'),
        "additional_services": "По факту / Upon arrival",
        "manager_id": query.from_user.id,
        "warehouse_code": data.get('c_warehouse')
    }
    
    db_success = save_contract_to_db(payload)
    
    try:
        requests.post(MAKE_CONTRACT_WEBHOOK, json=payload)
        if db_success:
            await query.message.reply_text(f"✅ **Договор {cn} создан!**\n💾 Сохранен в базу.")
        else:
            await query.message.reply_text(f"⚠️ PDF отправлен, но **ОШИБКА БАЗЫ**.")
    except: pass
    return ConversationHandler.END

def main():
    force_delete_webhook(TOKEN)
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_contract_process, pattern='^create_contract$')],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT, get_name)],
            ASK_PHONE: [MessageHandler(filters.TEXT, get_phone)],
            ASK_CITY: [MessageHandler(filters.TEXT, get_city)],
            ASK_WAREHOUSE: [CallbackQueryHandler(get_warehouse_callback, pattern='^wh_')],
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
    
    print("Admin Bot Started...")
    app.run_polling()

if __name__ == '__main__':
    main()