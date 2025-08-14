# main_cashier_bot.py
import os, json, secrets
from datetime import datetime, timedelta
from typing import Optional

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

load_dotenv()

# === ENV (обязательно заполнить .env) ===
BOT_TOKEN    = os.getenv("CASHIER_BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# Целевые боты
BOT_UNPACK = os.getenv("BOT_UNPACK", "jtbd_assistant_bot")
BOT_COPY   = os.getenv("BOT_COPY",   "content_helper_assist_bot")
BOT_PHOTO  = os.getenv("BOT_PHOTO",  "AIPromoPhotoBot")

# Юр-документы
POLICY_URL      = os.getenv("POLICY_URL")
OFFER_URL       = os.getenv("OFFER_URL")
ADS_CONSENT_URL = os.getenv("ADS_CONSENT_URL")

# Оплата на карту Т-Банка
PAY_PHONE   = os.getenv("PAY_PHONE", "+7XXXXXXXXXX")           # Номер телефона для СБП/Т-Банк
PAY_NAME    = os.getenv("PAY_NAME", "Ирина Александровна П.")  # Получатель
PAY_BANK    = os.getenv("PAY_BANK", "Т-Банк")                  # Отображаемое имя банка

# Кружок (video note): file_id (получите через отправку кружка админом — см. хендлер ниже)
DEV_VIDEO_NOTE_ID = os.getenv("DEV_VIDEO_NOTE_ID")  # например "AQAD...AAQ"
DEV_INFO = os.getenv("DEV_INFO", "Разработчик: заглушка.\nПоддержка: @your_username")

# TTL персональных ссылок (часы)
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "48"))

# Акция активна?
PROMO_ACTIVE = os.getenv("PROMO_ACTIVE", "true").lower() == "true"
# Акционные цены
PROMO_PRICES = {
    "unpack": 1890.00,
    "copy":   2490.00,
    "photo":  2490.00,
    "b12":    3990.00,
    "b13":    3790.00,
    "b23":    4490.00,
    "b123":   5990.00,
}

# Проверка .env
if not (BOT_TOKEN and ADMIN_ID and DATABASE_URL and POLICY_URL and OFFER_URL and ADS_CONSENT_URL):
    raise RuntimeError("Проверь .env: CASHIER_BOT_TOKEN, ADMIN_ID, DATABASE_URL, POLICY_URL, OFFER_URL, ADS_CONSENT_URL")

# === DB ===
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
cur.execute("""CREATE TABLE IF NOT EXISTS allowed_users(
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  PRIMARY KEY(user_id, bot_name)
);""")
# Запрос на чек от продавца
cur.execute("""CREATE TABLE IF NOT EXISTS invoice_requests(
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed BOOLEAN NOT NULL DEFAULT FALSE
);""")

# Каталог (базовые цены; при PROMO_ACTIVE применим PROMO_PRICES на этапе заказа)
CATALOG = {
    "unpack": {"title": "Бот №1 «Распаковка + Анализ ЦА (JTBD)»", "price": 2990.00,  "targets": [BOT_UNPACK]},
    "copy":   {"title": "Бот №2 «Твой личный контент-помощник»",  "price": 5490.00,  "targets": [BOT_COPY]},
    "photo":  {"title": "Бот №3 «Твой личный предметный фотограф»","price": 4490.00, "targets": [BOT_PHOTO]},
    "b12":    {"title": "Пакет 1+2",                              "price": 7990.00,  "targets": [BOT_UNPACK, BOT_COPY]},
    "b13":    {"title": "Пакет 1+3",                              "price": 6990.00,  "targets": [BOT_UNPACK, BOT_PHOTO]},
    "b23":    {"title": "Пакет 2+3",                              "price": 9490.00,  "targets": [BOT_COPY, BOT_PHOTO]},
    "b123":   {"title": "Пакет 1+2+3 (выгодно)",                  "price":11990.00,  "targets": [BOT_UNPACK, BOT_COPY, BOT_PHOTO]},
}
for code, p in CATALOG.items():
    cur.execute(
        """INSERT INTO products(code, title, price, targets)
           VALUES (%s,%s,%s,%s::jsonb)
           ON CONFLICT (code) DO UPDATE SET title=EXCLUDED.title, price=EXCLUDED.price, targets=EXCLUDED.targets""",
        (code, p["title"], p["price"], json.dumps(p["targets"]))
    )

