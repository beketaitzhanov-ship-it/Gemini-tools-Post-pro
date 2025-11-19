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

# Импортируем общие модули
from shared_calculations import universal_t1_calculation
from category_helper import get_product_category_from_ai

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Загружаем конфиг
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
except Exception as e:
    logger.error(f"❌ Ошибка загрузки config.json: {e}")
    CONFIG = {}

WAREHOUSE_CONFIGS = {
    "GZ": {"name": "Гуанчжоу", "prefix": "GZ"},
    "FS": {"name": "Фошань", "prefix": "FS"},
    "IW": {"name": "Иу", "prefix": "IW"}
}

# Состояния
WAITING_ACTUAL_WEIGHT, WAITING_ACTUAL_VOLUME, WAITING_ADDITIONAL_COST, WAITING_MEDIA = range(4)
WAITING_STATUS_TRACK = 5

def clean_number(text):
    """Очистка числа"""
    try:
        return float(text.replace(',', '.').strip())
    except:
        return 0.0

# 🔥 СБРОС ВЕБХУКА
def force_delete_webhook(token):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=True")
        logger.info("✅ Вебхук сброшен")
    except Exception as e:
        logger.error(f"❌ Ошибка сброса вебхука: {e}")

def get_db_connection():
    """Подключение к БД"""
    try: 
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        return None

def notify_make(payload):
    """Отправка данных в Make.com"""
    if not MAKE_WAREHOUSE_WEBHOOK: 
        logger.warning("⚠️ MAKE_WAREHOUSE_WEBHOOK не задан, отправка пропущена.")
        return
    
    try: 
        response = requests.post(MAKE_WAREHOUSE_WEBHOOK, json=payload, timeout=5)
        response.raise_for_status()
        logger.info(f"✅ Данные отправлены в Make: {payload.get('contract_num')}")
    except Exception as e: 
        logger.error(f"❌ Ошибка отправки в Make: {e}")

def calculate_t1_only(weight, volume, product_type, warehouse_code="GZ"):
    """Обертка для универсального расчета T1"""
    result = universal_t1_calculation(weight, volume, product_type, warehouse_code)
    return {"tariff_rate": result['rate'], "total_usd": result['cost_usd']}

# --- СЦЕНАРИЙ 1: ПРИЕМКА ГРУЗОВ ---

