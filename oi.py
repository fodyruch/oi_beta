import asyncio
import aiohttp
import hmac
import hashlib
import time
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import TimedOut, NetworkError

class BybitOITracker:
    def __init__(self, api_key: str, api_secret: str, telegram_token: str, admin_chat_id: int):
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_token = telegram_token
        self.admin_chat_id = admin_chat_id  # Только один пользователь
        self.base_url = "https://api.bybit.com"  # Убраны пробелы
        
        # Файл для сохранения данных
        self.data_file = "bot_data.json"
        
        # Настройки пользователя (по умолчанию)
        self.settings = {
            "oi_threshold_percent": 3.0,
            "time_window_minutes": 15,
            "monitored_symbols": set(),
            "enabled": True
        }
        
        # Хранилище данных
        self.oi_history: Dict[str, List[Dict]] = {}
        self.price_history: Dict[str, List[Dict]] = {}
        self.daily_alerts: Dict[str, List[Dict]] = {}  # Алерты по символам
        self.last_alert_messages: Dict[str, int] = {}  # {symbol: message_id}
        self.last_reset = datetime.now().date()
        self.last_alert_time: Dict[str, float] = {}  # {symbol: timestamp} для cooldown
        
        # Telegram
        self.bot_app = None
        
        # Загрузка сохраненных данных
        self.load_data()
    
    def save_data(self):
        """Сохранение настроек пользователя"""
        data = {
            "settings": {
                "oi_threshold_percent": self.settings["oi_threshold_percent"],
                "time_window_minutes": self.settings["time_window_minutes"],
                "monitored_symbols": list(self.settings["monitored_symbols"]),
                "enabled": self.settings["enabled"]
            }
        }
        
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[SAVE] Настройки сохранены: порог={self.settings['oi_threshold_percent']}%, окно={self.settings['time_window_minutes']}мин")
        except Exception as e:
            print(f"[ERROR] Ошибка сохранения данных: {e}")
    
    def load_data(self):
        """Загрузка настроек пользователя"""
        if not os.path.exists(self.data_file):
            print("[INFO] Файл настроек не найден, используются значения по умолчанию")
            return
        
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            saved_settings = data.get("settings", {})
            self.settings["oi_threshold_percent"] = saved_settings.get("oi_threshold_percent", 3.0)
            self.settings["time_window_minutes"] = saved_settings.get("time_window_minutes", 15)
            self.settings["monitored_symbols"] = set(saved_settings.get("monitored_symbols", []))
            self.settings["enabled"] = saved_settings.get("enabled", True)
            
            print(f"[LOAD] Настройки загружены: порог={self.settings['oi_threshold_percent']}%, окно={self.settings['time_window_minutes']}мин")
        except Exception as e:
            print(f"[ERROR] Ошибка загрузки данных: {e}")
    
    def _generate_signature(self, params: dict) -> str:
        """Генерация подписи для Bybit API"""
        param_str = ""
        for key in sorted(params.keys()):
            param_str += f"{key}={params[key]}&"
        param_str = param_str[:-1]
        
        signature = hmac.new(
            bytes(self.api_secret, "utf-8"),
            bytes(param_str, "utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def get_coinglass_url(self, symbol: str) -> str:
        """Получение ссылки на Coinglass"""
        coin = symbol.replace("USDT", "").replace("PERP", "")
        return f"https://www.coinglass.com/tv/Bybit_{coin}USDT"  # Убраны пробелы
    
    async def get_all_symbols(self, session: aiohttp.ClientSession) -> List[str]:
        """Получение всех USDT перпетуальных контрактов"""
        url = f"{self.base_url}/v5/market/instruments-info"
        params = {
            "category": "linear"
        }
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    data = await response.json()
                    if data.get("retCode") == 0:
                        symbols = [
                            item["symbol"] 
                            for item in data["result"]["list"]
                            if item["quoteCoin"] == "USDT" and item["status"] == "Trading"
                        ]
                        return symbols
                    else:
                        print(f"[ERROR] Ошибка получения символов: {data.get('retMsg')}")
                        return []
            except asyncio.TimeoutError:
                print(f"[WARN] Timeout при получении символов (попытка {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"[ERROR] Ошибка при запросе символов: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        
        return []
    
    async def get_open_interest(self, session: aiohttp.ClientSession, symbol: str) -> Dict:
        """Получение текущего Open Interest для символа"""
        url = f"{self.base_url}/v5/market/open-interest"
        params = {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": "5min"
        }
        
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                data = await response.json()
                if data.get("retCode") == 0 and data["result"]["list"]:
                    latest = data["result"]["list"][0]
                    return {
                        "symbol": symbol,
                        "openInterest": float(latest["openInterest"]),
                        "timestamp": int(latest["timestamp"]) / 1000,
                        "datetime": datetime.fromtimestamp(int(latest["timestamp"]) / 1000)
                    }
                return None
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            return None
    
    async def get_price_data(self, session: aiohttp.ClientSession, symbol: str) -> Dict:
        """Получение текущей цены для символа"""
        url = f"{self.base_url}/v5/market/tickers"
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                data = await response.json()
                if data.get("retCode") == 0 and data["result"]["list"]:
                    ticker = data["result"]["list"][0]
                    return {
                        "symbol": symbol,
                        "price": float(ticker["lastPrice"]),
                        "timestamp": time.time(),
                        "datetime": datetime.now()
                    }
                return None
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            return None
    
    def calculate_oi_change(self, symbol: str, current_oi: float, current_time: datetime) -> Dict:
        """Расчет изменения OI за заданный период (используем настройки пользователя)"""
        if symbol not in self.oi_history:
            return None
        
        # Используем персональные настройки времени
        time_window = self.settings["time_window_minutes"]
        cutoff_time = current_time - timedelta(minutes=time_window)
        
        # Фильтруем историю по временному окну
        historical_data = [
            entry for entry in self.oi_history[symbol]
            if entry["datetime"] >= cutoff_time
        ]
        
        if len(historical_data) < 2:
            return None
        
        # Берем самое раннее значение в окне
        oldest_oi = historical_data[0]["openInterest"]
        
        if oldest_oi == 0:
            return None
        
        change_percent = ((current_oi - oldest_oi) / oldest_oi) * 100
        
        return {
            "symbol": symbol,
            "change_percent": change_percent,
            "old_oi": oldest_oi,
            "new_oi": current_oi,
            "time_window": time_window,
            "timestamp": current_time
        }
    
    def calculate_price_change(self, symbol: str, current_price: float, current_time: datetime) -> Dict:
        """Расчет изменения цены за заданный период (используем настройки пользователя)"""
        if symbol not in self.price_history:
            return None
        
        # Используем персональные настройки времени
        time_window = self.settings["time_window_minutes"]
        cutoff_time = current_time - timedelta(minutes=time_window)
        
        # Фильтруем историю по временному окну
        historical_data = [
            entry for entry in self.price_history[symbol]
            if entry["datetime"] >= cutoff_time
        ]
        
        if len(historical_data) < 2:
            return None
        
        # Берем самое раннее значение в окне
        oldest_price = historical_data[0]["price"]
        
        if oldest_price == 0:
            return None
        
        price_change_percent = ((current_price - oldest_price) / oldest_price) * 100
        
        return {
            "price_change_percent": price_change_percent,
            "old_price": oldest_price,
            "new_price": current_price
        }
    
    def should_alert(self, symbol: str, change_percent: float) -> bool:
        """Проверка, нужно ли отправлять уведомление"""
        # Проверка порога
        if abs(change_percent) < self.settings["oi_threshold_percent"]:
            return False
        
        # Проверка лимита 5 алертов в день
        today = datetime.now().date()
        if today != self.last_reset:
            self.daily_alerts.clear()
            self.last_alert_messages.clear()
            self.last_alert_time.clear()
            self.last_reset = today
        
        if symbol not in self.daily_alerts:
            self.daily_alerts[symbol] = []
        
        if len(self.daily_alerts[symbol]) >= 5:
            return False
        
        # Cooldown 3 минуты между алертами на одну монету
        if symbol in self.last_alert_time:
            time_since_last = time.time() - self.last_alert_time[symbol]
            if time_since_last < 180:  # 3 минуты
                return False
        
        return True
    
    def get_alert_number(self, symbol: str) -> int:
        """Получение номера уведомления для символа за день"""
        if symbol not in self.daily_alerts:
            return 1
        return len(self.daily_alerts[symbol]) + 1
    
    def register_alert(self, symbol: str, alert_data: Dict):
        """Регистрация отправленного уведомления"""
        if symbol not in self.daily_alerts:
            self.daily_alerts[symbol] = []
        
        self.daily_alerts[symbol].append({
            "timestamp": datetime.now(),
            "data": alert_data
        })
        self.last_alert_time[symbol] = time.time()
    
    def format_alert(self, change_data: Dict, alert_number: int, price_data: Dict = None) -> str:
        """Форматирование уведомления для Telegram"""
        direction = "📈 РОСТ" if change_data["change_percent"] > 0 else "📉 ПАДЕНИЕ"
        emoji = "🟢" if change_data["change_percent"] > 0 else "🔴"
        
        coinglass_url = self.get_coinglass_url(change_data['symbol'])
        current_time = datetime.now()
        
        # Базовая информация
        alert = f"""
{emoji} <b>{direction} OPEN INTEREST</b> {emoji}

🪙 <b>Монета:</b> {change_data['symbol']}
📊 <b>Изменение OI:</b> {change_data['change_percent']:+.2f}%
⏱ <b>Период:</b> {change_data['time_window']} минут

📉 <b>OI было:</b> {change_data['old_oi']:,.2f}
📈 <b>OI стало:</b> {change_data['new_oi']:,.2f}
🔢 <b>Разница OI:</b> {change_data['new_oi'] - change_data['old_oi']:+,.2f}
"""
        
        # Добавляем информацию о цене если есть
        if price_data:
            price_emoji = "🟢" if price_data["price_change_percent"] > 0 else "🔴"
            alert += f"""
{price_emoji} <b>Изменение цены:</b> {price_data['price_change_percent']:+.2f}%
💰 <b>Цена была:</b> ${price_data['old_price']:,.4f}
💰 <b>Цена стала:</b> ${price_data['new_price']:,.4f}
"""
        
        alert += f"""
🔔 <b>Уведомление #{alert_number}</b> за сегодня
🕐 <b>Время:</b> {current_time.strftime('%H:%M:%S')}

🔗 <a href="{coinglass_url}">Открыть график на Coinglass</a>
"""
        return alert
    
    async def send_alert(self, alert_message: str, symbol: str):
        """Отправка уведомления с retry логикой"""
        if not self.settings["enabled"]:
            return
        
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # Получаем ID предыдущего сообщения для этой монеты
                reply_to_message_id = self.last_alert_messages.get(symbol)
                
                # Отправляем сообщение
                sent_message = await self.bot_app.bot.send_message(
                    chat_id=self.admin_chat_id,
                    text=alert_message,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_to_message_id=reply_to_message_id
                )
                
                # Сохраняем ID отправленного сообщения
                self.last_alert_messages[symbol] = sent_message.message_id
                
                print(f"[SEND] ✅ Алерт отправлен: {symbol}")
                return  # Успешно отправили
                
            except TimedOut:
                print(f"[WARN] ⏱ Timeout при отправке алерта {symbol} (попытка {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    print(f"[ERROR] ❌ Не удалось отправить алерт {symbol} после {max_retries} попыток")
                    
            except NetworkError as e:
                print(f"[WARN] 🌐 Network error при отправке {symbol} (попытка {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    print(f"[ERROR] ❌ Network error, не удалось отправить алерт {symbol}")
                    
            except Exception as e:
                print(f"[ERROR] ❌ Ошибка отправки алерта {symbol}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                else:
                    break
    
    async def update_oi_data(self, session: aiohttp.ClientSession, symbols: List[str]):
        """Обновление данных OI для всех символов"""
        # Ограничиваем количество одновременных запросов
        semaphore = asyncio.Semaphore(50)
        
        async def fetch_with_semaphore(coro):
            async with semaphore:
                return await coro
        
        # Получаем OI и цены параллельно с ограничением
        oi_tasks = [fetch_with_semaphore(self.get_open_interest(session, symbol)) for symbol in symbols]
        price_tasks = [fetch_with_semaphore(self.get_price_data(session, symbol)) for symbol in symbols]
        
        oi_results = await asyncio.gather(*oi_tasks, return_exceptions=True)
        price_results = await asyncio.gather(*price_tasks, return_exceptions=True)
        
        successful_updates = 0
        alerts_triggered = 0
        
        # Обрабатываем результаты
        for i, (oi_result, price_result) in enumerate(zip(oi_results, price_results)):
            symbol = symbols[i]
            
            # Пропускаем если исключение или None
            if isinstance(oi_result, Exception) or isinstance(price_result, Exception):
                continue
            
            if not oi_result or not price_result:
                continue
            
            successful_updates += 1
            
            # Инициализация истории для символа
            if symbol not in self.oi_history:
                self.oi_history[symbol] = []
            if symbol not in self.price_history:
                self.price_history[symbol] = []
            
            # Добавление новых данных
            self.oi_history[symbol].append(oi_result)
            self.price_history[symbol].append(price_result)
            
            # Очистка старых данных (храним данные за последние 2 часа)
            cutoff_time = oi_result["datetime"] - timedelta(hours=2)
            self.oi_history[symbol] = [
                entry for entry in self.oi_history[symbol]
                if entry["datetime"] >= cutoff_time
            ]
            self.price_history[symbol] = [
                entry for entry in self.price_history[symbol]
                if entry["datetime"] >= cutoff_time
            ]
            
            # Проверка изменений (только если есть данные и если отслеживается монета)
            if self.settings["monitored_symbols"] and symbol not in self.settings["monitored_symbols"]:
                continue
            
            if len(self.oi_history[symbol]) < 2 or len(self.price_history[symbol]) < 2:
                continue
            
            oi_change_data = self.calculate_oi_change(
                symbol,
                oi_result["openInterest"],
                oi_result["datetime"]
            )
            
            price_change_data = self.calculate_price_change(
                symbol,
                price_result["price"],
                price_result["datetime"]
            )
            
            if oi_change_data and self.should_alert(symbol, oi_change_data["change_percent"]):
                alert_number = self.get_alert_number(symbol)
                print(f"[ALERT] 🔔 Триггер алерта для {symbol}: {oi_change_data['change_percent']:+.2f}% (порог: {self.settings['oi_threshold_percent']}%)")
                
                alert_message = self.format_alert(oi_change_data, alert_number, price_change_data)
                await self.send_alert(alert_message, symbol)
                self.register_alert(symbol, oi_change_data)
                alerts_triggered += 1
        
        status = "✅ Активен" if self.settings["enabled"] else "⏸ Пауза"
        monitored = "Все монеты" if not self.settings["monitored_symbols"] else f"{len(self.settings['monitored_symbols'])} монет"
        print(f"[INFO] {datetime.now().strftime('%H:%M:%S')} | Обновлено: {successful_updates}/{len(symbols)} | "
              f"Алертов: {alerts_triggered} | Статус: {status} | Отслеживается: {monitored}")
    
    async def safe_reply(self, update_or_query, text: str, **kwargs):
        """Безопасная отправка ответа с retry логикой"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Проверяем что передано - Update или CallbackQuery
                if hasattr(update_or_query, 'message'):
                    # Это Update
                    await update_or_query.message.reply_text(text, **kwargs)
                else:
                    # Это CallbackQuery
                    await update_or_query.message.reply_text(text, **kwargs)
                return
            except (TimedOut, NetworkError):
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                else:
                    print(f"[ERROR] Не удалось отправить ответ после {max_retries} попыток")
            except Exception as e:
                print(f"[ERROR] Ошибка отправки ответа: {e}")
                break
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        chat_id = update.effective_chat.id
        
        # Проверка что это админ
        if chat_id != self.admin_chat_id:
            await update.message.reply_text("❌ Доступ запрещен. Бот работает только для владельца.")
            return
        
        self.settings["enabled"] = True
        self.save_data()
        
        welcome_message = f"""
👋 <b>Добро пожаловать в Bybit OI Tracker!</b>

Бот отслеживает изменения Open Interest на Bybit и присылает уведомления.

📊 <b>Ваши текущие настройки:</b>
• Порог изменения: {self.settings['oi_threshold_percent']}%
• Временное окно: {self.settings['time_window_minutes']} минут
• Максимум алертов в день: 5 на монету
• Cooldown между алертами: 3 минуты
• Отслеживаемые монеты: {'Все' if not self.settings['monitored_symbols'] else len(self.settings['monitored_symbols'])}

<b>Доступные команды:</b>
/settings - Изменить настройки
/status - Текущий статус
/test - Отправить тестовое уведомление
/stop - Остановить уведомления
/start - Возобновить уведомления
"""
        
        await self.safe_reply(update, welcome_message, parse_mode='HTML')
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /settings"""
        chat_id = update.effective_chat.id
        if chat_id != self.admin_chat_id:
            return
        
        keyboard = [
            [InlineKeyboardButton("📊 Изменить порог (%)", callback_data="set_threshold")],
            [InlineKeyboardButton("⏱ Изменить период (мин)", callback_data="set_time")],
            [InlineKeyboardButton("🪙 Управление монетами", callback_data="set_coins")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await self.safe_reply(
            update,
            "⚙️ <b>Настройки бота</b>\n\nВыберите параметр для изменения:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /status"""
        chat_id = update.effective_chat.id
        if chat_id != self.admin_chat_id:
            return
        
        status = "✅ Включен" if self.settings["enabled"] else "❌ Выключен"
        coins = "Все монеты" if not self.settings["monitored_symbols"] else f"{len(self.settings['monitored_symbols'])} монет"
        
        # Статистика алертов за сегодня
        total_alerts_today = sum(len(alerts) for alerts in self.daily_alerts.values())
        
        status_message = f"""
📊 <b>Текущий статус бота</b>

🔔 Статус: {status}
📈 Порог изменения: {self.settings['oi_threshold_percent']}%
⏱ Временное окно: {self.settings['time_window_minutes']} минут
🪙 Отслеживается: {coins}
📢 Уведомлений сегодня: {total_alerts_today}

<b>Монеты с алертами сегодня:</b>
"""
        
        if self.daily_alerts:
            for symbol, alerts in sorted(self.daily_alerts.items(), key=lambda x: len(x[1]), reverse=True):
                status_message += f"• {symbol}: {len(alerts)}/5\n"
        else:
            status_message += "Пока нет алертов\n"
        
        await self.safe_reply(update, status_message, parse_mode='HTML')
    
    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop"""
        chat_id = update.effective_chat.id
        if chat_id != self.admin_chat_id:
            return
        
        self.settings["enabled"] = False
        self.save_data()
        
        await self.safe_reply(
            update,
            "⏸ Уведомления остановлены.\nДля возобновления используйте /start"
        )
    
    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /test"""
        chat_id = update.effective_chat.id
        if chat_id != self.admin_chat_id:
            return
        
        test_change_data = {
            "symbol": "BTCUSDT",
            "change_percent": 5.23,
            "old_oi": 1234567.00,
            "new_oi": 1299135.00,
            "time_window": self.settings["time_window_minutes"],
            "timestamp": datetime.now()
        }
        
        test_price_data = {
            "price_change_percent": 2.15,
            "old_price": 45231.50,
            "new_price": 46203.75
        }
        
        test_message = self.format_alert(test_change_data, 1, test_price_data)
        
        # Отправка с retry логикой
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await update.message.reply_text(
                    "📬 Отправка тестового уведомления...\n\n" + test_message,
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
                return
            except (TimedOut, NetworkError) as e:
                if attempt < max_retries - 1:
                    print(f"[WARN] Timeout при отправке /test (попытка {attempt + 1}/{max_retries})")
                    await asyncio.sleep(2)
                else:
                    print(f"[ERROR] Не удалось отправить /test после {max_retries} попыток")
                    try:
                        await update.message.reply_text("❌ Ошибка отправки. Попробуйте еще раз.")
                    except:
                        pass
            except Exception as e:
                print(f"[ERROR] Ошибка в /test: {e}")
                break
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        chat_id = query.from_user.id
        
        if chat_id != self.admin_chat_id:
            await query.answer("❌ Доступ запрещен")
            return
        
        await query.answer()
        
        if query.data == "close":
            await query.message.delete()
        elif query.data == "set_threshold":
            await self.safe_reply(
                query,
                "📊 Введите новый порог изменения OI в процентах (например: 5):"
            )
            context.user_data["waiting_for"] = "threshold"
        elif query.data == "set_time":
            await self.safe_reply(
                query,
                "⏱ Введите новое временное окно в минутах (например: 15):"
            )
            context.user_data["waiting_for"] = "time"
        elif query.data == "set_coins":
            await self.safe_reply(
                query,
                "🪙 Отправьте список монет через запятую (например: BTCUSDT, ETHUSDT, SOLUSDT)\n"
                "Или отправьте 'все' для отслеживания всех монет."
            )
            context.user_data["waiting_for"] = "coins"
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        chat_id = update.effective_chat.id
        
        if chat_id != self.admin_chat_id:
            return
        
        text = update.message.text.strip()
        waiting_for = context.user_data.get("waiting_for")
        
        if waiting_for == "threshold":
            try:
                value = float(text)
                if 0 < value <= 100:
                    self.settings["oi_threshold_percent"] = value
                    self.save_data()
                    await self.safe_reply(update, f"✅ Порог изменения установлен: {value}%")
                else:
                    await self.safe_reply(update, "❌ Значение должно быть от 0 до 100")
            except ValueError:
                await self.safe_reply(update, "❌ Неверный формат. Введите число.")
            context.user_data.pop("waiting_for", None)
            
        elif waiting_for == "time":
            try:
                value = int(text)
                if 1 <= value <= 1440:
                    self.settings["time_window_minutes"] = value
                    self.save_data()
                    await self.safe_reply(update, f"✅ Временное окно установлено: {value} минут")
                else:
                    await self.safe_reply(update, "❌ Значение должно быть от 1 до 1440 минут")
            except ValueError:
                await self.safe_reply(update, "❌ Неверный формат. Введите число.")
            context.user_data.pop("waiting_for", None)
            
        elif waiting_for == "coins":
            if text.lower() == "все":
                self.settings["monitored_symbols"] = set()
                self.save_data()
                await self.safe_reply(update, "✅ Теперь отслеживаются все монеты")
            else:
                coins = [coin.strip().upper() for coin in text.split(",")]
                # Добавляем USDT если не указано
                coins = [coin if coin.endswith("USDT") else f"{coin}USDT" for coin in coins]
                self.settings["monitored_symbols"] = set(coins)
                self.save_data()
                await self.safe_reply(
                    update,
                    f"✅ Установлено отслеживание {len(coins)} монет:\n{', '.join(coins)}"
                )
            context.user_data.pop("waiting_for", None)
    
    async def monitoring_loop(self):
        """Основной цикл мониторинга"""
        print("🚀 Запуск мониторинга OI...")
        
        async with aiohttp.ClientSession() as session:
            # Получение списка всех символов
            print("📋 Получение списка торговых пар...")
            symbols = await self.get_all_symbols(session)
            
            if not symbols:
                print("❌ Не удалось получить список символов. Повтор через 60 секунд...")
                await asyncio.sleep(60)
                return await self.monitoring_loop()
            
            print(f"✅ Получено {len(symbols)} торговых пар\n")
            
            # Основной цикл
            while True:
                try:
                    await self.update_oi_data(session, symbols)
                    await asyncio.sleep(60)  # Проверка каждую минуту
                except Exception as e:
                    print(f"❌ Ошибка в цикле мониторинга: {e}")
                    await asyncio.sleep(60)
    
    async def run(self):
        """Запуск бота"""
        print("🤖 Запуск Telegram бота...")
        print(f"👤 Бот настроен только для пользователя: {self.admin_chat_id}")
        
        # Создание приложения
        self.bot_app = Application.builder().token(self.telegram_token).build()
        
        # Регистрация обработчиков
        self.bot_app.add_handler(CommandHandler("start", self.start_command))
        self.bot_app.add_handler(CommandHandler("settings", self.settings_command))
        self.bot_app.add_handler(CommandHandler("status", self.status_command))
        self.bot_app.add_handler(CommandHandler("stop", self.stop_command))
        self.bot_app.add_handler(CommandHandler("test", self.test_command))
        self.bot_app.add_handler(CallbackQueryHandler(self.button_callback))
        self.bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Запуск бота
        await self.bot_app.initialize()
        await self.bot_app.start()
        await self.bot_app.updater.start_polling()
        
        print("✅ Telegram бот запущен!")
        
        # Уведомляем пользователя о перезапуске
        try:
            await self.bot_app.bot.send_message(
                chat_id=self.admin_chat_id,
                text="🔄 Бот перезапущен и снова работает!\n\nВаши настройки сохранены ✅"
            )
        except Exception as e:
            print(f"[WARN] Не удалось отправить уведомление о запуске: {e}")
        
        print()
        
        # Запуск мониторинга
        await self.monitoring_loop()


async def main():
    # API ключи
    BYBIT_API_KEY = "AEpTFcr7Fvj2fPZYhf"
    BYBIT_API_SECRET = "vvjYDz7JRsf2aBhPIFuBEAob37O1W8NC7eHz"
    TELEGRAM_TOKEN = "8187121513:AAHnKKps-TTzvXcK08MeUFJcCil2C4_IB8I"
    
    # ВАЖНО: Укажи свой chat_id (узнать можно через @userinfobot)
    ADMIN_CHAT_ID = 725600839,  # ЗАМЕНИ НА СВОЙ CHAT_ID!
    
    # Создание и запуск трекера
    tracker = BybitOITracker(BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN, ADMIN_CHAT_ID)
    await tracker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Остановка бота...")
        print("До свидания!")