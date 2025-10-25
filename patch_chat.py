import re

# Читаем исходный файл
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Находим и заменяем блок обработки коробок
old_chat_code = '''        # Проверка на множественные коробки
        boxes = extract_boxes_from_message(user_message)
        if boxes and len(boxes) > 1:
            total_weight = sum(box['weight'] for box in boxes)
            session['multiple_boxes'] = boxes
            
            boxes_list = "\\n".join([f" {i+1}. {box['weight']} кг" for i, box in enumerate(boxes)])
            
            response = f"""
 **Обнаружено несколько коробок:**
{boxes_list}

 **Общий вес:** {total_weight} кг

 **Для расчета укажите:**
 Город доставки
 Тип товара  
 Габариты коробок

 **Пример:** "в Астану, одежда, коробки 604030 см"
            """
            return jsonify({"response": response})'''

new_chat_code = '''        # Проверка на множественные коробки
        boxes = extract_boxes_from_message(user_message)
        if boxes and len(boxes) > 1:
            total_weight = sum(box['weight'] for box in boxes)
            session['multiple_boxes'] = boxes
            
            boxes_list = "\\n".join([f" {i+1}. {box['weight']} кг" for i, box in enumerate(boxes)])
            
            response = f"""
 **Обнаружено несколько коробок:**
{boxes_list}

 **Общий вес:** {total_weight} кг

 **Для расчета укажите:**
 Город доставки
 Тип товара  
 Габариты коробок

 **Пример:** "в Астану, одежда, коробки 604030 см"
            """
            return jsonify({"response": response})

        # Проверка на паллеты
        pallets = extract_pallets_from_message(user_message)
        if pallets:
            total_weight = sum(pallet['weight'] for pallet in pallets)
            total_volume = sum(pallet['volume'] for pallet in pallets)
            
            response = f"""
 **Обнаружены паллеты:**
 Количество: {len(pallets)} шт
 Общий вес: {total_weight} кг  
 Общий объем: {total_volume:.1f} м

 **Для точного расчета укажите:**
 Город доставки
 Тип товара на паллетах

 **Пример:** "в Караганду, мебель на паллетах"
            """
            return jsonify({"response": response})'''

content = content.replace(old_chat_code, new_chat_code)

# Записываем измененный файл
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(" Обработка паллет добавлена в функцию chat")
