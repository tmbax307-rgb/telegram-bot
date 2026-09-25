"""
ربات تلگرامی پست‌گذار اشتراک V2Ray — سازگار با پنل Vodiwalker
------------------------------------------------------------
قابلیت‌ها:
1) ادمین با دستور /newpost لینک صفحه‌ی وضعیت اشتراک پنل Vodiwalker را می‌فرستد،
   یعنی همان لینکی که پنل زیر دکمه‌ی «اشتراک» یا در بخش سرویس هر کاربر می‌سازد و
   شکلش این‌طور است:
       https://<دامنه-پنل>/subscription/<uuid>
   (این لینک با /sub/<uuid> که فایل کانفیگ خام v2ray است فرق دارد؛ همین
   /subscription/<uuid> یک صفحه‌ی HTML آماده از خودِ پنل Vodiwalker است که
   وضعیت، مصرف، باقی‌مانده، سقف و اتصال‌های فعال را به فارسی نشان می‌دهد —
   ربات دقیقاً همین صفحه را می‌خواند و پارس می‌کند.)
2) ربات یک پست در کانال منتشر می‌کند با یک دکمه‌ی شیشه‌ای زیرش.
3) با زدن دکمه، کاربر وارد چت خصوصی ربات (deep link) می‌شود و ربات با خواندن
   همان صفحه‌ی وضعیت پنل، این موارد را نشان می‌دهد:
      - وضعیت فعال / غیرفعال
      - مصرف‌شده
      - باقی‌مانده
      - سقف اشتراک
      - درصد مصرف
      - اتصال‌های فعال (تعداد IP/دستگاه یکتا)
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT-YOUR-BOT-TOKEN-HERE").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel_username").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

# خط امضای پایین هر پست، مثل «📣 @faultpass». اگر تنظیم نشود و CHANNEL_ID با
# @ شروع شود، همان به‌عنوان امضا استفاده می‌شود.
CHANNEL_TAG = os.environ.get("CHANNEL_TAG", "").strip() or (
    CHANNEL_ID if CHANNEL_ID.startswith("@") else ""
)

DB_PATH = os.path.join(os.path.dirname(__file__), "subscriptions.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

WAIT_FOR_LINK, WAIT_FOR_VLESS, WAIT_FOR_CAPTION = range(3)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 (compatible; TelegramBot)"
)


# ---------------------------------------------------------------------------
# ذخیره‌سازی ساده روی فایل JSON:
#   id کوتاه -> {link, label, clicks, created_at}
# ---------------------------------------------------------------------------

def _load_db() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # سازگاری با نسخه‌ی قدیمی دیتابیس که فقط short_id -> link بود
    migrated = False
    for short_id, value in list(raw.items()):
        if isinstance(value, str):
            raw[short_id] = {
                "link": value,
                "label": "",
                "clicks": 0,
                "created_at": None,
            }
            migrated = True
    if migrated:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    return raw


def _save_db(db: dict) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def save_subscription_link(link: str, label: str = "") -> str:
    db = _load_db()
    short_id = str(int(time.time() * 1000))[-10:]
    db[short_id] = {
        "link": link,
        "label": label,
        "clicks": 0,
        "created_at": int(time.time()),
    }
    _save_db(db)
    return short_id


def get_subscription_link(short_id: str) -> Optional[str]:
    db = _load_db()
    entry = db.get(short_id)
    if not entry:
        return None
    return entry.get("link")


def increment_click(short_id: str) -> None:
    db = _load_db()
    entry = db.get(short_id)
    if entry:
        entry["clicks"] = entry.get("clicks", 0) + 1
        _save_db(db)


def update_post_link(short_id: str, post_link: str) -> None:
    db = _load_db()
    entry = db.get(short_id)
    if entry:
        entry["post_link"] = post_link
        _save_db(db)


def get_all_stats() -> list:
    """لیست پست‌ها را مرتب بر اساس تازه‌ترین برمی‌گرداند."""
    db = _load_db()
    items = []
    for short_id, entry in db.items():
        items.append(
            {
                "short_id": short_id,
                "label": entry.get("label") or "(بدون عنوان)",
                "clicks": entry.get("clicks", 0),
                "created_at": entry.get("created_at"),
                "post_link": entry.get("post_link"),
            }
        )
    items.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return items


# ---------------------------------------------------------------------------
# گرفتن و پارس کردن صفحه‌ی وضعیت اشتراک پنل Vodiwalker
# (/subscription/<uuid>)
# ---------------------------------------------------------------------------

# در متن فارسی «باقی‌مانده» معمولاً با نیم‌فاصله (ZWNJ, U+200C) نوشته می‌شود؛
# این الگو هم حالت با نیم‌فاصله و هم بدون آن را می‌پذیرد.
ZWNJ = "\u200c"


def _flatten_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # فاصله‌ی مصنوعی بین تگ‌ها می‌گذاریم تا کلمات به هم نچسبند
    return soup.get_text(separator=" ", strip=True)


def _find(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_vodiwalker_status_page(html: str) -> dict:
    text = _flatten_text(html)

    status = _find(r"وضعیت\s*(فعال|غیرفعال)", text)

    used = _find(r"مصرف\s*شده\s*([\d.,]+\s*(?:GB|MB|TB|KB))", text)
    remaining = _find(
        rf"باقی{ZWNJ}?\s*مانده\s*([\d.,]+\s*(?:GB|MB|TB|KB))", text
    )
    total = _find(r"سقف\s*اشتراک\s*([\d.,]+\s*(?:GB|MB|TB|KB))", text)
    percent = _find(r"درصد\s*مصرف\s*([\d.,]+\s*%)", text)

    # «اتصال‌های فعال»: عدد قبل از عبارت «دستگاه / IP یکتا»
    connections = _find(r"([\d,]+)\s*دستگاه\s*/\s*IP\s*یکتا", text)

    expire = _find(r"انقضا[ی]?\s*(?:اشتراک)?\s*([\d\-:T.]+)", text)

    return {
        "status": status,          # "فعال" / "غیرفعال" / None
        "used": used,               # مثلا "62.35 GB"
        "remaining": remaining,     # مثلا "37.65 GB"
        "total": total,             # مثلا "100.00 GB"
        "percent": percent,         # مثلا "62.3%"
        "connections": connections, # مثلا "190"
        "expire": expire,
    }


async def fetch_subscription_status(status_url: str) -> dict:
    headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(status_url, headers=headers)
        resp.raise_for_status()
        return parse_vodiwalker_status_page(resp.text)


def escape_html(text: str) -> str:
    """اسکیپ کاراکترهای خاص HTML برای امن بودن داخل تگ‌های تلگرام."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_channel_post(caption: str, vless_link: str, status_deep_link: str) -> str:
    """
    متن نهایی پست کانال را می‌سازد:
    - کپشن دلخواه ادمین (به‌صورت متن ساده در نظر گرفته می‌شود و escape می‌شود؛
      یعنی اگر کاراکترهایی مثل <, >, & در آن باشد، پیام خراب نمی‌شود)
    - لینک سرور داخل هم‌زمان quote (<blockquote>) و mono (<code>) تلگرام،
      که هم شکل جعبه‌ای و تمیز می‌دهد و هم با یک تاچ روی لینک کپی می‌شود.
    - لینک نوشتاری «📊مشاهده وضعیت کانفیگ: لینک ربات» (به‌جای دکمه شیشه‌ای)
    - خط امضای کانال (اختیاری، آن هم escape می‌شود)
    هر بخش با یک خط خالی از بخش بعدی جدا می‌شود.
    """
    safe_caption = escape_html(caption.strip())
    safe_link = escape_html(vless_link)
    safe_tag = escape_html(CHANNEL_TAG)
    safe_status_link = escape_html(status_deep_link)

    parts = [safe_caption] if safe_caption else []
    parts.append(f"<blockquote><code>{safe_link}</code></blockquote>")
    parts.append(f'📊مشاهده وضعیت کانفیگ: <a href="{safe_status_link}">لینک ربات</a>')
    if safe_tag:
        parts.append(safe_tag)
    return "\n\n".join(parts)


