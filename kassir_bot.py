import os, json, secrets, logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cashier")

async def safe_edit(q, text: str, **kwargs):
    """
    Универсальное редактирование: если сообщение медиа — меняем caption,
    если обычное — меняем text.
    """
    try:
        m = q.message
        if getattr(m, "photo", None) or getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "video_note", None):
            return await q.edit_message_caption(caption=text, **kwargs)
        return await q.edit_message_text(text, **kwargs)
    except Exception:
        # fallback на альтернативный метод + лог
        try:
            return await q.edit_message_caption(caption=text, **kwargs)
        except Exception:
            log.exception("safe_edit: both edit_message_text and edit_message_caption failed")
            return await q.edit_message_text(text, **kwargs)

# -------------------- CONFIG / ENV --------------------
load_dotenv()

BOT_TOKEN    = os.getenv("CASHIER_BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# целевые боты (username)
BOT_UNPACK = os.getenv("BOT_UNPACK", "jtbd_assistant_bot")              # Бот №1
BOT_COPY   = os.getenv("BOT_COPY",   "content_helper_assist_bot")       # Бот №2

# юр-документы + инфо о разработчике (ссылки)
POLICY_URL      = (os.getenv("POLICY_URL") or "").strip()
OFFER_URL       = (os.getenv("OFFER_URL") or "").strip()
ADS_CONSENT_URL = (os.getenv("ADS_CONSENT_URL") or "").strip()
DEV_INFO_URL    = (os.getenv("DEV_INFO_URL") or "").strip()

# кружок (video note) — file_id (опционально)
DEV_VIDEO_NOTE_ID = os.getenv("DEV_VIDEO_NOTE_ID", "").strip()

# оплата на карту
PAY_PHONE   = os.getenv("PAY_PHONE", "+7XXXXXXXXXX")
PAY_NAME    = os.getenv("PAY_NAME", "Ирина Александровна П.")
PAY_BANK    = os.getenv("PAY_BANK", "ОЗОН-Банк")

# срок жизни персональных ссылок (часы)
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "48"))

# примеры ответов (file_id картинок)
EXAMPLE_IDS = [
    os.getenv("EXAMPLE_1_ID"),
    os.getenv("EXAMPLE_2_ID"),
    os.getenv("EXAMPLE_3_ID"),
    os.getenv("EXAMPLE_4_ID"),
    os.getenv("EXAMPLE_5_ID"),
]

# Акция (только 2 бота)
PROMO_ACTIVE = os.getenv("PROMO_ACTIVE", "true").lower() == "true"
PROMO_PRICES = {
    "unpack": 1890.00,   # Бот №1
    "copy":   2490.00,   # Бот №2
    "b12":    3990.00,   # Пакет 1+2
}

# Конец акции и TZ для напоминаний
PROMO_END_ISO = os.getenv("PROMO_END_ISO", "").strip()  # напр. 2025-08-18T00:00:00+03:00
TIMEZONE      = os.getenv("TIMEZONE", "Europe/Moscow")

if not (BOT_TOKEN and ADMIN_ID and DATABASE_URL and POLICY_URL and OFFER_URL and ADS_CONSENT_URL):
    raise RuntimeError("Проверь .env: CASHIER_BOT_TOKEN, ADMIN_ID, DATABASE_URL, POLICY_URL, OFFER_URL, ADS_CONSENT_URL")

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("cashier")

# -------------------- DB --------------------
conn = psycopg.connect(DATABASE_URL, autocommit=True, sslmode="require", row_factory=dict_row)
cur  = conn.cursor()

