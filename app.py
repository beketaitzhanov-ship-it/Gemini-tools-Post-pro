import os
import logging
import requests
import json
import re
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()

# Используем твои названия переменных из Render
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')

# Новые переменные, которые нужно добавить
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 
MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK')

# Настройка логгера
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗАГРУЗКА КОНФИГУРАЦИИ (ТАРИФЫ) ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    logger.info("Config loaded successfully.")
except Exception as e:
    logger.error(f"!!! КРИТИЧЕСКАЯ ОШИБКА: Не могу загрузить config.json: {e}")
    CONFIG = {}

# --- СОСТОЯНИЯ ДИАЛОГА ---
NAME, CITY, PRODUCT, WEIGHT, VOLUME, PHONE = range(6)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_number(text):
    """Превращает текст '10,5' в число 10.5"""
    if not text: return 0.0
    try:
        return float(text.replace(',', '.').strip())
    except ValueError:
        return 0.0

def get_product_category_from_ai(product_text: str) -> str:
    """Запрашивает категорию у Make.com (Gemini)"""
    if not MAKE_CATEGORIZER_WEBHOOK:
        logger.warning("Webhook для Gemini не настроен. Используем 'obshhie'.")
        return "obshhie"
    
    try:
        response = requests.post(
            MAKE_CATEGORIZER_WEBHOOK,
            json={'product_text': product_text},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        key = data.get('category_key')
        return key.lower() if key else "obshhie"
    except Exception as e:
        logger.error(f"Ошибка Gemini Categorizer: {e}")
        return "obshhie"

def calculate_t1_cost(weight, volume, category_key, warehouse="GZ"):
    """Считает Т1 (Китай -> Алматы)"""
    try:
        rates = CONFIG.get('T1_RATES_DENSITY', {}).get(warehouse, {})
        # Если категории нет, ищем obshhie
        category_rates = rates.get(category_key, rates.get('obshhie'))
        
        density = weight / volume if volume > 0 else 0
        
        selected_price = 0
        # Ищем подходящую плотность
        if category_rates:
            for rule in sorted(category_rates, key=lambda x: x.get('min_density', 0), reverse=True):
                if density >= rule.get('min_density', 0):
                    selected_price = rule.get('price', 0)
                    break
            
            # Если не нашли (редкий случай), берем самую низкую плотность
            if selected_price == 0:
                 selected_price = category_rates[-1].get('price', 0)

        # Добавляем наценку 30% (как в других ботах)
        client_price = selected_price * 1.30

        # Проверка на кубы (если цена > 100, скорее всего это за куб)
        if client_price > 50: 
            cost = client_price * volume
        else:
            cost = client_price * weight
            
        return round(cost, 2), round(client_price, 2), round(density, 2)
    except Exception as e:
        logger.error(f"Ошибка расчёта Т1: {e}")
        return 0, 0, 0

def calculate_t2_cost(weight, city_name):
    """Считает Т2 (Алматы -> Город Клиента)"""
    try:
        city_key = city_name.lower().strip()
        # Упрощенный поиск зоны (можно доработать через Gemini позже)
        zone = "5" # По умолчанию самая дальняя
        
        if CONFIG and 'DESTINATION_ZONES' in CONFIG:
             for key, val in CONFIG['DESTINATION_ZONES'].items():
                 if key in city_key:
                     zone = val
                     break
        
        if zone == "алматы":
            return 0 # Бесплатно / Самовывоз
        
        # Простые ставки по зонам (примерные, можно уточнить)
        # Зона 1: 0.3$, Зона 5: 0.8$ и т.д.
        zone_rates = {"1": 0.4, "2": 0.5, "3": 0.6, "4": 0.7, "5": 0.8}
        rate = zone_rates.get(str(zone), 0.8)
        
        return round(weight * rate, 2)
    except Exception as e:
        logger.error(f"Ошибка расчёта Т2: {e}")
        return 0

# --- ОБРАБОТЧИКИ (HANDLERS) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я Айсулу, менеджер Post Pro.\n"
        "Я помогу рассчитать стоимость доставки и оформить заявку.\n\n"
        "Как к вам обращаться?"
    )
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Приятно познакомиться! 🏙 Из какого вы города?")
    return CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['city'] = update.message.text
    await update.message.reply_text("📦 Что планируете везти? (Напишите название товара, например: 'кроссовки' или 'запчасти')")
    return PRODUCT

