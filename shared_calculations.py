import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Загружаем конфиг
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
except Exception as e:
    print(f"❌ Ошибка загрузки config.json: {e}")
    CONFIG = {}

MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK')

def get_product_category_from_ai(text):
    """Универсальная функция определения категории для всех ботов"""
    if not MAKE_CATEGORIZER_WEBHOOK:
        return "obshhie"
    
    try:
        resp = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': text}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        category_key = data.get('category_key', 'obshhie')
        
        # Убеждаемся что ключ на английском
        english_keys = ['obuv', 'odezhda', 'sumki', 'mebel', 'elektronika', 
                       'telefony', 'tovary_dlja_doma', 'igrushki', 'avtozapchasti', 
                       'santehnika', 'oborudovanie', 'strojmaterialy', 
                       'tovary_dlja_zhivotnyh', 'obshhie']
        
        return category_key if category_key in english_keys else "obshhie"
        
    except Exception as e:
        print(f"❌ AI Category Error: {e}")
        return "obshhie"

def universal_t1_calculation(weight, volume, category_key, warehouse="GZ"):
    """Универсальный расчет T1 для всех ботов"""
    try:
        if volume <= 0:
            volume = weight / 200  # объем по умолчанию
            
        density = weight / volume if volume > 0 else 9999.0
        
        # Получаем тарифы для склада, если нет - берем GZ
        rates = CONFIG.get('T1_RATES_DENSITY', {}).get(warehouse, CONFIG.get('T1_RATES_DENSITY', {}).get('GZ', {}))
        cat_rates = rates.get(category_key, rates.get('obshhie', []))
        
        if not cat_rates:
            return {'cost_usd': 0, 'rate': 0, 'unit': 'kg', 'density': density}
        
        base_price = 0
        unit = 'kg'
        
        # Ищем подходящий тариф по плотности
        for rate in sorted(cat_rates, key=lambda x: x.get('min_density', 0), reverse=True):
            if density >= rate.get('min_density', 0):
                base_price = rate.get('price', 0)
                unit = rate.get('unit', 'kg')
                break
        
        # Если не нашли - берем последний (минимальная плотность)
        if base_price == 0 and cat_rates:
            base_price = cat_rates[-1].get('price', 0)
            unit = cat_rates[-1].get('unit', 'kg')
        
        # Наценка 30%
        client_rate = base_price * 1.30
        cost = client_rate * (volume if unit == 'm3' else weight)
        
        return {
            'cost_usd': round(cost, 2),
            'rate': round(client_rate, 2),
            'unit': unit,
            'density': round(density, 2)
        }
    except Exception as e:
        print(f"❌ T1 Calculation Error: {e}")
        return {'cost_usd': 0, 'rate': 0, 'unit': 'kg', 'density': 0}

def universal_t2_calculation(weight, city):
    """Универсальный расчет T2 для всех ботов"""
    try:
        city_key = city.lower().strip()
        zone = CONFIG.get('DESTINATION_ZONES', {}).get(city_key, "5")
        zone = str(zone)
        
        t2_config = CONFIG.get('T2_RATES_DETAILED', {}).get('large_parcel', {})
        weight_ranges = t2_config.get('weight_ranges', [])
        extra_kg_rate = t2_config.get('extra_kg_rate', {}).get(zone, 260)
        
        if weight <= 0:
            return 0, 0.8
            
        # Поиск в диапазонах веса
        final_cost = 0
        found = False
        for range_data in weight_ranges:
            if weight <= range_data['max']:
                final_cost = range_data['zones'].get(zone, 5000)
                found = True
                break
        
        # Если вес больше максимального диапазона
        if not found and weight_ranges:
            last_range = weight_ranges[-1]
            base_cost = last_range['zones'].get(zone, 5000)
            extra_weight = weight - 20
            final_cost = base_cost + (extra_weight * extra_kg_rate)
        
        ref_rate_usd = {"1": 0.4, "2": 0.5, "3": 0.6, "4": 0.7, "5": 0.8}.get(zone, 0.8)
        
        return int(final_cost), ref_rate_usd
    except Exception as e:
        print(f"❌ T2 Calculation Error: {e}")
        return 0, 0.8