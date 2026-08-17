import os
import json
import base64
import logging
import hashlib
import hmac
import io
import math
import threading
import uuid
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, Response
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ─── مسارات الملفات ───────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(_ROOT, "templates"),
    static_folder=os.path.join(_ROOT, "static"),
    static_url_path="/static",
)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector")

# ─── Config ──────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "").strip()
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "").strip()
ADMIN_SECRET_CODE   = os.environ.get("ADMIN_SECRET_CODE", "").strip()
SITE_URL            = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "ai-inspector-tau.vercel.app") or "ai-inspector-tau.vercel.app"
FREE_CREDITS        = 3
EXEMPT_CREDITS      = 999

FALLBACK_LOGO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAJAAAAB6CAYAAAAF"
    "c1g9AAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJ0UkG"
    "AAAAAAgI0AABz7lNQAAAAJ0UkGAAAAAABJRU5ErkJggg=="
)

# ─── PostgreSQL (Neon) ────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

EXEMPT_EMAILS = {
    "akashiiso04@gmail.com",
    "muhannadd0594@gmail.com",
    "mohammdlghmd@gmail.com",
}

PLAN_CREDITS = {
    "e810b85b-5273-4da2-9477-f3cf62f9737d": ("basic", 10),
    "db680fa5-9ec4-4fed-81fe-0ad4928266c3": ("pro",   50),
    "ceff30c8-9ba9-4c2a-bfb8-0cd520a9c072": ("vip",  120),
}

PHOTO_LIMIT_MAP = {"free": 1, "basic": 1, "pro": 2, "vip": 4, "exempt": 4}

def fallback_homepage_html():
    return """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>AI Inspector</title>
<style>body{font-family:Tahoma,Arial,sans-serif;background:#020617;color:#e2e8f0;margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;}
.box{max-width:700px;background:rgba(15,23,42,0.95);border:1px solid #334155;border-radius:20px;text-align:center;padding:40px 28px;}</style></head>
<body><div class="box"><h1>AI Inspector</h1><p>Loading...</p></div></body></html>"""

# ─── Temp Email Domains Blacklist ────────────────────────────────────────────
TEMP_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
    "guerrillamail.biz", "guerrillamail.de", "guerrillamail.info", "guerrillamail.me",
    "sharklasers.com", "guerrillamailblock.com", "grr.la", "spam4.me",
    "yopmail.com", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf", "nospam.ze.tc",
    "nomail.xl.cx", "mega.zik.dj", "speed.1s.fr", "courriel.fr.nf",
    "moncourrier.fr.nf", "monemail.fr.nf", "monmail.fr.nf",
    "tempmail.com", "temp-mail.org", "tempmail.net", "tempmail.io",
    "throwam.com", "throwam.net", "dispostable.com", "mailnull.com",
    "maildrop.cc", "trashmail.at", "trashmail.com", "trashmail.io",
    "trashmail.me", "trashmail.net", "trashmail.org", "trashmail.xyz",
    "fakeinbox.com", "mailnesia.com", "spamgourmet.com",
    "spamgourmet.net", "spamgourmet.org", "spamspot.com", "spamthis.co.uk",
    "spamtrap.ro", "spaml.com", "spaml.de", "spamoff.de",
    "10minutemail.com", "10minutemail.net", "10minutemail.org",
    "10minutemail.co.uk", "10minutemail.de", "10minutemail.it",
    "10minutemail.ru", "10minutemail.be", "10minutemail.nl",
    "20minutemail.com", "20minutemail.it", "minutemailbox.com", "mintemail.com",
    "mohmal.com", "moakt.com", "moakt.co", "moakt.ws",
    "discard.email", "discardmail.com", "discardmail.de",
    "throwaway.email", "emailondeck.com", "getairmail.com", "filzmail.com",
    "zetmail.com", "spamgob.com", "binkmail.com", "bobmail.info",
    "dayrep.com", "einrot.com", "fleckens.hu", "gustr.com",
    "hatespam.org", "inoutmail.de", "inoutmail.eu", "inoutmail.info",
    "inoutmail.net", "jnxjn.com", "jourrapide.com", "lazyinbox.com",
    "lookugly.com", "mt2014.com", "mt2015.com", "objectmail.com",
    "obobbo.com", "proxymail.eu", "rcpt.at", "rklips.com",
    "rmqkr.net", "royal.net", "rppkn.com", "rtrtr.com",
    "s0ny.net", "safe-mail.net", "safetymail.info", "safetypost.de",
    "sharedmailbox.org", "skeefmail.com", "slopsbox.com", "smellfear.com",
    "snakemail.com", "sneakemail.com", "sofimail.com", "sogetthis.com",
    "spamfree24.de", "spamfree24.eu", "spamfree24.info", "spamfree24.net",
    "spamfree24.org", "spamfree.eu", "tempinbox.com", "tempemail.net", "tempemail.com",
    "thanksnospam.info", "thisisnotmyrealemail.com", "tradermail.info",
    "trash-mail.at", "trash-mail.com", "trash-mail.de", "trash-mail.io",
    "trash-mail.net", "trash2009.com", "trashdevil.com", "trashdevil.de",
    "trashemail.de", "trashimail.com", "trbvm.com", "turual.com",
    "tyldd.com", "uggsrock.com", "wegwerfmail.de", "wegwerfmail.net",
    "wegwerfmail.org", "wh4f.org", "whyspam.me", "willhackforfood.biz",
    "willselfdestruct.com", "wilemail.com", "wimsg.com", "wronghead.com",
    "wuzupmail.net", "xagloo.com", "xemaps.com", "xents.com",
    "xmaily.com", "xoxy.net", "yapped.net", "yeah.net",
    "yep.it", "yogamaven.com", "yuurok.com", "zehnminutenmail.de",
    "zippymail.info", "zoemail.net", "zoemail.org",
    "emlpro.com", "emltmp.com", "emlhub.com",
}