# === Утилиты ===
def user_consented(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM consents WHERE user_id=%s", (user_id,))
    return cur.fetchone() is not None

def set_consent(user_id: int):
    cur.execute("INSERT INTO consents(user_id, accepted_at) VALUES(%s, now()) ON CONFLICT DO NOTHING", (user_id,))

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
        [InlineKeyboardButton("Оплатить Бота №1", callback_data="buy:unpack")],
        [InlineKeyboardButton("Оплатить Бота №2", callback_data="buy:copy")],
        [InlineKeyboardButton("Оплатить Бота №3", callback_data="buy:photo")],
        [InlineKeyboardButton("Пакет 1+2",        callback_data="buy:b12")],
        [InlineKeyboardButton("Пакет 1+3",        callback_data="buy:b13")],
        [InlineKeyboardButton("Пакет 2+3",        callback_data="buy:b23")],
        [InlineKeyboardButton("Пакет 1+2+3",      callback_data="buy:b123")],
    ])

PROMO_TEXT = (
    "🎁 Специальные стартовые цены\n"
    "(только 2 дня)\n\n"
    "🛠 Боты по отдельности\n"
    "• Бот №1 «Распаковка + Анализ ЦА» — 2 990 ₽ → 1 890 ₽ (выгода 1 100 ₽)\n"
    "• Бот №2 «Твой личный контент-помощник» — 5 490 ₽ → 2 490 ₽ (выгода 3 000 ₽)\n"
    "• Бот №3 «Твой личный предметный фотограф» — 4 490 ₽ → 2 490 ₽ (выгода 2 000 ₽)\n\n"
    "💎 Пакеты — ещё выгоднее\n"
    "• 1+2 — 7 990 ₽ → 3 990 ₽ (выгода 4 000 ₽)\n"
    "• 1+3 — 6 990 ₽ → 3 790 ₽ (выгода 3 200 ₽)\n"
    "• 2+3 — 8 490 ₽ → 4 490 ₽ (выгода 4 000 ₽)\n"
    "• 1+2+3 — 11 990 ₽ → 5 990 ₽ (выгода 6 000 ₽)\n\n"
    "📌 После окончания акции цены вырастут."
)
ABOUT_BOTS = (
    "Бот №1 «Распаковка + Анализ ЦА (JTBD)» — про понимание, что клиенты реально «покупают», и как под это подстроить позиционирование и контент.\n\n"
    "Бот №2 «Твой личный контент-помощник» — контент-план, посты, Reels/Stories, визуальные подсказки на основе распаковки.\n\n"
    "Бот №3 «Твой личный предметный фотограф» — генерирует продающие предметные фото из ваших снимков товара: фон/сцена/свет меняются, товар остаётся тем же."
)
CONSENT_TEXT = (
    "Перед оплатой подтвердите согласие с условиями:\n"
    f"• Политика конфиденциальности — {POLICY_URL}\n"
    f"• Договор оферты — {OFFER_URL}\n"
    f"• Согласие на получение рекламных материалов — {ADS_CONSENT_URL}\n\n"
    "Нажимая «✅ Согласен — перейти к оплате», вы принимаете условия."
)



# --- Examples block (screens only) ---
async def send_examples_screens(ctx, chat_id: int):
    """Send media group with example screenshots/videos stored as file_id in .env.
    ENV vars (fill what you need): EXAMPLE_1_ID..EXAMPLE_5_ID
    """
    ids = [
        os.getenv("EXAMPLE_1_ID"),
        os.getenv("EXAMPLE_2_ID"),
        os.getenv("EXAMPLE_3_ID"),
        os.getenv("EXAMPLE_4_ID"),
        os.getenv("EXAMPLE_5_ID"),
    ]
    media = []
    valid = [x for x in ids if x]
    for i, fid in enumerate(valid):
        try:
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption="Примеры ответов ботов"))
            else:
                media.append(InputMediaPhoto(media=fid))
        except Exception as e:
            print("[examples] bad file_id skipped:", e)
    if media:
        try:
            await ctx.bot.send_media_group(chat_id=chat_id, media=media)
        except Exception as e:
            print("[examples] send_media_group error:", e)

