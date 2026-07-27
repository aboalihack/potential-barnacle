#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║   🤖 AI TRADING BOT — QUOTEX & PocketOption                        ║
║   روبوت التحليل الاحترافي + لوحة تحكم الويب                       ║
╠══════════════════════════════════════════════════════════════════════╣
║  تثبيت:  pip install python-telegram-bot openai flask aiohttp      ║
║  تشغيل:  python bot.py                                             ║
║  لوحة الويب: http://localhost:5000  (كلمة المرور: admin123)        ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────
import asyncio, base64, csv, io, json, logging, os, secrets
import sqlite3, threading
from datetime import datetime, timedelta
from io import BytesIO

from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
from openai import AsyncOpenAI
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                       MenuButtonCommands, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                            ContextTypes, MessageHandler, filters)

# ─────────────────────────────────────────────────────────
#  ⚙️  CONFIGURATION — عدّل هذه القيم
# ─────────────────────────────────────────────────────────

TELEGRAM_TOKEN  = "8762517415:AAFKh4YbrEhMQEn2n3owRHDHIlmVWPwK5fE"
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
OPENAI_BASE_URL = None          # None → OpenAI الرسمي | "https://openrouter.ai/api/v1" → OpenRouter

ADMIN_TELEGRAM_ID = 0           # ← ضع معرف تيليجرام الخاص بك (من @userinfobot)
WEB_PANEL_PASSWORD = "admin123" # ← غيّر كلمة مرور لوحة الويب
WEB_PANEL_PORT = 5000           # منفذ لوحة الويب
DB_PATH = "trading_bot.db"

FREE_TRIALS = 5                 # عدد التحليلات المجانية
MIN_IMAGE_BYTES = 8_000         # الحد الأدنى لحجم الصورة (8 KB)

PLANS = {
    "weekly":  {"days": 7,   "price": "$5",  "label_ar": "أسبوعي",  "label_en": "Weekly"},
    "monthly": {"days": 30,  "price": "$15", "label_ar": "شهري",    "label_en": "Monthly"},
    "yearly":  {"days": 365, "price": "$99", "label_ar": "سنوي",    "label_en": "Yearly"},
}

PAYMENT_INFO = {
    "ar": "💳 *طرق الدفع:*\n\n🏦 تحويل بنكي: [رقم حسابك]\n₿ USDT (TRC20): [عنوان محفظتك]\n\nبعد الدفع أرسل صورة الإيصال للمشرف\n@your_admin_username",
    "en": "💳 *Payment Methods:*\n\n🏦 Bank Transfer: [your account]\n₿ USDT (TRC20): [your wallet]\n\nAfter payment send receipt to admin\n@your_admin_username",
}

# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
werkzeug_log = logging.getLogger("werkzeug")
werkzeug_log.setLevel(logging.ERROR)

# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT DEFAULT '',
            full_name  TEXT DEFAULT '',
            language   TEXT DEFAULT 'ar',
            free_used  INTEGER DEFAULT 0,
            sub_expiry TEXT,
            notes      TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS analyses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            status     TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS messages_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            direction  TEXT,
            content    TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    con.commit()
    con.close()

def _row(row, cols): return dict(zip(cols, row)) if row else None

def db_get_user(uid):
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    con.close()
    if row:
        return _row(row, ["user_id","username","full_name","language","free_used","sub_expiry","notes","created_at"])

