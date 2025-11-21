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
MANAGER_WA_LINK = "https://wa.me/77000479530"
MANAGER_TG_LINK = "https://t.me/PostProLogistics"

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

# --- КАТЕГОРИИ ---
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

# ИСПРАВЛЕНО: Добавлены ADM_CONFIRM и ADM_EDIT_FIELD в список
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

async def track_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track = update.message.text.strip().upper()
    conn = get_db_connection()
    if not conn: return
    cur = conn.cursor()
    cur.execute("SELECT status, actual_weight, product, warehouse_code, client_city, route_progress FROM shipments WHERE track_number = %s OR contract_num = %s", (track, track))
    row = cur.fetchone()
    conn.close()
    if row:
        status, weight, product, wh_code, city, progress = row
        if not wh_code: wh_code = "GZ"
        if not city: city = "Алматы"
        progress = progress if progress is not None else 10
        visual = generate_vertical_map(status, progress, wh_code, city)
        await update.message.reply_text(f"📦 <b>ГРУЗ НАЙДЕН!</b>\n🆔 {track}\n📄 {product}\n⚖️ {weight} кг\n📍 <b>{status}</b>\n\n{visual}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Груз не найден. Проверьте трек.")

async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if re.match(r'^[A-Za-z0-9-]{5,}$', user_text) and len(user_text) < 20: return await track_cargo(update, context)
    if not MAKE_AI_CHAT_WEBHOOK or user_text in ["🚚 Калькулятор", "🔎 Отследить груз"]: return
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        resp = requests.post(MAKE_AI_CHAT_WEBHOOK, json={'text_message': user_text}, timeout=20)
        await update.message.reply_text(resp.text)
    except:
        await start(update, context)

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
        [InlineKeyboardButton("💬 WhatsApp", url=MANAGER_WA_LINK)],
        [InlineKeyboardButton("✈️ Telegram", url=MANAGER_TG_LINK)]
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

# --- КЛИЕНТ (КАЛЬКУЛЯТОР) ---

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
    
    await update.message.reply_text(
        "📦 <b>Введите Объем (м³)</b>\n"
        "<i>Или габариты: 60*40*50\n"
        "Или партию: 10 шт 60*40*50</i>", 
        parse_mode='HTML', reply_markup=MAIN_MENU
    )
    return CLIENT_VOLUME

