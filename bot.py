"""
Telegram-бот для уведомлений о новых событиях на sp-zp.ru
Отслеживает: https://sp-zp.ru/tickets/mariinskiy-teatr-scene
"""

import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN        = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА")
SPZP_SESSION     = os.getenv("SPZP_SESSION", "ВСТАВЬТЕ_PHPSESSID_СЮДА")
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "5"))

DATA_FILE        = Path("data/seen_events.json")
SUBSCRIBERS_FILE = Path("data/subscribers.json")
TARGET_URL       = "https://sp-zp.ru/tickets/mariinskiy-teatr-scene"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Хранилище ────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_seen_events() -> set:
    return set(load_json(DATA_FILE, []))

def save_seen_events(events: set):
    save_json(DATA_FILE, list(events))

def get_subscribers() -> list:
    return load_json(SUBSCRIBERS_FILE, [])

def add_subscriber(chat_id: int) -> bool:
    subs = get_subscribers()
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)
        return True
    return False

def remove_subscriber(chat_id: int) -> bool:
    subs = get_subscribers()
    if chat_id in subs:
        subs.remove(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)
        return True
    return False

# ─── Парсер ───────────────────────────────────────────────────────────────────

async def fetch_events() -> list:
    events = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            # Передаём куку авторизации
            await context.add_cookies([{
                "name":   "PHPSESSID",
                "value":  SPZP_SESSION,
                "domain": "sp-zp.ru",
                "path":   "/",
            }])

            page = await context.new_page()
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

            # Ждём появления карточек событий
            try:
                await page.wait_for_selector(
                    ".event, .ticket, .show, .performance, "
                    "[class*='event'], [class*='ticket'], [class*='show'], "
                    "article, .card, .item",
                    timeout=15000
                )
            except Exception:
                pass

            raw = await page.evaluate("""
                () => {
                    const results = [];

                    // Перебираем возможные селекторы карточек
                    const candidates = [
                        '.event-item', '.ticket-item', '.show-item',
                        '.performance-item', '.card', '.item',
                        'article', '[class*="event"]', '[class*="ticket"]',
                        'li.event', 'div.event', 'tr.event'
                    ];

                    let items = [];
                    for (const sel of candidates) {
                        items = [...document.querySelectorAll(sel)];
                        if (items.length > 2) break;
                    }

                    // Запасной вариант — все ссылки с текстом
                    if (items.length === 0) {
                        items = [...document.querySelectorAll('a[href]')].filter(a =>
                            a.innerText && a.innerText.trim().length > 5
                        );
                    }

                    items.forEach(el => {
                        const titleEl = el.querySelector('h1,h2,h3,h4,.title,.name,.event-name') 
                                        || (el.tagName === 'A' ? el : null);
                        if (!titleEl) return;

                        const title = (titleEl.innerText || titleEl.textContent || '').trim();
                        if (!title || title.length < 3) return;

                        const dateEl  = el.querySelector('[class*="date"], time, .date');
                        const timeEl  = el.querySelector('[class*="time"], .time');
                        const priceEl = el.querySelector('[class*="price"], .price');
                        const linkEl  = el.tagName === 'A' ? el : el.querySelector('a[href]');

                        results.push({
                            title: title,
                            date:  (dateEl?.innerText  || '').trim(),
                            time:  (timeEl?.innerText  || '').trim(),
                            price: (priceEl?.innerText || '').trim(),
                            link:  linkEl?.href || ''
                        });
                    });

                    return results;
                }
            """)

            await browser.close()

            seen_titles = set()
            for item in raw:
                title = item.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)

                uid = hashlib.md5(
                    f"{title}|{item.get('date','')}|{item.get('time','')}".encode()
                ).hexdigest()[:12]

                events.append({
                    "id":    uid,
                    "title": title,
                    "date":  item.get("date", ""),
                    "time":  item.get("time", ""),
                    "price": item.get("price", ""),
                    "link":  item.get("link", ""),
                })

    except Exception as e:
        logger.error(f"Ошибка при парсинге: {e}")

    logger.info(f"Найдено событий: {len(events)}")
    return events

# ─── Уведомления ──────────────────────────────────────────────────────────────

def format_message(event: dict) -> str:
    lines = ["🎭 <b>Новое событие на sp-zp.ru!</b>\n"]
    lines.append(f"<b>{event['title']}</b>")
    dt = " ".join(filter(None, [event.get("date"), event.get("time")]))
    if dt:
        lines.append(f"📅 {dt}")
    if event.get("price"):
        lines.append(f"💰 {event['price']}")
    if event.get("link"):
        lines.append(f'\n🔗 <a href="{event["link"]}">Купить билет</a>')
    return "\n".join(lines)

async def notify_subscribers(bot: Bot, new_events: list):
    subscribers = get_subscribers()
    if not subscribers:
        return
    for event in new_events:
        text = format_message(event)
        for chat_id in subscribers:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Не удалось отправить {chat_id}: {e}")

# ─── Основная задача ──────────────────────────────────────────────────────────

async def check_for_new_events(bot: Bot):
    logger.info("🔍 Проверяем sp-zp.ru...")
    events = await fetch_events()
    if not events:
        logger.warning("Список пуст — возможно сессия истекла или сайт недоступен.")
        return

    seen_ids = get_seen_events()
    if not seen_ids:
        logger.info(f"Первый запуск — запоминаем {len(events)} событий.")
        save_seen_events({e["id"] for e in events})
        return

    new_events = [e for e in events if e["id"] not in seen_ids]
    if new_events:
        logger.info(f"🆕 Новых: {len(new_events)}")
        await notify_subscribers(bot, new_events)
        seen_ids.update(e["id"] for e in new_events)
        save_seen_events(seen_ids)
    else:
        logger.info("Новых событий нет.")

# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if add_subscriber(update.effective_chat.id):
        await update.message.reply_text(
            "✅ <b>Вы подписались на уведомления!</b>\n\n"
            "Буду сообщать о новых событиях на sp-zp.ru (Мариинский театр).\n\n"
            "/stop — отписаться\n/status — статистика\n/check — проверить сейчас",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("Вы уже подписаны! 🎭")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if remove_subscriber(update.effective_chat.id):
        await update.message.reply_text("❌ Вы отписались.")
    else:
        await update.message.reply_text("Вы не были подписаны.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = get_subscribers()
    seen = get_seen_events()
    status = "✅ подписан" if chat_id in subs else "❌ не подписан"
    await update.message.reply_text(
        f"Статус: {status}\n"
        f"Подписчиков: {len(subs)}\n"
        f"Событий в базе: {len(seen)}\n"
        f"Проверка каждые {CHECK_INTERVAL} мин.\n"
        f"Страница: {TARGET_URL}"
    )

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Проверяю (~15 сек)...")
    await check_for_new_events(context.application.bot)
    await update.message.reply_text("✅ Готово.")

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_for_new_events,
        "interval",
        minutes=CHECK_INTERVAL,
        args=[application.bot],
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info(f"🚀 Планировщик запущен. Проверка каждые {CHECK_INTERVAL} минут.")

def main():
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА":
        raise RuntimeError("❌ Укажите BOT_TOKEN.")
    if SPZP_SESSION == "ВСТАВЬТЕ_PHPSESSID_СЮДА":
        raise RuntimeError("❌ Укажите SPZP_SESSION.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check",  cmd_check))

    logger.info("🚀 Бот запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
