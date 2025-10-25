import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")

def list_available_models():
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # Получаем список всех доступных моделей
        models = genai.list_models()
        
        print("=== ДОСТУПНЫЕ МОДЕЛИ В API ===")
        for model in models:
            if 'generateContent' in model.supported_generation_methods:
                print(f"✅ {model.name}")
                print(f"   Описание: {model.description}")
                print("---")
                
    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    list_available_models()