async def show_expected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать ожидаемые грузы"""
    conn = get_db_connection()
    if not conn: 
        await update.message.reply_text("❌ Ошибка подключения к базе")
        return
    
    cur = conn.cursor()
    try:
        # Поиск по статусу 'оформлен' (как записывает Admin Bot)
        cur.execute("""
            SELECT contract_num, fio, product, declared_weight, warehouse_code 
            FROM shipments 
            WHERE status ILIKE 'оформлен' OR status IS NULL 
            ORDER BY created_at DESC LIMIT 10
        """)
        
        rows = cur.fetchall()
        
        if not rows:
            await update.message.reply_text("📋 **Список пуст.** Нет ожидаемых грузов.")
            return

        keyboard = []
        for row in rows:
            contract_num, fio, product, weight, wh_code = row
            wh_name = WAREHOUSE_CONFIGS.get(wh_code, {}).get('name', 'Гуанчжоу')
            text = f"{contract_num} — {fio} ({product[:20]}...) {weight}кг [{wh_name}]"
            keyboard.append([InlineKeyboardButton(text, callback_data=f"accept_{contract_num}")])
        
        await update.message.reply_text(
            "📋 **ОЖИДАЮТСЯ НА СКЛАДЕ:**\nНажми на груз, чтобы принять:", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения ожидаемых грузов: {e}")
        await update.message.reply_text("❌ Ошибка загрузки списка грузов")
    finally:
        conn.close()

async def start_contract_receive_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало приема груза по кнопке"""
    query = update.callback_query
    await query.answer()
    
    contract_num = query.data.replace("accept_", "")
    context.user_data['receiving_contract_num'] = contract_num
    
    conn = get_db_connection()
    if not conn:
        await query.edit_message_text("❌ Ошибка подключения к базе")
        return ConversationHandler.END
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fio, agreed_rate, product, client_city, warehouse_code 
            FROM shipments WHERE contract_num = %s
        """, (contract_num,))
        row = cur.fetchone()
        
        if row:
            fio, agreed_rate, product, city, warehouse_code = row
            
            # 🔥 КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Определяем категорию заново
            category_key = get_product_category_from_ai(product)
            
            context.user_data['agreed_rate'] = float(agreed_rate) if agreed_rate else 0
            context.user_data['cargo_type'] = category_key  # Сохраняем английский ключ
            context.user_data['cargo_city'] = city
            context.user_data['warehouse_code'] = warehouse_code 
            context.user_data['receiving_fio'] = fio
            context.user_data['original_product_name'] = product
            
            wh_name = WAREHOUSE_CONFIGS.get(warehouse_code, {}).get('name', 'Гуанчжоу')
            
            await query.edit_message_text(
                f"📥 **ПРИЕМКА {contract_num}**\n"
                f"👤 {fio}\n"
                f"🏭 Склад: {wh_name}\n"
                f"📦 Товар: {product}\n"
                f"🏷️ Категория: {category_key}\n"
                f"💲 Договорной тариф: **${agreed_rate}**\n\n"
                f"⚖️ **Введите ФАКТИЧЕСКИЙ ВЕС (кг):**"
            )
            return WAITING_ACTUAL_WEIGHT
        else:
            await query.edit_message_text("❌ Ошибка. Договор не найден в базе.")
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"❌ Ошибка начала приема: {e}")
        await query.edit_message_text("❌ Ошибка загрузки данных договора")
        return ConversationHandler.END
    finally:
        conn.close()

async def get_actual_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение фактического веса"""
    try:
        weight = clean_number(update.message.text)
        if weight <= 0:
            await update.message.reply_text("❌ Введите положительное число для веса:")
            return WAITING_ACTUAL_WEIGHT
            
        context.user_data['fact_weight'] = weight
        await update.message.reply_text("📏 **Введите ФАКТИЧЕСКИЙ ОБЪЕМ (м³):**")
        return WAITING_ACTUAL_VOLUME
    except:
        await update.message.reply_text("❌ Введите число!")
        return WAITING_ACTUAL_WEIGHT

async def get_actual_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение фактического объема"""
    try:
        volume = clean_number(update.message.text)
        if volume <= 0:
            await update.message.reply_text("❌ Введите положительное число для объема:")
            return WAITING_ACTUAL_VOLUME
            
        context.user_data['fact_volume'] = volume
        
        # 🔥 ПРАВИЛЬНЫЙ РАСЧЕТ с использованием универсальной функции
        result = universal_t1_calculation(
            context.user_data['fact_weight'], 
            volume, 
            context.user_data.get('cargo_type', 'obshhie'),
            context.user_data.get('warehouse_code', 'GZ')
        )
        
        new_rate = result['rate']
        old_rate = context.user_data['agreed_rate']
        
        # Уведомление об изменении тарифа
        if abs(new_rate - old_rate) > 0.01:  # если разница больше 1 цента
            await update.message.reply_text(
                f"⚠️ **ТАРИФ ИЗМЕНИЛСЯ!**\n"
                f"Был: ${old_rate:.2f} → Стал: **${new_rate:.2f}**\n"
                f"Причина: изменение веса/объема повлияло на категорию/плотность"
            )
        
        context.user_data['final_rate'] = new_rate
        
        await update.message.reply_text("🛠 **Введите стоимость ДОПОЛНИТЕЛЬНЫХ УСЛУГ ($)?**\n(0 если нет):")
        return WAITING_ADDITIONAL_COST
        
    except:
        await update.message.reply_text("❌ Введите число!")
        return WAITING_ACTUAL_VOLUME

async def get_additional_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение стоимости дополнительных услуг"""
    try:
        add_cost = clean_number(update.message.text)
        if add_cost < 0:
            await update.message.reply_text("❌ Введите положительное число или 0:")
            return WAITING_ADDITIONAL_COST
            
        context.user_data['add_cost'] = add_cost
        await update.message.reply_text(
            "📸 **Прикрепите ФОТО/ВИДЕО груза**\n"
            "Или отправьте /skip чтобы пропустить"
        )
        return WAITING_MEDIA
    except:
        await update.message.reply_text("❌ Введите число!")
        return WAITING_ADDITIONAL_COST

