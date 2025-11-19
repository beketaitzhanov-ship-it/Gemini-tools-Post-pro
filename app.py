import os
import logging
import requests
import json
import psycopg2
import re
import time
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from dotenv import load_dotenv

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') 
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 
MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK')
MAKE_CONTRACT_WEBHOOK = os.getenv('MAKE_CONTRACT_WEBHOOK')
MAKE_AI_CHAT_WEBHOOK = os.getenv('MAKE_AI_CHAT_WEBHOOK')
MAKE_TIKTOK_WEBHOOK = os.getenv('MAKE_TIKTOK_WEBHOOK')

# КОНТАКТЫ ДЛЯ ЖИВОГО ЧАТА
MANAGER_WA = "77000479530"
MANAGER_TG = "PostProLogistics"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
    EXCHANGE_RATE = CONFIG.get('EXCHANGE_RATE', {}).get('rate', 500)
    T2_RATES = CONFIG.get('T2_RATES_DETAILED', {}).get('large_parcel', {})
except Exception as e:
    logger.error(f"Config Error: {e}")
    CONFIG = {}
    EXCHANGE_RATE = 500
    T2_RATES = {}

WAREHOUSE_NAMES = {"GZ": "Гуанчжоу", "FS": "Фошань", "IW": "Иу"}

# --- КАТЕГОРИИ (КНОПКИ) ---
CATEGORY_BUTTONS = {
    "odezhda": "👕 Одежда", "obuv": "👟 Обувь", "sumki": "👜 Сумки",
    "tovary_dlja_doma": "🏠 Хозтовары", "igrushki": "🧸 Игрушки", "mebel": "🛋 Мебель",
    "elektronika": "💻 Электроника", "telefony": "📱 Телефоны", "avtozapchasti": "🚗 Автозапчасти",
    "santehnika": "🚿 Сантехника", "oborudovanie": "⚙️ Оборудование", "strojmaterialy": "🧱 Строймат.",
    "tovary_dlja_zhivotnyh": "🐾 Зоотовары", "obshhie": "📦 Прочее"
}

# --- СОСТОЯНИЯ ---
(CLIENT_CITY, CLIENT_WAREHOUSE, CLIENT_PRODUCT, CLIENT_WEIGHT, 
 CLIENT_VOLUME, CLIENT_ADD_MORE, CLIENT_DECISION, CLIENT_NAME, CLIENT_PHONE) = range(9)

(ADM_NAME, ADM_PHONE, ADM_CITY, ADM_WAREHOUSE, ADM_PRODUCT, 
 ADM_WEIGHT, ADM_VOLUME, ADM_RATE, ADM_CONFIRM, ADM_EDIT_FIELD) = range(9, 19)

# --- МЕНЮ ---
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🚚 Калькулятор"), KeyboardButton("🔎 Отследить груз")],
        [KeyboardButton("🗣 Живой чат"), KeyboardButton("ℹ️ О компании")]
    ],
    resize_keyboard=True
)

# ================= ФУНКЦИИ =================

def get_db_connection():
    try: return psycopg2.connect(DATABASE_URL)
    except: return None

def clean_number(text):
    if not text: return 0.0
    try: return float(text.replace(',', '.').strip())
    except: return 0.0

def parse_volume_input(text):
    text = text.lower().replace('х', 'x').replace('*', 'x')
    try:
        val = float(text.replace(',', '.'))
        if val < 20: return val 
    except: pass
    dims = re.findall(r'(\d+[.,]?\d*)', text)
    if len(dims) >= 3:
        try:
            l = float(dims[-3].replace(',', '.'))
            w = float(dims[-2].replace(',', '.'))
            h = float(dims[-1].replace(',', '.'))
            count = 1
            if len(dims) >= 4: count = float(dims[-4].replace(',', '.'))
            return round((l * w * h / 1000000) * count, 4)
        except: return 0.0
    return 0.0

