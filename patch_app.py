import re

# Читаем исходный файл
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Добавляем первую функцию после extract_weight
old_code = '''def extract_weight(text):
    """Извлекает вес из текста"""
    patterns = [
        r'(\\d+(?:[.,]\\d+)?)\\s*(?:кг|kg|килограмм)',
        r'вес\\s*[:-]?\\s*(\\d+(?:[.,]\\d+)?)\\s*(?:кг)?',
        r'(\\d+(?:[.,]\\d+)?)\\s*(?:т|тонн|тонны)'
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                weight = float(match.group(1).replace(',', '.'))
                # Конвертация тонн в кг
                if 'т' in pattern or 'тонн' in pattern:
                    weight *= 1000
                return weight
            except:
                continue
    return None

def extract_city(text):'''

new_code = '''def extract_weight(text):
    """Извлекает вес из текста"""
    patterns = [
        r'(\\d+(?:[.,]\\d+)?)\\s*(?:кг|kg|килограмм)',
        r'вес\\s*[:-]?\\s*(\\d+(?:[.,]\\d+)?)\\s*(?:кг)?',
        r'(\\d+(?:[.,]\\d+)?)\\s*(?:т|тонн|тонны)'
    ]
    
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                weight = float(match.group(1).replace(',', '.'))
                # Конвертация тонн в кг
                if 'т' in pattern or 'тонн' in pattern:
                    weight *= 1000
                return weight
            except:
                continue
    return None

def extract_boxes_from_message(message):
    """Извлекает информацию о коробках"""
    boxes = []
    try:
        text_lower = message.lower()
        
        # Паттерн: "N коробок по X кг"
        pattern = r'(\\d+)\\s*(?:коробк|посылк|упаковк|шт|штук)\\w*\\s+по\\s+(\\d+(?:[.,]\\d+)?)\\s*кг'
        matches = re.findall(pattern, text_lower)
        
        for count, weight in matches:
            box_count = int(count)
            box_weight = float(weight.replace(',', '.'))
            
            for i in range(box_count):
                boxes.append({
                    'weight': box_weight,
                    'product_type': None,
                    'volume': None,
                    'description': f"Коробка {i+1}"
                })
        
        return boxes
    except Exception as e:
        logger.error(f"Ошибка извлечения коробок: {e}")
        return []

def extract_pallets_from_message(message):
    """Извлекает информацию о паллетах"""
    try:
        text_lower = message.lower()
        
        # Паттерн: "N паллет"
        pallet_match = re.search(r'(\\d+)\\s*паллет\\w*', text_lower)
        if pallet_match:
            pallet_count = int(pallet_match.group(1))
            
            # Стандартные параметры паллета
            STANDARD_PALLET = {
                'weight': 500,  # кг
                'volume': 1.2,  # м
                'description': 'Стандартная паллета'
            }
            
            pallets = []
            for i in range(pallet_count):
                pallets.append({
                    'weight': STANDARD_PALLET['weight'],
                    'volume': STANDARD_PALLET['volume'],
                    'product_type': 'мебель',  # по умолчанию для паллет
                    'description': f'Паллета {i+1}'
                })
            
            return pallets
        
        return []
    except Exception as e:
        logger.error(f"Ошибка извлечения паллет: {e}")
        return []

def extract_city(text):'''

content = content.replace(old_code, new_code)
print(" Функции добавлены")

# Записываем измененный файл
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(" Файл app.py обновлен")