async def get_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vol = parse_volume_input(update.message.text)
    if vol <= 0: 
        vol = context.user_data['current_item']['weight'] / 200
        await update.message.reply_text(f"⚠️ Габариты не распознаны. Расчетный объем: {vol:.2f} м³")
    
    context.user_data['current_item']['volume'] = vol
    context.user_data['cart'].append(context.user_data['current_item'])
    
    kb = [[KeyboardButton("➕ Добавить товар"), KeyboardButton("🏁 Рассчитать итог")]]
    await update.message.reply_text(
        f"✅ Товар добавлен! В корзине: {len(context.user_data['cart'])} поз.\nДобавим еще или считаем?", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return CLIENT_ADD_MORE

async def handle_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Добавить" in text:
        keyboard = []
        row = []
        for key, name in CATEGORY_BUTTONS.items():
            row.append(InlineKeyboardButton(name, callback_data=f"cat_{key}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
        
        await update.message.reply_text("📦 <b>Выберите следующую категорию:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return CLIENT_PRODUCT
        
    elif "Рассчитать" in text:
        return await show_final_report(update, context)
        
    return CLIENT_ADD_MORE

async def show_final_report(update, context):
    d = context.user_data
    total_w = sum(i['weight'] for i in d['cart'])
    total_v = sum(i['volume'] for i in d['cart'])
    
    t1_total_usd = 0
    items_details = ""
    
    for i, item in enumerate(d['cart'], 1):
        cost, rate, dens, is_cbm = calculate_t1_line_item(item['weight'], item['volume'], item['category'], d['wh_code'])
        t1_total_usd += cost
        unit = "м³" if is_cbm else "кг"
        items_details += (
            f"<b>{i}. {item['name']}</b>\n"
            f"   ▫️ {item['weight']} кг / {item['volume']:.2f} м³ (Плотн: {dens:.0f})\n"
            f"   ▫️ Тариф: ${rate}/{unit}\n"
            f"   ▫️ Сумма: <b>${cost:.2f}</b>\n\n"
        )

    t2_kzt, t2_rate_usd = calculate_t2_total(total_w, d['city'])
    
    report = (
        f"📊 <b>ДЕТАЛЬНЫЙ РАСЧЕТ | Заявка</b>\n\n"
        f"🏙 <b>Маршрут:</b> {d['wh_name']} ➡️ {d['city']}\n\n"
        f"📦 <b>СОСТАВ ГРУЗА:</b>\n"
        f"{items_details}"
        f"----------------------------------\n"
        f"🇨🇳 <b>Т1 (КИТАЙ → АЛМАТЫ)</b>\n"
        f"• Общий вес: <b>{total_w} кг</b>\n"
        f"• Общий объем: <b>{total_v:.2f} м³</b>\n"
        f"💵 <b>ИТОГО Т1: ${t1_total_usd:.2f} USD</b>\n\n"
        
        f"🇰🇿 <b>Т2 (АЛМАТЫ → ДВЕРЬ)</b>\n"
        f"• Тарифная зона: {d['city']}\n"
        f"💵 <b>ИТОГО Т2: ~{t2_kzt} ₸</b>\n\n"
        
        f"<i>Тариф по РК предварительный. Точный расчет — по прибытию в Алматы.</i>\n\n"
        f"💡 <b>Страхование:</b> 1% от стоимости товара.\n"
        f"💳 <b>Оплата:</b> При получении груза в тенге удобным Вам способом."
    )
    
    kb = [[KeyboardButton("✅ Оставить заявку"), KeyboardButton("🔄 Новый расчет")]]
    await update.message.reply_text(report, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode='HTML')
    return CLIENT_DECISION

async def client_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "Оставить" in update.message.text:
        await update.message.reply_text("👤 Как к вам обращаться? (Имя):", reply_markup=ReplyKeyboardRemove()); return CLIENT_NAME
    elif "Новый" in update.message.text:
        return await calc_start(update, context)
    return CLIENT_DECISION

async def client_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['client_name'] = update.message.text
    await update.message.reply_text("📱 Ваш телефон:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📱 Отправить контакт", request_contact=True)]], resize_keyboard=True)); return CLIENT_PHONE

async def client_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    d = context.user_data
    send_tiktok_event(phone)

    if ADMIN_CHAT_ID:
        items_short = ", ".join([f"{i['name']} ({i['weight']}кг)" for i in d['cart']])
        total_w = sum(i['weight'] for i in d['cart'])
        total_v = sum(i['volume'] for i in d['cart'])
        
        context.bot_data['last_lead'] = {
            'name': d['client_name'], 'phone': phone, 'city': d['city'],
            'wh': d['wh_code'], 'prod': d['cart'][0]['category'], 
            'w': total_w, 'v': total_v
        }
        
        kb = InlineKeyboardButton("⚡️ Оформить контракт (Авто)", callback_data="admin_auto_create")
        
        admin_text = (
            f"🔥 <b>НОВАЯ ЗАЯВКА (Айсулу)</b>\n"
            f"👤 <b>{d['client_name']}</b> ({phone})\n"
            f"🏙 {d['city']} | 🏭 {d['wh_name']}\n\n"
            f"📦 <b>ТОВАРЫ:</b>\n{items_short}\n"
            f"⚖️ <b>Всего:</b> {total_w} кг | {total_v:.2f} м³"
        )
        try: await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[kb]]))
        except: pass
        
    await update.message.reply_text("✅ Заявка принята! Менеджер скоро свяжется с вами.", reply_markup=MAIN_MENU); return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("Отмена.", reply_markup=MAIN_MENU); return ConversationHandler.END