# === Handlers ===
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Кружок (если указан file_id)
    if DEV_VIDEO_NOTE_ID:
        try:
            await ctx.bot.send_video_note(chat_id=uid, video_note=DEV_VIDEO_NOTE_ID)
        except Exception:
            pass

    # Юр-гейт
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Согласен — перейти к оплате", callback_data="consent_ok")]
    ])
    await update.message.reply_text(CONSENT_TEXT, reply_markup=kb)
    # Инфо о разработчике (заглушка)
    await ctx.bot.send_message(chat_id=uid, text=DEV_INFO)

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "consent_ok":
        set_consent(uid)
        await q.edit_message_text(PROMO_TEXT)
        await ctx.bot.send_message(chat_id=uid, text="Выберите продукт для оформления заказа:", reply_markup=shop_keyboard())
        await ctx.bot.send_message(chat_id=uid, text=ABOUT_BOTS)
        # Reviews screenshots block
        await send_examples_screens(ctx, uid)
        return

    if q.data.startswith("buy:"):
        if not user_consented(uid):
            await q.answer("Сначала подтвердите согласие.", show_alert=True)
            return
        code = q.data.split(":", 1)[1]
        prod = get_product(code)
        if not prod:
            await q.edit_message_text("Продукт не найден.")
            return

        order_id = create_order(uid, code)
        price = current_price(code)

        # Инструкция по оплате на карту (без внешних ссылок)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Отправить чек по этому заказу", callback_data=f"send_receipt:{order_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="consent_ok")]
        ])
        await q.edit_message_text(
            f"🧾 <b>{prod['title']}</b>\n"
            f"Сумма к оплате: <b>{price:.2f} ₽</b>\n\n"
            f"Оплата по номеру телефона на карту {PAY_BANK}:\n"
            f"• Номер: <code>{PAY_PHONE}</code>\n"
            f"• Получатель: <b>{PAY_NAME}</b>\n"
            f"• Комментарий к переводу: <code>ORDER-{order_id}</code>\n\n"
            "После оплаты вернитесь и нажмите «📤 Отправить чек по этому заказу».",
            parse_mode="HTML",
            reply_markup=kb
        )
        # Ожидаем чек
        set_status(order_id, "await_receipt")
        return

    if q.data.startswith("send_receipt:"):
        order_id = int(q.data.split(":", 1)[1])
        order = get_order(order_id)
        if not order or order["user_id"] != uid:
            await q.edit_message_text("Заказ не найден.")
            return
        await q.edit_message_text(
            "Загрузите чек в ответ (фото/скан или документ PDF). "
            "После проверки пришлём персональные ссылки."
        )
        return

    if q.data.startswith("confirm:") or q.data.startswith("reject:") \
       or q.data.startswith("send_invoice:") or q.data.startswith("close_invoice:"):
        # Админские действия
        if uid != ADMIN_ID:
            await q.answer("Нет прав.", show_alert=True)
            return

        # Подтверждение/отклонение оплаты
        if q.data.startswith("confirm:") or q.data.startswith("reject:"):
            order_id = int(q.data.split(":", 1)[1])
            order = get_order(order_id)
            if not order:
                await q.edit_message_text("Заказ не найден.")
                return

            if q.data.startswith("reject:"):
                set_status(order_id, "rejected")
                await q.edit_message_text(f"Заказ #{order_id}: отклонён.")
                try:
                    await ctx.bot.send_message(order["user_id"], "Оплата не подтверждена. Если это ошибка — напишите нам.")
                except Exception:
                    pass
                return

            # confirm
            set_status(order_id, "paid")
            prod = get_product(order["product_code"])
            links = gen_tokens_with_ttl(order["user_id"], prod["targets"], TOKEN_TTL_HOURS)

            warn = (
                "⚠️ Ссылки индивидуальные. Они действуют ограниченное время "
                f"(~{TOKEN_TTL_HOURS} ч) и перестают работать после активации."
            )
            # ссылки кнопками
            btns = [[InlineKeyboardButton(f"Открыть @{bn}", url=link)] for bn, link in links]
            btns.append([InlineKeyboardButton("🧾 Запросить чек от продавца", callback_data=f"request_invoice:{order['id']}")])
            await q.edit_message_text(f"Заказ #{order_id}: подтверждён. Ссылки отправлены.")
            try:
                await ctx.bot.send_message(
                    chat_id=order["user_id"],
                    text="🎉 Доступ активирован!\n\n" + warn,
                    reply_markup=InlineKeyboardMarkup(btns)
                )
            except Exception:
                pass
            return

        # Отправка чека клиенту (после запроса)
        if q.data.startswith("send_invoice:"):
            order_id = int(q.data.split(":", 1)[1])
            cur.execute("UPDATE invoice_requests SET closed=FALSE WHERE order_id=%s", (order_id,))
            # даём подсказку администратору
            await q.edit_message_text(
                f"Загрузка чека для клиента по заказу #{order_id}.\n"
                "Отправьте документ/фото в этот чат — я перешлю покупателю.\n"
                "После отправки нажмите «Закрыть запрос».",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Закрыть запрос", callback_data=f"close_invoice:{order_id}")]
                ])
            )
            return

        if q.data.startswith("close_invoice:"):
            order_id = int(q.data.split(":", 1)[1])
            cur.execute("UPDATE invoice_requests SET closed=TRUE WHERE order_id=%s", (order_id,))
            await q.edit_message_text(f"Запрос на чек по заказу #{order_id} закрыт.")
            return

    if q.data.startswith("request_invoice:"):
        # это вызов с пользовательской кнопки после подтверждения
        order_id = int(q.data.split(":", 1)[1])
        order = get_order(order_id)
        if not order or order["user_id"] != uid:
            await q.answer("Заказ не найден.", show_alert=True)
            return
        # создаём/открываем запрос
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
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🧾 Запрос чека по заказу #{order_id}\nПокупатель: {uid}",
                reply_markup=kb_admin
            )
        except Exception:
            pass
        await q.answer("Запрос на чек отправлен. Ждём файл от продавца.", show_alert=True)
        return