async def get_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_text = update.message.text
    context.user_data['product_text'] = product_text
    
    msg = await update.message.reply_text("⏳ Секунду, советуюсь с экспертом по категории...")
    
    # --- ВЫЗОВ GEMINI ЧЕРЕЗ MAKE ---
    category_key = get_product_category_from_ai(product_text)
    context.user_data['category_key'] = category_key
    
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=f"Поняла! Категория товара: <b>{category_key}</b>\n\n⚖️ Введите примерный ВЕС груза (в кг):",
        parse_mode='HTML'
    )
    return WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    weight = clean_number(update.message.text)
    if weight <= 0:
        await update.message.reply_text("❌ Пожалуйста, введите число (например: 50.5)")
        return WEIGHT
    context.user_data['weight'] = weight
    await update.message.reply_text("📦 Введите примерный ОБЪЕМ груза (в м³):\n(Если не знаете, просто напишите '0.1')")
    return VOLUME

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    vol = clean_number(text)
    
    if vol <= 0:
        vol = context.user_data['weight'] / 200 # Авто-расчет плотности 200
        await update.message.reply_text(f"⚠️ Приму примерный объем: {vol:.2f} м³.")
    
    context.user_data['volume'] = vol
    
    await update.message.reply_text("📱 Оставьте ваш номер телефона для связи:", reply_markup=ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Отправить контакт", request_contact=True)]], one_time_keyboard=True, resize_keyboard=True
    ))
    return PHONE

async def get_phone_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text
    
    context.user_data['phone'] = phone
    
    # --- РАСЧЕТ ---
    data = context.user_data
    
    # 1. Т1 (Китай -> Алматы)
    t1_cost, t1_rate, density = calculate_t1_cost(data['weight'], data['volume'], data['category_key'])
    
    # 2. Т2 (Алматы -> Город)
    t2_cost = calculate_t2_cost(data['weight'], data['city'])
    
    total_cost = t1_cost + t2_cost
    
    # --- СООБЩЕНИЕ АДМИНУ ---
    admin_message = (
        f"🔥 <b>НОВАЯ ЗАЯВКА (Aisulu Bot)</b>\n"
        f"👤 <b>Клиент:</b> {data['name']}\n"
        f"📱 <b>Телефон:</b> {phone}\n"
        f"🏙 <b>Город:</b> {data['city']}\n"
        f"📦 <b>Товар:</b> {data['product_text']} (Кат: {data['category_key']})\n"
        f"⚖️ <b>Вес:</b> {data['weight']} кг | <b>Объем:</b> {data['volume']} м³\n"
        f"📊 <b>Плотность:</b> {density}\n\n"
        f"💰 <b>Предварительный расчет:</b>\n"
        f"🇨🇳 Т1 (Китай-Алматы): ${t1_cost} (Тариф: ${t1_rate})\n"
        f"🇰🇿 Т2 (По РК): ${t2_cost}\n"
        f"💵 <b>ИТОГО: ${total_cost:.2f}</b>"
    )
    
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить админу: {e}")
    
    # --- ОТВЕТ КЛИЕНТУ ---
    await update.message.reply_text(
        f"✅ Спасибо, {data['name']}! Ваша заявка принята.\n\n"
        f"📊 <b>Предварительный расчет:</b>\n"
        f"📦 Категория: {data['category_key']}\n"
        f"🇨🇳 Доставка до Алматы: ~${t1_cost}\n"
        f"🇰🇿 Доставка до {data['city']}: ~${t2_cost}\n"
        f"💰 <b>Итого: ~${total_cost:.2f}</b>\n\n"
        f"👨‍💻 Менеджер скоро свяжется с вами!",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='HTML'
    )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Диалог отменен.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def setup_application():
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
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
    
    application.add_handler(conv_handler)
    return application

if __name__ == '__main__':
    # Удаляем вебхук перед поллингом (чтобы не было конфликтов)
    try:
        temp_app = Application.builder().token(TOKEN).build()
        # В новых версиях python-telegram-bot удаление вебхука делается через run_polling автоматически,
        # но для надежности можно дернуть API напрямую, если есть requests, или просто запустить:
    except:
        pass

    if not TOKEN:
        logger.error("Токен бота не найден! Проверь TELEGRAM_BOT_TOKEN в Render.")
    else:
        app = setup_application()
        logger.info("Айсулу (Менеджер) запущена...")
        app.run_polling()