def is_temp_email(email_addr: str) -> bool:
    try:
        domain = email_addr.strip().lower().split("@")[1]
        return domain in TEMP_EMAIL_DOMAINS
    except IndexError:
        return True

# ─── DB Connection ────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY, credits INTEGER DEFAULT 0,
                plan TEXT DEFAULT 'free', updated_at TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
                report TEXT, credits INTEGER, cost INTEGER, plan TEXT,
                is_exempt BOOLEAN DEFAULT FALSE, error TEXT,
                ts DOUBLE PRECISION, created_at TIMESTAMP DEFAULT NOW())""")
        conn.commit()

try:
    init_db()
except Exception as _e:
    log.error("DB init error: %s", _e)

# ─── Routes (home) ───────────────────────────────────────────────────────────
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/favicon.ico')
@app.route('/favicon.png')
def favicon_redirect():
    logo_path = os.path.join(_ROOT, 'static', 'logo.png')
    if os.path.exists(logo_path):
        return app.send_static_file('logo.png')
    return send_file(io.BytesIO(FALLBACK_LOGO_PNG), mimetype='image/png', as_attachment=False)

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(os.path.join(_ROOT, 'static'), filename)

# ─── User Helpers ─────────────────────────────────────────────────────────────
def is_exempt(email_addr):
    return email_addr.strip().lower() in EXEMPT_EMAILS

def get_or_create_user(email_addr):
    email_addr = email_addr.strip().lower()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if is_exempt(email_addr):
                cur.execute("""INSERT INTO users (email, credits, plan, updated_at) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET credits=EXCLUDED.credits, plan=EXCLUDED.plan, updated_at=EXCLUDED.updated_at""",
                    (email_addr, EXEMPT_CREDITS, "vip", now))
                conn.commit()
                return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "vip"}
            cur.execute("SELECT * FROM users WHERE email = %s", (email_addr,))
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute("INSERT INTO users (email, credits, plan, updated_at) VALUES (%s, %s, %s, %s)",
                (email_addr, FREE_CREDITS, "free", now))
            conn.commit()
            return {"email": email_addr, "credits": FREE_CREDITS, "plan": "free"}

def add_vip_credits(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr): return
    get_or_create_user(email_addr)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits=LEAST(credits+120,200), plan='vip', updated_at=%s WHERE email=%s",
                (now, email_addr))
        conn.commit()
    log.info("VIP credits added for %s", email_addr)

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr): return
    get_or_create_user(email_addr)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits=credits+%s, plan=%s, updated_at=%s WHERE email=%s",
                (amount, plan, now, email_addr))
        conn.commit()

# ─── Job Helpers ─────────────────────────────────────────────────────────────
def create_job(job_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO jobs (job_id, status, ts) VALUES (%s, %s, %s)",
                (job_id, "pending", time.time()))
        conn.commit()

def update_job(job_id, **kwargs):
    if not kwargs: return
    fields = ", ".join(f"{k} = %s" for k in kwargs.keys())
    values = list(kwargs.values()) + [job_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE jobs SET {fields} WHERE job_id = %s", values)
        conn.commit()

def get_job(job_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def cleanup_old_jobs():
    cutoff = time.time() - 1800
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE ts < %s", (cutoff,))
        conn.commit()

# ─── Image & AI ───────────────────────────────────────────────────────────────
def compress_image(image_bytes, max_size=(1280, 1280), quality=88):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        log.error("compress_image error: %s", e)
        return image_bytes


# ─── Arabic Prompt ────────────────────────────────────────────────────────────
def get_dynamic_prompt_ar(subject, caption):
    combined = f"{subject} {caption}".lower()

    base = """أنت خبير محترف ومعتمد في فحص جودة المنتجات وتوثيق حالة السلع بدقة فائقة.