def get_product_category_from_ai(text):
    """Только для админки и AI чата"""
    if not MAKE_CATEGORIZER_WEBHOOK: return "obshhie"
    try:
        resp = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': text}, timeout=10)
        key = resp.json().get('category_key')
        return key.lower() if key else "obshhie"
    except: return "obshhie"

def send_tiktok_event(phone):
    if not MAKE_TIKTOK_WEBHOOK: return
    try: requests.post(MAKE_TIKTOK_WEBHOOK, json={'phone': phone}, timeout=5)
    except: pass

def calculate_t1_line_item(weight, volume, category_key, warehouse):
    rates = CONFIG.get('T1_RATES_DENSITY', {}).get(warehouse, CONFIG.get('T1_RATES_DENSITY', {}).get('GZ', {}))
    cat_rates = rates.get(category_key, rates.get('obshhie'))
    density = weight / volume if volume > 0 else 9999.0
    base_price = 0
    if cat_rates:
        for r in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= r.get('min_density', 0):
                base_price = r.get('price', 0); break
        if base_price == 0: base_price = cat_rates[-1].get('price', 0)
    client_rate = base_price * 1.30
    is_cbm = client_rate > 50
    cost = (client_rate * volume) if is_cbm else (client_rate * weight)
    return round(cost, 2), round(client_rate, 2), round(density, 2), is_cbm

def calculate_t2_total(total_weight, city_name):
    city_key = city_name.lower().strip()
    zone = CONFIG.get('DESTINATION_ZONES', {}).get(city_key, "5") 
    zone = str(zone)
    weight_ranges = T2_RATES.get('weight_ranges', [])
    extra_kg_rate = T2_RATES.get('extra_kg_rate', {}).get(zone, 260)
    final_kzt_cost = 0
    if total_weight <= 0: return 0, 0.8
    found_range = False
    for r in weight_ranges:
        if total_weight <= r['max']:
            final_kzt_cost = r['zones'].get(zone, 5000)
            found_range = True; break
    if not found_range and total_weight > 0 and weight_ranges:
        rate_20kg_info = weight_ranges[-1].get('zones', {}) 
        base_rate_20kg = rate_20kg_info.get(zone, 5000) 
        final_kzt_cost = base_rate_20kg + (total_weight - 20) * extra_kg_rate
    ref_rate_usd = {"1": 0.4, "2": 0.5, "3": 0.6, "4": 0.7, "5": 0.8}.get(zone, 0.8)
    return int(final_kzt_cost), ref_rate_usd

def generate_vertical_map(status, progress, warehouse_code="GZ", city_to="Алматы"):
    start_city = WAREHOUSE_NAMES.get(warehouse_code, "Гуанчжоу")
    route = [start_city, "Чанша", "Сиань", "Ланьчжоу", "Урумчи", "Хоргос (Граница)", city_to]
    pos = 0
    if progress >= 100: pos = 6
    elif progress >= 90: pos = 5
    elif progress >= 70: pos = 4
    elif progress >= 50: pos = 3
    elif progress >= 30: pos = 2
    elif progress >= 15: pos = 1
    map_lines = []
    for i, city in enumerate(route):
        if i < pos: map_lines.append(f"✅ {city}\n      ⬇️")
        elif i == pos: map_lines.append(f"🚚 <b>{city.upper()}</b> 📍" + ("\n      ⬇️" if i != 6 else ""))
        else: map_lines.append(f"⬜️ {city}" + ("\n      ⬇️" if i != 6 else ""))
    return "\n".join(map_lines)

# ================= HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Здравствуйте! Я — Айсулу, ваш менеджер Post Pro.</b>\n"
        "Я помогу рассчитать доставку, отследить груз и отвечу на вопросы на 3 языках.\n\n"
        "<b>Меню:</b>",
        reply_markup=MAIN_MENU, parse_mode='HTML'
    )
    return ConversationHandler.END

