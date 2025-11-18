import os
import logging
import requests
import json
import psycopg2
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ (ENVIRONMENT) ---
load_dotenv()
# Убедись, что эти ключи есть в Render!
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 
MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK')

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

# --- СОСТОЯНИЯ ДИАЛОГА ---
NAME, CITY, PRODUCT, WEIGHT, VOLUME, PHONE = range(6)

# --- 1. ФУНКЦИИ КАРТЫ И ОТСЛЕЖИВАНИЯ (НОВЫЙ ВИЗУАЛ) ---

def generate_vertical_map(status, progress, warehouse_code="GZ", city_to="Алматы"):
    """Рисует вертикальный маршрут фуры по городам Китая"""
    
    # Определяем стартовый город
    start_city = "Гуанчжоу"
    if warehouse_code == "IW": start_city = "Иу"
    elif warehouse_code == "FS": start_city = "Фошань"

    # Маршрут (Ключевые точки)
    route = [
        start_city,      # 0
        "Чанша",         # 1
        "Сиань",         # 2
        "Ланьчжоу",      # 3
        "Урумчи",        # 4
        "Хоргос (Граница)", # 5
        city_to          # 6
    ]
    
    # Определяем позицию фуры (0..6) на основе прогресса (0..100%)
    # 0-15: Старт, 15-30: Чанша, 30-50: Сиань, 50-70: Ланьчжоу, 70-90: Урумчи, 90-99: Хоргос, 100: Финиш
    pos = 0
    if progress >= 100: pos = 6
    elif progress >= 90: pos = 5 # Граница
    elif progress >= 70: pos = 4 # Урумчи
    elif progress >= 50: pos = 3 # Ланьчжоу
    elif progress >= 30: pos = 2 # Сиань
    elif progress >= 15: pos = 1 # Чанша
    else: pos = 0 # Старт
    
    map_lines = []
    for i, city in enumerate(route):
        if i < pos:
            # Город пройден
            map_lines.append(f"✅ {city}")
            map_lines.append("      ⬇️")
        elif i == pos:
            # Фура здесь (Текущая точка)
            map_lines.append(f"🚚 <b>{city.upper()}</b> 📍")
            if i != len(route) - 1: # Если не финиш, рисуем стрелку вниз
                map_lines.append("      ⬇️")
        else:
            # Город впереди
            map_lines.append(f"⬜️ {city}")
            if i != len(route) - 1:
                map_lines.append("      ⬇️")
                
    return "\n".join(map_lines)

async def track_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_number = update.message.text.strip().upper()
    
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    # Ищем груз
    cur.execute("SELECT status, actual_weight, product, warehouse_code, client_city, route_progress FROM shipments WHERE track_number = %s OR contract_num = %s", (track_number, track_number))
    row = cur.fetchone()
    conn.close()

    if row:
        status, weight, product, wh_code, city, progress_db = row
        if not wh_code: wh_code = "GZ"
        if not city: city = "Алматы"
        
        # Если прогресса нет в базе, придумываем по статусу (для старых записей)
        progress = progress_db if progress_db is not None else 0
        if not progress_db:
            st = status.lower()
            if "принят" in st: progress = 10
            elif "пути" in st: progress = 40
            elif "границе" in st: progress = 90
            elif "алматы" in st or "прибыл" in st: progress = 100

        # Генерируем карту
        visual_map = generate_vertical_map(status, progress, wh_code, city)
        
        # Ответ в стиле Айсулу
        await update.message.reply_text(
            f"📦 <b>Груз найден!</b>\n\n"
            f"🆔 Трек: <code>{track_number}</code>\n"
            f"⚖️ Вес: {weight} кг\n"
            f"📄 Товар: {product}\n\n"
            f"📍 <b>ТЕКУЩИЙ СТАТУС: {status}</b>\n\n"
            f"{visual_map}\n\n"
            f"👩‍💼 <i>Если у вас есть вопросы по местоположению, я всегда на связи!</i>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "😔 К сожалению, я не нашла груз с таким номером.\n"
            "Пожалуйста, проверьте, правильно ли вы ввели трек-номер (например: GZ123456)."
        )

# --- 2. ФУНКЦИИ КАЛЬКУЛЯТОРА И AI ---

def clean_number(text):
    if not text: return 0.0
    try: return float(text.replace(',', '.').strip())
    except: return 0.0

def get_product_category_from_ai(product_text: str) -> str:
    """Спрашиваем у Gemini категорию"""
    if not MAKE_CATEGORIZER_WEBHOOK: return "obshhie"
    try:
        response = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': product_text}, timeout=15)
        response.raise_for_status()
        key = response.json().get('category_key')
        return key.lower() if key else "obshhie"
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "obshhie"