مهمتك هي تحليل صورة المنتج والوصف المرفق بعمق شديد، وتقديم تقرير فحص احترافي شامل ومفصل.

التعليمات الصارمة (CRITICAL INSTRUCTIONS):
1. الإخراج يجب أن يكون كائن JSON صالحاً (Valid JSON) فقط، بدون أي نصوص إضافية قبله أو بعده، وبدون علامات تنسيق مثل ` ```json `.
2. جميع النصوص والقيم داخل JSON يجب أن تكون باللغة العربية الفصحى الاحترافية والسليمة إملائياً.
3. التقييمات الرقمية يجب أن تكون واقعية ومبنية على تحليل بصري دقيق (بين 30 إلى 95).
4. اجعل قيمة "image_quality" تساوي "good" دائماً، إلا إذا كانت الصورة مشوهة تماماً أو يستحيل رؤية تفاصيلها.
5. يجب أن تحتوي قائمة "metrics" على 4 معايير على الأقل مع تقييم رقمي.
6. يجب أن تحتوي قائمة "observations" على 3 ملاحظات تفصيلية على الأقل تعكس حالة المنتج الفعلية.

هيكل الإخراج المطلوب (JSON Schema):
{
  "image_quality": "good|poor|unusable",
  "quality_note": "شرح تفصيلي لحالة الصورة إن كانت بحاجة لتحسين، وإلا اتركها فارغة",
  "overall_score": 85,
  "verdict_title": "عنوان احترافي يصف الحالة بدقة",
  "verdict_status": "success|warning|danger",
  "metrics": [
    {"name": "النظافة العامة وخلو السطح من الخدوش والعيوب", "score": 85},
    {"name": "سلامة الهيكل والمكونات الأساسية", "score": 90},
    {"name": "التطابق الدقيق مع وصف البائع والمعايير", "score": 88},
    {"name": "مستوى الاستخدام والتآكل العام", "score": 80}
  ],
  "observations": [
    {
      "type": "damage|discrepancy|note",
      "title": "عنوان الملاحظة",
      "description": "شرح وافٍ ومفصل للملاحظة مع تحليل تأثيرها على قيمة المنتج وأدائه."
    }
  ],
  "summary_for_user": "توصية نهائية احترافية ومفصلة توجه المشتري حول جدوى الشراء والمخاطر المحتملة بناءً على الفحص."
}"""

    if any(w in combined for w in ["جوال", "ايفون", "لابتوب", "شاشة", "ايباد", "phone", "electronics", "laptop", "mobile", "apple", "samsung"]):
        cat = "\n\nالتركيز الخاص (إلكترونيات): ركز بشدة على خدوش الشاشة، زوايا الجهاز، سلامة عدسات الكاميرا، الزجاج الخلفي، وأي علامات تآكل في المنافذ أو الهيكل."
    elif any(w in combined for w in ["ساعة", "ماركة", "شنطة", "نظارة", "محفظة", "watch", "bag", "luxury", "rolex", "gucci"]):
        cat = "\n\nالتركيز الخاص (سلع فاخرة): دقق في دقة الشعارات، جودة الخياطة، النقوش، تآكل الجلد أو المعدن، وأي علامات تدل على الأصالة أو التزييف."
    elif any(w in combined for w in ["سيارة", "سيارات", "قطع", "صدام", "جنط", "car", "auto", "engine", "tire"]):
        cat = "\n\nالتركيز الخاص (السيارات وقطع الغيار): ابحث عن الصدأ، الشقوق، إعادة الطلاء، عدم تطابق الألوان، والانبعاجات أو الخدوش العميقة."
    elif any(w in combined for w in ["ملابس", "ثوب", "قميص", "فستان", "حذاء", "clothes", "fashion", "shoes", "sneakers"]):
        cat = "\n\nالتركيز الخاص (الأزياء والملابس): افحص حالة القماش، وجود بقع، خيوط مفكوكة، تمزق، جودة الخياطة، والتشطيب العام."
    else:
        cat = "\n\nالتركيز الخاص (عام): قم بإجراء فحص شامل لجودة المنتج وحالته العامة وأي عيوب ظاهرة."

    user_input = f'\n\nمعلومات المنتج المراد فحصه:\n- نوع المنتج: "{subject}"'
    if caption:
        user_input += f'\n- وصف البائع: "{caption}"'
    return base + cat + user_input


# ─── English Prompt ───────────────────────────────────────────────────────────
def get_dynamic_prompt_en(subject, caption):
    combined = f"{subject} {caption}".lower()

    base = """You are a certified professional expert in product quality inspection and documenting the condition of goods with exceptional precision.
Your task is to deeply analyze the product image and the accompanying description, and provide a comprehensive and detailed professional inspection report.

CRITICAL INSTRUCTIONS:
1. Output must be a valid JSON object ONLY, with no additional text before or after it, and no formatting markers like ```json.
2. All text and values inside the JSON must be in professional, grammatically correct English.
3. Numerical ratings must be realistic and based on precise visual analysis (between 30 and 95).
4. Set "image_quality" to "good" always, unless the image is completely distorted or details are impossible to see.
5. The "metrics" list must contain at least 4 criteria with numerical ratings.
6. The "observations" list must contain at least 3 detailed observations reflecting the actual condition of the product.