async def receipts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Покупатель отправляет чек (фото/документ) при статусе await_receipt."""
    uid = update.effective_user.id
    # найдём последний заказ в ожидании чека
    cur.execute(
        "SELECT id FROM orders WHERE user_id=%s AND status='await_receipt' ORDER BY id DESC LIMIT 1",
        (uid,)
    )
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
        return

    cur.execute("INSERT INTO receipts(order_id, file_id, file_type) VALUES(%s,%s,%s)", (order_id, file_id, file_type))
    set_status(order_id, "pending")

    kb_admin = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{order_id}"),
         InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject:{order_id}")]
    ])
    caption = f"💳 Чек по заказу #{order_id}\nПокупатель: {uid}"
    try:
        if file_type == "photo":
            await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb_admin)
        else:
            await ctx.bot.send_document(chat_id=ADMIN_ID, document=file_id, caption=caption, reply_markup=kb_admin)
    except Exception:
        pass

    await update.message.reply_text("Спасибо! Чек отправлен на проверку. Обычно подтверждение занимает несколько минут.")

async def admin_invoice_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ отправляет файл чека клиенту в ответ на запрос (ищем последний открытый запрос)."""
    if update.effective_user.id != ADMIN_ID:
        return
    # берём последний открытый запрос
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
        file_id, is_photo = update.message.photo[-1].file_id, True
    elif update.message.document:
        file_id = update.message.document.file_id
    if not file_id:
        return

    # Пересылаем покупателю
    try:
        if is_photo:
            await ctx.bot.send_photo(chat_id=order["user_id"], photo=file_id, caption="🧾 Чек от продавца")
        else:
            await ctx.bot.send_document(chat_id=order["user_id"], document=file_id, caption="🧾 Чек от продавца")
        # Закрываем запрос
        cur.execute("UPDATE invoice_requests SET closed=TRUE WHERE order_id=%s", (order_id,))
        await update.message.reply_text(f"Чек отправлен покупателю (заказ #{order_id}). Запрос закрыт.")
    except Exception:
        pass

async def help_vnote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ может прислать кружок — бот вернёт file_id (чтобы занести в .env)."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Пришлите кружок (video note) — верну file_id.")

async def detect_vnote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Если админ прислал video note — вернуть его file_id (для DEV_VIDEO_NOTE_ID)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if update.message.video_note:
        file_id = update.message.video_note.file_id
        await update.message.reply_text(f"file_id кружка: {file_id}\nСкопируйте в .env как DEV_VIDEO_NOTE_ID")

async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажмите /start.")


async def grab_review_photo_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sends a photo to get its file_id for reviews.
    Skips if there is an open invoice request to avoid collision with admin_invoice_upload.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    # Skip if we are in the middle of invoice upload flow
    cur.execute("SELECT 1 FROM invoice_requests WHERE closed=FALSE ORDER BY id DESC LIMIT 1")
    if cur.fetchone():
        return
    if update.message and update.message.photo:
        fid = update.message.photo[-1].file_id
        await update.message.reply_text(f"[ADMIN] example file_id: {fid}")


async def cmd_photoid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Reply with /photoid to a photo to get its file_id."""
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message
    if not msg:
        return
    # Reply-mode preferred
    if msg.reply_to_message and msg.reply_to_message.photo:
        fid = msg.reply_to_message.photo[-1].file_id
        await msg.reply_text(f"[ADMIN] example file_id: {fid}")
        return
    # Fallback: command sent with attached photo (rare)
    if msg.photo:
        fid = msg.photo[-1].file_id
        await msg.reply_text(f"[ADMIN] example file_id: {fid}")
        return
    await msg.reply_text("Пришлите фото и ответьте на него командой /photoid (как reply).")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vnote", help_vnote))  # подсказка по кружку
    app.add_handler(CommandHandler("photoid", cmd_photoid))
    app.add_handler(CallbackQueryHandler(cb))

    # Admin: grab photo file_id for reviews
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, grab_review_photo_id))

    # Покупатель присылает чек (фото/док)
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, receipts))
    # Админ присылает кружок — получить file_id
    app.add_handler(MessageHandler(filters.VIDEO_NOTE & ~filters.COMMAND, detect_vnote))
    # Админ заливает чек для клиента (после запроса)
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, admin_invoice_upload))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))
    app.run_polling()

if __name__ == "__main__":
    main()
