import requests
import os
from dotenv import load_dotenv

load_dotenv()

MAKE_CATEGORIZER_WEBHOOK = os.getenv('MAKE_CATEGORIZER_WEBHOOK')

def get_product_category_from_ai(text):
    """Универсальная функция определения категории для всех ботов"""
    if not MAKE_CATEGORIZER_WEBHOOK:
        print("⚠️ MAKE_CATEGORIZER_WEBHOOK не задан, используем категорию по умолчанию")
        return "obshhie"
    
    try:
        resp = requests.post(MAKE_CATEGORIZER_WEBHOOK, json={'product_text': text}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        category_key = data.get('category_key', 'obshhie')
        
        # Убеждаемся что ключ на английском
        english_keys = [
            'obuv', 'odezhda', 'sumki', 'mebel', 'elektronika', 
            'telefony', 'tovary_dlja_doma', 'igrushki', 'avtozapchasti', 
            'santehnika', 'oborudovanie', 'strojmaterialy', 
            'tovary_dlja_zhivotnyh', 'obshhie'
        ]
        
        if category_key in english_keys:
            print(f"✅ Определена категория: {category_key} для товара: {text}")
            return category_key
        else:
            print(f"⚠️ Неизвестная категория: {category_key}, используем obshhie")
            return "obshhie"
        
    except Exception as e:
        print(f"❌ Ошибка определения категории: {e}")
        return "obshhie"