Required output structure (JSON Schema):
{
  "image_quality": "good|poor|unusable",
  "quality_note": "Detailed explanation of image condition if improvement needed, otherwise leave empty",
  "overall_score": 85,
  "verdict_title": "Professional title describing the condition accurately (e.g., Excellent Condition with Minor Signs of Use)",
  "verdict_status": "success|warning|danger",
  "metrics": [
    {"name": "General cleanliness and surface free of scratches and defects", "score": 85},
    {"name": "Structural integrity and core components", "score": 90},
    {"name": "Accurate match with seller description and standards", "score": 88},
    {"name": "Overall usage and wear level", "score": 80}
  ],
  "observations": [
    {
      "type": "damage|discrepancy|note",
      "title": "Observation title (e.g., Minor scratch on upper corner)",
      "description": "Comprehensive and detailed explanation of the observation with analysis of its impact on the product value and performance."
    }
  ],
  "summary_for_user": "Final comprehensive professional recommendation guiding the buyer on purchase worthiness and potential risks based on the inspection."
}"""

    if any(w in combined for w in ["جوال", "ايفون", "لابتوب", "شاشة", "ايباد", "phone", "electronics", "laptop", "mobile", "apple", "samsung"]):
        cat = "\n\nSpecial Focus (Electronics): Focus strongly on screen scratches, device corners, camera lens integrity, back glass, and any signs of wear on ports or the frame."
    elif any(w in combined for w in ["ساعة", "ماركة", "شنطة", "نظارة", "محفظة", "watch", "bag", "luxury", "rolex", "gucci"]):
        cat = "\n\nSpecial Focus (Luxury Goods): Examine logo accuracy, stitching quality, engravings, leather or metal wear, and any signs indicating authenticity or counterfeiting."
    elif any(w in combined for w in ["سيارة", "سيارات", "قطع", "صدام", "جنط", "car", "auto", "engine", "tire"]):
        cat = "\n\nSpecial Focus (Automotive & Parts): Look for rust, cracks, repainting, color mismatches, dents, or deep scratches."
    elif any(w in combined for w in ["ملابس", "ثوب", "قميص", "فستان", "حذاء", "clothes", "fashion", "shoes", "sneakers"]):
        cat = "\n\nSpecial Focus (Fashion & Clothing): Inspect fabric condition, stains, loose threads, tears, stitching quality, and overall finish."
    else:
        cat = "\n\nSpecial Focus (General): Conduct a comprehensive inspection of the product quality, general condition, and any visible defects."

    user_input = f'\n\nProduct Information for Inspection:\n- Product Type: "{subject}"'
    if caption:
        user_input += f'\n- Seller Description: "{caption}"'
    return base + cat + user_input


# ─── Router ───────────────────────────────────────────────────────────────────
def get_dynamic_prompt(subject, caption, lang="ar"):
    if lang == "en":
        return get_dynamic_prompt_en(subject, caption)
    return get_dynamic_prompt_ar(subject, caption)


def analyze_image(images_bytes_list, caption, subject, lang="ar"):
    num    = len(images_bytes_list)
    prompt = get_dynamic_prompt(subject, caption, lang)

    if num > 1:
        if lang == "en":
            prompt += f"""

