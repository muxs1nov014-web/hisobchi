# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import time
import threading
import urllib.request
import urllib.parse
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Fayl yo'llari
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pul.db")
TOKEN_PATH = os.path.join(BASE_DIR, "token.txt")
FON_PATH = os.path.join(BASE_DIR, "fon.jpg")

# SQLite ma'lumotlar bazasini sozlash
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

# 1. Kirim-chiqim jadvali
db.execute("""CREATE TABLE IF NOT EXISTS t(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user INTEGER,
    amount INTEGER,
    note TEXT,
    currency TEXT DEFAULT 'UZS',
    date TEXT DEFAULT (datetime('now','+5 hours'))
)""")

try:
    db.execute("ALTER TABLE t ADD COLUMN currency TEXT DEFAULT 'UZS'")
    db.commit()
except Exception:
    pass

# 2. Qarz daftari jadvali
db.execute("""CREATE TABLE IF NOT EXISTS debts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user INTEGER,
    debt_type TEXT,
    name TEXT,
    amount INTEGER,
    note TEXT,
    currency TEXT DEFAULT 'UZS',
    is_closed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','+5 hours')),
    closed_at TEXT
)""")

try:
    db.execute("ALTER TABLE debts ADD COLUMN currency TEXT DEFAULT 'UZS'")
    db.commit()
except Exception:
    pass

# 3. Foydalanuvchilar jadvali (barcha foydalanuvchilarni kuzatish uchun)
db.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    created_at TEXT DEFAULT (datetime('now','+5 hours')),
    last_active TEXT DEFAULT (datetime('now','+5 hours'))
)""")

# 4. Adminlar jadvali (rahbar va boshqaruvchilar)
db.execute("""CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now','+5 hours'))
)""")

# Mavjud tranzaksiyalar va qarzdorliklardagi foydalanuvchilarni saqlab qolish (Zero Data Loss)
try:
    db.execute("""
        INSERT OR IGNORE INTO users (user_id, first_name, last_name, username, created_at, last_active)
        SELECT DISTINCT user, 'Foydalanuvchi ' || user, '', '', datetime('now', '+5 hours'), datetime('now', '+5 hours')
        FROM t WHERE user IS NOT NULL
        UNION
        SELECT DISTINCT user, 'Foydalanuvchi ' || user, '', '', datetime('now', '+5 hours'), datetime('now', '+5 hours')
        FROM debts WHERE user IS NOT NULL
    """)
    for adm_id in [8042453163, 874784622]:
        db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (adm_id,))
    db.commit()
except Exception:
    pass

db.commit()

# Admin ID lari (Asosiy rahbar)
ADMIN_IDS = {8042453163, 874784622}


def is_admin(user_id: int) -> bool:
    """Foydalanuvchi admin yoki rahbar ekanligini tekshirish"""
    if user_id in ADMIN_IDS:
        return True
    try:
        row = db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row)
    except Exception:
        return False


def track_user(user):
    """Foydalanuvchi ma'lumotlarini bazada xavfsiz saqlash va yangilash"""
    if not user:
        return
    try:
        fn = user.first_name or ""
        ln = user.last_name or ""
        un = user.username or ""
        db.execute("""
            INSERT INTO users(user_id, first_name, last_name, username, created_at, last_active)
            VALUES(?, ?, ?, ?, datetime('now', '+5 hours'), datetime('now', '+5 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = CASE WHEN excluded.first_name != '' THEN excluded.first_name ELSE users.first_name END,
                last_name = CASE WHEN excluded.last_name != '' THEN excluded.last_name ELSE users.last_name END,
                username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                last_active = datetime('now', '+5 hours')
        """, (user.id, fn, ln, un))
        db.commit()
    except Exception:
        pass


def fmt(n: int) -> str:
    """Raqamlarni formatlash: 1 000 000"""
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def fmt_money(amount: int, currency: str = "UZS") -> str:
    """Valyutani chiroyli ko'rsatish"""
    val = fmt(abs(amount))
    if currency == "USD":
        return f"${val}"
    return f"{val} so'm"


def format_date(dt_str: str) -> str:
    """Sanani ixcham ko'rinishga keltirish"""
    if not dt_str:
        return ""
    try:
        parts = dt_str.split(" ")
        ymd = parts[0].split("-")
        day_month = f"{ymd[2]}.{ymd[1]}"
        time_part = parts[1][:5] if len(parts) > 1 else ""
        return f"{day_month} {time_part}".strip()
    except Exception:
        return dt_str[:16]


def get_totals_by_currency(user_id: int, where_clause: str = "", params: tuple = ()) -> dict:
    """Kirim va chiqimlar yig'indisi"""
    q = f"""SELECT 
               COALESCE(currency, 'UZS') AS cur,
               COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS doh,
               COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS rash
            FROM t 
            WHERE user = ? {where_clause}
            GROUP BY COALESCE(currency, 'UZS')"""
    rows = db.execute(q, (user_id, *params)).fetchall()

    totals = {
        "UZS": {"doh": 0, "rash": 0, "bal": 0},
        "USD": {"doh": 0, "rash": 0, "bal": 0}
    }
    for r in rows:
        cur = r["cur"] if r["cur"] in ["UZS", "USD"] else "UZS"
        d = int(r["doh"])
        ra = int(r["rash"])
        totals[cur] = {"doh": d, "rash": ra, "bal": d - ra}
    return totals


def save_transaction(user_id: int, amount: int, note: str = "-", currency: str = "UZS") -> int:
    """Kirim/chiqimni bazaga saqlash"""
    cur = db.execute(
        "INSERT INTO t(user, amount, note, currency) VALUES(?, ?, ?, ?)",
        (user_id, amount, note or "-", currency)
    )
    db.commit()
    return cur.lastrowid


# Boshqaruv tugmalari (Asosiy menyu)
MAIN_KEYBOARD = [
    [KeyboardButton("➕ Доход / Kirim"), KeyboardButton("➖ Расход / Chiqim")],
    [KeyboardButton("📕 Qarz daftari (Долги)")],
    [KeyboardButton("📊 Общий отчет"), KeyboardButton("📅 Отчет за сегодня")],
    [KeyboardButton("📋 История (Tarix)"), KeyboardButton("🗑 Отменить последнее")],
    [KeyboardButton("🖼 Chiroyli Fon"), KeyboardButton("ℹ️ Помощь / Yordam")]
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)


