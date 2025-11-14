import os
import psycopg2
import logging
from dotenv import load_dotenv

# Загружаем настройки
load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

logger = logging.getLogger(__name__)

class LogisticsCalculator:
    def __init__(self, db_url=None):
        self.db_url = db_url or DATABASE_URL

    def get_connection(self):
        try:
            return psycopg2.connect(self.db_url)
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД в калькуляторе: {e}")
            return None

    def get_exchange_rate(self):
        """Получает текущий курс валют из БД"""
        conn = self.get_connection()
        if not conn: return 550.0 # Запасной вариант
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = 'exchange_rate'")
            row = cur.fetchone()
            return float(row[0]) if row else 550.0
        finally:
            conn.close()

    def find_zone(self, city_name):
        """Определяет зону доставки"""
        if not city_name: return "5"
        city_lower = city_name.lower().strip()
        
        conn = self.get_connection()
        if not conn: return "5"
        try:
            cur = conn.cursor()
            cur.execute("SELECT zone FROM cities WHERE city_name = %s", (city_lower,))
            row = cur.fetchone()
            if row: return row[0]
            
            cur.execute("SELECT zone FROM cities WHERE %s LIKE '%%' || city_name || '%%'", (city_lower,))
            row = cur.fetchone()
            return row[0] if row else "5"
        finally:
            conn.close()

    def get_t1_cost(self, weight, volume, category_name="общие"):
        """
        Считает БАЗОВУЮ стоимость Китай-Алматы (без наценки).
        Возвращает: (Сумма_USD, Тариф_USD, Плотность)
        """
        conn = self.get_connection()
        if not conn: return 0, 0, 0
        
        try:
            cur = conn.cursor()
            density = weight / volume if volume > 0 else 0
            
            # Ищем тариф
            query = """
                SELECT price, unit FROM t1_rates 
                WHERE category_name = %s AND min_density <= %s 
                ORDER BY min_density DESC LIMIT 1
            """
            cur.execute(query, (category_name, density))
            row = cur.fetchone()
            
            # Если не нашли, ищем в общих
            if not row:
                cur.execute(query, ("общие", density))
                row = cur.fetchone()

            if not row:
                return 0, 0, density

            price, unit = row
            cost_usd = price * volume if unit == 'm3' else price * weight
                
            return cost_usd, price, density
        finally:
            conn.close()

    def get_t2_cost(self, weight, zone):
        """Считает доставку по Казахстану (Базовая цена)"""
        conn = self.get_connection()
        if not conn: return 0
        
        try:
            if zone == 'алматы': return weight * 250 

            cur = conn.cursor()
            
            # 1. Поиск по весовым диапазонам
            zone_column = f"zone_{zone}_cost"
            if zone not in ['1', '2', '3', '4', '5']: zone = '5'
            
            cur.execute(f"SELECT {zone_column}, max_weight FROM t2_rates WHERE max_weight >= %s ORDER BY max_weight ASC LIMIT 1", (weight,))
            row = cur.fetchone()
            
            if row: return float(row[0])
            
            # 2. Если вес большой, считаем доплату
            cur.execute(f"SELECT {zone_column}, max_weight FROM t2_rates ORDER BY max_weight DESC LIMIT 1")
            max_row = cur.fetchone()
            
            if max_row:
                base_cost = float(max_row[0])
                max_w = float(max_row[1])
                extra_weight = weight - max_w
                
                cur.execute("SELECT extra_kg_rate FROM t2_rates_extra WHERE zone = %s", (zone,))
                extra_row = cur.fetchone()
                extra_rate = float(extra_row[0]) if extra_row else 300
                
                return base_cost + (extra_weight * extra_rate)
            
            return weight * 300
        except Exception as e:
            logger.error(f"Ошибка расчета T2: {e}")
            return weight * 300
        finally:
            conn.close()

    def calculate_all(self, weight, volume, product_type, city):
        """
        ГЛАВНАЯ ФУНКЦИЯ (С НАЦЕНКАМИ)
        Используется и Айсулу, и Админом.
        """
        rate_kzt = self.get_exchange_rate()
        
        # 1. Расчет T1 (Китай) + НАЦЕНКА 30%
        raw_t1_usd, raw_rate, density = self.get_t1_cost(weight, volume, product_type)
        
        markup_t1 = 1.30  # 👈 Твоя наценка 30%
        
        # Клиент увидит уже увеличенную цену и тариф
        client_t1_usd = raw_t1_usd * markup_t1
        client_rate = raw_rate * markup_t1  
        
        t1_kzt = client_t1_usd * rate_kzt
        
        # 2. Расчет T2 (Казахстан) + НАЦЕНКА 20%
        zone = self.find_zone(city)
        raw_t2_kzt = self.get_t2_cost(weight, zone)
        
        markup_t2 = 1.20 # 👈 Твоя наценка 20%
        client_t2_kzt = raw_t2_kzt * markup_t2
        
        # 3. Итоговая сумма
        total_kzt = t1_kzt + client_t2_kzt
        total_usd = total_kzt / rate_kzt

        return {
            "success": True,
            "weight": weight,
            "volume": volume,
            "density": round(density, 2),
            "tariff_rate": round(client_rate, 2), # Клиент видит тариф уже с наценкой
            "t1_usd": round(client_t1_usd, 2),
            "t2_kzt": round(client_t2_kzt, 2),
            "total_kzt": round(total_kzt), # Округляем тенге до целого
            "total_usd": round(total_usd, 2),
            "zone": zone,
            "exchange_rate": rate_kzt
        }

# Тест (для проверки локально)
if __name__ == "__main__":
    calc = LogisticsCalculator()
    print("Тестовый расчет:")
    print(calc.calculate_all(100, 0.5, "одежда", "Алматы"))