async def info_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>О компании Post Pro</b>\n\n"
        "Мы занимаемся доставкой грузов из Китая в Казахстан уже более 5 лет.\n"
        "✅ Склады: Гуанчжоу, Иу, Фошань\n"
        "✅ Авто и ЖД доставка\n"
        "✅ Полное сопровождение\n\n"
        "📍 Адрес в Алматы: Рыскулова 103В.",
        parse_mode='HTML'
    )

async def live_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💬 WhatsApp", url=f"https://wa.me/{MANAGER_WA}")],
        [InlineKeyboardButton("✈️ Telegram", url=f"https://t.me/{MANAGER_TG}")]
    ]
    await update.message.reply_text(
        "👩‍💻 <b>Свяжитесь с менеджером в удобном мессенджере:</b>",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='HTML'
    )

async def restart_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Начинаем новый расчет.")
    return await calc_start(update, context)

async def restart_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Уважаемый клиент, введите трэк номер:")
    return ConversationHandler.END

# --- КЛИЕНТ ---

async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cart'] = []
    await update.message.reply_text("🏙 Введите <b>Город доставки</b> (в Казахстане):", parse_mode='HTML', reply_markup=MAIN_MENU)
    return CLIENT_CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['city'] = update.message.text
    kb = [[KeyboardButton("🇨🇳 Гуанчжоу"), KeyboardButton("🇨🇳 Фошань"), KeyboardButton("🇨🇳 Иу")]]
    await update.message.reply_text("✅ Склад:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True), parse_mode='HTML')
    return CLIENT_WAREHOUSE

async def get_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    code = "GZ"
    if "Фошань" in text: code = "FS"
    elif "Иу" in text: code = "IW"
    context.user_data['wh_code'] = code
    context.user_data['wh_name'] = WAREHOUSE_NAMES.get(code, "Гуанчжоу")
    
    keyboard = []
    row = []
    for key, name in CATEGORY_BUTTONS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"cat_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    
    await update.message.reply_text(
        f"📦 <b>Выберите категорию товара:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return CLIENT_PRODUCT

async def save_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_key = query.data.replace("cat_", "")
    cat_name = CATEGORY_BUTTONS.get(cat_key, cat_key)
    context.user_data['current_item'] = {'name': cat_name, 'category': cat_key}
    await query.edit_message_text(f"📦 Товар: <b>{cat_name}</b>\n⚖️ Введите <b>Вес (кг)</b>:", parse_mode='HTML')
    return CLIENT_WEIGHT

async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    w = clean_number(update.message.text)
    if w <= 0:
        await update.message.reply_text("🔢 Введите число:", reply_markup=MAIN_MENU); return CLIENT_WEIGHT
    context.user_data['current_item']['weight'] = w
    await update.message.reply_text("📦 <b>Объем (м³)</b>:", parse_mode='HTML', reply_markup=MAIN_MENU)
    return CLIENT_VOLUME

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vol = parse_volume_input(update.message.text)
    if vol <= 0: vol = context.user_data['current_item']['weight'] / 200
    context.user_data['current_item']['volume'] = vol
    context.user_data['cart'].append(context.user_data['current_item'])
    return await show_final_report(update, context)

async def handle_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Добавить" in update.message.text:
        keyboard = []
        row = []
        for key, name in CATEGORY_BUTTONS.items():
            row.append(InlineKeyboardButton(name, callback_data=f"cat_{key}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
        await update.message.reply_text("📦 <b>Выберите следующий товар:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return CLIENT_PRODUCT
    return await show_final_report(update, context)

async def show_final_report(update, context):
    d = context.user_data
    total_w = sum(i['weight'] for i in d['cart'])
    
    t1_total = 0
    for item in d['cart']:
        cost, _, _, _ = calculate_t1_line_item(item['weight'], item['volume'], item['category'], d['wh_code'])
        t1_total += cost
    
    t2_kzt, _ = calculate_t2_total(total_w, d['city'])
    
    item = d['cart'][0]
    report = (
        f"📊 <b>Расчет для г. {d['city']} (Склад: {d['wh_name']})</b>\n"
        f"📦 Товар: {item['name']} ({item['weight']} кг / {item['volume']} м³)\n\n"
        f"🇨🇳 <b>Т1 (Китай → Алматы):</b>\n"
        f"• Сумма: <b>${t1_total:.2f}</b>\n\n"
        f"🇰🇿 <b>Т2 (Алматы → Дверь):</b>\n"
        f"• Сумма: <b>~{t2_kzt} ₸</b>\n\n"
        f"<i>Тариф по РК предварительный, точный — по прибытию в Алматы.</i>"
    )
    
    kb = [[KeyboardButton("✅ Оставить заявку"), KeyboardButton("🔄 Новый расчет")]]
    if update.callback_query:
        await update.callback_query.message.reply_text(report, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    else:
        await update.message.reply_text(report, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    return CLIENT_DECISION

async def client_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Оставить" in update.message.text:
        await update.message.reply_text("👤 Как к вам обращаться? (Имя):", reply_markup=ReplyKeyboardRemove()); return CLIENT_NAME
    return await calc_start(update, context)

async def client_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['client_name'] = update.message.text
    await update.message.reply_text("📱 Ваш телефон:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Отправить контакт", request_contact=True)]], resize_keyboard=True)); return CLIENT_PHONE

async def client_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    d = context.user_data
    send_tiktok_event(phone)

    if ADMIN_CHAT_ID:
        total_w = sum(i['weight'] for i in d['cart'])
        t1_total = sum(calculate_t1_line_item(i['weight'], i['volume'], i['category'], d['wh_code'])[0] for i in d['cart'])
        
        kb = InlineKeyboardButton("⚡️ Оформить контракт (Авто)", callback_data="admin_auto_create")
        
        # Сохраняем лид в контекст бота для автозаполнения
        context.bot_data['last_lead'] = {
            'name': d['client_name'], 'phone': phone, 'city': d['city'],
            'wh': d['wh_code'], 'prod': d['cart'][0]['category'], 
            'w': total_w, 'v': d['cart'][0]['volume']
        }
        
        admin_text = (
            f"🔥 <b>НОВАЯ ЗАЯВКА</b>\n"
            f"👤 {d['client_name']}\n📞 {phone}\n🏙 {d['city']}\n"
            f"📦 {d['cart'][0]['name']} ({d['cart'][0]['category']})\n"
            f"⚖️ {total_w} кг\n💰 ${t1_total:.2f}"
        )
        try: 
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[kb]]))
        except: pass
        
    await update.message.reply_text("✅ Заявка принята!", reply_markup=MAIN_MENU); return ConversationHandler.END