cur.execute("""CREATE TABLE IF NOT EXISTS consents(
  user_id BIGINT PRIMARY KEY,
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS products(
  code TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  price NUMERIC(10,2) NOT NULL,
  targets JSONB NOT NULL
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS orders(
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  product_code TEXT NOT NULL REFERENCES products(code),
  amount NUMERIC(10,2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending/await_receipt/paid/rejected
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS receipts(
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  file_id TEXT NOT NULL,
  file_type TEXT NOT NULL,  -- photo/document
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY,
  bot_name TEXT NOT NULL,
  user_id BIGINT NOT NULL,
  expires_at TIMESTAMPTZ NULL
);""")
# гарантируем наличие столбца для сроков действия токенов
cur.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL;")
cur.execute("""CREATE TABLE IF NOT EXISTS allowed_users(
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  PRIMARY KEY(user_id, bot_name)
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS invoice_requests(
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed BOOLEAN NOT NULL DEFAULT FALSE
);""")


# Каталог базовых цен (без фото-бота)
CATALOG = {
    "unpack": {"title": "Бот №1 «Распаковка + Анализ ЦА (JTBD)»",        "price": 2990.00, "targets": [BOT_UNPACK]},
    "copy":   {"title": "Бот №2 «Твой личный контент-помощник»",         "price": 5490.00, "targets": [BOT_COPY]},
    "b12":    {"title": "Пакет «Распаковка + контент»",                  "price": 7990.00, "targets": [BOT_UNPACK, BOT_COPY]},
}
for code, p in CATALOG.items():
    cur.execute(
        """INSERT INTO products(code, title, price, targets)
           VALUES (%s,%s,%s,%s::jsonb)
           ON CONFLICT (code) DO UPDATE SET title=EXCLUDED.title, price=EXCLUDED.price, targets=EXCLUDED.targets""",
        (code, p["title"], p["price"], json.dumps(p["targets"]))
    )

# -------------------- Utils --------------------
def set_consent(user_id: int):
    try:
        cur.execute(
            "INSERT INTO consents(user_id, accepted_at) VALUES(%s, now()) ON CONFLICT DO NOTHING",
            (user_id,)
        )
    except Exception as e:
        log.warning("Ошибка при сохранении согласия: %s", e)

def get_product(code: str) -> Optional[dict]:
    cur.execute("SELECT * FROM products WHERE code=%s", (code,))
    return cur.fetchone()

def current_price(code: str) -> float:
    base = float(get_product(code)["price"])
    if PROMO_ACTIVE and code in PROMO_PRICES:
        return float(PROMO_PRICES[code])
    return base

def create_order(user_id: int, code: str) -> int:
    price = current_price(code)
    cur.execute(
        "INSERT INTO orders(user_id, product_code, amount, status) VALUES(%s,%s,%s,'pending') RETURNING id",
        (user_id, code, price)
    )
    return cur.fetchone()["id"]

def set_status(order_id: int, status: str):
    cur.execute("UPDATE orders SET status=%s WHERE id=%s", (status, order_id))

def get_order(order_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    return cur.fetchone()

def gen_tokens_with_ttl(user_id: int, targets: list[str], ttl_hours: int):
    links = []
    expires_at = datetime.utcnow() + timedelta(hours=ttl_hours) if ttl_hours > 0 else None
    for bot_name in targets:
        token = secrets.token_urlsafe(8)
        cur.execute(
            "INSERT INTO tokens(token, bot_name, user_id, expires_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (token, bot_name, user_id, expires_at)
        )
        links.append((bot_name, f"https://t.me/{bot_name}?start={token}"))
    return links

def shop_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Оплатить бота «Распаковка + Анализ ЦА»",         callback_data="buy:unpack")],
        [InlineKeyboardButton("Оплатить бота «Твой личный контент-помощник»",   callback_data="buy:copy")],
        [InlineKeyboardButton("Оплатить ботов «Распаковка+контент»",            callback_data="buy:b12")],
        [InlineKeyboardButton("📄 Загрузить чек",                                callback_data="upload_receipt")],
    ])