# --- АДМИНКА (ПОЛНАЯ) ---

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
async def admin_wh(u, c): 
    c.user_data['adm_wh'] = u.message.text
    keyboard = []
    row = []
    for key, name in CATEGORY_BUTTONS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"adm_cat_{key}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    await u.message.reply_text("📦 <b>Товар:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return ADM_PRODUCT

async def admin_save_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_key = query.data.replace("adm_cat_", "")
    context.user_data['adm_prod'] = cat_key
    await query.edit_message_text(f"📦 Товар: <b>{cat_key}</b>\n⚖️ Вес:", parse_mode='HTML')
    return ADM_WEIGHT

async def admin_w(u, c): c.user_data['adm_w'] = clean_number(u.message.text); await u.message.reply_text("📦 Объем:"); return ADM_VOLUME

async def admin_v_preview(u, c): 
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
    else: await u.effective_message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
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
    return await admin_v_preview(u, c)

async def admin_fin(u, c):
    d = c.user_data
    rate = d['final_rate']
    contract_num = f"CN-{int(time.time())}"
    
    # 1. РАСЧЕТ ИТОГА
    total_price_usd = rate * d['adm_w']
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO shipments (
                contract_num, fio, phone, client_city, warehouse_code, 
                product, declared_weight, declared_volume, agreed_rate, 
                total_price_final, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'оформлен', NOW())
        """, (contract_num, d['adm_name'], d['adm_phone'], d['adm_city'], d['adm_wh'], d['adm_prod'], d['adm_w'], d['adm_vol'], rate, total_price_usd))
        conn.commit(); conn.close()
        
    if MAKE_CONTRACT_WEBHOOK:
        try: 
            requests.post(MAKE_CONTRACT_WEBHOOK, json={
                "action":"create",
                "contract_num":contract_num,
                "chat_id":u.effective_chat.id,
                "fio":d['adm_name'],
                "phone":d['adm_phone'],
                "warehouse_code":d['adm_wh'],
                "product":d['adm_prod'],
                "declared_weight":d['adm_w'],
                "declared_volume":d['adm_vol'],
                "rate":rate,
                "total_amount": total_price_usd,
                "created_at":str(datetime.now())
            }, timeout=5)
        except: pass
        
    await u.effective_message.reply_text(
        f"✅ <b>Контракт {contract_num} создан!</b>\n\n"
        f"👤 {d['adm_name']}\n"
        f"📦 {d['adm_prod']}\n"
        f"💰 Тариф: ${rate} | Итого: <b>${total_price_usd:.2f}</b>\n\n"
        f"<i>Данные отправлены в таблицу.</i>", 
        parse_mode='HTML'
    )
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
            MessageHandler(filters.Regex('^📝 Создать контракт'), admin_create_manual),
            CallbackQueryHandler(admin_auto_start, pattern='^admin_auto_create$')
        ],
        states={
            ADM_NAME: [MessageHandler(filters.TEXT, admin_name)],
            ADM_PHONE: [MessageHandler(filters.TEXT, admin_phone)],
            ADM_CITY: [MessageHandler(filters.TEXT, admin_city)],
            ADM_WAREHOUSE: [MessageHandler(filters.TEXT, admin_wh)],
            ADM_PRODUCT: [CallbackQueryHandler(admin_save_category_choice, pattern='^adm_cat_')],
            ADM_WEIGHT: [MessageHandler(filters.TEXT, admin_w)],
            ADM_VOLUME: [MessageHandler(filters.TEXT, admin_v_preview)], 
            ADM_CONFIRM: [CallbackQueryHandler(admin_confirm_handler)],
            ADM_EDIT_INPUT: [MessageHandler(filters.TEXT, admin_edit_field_handler)]
        },
        fallbacks=[CommandHandler('cancel', cancel), MessageHandler(filters.Regex('^🔙 Выход'), start)]
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