async def save_contract_final_with_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение итоговых данных с медиа"""
    file_id, media_link = None, None

    # Обработка медиа
    if update.message.text and update.message.text == '/skip':
        media_link = "Медиа не предоставлено"
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.video:
        file_id = update.message.video.file_id
    
    # Получение ссылки на медиа
    if file_id:
        try:
            file = await context.bot.get_file(file_id)
            file_path_clean = file.file_path.replace('https://api.telegram.org/file/bot', '').replace(TOKEN, '').replace('//', '')
            if file_path_clean.startswith('/'):
                file_path_clean = file_path_clean[1:]
            media_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path_clean}"
            logger.info(f"✅ Медиа ссылка создана: {media_link}")
        except Exception as e:
            logger.error(f"❌ Ошибка создания ссылки на медиа: {e}")
            media_link = "Ошибка получения ссылки"

    # Получение данных
    data = context.user_data
    contract_num = data['receiving_contract_num']
    weight = data['fact_weight']
    volume = data['fact_volume']
    add_cost = data['add_cost']
    rate = data['final_rate']
    
    # Генерация трек-номера
    wh_code = data.get('warehouse_code', 'GZ')
    prefix = WAREHOUSE_CONFIGS.get(wh_code, {}).get('prefix', 'GZ')
    gz_track = f"{prefix}{random.randint(100000, 999999)}"
    
    # 🔥 ПРАВИЛЬНЫЙ РАСЧЕТ итоговой стоимости
    result = universal_t1_calculation(weight, volume, data.get('cargo_type', 'obshhie'), wh_code)
    t1_cost = result['cost_usd']
    final_price = round(t1_cost + add_cost, 2)
    
    new_status = "Принят на складе"
    
    # Сохранение в БД
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shipments 
                SET status = %s, 
                    track_number = %s, 
                    actual_weight = %s, 
                    actual_volume = %s, 
                    additional_cost = %s, 
                    total_price_final = %s, 
                    agreed_rate = %s,
                    category = %s,
                    media_link = %s,
                    created_at = NOW() 
                WHERE contract_num = %s 
                RETURNING fio, phone
            """, (
                new_status, gz_track, weight, volume, add_cost, 
                final_price, rate, data.get('cargo_type', 'obshhie'),
                media_link, contract_num
            ))
            
            result = cur.fetchone()
            conn.commit()
            
            if result:
                fio, phone = result
                logger.info(f"✅ Груз принят: {contract_num} -> {gz_track}")
                
                # Отправка в Make.com
                make_payload = {
                    "contract_num": contract_num,
                    "track_number": gz_track,
                    "actual_weight": weight,
                    "actual_volume": volume,
                    "additional_cost": add_cost,
                    "total_price": final_price,
                    "status": new_status,
                    "media_link": media_link,
                    "fio": fio,
                    "phone": phone,
                    "warehouse": wh_code
                }
                
                notify_make(make_payload)
                
                await update.message.reply_text(
                    f"✅ **ГРУЗ ПРИНЯТ!**\n\n"
                    f"📦 Договор: `{contract_num}`\n"
                    f"🚚 Трек: `{gz_track}`\n"
                    f"⚖️ Вес: {weight} кг\n"
                    f"📏 Объем: {volume} м³\n"
                    f"💰 Итог: **${final_price}**\n"
                    f"💲 Тариф: ${rate:.2f}\n"
                    f"🏷️ Категория: {data.get('cargo_type', 'obshhie')}\n\n"
                    f"Груз готов к отправке!",
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text("❌ Ошибка обновления базы данных")
                
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения груза: {e}")
            await update.message.reply_text("❌ Ошибка сохранения данных")
        finally:
            conn.close()
    else:
        await update.message.reply_text("❌ Ошибка подключения к базе")
    
    return ConversationHandler.END

# --- СЦЕНАРИЙ 2: ОБНОВЛЕНИЕ СТАТУСОВ ---

async def set_status_mode(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    """Установка режима обновления статуса"""
    text = update.message.text
    if "ОТПРАВЛЕНО" in text:
        context.user_data['smode'] = "sent"
        status_text = "в пути до границы"
    elif "ГРАНИЦЕ" in text:
        context.user_data['smode'] = "border" 
        status_text = "на границе"
    elif "ДОСТАВЛЕНО" in text:
        context.user_data['smode'] = "delivered"
        status_text = "доставлен"
    else:
        await update.message.reply_text("❌ Неизвестный статус")
        return ConversationHandler.END
    
    # Показать список грузов для выбора
    conn = get_db_connection()
    if not conn:
        await update.message.reply_text("❌ Ошибка подключения к базе")
        return ConversationHandler.END
    
    try:
        cur = conn.cursor()
        
        # Определяем какие грузы показывать в зависимости от статуса
        if context.user_data['smode'] == "sent":
            # Показываем грузы со статусом "Принят на складе"
            cur.execute("""
                SELECT track_number, contract_num, product, actual_weight 
                FROM shipments 
                WHERE status ILIKE 'принят%' OR status ILIKE 'оформлен'
                ORDER BY created_at DESC LIMIT 10
            """)
        elif context.user_data['smode'] == "border":
            # Показываем грузы в пути
            cur.execute("""
                SELECT track_number, contract_num, product, actual_weight 
                FROM shipments 
                WHERE status ILIKE '%пути%' OR status ILIKE '%границ%'
                ORDER BY created_at DESC LIMIT 10
            """)
        else:  # delivered
            # Показываем грузы на границе
            cur.execute("""
                SELECT track_number, contract_num, product, actual_weight 
                FROM shipments 
                WHERE status ILIKE '%границ%'
                ORDER BY created_at DESC LIMIT 10
            """)
        
        rows = cur.fetchall()
        
        if not rows:
            await update.message.reply_text(f"📋 Нет грузов для статуса '{status_text}'")
            return ConversationHandler.END

        keyboard = []
        for row in rows:
            track, contract, product, weight = row
            display_track = track if track else contract
            text = f"{display_track} — {product[:20]}... ({weight}кг)"
            callback_data = f"status_{display_track}_{context.user_data['smode']}"
            keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])
        
        await update.message.reply_text(
            f"📋 **Выберите груз для статуса '{status_text}':**", 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки грузов: {e}")
        await update.message.reply_text("❌ Ошибка загрузки списка грузов")
    finally:
        conn.close()
    
    return WAITING_STATUS_TRACK

async def update_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора груза для обновления статуса"""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split('_')
    if len(data_parts) >= 3:
        track_number = data_parts[1]
        status_mode = data_parts[2]
        
        # Определяем новый статус и прогресс
        if status_mode == "sent":
            new_status = "в пути до границы"
            progress = 30
        elif status_mode == "border":
            new_status = "на границе" 
            progress = 60
        else:  # delivered
            new_status = "доставлен"
            progress = 100
        
        # Обновляем в БД
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE shipments 
                    SET status = %s, route_progress = %s 
                    WHERE track_number = %s OR contract_num = %s
                """, (new_status, progress, track_number, track_number))
                conn.commit()
                
                # Отправка в Make.com
                notify_make({
                    "event": "status_update",
                    "track_number": track_number,
                    "status": new_status,
                    "progress": progress
                })
                
                await query.edit_message_text(f"✅ Статус обновлен: {track_number} -> {new_status}")
                logger.info(f"✅ Статус обновлен: {track_number} -> {new_status}")
                
            except Exception as e:
                logger.error(f"❌ Ошибка обновления статуса: {e}")
                await query.edit_message_text("❌ Ошибка обновления статуса")
            finally:
                conn.close()
        else:
            await query.edit_message_text("❌ Ошибка подключения к базе")
    
    return ConversationHandler.END