IMPORTANT — MULTI-IMAGE INSPECTION:
The user has uploaded {num} images of the SAME product from different angles.
Analyze ALL {num} images together as one comprehensive inspection.
In observations, specify which image revealed which detail using:
"Image 1:", "Image 2:", "Image 3:", "Image 4:"
Give a unified overall_score that reflects all images combined."""
        else:
            prompt += f"""

IMPORTANT — MULTI-IMAGE INSPECTION:
The user has uploaded {num} images of the SAME product from different angles.
Analyze ALL {num} images together as one comprehensive inspection.
In observations, specify which image revealed which detail using:
"الصورة الأولى:", "الصورة الثانية:", "الصورة الثالثة:", "الصورة الرابعة:"
Give a unified overall_score that reflects all images combined."""

    sizes = {1: (1280, 1280), 2: (1100, 1100), 3: (960, 960), 4: (900, 900)}
    quals = {1: 88,           2: 85,           3: 82,         4: 80        }

    content = [{"type": "text", "text": prompt}]
    for img_bytes in images_bytes_list:
        compressed = compress_image(
            img_bytes,
            max_size=sizes.get(num, (900, 900)),
            quality =quals.get(num, 80),
        )
        b64 = base64.b64encode(compressed).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    payload = {
        "model":       "google/gemini-2.5-pro",
        "temperature": 0.2,
        "messages":    [{"role": "user", "content": content}],
    }

    try:
        if not OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is missing")
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  f"https://{SITE_URL}",
                "X-Title":       "AI Product Inspector",
            },
            timeout=120,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("OpenRouter API Error: %s", str(e))
        raise

    raw   = resp.json()["choices"][0]["message"]["content"]
    clean = raw.strip()
    if "```json" in clean:
        clean = clean.split("```json")[1].split("```")[0].strip()
    elif "```" in clean:
        clean = clean.split("```")[1].split("```")[0].strip()
    s = clean.find("{")
    e = clean.rfind("}") + 1
    if s != -1 and e > s:
        clean = clean[s:e]

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("JSON parse error: %s", raw[:300])
        if lang == "en":
            return {
                "image_quality": "unusable", "quality_note": "Failed to parse the response.",
                "overall_score": 50, "verdict_title": "Incomplete Analysis",
                "verdict_status": "warning", "metrics": [], "observations": [],
                "summary_for_user": "An error occurred during processing. Please try again.",
            }
        return {
            "image_quality": "unusable", "quality_note": "تعذر تحليل الاستجابة.",
            "overall_score": 50, "verdict_title": "تحليل غير مكتمل",
            "verdict_status": "warning", "metrics": [], "observations": [],
            "summary_for_user": "حدث خطأ أثناء المعالجة. يُرجى إعادة المحاولة.",
        }


# ─── Report HTML ──────────────────────────────────────────────────────────────
def format_report_html(result, logo_b64=None, lang="ar"):
    is_en  = (lang == "en")
    status = result.get("verdict_status", "warning")
    color_map = {
        "success": {"badge_bg": "rgba(34,197,94,0.15)",  "border": "#22c55e", "text": "#4ade80"},
        "warning": {"badge_bg": "rgba(234,179,8,0.15)",  "border": "#eab308", "text": "#fde047"},
        "danger":  {"badge_bg": "rgba(239,68,68,0.15)",  "border": "#ef4444", "text": "#fca5a5"},
    }
    theme = color_map.get(status, color_map["warning"])

    # Labels
    lbl_report     = "Professional AI Inspection Report" if is_en else "تقرير الفحص الذكي الاحترافي"
    lbl_verdict    = "Inspection Result"                 if is_en else "نتيجة الفحص"
    lbl_rating     = "Overall Rating"                    if is_en else "التقييم العام"
    lbl_metrics    = "📊 Detailed Quality Indicators:"   if is_en else "📊 مؤشرات الجودة التفصيلية:"
    lbl_obs        = "🔍 Analysis & Observed Notes:"     if is_en else "🔍 التحليل والملاحظات المرصودة:"
    lbl_rec        = "💡 Final Comprehensive Recommendation:" if is_en else "💡 التوصية النهائية الشاملة:"
    lbl_no_obs     = "No visible defects or negative observations were recorded." if is_en else "لم يتم تسجيل أي عيوب أو ملاحظات سلبية ظاهرة."
    lbl_disclaimer = "Automated advisory analysis — The final decision is always yours." if is_en else "تحليل استرشادي آلي — القرار النهائي يعود إليك."
    lbl_criterion  = "Inspection Criterion" if is_en else "معيار الفحص"
    lbl_note       = "Note"                 if is_en else "ملاحظة"
    lbl_failed     = "Precise inspection failed:" if is_en else "تعذر الفحص الدقيق:"

    dir_attr   = 'ltr' if is_en else 'rtl'
    text_align = 'left' if is_en else 'right'
    border_obs = 'border-left' if is_en else 'border-right'

    quality_warning = ""
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "Image is not clear enough." if is_en else "الصورة المرفوقة غير واضحة بشكل كافٍ.")
        if not result.get("observations") and not result.get("metrics"):
            return (f'<div dir="{dir_attr}" style="background:#0f172a;border:1px solid #334155;border-radius:12px;'
                    f'padding:20px;color:#f8fafc;font-family:system-ui,sans-serif;text-align:{text_align};">'
                    f'<div style="background:rgba(239,68,68,0.15);border:1px solid #ef4444;border-radius:8px;'
                    f'padding:15px;color:#fca5a5;font-weight:600;text-align:center;">⚠️ {lbl_failed} {note}</div></div>')
        quality_warning = (f'<div style="background:rgba(234,179,8,0.15);border:1px solid #eab308;'
                           f'border-radius:8px;padding:10px 14px;color:#fde047;font-size:12px;margin-bottom:16px;">⚠️ {note}</div>')

    metrics_html = ""
    for m in result.get("metrics", []):
        score = min(max(int(m.get("score", 50)), 0), 100)
        metrics_html += (
            f'<div style="margin-bottom:12px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:4px;">'
            f'<span>{m.get("name", lbl_criterion)}</span>'
            f'<span style="font-weight:bold;color:#f8fafc;">{score}%</span></div>'
            f'<div style="background:#334155;height:6px;border-radius:3px;overflow:hidden;">'
            f'<div style="background:{theme["border"]};width:{score}%;height:100%;border-radius:3px;"></div>'
            f'</div></div>'
        )

    icons = {"damage": ("❌", "#ef4444"), "discrepancy": ("⚠️", "#eab308"), "note": ("💡", "#3b82f6")}
    obs_html = ""
    for o in result.get("observations", []):
        icon, oc = icons.get(o.get("type", "note"), ("📌", "#94a3b8"))
        obs_html += (
            f'<div style="background:#1e293b;{border_obs}:3px solid {oc};padding:10px 12px;'
            f'border-radius:4px 8px 8px 4px;margin-bottom:8px;">'
            f'<div style="font-size:14px;font-weight:bold;color:#f8fafc;margin-bottom:2px;">{icon} {o.get("title", lbl_note)}</div>'
            f'<div style="font-size:13px;color:#94a3b8;line-height:1.4;">{o.get("description","")}</div></div>'
        )

    if not obs_html:
        obs_html = f'<div style="font-size:13px;color:#94a3b8;text-align:center;">{lbl_no_obs}</div>'

    score_val = min(max(int(result.get("overall_score", 70)), 0), 100)

    logo_html = ""
    if logo_b64:
        logo_html = (
            f'<div style="text-align:center;margin-bottom:16px;padding:12px;background:#1e293b;'
            f'border-radius:10px;border:1px solid #334155;">'
            f'<img src="data:image/png;base64,{logo_b64}" '
            f'style="max-height:56px;max-width:180px;object-fit:contain;" alt="Store Logo"></div>'
        )

    return (
        f'<div id="report-content" dir="{dir_attr}" style="background:#0f172a;border:1px solid #1e293b;border-radius:16px;'
        f'padding:24px;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;max-width:650px;'
        f'margin:auto;text-align:{text_align};box-shadow:0 10px 25px rgba(0,0,0,0.3);">'
        f'{logo_html}{quality_warning}'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'border-bottom:1px solid #1e293b;padding-bottom:18px;margin-bottom:20px;">'
        f'<div><span style="font-size:12px;font-weight:600;color:#94a3b8;">{lbl_report}</span>'
        f'<h3 style="margin:4px 0 0 0;font-size:18px;color:{theme["text"]};font-weight:bold;">{result.get("verdict_title", lbl_verdict)}</h3></div>'
        f'<div style="background:{theme["badge_bg"]};border:1px solid {theme["border"]};border-radius:12px;padding:8px 16px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:bold;color:{theme["text"]};line-height:1;">{score_val}'
        f'<span style="font-size:13px;color:#94a3b8;">/100</span></div>'
        f'<div style="font-size:10px;color:#94a3b8;margin-top:2px;">{lbl_rating}</div></div></div>'
        f'<div style="margin-bottom:20px;background:#182234;padding:14px;border-radius:12px;border:1px solid #1e293b;">'
        f'<div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:12px;">{lbl_metrics}</div>'
        f'{metrics_html}</div>'
        f'<div style="margin-bottom:20px;">'
        f'<div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:10px;">{lbl_obs}</div>'
        f'{obs_html}</div>'
        f'<div style="background:{theme["badge_bg"]};border:1px dashed {theme["border"]};border-radius:12px;padding:14px;margin-top:16px;">'
        f'<div style="font-size:13px;font-weight:bold;color:{theme["text"]};margin-bottom:4px;">{lbl_rec}</div>'
        f'<div style="font-size:13px;color:#e2e8f0;line-height:1.5;">{result.get("summary_for_user","")}</div></div>'
        f'<div style="font-size:10px;color:#64748b;text-align:center;margin-top:16px;border-top:1px solid #1e293b;padding-top:10px;">'
        f'{lbl_disclaimer}</div></div>'
    )


# ─── Background Job ───────────────────────────────────────────────────────────
def _run_analysis_job(job_id, email_addr, images_bytes_list, description, cost, logo_bytes, lang="ar"):
    with app.app_context():
        try:
            result = analyze_image(images_bytes_list, description, description, lang=lang)

            custom_logo_b64 = None
            if logo_bytes:
                try:
                    limg = Image.open(io.BytesIO(logo_bytes))
                    if limg.mode == "P":
                        limg = limg.convert("RGBA")
                    limg.thumbnail((240, 90), Image.Resampling.LANCZOS)
                    out = io.BytesIO()
                    limg.save(out, format="PNG")
                    custom_logo_b64 = base64.b64encode(out.getvalue()).decode()
                except Exception as le:
                    log.error("Logo job error: %s", le)

            report = format_report_html(result, logo_b64=custom_logo_b64, lang=lang)

            if not is_exempt(email_addr):
                now = datetime.now(timezone.utc).isoformat()
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE users SET credits = credits - %s, updated_at = %s WHERE email = %s",
                            (cost, now, email_addr)
                        )
                    conn.commit()

            user = get_or_create_user(email_addr)
            update_job(job_id,
                status="done",
                report=report,
                credits=user["credits"],
                cost=cost,
                plan=user["plan"],
                is_exempt=is_exempt(email_addr),
            )
            log.info("Job %s done for %s (lang=%s)", job_id, email_addr, lang)

        except Exception as e:
            log.exception("Job %s failed", job_id)
            update_job(job_id, status="error", error=str(e))


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/credits", methods=["GET"])
def credits_check():
    email_addr = request.args.get("email", "").strip().lower()
    if not email_addr:
        return jsonify({"error": "email required"}), 400
    if is_temp_email(email_addr):
        return jsonify({"error": "Temporary emails are not allowed."}), 400
    user = get_or_create_user(email_addr)
    return jsonify({
        "credits":     user["credits"],
        "plan":        user["plan"],
        "photo_limit": PHOTO_LIMIT_MAP.get(user["plan"], 1),
        "is_exempt":   is_exempt(email_addr),
    })


@app.route("/upload", methods=["POST"])
def direct_upload():
    email_addr        = request.form.get("email", "").strip().lower()
    description       = request.form.get("description", "")
    secret_code_input = request.form.get("secret_code", "").strip()
    lang              = request.form.get("lang", "ar").strip().lower()  # ← اللغة
    if lang not in ("ar", "en"):
        lang = "ar"
    image_files = request.files.getlist("images") or request.files.getlist("image")

    if not email_addr:
        return jsonify({"error": "Email is required"}), 400

    if is_temp_email(email_addr):
        return jsonify({"error": "Temporary emails are not allowed. Please use your real email."}), 400

    if not image_files or all(f.filename == "" for f in image_files):
        return jsonify({"error": "No image uploaded"}), 400

    if is_exempt(email_addr):
        if not secret_code_input:
            return jsonify({"error": "secret_required", "message": "Enter the secret code"}), 401
        if not ADMIN_SECRET_CODE or secret_code_input != ADMIN_SECRET_CODE:
            log.warning("Bad secret for %s", email_addr)
            return jsonify({"error": "invalid_secret", "message": "Invalid secret code"}), 403

    valid_files = [f for f in image_files if f.filename != ""]
    num_images  = len(valid_files)

    user      = get_or_create_user(email_addr)
    photo_lim = PHOTO_LIMIT_MAP.get(user["plan"], 1)

    if num_images > photo_lim:
        return jsonify({"error": f"Your plan allows a maximum of {photo_lim} photo(s)"}), 400

    cost = max(1, math.ceil(num_images / 2))

    if not is_exempt(email_addr) and user["credits"] < cost:
        return jsonify({"error": "Insufficient credits", "credits": user["credits"]}), 402

    images_bytes = [f.read() for f in valid_files]
    logo_bytes   = None
    lf = request.files.get("custom_logo")
    if lf and lf.filename:
        logo_bytes = lf.read()

    job_id = str(uuid.uuid4())
    cleanup_old_jobs()
    create_job(job_id)

    threading.Thread(
        target=_run_analysis_job,
        args=(job_id, email_addr, images_bytes, description, cost, logo_bytes, lang),
        daemon=True,
    ).start()

    est = {1: 40, 2: 55, 3: 75, 4: 105}.get(num_images, 60)
    return jsonify({
        "status":            "processing",
        "job_id":            job_id,
        "estimated_seconds": est,
        "num_images":        num_images,
    })


@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    cleanup_old_jobs()
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "expired"}), 200
    return jsonify(dict(job)), 200


@app.route("/lemonsqueezy/webhook", methods=["POST"])
def lemonsqueezy_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Signature", "")
    if LEMONSQUEEZY_SECRET:
        expected = hmac.new(LEMONSQUEEZY_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error": "invalid signature"}), 401

    try:
        payload        = request.get_json(force=True) or {}
        event_name     = payload.get("meta", {}).get("event_name", "")
        customer_email = payload.get("data", {}).get("attributes", {}).get("user_email", "").strip().lower()

        if not customer_email:
            return jsonify({"error": "no email"}), 400

        if event_name == "order_created":
            variant_id = None
            for item in payload.get("included", []):
                if item.get("type") == "order-items":
                    variant_id = str(item.get("attributes", {}).get("variant_id", "")).strip()
                    break
            plan_info = PLAN_CREDITS.get(variant_id)
            if not plan_info:
                return jsonify({"status": "unknown_plan"}), 200
            plan_name, credits = plan_info
            add_credits(customer_email, plan_name, credits)
            log.info("Granted %d credits (%s) to %s", credits, plan_name, customer_email)
            return jsonify({"status": "success", "plan": plan_name, "credits": credits}), 200

        elif event_name in ("subscription_created", "subscription_payment_success"):
            add_vip_credits(customer_email)
            log.info("VIP updated for %s", customer_email)
            return jsonify({"status": "vip_updated"}), 200

        elif event_name in ("subscription_cancelled", "subscription_expired", "subscription_payment_failed"):
            log.info("VIP ended for %s", customer_email)
            return jsonify({"status": "cancelled"}), 200

        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        log.exception("Webhook error")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
