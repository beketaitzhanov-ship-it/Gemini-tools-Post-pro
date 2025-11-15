# Вставь это вместо старой функции generate_contract

async def generate_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'generate_no':
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    
    await query.edit_message_text("⏳ **Печатаю...**")
    
    # Генерируем номер
    contract_num = f"CN-{datetime.now().strftime('%m%d%H')}"
    
    # 👇 БЕРЕМ ДАННЫЕ ИЗ CONTEXT (АРГУМЕНТ ФУНКЦИИ)
    data = context.user_data 
    
    # Рассчитываем плотность для записи (если объем > 0)
    w = float(data.get('c_weight', 0))
    v = float(data.get('c_volume', 0))
    density = round(w / v, 2) if v > 0 else 0

    payload = {
        "contract_num": contract_num,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "client_name": data.get('c_name'),
        "client_phone": data.get('c_phone'),
        "city": data.get('c_city'),
        "cargo_name": data.get('c_cargo'),
        "weight": str(w),
        "volume": str(v),
        "density": density,
        "rate": str(data.get('final_rate')),
        "total_sum": f"{data.get('final_total')} (Предварительно)",
        "clean_total": data.get('final_total'), # Число для базы
        "additional_services": "По факту / Upon arrival",
        "manager_id": query.from_user.id
    }
    
    # 1. В БАЗУ
    save_contract_to_db(payload)
    
    # 2. В MAKE
    try:
        requests.post(MAKE_CONTRACT_WEBHOOK, json=payload)
        await query.message.reply_text(f"✅ **Договор {contract_num} создан!**\nСохранен в базу.")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Ошибка Make: {e}")

    return ConversationHandler.END