def get_main_markup(user_id: int) -> ReplyKeyboardMarkup:
    """Adminlar uchun boshqaruv tugmasi (👑 Admin Panel) qo'shilgan asosiy menyu"""
    kb = [
        [KeyboardButton("➕ Доход / Kirim"), KeyboardButton("➖ Расход / Chiqim")],
        [KeyboardButton("📕 Qarz daftari (Долги)")],
        [KeyboardButton("📊 Общий отчет"), KeyboardButton("📅 Отчет за сегодня")],
        [KeyboardButton("📋 История (Tarix)"), KeyboardButton("🗑 Отменить последнее")],
        [KeyboardButton("🖼 Chiroyli Fon"), KeyboardButton("ℹ️ Помощь / Yordam")]
    ]
    if is_admin(user_id):
        kb.append([KeyboardButton("👑 Admin Panel (Boshqaruv)")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


# Qarz daftari menyusi
DEBT_KEYBOARD = [
    [KeyboardButton("🟢 Qarz berdim (Menga berishadi)"), KeyboardButton("🔴 Qarz oldim (Men berishim kerak)")],
    [KeyboardButton("📋 Qarzdorlar ro'yxati"), KeyboardButton("✅ Qarzni yopish")],
    [KeyboardButton("📢 Qarzdorga ogohlantirish yuborish")],
    [KeyboardButton("🔙 Asosiy menyu")]
]
DEBT_REPLY_MARKUP = ReplyKeyboardMarkup(DEBT_KEYBOARD, resize_keyboard=True)

# Admin paneli menyusi
ADMIN_KEYBOARD = [
    [KeyboardButton("👥 Barcha foydalanuvchilar"), KeyboardButton("📊 Umumiy kassa")],
    [KeyboardButton("🔍 Foydalanuvchini ko'rish"), KeyboardButton("📢 Hammaga xabar")],
    [KeyboardButton("🔙 Asosiy menyu")]
]
ADMIN_REPLY_MARKUP = ReplyKeyboardMarkup(ADMIN_KEYBOARD, resize_keyboard=True)


def make_debt_share_url(name: str, amount: int, curr: str, note: str = "-") -> str:
    """Qarzdorga Telegram orqali yuborish uchun rasmiy OGOHLANTIRISH matni va havolasi"""
    money_str = fmt_money(amount, curr)
    clean_name = name.lstrip("@").strip()
    msg = (
        f"⚠️ DIQQAT! QARZ BILDIRISHNOMASI 📢\n\n"
        f"Hurmatli {clean_name}!\n"
        f"Sizning hisobingizda quyidagi qarz mavjudligi to'g'risida OGOHLANTIRISH:\n\n"
        f"💰 Qarz miqdori: {money_str}\n"
    )
    if note and note != "-":
        msg += f"📝 Sababi / Izoh: {note}\n"
    msg += "\nIltimos, ushbu qarzni o'z vaqtida to'lab, hisob-kitobni yopishingizni so'raymiz! 🤝"
    return f"https://t.me/share/url?text={urllib.parse.quote(msg)}"


def parse_natural_uzbek_text(raw_text: str):
    """Jonli tildagi xabarni avtomatik tushunish"""
    text = raw_text.strip()

    explicit_sign = None
    s_match = re.match(r"^([+-])\s*(.*)$", text)
    if s_match:
        explicit_sign = s_match.group(1)
        text = s_match.group(2).strip()

    is_usd = bool(re.search(r"(\$|dollar|dollor|usd|доллар|доллор|\bбакс\b)", text, re.IGNORECASE))
    currency = "USD" if is_usd else "UZS"

    m = re.search(r"(\d[\d\s.,]*)\s*(k|к|ming|минг|мин)?", text, re.IGNORECASE)
    if not m:
        return None, currency, "", None

    num_str = m.group(1)
    suffix_mult = m.group(2)
    digits = re.sub(r"\D", "", num_str)
    if not digits:
        return None, currency, "", None

    amount = int(digits)
    if suffix_mult and amount < 1000:
        amount *= 1000

    start_pos = m.start()
    end_pos = m.end()
    remainder = text[end_pos:]
    remainder_cleaned = re.sub(r"^(га|ga|ка|ka|қа|qa|да|da|дан|dan|ни|ni)\b\s*", "", remainder, flags=re.IGNORECASE)

    note_raw = (text[:start_pos] + " " + remainder_cleaned).strip()
    note_clean = re.sub(r"(\$|dollar|dollor|usd|доллар|доллор|\bбакс\b|so['`ʼ]?m|som|сум|sum|uzs)", "", note_raw, flags=re.IGNORECASE).strip()
    note_clean = re.sub(r"^[\s,.-]+|[\s,.-]+$", "", note_clean).strip()
    note_clean = re.sub(r"\s+", " ", note_clean)
    if not note_clean:
        note_clean = "-"

    lower_text = raw_text.lower()
    expense_patterns = [
        r"\b(quy|қуй|куй)(dim|dik|д|дик|дим)?\b",
        r"\b(ol|ол)(dim|dik|дим|дик)?\b",
        r"\b(ber|бер)(dim|dik|дим|дик)?\b",
        r"\b(to['`ʼ]?la|тула|тўла)(dim|dik|дим|дик)?\b",
        r"\b(sarf|сарф)(ladim|ладик)?\b",
        r"\b(ket|кет)(di|дик)?\b",
        r"\b(harid|xarid|харид)\b",
        r"\b(taksi|такси|bozor|бозор|tushlik|тушлик|obed|обед|ovqat|овқат|овкат|gaz|газ|benzin|бензин|svet|свет|arenda|аренда|ijara|ижара|dorilar?|дори|atir|атир|go['`ʼ]?sht|гушт|гўшт|un|ун|moy|мой|non|нон|kartoshka|пиёз|piyoz)\b",
        r"\b(купил|потратил|отдал|заправил|оплатил|расход)\b"
    ]
    income_patterns = [
        r"\b(tush|туш)(di|ган)?\b",
        r"\b(kel|кел)(di)?\b",
        r"\b(ishla|ишла)(dim|дик)?\b",
        r"\b(top|топ)(dim|дик)?\b",
        r"\b(qayt|қайт|кайт)(di|ardi|арди)?\b",
        r"\b(berish|бериш)(di|gan)?\b",
        r"\b(oylik|ойлик|maosh|маош|avans|аванс|daromad|даромад|foyda|фойда|tushum|тушум|kassa|касса)\b",
        r"\b(зарплата|доход|приход|получка|получил)\b"
    ]

    is_exp = any(re.search(p, lower_text) for p in expense_patterns)
    is_inc = any(re.search(p, lower_text) for p in income_patterns)

    if explicit_sign == "+":
        inferred_type = "kirim"
    elif explicit_sign == "-":
        inferred_type = "chiqim"
    elif is_exp and not is_inc:
        inferred_type = "chiqim"
    elif is_inc and not is_exp:
        inferred_type = "kirim"
    else:
        inferred_type = None

    return amount, currency, note_clean, inferred_type


def parse_debt_input(text: str) -> tuple[str, int, str, str]:
    """Qarz matnidan ism, summa, valyuta va izohni ajratish"""
    raw = text.strip()
    is_usd = bool(re.search(r"(\$|dollar|dollor|usd|доллар|доллор|\bбакс\b)", raw, re.IGNORECASE))
    currency = "USD" if is_usd else "UZS"

    m = re.search(r"(\d[\d\s.,]*)", raw)
    if not m:
        return "", 0, currency, ""

    num_str = m.group(1)
    digits = re.sub(r"\D", "", num_str)
    if not digits:
        return "", 0, currency, ""
    amount = int(digits)

    start_idx = m.start()
    end_idx = m.end()
    before = raw[:start_idx].strip()
    after = raw[end_idx:].strip()

    def clean_curr(s):
        s = re.sub(r"(\$|dollar|dollor|usd|доллар|доллор|\bбакс\b|so['`ʼ]?m|som|сум|sum|uzs)", "", s, flags=re.IGNORECASE)
        return re.sub(r"^[\s,.-]+|[\s,.-]+$", "", s).strip()

    before_clean = clean_curr(before)
    after_clean = clean_curr(after)

    if before_clean:
        name = before_clean
        note = after_clean or "-"
    elif after_clean:
        parts = after_clean.split(maxsplit=1)
        name = parts[0]
        note = parts[1] if len(parts) > 1 else "-"
    else:
        name = "Noma'lum"
        note = "-"

    return name, amount, currency, note


async def send_summary(update: Update, user_id: int, title: str, where_clause: str = "", params: tuple = ()):
    """So'm va Dollar alohida ko'rsatilgan hisobot"""
    totals = get_totals_by_currency(user_id, where_clause, params)
    uzs = totals["UZS"]
    usd = totals["USD"]

    icon_uzs = "💰" if uzs["bal"] >= 0 else "🔻"
    sign_uzs = "+" if uzs["bal"] > 0 else ""

    icon_usd = "💰" if usd["bal"] >= 0 else "🔻"
    sign_usd = "+" if usd["bal"] > 0 else ""

    text = (
        f"{title}\n\n"
        f"🇺🇿 <b>SO'MDA:</b>\n"
        f"🟢 Kirim: {fmt(uzs['doh'])} so'm\n"
        f"🔴 Chiqim: {fmt(uzs['rash'])} so'm\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon_uzs} <b>Qoldiq:</b> {sign_uzs}{fmt(uzs['bal'])} so'm\n\n"
        f"🇺🇸 <b>DOLLARDA ($):</b>\n"
        f"🟢 Kirim: ${fmt(usd['doh'])}\n"
        f"🔴 Chiqim: ${fmt(usd['rash'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon_usd} <b>Qoldiq:</b> {sign_usd}${fmt(usd['bal'])}"
    )

    markup = get_main_markup(user_id)
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def send_wallpaper_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fon rasmini yuborish va o'rnatish yo'riqnomasi"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    caption = (
        "🖼 <b>Mana bu botingiz uchun maxsus 3D fon rasmi!</b>\n\n"
        "Buni ushbu chatning orqa foni (обои) qilib qo'yish uchun:\n"
        "1️⃣ Rasmni bosing\n"
        "2️⃣ Yuqori o'ng burchakdagi 3 nuqta (<b>⋮</b>) ni bosing\n"
        "3️⃣ <b>«Установить как обои»</b> (Fon qilib o'rnatish) ni tanlang!\n\n"
        "<i>Shunda butun chat foni zamonaviy va chiroyli bo'ladi! ✨</i>"
    )
    markup = get_main_markup(uid)
    if os.path.exists(FON_PATH):
        try:
            with open(FON_PATH, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption, parse_mode="HTML", reply_markup=markup)
                return
        except Exception:
            pass
    await update.message.reply_text("Fon rasmi topilmadi.", reply_markup=markup)


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/start buyrug'i"""
    ctx.user_data.clear()
    user = update.effective_user
    track_user(user)
    uid = user.id
    user_name = user.first_name or "Foydalanuvchi"

    markup = get_main_markup(uid)
    text = (
        f"Assalomu alaykum, {user_name}! 👋\n\n"
        f"Men shaxsiy <b>Hisob-Kitob va Qarz Daftari</b> botingizman.\n\n"
        f"🎙 <b>Nima qilganingizni yozing — bot tushunib oladi!</b>\n"
        f"• <code>72,000мин газ кудим</code> ➔ Chiqim: 72 000 so'm (газ кудим)\n"
        f"• <code>200,000га атир олдим</code> ➔ Chiqim: 200 000 so'm (атир олдим)\n"
        f"• <code>500000 oylik tushdi</code> ➔ Kirim: 500 000 so'm (oylik tushdi)\n"
        f"• <code>100$ benzin</code> ➔ Chiqim: $100 (benzin)\n\n"
        f"🖼 <i>Orqa fon rasmini o'rnatish uchun: «🖼 Chiroyli Fon» tugmasini bosing.</i>\n\n"
        f"👇 Kerakli bo'limni quyidagi tugmalardan tanlang:"
    )
    if os.path.exists(FON_PATH):
        try:
            with open(FON_PATH, "rb") as photo:
                await update.message.reply_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=markup)
                return
        except Exception:
            pass
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help buyrug'i"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    text = (
        "ℹ️ <b>Qanday yozish mumkin:</b>\n\n"
        "Oddiy gap bilan yozsangiz ham bot nomi bilan tushunib oladi:\n"
        "• <code>72 000 газ куйдим</code>\n"
        "• <code>200,000га атир олдим</code>\n"
        "• <code>35000 обедга кетди</code>\n"
        "• <code>500000 маош тушди</code>\n"
        "• <code>50$ бозорлик қилдим</code>\n\n"
        "📋 <b>«История (Tarix)»</b> бўлимида чиқимларингиз номи билан чиқади ва пастида <b>ЖАМИ ЧИҚИМ</b> ҳисоблаб берилади!\n"
        "🖼 <b>«Chiroyli Fon»</b> бўлимида чат учун махсус 3D фон расмини ўрнатишингиз мумкин."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_markup(uid))


