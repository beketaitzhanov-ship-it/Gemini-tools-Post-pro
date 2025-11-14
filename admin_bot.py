import os
import logging
import requests
import psycopg2
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# --- НАСТРОЙКИ (ВСЕ КЛЮЧИ ВНУТРИ) ---

# 1. Твой Токен Телеграм
TOKEN = "8564264238:AAHERL8IJgD2pVv-TbrCsV0lhWAynsNRMaI"

# 2. Твоя ссылка на Make (Contract Hook)
MAKE_CONTRACT_WEBHOOK = "https://hook.eu1.make.com/j8wj8r7v3oll7jhyeigh4rdsk8snnc19"

# 3. Твоя База Данных (Render)
DATABASE_URL = "postgresql://postpro_user:3WMTk2ZhwyiCNnggAFzHACVUQgJKMERU@dpg-d3t8e83ipnbc738h30sg-a.frankfurt-postgres.render.com/postpro_db"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния диалога
ASK_NAME, ASK_PHONE, ASK_CITY, ASK_CARGO, ASK_WEIGHT, ASK_VOLUME, ASK_DENSITY, ASK_RATE, ASK_TOTAL_SUM, ASK_ADDITIONAL, CONFIRM = range(11)

# Функция очистки текста
def clean_number(text):
    return text.replace(',', '.').strip()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def save_contract_to_db(data):
    """Сохраняет данные договора в PostgreSQL"""
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        # Мы создаем запись в таблице shipments
        # track_number используем как номер договора пока (или генерируем новый)
        # Статус ставим 'Оформлен'
        
        sql = """
        INSERT INTO shipments (
            contract_num, track_number, fio, phone, 
            product, declared_weight, declared_volume, 
            client_city, agreed_rate, total_price_final, 
            status, created_at, manager
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (track_number) DO UPDATE SET
            fio = EXCLUDED.fio,
            contract_num = EXCLUDED.contract_num;
        """
        
        # Генерируем временный трек (или берем из договора)
        # В будущем можно сделать генератор треков GZ...
        track_temp = f"DOC-{data['contract_num']}" 

        cursor.execute(sql, (
            data['contract_num'],   # contract_num
            track_temp,             # track_number (пока используем номер договора как ID)
            data['client_name'],    # fio
            data['client_phone'],   # phone
            data['cargo_name'],     # product
            float(data['weight']),  # declared_weight
            float(data['volume']),  # declared_volume
            data['city'],           # client_city
            float(data['rate']),    # agreed_rate
            float(data['total_sum']), # total_price_final
            "Оформлен",             # status
            "Manager_Bot"           # manager
        ))

        conn.commit()
        print(f"✅ Договор {data['contract_num']} сохранен в БД!")
        return True
    except Exception as e:
        print(f"❌ Ошибка записи в БД: {e}")
        return False
    finally:
        if conn: conn.close()

# --- СТАРТ БОТА ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📝 Создать Договор (PDF)", callback_data='create_contract')]]
    await update.message.reply_text(
        "🏭 **POST PRO ADMIN**\nПанель менеджера.\nБаза данных подключена 🟢\nНажми кнопку, чтобы оформить сделку:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_contract_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 **Оформление Договора**\n\n1️⃣ ФИО Клиента (как в паспорте):")
    return ASK_NAME

# 1. Имя -> Телефон
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_name'] = update.message.text
    await update.message.reply_text("2️⃣ Номер телефона клиента:")
    return ASK_PHONE