# --- АДМИНКА ---

async def admin_start(u, c): 
    if str(u.effective_user.id) != str(ADMIN_CHAT_ID): return ConversationHandler.END
    kb = [[KeyboardButton("📝 Создать контракт")], [KeyboardButton("🔙 Выход")]]
    await u.message.reply_text("👨‍💻 Админка", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)); return ConversationHandler.END

async def admin_auto_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if 'last_lead' not in context.bot_data:
        await query.message.reply_text("Нет данных.")
        return ConversationHandler.END
    lead = context.bot_data['last_lead']
    context.user_data.update({
        'adm_name': lead['name'], 'adm_phone': lead['phone'], 'adm_city': lead['city'],
        'adm_wh': lead['wh'], 'adm_prod': lead['prod'], 'adm_w': lead['w'], 'adm_vol': lead['v']
    })
    return await admin_v_preview(query, context)

async def admin_create_manual(u, c):
    if str(u.effective_user.id) != str(ADMIN_CHAT_ID): return ConversationHandler.END
    await u.message.reply_text("👤 Клиент:", reply_markup=ReplyKeyboardRemove()); return ADM_NAME

async def admin_name(u, c): c.user_data['adm_name'] = u.message.text; await u.message.reply_text("📱 Телефон:"); return ADM_PHONE
async def admin_phone(u, c): c.user_data['adm_phone'] = u.message.text; await u.message.reply_text("🏙 Город:"); return ADM_CITY
async def admin_city(u, c): c.user_data['adm_city'] = u.message.text; await u.message.reply_text("🏭 Склад (GZ/IW/FS):", reply_markup=ReplyKeyboardMarkup([["GZ","IW","FS"]], one_time_keyboard=True)); return ADM_WAREHOUSE
async def admin_wh(u, c): c.user_data['adm_wh'] = u.message.text; await u.message.reply_text("📦 Товар (код):", reply_markup=ReplyKeyboardRemove()); return ADM_PRODUCT
async def admin_prod(u, c): c.user_data['adm_prod'] = u.message.text; await u.message.reply_text("⚖️ Вес:"); return ADM_WEIGHT
async def admin_w(u, c): c.user_data['adm_w'] = clean_number(u.message.text); await u.message.reply_text("📦 Объем:"); return ADM_VOLUME

