import os
import logging
import requests
import psycopg2
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from dotenv import load_dotenv

# 👇 ПОДКЛЮЧАЕМ НАШ КАЛЬКУЛЯТОР
from calculator import LogisticsCalculator

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

def save_contract_to_db(data):
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
            data['city'], float(data['rate']), float(data['total_sum']),
            "Оформлен", "Manager_Bot"
        ))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
        return False
    finally:
        if conn: conn.close()

# --- ЛОГИКА БОТА ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📝 Создать Договор", callback_data='create_contract')]]
    await update.message.reply_text("🏭 **POST PRO ADMIN**\nПанель менеджера.\nКалькулятор подключен 🟢", reply_markup=InlineKeyboardMarkup(keyboard))

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
    keyboard = [
        [InlineKeyboardButton("🏭 Гуанчжоу", callback_data='city_Гуанчжоу')],
        [InlineKeyboardButton("🏗 Иу", callback_data='city_Иу')],
        [InlineKeyboardButton("🛋 Фошань", callback_data='city_Фошань')]
    ]
    await update.message.reply_text("3️⃣ Город отправки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_CITY

async def get_city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['c_city'] = query.data.replace("city_", "")
    await query.edit_message_text(f"✅ Город отправки: {context.user_data['c_city']}\n\n4️⃣ Груз (название):")
    return ASK_CARGO

async def get_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_cargo'] = update.message.text
    await update.message.reply_text("5️⃣ Заявленный ВЕС (кг):")
    return ASK_WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = clean_number(update.message.text)
        context.user_data['c_weight'] = float(val)
        await update.message.reply_text("6️⃣ Заявленный ОБЪЕМ (м³):")
        return ASK_VOLUME
    except ValueError:
        await update.message.reply_text("❌ Число!")
        return ASK_WEIGHT

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = clean_number(update.message.text)
        context.user_data['c_volume'] = float(val)
        
        # 🤖 АВТО-РАСЧЕТ ЧЕРЕЗ КАЛЬКУЛЯТОР
        await update.message.reply_text("🧮 **Считаю тариф через базу...**")
        
        calc = LogisticsCalculator()
        # Используем 'Алматы' как город назначения по умолчанию для договора, 
        # либо можно добавить шаг выбора города назначения.
        result = calc.calculate_all(
            weight=context.user_data['c_weight'],
            volume=context.user_data['c_volume'],
            product_type=context.user_data['c_cargo'], # Пробуем найти по названию
            city="Алматы" 
        )
        
        # Сохраняем расчетные данные
        context.user_data['calc_rate'] = result['tariff_rate'] # Тариф с наценкой
        context.user_data['calc_total'] = result['total_usd']  # Итог в USD
        
        msg = (
            f"📊 **РЕКОМЕНДАЦИЯ СИСТЕМЫ:**\n"
            f"Плотность: {result['density']}\n"
            f"Тариф (с наценкой): **${result['tariff_rate']}**\n"
            f"Итого (предварительно): **${result['total_usd']}**\n\n"
            "Использовать этот расчет?"
        )
        
        keyboard = [
            [InlineKeyboardButton(f"✅ Да (${result['tariff_rate']})", callback_data='use_auto')],
            [InlineKeyboardButton("✏️ Нет, ввести вручную", callback_data='use_manual')]
        ]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_CONFIRM_CALC
        
    except ValueError:
        await update.message.reply_text("❌ Число!")
        return ASK_VOLUME

# Ветка: Принять расчет системы
async def use_auto_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data['final_rate'] = context.user_data['calc_rate']
    context.user_data['final_total'] = context.user_data['calc_total']
    
    await show_final_summary(query, context)
    return CONFIRM

# Ветка: Ввести вручную
async def use_manual_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✏️ Введите ваш ТАРИФ ($ за кг):")
    return ASK_MANUAL_RATE

async def get_manual_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(clean_number(update.message.text))
        weight = context.user_data['c_weight']
        total = round(weight * rate, 2)
        
        context.user_data['final_rate'] = rate
        context.user_data['final_total'] = total
        
        # Передаем update.message как query для совместимости функций отображения
        await show_final_summary(update.message, context) 
        return CONFIRM
    except ValueError:
        await update.message.reply_text("❌ Число!")
        return ASK_MANUAL_RATE

# Финальная проверка перед генерацией
async def show_final_summary(message_object, context):
    # message_object может быть query или message, в зависимости от ветки
    
    summary = (
        "📑 **ИТОГОВЫЕ ДАННЫЕ:**\n\n"
        f"👤 {context.user_data['c_name']}\n"
        f"📦 {context.user_data['c_cargo']}\n"
        f"⚖️ {context.user_data['c_weight']} кг\n"
        f"💲 Тариф: {context.user_data['final_rate']} $\n"
        f"💰 **ИТОГО: {context.user_data['final_total']} $** (Предв.)\n\n"
        "Генерируем?"
    )
    keyboard = [[InlineKeyboardButton("✅ Генерировать", callback_data='generate_yes')], [InlineKeyboardButton("❌ Отмена", callback_data='generate_no')]]
    
    if hasattr(message_object, 'reply_text'): # Если это сообщение
        await message_object.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    else: # Если это query
        await message_object.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))

async def generate_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'generate_no':
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    
    await query.edit_message_text("⏳ **Печатаю...**")
    
    contract_num = f"CN-{datetime.now().strftime('%m%d%H')}"
    payload = {
        "contract_num": contract_num,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "client_name": context.user_data['c_name'],
        "client_phone": context.user_data['c_phone'],
        "city": context.user_data['c_city'],
        "cargo_name": context.user_data['c_cargo'],
        "weight": context.user_data['c_weight'],
        "volume": context.user_data['c_volume'],
        "density": round(context.user_data['c_weight']/context.user_data['c_volume'], 2),
        "rate": str(context.user_data['final_rate']),
        "total_sum": f"{context.user_data['final_total']} (Предварительно)",
        "additional_services": "По факту / Upon arrival",
        
        "clean_total": context.user_data['final_total'], # Для базы
        "manager_id": query.from_user.id
    }
    
    save_contract_to_db(payload)
    try:
        requests.post(MAKE_CONTRACT_WEBHOOK, json=payload)
        await query.message.reply_text(f"✅ Готово! Номер: {contract_num}")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Ошибка Make: {e}")

    return ConversationHandler.END

def main():
    app = Application.builder().token(TOKEN).build()
    handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_contract_process, pattern='^create_contract$')],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT, get_name)],
            ASK_PHONE: [MessageHandler(filters.TEXT, get_phone)],
            ASK_CITY: [CallbackQueryHandler(get_city_callback, pattern='^city_')],
            ASK_CARGO: [MessageHandler(filters.TEXT, get_cargo)],
            ASK_WEIGHT: [MessageHandler(filters.TEXT, get_weight)],
            ASK_VOLUME: [MessageHandler(filters.TEXT, get_volume)],
            
            # 👇 Новые шаги
            ASK_CONFIRM_CALC: [
                CallbackQueryHandler(use_auto_calc, pattern='^use_auto$'),
                CallbackQueryHandler(use_manual_calc, pattern='^use_manual$')
            ],
            ASK_MANUAL_RATE: [MessageHandler(filters.TEXT, get_manual_rate)],
            
            CONFIRM: [CallbackQueryHandler(generate_contract)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(handler)
    app.run_polling()

if __name__ == '__main__':
    main()