# 2. Телефон -> Город
async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_phone'] = update.message.text
    keyboard = [
        [InlineKeyboardButton("🏭 Гуанчжоу", callback_data='city_Гуанчжоу')],
        [InlineKeyboardButton("🏗 Иу", callback_data='city_Иу')],
        [InlineKeyboardButton("🛋 Фошань", callback_data='city_Фошань')]
    ]
    await update.message.reply_text("3️⃣ Выберите город отправки:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_CITY

# 3. Город -> Груз
async def get_city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city = query.data.replace("city_", "")
    context.user_data['c_city'] = city
    await query.edit_message_text(f"✅ Город: **{city}**\n\n4️⃣ Наименование груза (например: Мебель):")
    return ASK_CARGO

# 4. Груз -> Вес
async def get_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_cargo'] = update.message.text
    await update.message.reply_text("5️⃣ ЗАЯВЛЕННЫЙ Вес груза (кг):")
    return ASK_WEIGHT

# 5. Вес -> Объем
async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_weight'] = clean_number(update.message.text)
    await update.message.reply_text("6️⃣ ЗАЯВЛЕННЫЙ Объем груза (м³):")
    return ASK_VOLUME

# 6. Объем -> Плотность
async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_volume'] = clean_number(update.message.text)
    await update.message.reply_text("7️⃣ Плотность (кг/м³):")
    return ASK_DENSITY

# 7. Плотность -> Тариф
async def get_density(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_density'] = clean_number(update.message.text)
    await update.message.reply_text("8️⃣ Тариф ($ за кг/куб):")
    return ASK_RATE

# 8. Тариф -> Сумма
async def get_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_rate'] = clean_number(update.message.text)
    await update.message.reply_text("9️⃣ **ИТОГОВАЯ СУММА ($)?**\nНапиши финальную цифру:")
    return ASK_TOTAL_SUM

# 9. Сумма -> Доп услуги
async def get_total_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_total'] = clean_number(update.message.text)
    await update.message.reply_text("🔟 **Доп. услуги?**\nНапиши: 'Включено', 'Нет' или сумму.")
    return ASK_ADDITIONAL

# 10. Проверка
async def get_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['c_additional'] = update.message.text
    
    summary = (
        "📑 **ПРОВЕРЬТЕ ДАННЫЕ:**\n\n"
        f"👤 {context.user_data['c_name']}\n"
        f"📞 {context.user_data['c_phone']}\n"
        f"🏙 {context.user_data['c_city']}\n"
        f"📦 {context.user_data['c_cargo']}\n"
        f"⚖️ {context.user_data['c_weight']} кг / {context.user_data['c_volume']} м³\n"
        f"💰 **ИТОГО: {context.user_data['c_total']} $**\n\n"
        "Генерируем?"
    )
    keyboard = [
        [InlineKeyboardButton("✅ Создать PDF + Сохранить в БД", callback_data='generate_yes')],
        [InlineKeyboardButton("❌ Отмена", callback_data='generate_no')]
    ]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM

# 11. Генерация и Сохранение
async def generate_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'generate_no':
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    
    await query.edit_message_text("⏳ **Сохраняю в Базу и печатаю договор...**")
    
    # Данные
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
        "density": context.user_data['c_density'],
        "rate": str(context.user_data['c_rate']),
        "additional_services": context.user_data['c_additional'],
        "total_sum": str(context.user_data['c_total']),
        "manager_id": query.from_user.id
    }
    
    # 1. Сохраняем в БД
    db_success = save_contract_to_db(payload)
    
    # 2. Отправляем в Make
    try:
        requests.post(MAKE_CONTRACT_WEBHOOK, json=payload)
        if db_success:
            await query.message.reply_text(f"✅ **Договор {contract_num} сохранен в Базе!**\n📄 PDF скоро придет.")
        else:
            await query.message.reply_text(f"⚠️ PDF отправлен, но **ошибка сохранения в Базу**.")
            
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка Make: {e}")

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
            ASK_DENSITY: [MessageHandler(filters.TEXT, get_density)],
            ASK_RATE: [MessageHandler(filters.TEXT, get_rate)],
            ASK_TOTAL_SUM: [MessageHandler(filters.TEXT, get_total_sum)],
            ASK_ADDITIONAL: [MessageHandler(filters.TEXT, get_additional)],
            CONFIRM: [CallbackQueryHandler(generate_contract)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(handler)
    print("Post Pro Admin Bot запущен (DB + Make)...")
    app.run_polling()

if __name__ == '__main__':
    main()