PROMO_TEXT = (
    "🎁 Специальные цены для моей аудитории (только 2 дня)\n\n"
    "🛠 Боты по отдельности\n"
    "• «Распаковка + Анализ ЦА» — <s>2 990 ₽</s> → 1 890 ₽ (выгода 1 100 ₽)\n"
    "• «Твой личный контент-помощник» — <s>5 490 ₽</s> → 2 490 ₽ (выгода 3 000 ₽)\n\n"
    "💎 Пакет — ещё выгоднее\n"
    "• Боты «Распаковка+контент» — <s>7 990 ₽</s> → 3 990 ₽ (выгода 4 000 ₽)"
)
ABOUT_BOTS = (
    "Бот №1 «Распаковка + Анализ ЦА (JTBD)» — про понимание, что клиенты реально «покупают», "
    "и как под это подстроить позиционирование и контент.\n\n"
    "Бот №2 «Твой личный контент-помощник» — контент-план, посты, Reels/Stories, визуальные подсказки "
    "на основе распаковки."
)

# ----- Примеры ответов -----
async def send_examples_screens(ctx, chat_id: int):
    ids = [fid for fid in EXAMPLE_IDS if fid]
    if not ids:
        return
    media = []
    for i, fid in enumerate(ids):
        try:
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption="Примеры ответов ботов"))
            else:
                media.append(InputMediaPhoto(media=fid))
        except Exception as e:
            log.warning("Bad example file_id skipped: %s", e)
    if media:
        try:
            await ctx.bot.send_media_group(chat_id=chat_id, media=media)
        except Exception as e:
            log.warning("send_media_group error: %s", e)

# ----- Напоминания об окончании акции (T-48/T-24) -----
def get_audience_user_ids() -> list[int]:
    cur.execute("SELECT user_id FROM consents")
    return [r["user_id"] for r in cur.fetchall()]

async def job_promo_countdown(ctx: ContextTypes.DEFAULT_TYPE):
    hours_left = ctx.job.data
    if hours_left == 48:
        text = "⏰ Через 2 суток спеццены закончатся. Успейте оформить заказ по акции."
    elif hours_left == 24:
        text = "⏰ Через сутки спеццены закончатся. Последний шанс купить выгодно."
    else:
        text = f"⏰ Напоминание: осталось ~{hours_left} часов до окончания акции."
    kb = shop_keyboard()
    for uid in get_audience_user_ids():
        try:
            await ctx.bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