def format_status_message(info: dict) -> str:
    status = info.get("status")
    status_display = "✅ فعال" if status == "فعال" else ("❌ غیرفعال" if status == "غیرفعال" else "❓ نامشخص")

    def val(key, fallback="نامشخص"):
        return info.get(key) or fallback

    lines = [
        "📊 <b>وضعیت اشتراک شما</b>\n",
        f"📶 وضعیت: <b>{status_display}</b>",
        f"🔺 مصرف‌شده: <b>{val('used')}</b>",
        f"🟢 باقی‌مانده: <b>{val('remaining')}</b>",
        f"📦 سقف اشتراک: <b>{val('total')}</b>",
        f"📈 درصد مصرف: <b>{val('percent')}</b>",
        f"🔗 <b>اتصال‌های فعال: {val('connections')}</b>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# هندلرهای ادمین برای ساخت پست
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین است.")
        return ConversationHandler.END

    await update.message.reply_text(
        "لینک صفحه‌ی وضعیت اشتراک پنل را بفرستید — همان لینک /subscription/... "
        "که خودِ پنل برای هر سرویس می‌سازد (نه لینک خام /sub/...):\n\n"
        "مثال:\nhttps://your-panel.up.railway.app/subscription/xxxxxxxx-xxxx-xxxx"
    )
    return WAIT_FOR_LINK


async def newpost_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    if not re.match(r"^https?://", link):
        await update.message.reply_text("این یک لینک معتبر نیست. دوباره بفرستید:")
        return WAIT_FOR_LINK

    # تست سریع: ببینیم صفحه قابل خواندن و پارس هست یا نه
    try:
        info = await fetch_subscription_status(link)
        if not any(info.values()):
            await update.message.reply_text(
                "⚠️ لینک باز شد ولی هیچ‌کدام از فیلدها استخراج نشد. "
                "مطمئن شوید لینک از نوع /subscription/<uuid> پنل است، نه /sub/<uuid>. "
                "در هر صورت لینک ذخیره شد."
            )
    except Exception:
        await update.message.reply_text(
            "⚠️ در تست لینک خطا رخ داد (شاید موقتی باشد)، ولی لینک ذخیره شد و "
            "دوباره در زمان استفاده‌ی کاربر تلاش می‌شود."
        )

    context.user_data["pending_link"] = link
    await update.message.reply_text(
        "حالا لینک سرور (vless://...) را بفرست — همین لینک به‌صورت quote + "
        "mono (قابل کپی با یک تاچ) داخل پست کانال نمایش داده می‌شود:"
    )
    return WAIT_FOR_VLESS


async def newpost_receive_vless(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vless_link = update.message.text.strip()
    if not re.match(r"^[a-zA-Z0-9+.\-]+://", vless_link):
        await update.message.reply_text(
            "این متن یک لینک کانفیگ معتبر (vless://... یا مشابه) به نظر نمی‌رسد. دوباره بفرستید:"
        )
        return WAIT_FOR_VLESS

    context.user_data["pending_vless"] = vless_link
    await update.message.reply_text(
        "یک کپشن/متن برای پست کانال بفرست (یا بنویس «پیش‌فرض» برای متن پیش‌فرض، "
        "یا «خالی» برای بدون کپشن):"
    )
    return WAIT_FOR_CAPTION


def build_post_link(message_id: int) -> Optional[str]:
    """
    از روی CHANNEL_ID لینک قابل‌کلیک خودِ پست را می‌سازد.
    - کانال پابلیک (@username): https://t.me/username/<id>
    - کانال پرایوت (-100xxxxxxxxxx): https://t.me/c/xxxxxxxxxx/<id>
    """
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}/{message_id}"
    if CHANNEL_ID.startswith("-100"):
        internal_id = CHANNEL_ID[4:]
        return f"https://t.me/c/{internal_id}/{message_id}"
    return None


async def newpost_receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption_text = update.message.text.strip()
    if caption_text == "پیش‌فرض":
        caption_text = "🔥 سرویس جدید اضافه شد!\nبرای مشاهده وضعیت اشتراک خودتان روی دکمه زیر بزنید 👇"
    elif caption_text == "خالی":
        caption_text = ""

    status_link = context.user_data.pop("pending_link")
    vless_link = context.user_data.pop("pending_vless")

    # یک برچسب کوتاه از کپشن برای نمایش در آمار می‌سازیم
    label_source = caption_text or vless_link
    label = label_source.splitlines()[0][:40]
    short_id = save_subscription_link(status_link, label=label)

    bot_username = (await context.bot.get_me()).username
    deep_link = f"https://t.me/{bot_username}?start={short_id}"

    post_text = build_channel_post(caption_text, vless_link, deep_link)

    sent_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=post_text,
        parse_mode=ParseMode.HTML,
    )

    post_link = build_post_link(sent_msg.message_id)
    if post_link:
        update_post_link(short_id, post_link)

    await update.message.reply_text("✅ پست در کانال منتشر شد.")
    return ConversationHandler.END