async def show_debt_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qarz daftari menyusi"""
    ctx.user_data.clear()
    track_user(update.effective_user)
    text = (
        "📕 <b>QARZ DAFTARI (So'm va Dollar)</b>\n\n"
        "Bergan va olgan qarzlaringizni hisoblab boring:\n\n"
        "🟢 <b>Qarz berdim</b> — kimgadir qarz berganingizni yozish\n"
        "🔴 <b>Qarz oldim</b> — birovdan qarz olganingizni yozish\n"
        "📋 <b>Qarzdorlar ro'yxati</b> — kim qancha qarzdorligini ko'rish\n"
        "✅ <b>Qarzni yopish</b> — to'langan qarzni yopish\n"
        "📢 <b>Qarzdorga ogohlantirish</b> — qarzdorga Telegram orqali rasmiy qarz ogohlantirishini yuborish!\n\n"
        "👇 Kerakli amalni tanlang:"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=DEBT_REPLY_MARKUP)


async def show_debt_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qarzdorlar ro'yxati"""
    uid = update.effective_user.id
    track_user(update.effective_user)

    berdim_rows = db.execute(
        "SELECT id, name, amount, currency, note, created_at FROM debts WHERE user = ? AND debt_type = 'berdim' AND is_closed = 0 ORDER BY id DESC",
        (uid,)
    ).fetchall()

    oldim_rows = db.execute(
        "SELECT id, name, amount, currency, note, created_at FROM debts WHERE user = ? AND debt_type = 'oldim' AND is_closed = 0 ORDER BY id DESC",
        (uid,)
    ).fetchall()

    if not berdim_rows and not oldim_rows:
        await update.message.reply_text("📋 Hozirda faol qarzlar yo'q. Qarz daftari bo'sh!", reply_markup=DEBT_REPLY_MARKUP)
        return

    lines = ["📕 <b>QARZLAR HISOBOTI:</b>\n"]

    if berdim_rows:
        lines.append("🟢 <b>MENGA QAYTARISHI KERAK (Mening haqim):</b>")
        berdim_uzs = [r for r in berdim_rows if (r["currency"] or "UZS") == "UZS"]
        berdim_usd = [r for r in berdim_rows if r["currency"] == "USD"]

        if berdim_uzs:
            tot_uzs = sum(r["amount"] for r in berdim_uzs)
            lines.append("🇺🇿 <i>So'mda:</i>")
            for r in berdim_uzs:
                dt = format_date(r["created_at"])
                lines.append(f"  <b>#{r['id']}</b> | <b>{r['name']}</b>: {fmt(r['amount'])} so'm | <i>{r['note']}</i> | 🕒 {dt}")
            lines.append(f"  👉 <b>Jami so'mda: {fmt(tot_uzs)} so'm</b>\n")

        if berdim_usd:
            tot_usd = sum(r["amount"] for r in berdim_usd)
            lines.append("🇺🇸 <i>Dollarda ($):</i>")
            for r in berdim_usd:
                dt = format_date(r["created_at"])
                lines.append(f"  <b>#{r['id']}</b> | <b>{r['name']}</b>: ${fmt(r['amount'])} | <i>{r['note']}</i> | 🕒 {dt}")
            lines.append(f"  👉 <b>Jami dollarda: ${fmt(tot_usd)}</b>\n")

    if oldim_rows:
        lines.append("🔴 <b>MENING QARZLARIM (Men to'lashim kerak):</b>")
        oldim_uzs = [r for r in oldim_rows if (r["currency"] or "UZS") == "UZS"]
        oldim_usd = [r for r in oldim_rows if r["currency"] == "USD"]

        if oldim_uzs:
            tot_uzs = sum(r["amount"] for r in oldim_uzs)
            lines.append("🇺🇿 <i>So'mda:</i>")
            for r in oldim_uzs:
                dt = format_date(r["created_at"])
                lines.append(f"  <b>#{r['id']}</b> | <b>{r['name']}</b>: {fmt(r['amount'])} so'm | <i>{r['note']}</i> | 🕒 {dt}")
            lines.append(f"  👉 <b>Jami so'mda: {fmt(tot_uzs)} so'm</b>\n")

        if oldim_usd:
            tot_usd = sum(r["amount"] for r in oldim_usd)
            lines.append("🇺🇸 <i>Dollarda ($):</i>")
            for r in oldim_usd:
                dt = format_date(r["created_at"])
                lines.append(f"  <b>#{r['id']}</b> | <b>{r['name']}</b>: ${fmt(r['amount'])} | <i>{r['note']}</i> | 🕒 {dt}")
            lines.append(f"  👉 <b>Jami dollarda: ${fmt(tot_usd)}</b>\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>Qarz yopilganda «✅ Qarzni yopish» tugmasini bosing yoki: <code>/qarz_yop ID</code></i>")

    extra_kb = None
    if berdim_rows:
        extra_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Qarzdorlarga ogohlantirish yuborish", callback_data="open_sms_reminders")]
        ])

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=extra_kb or DEBT_REPLY_MARKUP)