async def update_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Резервный метод обновления статуса по тексту"""
    track = update.message.text.strip().upper()
    
    if track.startswith('➕') or track.startswith('📋'):
        return ConversationHandler.END
    
    status_mode = context.user_data.get('smode', 'sent')
    
    if status_mode == "sent":
        new_status = "в пути до границы"
        progress = 30
    elif status_mode == "border":
        new_status = "на границе"
        progress = 60
    else:  # delivered
        new_status = "доставлен" 
        progress = 100
    
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shipments 
                SET status = %s, route_progress = %s 
                WHERE track_number = %s OR contract_num = %s
            """, (new_status, progress, track, track))
            conn.commit()
            
            notify_make({
                "event": "status_update",
                "track_number": track,
                "status": new_status
            })
            
            await update.message.reply_text(f"✅ {new_status}: {track}")
            logger.info(f"✅ Статус обновлен: {track} -> {new_status}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка обновления статуса: {e}")
            await update.message.reply_text("❌ Ошибка обновления статуса")
        finally:
            conn.close()
    else:
        await update.message.reply_text("❌ Ошибка подключения к базе")
    
    return WAITING_STATUS_TRACK

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    """Отмена операции"""
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда старт"""
    kb = [
        [KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ")], 
        [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]
    ]
    await update.message.reply_text(
        "🏭 **СКЛАД POST PRO**\n\n"
        "Выберите действие:", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

def setup_handlers(app):
    """Настройка обработчиков"""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.Regex('^(📋 ОЖИДАЕМЫЕ ГРУЗЫ)$'), show_expected))
    
    # Обработчик приема грузов
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(start_contract_receive_button, pattern='^accept_')],
        states={
            WAITING_ACTUAL_WEIGHT: [MessageHandler(filters.TEXT, get_actual_weight)],
            WAITING_ACTUAL_VOLUME: [MessageHandler(filters.TEXT, get_actual_volume)],
            WAITING_ADDITIONAL_COST: [MessageHandler(filters.TEXT, get_additional_cost)],
            WAITING_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Regex('^/skip$'), save_contract_final_with_media)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    # Обработчик обновления статусов
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^(🚚 ОТПРАВЛЕНО|🛃 НА ГРАНИЦЕ|✅ ДОСТАВЛЕНО)$'), set_status_mode)],
        states={
            WAITING_STATUS_TRACK: [
                CallbackQueryHandler(update_status_callback, pattern='^status_'),
                MessageHandler(filters.TEXT, update_status)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))

if __name__ == '__main__':
    # Проверка обязательных переменных
    if not TOKEN:
        logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Не задан GUANGZHOU_BOT_TOKEN")
    elif not DATABASE_URL:
        logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Не задан DATABASE_URL")
    else:
        if not MAKE_WAREHOUSE_WEBHOOK:
            logger.warning("⚠️ ВНИМАНИЕ: MAKE_WAREHOUSE_WEBHOOK не задан. Бот будет работать, но не будет отправлять данные в Make.com")
        
        # Сброс вебхука и запуск
        force_delete_webhook(TOKEN)
        app = Application.builder().token(TOKEN).build()
        setup_handlers(app)
        logger.info("🚀 Складской бот запущен...")
        app.run_polling()
