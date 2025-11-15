import os
import logging
import random
import psycopg2
import requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
# Ссылка на Make (Сценарий 3: Уведомления)
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

GUANGZHOU_CONFIG = {"warehouse_name": "Гуанчжоу", "track_prefix": "GZ"}

# Состояния диалогов
WAITING_FIO, WAITING_PRODUCT, WAITING_WEIGHT, WAITING_VOLUME, WAITING_PHONE = range(5)
# Состояния для приемки по договору (План-Факт)
WAITING_ACTUAL_WEIGHT, WAITING_ACTUAL_VOLUME, WAITING_ADDITIONAL_COST, WAITING_MEDIA = range(5, 9)
# Состояние для смены статуса
WAITING_STATUS_TRACK = 9

class GuangzhouBot:
    def __init__(self):
        self.token = TOKEN
        self.application = None
        self.setup_bot()
    
    def setup_bot(self):
        if not self.token:
            logger.error("❌ ОШИБКА: Токен не найден! Проверьте GUANGZHOU_BOT_TOKEN.")
            return
        self.application = Application.builder().token(self.token).build()
        self.setup_handlers()
    
    def get_db_connection(self):
        try: return psycopg2.connect(DATABASE_URL)
        except Exception: return None

    # --- УВЕДОМЛЕНИЯ В MAKE ---
    def notify_make(self, event_type, data):
        if not MAKE_WAREHOUSE_WEBHOOK: return
        
        payload = {
            "event": event_type,
            "track": data.get('track_number'),
            "fio": data.get('fio'),
            "phone": data.get('phone'),
            "weight": data.get('actual_weight'),
            "final_price": data.get('final_price', 0),
            "additional_cost": data.get('additional_cost', 0),
            "status": data.get('status'),
            "manager": data.get('manager'),
            "file_id": data.get('file_id'), # ID фото/видео для Телеграма
            "media_type": data.get('media_type'),
            "timestamp": datetime.now().isoformat()
        }
        try: requests.post(MAKE_WAREHOUSE_WEBHOOK, json=payload, timeout=2)
        except: pass

    # --- СЦЕНАРИЙ 1: ПРИЕМКА ПО ДОГОВОРУ (ГЛАВНЫЙ) ---
    async def show_expected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        conn = self.get_db_connection()
        if not conn: return
        cur = conn.cursor()
        # Ищем грузы со статусом 'Оформлен'
        cur.execute("SELECT contract_num, fio, product, declared_weight FROM shipments WHERE status = 'Оформлен' ORDER BY created_at DESC LIMIT 10")
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            await update.message.reply_text("📋 **Список пуст.** Нет ожидаемых грузов.")
            return

        text = "📋 **ОЖИДАЮТСЯ:**\n"
        for row in rows: text += f"🔹 `{row[0]}` — {row[1]} ({row[2]}, ~{row[3]}кг)\n"
        text += "\n👇 **Введи номер CN-..., чтобы принять.**"
        await update.message.reply_text(text)

    async def start_contract_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        track = update.message.text.strip().upper()
        context.user_data['receiving_track'] = track
        
        conn = self.get_db_connection()
        if conn:
            cur = conn.cursor()
            # Достаем тариф (agreed_rate), чтобы посчитать финальную цену
            cur.execute("SELECT fio, agreed_rate FROM shipments WHERE contract_num = %s OR track_number = %s", (track, track))
            row = cur.fetchone()
            conn.close()
            
            if row:
                context.user_data['agreed_rate'] = float(row[1]) if row[1] else 0
                await update.message.reply_text(
                    f"📥 Приемка **{track}**\n"
                    f"👤 {row[0]}\n"
                    f"💲 Тариф из договора: **{row[1]} $/кг**\n\n"
                    f"⚖️ **Введите ФАКТИЧЕСКИЙ ВЕС (кг):**"
                )
                return WAITING_ACTUAL_WEIGHT
            else:
                await update.message.reply_text("❌ Не найдено.")
                return ConversationHandler.END

    async def get_actual_weight(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['fact_weight'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("📏 **Введите ФАКТ. ОБЪЕМ (м³):**")
            return WAITING_ACTUAL_VOLUME
        except ValueError:
            await update.message.reply_text("❌ Число!")
            return WAITING_ACTUAL_WEIGHT

    async def get_actual_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['fact_volume'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text(
                "🛠 **Стоимость доп. услуг ($)?**\n"
                "(Упаковка, обрешетка, страховка).\n"
                "Введите сумму (например: 20). Если нет - 0."
            )
            return WAITING_ADDITIONAL_COST
        except ValueError:
            await update.message.reply_text("❌ Число!")
            return WAITING_ACTUAL_VOLUME

    async def get_additional_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['add_cost'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text(
                "📸 **Сделай ФОТО груза на весах!**\n"
                "Отправь фото или видео.\n"
                "Если не нужно, нажми /skip"
            )
            return WAITING_MEDIA
        except ValueError:
            await update.message.reply_text("❌ Число.")
            return WAITING_ADDITIONAL_COST

    async def save_contract_final_with_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Ловим медиа
        file_id = None
        media_type = None
        
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            media_type = "photo"
        elif update.message.video:
            file_id = update.message.video.file_id
            media_type = "video"
        
        if update.message.text == '/skip':
            file_id = None

        # Расчет
        track = context.user_data['receiving_track']
        weight = context.user_data['fact_weight']
        volume = context.user_data['fact_volume']
        add_cost = context.user_data['add_cost']
        rate = context.user_data['agreed_rate']
        
        # 💰 ФОРМУЛА: (Вес * Тариф) + Допы
        final_price = round((weight * rate) + add_cost, 2)
        
        conn = self.get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shipments 
                SET status = 'принят на складе', 
                    actual_weight = %s, actual_volume = %s,
                    additional_cost = %s, total_price_final = %s,
                    created_at = NOW() 
                WHERE contract_num = %s OR track_number = %s
                RETURNING fio, phone
            """, (weight, volume, add_cost, final_price, track, track))
            
            res = cur.fetchone()
            conn.commit()
            conn.close()
            
            # Отправка в Make (Сценарий 3)
            self.notify_make("received_final", {
                "track_number": track,
                "fio": res[0],
                "phone": res[1],
                "actual_weight": weight,
                "final_price": final_price,
                "additional_cost": add_cost,
                "file_id": file_id,   # ID фото для пересылки
                "media_type": media_type,
                "status": "принят на складе",
                "manager": update.message.from_user.first_name
            })

            await update.message.reply_text(
                f"✅ **ГРУЗ ПРИНЯТ!**\n"
                f"⚖️ Вес: {weight} кг\n"
                f"💰 **ИТОГ К ОПЛАТЕ: {final_price} $**\n"
                f"Фото отправлено менеджеру."
            )
        
        return ConversationHandler.END

    # --- СЦЕНАРИЙ 2: НОВЫЙ ГРУЗ (Упрощенный) ---
    async def start_new_cargo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👤 ФИО:")
        return WAITING_FIO
    async def get_fio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['new_fio'] = update.message.text
        await update.message.reply_text("📦 Товар:")
        return WAITING_PRODUCT
    async def get_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['new_product'] = update.message.text
        await update.message.reply_text("⚖️ Вес:")
        return WAITING_WEIGHT
    async def get_weight(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['new_weight'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("📏 Объем:")
            return WAITING_VOLUME
        except: 
            await update.message.reply_text("Число!")
            return WAITING_WEIGHT
    async def get_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['new_volume'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("📞 Телефон:")
            return WAITING_PHONE
        except: 
            await update.message.reply_text("Число!")
            return WAITING_VOLUME
    async def get_phone_and_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        phone = update.message.text
        track = f"{GUANGZHOU_CONFIG['track_prefix']}{random.randint(100000, 999999)}"
        conn = self.get_db_connection()
        if conn:
            cur = conn.cursor()
            w = context.user_data['new_weight']
            v = context.user_data['new_volume']
            cur.execute("INSERT INTO shipments (track_number, fio, phone, product, declared_weight, actual_weight, declared_volume, actual_volume, status, route_progress, warehouse_code, manager, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", 
                        (track, context.user_data['new_fio'], phone, context.user_data['new_product'], w, w, v, v, "принят на складе", 0, GUANGZHOU_CONFIG['warehouse_name'], update.message.from_user.first_name))
            conn.commit()
            conn.close()
            self.notify_make("received", {"track_number": track, "fio": context.user_data['new_fio'], "status": "принят на складе"})
            await update.message.reply_text(f"✅ Груз {track} создан!")
        return ConversationHandler.END

    # --- СЦЕНАРИЙ 3: СТАТУСЫ ---
    async def set_status_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        mode = "sent" if "ОТПРАВЛЕНО" in text else "border" if "НА ГРАНИЦЕ" in text else "delivered"
        context.user_data['status_mode'] = mode
        await update.message.reply_text(f"🔄 Режим: **{text}**\n👇 Сканируй треки:")
        return WAITING_STATUS_TRACK

    async def update_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        track = update.message.text.strip().upper()
        if track in ["➕ НОВЫЙ ГРУЗ", "📋 ОЖИДАЕМЫЕ ГРУЗЫ", "🚚 ОТПРАВЛЕНО", "🛃 НА ГРАНИЦЕ", "✅ ДОСТАВЛЕНО"]: return ConversationHandler.END
        mode = context.user_data.get('status_mode')
        status_map = {"sent": "в пути до границы", "border": "на границе", "delivered": "доставлен"}
        
        if mode in status_map:
            new_status = status_map[mode]
            conn = self.get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT fio, phone, actual_weight FROM shipments WHERE track_number = %s OR contract_num = %s", (track, track))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE shipments SET status = %s WHERE track_number = %s OR contract_num = %s", (new_status, track, track))
                    conn.commit()
                    self.notify_make(mode, {"track_number": track, "fio": row[0], "status": new_status})
                    await update.message.reply_text(f"✅ {new_status}: {track}")
                else: await update.message.reply_text("❌ Не найден.")
                conn.close()
        return WAITING_STATUS_TRACK

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🏠 Меню.")
        return ConversationHandler.END
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [[KeyboardButton("➕ НОВЫЙ ГРУЗ"), KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ")], [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("🛃 НА ГРАНИЦЕ"), KeyboardButton("✅ ДОСТАВЛЕНО")]]
        await update.message.reply_text("🏭 **СКЛАД ГУАНЧЖОУ**", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 ОЖИДАЕМЫЕ ГРУЗЫ)$'), self.show_expected))
        
        # Приемка по договору (CN-...)
        self.application.add_handler(ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(r'^CN-\d+'), self.start_contract_receive)],
            states={
                WAITING_ACTUAL_WEIGHT: [MessageHandler(filters.TEXT, self.get_actual_weight)],
                WAITING_ACTUAL_VOLUME: [MessageHandler(filters.TEXT, self.get_actual_volume)],
                WAITING_ADDITIONAL_COST: [MessageHandler(filters.TEXT, self.get_additional_cost)],
                WAITING_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Regex('/skip'), self.save_contract_final_with_media)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel)]
        ))
        
        # Новый груз
        self.application.add_handler(ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ НОВЫЙ ГРУЗ)'), self.start_new_cargo)],
            states={WAITING_FIO: [MessageHandler(filters.TEXT, self.get_fio)], WAITING_PRODUCT: [MessageHandler(filters.TEXT, self.get_product)], WAITING_WEIGHT: [MessageHandler(filters.TEXT, self.get_weight)], WAITING_VOLUME: [MessageHandler(filters.TEXT, self.get_volume)], WAITING_PHONE: [MessageHandler(filters.TEXT, self.get_phone_and_save)]},
            fallbacks=[CommandHandler('cancel', self.cancel)]
        ))
        
        # Статусы
        self.application.add_handler(ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🚚|🛃|✅)'), self.set_status_mode)],
            states={WAITING_STATUS_TRACK: [MessageHandler(filters.TEXT, self.update_status)]},
            fallbacks=[CommandHandler('cancel', self.cancel), MessageHandler(filters.Regex('^➕'), self.cancel)]
        ))

    def run(self):
        self.application.run_polling()

if __name__ == '__main__':
    bot = GuangzhouBot()
    bot.run()