# -------------------- Handlers --------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # 1) Кружок (если указан file_id)
    if DEV_VIDEO_NOTE_ID:
        try:
            await ctx.bot.send_video_note(chat_id=uid, video_note=DEV_VIDEO_NOTE_ID)
        except Exception as e:
            log.warning("video note send error: %s", e)

    # 2) Юридический «гейт» — ссылки только в кнопках
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Политика конфиденциальности", url=POLICY_URL)],
        [InlineKeyboardButton("📜 Договор оферты",              url=OFFER_URL)],
        [InlineKeyboardButton("✉️ Согласие на рекламу",        url=ADS_CONSENT_URL)],
        [InlineKeyboardButton("✅ Согласен — перейти к оплате", callback_data="consent_ok")],
    ])
    await ctx.bot.send_message(
        chat_id=uid,
        text=(
            "Прежде чем продолжить, подтвердите согласие с условиями использования.\n\n"
            "Нажимая кнопку \u00ab✅ Согласен — перейти к оплате\u00bb, вы принимаете условия:"
        ),
        reply_markup=kb
    )


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    try:
        await q.answer("⏳ Обрабатываю…", show_alert=False)
    except Exception:
        pass

    try:
        if data == "consent_ok":
            set_consent(uid)

            await safe_edit(q, "✅ Вы подтвердили согласие. Давайте покажу, как работают боты:", parse_mode="HTML")

            await ctx.bot.send_message(
                chat_id=uid,
                text=(
                    "🧠 <b>Бот №1: Распаковка + Анализ ЦА (JTBD)</b>\n"
                    "Поможет понять, что на самом деле «покупает» клиент, и как правильно сформулировать позиционирование.\n\n"
                    "✍️ <b>Бот №2: Контент-помощник</b>\n"
                    "Создаёт контент-план, тексты, Reels, визуальные подсказки — на основе вашей распаковки."
                ),
                parse_mode="HTML"
            )

            await send_examples_screens(ctx, uid)

            await ctx.bot.send_message(
                chat_id=uid,
                text=(
                    "🎁 <b>Спеццены только 2 дня:</b>\n\n"
                    "🛠 <b>Отдельные боты</b>\n"
                    "• Распаковка + Анализ ЦА — <s>2 990 ₽</s> → <b>1 890 ₽</b>\n"
                    "• Контент-помощник — <s>3 890 ₽</s> → <b>2 490 ₽</b>\n\n"
                    "💎 <b>Пакет 1+2</b>\n"
                    "• Всё вместе — <s>6 880 ₽</s> → <b>3 990 ₽</b>"
                ),
                parse_mode="HTML"
            )

            await ctx.bot.send_message(
                chat_id=uid,
                text="👇 Выберите продукт, который хотите оплатить:",
                reply_markup=shop_keyboard()
            )
            return

        if data.startswith("buy:"):
            code = data.split(":", 1)[1]
            prod = get_product(code)
            if not prod:
                await q.edit_message_text("Продукт не найден. Обновите витрину: /start")
                return

            order_id = create_order(uid, code)
            price = current_price(code)
            set_status(order_id, "await_receipt")

            old = float(prod["price"])
            old_line = f"Старая цена: <s>{old:.2f} ₽</s>\n" if PROMO_ACTIVE else ""

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить чек по этому заказу", callback_data=f"send_receipt:{order_id}")],
                [InlineKeyboardButton("◀️ Назад к списку", callback_data="go_shop")]
            ])

            await q.edit_message_text(
                f"🧾 <b>{prod['title']}</b>\n\n"
                f"{old_line}Сумма к оплате: <b>{price:.2f} ₽</b>\n\n"
                f"💳 <b>Оплата на карту {PAY_BANK}</b>\n"
                f"• Номер: <code>{PAY_PHONE}</code>\n"
                f"• Получатель: <b>{PAY_NAME}</b>\n"
                f"• Комментарий к переводу: <code>ORDER-{order_id}</code>\n\n"
                "После оплаты нажмите кнопку ниже или прикрепите чек через витрину.",
                parse_mode="HTML",
                reply_markup=kb
            )

            # Подсказка сразу после оформления заказа
            await ctx.bot.send_message(
                chat_id=uid,
                text=(
                    "🔔 <b>Важно:</b> После оплаты прикрепите чек.\n"
                    "Я проверю его и отправлю доступ к выбранному боту.\n\n"
                    "Если возникнут вопросы — просто напишите сюда."
                ),
                parse_mode="HTML"
            )

            # Напоминание через 1 час, если чек не отправлен
            async def remind_unpaid(context: ContextTypes.DEFAULT_TYPE):
                cur.execute(
                    "SELECT status FROM orders WHERE id=%s", (order_id,)
                )
                row = cur.fetchone()
                if row and row["status"] == "await_receipt":
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=(
                                "⏰ Напоминание: вы оформили заказ, но ещё не прикрепили чек.\n"
                                "Пожалуйста, завершите оплату, чтобы получить доступ к боту."
                            )
                        )
                    except Exception:
                        pass

            ctx.job_queue.run_once(remind_unpaid, when=3600, name=f"remind_order_{order_id}")

            return

        if data.startswith("send_receipt:"):
            order_id = int(data.split(":", 1)[1])
            order = get_order(order_id)
            if not order or order["user_id"] != uid:
                await q.edit_message_text("Заказ не найден. Откройте витрину: /start")
                return
            await q.edit_message_text("Загрузите чек (фото или PDF) одним сообщением. После проверки пришлю доступ.")
            return

        # --- админские действия ---
        if data.startswith("confirm:") or data.startswith("reject:") \
           or data.startswith("send_invoice:") or data.startswith("close_invoice:"):
            if uid != ADMIN_ID:
                try:
                    await q.answer("Нет прав.", show_alert=True)
                except Exception:
                    pass
                return

            if data.startswith("confirm:") or data.startswith("reject:"):
                order_id = int(data.split(":", 1)[1])
                order = get_order(order_id)
                if not order:
                    await q.edit_message_text("Заказ не найден.")
                    return

                if data.startswith("reject:"):
                    set_status(order_id, "rejected")
                    await safe_edit(q, f"Заказ #{order_id}: отклонён.", reply_markup=None)
                    try:
                        await ctx.bot.send_message(
                            order["user_id"],
                            "❌ Оплата не подтверждена. Если это ошибка — загрузите чек ещё раз."
                        )
                    except Exception:
                        pass
                    return


                # confirm
                set_status(order_id, "paid")
                prod  = get_product(order["product_code"])
                links = gen_tokens_with_ttl(order["user_id"], prod["targets"], TOKEN_TTL_HOURS)

                warn = (
                    "✅ Чек проверен.\n\n"
                    "⚠️ Ссылки индивидуальные. Они действуют ограниченное время "
                    f"(~{TOKEN_TTL_HOURS} ч) и перестают работать после активации."
                )
                btns = [[InlineKeyboardButton(f"Открыть @{bn}", url=link)] for bn, link in links]

                await safe_edit(q, f"Заказ #{order_id}: подтверждён. Ссылки отправлены.", reply_markup=None)

                try:
                    await ctx.bot.send_message(
                        order["user_id"], warn,
                        reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML"
                    )
                except Exception:
                    pass
                return


            if data.startswith("close_invoice:"):
                order_id = int(data.split(":", 1)[1])
                cur.execute("UPDATE invoice_requests SET closed=TRUE WHERE order_id=%s", (order_id,))
                await safe_edit(f"Запрос на чек по заказу #{order_id} закрыт.")
                return

        if data.startswith("request_invoice:"):
            order_id = int(data.split(":", 1)[1])
            order = get_order(order_id)
            if not order or order["user_id"] != uid:
                await q.answer("Заказ не найден.", show_alert=True)
                return
            cur.execute(
                "INSERT INTO invoice_requests(order_id, closed) VALUES(%s, FALSE) "
                "ON CONFLICT (order_id) DO UPDATE SET closed=FALSE",
                (order_id,)
            )
            kb_admin = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить чек клиенту", callback_data=f"send_invoice:{order_id}")],
                [InlineKeyboardButton("✅ Закрыть запрос",        callback_data=f"close_invoice:{order_id}")],
            ])
            try:
                await ctx.bot.send_message(ADMIN_ID, f"🧾 Запрос чека по заказу #{order_id}\nПокупатель: {uid}", reply_markup=kb_admin)
            except Exception:
                pass
            await q.answer("Запрос на чек отправлен. Ждём файл от продавца.", show_alert=True)
            return

        await q.answer("Команда не распознана.", show_alert=False)

    except Exception:
        log.exception("Callback error for data=%r uid=%s", data, uid)
        try:
            await ctx.bot.send_message(uid, "⚠️ Во время обработки произошла ошибка. Попробуйте ещё раз: /start")
        except Exception:
            pass