async def admin_v_preview(u, c): 
    # Точка входа перед подтверждением (из ручного или авто)
    if hasattr(u, 'message') and u.message: c.user_data['adm_vol'] = clean_number(u.message.text)
    d = c.user_data
    
    _, final_rate, _, _ = calculate_t1_line_item(d['adm_w'], d['adm_vol'], d['adm_prod'], d['adm_wh'])
    c.user_data['final_rate'] = final_rate
    
    msg = (
        f"⚙️ <b>Проверка:</b>\n"
        f"👤 {d['adm_name']}\n📦 {d['adm_prod']}\n"
        f"⚖️ {d['adm_w']} кг | {d['adm_vol']} м³\n"
        f"💰 Тариф: <b>${final_rate}</b>"
    )
    kb = [
        [InlineKeyboardButton(f"✅ СОЗДАТЬ", callback_data="confirm_create")],
        [InlineKeyboardButton("✏️ Изм. Тариф", callback_data="edit_rate"), InlineKeyboardButton("✏️ Изм. Вес", callback_data="edit_weight")]
    ]
    
    if hasattr(u, 'message') and u.message: await u.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    else: await u.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    
    return ADM_CONFIRM

async def admin_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "confirm_create": return await admin_fin(query, context)
    elif data == "edit_rate":
        await query.message.reply_text("💰 Новый тариф:")
        context.user_data['edit_mode'] = 'rate'
        return ADM_EDIT_FIELD
    elif data == "edit_weight":
        await query.message.reply_text("⚖️ Новый вес:")
        context.user_data['edit_mode'] = 'weight'
        return ADM_EDIT_FIELD

async def admin_edit_field_handler(u, c):
    val = clean_number(u.message.text)
    mode = c.user_data.get('edit_mode')
    if mode == 'rate': c.user_data['final_rate'] = val
    elif mode == 'weight': c.user_data['adm_w'] = val
    # Возврат к превью
    return await admin_v_preview(u, c)

async def admin_fin(u, c):
    d = c.user_data
    rate = d['final_rate']
    contract_num = f"CN-{int(time.time())}"
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO shipments (contract_num, fio, phone, client_city, warehouse_code, product, declared_weight, declared_volume, agreed_rate, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'оформлен',NOW())", 
                    (contract_num, d['adm_name'], d['adm_phone'], d['adm_city'], d['adm_wh'], d['adm_prod'], d['adm_w'], d['adm_vol'], rate))
        conn.commit(); conn.close()
        
    if MAKE_CONTRACT_WEBHOOK:
        try: requests.post(MAKE_CONTRACT_WEBHOOK, json={"action":"create","contract_num":contract_num,"chat_id":ADMIN_CHAT_ID,"fio":d['adm_name'],"phone":d['adm_phone'],"warehouse_code":d['adm_wh'],"product":d['adm_prod'],"declared_weight":d['adm_w'],"declared_volume":d['adm_vol'],"rate":rate,"created_at":str(datetime.now())}, timeout=5)
        except: pass
        
    await u.message.reply_text(f"✅ <b>Контракт {contract_num} создан!</b>", parse_mode='HTML')
    # Возврат в админ меню не нужен, просто завершаем
    return ConversationHandler.END