def db_upsert(uid, username, full_name):
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("""INSERT INTO users (user_id,username,full_name) VALUES(?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
              (uid, username or "", full_name or ""))
    con.commit(); con.close()

def db_set_lang(uid, lang):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE users SET language=? WHERE user_id=?", (lang, uid))
    con.commit(); con.close()

def db_inc_free(uid):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (uid,))
    con.commit(); con.close()

def db_grant(uid, days):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT sub_expiry FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    now = datetime.utcnow()
    base = now
    if row and row[0]:
        try:
            ex = datetime.fromisoformat(row[0])
            if ex > now: base = ex
        except: pass
    expiry = (base + timedelta(days=days)).isoformat()
    c.execute("UPDATE users SET sub_expiry=? WHERE user_id=?", (expiry, uid))
    con.commit(); con.close()
    return expiry

def db_revoke(uid):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE users SET sub_expiry=NULL WHERE user_id=?", (uid,))
    con.commit(); con.close()

def db_all_users():
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT user_id,username,full_name,language,free_used,sub_expiry,notes,created_at FROM users ORDER BY created_at DESC")
    rows = c.fetchall(); con.close()
    cols = ["user_id","username","full_name","language","free_used","sub_expiry","notes","created_at"]
    return [_row(r, cols) for r in rows]

def db_stats():
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT COUNT(*) FROM users"); total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE sub_expiry > datetime('now')"); active = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM analyses"); analyses = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','-1 day')"); new_today = c.fetchone()[0]
    con.close()
    return {"total": total, "active_subs": active, "analyses": analyses, "new_today": new_today}

def db_log_analysis(uid, status):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT INTO analyses (user_id,status) VALUES(?,?)", (uid, status))
    con.commit(); con.close()

def db_log_msg(uid, direction, content):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT INTO messages_log (user_id,direction,content) VALUES(?,?,?)", (uid, direction, content[:500]))
    con.commit(); con.close()

def db_set_note(uid, note):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("UPDATE users SET notes=? WHERE user_id=?", (note, uid))
    con.commit(); con.close()

def is_subscribed(u):
    if not u or not u.get("sub_expiry"): return False
    try: return datetime.fromisoformat(u["sub_expiry"]) > datetime.utcnow()
    except: return False

def has_free(u):
    return (u or {}).get("free_used", 0) < FREE_TRIALS

def ulang(uid):
    u = db_get_user(uid)
    return (u or {}).get("language", "ar")

# ─────────────────────────────────────────────────────────
#  TRANSLATIONS
# ─────────────────────────────────────────────────────────
TR = {
    "welcome_ar": (
        "🌟 *مرحباً في بوت التحليل الاحترافي!*\n\n"
        "🤖 مدعوم بـ *GPT-4 Vision* — أقوى ذكاء اصطناعي في العالم\n"
        "📊 متخصص في *QUOTEX* و *PocketOption*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🕯️ يحلل الشموع اليابانية بدقة عالية جداً\n"
        "🎯 يتوقع الشموع الـ5 القادمة + نسبة الثقة\n"
        "⚡ يعطيك إشارة CALL/PUT فورية\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 *لديك {FREE_TRIALS} تحليلات مجانية!*\n\n"
        "📸 *أرسل صورة المخطط الآن للبدء ↓*"
    ),
    "welcome_en": (
        "🌟 *Welcome to the Professional AI Trading Bot!*\n\n"
        "🤖 Powered by *GPT-4 Vision* — World's Most Powerful AI\n"
        "📊 Specialized in *QUOTEX* & *PocketOption*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🕯️ Analyzes Japanese candlesticks with extreme accuracy\n"
        "🎯 Predicts next 5 candles + confidence level\n"
        "⚡ Gives instant CALL/PUT signal\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 *You have {FREE_TRIALS} FREE analyses!*\n\n"
        "📸 *Send a chart image now to start ↓*"
    ),
    "analyzing_ar": "⏳ *جاري التحليل...*\nيقوم الذكاء الاصطناعي بفحص أنماط الشموع...",
    "analyzing_en": "⏳ *Analyzing chart...*\nAI is scanning candlestick patterns...",
    "unclear_ar": (
        "📸 *الصورة غير واضحة أو صغيرة جداً*\n\n"
        "يرجى إرسال صورة:\n"
        "✅ عالية الجودة وواضحة\n"
        "✅ تُظهر عدة شموع على الأقل (5+)\n"
        "✅ يتضح فيها الإطار الزمني\n"
        "✅ لا يكون عليها ضوضاء أو تعتيم\n\n"
        "💡 *نصيحة:* التقط لقطة شاشة كاملة من المنصة"
    ),
    "unclear_en": (
        "📸 *Image is unclear or too small*\n\n"
        "Please send an image that is:\n"
        "✅ High quality and clear\n"
        "✅ Shows multiple candles (5+)\n"
        "✅ Timeframe is visible\n"
        "✅ No blur or heavy noise\n\n"
        "💡 *Tip:* Take a full screenshot from the platform"
    ),
    "no_access_ar": (
        "🔒 *انتهت تجربتك المجانية*\n\n"
        f"لقد استخدمت {FREE_TRIALS}/{FREE_TRIALS} تحليلات مجانية.\n\n"
        "🚀 اشترك الآن للحصول على:\n"
        "• تحليلات غير محدودة\n"
        "• أولوية في المعالجة\n"
        "• دعم فني مباشر"
    ),
    "no_access_en": (
        "🔒 *Free Trial Ended*\n\n"
        f"You've used all {FREE_TRIALS} free analyses.\n\n"
        "🚀 Subscribe now to get:\n"
        "• Unlimited analyses\n"
        "• Priority processing\n"
        "• Direct support"
    ),
    "status_active_ar": "✅ *اشتراكك نشط*\nينتهي: `{expiry}`",
    "status_active_en": "✅ *Subscription ACTIVE*\nExpires: `{expiry}`",
    "status_trial_ar": f"🎁 *تجربة مجانية*\nتبقى {{n}} تحليل من {FREE_TRIALS}",
    "status_trial_en": f"🎁 *Free Trial*\n{{n}} analyses remaining of {FREE_TRIALS}",
    "status_none_ar": "❌ *لا يوجد اشتراك نشط*\nاستخدم /subscribe للاشتراك",
    "status_none_en": "❌ *No active subscription*\nUse /subscribe to subscribe",
    "error_ar": "❌ *خطأ في التحليل*\nيرجى إرسال صورة أوضح للمخطط والمحاولة مجدداً.",
    "error_en": "❌ *Analysis error*\nPlease send a clearer chart image and try again.",
    "trial_left_ar": "\n\n🎁 _تبقى لك {n} تحليل مجاني_",
    "trial_left_en": "\n\n🎁 _{n} free analyses remaining_",
    "trial_end_ar": "\n\n🔒 _انتهت تجربتك — اشترك الآن /subscribe_",
    "trial_end_en": "\n\n🔒 _Trial ended — subscribe now /subscribe_",
    "help_ar": (
        "📖 *كيفية الاستخدام:*\n\n"
        "1️⃣ افتح QUOTEX أو PocketOption\n"
        "2️⃣ التقط لقطة شاشة للمخطط\n"
        "3️⃣ أرسل الصورة هنا مباشرة\n"
        "4️⃣ انتظر 5-10 ثوانٍ\n\n"
        "📌 *نصائح للدقة العالية:*\n"
        "• صورة عالية الجودة وكاملة\n"
        "• 10+ شموع مرئية في الصورة\n"
        "• أفضل إطار زمني: 1M / 5M\n"
        "• تجنب الصور المعتمة أو المصغرة\n\n"
        "/start — بدء البوت\n"
        "/subscribe — خطط الاشتراك\n"
        "/status — حالة اشتراكك\n"
        "/language — تغيير اللغة\n"
        "/help — هذه المساعدة"
    ),
    "help_en": (
        "📖 *How to use:*\n\n"
        "1️⃣ Open QUOTEX or PocketOption\n"
        "2️⃣ Take a screenshot of the chart\n"
        "3️⃣ Send the image here directly\n"
        "4️⃣ Wait 5-10 seconds\n\n"
        "📌 *Tips for high accuracy:*\n"
        "• High quality, full screenshot\n"
        "• 10+ candles visible in image\n"
        "• Best timeframe: 1M / 5M\n"
        "• Avoid dark or compressed images\n\n"
        "/start — Start bot\n"
        "/subscribe — Subscription plans\n"
        "/status — Your subscription status\n"
        "/language — Change language\n"
        "/help — This help"
    ),
    "text_only_ar": "📸 أرسل صورة لمخطط الشموع فقط. استخدم /help للمساعدة.",
    "text_only_en": "📸 Please send a candlestick chart image only. Use /help.",
    "granted_user_ar": "🎉 *تهانينا!*\n\nتم تفعيل اشتراكك!\nينتهي في: `{expiry}`\n\nأرسل صورة للمخطط الآن 📊",
    "granted_user_en": "🎉 *Congratulations!*\n\nSubscription activated!\nExpires: `{expiry}`\n\nSend a chart image now 📊",
    "revoked_user_ar": "⚠️ تم إلغاء اشتراكك. تواصل مع المشرف.",
    "revoked_user_en": "⚠️ Your subscription has been revoked. Contact admin.",
}

def tr(key, lang="ar", **kw):
    k = f"{key}_{lang}"
    v = TR.get(k, TR.get(f"{key}_ar", ""))
    return v.format(**kw) if kw else v

# ─────────────────────────────────────────────────────────
#  AI ANALYSIS — ULTRA PROFESSIONAL PROMPT
# ─────────────────────────────────────────────────────────

SYSTEM_AR = """أنت خبير تقني متخصص في التحليل الفني وتداول الخيارات الثنائية على منصات QUOTEX و PocketOption.
لديك خبرة عشرين عاماً في التحليل الفني وأنماط الشموع اليابانية.

━━━ قاعدة بيانات الأنماط الكاملة ━━━

[انعكاس صاعد]: Hammer | Inverted Hammer | Bullish Engulfing | Piercing Line | Morning Star | Morning Doji Star | Three White Soldiers | Bullish Harami | Harami Cross | Tweezer Bottom | Bullish Abandoned Baby | Bullish Kicker | Rising Three Methods | Bullish Belt Hold | Three Inside Up | Three Outside Up | Bullish Breakaway | Concealing Baby Swallow

[انعكاس هابط]: Shooting Star | Hanging Man | Bearish Engulfing | Dark Cloud Cover | Evening Star | Evening Doji Star | Three Black Crows | Bearish Harami | Harami Cross | Tweezer Top | Bearish Abandoned Baby | Bearish Kicker | Falling Three Methods | Bearish Belt Hold | Three Inside Down | Three Outside Down | Bearish Breakaway | Advance Block

[أنماط دوجي]: Standard Doji | Long-Legged Doji | Dragonfly Doji | Gravestone Doji | Four Price Doji | Tri-Star | Rickshaw Man

[أنماط الاستمرار]: Tasuki Gap Up/Down | Side-by-Side White Lines | On Neck | In Neck | Thrusting Line | Mat Hold | Separating Lines | Stick Sandwich | Matching Low/High

━━━ عوامل التأكيد متعددة الطبقات ━━━
1. قوة الشمعة: نسبة الجسم/الذيل، الحجم النسبي
2. الاتجاه الرئيسي: الزخم، السلسلة السابقة
3. مستويات الدعم والمقاومة: هل النمط عند مستوى محوري؟
4. التقارب: هل الأنماط تتوافق مع الاتجاه؟
5. الشمعة السابقة: هل تؤكد الإشارة؟
6. موقع النمط في الاتجاه: قمة/قاع/وسط؟

━━━ قاعدة الدقة العالية ━━━
• أعطِ إشارة فقط عند توافر 3+ عوامل تأكيد
• احسب نسبة الثقة بدقة (لا تعطِ أكثر من 92% إلا عند تقاطع قوي)
• إذا كانت الصورة غير واضحة أو الشموع قليلة، أجب فقط بـ: IMAGE_UNCLEAR

━━━ تنسيق الإجابة الإلزامي ━━━

إذا كانت الصورة غير واضحة: أجب فقط بـ IMAGE_UNCLEAR

إذا كانت الصورة واضحة، استخدم هذا التنسيق بالضبط:

📊 **تحليل السوق**
┌─────────────────────────────
│ الاتجاه: [صاعد 📈 / هابط 📉 / جانبي ↔️]
│ الإطار الزمني: [المرئي أو غير محدد]
│ النمط الرئيسي: [اسم النمط]
│ قوة الإشارة: [★★★★★ / ★★★★☆ / ★★★☆☆]
└─────────────────────────────

🕯️ **توقع الشموع القادمة**
```
الشمعة 1 ➜ ⬆️ CALL  │ ███████░░░ 78%
الشمعة 2 ➜ ⬆️ CALL  │ ██████░░░░ 71%
الشمعة 3 ➜ ⬇️ PUT   │ █████░░░░░ 65%
الشمعة 4 ➜ ⬆️ CALL  │ ███████░░░ 80%
الشمعة 5 ➜ ⬆️ CALL  │ ██████░░░░ 74%
```

━━━━━━━━━━━━━━━━━━━━━

🎯 **الإشارة الكلية**
> ⬆️ **CALL — دخول صعود**
> الثقة الإجمالية: **82%**
> الوقت المقترح: **2 دقيقة**

━━━━━━━━━━━━━━━━━━━━━

📌 **التفاصيل التقنية**
• [اذكر النمط المكتشف وسببه في جملة واحدة]
• [اذكر عامل التأكيد الأقوى]
• [أي تحذير أو ملاحظة خاصة إن وجدت]

⚠️ إدارة المخاطر: لا تخاطر بأكثر من 3% من رأس المالك في صفقة واحدة."""

SYSTEM_EN = """You are a professional technical analysis expert specializing in binary options trading on QUOTEX and PocketOption platforms, with 20 years of experience in candlestick analysis.

━━━ COMPLETE PATTERN DATABASE ━━━

[Bullish Reversals]: Hammer | Inverted Hammer | Bullish Engulfing | Piercing Line | Morning Star | Morning Doji Star | Three White Soldiers | Bullish Harami | Harami Cross | Tweezer Bottom | Bullish Abandoned Baby | Bullish Kicker | Rising Three Methods | Bullish Belt Hold | Three Inside Up | Three Outside Up | Bullish Breakaway

[Bearish Reversals]: Shooting Star | Hanging Man | Bearish Engulfing | Dark Cloud Cover | Evening Star | Evening Doji Star | Three Black Crows | Bearish Harami | Harami Cross | Tweezer Top | Bearish Abandoned Baby | Bearish Kicker | Falling Three Methods | Bearish Belt Hold | Three Inside Down | Three Outside Down | Bearish Breakaway

[Doji Patterns]: Standard Doji | Long-Legged Doji | Dragonfly Doji | Gravestone Doji | Four Price Doji | Tri-Star

[Continuation Patterns]: Tasuki Gap Up/Down | On Neck | In Neck | Thrusting Line | Mat Hold | Separating Lines

━━━ MULTI-LAYER CONFIRMATION ━━━
1. Candle strength: body/wick ratio, relative size
2. Primary trend: momentum, prior sequence
3. Support/Resistance: is pattern at a key level?
4. Confluence: do patterns align with trend?
5. Preceding candle: does it confirm the signal?
6. Pattern location in trend: top/bottom/middle?

━━━ HIGH ACCURACY RULE ━━━
• Only give signal with 3+ confirmation factors
• Calculate confidence precisely (max 92% unless very strong confluence)
• If image is unclear or few candles: respond ONLY with IMAGE_UNCLEAR

━━━ MANDATORY OUTPUT FORMAT ━━━

If image unclear: respond only with IMAGE_UNCLEAR

If image clear, use this exact format:

📊 **Market Analysis**
┌─────────────────────────────
│ Trend: [Bullish 📈 / Bearish 📉 / Sideways ↔️]
│ Timeframe: [visible or unspecified]
│ Main Pattern: [pattern name]
│ Signal Strength: [★★★★★ / ★★★★☆ / ★★★☆☆]
└─────────────────────────────

🕯️ **Next Candles Prediction**
```
Candle 1 ➜ ⬆️ CALL  │ ███████░░░ 78%
Candle 2 ➜ ⬆️ CALL  │ ██████░░░░ 71%
Candle 3 ➜ ⬇️ PUT   │ █████░░░░░ 65%
Candle 4 ➜ ⬆️ CALL  │ ███████░░░ 80%
Candle 5 ➜ ⬆️ CALL  │ ██████░░░░ 74%
```

━━━━━━━━━━━━━━━━━━━━━

🎯 **Overall Signal**
> ⬆️ **CALL — BUY**
> Overall Confidence: **82%**
> Suggested Expiry: **2 minutes**

━━━━━━━━━━━━━━━━━━━━━

📌 **Technical Details**
• [Detected pattern and reason in one sentence]
• [Strongest confirmation factor]
• [Any warning or special note]

⚠️ Risk Management: Never risk more than 3% of your capital per trade."""


async def ai_analyze(img_bytes: bytes, mime: str, lang: str) -> str:
    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"
    sys = SYSTEM_AR if lang == "ar" else SYSTEM_EN
    prompt = ("حلل هذا المخطط بدقة عالية." if lang == "ar"
               else "Analyze this chart with high accuracy.")

    kw = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        kw["base_url"] = OPENAI_BASE_URL
    client = AsyncOpenAI(**kw)

    r = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                {"type": "text", "text": prompt},
            ]},
        ],
    )
    return r.choices[0].message.content or ""

# ─────────────────────────────────────────────────────────
#  TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    """Set bot commands menu."""
    cmds_ar = [
        BotCommand("start", "🏠 الرئيسية"),
        BotCommand("subscribe", "💎 الاشتراك"),
        BotCommand("status", "📊 حالة اشتراكك"),
        BotCommand("language", "🌐 تغيير اللغة"),
        BotCommand("help", "❓ المساعدة"),
    ]
    cmds_en = [
        BotCommand("start", "🏠 Home"),
        BotCommand("subscribe", "💎 Subscribe"),
        BotCommand("status", "📊 Subscription status"),
        BotCommand("language", "🌐 Change language"),
        BotCommand("help", "❓ Help"),
    ]
    await app.bot.set_my_commands(cmds_ar)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    log.info("Bot commands menu set.")


async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db_upsert(uid, u.effective_user.username, u.effective_user.full_name)
    lang = ulang(uid)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    ]])
    await u.message.reply_text("🌐 اختر لغتك / Choose your language:", reply_markup=kb)


async def cb_lang(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    uid = q.from_user.id
    db_upsert(uid, q.from_user.username, q.from_user.full_name)
    lang = "ar" if q.data == "lang_ar" else "en"
    db_set_lang(uid, lang)
    await q.edit_message_text(f"{'✅ تم تحديد العربية' if lang=='ar' else '✅ English selected'}")
    await ctx.bot.send_message(chat_id=uid,
        text=tr("welcome", lang), parse_mode=ParseMode.MARKDOWN)


async def cmd_language(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    ]])
    await u.message.reply_text("🌐 اختر لغتك / Choose your language:", reply_markup=kb)


async def cmd_subscribe(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id; lang = ulang(uid)
    title = "💎 *خطط الاشتراك*\n\n" if lang == "ar" else "💎 *Subscription Plans*\n\n"
    for k, v in PLANS.items():
        label = v["label_ar"] if lang == "ar" else v["label_en"]
        days_txt = "يوم" if lang == "ar" else "days"
        title += f"• *{label}* — {v['price']} / {v['days']} {days_txt}\n"
    title += "\n" + ("اختر خطة:" if lang == "ar" else "Choose a plan:")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 {PLANS['weekly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['weekly']['price']}", callback_data="pay_weekly")],
        [InlineKeyboardButton(f"📆 {PLANS['monthly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['monthly']['price']}", callback_data="pay_monthly")],
        [InlineKeyboardButton(f"🗓️ {PLANS['yearly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['yearly']['price']}", callback_data="pay_yearly")],
    ])
    await u.message.reply_text(title, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def cb_pay(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    uid = q.from_user.id; lang = ulang(uid)
    plan_key = q.data.replace("pay_", "")
    plan = PLANS.get(plan_key, PLANS["monthly"])
    label = plan["label_ar"] if lang == "ar" else plan["label_en"]
    msg = PAYMENT_INFO[lang]
    msg += f"\n\n🔖 *{'الخطة' if lang=='ar' else 'Plan'}:* {label} — {plan['price']}"
    await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    if ADMIN_TELEGRAM_ID:
        try:
            await ctx.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=(f"💰 *طلب اشتراك جديد*\n\n"
                      f"المستخدم: @{q.from_user.username or 'N/A'} (`{uid}`)\n"
                      f"الخطة: *{label}* — {plan['price']}\n\n"
                      f"للتفعيل: `/grant {uid} {plan['days']}`"),
                parse_mode=ParseMode.MARKDOWN,
            )
        except: pass


async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id; lang = ulang(uid)
    usr = db_get_user(uid)
    if not usr:
        db_upsert(uid, u.effective_user.username, u.effective_user.full_name)
        usr = db_get_user(uid)
    if is_subscribed(usr):
        exp = datetime.fromisoformat(usr["sub_expiry"]).strftime("%Y-%m-%d %H:%M UTC")
        msg = tr("status_active", lang, expiry=exp)
    elif has_free(usr):
        n = FREE_TRIALS - usr["free_used"]
        msg = tr("status_trial", lang, n=n)
    else:
        msg = tr("status_none", lang)
    await u.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(tr("help", ulang(u.effective_user.id)), parse_mode=ParseMode.MARKDOWN)


async def handle_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db_upsert(uid, u.effective_user.username, u.effective_user.full_name)
    lang = ulang(uid)
    usr = db_get_user(uid)

    # Access check
    if not is_subscribed(usr) and not has_free(usr):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "💎 اشترك الآن" if lang=="ar" else "💎 Subscribe Now", callback_data="show_plans"
        )]])
        await u.message.reply_text(tr("no_access", lang), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    # Get file
    photo = u.message.photo[-1] if u.message.photo else None
    doc   = u.message.document if (u.message.document and
                                    u.message.document.mime_type and
                                    u.message.document.mime_type.startswith("image/")) else None

    if not photo and not doc:
        await u.message.reply_text(tr("text_only", lang))
        return

    # Size check — too small = unclear
    file_size = (photo.file_size if photo else doc.file_size) or 0
    if file_size < MIN_IMAGE_BYTES:
        await u.message.reply_text(tr("unclear", lang), parse_mode=ParseMode.MARKDOWN)
        return

    wait = await u.message.reply_text(tr("analyzing", lang), parse_mode=ParseMode.MARKDOWN)

    try:
        file_obj = await ctx.bot.get_file(photo.file_id if photo else doc.file_id)
        mime = "image/jpeg" if photo else (doc.mime_type or "image/jpeg")
        buf = BytesIO()
        await file_obj.download_to_memory(buf)
        img = buf.getvalue()

        result = await ai_analyze(img, mime, lang)

        # AI said image is unclear
        if "IMAGE_UNCLEAR" in result.upper():
            await wait.delete()
            await u.message.reply_text(tr("unclear", lang), parse_mode=ParseMode.MARKDOWN)
            db_log_analysis(uid, "unclear")
            return

        # Deduct free trial
        if not is_subscribed(usr):
            db_inc_free(uid)
            remaining = FREE_TRIALS - usr["free_used"] - 1
            if remaining > 0:
                result += tr("trial_left", lang, n=remaining)
            else:
                result += tr("trial_end", lang)

        title = "🔍 *تحليل الشموع اليابانية*\n\n" if lang=="ar" else "🔍 *Candlestick Analysis*\n\n"
        await wait.delete()
        await u.message.reply_text(title + result, parse_mode=ParseMode.MARKDOWN)
        db_log_analysis(uid, "success")
        db_log_msg(uid, "analysis", result[:300])

    except Exception as e:
        log.error(f"Analysis error uid={uid}: {e}")
        try: await wait.delete()
        except: pass
        await u.message.reply_text(tr("error", lang), parse_mode=ParseMode.MARKDOWN)
        db_log_analysis(uid, "error")


async def cb_show_plans(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    lang = ulang(q.from_user.id)
    title = "💎 *خطط الاشتراك*\n\n" if lang=="ar" else "💎 *Subscription Plans*\n\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 {PLANS['weekly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['weekly']['price']}", callback_data="pay_weekly")],
        [InlineKeyboardButton(f"📆 {PLANS['monthly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['monthly']['price']}", callback_data="pay_monthly")],
        [InlineKeyboardButton(f"🗓️ {PLANS['yearly']['label_ar' if lang=='ar' else 'label_en']} — {PLANS['yearly']['price']}", callback_data="pay_yearly")],
    ])
    await q.edit_message_text(title, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def handle_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = ulang(u.effective_user.id)
    await u.message.reply_text(tr("text_only", lang))

# ─────────────────────────────────────────────────────────
#  ADMIN TELEGRAM COMMANDS (via Telegram directly)
# ─────────────────────────────────────────────────────────
def is_admin(uid): return uid == ADMIN_TELEGRAM_ID

async def cmd_grant(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if len(ctx.args) < 2:
        await u.message.reply_text("Usage: /grant <user_id> <days>"); return
    try:
        tid, days = int(ctx.args[0]), int(ctx.args[1])
    except: await u.message.reply_text("❌ Invalid args"); return
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (tid,))
    con.commit(); con.close()
    expiry = db_grant(tid, days)
    exp_fmt = datetime.fromisoformat(expiry).strftime("%Y-%m-%d")
    await u.message.reply_text(f"✅ Granted {days} days to `{tid}`. Expires: {exp_fmt}", parse_mode=ParseMode.MARKDOWN)
    try:
        tu = db_get_user(tid)
        tl = tu["language"] if tu else "ar"
        await ctx.bot.send_message(tid, tr("granted_user", tl, expiry=exp_fmt), parse_mode=ParseMode.MARKDOWN)
    except: pass

async def cmd_revoke(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not ctx.args: await u.message.reply_text("Usage: /revoke <user_id>"); return
    try: tid = int(ctx.args[0])
    except: await u.message.reply_text("❌ Invalid"); return
    db_revoke(tid)
    await u.message.reply_text(f"✅ Revoked subscription for `{tid}`", parse_mode=ParseMode.MARKDOWN)
    try:
        tu = db_get_user(tid)
        tl = tu["language"] if tu else "ar"
        await ctx.bot.send_message(tid, tr("revoked_user", tl), parse_mode=ParseMode.MARKDOWN)
    except: pass

async def cmd_broadcast(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not ctx.args: await u.message.reply_text("Usage: /broadcast <message>"); return
    msg = " ".join(ctx.args)
    users = db_all_users()
    sent = fail = 0
    for usr in users:
        try:
            await ctx.bot.send_message(usr["user_id"], msg, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except: fail += 1
    await u.message.reply_text(f"📢 Sent: {sent} | Failed: {fail}")

# ─────────────────────────────────────────────────────────
#  WEB ADMIN PANEL HTML
# ─────────────────────────────────────────────────────────
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🤖 لوحة تحكم بوت التداول</title>
<style>
  :root {
    --bg: #0a0e1a; --bg2: #111827; --bg3: #1a2235;
    --accent: #00d4ff; --accent2: #7c3aed;
    --green: #10b981; --red: #ef4444; --yellow: #f59e0b;
    --text: #e2e8f0; --muted: #94a3b8;
    --radius: 12px; --shadow: 0 4px 24px rgba(0,0,0,0.4);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', Tahoma, Arial, sans-serif; min-height: 100vh; }
  /* NAV */
  .nav { background: var(--bg2); border-bottom: 1px solid #1e2d45; padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
  .nav-logo { font-size: 1.2rem; font-weight: 700; color: var(--accent); }
  .nav-badge { background: var(--green); color: #fff; padding: 3px 10px; border-radius: 20px; font-size: .75rem; }
  /* TABS */
  .tabs { display: flex; gap: 4px; padding: 16px 24px 0; border-bottom: 1px solid #1e2d45; background: var(--bg2); overflow-x: auto; }
  .tab { padding: 10px 20px; border: none; background: none; color: var(--muted); cursor: pointer; border-radius: var(--radius) var(--radius) 0 0; font-size: .9rem; white-space: nowrap; transition: all .2s; }
  .tab.active { background: var(--bg3); color: var(--accent); border-bottom: 2px solid var(--accent); }
  .tab:hover:not(.active) { color: var(--text); background: var(--bg3); }
  /* MAIN */
  .main { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .page { display: none; } .page.active { display: block; }
  /* CARDS */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: var(--bg3); border-radius: var(--radius); padding: 20px; border: 1px solid #1e2d45; }
  .card-icon { font-size: 2rem; margin-bottom: 8px; }
  .card-val { font-size: 2rem; font-weight: 700; color: var(--accent); }
  .card-lbl { color: var(--muted); font-size: .85rem; margin-top: 4px; }
  /* TABLE */
  .tbl-wrap { background: var(--bg3); border-radius: var(--radius); border: 1px solid #1e2d45; overflow: hidden; }
  .tbl-hdr { padding: 16px 20px; border-bottom: 1px solid #1e2d45; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
  .tbl-hdr h3 { font-size: 1rem; }
  input.search { background: var(--bg2); border: 1px solid #2d3748; color: var(--text); padding: 8px 14px; border-radius: 8px; font-size: .85rem; outline: none; }
  input.search:focus { border-color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th { background: var(--bg2); padding: 12px 14px; text-align: right; color: var(--muted); font-weight: 600; white-space: nowrap; }
  td { padding: 12px 14px; border-bottom: 1px solid #1a2235; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(0,212,255,.04); }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: .75rem; font-weight: 600; }
  .badge.active { background: rgba(16,185,129,.15); color: var(--green); }
  .badge.trial { background: rgba(245,158,11,.15); color: var(--yellow); }
  .badge.expired { background: rgba(239,68,68,.15); color: var(--red); }
  /* BUTTONS */
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border: none; border-radius: 8px; cursor: pointer; font-size: .85rem; transition: all .2s; font-weight: 500; }
  .btn-primary { background: linear-gradient(135deg,var(--accent),#0095b3); color: #fff; }
  .btn-success { background: rgba(16,185,129,.15); color: var(--green); border: 1px solid var(--green); }
  .btn-danger  { background: rgba(239,68,68,.15); color: var(--red); border: 1px solid var(--red); }
  .btn-warning { background: rgba(245,158,11,.15); color: var(--yellow); border: 1px solid var(--yellow); }
  .btn-purple  { background: rgba(124,58,237,.2); color: #a78bfa; border: 1px solid #7c3aed; }
  .btn:hover { opacity: .85; transform: translateY(-1px); }
  .btn-sm { padding: 5px 10px; font-size: .78rem; }
  /* MODAL */
  .overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 200; align-items: center; justify-content: center; }
  .overlay.show { display: flex; }
  .modal { background: var(--bg2); border-radius: var(--radius); padding: 28px; width: 480px; max-width: 95vw; border: 1px solid #1e2d45; box-shadow: var(--shadow); }
  .modal h3 { margin-bottom: 18px; color: var(--accent); }
  .form-group { margin-bottom: 14px; }
  label { display: block; margin-bottom: 6px; color: var(--muted); font-size: .85rem; }
  input, textarea, select { width: 100%; background: var(--bg3); border: 1px solid #2d3748; color: var(--text); padding: 10px 14px; border-radius: 8px; font-size: .9rem; outline: none; }
  input:focus, textarea:focus, select:focus { border-color: var(--accent); }
  textarea { resize: vertical; min-height: 100px; font-family: inherit; }
  .modal-footer { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  /* TOAST */
  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--bg3); border: 1px solid var(--accent); color: var(--text); padding: 12px 24px; border-radius: 8px; font-size: .9rem; z-index: 300; display: none; }
  .toast.show { display: block; animation: slideUp .3s; }
  @keyframes slideUp { from { opacity:0; transform: translateX(-50%) translateY(20px); } to { opacity:1; transform: translateX(-50%) translateY(0); } }
  /* LOGIN */
  .login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .login-box { background: var(--bg2); border: 1px solid #1e2d45; border-radius: var(--radius); padding: 40px; width: 380px; text-align: center; box-shadow: var(--shadow); }
  .login-box h2 { color: var(--accent); margin-bottom: 8px; }
  .login-box p { color: var(--muted); font-size: .9rem; margin-bottom: 24px; }
  .login-box input { margin-bottom: 14px; text-align: center; }
  .login-box .btn { width: 100%; justify-content: center; padding: 12px; font-size: 1rem; }
  /* BACKUP */
  .backup-card { background: var(--bg3); border-radius: var(--radius); padding: 24px; border: 1px solid #1e2d45; text-align: center; }
  .backup-card h3 { margin-bottom: 8px; }
  .backup-card p { color: var(--muted); font-size: .9rem; margin-bottom: 18px; }
  .backup-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-top: 24px; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,.3); border-top-color: #fff; border-radius: 50%; animation: spin .6s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

{% if not logged_in %}
<div class="login-wrap">
  <div class="login-box">
    <div style="font-size:3rem;margin-bottom:12px">🤖</div>
    <h2>لوحة تحكم البوت</h2>
    <p>أدخل كلمة المرور للدخول</p>
    <form method="POST" action="/login">
      <input type="password" name="password" placeholder="كلمة المرور" required autofocus>
      <button class="btn btn-primary" type="submit">دخول ←</button>
    </form>
    {% if error %}<p style="color:var(--red);margin-top:12px">❌ كلمة مرور خاطئة</p>{% endif %}
  </div>
</div>
{% else %}

<div class="nav">
  <span class="nav-logo">🤖 بوت التداول الاحترافي</span>
  <div style="display:flex;gap:10px;align-items:center">
    <span class="nav-badge">● نشط</span>
    <a href="/logout" class="btn btn-danger btn-sm">خروج</a>
  </div>
</div>

<div class="tabs">
  <button class="tab active" onclick="showPage('dashboard',this)">📊 لوحة التحكم</button>
  <button class="tab" onclick="showPage('subscribers',this)">👥 المشتركون</button>
  <button class="tab" onclick="showPage('messages',this)">💬 الرسائل</button>
  <button class="tab" onclick="showPage('backup',this)">💾 النسخ الاحتياطي</button>
  <button class="tab" onclick="showPage('settings',this)">⚙️ الإعدادات</button>
</div>

<div class="main">

  <!-- DASHBOARD -->
  <div id="page-dashboard" class="page active">
    <div class="cards" id="stats-cards">
      <div class="card"><div class="card-icon">👥</div><div class="card-val" id="s-total">—</div><div class="card-lbl">إجمالي المستخدمين</div></div>
      <div class="card"><div class="card-icon">✅</div><div class="card-val" id="s-active">—</div><div class="card-lbl">اشتراكات نشطة</div></div>
      <div class="card"><div class="card-icon">🔍</div><div class="card-val" id="s-analyses">—</div><div class="card-lbl">تحليلات كلية</div></div>
      <div class="card"><div class="card-icon">🆕</div><div class="card-val" id="s-today">—</div><div class="card-lbl">جديد اليوم</div></div>
    </div>
    <div class="tbl-wrap">
      <div class="tbl-hdr"><h3>🕐 آخر المستخدمين</h3><button class="btn btn-primary btn-sm" onclick="loadStats()">🔄 تحديث</button></div>
      <table id="recent-table">
        <thead><tr><th>المعرف</th><th>الاسم</th><th>@يوزرنيم</th><th>الاشتراك</th><th>التجربة</th><th>الإجراءات</th></tr></thead>
        <tbody id="recent-body"></tbody>
      </table>
    </div>
  </div>

  <!-- SUBSCRIBERS -->
  <div id="page-subscribers" class="page">
    <div class="tbl-wrap">
      <div class="tbl-hdr">
        <h3>👥 إدارة المشتركين</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <input class="search" type="text" placeholder="🔍 بحث..." oninput="filterUsers(this.value)" id="search-input">
          <button class="btn btn-success btn-sm" onclick="openAddModal()">➕ إضافة</button>
          <button class="btn btn-warning btn-sm" onclick="loadUsers()">🔄 تحديث</button>
        </div>
      </div>
      <table>
        <thead><tr><th>المعرف</th><th>الاسم</th><th>يوزرنيم</th><th>اللغة</th><th>الاشتراك</th><th>التجربة</th><th>ملاحظات</th><th>الإجراءات</th></tr></thead>
        <tbody id="users-body"></tbody>
      </table>
    </div>
  </div>

  <!-- MESSAGES -->
  <div id="page-messages" class="page">
    <div class="tbl-wrap" style="margin-bottom:20px">
      <div class="tbl-hdr"><h3>📢 إرسال رسالة جماعية</h3></div>
      <div style="padding:20px">
        <div class="form-group">
          <label>المستهدفون</label>
          <select id="msg-target">
            <option value="all">الكل</option>
            <option value="active">المشتركون النشطون فقط</option>
            <option value="trial">المستخدمون في التجربة</option>
          </select>
        </div>
        <div class="form-group">
          <label>الرسالة (يدعم Markdown)</label>
          <textarea id="msg-text" placeholder="اكتب رسالتك هنا...&#10;&#10;يمكن استخدام **عريض** أو _مائل_"></textarea>
        </div>
        <button class="btn btn-primary" onclick="sendBroadcast()">📤 إرسال للجميع</button>
      </div>
    </div>
    <div class="tbl-wrap">
      <div class="tbl-hdr"><h3>💬 إرسال رسالة لمستخدم محدد</h3></div>
      <div style="padding:20px;display:flex;gap:12px;flex-wrap:wrap">
        <input type="text" id="dm-uid" placeholder="معرف المستخدم..." style="flex:1;min-width:180px">
        <textarea id="dm-text" placeholder="الرسالة..." style="flex:2;min-width:220px;min-height:60px"></textarea>
        <button class="btn btn-primary" style="align-self:flex-end" onclick="sendDM()">📨 إرسال</button>
      </div>
    </div>
  </div>

  <!-- BACKUP -->
  <div id="page-backup" class="page">
    <div class="backup-grid">
      <div class="backup-card">
        <div style="font-size:2.5rem;margin-bottom:8px">📥</div>
        <h3>تصدير CSV</h3>
        <p>تنزيل قاعدة بيانات المستخدمين بصيغة Excel/CSV</p>
        <button class="btn btn-success" onclick="downloadBackup('csv')">⬇️ تنزيل CSV</button>
      </div>
      <div class="backup-card">
        <div style="font-size:2.5rem;margin-bottom:8px">📦</div>
        <h3>تصدير JSON</h3>
        <p>تنزيل بيانات كاملة بصيغة JSON للاستيراد مستقبلاً</p>
        <button class="btn btn-purple" onclick="downloadBackup('json')">⬇️ تنزيل JSON</button>
      </div>
      <div class="backup-card">
        <div style="font-size:2.5rem;margin-bottom:8px">📋</div>
        <h3>قائمة المشتركين النشطين</h3>
        <p>تصدير قائمة المشتركين النشطين فقط بصيغة TXT</p>
        <button class="btn btn-warning" onclick="downloadBackup('active')">⬇️ تنزيل TXT</button>
      </div>
      <div class="backup-card">
        <div style="font-size:2.5rem;margin-bottom:8px">📊</div>
        <h3>إحصائيات مفصلة</h3>
        <p>تقرير كامل عن استخدام البوت والاشتراكات</p>
        <button class="btn btn-primary" onclick="downloadBackup('stats')">⬇️ تنزيل التقرير</button>
      </div>
    </div>
  </div>

  <!-- SETTINGS -->
  <div id="page-settings" class="page">
    <div style="max-width:600px">
      <div class="tbl-wrap" style="margin-bottom:20px">
        <div class="tbl-hdr"><h3>ℹ️ معلومات البوت</h3></div>
        <div style="padding:20px">
          <div style="display:grid;gap:12px">
            <div><span style="color:var(--muted)">التوكن:</span> <code style="color:var(--accent);font-size:.8rem">{{ bot_token[:20] }}...</code></div>
            <div><span style="color:var(--muted)">قاعدة البيانات:</span> <code style="color:var(--accent)">{{ db_path }}</code></div>
            <div><span style="color:var(--muted)">التجارب المجانية:</span> <code style="color:var(--accent)">{{ free_trials }}</code></div>
          </div>
        </div>
      </div>
      <div class="tbl-wrap">
        <div class="tbl-hdr"><h3>🔑 تغيير كلمة المرور</h3></div>
        <div style="padding:20px">
          <div class="form-group"><label>كلمة المرور الجديدة</label><input type="password" id="new-pass"></div>
          <div class="form-group"><label>تأكيد كلمة المرور</label><input type="password" id="conf-pass"></div>
          <p style="color:var(--muted);font-size:.8rem;margin-bottom:14px">⚠️ لتطبيق كلمة المرور الجديدة بشكل دائم، عدّلها في ملف bot.py</p>
          <button class="btn btn-primary" onclick="changePass()">💾 حفظ</button>
        </div>
      </div>
    </div>
  </div>

</div><!-- main -->

<!-- MODALS -->
<div class="overlay" id="modal-add">
  <div class="modal">
    <h3>➕ إضافة مشترك</h3>
    <div class="form-group"><label>معرف المستخدم (User ID)</label><input type="number" id="add-uid" placeholder="مثال: 123456789"></div>
    <div class="form-group"><label>عدد الأيام</label>
      <select id="add-days">
        <option value="7">7 أيام (أسبوعي)</option>
        <option value="30" selected>30 يوم (شهري)</option>
        <option value="365">365 يوم (سنوي)</option>
        <option value="custom">مخصص</option>
      </select>
    </div>
    <div class="form-group" id="custom-days-wrap" style="display:none"><label>أيام مخصصة</label><input type="number" id="custom-days-val" placeholder="عدد الأيام" min="1"></div>
    <div class="form-group"><label>ملاحظة (اختياري)</label><input type="text" id="add-note" placeholder="مثال: دفع شهر يناير"></div>
    <div class="modal-footer">
      <button class="btn btn-danger" onclick="closeModal('modal-add')">إلغاء</button>
      <button class="btn btn-success" onclick="addSubscriber()">✅ تفعيل الاشتراك</button>
    </div>
  </div>
</div>

<div class="overlay" id="modal-msg">
  <div class="modal">
    <h3>💬 إرسال رسالة للمستخدم <span id="msg-uid-display"></span></h3>
    <div class="form-group"><textarea id="modal-msg-text" placeholder="اكتب رسالتك هنا..."></textarea></div>
    <div class="modal-footer">
      <button class="btn btn-danger" onclick="closeModal('modal-msg')">إلغاء</button>
      <button class="btn btn-primary" onclick="sendModalMsg()">📨 إرسال</button>
    </div>
  </div>
</div>

<div class="overlay" id="modal-extend">
  <div class="modal">
    <h3>📅 تمديد اشتراك <span id="extend-uid-display"></span></h3>
    <div class="form-group">
      <label>أيام إضافية</label>
      <select id="extend-days">
        <option value="7">7 أيام</option>
        <option value="30" selected>30 يوم</option>
        <option value="90">90 يوم</option>
        <option value="365">365 يوم</option>
      </select>
    </div>
    <div class="modal-footer">
      <button class="btn btn-danger" onclick="closeModal('modal-extend')">إلغاء</button>
      <button class="btn btn-success" onclick="doExtend()">✅ تمديد</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

{% endif %}

<script>
let allUsers = [], currentMsgUid = null, currentExtendUid = null;

function showPage(id, btn) {
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  btn.classList.add('active');
  if(id==='dashboard') loadStats();
  if(id==='subscribers') loadUsers();
}

function toast(msg, color='var(--green)') {
  const el = document.getElementById('toast');
  el.textContent = msg; el.style.borderColor = color;
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 3000);
}

async function loadStats() {
  const r = await fetch('/api/stats'); const d = await r.json();
  document.getElementById('s-total').textContent = d.total;
  document.getElementById('s-active').textContent = d.active_subs;
  document.getElementById('s-analyses').textContent = d.analyses;
  document.getElementById('s-today').textContent = d.new_today;
  // recent users
  const users = d.recent || [];
  const tbody = document.getElementById('recent-body');
  tbody.innerHTML = users.map(u => `
    <tr>
      <td><code>${u.user_id}</code></td>
      <td>${u.full_name||'—'}</td>
      <td>${u.username?'@'+u.username:'—'}</td>
      <td>${subBadge(u)}</td>
      <td>${u.free_used}/${d.free_trials}</td>
      <td><div style="display:flex;gap:6px">
        <button class="btn btn-success btn-sm" onclick="openExtendModal(${u.user_id})">📅 تمديد</button>
        <button class="btn btn-warning btn-sm" onclick="openMsgModal(${u.user_id})">💬</button>
        <button class="btn btn-danger btn-sm" onclick="revokeUser(${u.user_id})">🗑️</button>
      </div></td>
    </tr>`).join('');
}

async function loadUsers() {
  const r = await fetch('/api/users'); allUsers = await r.json();
  renderUsers(allUsers);
}

function filterUsers(q) {
  const f = q.toLowerCase();
  renderUsers(allUsers.filter(u =>
    String(u.user_id).includes(f) ||
    (u.username||'').toLowerCase().includes(f) ||
    (u.full_name||'').toLowerCase().includes(f)
  ));
}

function subBadge(u) {
  if(!u.sub_expiry) return '<span class="badge trial">تجربة</span>';
  const exp = new Date(u.sub_expiry);
  if(exp > new Date()) {
    const days = Math.ceil((exp-new Date())/86400000);
    return `<span class="badge active">✅ ${days} يوم</span>`;
  }
  return '<span class="badge expired">منتهي</span>';
}

function renderUsers(users) {
  document.getElementById('users-body').innerHTML = users.map(u=>`
    <tr>
      <td><code>${u.user_id}</code></td>
      <td>${u.full_name||'—'}</td>
      <td>${u.username?'@'+u.username:'—'}</td>
      <td>${u.language==='ar'?'🇸🇦 عربي':'🇬🇧 EN'}</td>
      <td>${subBadge(u)}</td>
      <td>${u.free_used}/${{{ free_trials }}}</td>
      <td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">${u.notes||'—'}</td>
      <td><div style="display:flex;gap:5px;flex-wrap:wrap">
        <button class="btn btn-success btn-sm" onclick="openExtendModal(${u.user_id})">📅</button>
        <button class="btn btn-warning btn-sm" onclick="openMsgModal(${u.user_id})">💬</button>
        <button class="btn btn-danger btn-sm" onclick="revokeUser(${u.user_id})">🗑️</button>
      </div></td>
    </tr>`).join('');
}

function openAddModal() {
  document.getElementById('add-uid').value='';
  document.getElementById('add-note').value='';
  document.getElementById('modal-add').classList.add('show');
}
document.getElementById('add-days').addEventListener('change', function(){
  document.getElementById('custom-days-wrap').style.display = this.value==='custom'?'block':'none';
});

async function addSubscriber() {
  const uid = document.getElementById('add-uid').value;
  let days = document.getElementById('add-days').value;
  if(days==='custom') days = document.getElementById('custom-days-val').value;
  const note = document.getElementById('add-note').value;
  if(!uid||!days){toast('❌ أدخل المعرف والأيام','var(--red)');return;}
  const r = await fetch('/api/grant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:+uid,days:+days,note})});
  const d = await r.json();
  if(d.ok){toast('✅ تم تفعيل الاشتراك!');closeModal('modal-add');loadUsers();}
  else toast('❌ '+d.error,'var(--red)');
}

function openMsgModal(uid) {
  currentMsgUid = uid;
  document.getElementById('msg-uid-display').textContent = uid;
  document.getElementById('modal-msg-text').value='';
  document.getElementById('modal-msg').classList.add('show');
}

async function sendModalMsg() {
  const text = document.getElementById('modal-msg-text').value;
  if(!text){toast('❌ اكتب الرسالة','var(--red)');return;}
  const r = await fetch('/api/send_message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:currentMsgUid,message:text})});
  const d = await r.json();
  if(d.ok){toast('✅ تم الإرسال!');closeModal('modal-msg');}
  else toast('❌ '+d.error,'var(--red)');
}

function openExtendModal(uid) {
  currentExtendUid = uid;
  document.getElementById('extend-uid-display').textContent = uid;
  document.getElementById('modal-extend').classList.add('show');
}

async function doExtend() {
  const days = document.getElementById('extend-days').value;
  const r = await fetch('/api/grant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:currentExtendUid,days:+days,note:''})});
  const d = await r.json();
  if(d.ok){toast('✅ تم التمديد!');closeModal('modal-extend');loadUsers();}
  else toast('❌ '+d.error,'var(--red)');
}

async function revokeUser(uid) {
  if(!confirm('هل تريد إلغاء اشتراك المستخدم '+uid+'؟')) return;
  const r = await fetch('/api/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid})});
  const d = await r.json();
  if(d.ok){toast('✅ تم الإلغاء');loadUsers();loadStats();}
  else toast('❌ خطأ','var(--red)');
}

async function sendBroadcast() {
  const target = document.getElementById('msg-target').value;
  const text = document.getElementById('msg-text').value;
  if(!text){toast('❌ اكتب الرسالة','var(--red)');return;}
  if(!confirm('إرسال الرسالة لجميع المستخدمين المستهدفين؟')) return;
  const r = await fetch('/api/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target,message:text})});
  const d = await r.json();
  toast(`✅ أُرسل: ${d.sent} | فشل: ${d.failed}`);
}

async function sendDM() {
  const uid = document.getElementById('dm-uid').value;
  const text = document.getElementById('dm-text').value;
  if(!uid||!text){toast('❌ أدخل المعرف والرسالة','var(--red)');return;}
  const r = await fetch('/api/send_message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:+uid,message:text})});
  const d = await r.json();
  if(d.ok) toast('✅ تم الإرسال!');
  else toast('❌ '+d.error,'var(--red)');
}

function downloadBackup(type) {
  window.location.href = '/api/backup/'+type;
}

function closeModal(id) { document.getElementById(id).classList.remove('show'); }
document.querySelectorAll('.overlay').forEach(el=>el.addEventListener('click',e=>{if(e.target===el)el.classList.remove('show');}));

function changePass() {
  const np = document.getElementById('new-pass').value;
  const cp = document.getElementById('conf-pass').value;
  if(!np){toast('❌ أدخل كلمة المرور','var(--red)');return;}
  if(np!==cp){toast('❌ كلمتا المرور لا تتطابقان','var(--red)');return;}
  toast('ℹ️ عدّل WEB_PANEL_PASSWORD في ملف bot.py لحفظها بشكل دائم','var(--yellow)');
}

// Initial load
loadStats();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────
#  FLASK WEB APP
# ─────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = secrets.token_hex(32)

# Shared bot reference so Flask can send messages
_bot_app: Application | None = None

def set_bot_app(app: Application):
    global _bot_app
    _bot_app = app


@flask_app.route("/login", methods=["POST"])
def login():
    pw = request.form.get("password","")
    if pw == WEB_PANEL_PASSWORD:
        session["logged_in"] = True
        return redirect("/")
    return render_template_string(ADMIN_HTML, logged_in=False, error=True,
                                  bot_token=TELEGRAM_TOKEN, db_path=DB_PATH, free_trials=FREE_TRIALS)

@flask_app.route("/logout")
def logout():
    session.clear(); return redirect("/")

@flask_app.route("/")
def index():
    li = session.get("logged_in", False)
    return render_template_string(ADMIN_HTML, logged_in=li, error=False,
                                  bot_token=TELEGRAM_TOKEN, db_path=DB_PATH, free_trials=FREE_TRIALS)

@flask_app.route("/api/stats")
def api_stats():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    s = db_stats()
    users = db_all_users()[:10]
    s["recent"] = users
    s["free_trials"] = FREE_TRIALS
    return jsonify(s)

@flask_app.route("/api/users")
def api_users():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    return jsonify(db_all_users())

@flask_app.route("/api/grant", methods=["POST"])
def api_grant():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    d = request.json or {}
    uid = d.get("user_id"); days = d.get("days"); note = d.get("note","")
    if not uid or not days: return jsonify({"ok":False,"error":"missing fields"})
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    con.commit(); con.close()
    if note: db_set_note(uid, note)
    expiry = db_grant(uid, days)
    exp_fmt = datetime.fromisoformat(expiry).strftime("%Y-%m-%d")
    if _bot_app:
        async def _notify():
            try:
                tu = db_get_user(uid)
                tl = tu["language"] if tu else "ar"
                await _bot_app.bot.send_message(uid, tr("granted_user", tl, expiry=exp_fmt), parse_mode=ParseMode.MARKDOWN)
            except: pass
        asyncio.run_coroutine_threadsafe(_notify(), _bot_app.update_queue._loop if hasattr(_bot_app.update_queue,'_loop') else asyncio.get_event_loop())
    return jsonify({"ok":True,"expiry":exp_fmt})

@flask_app.route("/api/revoke", methods=["POST"])
def api_revoke():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    uid = (request.json or {}).get("user_id")
    if not uid: return jsonify({"ok":False,"error":"missing uid"})
    db_revoke(uid)
    if _bot_app:
        async def _notify():
            try:
                tu = db_get_user(uid)
                tl = tu["language"] if tu else "ar"
                await _bot_app.bot.send_message(uid, tr("revoked_user", tl), parse_mode=ParseMode.MARKDOWN)
            except: pass
        asyncio.run_coroutine_threadsafe(_notify(), asyncio.get_event_loop())
    return jsonify({"ok":True})

@flask_app.route("/api/send_message", methods=["POST"])
def api_send_message():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    d = request.json or {}
    uid = d.get("user_id"); msg = d.get("message","")
    if not uid or not msg: return jsonify({"ok":False,"error":"missing fields"})
    if not _bot_app: return jsonify({"ok":False,"error":"bot not ready"})
    result = {"ok":False,"error":"unknown"}
    evt = threading.Event()
    async def _send():
        try:
            await _bot_app.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN)
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
        finally:
            evt.set()
    fut = asyncio.run_coroutine_threadsafe(_send(), _loop)
    evt.wait(timeout=10)
    db_log_msg(uid, "admin→user", msg)
    return jsonify(result)

@flask_app.route("/api/broadcast", methods=["POST"])
def api_broadcast():
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    d = request.json or {}
    target = d.get("target","all"); msg = d.get("message","")
    if not msg: return jsonify({"ok":False,"error":"no message"})
    users = db_all_users()
    now = datetime.utcnow()
    if target == "active":
        users = [u for u in users if u.get("sub_expiry") and datetime.fromisoformat(u["sub_expiry"]) > now]
    elif target == "trial":
        users = [u for u in users if not u.get("sub_expiry") or datetime.fromisoformat(u["sub_expiry"]) <= now]
    if not _bot_app: return jsonify({"ok":False,"error":"bot not ready"})
    sent = fail = 0
    for u in users:
        evt = threading.Event()
        async def _s(uid=u["user_id"]):
            try: await _bot_app.bot.send_message(uid, msg, parse_mode=ParseMode.MARKDOWN); return True
            except: return False
        fut = asyncio.run_coroutine_threadsafe(_s(), _loop)
        try:
            if fut.result(timeout=5): sent += 1
            else: fail += 1
        except: fail += 1
    return jsonify({"ok":True,"sent":sent,"failed":fail})

@flask_app.route("/api/backup/<fmt>")
def api_backup(fmt):
    if not session.get("logged_in"): return jsonify({"error":"auth"}), 401
    users = db_all_users()
    now_str = datetime.utcnow().strftime("%Y%m%d_%H%M")

    if fmt == "json":
        data = json.dumps(users, ensure_ascii=False, indent=2, default=str)
        return flask_app.response_class(
            data.encode("utf-8"),
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=backup_{now_str}.json"}
        )
    elif fmt == "active":
        active = [u for u in users if u.get("sub_expiry") and datetime.fromisoformat(u["sub_expiry"]) > datetime.utcnow()]
        lines = [f"{u['user_id']} | @{u['username'] or 'N/A'} | {u['full_name'] or 'N/A'} | expires:{u['sub_expiry'][:10]}" for u in active]
        return flask_app.response_class(
            "\n".join(lines).encode("utf-8"),
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=active_subscribers_{now_str}.txt"}
        )
    elif fmt == "stats":
        s = db_stats()
        report = f"""بوت التداول الاحترافي — تقرير إحصائي
========================================
التاريخ: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

إجمالي المستخدمين: {s['total']}
الاشتراكات النشطة: {s['active_subs']}
إجمالي التحليلات: {s['analyses']}
مستخدمون جدد اليوم: {s['new_today']}
"""
        return flask_app.response_class(
            report.encode("utf-8"),
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=stats_{now_str}.txt"}
        )
    else:  # csv
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["user_id","username","full_name","language","free_used","sub_expiry","notes","created_at"])
        writer.writeheader()
        writer.writerows(users)
        return flask_app.response_class(
            ("\ufeff" + out.getvalue()).encode("utf-8"),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=users_{now_str}.csv"}
        )

# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop = None

def run_flask():
    log.info(f"🌐 Web panel: http://localhost:{WEB_PANEL_PORT}  |  Password: {WEB_PANEL_PASSWORD}")
    flask_app.run(host="0.0.0.0", port=WEB_PANEL_PORT, debug=False, use_reloader=False)

async def run_bot():
    global _loop
    _loop = asyncio.get_event_loop()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    set_bot_app(app)

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("language",  cmd_language))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("help",      cmd_help))
    # Admin shortcuts via Telegram
    app.add_handler(CommandHandler("grant",     cmd_grant))
    app.add_handler(CommandHandler("revoke",    cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_lang,       pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(cb_pay,        pattern="^pay_"))
    app.add_handler(CallbackQueryHandler(cb_show_plans, pattern="^show_plans$"))

    # Media
    app.add_handler(MessageHandler(filters.PHOTO,           handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE,  handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("🤖 Telegram bot starting (long polling)...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message","callback_query"])
    log.info("✅ Bot is running!")

    try:
        await asyncio.Event().wait()   # Run forever
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    init_db()
    log.info("✅ Database ready")

    # Start Flask in background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Run Telegram bot in main async loop
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