async def send_debt_sms_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qarzdorlarga OGOHLANTIRISH / SMS yuborish ro'yxati"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    rows = db.execute(
        "SELECT id, name, amount, currency, note FROM debts WHERE user = ? AND debt_type = 'berdim' AND is_closed = 0 ORDER BY id DESC",
        (uid,)
    ).fetchall()

    if not rows:
        await update.message.reply_text(
            "📋 Hozirda sizdan qarz olgan faol qarzdorlar yo'q. Qarz daftari bo'sh!",
            reply_markup=DEBT_REPLY_MARKUP
        )
        return

    buttons = []
    for r in rows:
        curr = r["currency"] or "UZS"
        money_str = fmt_money(r["amount"], curr)
        share_url = make_debt_share_url(r["name"], r["amount"], curr, r["note"])
        btn_text = f"📢 {r['name']} ({money_str}) ga ogohlantirish"
        buttons.append([InlineKeyboardButton(btn_text, url=share_url)])

    await update.message.reply_text(
        "📢 <b>Qaysi qarzdorga qarz borligi haqida OGOHLANTIRISH (SMS) yubormoqchisiz?</b>\n\n"
        "👇 Kerakli odamni tanlang — Telegram darhol rasmiy ogohlantirish xabarini tayyorlab beradi:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def close_debt_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Qaysi qarzni yopishni tanlash"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    rows = db.execute(
        "SELECT id, debt_type, name, amount, currency FROM debts WHERE user = ? AND is_closed = 0 ORDER BY id DESC",
        (uid,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("Hozirda yopish uchun ochiq qarzlar yo'q.", reply_markup=DEBT_REPLY_MARKUP)
        return

    buttons = []
    for r in rows:
        icon = "🟢" if r["debt_type"] == "berdim" else "🔴"
        curr = r["currency"] or "UZS"
        money_str = fmt_money(r["amount"], curr)
        btn_text = f"{icon} #{r['id']} {r['name']} — {money_str}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"close_debt:{r['id']}")])

    buttons.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_close_debt")])

    await update.message.reply_text(
        "✅ <b>Qaysi qarz qaytarildi / yopildi?</b>\nQuyidagi ro'yxatdan tanlang:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ochir_oxirgisi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Oxirgi yozuvni o'chirish"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    last_row = db.execute(
        "SELECT id, amount, currency, note FROM t WHERE user = ? ORDER BY id DESC LIMIT 1",
        (uid,)
    ).fetchone()

    markup = get_main_markup(uid)
    if not last_row:
        await update.message.reply_text("O'chirish uchun kirim-chiqim yozuvi topilmadi.", reply_markup=markup)
        return

    db.execute("DELETE FROM t WHERE id = ? AND user = ?", (last_row["id"], uid))
    db.commit()

    is_doh = last_row["amount"] > 0
    icon = "🟢 +" if is_doh else "🔴 -"
    curr = last_row["currency"] or "UZS"
    text = (
        f"🗑 <b>Oxirgi yozuv muvaffaqiyatli o'chirildi!</b>\n\n"
        f"<b>#{last_row['id']}</b> | {icon}{fmt_money(last_row['amount'], curr)} (<i>{last_row['note']}</i>)"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tarixni ko'rsatish va oxirida CHIQIMLAR JAMINI hisoblab chiqarish"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    markup = get_main_markup(uid)

    rows = db.execute(
        "SELECT id, amount, currency, note, date FROM t WHERE user = ? ORDER BY id DESC LIMIT 15",
        (uid,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📋 Hozircha hech qanday yozuv yo'q.", reply_markup=markup)
        return

    lines = ["📋 <b>OXIRGI AMALLAR TARIXI:</b>\n"]

    shown_chiqim_uzs = 0
    shown_chiqim_usd = 0
    shown_kirim_uzs = 0
    shown_kirim_usd = 0

    for r in rows:
        amt = r["amount"]
        curr = r["currency"] or "UZS"
        dt = format_date(r["date"])

        if amt < 0:
            icon = "🔴 -"
            if curr == "USD":
                shown_chiqim_usd += abs(amt)
            else:
                shown_chiqim_uzs += abs(amt)
        else:
            icon = "🟢 +"
            if curr == "USD":
                shown_kirim_usd += amt
            else:
                shown_kirim_uzs += amt

        lines.append(f"<b>#{r['id']}</b> | {icon}{fmt_money(amt, curr)} | <b>{r['note']}</b> | 🕒 {dt}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔴 <b>OXIRGI CHIQIMLAR JAMI (ЖАМИ ЧИҚИМ):</b>")
    if shown_chiqim_uzs > 0:
        lines.append(f"🇺🇿 So'mda: <b>{fmt(shown_chiqim_uzs)} so'm</b>")
    if shown_chiqim_usd > 0:
        lines.append(f"🇺🇸 Dollarda: <b>${fmt(shown_chiqim_usd)}</b>")
    if shown_chiqim_uzs == 0 and shown_chiqim_usd == 0:
        lines.append("<i>Chiqimlar yo'q</i>")

    if shown_kirim_uzs > 0 or shown_kirim_usd > 0:
        lines.append("\n🟢 <b>OXIRGI KIRIMLAR JAMI:</b>")
        if shown_kirim_uzs > 0:
            lines.append(f"🇺🇿 So'mda: <b>{fmt(shown_kirim_uzs)} so'm</b>")
        if shown_kirim_usd > 0:
            lines.append(f"🇺🇸 Dollarda: <b>${fmt(shown_kirim_usd)}</b>")

    all_totals = get_totals_by_currency(uid)
    tot_rash_uzs = all_totals["UZS"]["rash"]
    tot_rash_usd = all_totals["USD"]["rash"]

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 <b>Barcha davrdagi umumiy chiqim:</b> {fmt(tot_rash_uzs)} so'm" + (f" | ${fmt(tot_rash_usd)}" if tot_rash_usd > 0 else ""))
    lines.append("\n💡 <i>O'chirish uchun: «🗑 Отменить последнее» tugmasini bosing.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)


# =====================================================================
# ADMIN PANEL VA BOSHQARUV FUNKSIYALARI (Boshliq uchun)
# =====================================================================

async def boss_claim_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/boss yoki /rahbar buyrug'i orqali adminlikni biriktirish"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    db.commit()
    await update.message.reply_text(
        "👑 <b>Siz bot rahbari (Admin) deb muvaffaqiyatli belgilandingiz!</b>\n\n"
        "Endi botdagi barcha foydalanuvchilarni va ularning hisob-kitoblarini ko'rishingiz mumkin.",
        parse_mode="HTML",
        reply_markup=get_main_markup(uid)
    )
    await show_admin_menu(update, ctx)


async def show_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel bosh oynasi"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    if not is_admin(uid):
        await update.message.reply_text(
            "⛔️ Ushbu bo'lim faqat bot rahbari uchun mo'ljallangan.",
            reply_markup=get_main_markup(uid)
        )
        return

    ctx.user_data.clear()

    total_users = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    active_today = db.execute("SELECT COUNT(*) as c FROM users WHERE date(last_active) = date('now', '+5 hours')").fetchone()["c"]
    total_tx = db.execute("SELECT COUNT(*) as c FROM t").fetchone()["c"]
    total_debts = db.execute("SELECT COUNT(*) as c FROM debts WHERE is_closed = 0").fetchone()["c"]

    text = (
        "👑 <b>HURMATLI BOSHLIQ, ADMIN PANELGA XUSH KELIBSIZ!</b>\n\n"
        "Bu yerda botdan foydalanayotgan barcha odamlarni va ularning hisob-kitoblarini ko'rishingiz mumkin.\n\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {total_users} ta\n"
        f"📅 <b>Bugun faol bo'lganlar:</b> {active_today} ta\n"
        f"📝 <b>Jami yozuvlar:</b> {total_tx} ta\n"
        f"📕 <b>Faol ochiq qarzlar:</b> {total_debts} ta\n\n"
        "👇 <b>Quyidagi bo'limlardan birini tanlang:</b>"
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=ADMIN_REPLY_MARKUP)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=ADMIN_REPLY_MARKUP)


async def show_admin_users_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Barcha foydalanuvchilar ro'yxati va ularning balanslari"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    if not is_admin(uid):
        return

    rows = db.execute("SELECT * FROM users ORDER BY last_active DESC").fetchall()
    if not rows:
        await update.message.reply_text("Foydalanuvchilar topilmadi.", reply_markup=ADMIN_REPLY_MARKUP)
        return

    text_parts = [f"👥 <b>BOT FOYDALANUVCHILARI RO'YXATI ({len(rows)} ta):</b>\n"]
    buttons = []

    for idx, r in enumerate(rows, 1):
        u_id = r["user_id"]
        full_name = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or f"Foydalanuvchi {u_id}"
        username_str = f"@{r['username']}" if r["username"] else "mavjud emas"
        dt = format_date(r["last_active"])

        totals = get_totals_by_currency(u_id)
        uzs_bal = totals["UZS"]["bal"]
        usd_bal = totals["USD"]["bal"]

        debts_cnt = db.execute("SELECT COUNT(*) as c FROM debts WHERE user = ? AND is_closed = 0", (u_id,)).fetchone()["c"]

        bal_str = f"Qoldiq: {fmt(uzs_bal)} so'm"
        if usd_bal != 0:
            bal_str += f" | ${fmt(usd_bal)}"

        user_info = (
            f"<b>{idx}. {full_name}</b> ({username_str})\n"
            f"   🆔 ID: <code>{u_id}</code> | 🕒 {dt}\n"
            f"   💰 {bal_str}\n"
            f"   📕 Ochiq qarzlari: {debts_cnt} ta\n"
        )
        text_parts.append(user_info)

        btn_label = f"🔍 #{idx} {full_name[:15]} hisoboti"
        buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_user:{u_id}")])

    full_text = "\n".join(text_parts)
    if len(full_text) > 4000:
        full_text = full_text[:3950] + "\n\n<i>...va boshqa foydalanuvchilar</i>"

    if update.callback_query:
        await update.callback_query.message.reply_text(
            full_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else ADMIN_REPLY_MARKUP
        )
    else:
        await update.message.reply_text(
            full_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else ADMIN_REPLY_MARKUP
        )


async def show_admin_user_detail(update: Update, target_user_id: int):
    """Biror bir foydalanuvchining to'liq hisob-kitobini ko'rish"""
    user_row = db.execute("SELECT * FROM users WHERE user_id = ?", (target_user_id,)).fetchone()
    name = f"{user_row['first_name'] or ''} {user_row['last_name'] or ''}".strip() if user_row else f"Foydalanuvchi {target_user_id}"
    username = f"@{user_row['username']}" if (user_row and user_row['username']) else "mavjud emas"
    last_act = format_date(user_row['last_active']) if user_row else "noma'lum"

    totals = get_totals_by_currency(target_user_id)
    uzs = totals["UZS"]
    usd = totals["USD"]

    tx_rows = db.execute("SELECT * FROM t WHERE user = ? ORDER BY id DESC LIMIT 10", (target_user_id,)).fetchall()
    debt_rows = db.execute("SELECT * FROM debts WHERE user = ? AND is_closed = 0 ORDER BY id DESC", (target_user_id,)).fetchall()

    lines = [
        f"👤 <b>FOYDALANUVCHI HISOBI:</b>",
        f"Ismi: <b>{name}</b> ({username})",
        f"ID: <code>{target_user_id}</code> | Oxirgi faollik: 🕒 {last_act}\n",
        f"📊 <b>BALANSI:</b>",
        f"🇺🇿 So'mda: Kirim: {fmt(uzs['doh'])} | Chiqim: {fmt(uzs['rash'])} | <b>Qoldiq: {fmt(uzs['bal'])} so'm</b>",
        f"🇺🇸 Dollarda: Kirim: ${fmt(usd['doh'])} | Chiqim: ${fmt(usd['rash'])} | <b>Qoldiq: ${fmt(usd['bal'])}</b>\n",
        f"📋 <b>OXIRGI AMALLARI (Tranzaksiyalar):</b>"
    ]

    if not tx_rows:
        lines.append("  <i>Hozircha yozuvlar yo'q</i>")
    else:
        for r in tx_rows:
            amt = r["amount"]
            curr = r["currency"] or "UZS"
            icon = "🟢 +" if amt > 0 else "🔴 -"
            dt = format_date(r["date"])
            lines.append(f"  <b>#{r['id']}</b> | {icon}{fmt_money(amt, curr)} | <i>{r['note']}</i> | {dt}")

    lines.append("\n📕 <b>OCHIQ QARZLARI:</b>")
    if not debt_rows:
        lines.append("  <i>Qarzlar yo'q</i>")
    else:
        for d in debt_rows:
            d_icon = "🟢 Bergan" if d["debt_type"] == "berdim" else "🔴 Olgan"
            curr = d["currency"] or "UZS"
            lines.append(f"  {d_icon}: <b>{d['name']}</b> — {fmt_money(d['amount'], curr)} ({d['note']})")

    res_text = "\n".join(lines)
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Ro'yxatga qaytish", callback_data="adm_back")]])

    if update.callback_query:
        await update.callback_query.message.reply_text(res_text, parse_mode="HTML", reply_markup=back_markup)
    else:
        await update.message.reply_text(res_text, parse_mode="HTML", reply_markup=back_markup)


async def show_admin_kassa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Barcha foydalanuvchilarning umumiy kassa holati"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    if not is_admin(uid):
        return

    rows = db.execute("""
        SELECT 
            COALESCE(currency, 'UZS') as cur,
            COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS doh,
            COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS rash
        FROM t
        GROUP BY COALESCE(currency, 'UZS')
    """).fetchall()

    tots = {"UZS": {"doh": 0, "rash": 0, "bal": 0}, "USD": {"doh": 0, "rash": 0, "bal": 0}}
    for r in rows:
        c = r["cur"] if r["cur"] in ["UZS", "USD"] else "UZS"
        d = int(r["doh"])
        ra = int(r["rash"])
        tots[c] = {"doh": d, "rash": ra, "bal": d - ra}

    uzs = tots["UZS"]
    usd = tots["USD"]

    total_users = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    total_tx = db.execute("SELECT COUNT(*) as c FROM t").fetchone()["c"]
    total_debts = db.execute("SELECT COUNT(*) as c FROM debts WHERE is_closed = 0").fetchone()["c"]

    text = (
        "📊 <b>BOTNING UMUMIY KASSA VA FOYDALANUVCHILAR STATISTIKASI:</b>\n\n"
        f"👥 Foydalanuvchilar soni: <b>{total_users} ta</b>\n"
        f"📝 Jami kiritilgan yozuvlar: <b>{total_tx} ta</b>\n"
        f"📕 Faol ochiq qarzlar: <b>{total_debts} ta</b>\n\n"
        "🇺🇿 <b>UMUMIY SO'MDA:</b>\n"
        f"🟢 Jami kirim: {fmt(uzs['doh'])} so'm\n"
        f"🔴 Jami chiqim: {fmt(uzs['rash'])} so'm\n"
        f"💰 <b>Jami aylanma qoldig'i: {fmt(uzs['bal'])} so'm</b>\n\n"
        "🇺🇸 <b>UMUMIY DOLLARDA ($):</b>\n"
        f"🟢 Jami kirim: ${fmt(usd['doh'])}\n"
        f"🔴 Jami chiqim: ${fmt(usd['rash'])}\n"
        f"💰 <b>Jami aylanma qoldig'i: ${fmt(usd['bal'])}</b>\n\n"
        "<i>Eslatma: Har bir foydalanuvchi faqat o'z hisob-kitobini ko'radi. Barcha ma'lumotlar xavfsiz saqlanmoqda.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=ADMIN_REPLY_MARKUP)


# =====================================================================
# ASOSIY XABARLAR VA HODISALARNI QAYTA ISHLASH (Message Handlers)
# =====================================================================

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Barcha xabarlarni qayta ishlash"""
    if not update.message or not update.message.text:
        return
    raw_text = update.message.text.strip()
    user = update.effective_user
    track_user(user)
    uid = user.id
    markup = get_main_markup(uid)

    # 1. Admin buyruqlari va tugmalari (Lotin va Kirill tillarida)
    lower_raw = raw_text.lower().strip()
    admin_triggers = [
        "админ", "/админ", "admin", "/admin", "admin panel", "админ панел", "админ панели",
        "👑 admin panel (boshqaruv)", "👑 admin panel", "босс", "/босс", "boss", "/boss",
        "рахбар", "/рахбар", "раҳбар", "/раҳбар", "rahbar", "/rahbar"
    ]
    if lower_raw in admin_triggers or raw_text in ["👑 Admin Panel (Boshqaruv)", "👑 Admin Panel", "Admin Panel"]:
        db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        db.commit()
        await show_admin_menu(update, ctx)
        return

    elif raw_text in ["👥 Barcha foydalanuvchilar", "👥 Foydalanuvchilar ro'yxati", "/users"]:
        await show_admin_users_list(update, ctx)
        return

    elif raw_text in ["📊 Umumiy kassa", "📊 Bot statistikasi", "/kassa"]:
        await show_admin_kassa(update, ctx)
        return

    elif raw_text in ["🔍 Foydalanuvchini ko'rish", "🔍 Foydalanuvchini tekshirish"]:
        if not is_admin(uid):
            return
        ctx.user_data["action"] = "admin_check_user"
        await update.message.reply_text(
            "🔍 <b>Foydalanuvchi ID raqamini kiriting:</b>\n\n"
            "Masalan: <code>8042453163</code> yoki <code>7489502905</code>\n"
            "(Bekor qilish uchun: 'Bekor qilish' deb yozing)",
            parse_mode="HTML"
        )
        return

    elif raw_text in ["📢 Hammaga xabar", "📢 Hammaga xabar yuborish"]:
        if not is_admin(uid):
            return
        ctx.user_data["action"] = "admin_broadcast"
        await update.message.reply_text(
            "📢 <b>Barcha foydalanuvchilarga yuboriladigan xabarni yozing:</b>\n\n"
            "(Bekor qilish uchun: 'Bekor qilish' deb yozing)",
            parse_mode="HTML"
        )
        return

    # Admin faol amallarini tekshirish
    action = ctx.user_data.get("action")
    if action == "admin_check_user":
        if raw_text.lower() in ["bekor", "bekor qilish", "cancel"]:
            ctx.user_data.pop("action", None)
            await update.message.reply_text("Bekor qilindi.", reply_markup=ADMIN_REPLY_MARKUP)
            return
        digits = re.sub(r"\D", "", raw_text)
        if digits:
            target_uid = int(digits)
            ctx.user_data.pop("action", None)
            await show_admin_user_detail(update, target_uid)
            return
        else:
            await update.message.reply_text("Iltimos, faqat foydalanuvchi ID raqamini kiriting (masalan: 7489502905):")
            return

    if action == "admin_broadcast":
        if raw_text.lower() in ["bekor", "bekor qilish", "cancel"]:
            ctx.user_data.pop("action", None)
            await update.message.reply_text("Xabar tarqatish bekor qilindi.", reply_markup=ADMIN_REPLY_MARKUP)
            return
        ctx.user_data.pop("action", None)
        users = db.execute("SELECT DISTINCT user_id FROM users").fetchall()
        sent_cnt = 0
        fail_cnt = 0
        status_msg = await update.message.reply_text("⏳ Xabar barcha foydalanuvchilarga yuborilmoqda...")
        for u in users:
            t_id = u["user_id"]
            try:
                await ctx.bot.send_message(
                    chat_id=t_id,
                    text=f"📢 <b>ADMINISTRATOR XABARI:</b>\n\n{raw_text}",
                    parse_mode="HTML"
                )
                sent_cnt += 1
            except Exception:
                fail_cnt += 1
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ <b>Xabar tarqatildi!</b>\n\n"
            f"📤 Yuborildi: {sent_cnt} ta foydalanuvchiga\n"
            f"⚠️ Yetib bormadi (bloklagan): {fail_cnt} ta",
            parse_mode="HTML",
            reply_markup=ADMIN_REPLY_MARKUP
        )
        return

    # 2. Asosiy menyu va navigatsiya
    if raw_text in ["📕 Qarz daftari (Долги)", "📕 Qarz daftari", "Qarz daftari", "/debts", "/qarz"]:
        await show_debt_menu(update, ctx)
        return

    elif raw_text in ["🔙 Asosiy menyu", "🔙 Главное меню", "/menu"]:
        ctx.user_data.clear()
        await update.message.reply_text("Asosiy menyuga qaytdingiz:", reply_markup=markup)
        return

    elif raw_text in ["📋 Qarzdorlar ro'yxati", "Qarzdorlar ro'yxati", "/qarzlar"]:
        await show_debt_list(update, ctx)
        return

    elif raw_text in ["✅ Qarzni yopish", "Qarzni yopish"]:
        await close_debt_prompt(update, ctx)
        return

    elif raw_text in ["🟢 Qarz berdim (Menga berishadi)", "Qarz berdim"]:
        ctx.user_data["action"] = "debt_berdim"
        await update.message.reply_text(
            "🟢 <b>Kimga va qancha qarz berdingiz?</b>\n\n"
            "Misol: <code>Ali 100$ 1 haftaga</code>\n"
            "yoki: <code>Ali 500000</code>",
            parse_mode="HTML"
        )
        return

    elif raw_text in ["🔴 Qarz oldim (Men berishim kerak)", "Qarz oldim"]:
        ctx.user_data["action"] = "debt_oldim"
        await update.message.reply_text(
            "🔴 <b>Kimdan va qancha qarz oldingiz?</b>\n\n"
            "Misol: <code>Vali 50$ doriga</code>\n"
            "yoki: <code>Vali 300000</code>",
            parse_mode="HTML"
        )
        return

    elif raw_text in ["➕ Доход / Kirim", "➕ Доход", "➕ Kirim"]:
        ctx.user_data["action"] = "dohod"
        await update.message.reply_text(
            "➕ <b>Kirim summasini va nimaligini yozing:</b>\n\n"
            "Misol: <code>500000 maosh</code> yoki <code>100$ bonus</code>",
            parse_mode="HTML"
        )
        return

    elif raw_text in ["➖ Расход / Chiqim", "➖ Расход", "➖ Chiqim"]:
        ctx.user_data["action"] = "rashod"
        await update.message.reply_text(
            "➖ <b>Chiqim summasini va nimaligini yozing:</b>\n\n"
            "Misol: <code>72,000 газ куйдим</code>\n"
            "yoki: <code>200,000га атир олдим</code>",
            parse_mode="HTML"
        )
        return

    elif raw_text in ["📊 Общий отчет", "📊 Jami hisobot", "/balans", "/report"]:
        await send_summary(update, uid, "📊 <b>UMUMIY HISOB-KITOB</b>")
        return

    elif raw_text in ["📅 Отчет за сегодня", "📅 Bugungi hisobot", "/bugun", "/today"]:
        await send_summary(update, uid, "📅 <b>BUGUNGI HISOB-KITOB</b>", "AND date(date) = date('now', '+5 hours')")
        return

    elif raw_text in ["📋 История (Tarix)", "📋 История", "📋 Tarix", "/history", "/tarix"]:
        await show_history(update, ctx)
        return

    elif raw_text in ["🗑 Отменить последнее", "🗑 Oxirgisini o'chirish", "/undo"]:
        await ochir_oxirgisi(update, ctx)
        return

    elif raw_text in ["🖼 Chiroyli Fon", "🖼 Fon", "/fon", "Fon"]:
        await send_wallpaper_message(update, ctx)
        return

    elif raw_text in [
        "📢 Qarzdorga ogohlantirish yuborish", "Qarzdorga ogohlantirish",
        "📤 Qarzdorga SMS (Eslatma)", "📤 Qarzdorga SMS", "Qarzdorga SMS",
        "SMS", "/sms", "sms", "ogohlantirish", "Ogohlantirish"
    ]:
        await send_debt_sms_prompt(update, ctx)
        return

    elif raw_text in ["ℹ️ Помощь / Yordam", "ℹ️ Yordam", "ℹ️ Помощь", "/help"]:
        await help_command(update, ctx)
        return

    # 3. Qarz kiritish holati
    if action in ["debt_berdim", "debt_oldim"]:
        name, amount, curr, note = parse_debt_input(raw_text)
        if amount > 0 and name:
            debt_type = "berdim" if action == "debt_berdim" else "oldim"
            cur = db.execute(
                "INSERT INTO debts(user, debt_type, name, amount, note, currency) VALUES(?, ?, ?, ?, ?, ?)",
                (uid, debt_type, name, amount, note, curr)
            )
            db.commit()
            ctx.user_data.pop("action", None)

            type_title = "🟢 <b>Qarz berildi (Menga qaytarishadi):</b>" if debt_type == "berdim" else "🔴 <b>Qarz olindi (Men to'lashim kerak):</b>"
            
            if debt_type == "berdim":
                share_url = make_debt_share_url(name, amount, curr, note)
                kb_list = [
                    [InlineKeyboardButton("📢 Qarzdorga ogohlantirish (SMS) jo'natish", url=share_url)]
                ]
                if name.startswith("@"):
                    clean_u = name.lstrip("@").strip()
                    kb_list.append([InlineKeyboardButton(f"💬 @{clean_u} ga to'g'ridan-to'g'ri yozish", url=f"https://t.me/{clean_u}")])
                
                await update.message.reply_text(
                    f"📕 <b>Qarz daftariga yozildi!</b>\n\n"
                    f"{type_title}\n"
                    f"👤 <b>Ism:</b> {name}\n"
                    f"💰 <b>Summa:</b> {fmt_money(amount, curr)}\n"
                    f"📝 <b>Izoh:</b> {note}\n\n"
                    f"👇 <i>Qarzdorga hoziroq Telegram orqali qarz borligi haqida <b>OGOHLANTIRISH</b> jo'natish uchun pastdagi tugmani bosing:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb_list)
                )
                return
            else:
                await update.message.reply_text(
                    f"📕 <b>Qarz daftariga yozildi!</b>\n\n"
                    f"{type_title}\n"
                    f"👤 <b>Ism:</b> {name}\n"
                    f"💰 <b>Summa:</b> {fmt_money(amount, curr)}\n"
                    f"📝 <b>Izoh:</b> {note}",
                    parse_mode="HTML",
                    reply_markup=DEBT_REPLY_MARKUP
                )
                return
        else:
            await update.message.reply_text(
                "❌ Qarz ma'lumotini tushunmadim.\nMisol: <code>Ali 100$</code> yoki <code>Ali 500000 1 haftaga</code>",
                parse_mode="HTML"
            )
            return

    # 4. Jonli matnni tahlil qilish (Natural Language Parser)
    amount_val, curr, note_val, inf_type = parse_natural_uzbek_text(raw_text)

    # Agar foydalanuvchi oldin "➕ Kirim" yoki "➖ Chiqim" tugmasini bosgan bo'lsa
    if action in ["dohod", "rashod"]:
        if amount_val and amount_val > 0:
            final_type = "kirim" if action == "dohod" else "chiqim"
            final_amount = amount_val if final_type == "kirim" else -amount_val
            save_transaction(uid, final_amount, note_val, curr)
            ctx.user_data.pop("action", None)

            totals = get_totals_by_currency(uid)
            c_info = totals[curr]
            kind_text = "🟢 <b>Kirim saqlandi:</b>" if final_amount > 0 else "🔴 <b>Chiqim saqlandi:</b>"
            curr_label = "🇺🇸 Dollarda" if curr == "USD" else "🇺🇿 So'mda"
            await update.message.reply_text(
                f"{kind_text} {fmt_money(amount_val, curr)}\n"
                f"📝 <b>Nomi:</b> {note_val}\n\n"
                f"📊 <b>{curr_label} hisob:</b>\n"
                f"Kirim: {fmt_money(c_info['doh'], curr)} | Chiqim: {fmt_money(c_info['rash'], curr)}\n"
                f"💰 Qoldiq: {fmt_money(c_info['bal'], curr)}",
                parse_mode="HTML",
                reply_markup=markup
            )
            return

    # Avtomatik aniqlangan Kirim yoki Chiqim (masalan: "72,000мин газ кудим", "200,000га атир олдим")
    if amount_val and amount_val > 0 and inf_type in ["kirim", "chiqim"]:
        final_amount = amount_val if inf_type == "kirim" else -amount_val
        save_transaction(uid, final_amount, note_val, curr)

        totals = get_totals_by_currency(uid)
        c_info = totals[curr]
        kind_text = "🟢 <b>Kirim saqlandi:</b>" if inf_type == "kirim" else "🔴 <b>Chiqim saqlandi:</b>"
        curr_label = "🇺🇸 Dollarda" if curr == "USD" else "🇺🇿 So'mda"
        await update.message.reply_text(
            f"{kind_text} {fmt_money(amount_val, curr)}\n"
            f"📝 <b>Nomi:</b> {note_val}\n\n"
            f"📊 <b>{curr_label} hisob:</b>\n"
            f"Kirim: {fmt_money(c_info['doh'], curr)} | Chiqim: {fmt_money(c_info['rash'], curr)}\n"
            f"💰 Qoldiq: {fmt_money(c_info['bal'], curr)}",
            parse_mode="HTML",
            reply_markup=markup
        )
        return

    # Agar shunchaki summa kiritilgan bo'lsa (Kirimmi yoki Chiqimligi noaniq)
    if amount_val and amount_val > 0:
        ctx.user_data["pending_amount"] = amount_val
        ctx.user_data["pending_curr"] = curr
        ctx.user_data["pending_note"] = note_val
        inline_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Kirim", callback_data="type_dohod"),
                InlineKeyboardButton("➖ Chiqim", callback_data="type_rashod")
            ],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="type_cancel")]
        ])
        await update.message.reply_text(
            f"❓ <b>{fmt_money(amount_val, curr)}</b> (<i>{note_val}</i>)\nBu kirimmi yoki chiqim?",
            parse_mode="HTML",
            reply_markup=inline_kb
        )
        return

    # Tushunarsiz xabar
    await update.message.reply_text(
        "Tushunmadim 🤔\n"
        "Shunchaki nima qilganingizni yozing, masalan:\n"
        "• <code>72,000 газ қуйдим</code>\n"
        "• <code>200,000га атир олдим</code>\n"
        "• <code>500000 ойлик тушди</code>",
        parse_mode="HTML",
        reply_markup=markup
    )


async def handle_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline tugmalar bosilganda"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    track_user(user)
    uid = user.id

    # Admin boshqaruvi callbacklari
    if data.startswith("adm_user:"):
        if not is_admin(uid):
            await query.answer("Ruxsat berilmagan!", show_alert=True)
            return
        target_uid = int(data.split(":")[1])
        await show_admin_user_detail(update, target_uid)
        return

    elif data == "adm_back":
        if not is_admin(uid):
            return
        await show_admin_users_list(update, ctx)
        return

    # Qarzni yopish
    if data.startswith("close_debt:"):
        debt_id = int(data.split(":")[1])
        row = db.execute("SELECT id, name, amount, currency, debt_type FROM debts WHERE id = ? AND user = ?", (debt_id, uid)).fetchone()
        if row:
            db.execute("UPDATE debts SET is_closed = 1, closed_at = datetime('now','+5 hours') WHERE id = ? AND user = ?", (debt_id, uid))
            db.commit()
            curr = row["currency"] or "UZS"
            type_str = "haqingiz qaytarildi" if row["debt_type"] == "berdim" else "qarzingiz to'landi"
            await query.edit_message_text(
                f"✅ <b>Qarz yopildi!</b>\n\n"
                f"#{row['id']} — <b>{row['name']}</b> ({fmt_money(row['amount'], curr)}) {type_str}.",
                parse_mode="HTML"
            )
        else:
            await query.edit_message_text("Qarz topilmadi yoki allaqachon yopilgan.")
        return

    elif data == "cancel_close_debt":
        await query.edit_message_text("❌ Qarzni yopish bekor qilindi.")
        return

    # Qarzdorlarga SMS eslatma ochish
    elif data == "open_sms_reminders":
        rows = db.execute(
            "SELECT id, name, amount, currency, note FROM debts WHERE user = ? AND debt_type = 'berdim' AND is_closed = 0 ORDER BY id DESC",
            (uid,)
        ).fetchall()
        if not rows:
            await query.edit_message_text("Qarz berilgan ochiq qarzlar topilmadi.")
            return
        buttons = []
        for r in rows:
            curr = r["currency"] or "UZS"
            money_str = fmt_money(r["amount"], curr)
            share_url = make_debt_share_url(r["name"], r["amount"], curr, r["note"])
            buttons.append([InlineKeyboardButton(f"📤 {r['name']} ({money_str}) ga SMS", url=share_url)])
        buttons.append([InlineKeyboardButton("❌ Yopish", callback_data="cancel_remind_debts")])
        await query.edit_message_text(
            "📤 <b>Kimga Telegram orqali qarz eslatmasini (SMS) yubormoqchisiz?</b>\n\n"
            "👇 Kerakli odamni tanlang — Telegram darhol xabarni tayyorlab beradi:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    elif data == "cancel_remind_debts":
        await query.edit_message_text("❌ Bekor qilindi.")
        return

    # Kirim/Chiqim inline tanlash
    amt = ctx.user_data.get("pending_amount")
    curr = ctx.user_data.get("pending_curr", "UZS")
    note = ctx.user_data.get("pending_note", "-")

    if data == "type_cancel":
        ctx.user_data.pop("pending_amount", None)
        ctx.user_data.pop("pending_curr", None)
        ctx.user_data.pop("pending_note", None)
        await query.edit_message_text("❌ Bekor qilindi.")
        return

    if not amt:
        await query.edit_message_text("Amal muddati o'tgan.")
        return

    if data == "type_dohod":
        save_transaction(uid, amt, note, curr)
        totals = get_totals_by_currency(uid)
        c_info = totals[curr]
        await query.edit_message_text(
            f"🟢 <b>Kirim saqlandi:</b> +{fmt_money(amt, curr)}\n"
            f"📝 <b>Nomi:</b> {note}\n\n"
            f"📊 Kirim: {fmt_money(c_info['doh'], curr)} | Chiqim: {fmt_money(c_info['rash'], curr)}\n"
            f"💰 Qoldiq: {fmt_money(c_info['bal'], curr)}",
            parse_mode="HTML"
        )
    elif data == "type_rashod":
        save_transaction(uid, -amt, note, curr)
        totals = get_totals_by_currency(uid)
        c_info = totals[curr]
        await query.edit_message_text(
            f"🔴 <b>Chiqim saqlandi:</b> -{fmt_money(amt, curr)}\n"
            f"📝 <b>Nomi:</b> {note}\n\n"
            f"📊 Kirim: {fmt_money(c_info['doh'], curr)} | Chiqim: {fmt_money(c_info['rash'], curr)}\n"
            f"💰 Qoldiq: {fmt_money(c_info['bal'], curr)}",
            parse_mode="HTML"
        )

    ctx.user_data.pop("pending_amount", None)
    ctx.user_data.pop("pending_curr", None)
    ctx.user_data.pop("pending_note", None)


async def qarz_yop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/qarz_yop 5 buyrug'i"""
    uid = update.effective_user.id
    track_user(update.effective_user)
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Qarz ID raqamini kiriting. Masalan: <code>/qarz_yop 2</code>", parse_mode="HTML")
        return
    debt_id = int(ctx.args[0])
    cur = db.execute("UPDATE debts SET is_closed = 1, closed_at = datetime('now','+5 hours') WHERE id = ? AND user = ? AND is_closed = 0", (debt_id, uid))
    db.commit()
    if cur.rowcount > 0:
        await update.message.reply_text(f"✅ #{debt_id}-raqamli qarz yopildi deb belgilandi!", reply_markup=DEBT_REPLY_MARKUP)
    else:
        await update.message.reply_text(f"❌ #{debt_id}-raqamli ochiq qarz topilmadi.", reply_markup=DEBT_REPLY_MARKUP)


def main():
    token = ""
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except Exception:
            pass

    if not token:
        token = os.getenv("BOT_TOKEN", "").strip()

    if not token:
        print("Xatolik: Token topilmadi!")
        return

    print("=" * 60)
    print("Bot Admin Panel va foydalanuvchilar nazorati bilan ishga tushmoqda...")
    print("=" * 60)

    def keep_alive_worker():
        url = os.environ.get("RENDER_EXTERNAL_URL", "https://hisobchi-i0k5.onrender.com")
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url, timeout=10)
            except Exception:
                pass

    threading.Thread(target=keep_alive_worker, daemon=True).start()

    app = ApplicationBuilder().token(token).build()

    # Buyruqlar
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("fon", send_wallpaper_message))
    app.add_handler(CommandHandler("sms", send_debt_sms_prompt))
    app.add_handler(CommandHandler("qarz_yop", qarz_yop_command))
    app.add_handler(CommandHandler("close_debt", qarz_yop_command))

    # Admin buyruqlari
    app.add_handler(CommandHandler("admin", show_admin_menu))
    app.add_handler(CommandHandler("boss", boss_claim_command))
    app.add_handler(CommandHandler("rahbar", boss_claim_command))
    app.add_handler(CommandHandler("users", show_admin_users_list))

    # Tugmalar va xabarlar
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