async def receipts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Покупатель отправляет чек (фото/документ). Привяжем к последнему await_receipt и перешлём админу."""
    uid = update.effective_user.id
    # последний заказ, который ждёт чека
    cur.execute("SELECT id FROM orders WHERE user_id=%s AND status='await_receipt' ORDER BY id DESC LIMIT 1", (uid,))
    row = cur.fetchone()
    if not row:
        return
    order_id = row["id"]

    file_id, file_type = None, None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
    if not file_id:
        await update.message.reply_text("⚠ Пришлите фото или PDF одним сообщением.")
        return

    cur.execute("INSERT INTO receipts(order_id, file_id, file_type) VALUES(%s,%s,%s)", (order_id, file_id, file_type))
    set_status(order_id, "pending")

    kb_admin = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{order_id}"),
                                      InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject:{order_id}")]])
    caption = f"💳 Чек по заказу #{order_id}\nПокупатель: {uid}"
    try:
        if file_type == "photo":
            await ctx.bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=kb_admin)
        else:
            await ctx.bot.send_document(ADMIN_ID, file_id, caption=caption, reply_markup=kb_admin)
    except Exception:
        pass
    await update.message.reply_text("✅ Чек отправлен на проверку. Ожидайте ответа.")

# --- Админ: отправка своего чека клиенту после запроса (если используешь запросы) ---
async def admin_invoice_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT order_id FROM invoice_requests WHERE closed=FALSE ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return
    order_id = row["order_id"]
    order = get_order(order_id)
    if not order:
        return

    file_id, is_photo = None, False
    if update.message.photo:
        file_id = update.message.photo[-1].file_id; is_photo = True
    elif update.message.document:
        file_id = update.message.document.file_id
    if not file_id:
        return

    try:
        if is_photo:
            await ctx.bot.send_photo(order["user_id"], file_id, caption="🧾 Чек от продавца")
        else:
            await ctx.bot.send_document(order["user_id"], file_id, caption="🧾 Чек от продавца")
        cur.execute("UPDATE invoice_requests SET closed=TRUE WHERE order_id=%s", (order_id,))
        await update.message.reply_text(f"Чек отправлен покупателю (заказ #{order_id}). Запрос закрыт.")
    except Exception:
        pass

# --- /vnote: получить file_id кружка ---
async def help_vnote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Пришлите кружок (video note) — верну file_id.")

async def detect_vnote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if update.message.video_note:
        await update.message.reply_text(f"file_id кружка: {update.message.video_note.file_id}\nСкопируйте в .env как DEV_VIDEO_NOTE_ID")

# --- /photoid: выдать file_id примера (админ) ---
async def cmd_photoid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    m = update.message
    if m and m.reply_to_message and m.reply_to_message.photo:
        await m.reply_text(f"[ADMIN] example file_id: {m.reply_to_message.photo[-1].file_id}")
        return
    if m and m.photo:
        await m.reply_text(f"[ADMIN] example file_id: {m.photo[-1].file_id}")
        return
    await m.reply_text("Пришлите фото и ответьте на него командой /photoid (как reply).")

async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажмите /start.")

def main():
    log.info("run_polling... token prefix: %s******", (BOT_TOKEN or "")[:10])
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vnote", help_vnote))
    app.add_handler(CommandHandler("photoid", cmd_photoid))
    app.add_handler(CallbackQueryHandler(cb))

    # клиент присылает чек
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, receipts))
    # админ присылает кружок — получить file_id
    app.add_handler(MessageHandler(filters.VIDEO_NOTE & ~filters.COMMAND, detect_vnote))
    # админ загружает чек для клиента (после запроса)
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, admin_invoice_upload))
    # текст —fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    # --- Два одноразовых напоминания: T-48h и T-24h до конца акции ---
    if PROMO_END_ISO:
        try:
            tz = ZoneInfo(TIMEZONE)
            promo_end = datetime.fromisoformat(PROMO_END_ISO)
            if promo_end.tzinfo is None:
                promo_end = promo_end.replace(tzinfo=tz)

            t_minus_48 = promo_end - timedelta(hours=48)
            t_minus_24 = promo_end - timedelta(hours=24)
            now = datetime.now(promo_end.tzinfo)

            if t_minus_48 > now:
                app.job_queue.run_once(job_promo_countdown, when=t_minus_48, data=48, name="promo_Tminus48h")
                log.info("Scheduled T-48h at %s", t_minus_48.isoformat())

            if t_minus_24 > now:
                app.job_queue.run_once(job_promo_countdown, when=t_minus_24, data=24, name="promo_Tminus24h")
                log.info("Scheduled T-24h at %s", t_minus_24.isoformat())

        except Exception as e:
            log.warning("PROMO countdown schedule error: %s", e)

    app.run_polling()

if __name__ == "__main__":
    main()
