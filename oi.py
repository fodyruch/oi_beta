import asyncio
import aiohttp
import hmac
import hashlib
import time
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Set
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

class BybitOITracker:
    def __init__(self, api_key: str, api_secret: str, telegram_token: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_token = telegram_token
        self.base_url = "https://api.bybit.com"
        
        # Файл для сохранения данных
        self.data_file = "bot_data.json"
        
        # Настройки пользователя (по умолчанию)
        self.user_settings = {}
        
        # Хранилище данных
        self.oi_history: Dict[str, List[Dict]] = {}
        self.price_history: Dict[str, List[Dict]] = {}
        self.daily_alerts: Dict[str, List[Dict]] = {}
        self.last_reset = datetime.now().date()
        
        # Telegram
        self.bot_app = None
        self.chat_ids: Set[int] = set()
        
        # Загрузка сохраненных данных
        self.load_data()
    
    def save_data(self):
        """Сохранение настроек пользователей и chat_ids"""
        data = {
            "chat_ids": list(self.chat_ids),
            "user_settings": {
                str(chat_id): {
                    "oi_threshold_percent": settings["oi_threshold_percent"],
                    "time_window_minutes": settings["time_window_minutes"],
                    "monitored_symbols": list(settings["monitored_symbols"]),
                    "enabled": settings["enabled"]
                }
                for chat_id, settings in self.user_settings.items()
            }
        }
        
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[SAVE] Данные сохранены: {len(self.chat_ids)} пользователей")
        except Exception as e:
            print(f"[ERROR] Ошибка сохранения данных: {e}")
    
    def load_data(self):
        """Загрузка настроек пользователей и chat_ids"""
        if not os.path.exists(self.data_file):
            print("[LOAD] Файл данных не найден, используются настройки по умолчанию")
            return
        
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.chat_ids = set(data.get("chat_ids", []))
            
            for chat_id_str, settings in data.get("user_settings", {}).items():
                chat_id = int(chat_id_str)
                self.user_settings[chat_id] = {
                    "oi_threshold_percent": settings["oi_threshold_percent"],
                    "time_window_minutes": settings["time_window_minutes"],
                    "monitored_symbols": set(settings["monitored_symbols"]),
                    "enabled": settings["enabled"]
                }
            
            print(f"[LOAD] ✅ Загружено {len(self.chat_ids)} пользователей")
        except Exception as e:
            print(f"[ERROR] Ошибка загрузки данных: {e}")
        
    def get_user_settings(self, chat_id: int) -> Dict:
        """Получение настроек пользователя"""
        if chat_id not in self.user_settings:
            self.user_settings[chat_id] = {
                "oi_threshold_percent": 3.0,
                "time_window_minutes": 15,
                "monitored_symbols": set(),
                "enabled": True
            }
        return self.user_settings[chat_id]
    
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
        # Убираем USDT из символа для Coinglass
        coin = symbol.replace("USDT", "").replace("PERP", "")
        return f"https://www.coinglass.com/tv/Bybit_{coin}USDT"
    
    async def get_all_symbols(self, session: aiohttp.ClientSession) -> List[str]:
        """Получение всех USDT перпетуальных контрактов"""
        url = f"{self.base_url}/v5/market/instruments-info"
        params = {
            "category": "linear"
        }
        
        try:
            async with session.get(url, params=params) as response:
                data = await response.json()
                if data.get("retCode") == 0:
                    symbols = [
                        item["symbol"] 
                        for item in data["result"]["list"]
                        if item["quoteCoin"] == "USDT" and item["status"] == "Trading"
                    ]
                    return symbols
                else:
                    print(f"Ошибка получения символов: {data.get('retMsg')}")
                    return []
        except Exception as e:
            print(f"Ошибка при запросе символов: {e}")
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
            async with session.get(url, params=params) as response:
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
        except Exception as e:
            print(f"Ошибка получения OI для {symbol}: {e}")
            return None
    
    async def get_price_data(self, session: aiohttp.ClientSession, symbol: str) -> Dict:
        """Получение текущей цены для символа"""
        url = f"{self.base_url}/v5/market/tickers"
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        try:
            async with session.get(url, params=params) as response:
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
        except Exception as e:
            print(f"Ошибка получения цены для {symbol}: {e}")
            return None
    
    def calculate_oi_change(self, symbol: str, current_oi: float, current_time: datetime, time_window: int) -> Dict:
        """Расчет изменения OI за заданный период"""
        if symbol not in self.oi_history:
            return None
        
        cutoff_time = current_time - timedelta(minutes=time_window)
        
        # Фильтруем историю по временному окну
        historical_data = [
            entry for entry in self.oi_history[symbol]
            if entry["datetime"] >= cutoff_time
        ]
        
        if not historical_data:
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
    
    def calculate_price_change(self, symbol: str, current_price: float, current_time: datetime, time_window: int) -> Dict:
        """Расчет изменения цены за заданный период"""
        if symbol not in self.price_history:
            return None
        
        cutoff_time = current_time - timedelta(minutes=time_window)
        
        # Фильтруем историю по временному окну
        historical_data = [
            entry for entry in self.price_history[symbol]
            if entry["datetime"] >= cutoff_time
        ]
        
        if not historical_data:
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
    
    def should_alert(self, change_percent: float, threshold: float) -> bool:
        """Проверка, нужно ли отправлять уведомление"""
        return abs(change_percent) >= threshold
    
    def get_alert_number(self, symbol: str) -> int:
        """Получение номера уведомления для символа за день"""
        today = datetime.now().date()
        
        if today != self.last_reset:
            self.daily_alerts.clear()
            self.last_reset = today
        
        if symbol not in self.daily_alerts:
            self.daily_alerts[symbol] = []
        
        # Ограничение до 5 уведомлений в день
        if len(self.daily_alerts[symbol]) >= 5:
            return -1  # Не отправляем больше уведомлений
        
        # Проверяем, не было ли уведомления в последние 2 минуты (защита от дублей)
        if self.daily_alerts[symbol]:
            last_alert_time = self.daily_alerts[symbol][-1]["timestamp"]
            if (datetime.now() - last_alert_time).total_seconds() < 120:
                return -1  # Слишком рано для нового уведомления
        
        return len(self.daily_alerts[symbol]) + 1
    
    def register_alert(self, symbol: str, alert_data: Dict):
        """Регистрация отправленного уведомления"""
        if symbol not in self.daily_alerts:
            self.daily_alerts[symbol] = []
        
        self.daily_alerts[symbol].append({
            "timestamp": datetime.now(),
            "data": alert_data
        })
    
    def format_alert(self, change_data: Dict, alert_number: int, price_data: Dict = None) -> str:
        """Форматирование уведомления для Telegram"""
        direction = "📈 РОСТ" if change_data["change_percent"] > 0 else "📉 ПАДЕНИЕ"
        emoji = "🟢" if change_data["change_percent"] > 0 else "🔴"
        
        coinglass_url = self.get_coinglass_url(change_data['symbol'])
        
        # Используем текущее время для уведомления
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
    
    async def send_alert_to_users(self, alert_message: str):
        """Отправка уведомления всем активным пользователям"""
        for chat_id in self.chat_ids:
            settings = self.get_user_settings(chat_id)
            if settings["enabled"]:
                try:
                    await self.bot_app.bot.send_message(
                        chat_id=chat_id,
                        text=alert_message,
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    print(f"Ошибка отправки сообщения пользователю {chat_id}: {e}")
    
    async def update_oi_data(self, session: aiohttp.ClientSession, symbols: List[str]):
        """Обновление данных OI для всех символов"""
        # Получаем OI и цены параллельно
        oi_tasks = [self.get_open_interest(session, symbol) for symbol in symbols]
        price_tasks = [self.get_price_data(session, symbol) for symbol in symbols]
        
        oi_results = await asyncio.gather(*oi_tasks)
        price_results = await asyncio.gather(*price_tasks)
        
        successful_updates = 0
        alerts_triggered = 0
        
        # Для отладки - показываем топ монеты
        debug_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        
        # Обрабатываем результаты
        for oi_result, price_result in zip(oi_results, price_results):
            if oi_result and price_result:
                successful_updates += 1
                symbol = oi_result["symbol"]
                
                # Инициализация истории для символа
                if symbol not in self.oi_history:
                    self.oi_history[symbol] = []
                if symbol not in self.price_history:
                    self.price_history[symbol] = []
                
                # Добавление новых данных OI
                self.oi_history[symbol].append(oi_result)
                
                # Добавление новых данных цены
                self.price_history[symbol].append(price_result)
                
                # Очистка старых данных (храним данные за последний час)
                cutoff_time = oi_result["datetime"] - timedelta(hours=1)
                self.oi_history[symbol] = [
                    entry for entry in self.oi_history[symbol]
                    if entry["datetime"] >= cutoff_time
                ]
                self.price_history[symbol] = [
                    entry for entry in self.price_history[symbol]
                    if entry["datetime"] >= cutoff_time
                ]
                
                # Проверка изменений для каждого пользователя
                if len(self.oi_history[symbol]) > 1 and len(self.price_history[symbol]) > 1:
                    alert_sent = False  # Флаг для отправки только одного уведомления
                    
                    for chat_id in self.chat_ids:
                        if alert_sent:  # Если уже отправили уведомление, пропускаем остальных
                            break
                            
                        settings = self.get_user_settings(chat_id)
                        
                        if not settings["enabled"]:
                            continue
                        
                        # Если пользователь отслеживает конкретные монеты
                        if settings["monitored_symbols"] and symbol not in settings["monitored_symbols"]:
                            continue
                        
                        oi_change_data = self.calculate_oi_change(
                            symbol,
                            oi_result["openInterest"],
                            oi_result["datetime"],
                            settings["time_window_minutes"]
                        )
                        
                        price_change_data = self.calculate_price_change(
                            symbol,
                            price_result["price"],
                            price_result["datetime"],
                            settings["time_window_minutes"]
                        )
                        
                        # DEBUG: показываем изменения для топ монет
                        if symbol in debug_symbols and oi_change_data:
                            print(f"[DEBUG] {symbol}: OI {oi_change_data['change_percent']:+.2f}% | "
                                  f"Цена {price_change_data['price_change_percent']:+.2f}% | "
                                  f"Порог: {settings['oi_threshold_percent']}% | "
                                  f"История: {len(self.oi_history[symbol])} точек")
                        
                        if oi_change_data and self.should_alert(oi_change_data["change_percent"], settings["oi_threshold_percent"]):
                            alert_number = self.get_alert_number(symbol)
                            
                            if alert_number > 0:
                                print(f"[ALERT] 🔔 Отправка алерта для {symbol}: {oi_change_data['change_percent']:+.2f}%")
                                alert_message = self.format_alert(oi_change_data, alert_number, price_change_data)
                                await self.send_alert_to_users(alert_message)
                                self.register_alert(symbol, oi_change_data)
                                alerts_triggered += 1
                                alert_sent = True  # Помечаем что уведомление отправлено
                            else:
                                print(f"[SKIP] {symbol}: Достигнут лимит уведомлений или слишком рано")
        
        active_users = len([cid for cid in self.chat_ids if self.get_user_settings(cid)["enabled"]])
        print(f"[INFO] {datetime.now().strftime('%H:%M:%S')} - Обновлено: {successful_updates}/{len(symbols)} | "
              f"Алертов: {alerts_triggered} | Активных пользователей: {active_users}")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        chat_id = update.effective_chat.id
        self.chat_ids.add(chat_id)
        self.save_data()  # Сохраняем после добавления пользователя
        
        settings = self.get_user_settings(chat_id)
        
        welcome_message = f"""
👋 <b>Добро пожаловать в Bybit OI Tracker!</b>

Бот отслеживает изменения Open Interest на Bybit и присылает уведомления.

📊 <b>Ваши текущие настройки:</b>
• Порог изменения: {settings['oi_threshold_percent']}%
• Временное окно: {settings['time_window_minutes']} минут
• Максимум алертов в день: 5
• Отслеживаемые монеты: {'Все' if not settings['monitored_symbols'] else len(settings['monitored_symbols'])}

<b>Доступные команды:</b>
/settings - Изменить настройки
/status - Текущий статус
/test - Отправить тестовое уведомление
/stop - Остановить уведомления
/start - Возобновить уведомления
"""
        
        await update.message.reply_text(welcome_message, parse_mode='HTML')
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /settings"""
        keyboard = [
            [InlineKeyboardButton("📊 Изменить порог (%)", callback_data="set_threshold")],
            [InlineKeyboardButton("⏱ Изменить период (мин)", callback_data="set_time")],
            [InlineKeyboardButton("🪙 Управление монетами", callback_data="set_coins")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚙️ <b>Настройки бота</b>\n\nВыберите параметр для изменения:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /status"""
        chat_id = update.effective_chat.id
        settings = self.get_user_settings(chat_id)
        
        status = "✅ Включен" if settings["enabled"] else "❌ Выключен"
        coins = "Все монеты" if not settings["monitored_symbols"] else f"{len(settings['monitored_symbols'])} монет"
        
        # Статистика алертов за сегодня
        total_alerts_today = sum(len(alerts) for alerts in self.daily_alerts.values())
        
        status_message = f"""
📊 <b>Текущий статус бота</b>

🔔 Статус: {status}
📈 Порог изменения: {settings['oi_threshold_percent']}%
⏱ Временное окно: {settings['time_window_minutes']} минут
🪙 Отслеживается: {coins}
📢 Уведомлений сегодня: {total_alerts_today}
"""
        
        await update.message.reply_text(status_message, parse_mode='HTML')
    
    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop"""
        chat_id = update.effective_chat.id
        settings = self.get_user_settings(chat_id)
        settings["enabled"] = False
        self.save_data()  # Сохраняем изменения
        
        await update.message.reply_text(
            "⏸ Уведомления остановлены.\nДля возобновления используйте /start"
        )
    
    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /test - отправка тестового алерта"""
        test_change_data = {
            "symbol": "BTCUSDT",
            "change_percent": 5.23,
            "old_oi": 1234567.00,
            "new_oi": 1299135.00,
            "time_window": 15,
            "timestamp": datetime.now()
        }
        
        test_price_data = {
            "price_change_percent": 2.15,
            "old_price": 45231.50,
            "new_price": 46203.75
        }
        
        test_message = self.format_alert(test_change_data, 1, test_price_data)
        await update.message.reply_text(
            "📬 Отправка тестового уведомления...\n\n" + test_message,
            parse_mode='HTML',
            disable_web_page_preview=True
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "close":
            await query.message.delete()
        elif query.data == "set_threshold":
            await query.message.reply_text(
                "📊 Введите новый порог изменения OI в процентах (например: 5):"
            )
            context.user_data["waiting_for"] = "threshold"
        elif query.data == "set_time":
            await query.message.reply_text(
                "⏱ Введите новое временное окно в минутах (например: 15):"
            )
            context.user_data["waiting_for"] = "time"
        elif query.data == "set_coins":
            await query.message.reply_text(
                "🪙 Отправьте список монет через запятую (например: BTCUSDT, ETHUSDT, SOLUSDT)\n"
                "Или отправьте 'все' для отслеживания всех монет."
            )
            context.user_data["waiting_for"] = "coins"
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        chat_id = update.effective_chat.id
        settings = self.get_user_settings(chat_id)
        text = update.message.text.strip()
        
        waiting_for = context.user_data.get("waiting_for")
        
        if waiting_for == "threshold":
            try:
                value = float(text)
                if 0 < value <= 100:
                    settings["oi_threshold_percent"] = value
                    self.save_data()  # Сохраняем изменения
                    await update.message.reply_text(
                        f"✅ Порог изменения установлен: {value}%"
                    )
                else:
                    await update.message.reply_text("❌ Значение должно быть от 0 до 100")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат. Введите число.")
            context.user_data.pop("waiting_for", None)
            
        elif waiting_for == "time":
            try:
                value = int(text)
                if 1 <= value <= 1440:
                    settings["time_window_minutes"] = value
                    self.save_data()  # Сохраняем изменения
                    await update.message.reply_text(
                        f"✅ Временное окно установлено: {value} минут"
                    )
                else:
                    await update.message.reply_text("❌ Значение должно быть от 1 до 1440 минут")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат. Введите число.")
            context.user_data.pop("waiting_for", None)
            
        elif waiting_for == "coins":
            if text.lower() == "все":
                settings["monitored_symbols"] = set()
                self.save_data()  # Сохраняем изменения
                await update.message.reply_text("✅ Теперь отслеживаются все монеты")
            else:
                coins = [coin.strip().upper() for coin in text.split(",")]
                # Добавляем USDT если не указано
                coins = [coin if coin.endswith("USDT") else f"{coin}USDT" for coin in coins]
                settings["monitored_symbols"] = set(coins)
                self.save_data()  # Сохраняем изменения
                await update.message.reply_text(
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
        
        if self.chat_ids:
            print(f"📱 Найдено {len(self.chat_ids)} сохраненных пользователей")
        
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
        
        # Уведомляем пользователей о перезапуске
        if self.chat_ids:
            for chat_id in self.chat_ids:
                try:
                    await self.bot_app.bot.send_message(
                        chat_id=chat_id,
                        text="🔄 Бот перезапущен и снова работает!\n\nВаши настройки сохранены ✅"
                    )
                except Exception as e:
                    print(f"[ERROR] Не удалось отправить уведомление пользователю {chat_id}: {e}")
        
        print()
        
        # Запуск мониторинга
        await self.monitoring_loop()


async def main():
    # API ключи
    BYBIT_API_KEY = "AEpTFcr7Fvj2fPZYhf"
    BYBIT_API_SECRET = "vvjYDz7JRsf2aBhPIFuBEAob37O1W8NC7eHz"
    TELEGRAM_TOKEN = "8187121513:AAHnKKps-TTzvXcK08MeUFJcCil2C4_IB8I"
    
    # Создание и запуск трекера
    tracker = BybitOITracker(BYBIT_API_KEY, BYBIT_API_SECRET, TELEGRAM_TOKEN)
    await tracker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Остановка бота...")
        print("До свидания!")