# --- SETUP ---
def setup_application():
    app = Application.builder().token(TOKEN).build()
    stop_filter = filters.Regex('^🚚 Калькулятор$') | filters.Regex('^🔎 Отследить груз$')
    
    client_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🚚 Калькулятор$'), calc_start)],
        states={
            CLIENT_CITY: [MessageHandler(filters.TEXT & ~stop_filter, get_city)],
            CLIENT_WAREHOUSE: [MessageHandler(filters.TEXT & ~stop_filter, get_warehouse)],
            CLIENT_PRODUCT: [CallbackQueryHandler(save_category_choice, pattern='^cat_')],
            CLIENT_WEIGHT: [MessageHandler(filters.TEXT & ~stop_filter, get_weight)],
            CLIENT_VOLUME: [MessageHandler(filters.TEXT & ~stop_filter, get_volume)],
            CLIENT_ADD_MORE: [MessageHandler(filters.TEXT & ~stop_filter, handle_add_more)],
            CLIENT_DECISION: [MessageHandler(filters.TEXT & ~stop_filter, client_decision)],
            CLIENT_NAME: [MessageHandler(filters.TEXT & ~stop_filter, client_get_name)],
            CLIENT_PHONE: [MessageHandler(filters.CONTACT | filters.TEXT & ~stop_filter, client_finish)]
        },
        fallbacks=[
            MessageHandler(filters.Regex('^🚚 Калькулятор$'), restart_calc),
            MessageHandler(filters.Regex('^🔎 Отследить груз$'), restart_track),
            CommandHandler('cancel', cancel)
        ]
    )
    
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^📝 Создать контракт$'), admin_create_manual),
            CallbackQueryHandler(admin_auto_start, pattern='^admin_auto_create$')
        ],
        states={
            ADM_NAME: [MessageHandler(filters.TEXT, admin_name)],
            ADM_PHONE: [MessageHandler(filters.TEXT, admin_phone)],
            ADM_CITY: [MessageHandler(filters.TEXT, admin_city)],
            ADM_WAREHOUSE: [MessageHandler(filters.TEXT, admin_wh)],
            ADM_PRODUCT: [MessageHandler(filters.TEXT, admin_prod)],
            ADM_WEIGHT: [MessageHandler(filters.TEXT, admin_w)],
            ADM_VOLUME: [MessageHandler(filters.TEXT, admin_v_preview)], # Сразу в превью
            ADM_CONFIRM: [CallbackQueryHandler(admin_confirm_handler)],
            ADM_EDIT_FIELD: [MessageHandler(filters.TEXT, admin_edit_field_handler)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('admin', admin_start))
    app.add_handler(MessageHandler(filters.Regex('^ℹ️ О компании$'), info_company))
    app.add_handler(MessageHandler(filters.Regex('^🗣 Живой чат$'), live_chat))
    app.add_handler(client_conv)
    app.add_handler(admin_conv)
    app.add_handler(MessageHandler(filters.Regex('^🔎 Отследить груз$'), lambda u,c: u.message.reply_text("Уважаемый клиент, введите трэк номер:")))
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z0-9-]{5,}$'), track_cargo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_chat))
    
    return app

if __name__ == '__main__':
    try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    except: pass
    if not TOKEN: logger.error("NO TOKEN")
    else:
        app = setup_application()
        app.run_polling()