def calculate_total_cost(weight, volume, category_key, city_name, warehouse="GZ"):
    # Т1
    rates = CONFIG.get('T1_RATES_DENSITY', {}).get(warehouse, {})
    cat_rates = rates.get(category_key, rates.get('obshhie'))
    density = weight / volume if volume > 0 else 0
    price = 0
    if cat_rates:
        for r in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= r.get('min_density', 0):
                price = r.get('price', 0); break
        if price == 0: price = cat_rates[-1].get('price', 0)
    
    t1 = (price * 1.30 * volume) if (price * 1.30) > 50 else (price * 1.30 * weight)
    
    # Т2
    zone = "5"
    if CONFIG and 'DESTINATION_ZONES' in CONFIG:
        for k, v in CONFIG['DESTINATION_ZONES'].items():
            if k in city_name.lower(): zone = v; break
    
    t2_rate = {"1": 0.4, "2": 0.5, "3": 0.6, "4": 0.7, "5": 0.8}.get(str(zone), 0.8)
    t2 = 0 if zone == "алматы" else (weight * t2_rate)
    
    return round(t1, 2), round(t2, 2), round(t1+t2, 2)

# --- 3. ДИАЛОГ С АЙСУЛУ (ЛИЧНОСТЬ) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🚚 Рассчитать стоимость")], [KeyboardButton("🔎 Отследить груз")]]
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я — Айсулу, ваш персональный менеджер в Post Pro.</b>\n\n"
        "Я помогу вам рассчитать выгодную доставку из Китая и отследить ваш груз.\n"
        "Чем могу быть полезна сегодня?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Очень приятно! 😊\nПодскажите, в какой город планируете доставку?")
    return CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['city'] = update.message.text
    await update.message.reply_text("Поняла. 📦 Что именно вы хотите привезти?\n(Напишите название товара, например: 'женская одежда' или 'автозапчасти')")
    return PRODUCT

async def get_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['product_text'] = text
    
    msg = await update.message.reply_text("⏳ Минутку, я уточняю категорию вашего товара у специалиста...")
    
    key = get_product_category_from_ai(text)
    context.user_data['category_key'] = key
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=f"Готово! ✅\nКатегория определена: <b>{key}</b>.\n\n⚖️ Напишите, пожалуйста, примерный вес груза (в кг):",
        parse_mode='HTML'
    )
    return WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = clean_number(update.message.text)
    if w <= 0:
        await update.message.reply_text("Ой, кажется, это не число. Пожалуйста, введите вес цифрами (например: 10.5) 🙏")
        return WEIGHT
    context.user_data['weight'] = w
    await update.message.reply_text("Отлично! А теперь объем в кубах (м³)?\n(Если не знаете точно, напишите примерные размеры, или просто '0.1', я посчитаю приблизительно)")
    return VOLUME

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = clean_number(update.message.text)
    if v <= 0: v = context.user_data['weight'] / 200
    context.user_data['volume'] = v
    
    await update.message.reply_text(
        "Спасибо! Всё записала. 📝\n"
        "Оставьте, пожалуйста, ваш номер телефона. Я отправлю вам расчет и, если нужно, свяжусь для уточнения деталей.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Отправить мой номер", request_contact=True)]], one_time_keyboard=True, resize_keyboard=True)
    )
    return PHONE

async def get_phone_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    d = context.user_data
    
    t1, t2, total = calculate_total_cost(d['weight'], d['volume'], d['category_key'], d['city'])
    
    # Отчет Админу
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔥 <b>НОВАЯ ЗАЯВКА (Айсулу)</b>\n👤 {d['name']} {phone}\n🏙 {d['city']}\n📦 {d['product_text']} ({d['category_key']})\n💰 ${total}",
                parse_mode='HTML'
            )
        except: pass
    
    # Ответ Клиенту (Стиль Айсулу)
    await update.message.reply_text(
        f"🎉 <b>{d['name']}, ваш расчет готов!</b>\n\n"
        f"🇨🇳 Доставка Китай-Алматы: <b>~${t1}</b>\n"
        f"🇰🇿 Доставка в {d['city']}: <b>~${t2}</b>\n"
        f"🏁 <b>Итоговая сумма: ~${total}</b>\n\n"
        f"Я уже передала вашу заявку нашим логистам. Мы свяжемся с вами в ближайшее время! 🤝\n"
        f"Если хотите отследить другой груз, просто нажмите кнопку в меню.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Хорошо, отменяю. Если понадоблюсь - пишите! /start", reply_markup=ReplyKeyboardRemove()); return ConversationHandler.END

def setup_application():
    app = Application.builder().token(TOKEN).build()
    
    # Калькулятор
    calc = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🚚 Рассчитать стоимость$'), get_name)],
        states={
            NAME: [MessageHandler(filters.TEXT, get_name)],
            CITY: [MessageHandler(filters.TEXT, get_city)],
            PRODUCT: [MessageHandler(filters.TEXT, get_product)],
            WEIGHT: [MessageHandler(filters.TEXT, get_weight)],
            VOLUME: [MessageHandler(filters.TEXT, get_volume)],
            PHONE: [MessageHandler(filters.CONTACT | filters.TEXT, get_phone_and_finish)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(calc)
    # Отслеживание по кнопке и по тексту трека
    app.add_handler(MessageHandler(filters.Regex('^🔎 Отследить груз$'), lambda u,c: u.message.reply_text("✍️ Пожалуйста, напишите трек-номер вашего груза (например: GZ12345):")))
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z0-9-]{5,}$') & ~filters.COMMAND, track_cargo))
    
    return app

if __name__ == '__main__':
    # Очистка вебхука перед запуском
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    except: pass

    if not TOKEN: logger.error("Нет токена!")
    else:
        app = setup_application()
        logger.info("Айсулу запущена...")
        app.run_polling()