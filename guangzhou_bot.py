import os
import logging
import random
import psycopg2
import requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('GUANGZHOU_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
MAKE_WAREHOUSE_WEBHOOK = os.getenv('MAKE_WAREHOUSE_WEBHOOK')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

GUANGZHOU_CONFIG = {"warehouse_name": "Гуанчжоу", "track_prefix": "GZ"}

WAITING_FIO, WAITING_PRODUCT, WAITING_WEIGHT, WAITING_VOLUME, WAITING_PHONE = range(5)
WAITING_ACTUAL_WEIGHT, WAITING_ACTUAL_VOLUME, WAITING_ADDITIONAL_COST, WAITING_MEDIA = range(5, 9)
WAITING_STATUS_TRACK = 9

class GuangzhouBot:
    def __init__(self):
        self.token = TOKEN
        self.application = None
        self.setup_bot()
    
    def setup_bot(self):
        if not self.token: return
        
        # 🔥 СБРОС ВЕБХУКА ПЕРЕД ЗАПУСКОМ
        try:
            url = f"https://api.telegram.org/bot{self.token}/deleteWebhook?drop_pending_updates=True"
            requests.get(url)
            logger.info("♻️ Склад: Вебхук сброшен.")
        except: pass
        
        self.application = Application.builder().token(self.token).build()
        self.setup_handlers()
    
    def get_db_connection(self):
        try: return psycopg2.connect(DATABASE_URL)
        except Exception: return None

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
            "file_id": data.get('file_id'),
            "media_type": data.get('media_type'),
            "timestamp": datetime.now().isoformat()
        }
        try: requests.post(MAKE_WAREHOUSE_WEBHOOK, json=payload, timeout=2)
        except: pass

    # --- ОСНОВНОЙ СЦЕНАРИЙ (CN) ---
    async def show_expected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        conn = self.get_db_connection()
        if not conn: return
        cur = conn.cursor()
        cur.execute("SELECT contract_num, fio, product FROM shipments WHERE status = 'Оформлен' ORDER BY created_at DESC LIMIT 10")
        rows = cur.fetchall()
        conn.close()
        text = "📋 **ОЖИДАЮТСЯ:**\n"
        for row in rows: text += f"🔹 `{row[0]}` — {row[1]} ({row[2]})\n"
        text += "\n👇 **Введи номер CN-..., чтобы принять.**"
        await update.message.reply_text(text)

    async def start_contract_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        track = update.message.text.strip().upper()
        context.user_data['receiving_track'] = track
        conn = self.get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT fio, agreed_rate FROM shipments WHERE contract_num = %s OR track_number = %s", (track, track))
            row = cur.fetchone()
            conn.close()
            if row:
                context.user_data['agreed_rate'] = float(row[1]) if row[1] else 0
                await update.message.reply_text(f"📥 Приемка **{track}**\n👤 {row[0]}\n💲 Тариф: **{row[1]}**\n\n⚖️ **Введите ФАКТ. ВЕС:**")
                return WAITING_ACTUAL_WEIGHT
            else:
                await update.message.reply_text("❌ Не найдено.")
                return ConversationHandler.END

    async def get_actual_weight(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['fact_weight'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("📏 **ФАКТ. ОБЪЕМ:**")
            return WAITING_ACTUAL_VOLUME
        except: 
            await update.message.reply_text("❌ Число!")
            return WAITING_ACTUAL_WEIGHT

    async def get_actual_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['fact_volume'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("🛠 **Доп. услуги ($)?**\n(0 если нет):")
            return WAITING_ADDITIONAL_COST
        except: 
            await update.message.reply_text("❌ Число!")
            return WAITING_ACTUAL_VOLUME

    async def get_additional_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['add_cost'] = float(update.message.text.replace(',', '.'))
            await update.message.reply_text("📸 **ФОТО/ВИДЕО?**\n(/skip если нет)")
            return WAITING_MEDIA
        except: 
            await update.message.reply_text("❌ Число!")
            return WAITING_ADDITIONAL_COST

    async def save_contract_final_with_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        file_id = update.message.photo[-1].file_id if update.message.photo else (update.message.video.file_id if update.message.video else None)
        media_type = "photo" if update.message.photo else ("video" if update.message.video else None)
        
        track = context.user_data['receiving_track']
        weight = context.user_data['fact_weight']
        final_price = round((weight * context.user_data['agreed_rate']) + context.user_data['add_cost'], 2)
        
        conn = self.get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shipments SET status = 'принят на складе', actual_weight = %s, actual_volume = %s, additional_cost = %s, total_price_final = %s, created_at = NOW() 
                WHERE contract_num = %s OR track_number = %s RETURNING fio, phone
            """, (weight, context.user_data['fact_volume'], context.user_data['add_cost'], final_price, track, track))
            res = cur.fetchone()
            conn.commit()
            conn.close()
            
            self.notify_make("received_final", {
                "track_number": track, "fio": res[0], "weight": weight, "final_price": final_price,
                "additional_cost": context.user_data['add_cost'], "file_id": file_id, "media_type": media_type, "status": "принят на складе"
            })
            await update.message.reply_text(f"✅ **ПРИНЯТО!**\n💰 Итог: {final_price} $")
        return ConversationHandler.END

    # --- ПРОЧИЕ ФУНКЦИИ (СОКРАЩЕНО ДЛЯ ВСТАВКИ) ---
    async def cancel(self, u, c): await u.message.reply_text("Меню."); return ConversationHandler.END
    async def start_command(self, u, c):
        kb = [[KeyboardButton("➕ НОВЫЙ ГРУЗ"), KeyboardButton("📋 ОЖИДАЕМЫЕ ГРУЗЫ")], [KeyboardButton("🚚 ОТПРАВЛЕНО"), KeyboardButton("✅ ДОСТАВЛЕНО")]]
        await u.message.reply_text("🏭 **СКЛАД**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    
    # --- НОВЫЙ ГРУЗ (БЕЗ ДОГОВОРА) ---
    async def start_new(self, u, c): await u.message.reply_text("👤 ФИО:"); return WAITING_FIO
    async def get_fio(self, u, c): c.user_data['new_fio']=u.message.text; await u.message.reply_text("📦 Товар:"); return WAITING_PRODUCT
    async def get_prod(self, u, c): c.user_data['new_prod']=u.message.text; await u.message.reply_text("⚖️ Вес:"); return WAITING_WEIGHT
    async def get_wei(self, u, c): c.user_data['new_wei']=float(u.message.text.replace(',','.')); await u.message.reply_text("📏 Объем:"); return WAITING_VOLUME
    async def get_vol(self, u, c): c.user_data['new_vol']=float(u.message.text.replace(',','.')); await u.message.reply_text("📞 Тел:"); return WAITING_PHONE
    async def get_pho(self, u, c):
        t = f"GZ{random.randint(100000,999999)}"
        cn = self.get_db_connection()
        if cn:
            cr = cn.cursor()
            w = c.user_data['new_wei']
            cr.execute("INSERT INTO shipments (track_number, fio, phone, product, declared_weight, actual_weight, declared_volume, actual_volume, status, route_progress, warehouse_code, manager, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", 
                       (t, c.user_data['new_fio'], u.message.text, c.user_data['new_prod'], w, w, c.user_data['new_vol'], c.user_data['new_vol'], "принят на складе", 0, GUANGZHOU_CONFIG['warehouse_name'], "Sklad"))
            cn.commit(); cn.close()
            self.notify_make("received", {"track_number": t, "fio": c.user_data['new_fio'], "status": "принят на складе"})
            await u.message.reply_text(f"✅ Груз {t} создан!")
        return ConversationHandler.END

    # --- СМЕНА СТАТУСА ---
    async def set_stat(self, u, c): c.user_data['smode'] = "sent" if "ОТПРАВЛЕНО" in u.message.text else "delivered"; await u.message.reply_text("👇 Трек:"); return WAITING_STATUS_TRACK
    async def upd_stat(self, u, c):
        t = u.message.text.strip().upper()
        if t.startswith("➕") or t.startswith("📋"): return ConversationHandler.END
        st = "в пути до границы" if c.user_data['smode'] == "sent" else "доставлен"
        cn = self.get_db_connection()
        if cn:
            cr = cn.cursor(); cr.execute("UPDATE shipments SET status=%s WHERE track_number=%s OR contract_num=%s", (st, t, t)); cn.commit(); cn.close()
            self.notify_make(c.user_data['smode'], {"track_number": t, "status": st})
            await u.message.reply_text(f"✅ {st}: {t}")
        return WAITING_STATUS_TRACK

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 ОЖИДАЕМЫЕ ГРУЗЫ)$'), self.show_expected))
        self.application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex(r'^CN-\d+'), self.start_contract_receive)], states={WAITING_ACTUAL_WEIGHT: [MessageHandler(filters.TEXT, self.get_actual_weight)], WAITING_ACTUAL_VOLUME: [MessageHandler(filters.TEXT, self.get_actual_volume)], WAITING_ADDITIONAL_COST: [MessageHandler(filters.TEXT, self.get_additional_cost)], WAITING_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Regex('/skip'), self.save_contract_final_with_media)]}, fallbacks=[CommandHandler('cancel', self.cancel)]))
        self.application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex('^(➕ НОВЫЙ ГРУЗ)'), self.start_new)], states={WAITING_FIO: [MessageHandler(filters.TEXT, self.get_fio)], WAITING_PRODUCT: [MessageHandler(filters.TEXT, self.get_prod)], WAITING_WEIGHT: [MessageHandler(filters.TEXT, self.get_wei)], WAITING_VOLUME: [MessageHandler(filters.TEXT, self.get_vol)], WAITING_PHONE: [MessageHandler(filters.TEXT, self.get_pho)]}, fallbacks=[CommandHandler('cancel', self.cancel)]))
        self.application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex('^(🚚|✅)'), self.set_stat)], states={WAITING_STATUS_TRACK: [MessageHandler(filters.TEXT, self.upd_stat)]}, fallbacks=[CommandHandler('cancel', self.cancel), MessageHandler(filters.Regex('^➕'), self.cancel)]))

    def run(self):
        logger.info("🚀 Склад запущен (Webhook killed).")
        self.application.run_polling()

if __name__ == '__main__':
    bot = GuangzhouBot()
    bot.run()