async def newpost_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_link", None)
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# دستور ادمین: /stats — تعداد کلیک هر پست
# ---------------------------------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین است.")
        return

    items = get_all_stats()
    if not items:
        await update.message.reply_text("هنوز هیچ پستی ساخته نشده است.")
        return

    total_clicks = sum(i["clicks"] for i in items)
    lines = [f"📊 <b>آمار پست‌ها</b> (مجموع کلیک‌ها: {total_clicks})\n"]

    for i in items:
        link_line = ""
        if i.get("post_link"):
            link_line = f'\n   📎 <a href="{escape_html(i["post_link"])}">مشاهده پست</a>'
        lines.append(
            f"• {escape_html(i['label'])}\n"
            f"   🔗 آیدی: <code>{i['short_id']}</code> — 👆 کلیک: <b>{i['clicks']}</b>"
            f"{link_line}"
        )

    text = "\n".join(lines)
    # تلگرام محدودیت طول پیام دارد؛ اگر خیلی طولانی شد، تکه‌تکه بفرست
    MAX_LEN = 3500
    for start_i in range(0, len(text), MAX_LEN):
        await update.message.reply_text(
            text[start_i : start_i + MAX_LEN], parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------------------
# هندلر کاربر عادی: /start با پارامتر deep-link
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "سلام! برای دیدن وضعیت اشتراک، از طریق دکمه‌ی زیر پست کانال وارد شوید."
        )
        return

    short_id = args[0]
    link = get_subscription_link(short_id)
    if not link:
        await update.message.reply_text("این لینک معتبر نیست یا منقضی شده است.")
        return

    increment_click(short_id)

    wait_msg = await update.message.reply_text("⏳ در حال دریافت اطلاعات اشتراک...")
    try:
        info = await fetch_subscription_status(link)
        text = format_status_message(info)
    except Exception:
        logger.exception("خطا در گرفتن وضعیت اشتراک")
        text = "❌ در دریافت اطلاعات اشتراک خطایی رخ داد. لطفاً بعداً دوباره امتحان کنید."

    await wait_msg.edit_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# راه‌اندازی برنامه
# ---------------------------------------------------------------------------

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("newpost", newpost_start)],
        states={
            WAIT_FOR_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_receive_link)],
            WAIT_FOR_VLESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_receive_vless)],
            WAIT_FOR_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_receive_caption)
            ],
        },
        fallbacks=[CommandHandler("cancel", newpost_cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))

    logger.info("ربات شروع به کار کرد...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
