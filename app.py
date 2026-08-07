import os
import json
import base64
import logging
import sqlite3
import hashlib
import hmac
import io
import math
import threading
import uuid
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, g
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB

_jobs: dict = {}
_jobs_lock  = threading.Lock()
_JOB_TTL    = 1800  # 30 دقيقة
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector")

@app.route('/')
def home():
    return render_template('index.html')

OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")
ADMIN_SECRET_CODE   = os.environ.get("ADMIN_SECRET_CODE", "").strip()
SITE_URL            = "editchecker.com"
DB_PATH             = os.environ.get("DB_PATH", "/tmp/inspector.db")
FREE_CREDITS        = 3

EXEMPT_CREDITS = 999
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

# ─── Database ───────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("""CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, credits INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free', updated_at TEXT)""")
        g.db.commit()
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def is_exempt(email_addr):
    return email_addr.strip().lower() in EXEMPT_EMAILS

def get_or_create_user(email_addr):
    db = get_db()
    email_addr = email_addr.strip().lower()
    row = db.execute("SELECT * FROM users WHERE email=?", (email_addr,)).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if is_exempt(email_addr):
        db.execute("""INSERT INTO users (email,credits,plan,updated_at) VALUES (?,?,?,?)
                      ON CONFLICT(email) DO UPDATE SET credits=excluded.credits,
                      plan=excluded.plan, updated_at=excluded.updated_at""",
                   (email_addr, EXEMPT_CREDITS, "vip", now))
        db.commit()
        return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "vip"}
    if row:
        return dict(row)
    db.execute("INSERT INTO users (email,credits,plan,updated_at) VALUES (?,?,?,?)",
               (email_addr, FREE_CREDITS, "free", now))
    db.commit()
    return {"email": email_addr, "credits": FREE_CREDITS, "plan": "free"}

def add_vip_credits(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        return
    get_or_create_user(email_addr)
    db = get_db()
    db.execute("UPDATE users SET credits=MIN(credits+120,200), plan='vip', updated_at=? WHERE email=?",
               (datetime.now(timezone.utc).isoformat(), email_addr))
    db.commit()
    log.info("VIP credits added for %s (cap 200)", email_addr)

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        return
    get_or_create_user(email_addr)
    db = get_db()
    db.execute("UPDATE users SET credits=credits+?, plan=?, updated_at=? WHERE email=?",
               (amount, plan, datetime.now(timezone.utc).isoformat(), email_addr))
    db.commit()

# ─── Image & AI ─────────────────────────────────────────────────────────────
# ─── Image & AI ─────────────────────────────────────────────────────────────
def compress_image(image_bytes, num_images=1):
    try:
        # تحديد الأبعاد والجودة بناءً على عدد الصور لكشف البقع والعيوب الدقيقة
        if num_images <= 2:
            max_size = (1280, 1280)  # دقة عالية جداً للصور القليلة تكشف أصفر البقع
            quality = 88
        else:
            max_size = (1024, 1024)  # دقة ممتازة لـ 3 صور أو أكثر تمنع الـ Timeout
            quality = 82

        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        # تصغير الصورة مع الحفاظ على النسب وأعلى حدة تفاصيل
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        log.error("compress_image error: %s", e)
        return image_bytes


def get_dynamic_prompt(subject, caption):
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
  "verdict_title": "عنوان احترافي يصف الحالة بدقة (مثال: حالة ممتازة مع آثار استخدام طفيفة)",
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
      "title": "عنوان الملاحظة (مثال: خدش دقيق في الزاوية العلوية)",
      "description": "شرح وافٍ ومفصل للملاحظة مع تحليل تأثيرها على قيمة المنتج وأدائه."
    }
  ],
  "summary_for_user": "توصية نهائية احترافية ومفصلة توجه المشتري حول جدوى الشراء والمخاطر المحتملة بناءً على الفحص."
}"""

    # فئات الفحص (مترجمة وموسعة)
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

def analyze_image(images_bytes_list, caption, subject):
    num    = len(images_bytes_list)
    prompt = get_dynamic_prompt(subject, caption)

    if num > 1:
        prompt += f"""

IMPORTANT — MULTI-IMAGE INSPECTION:
The user has uploaded {num} images of the SAME product from different angles.
Analyze ALL {num} images together as one comprehensive inspection.
In observations, specify which image revealed which detail using:
"الصورة الأولى:", "الصورة الثانية:", "الصورة الثالثة:", "الصورة الرابعة:"
Give a unified overall_score that reflects all images combined."""

        # ضغط أقوى لـ 3+ صور لتجنب timeout
    _sizes    = {1:(800,800), 2:(720,720), 3:(600,600), 4:(480,480)}
    _quals    = {1:85,        2:80,        3:72,        4:60}
    c_size    = _sizes.get(num, (480,480))
    c_quality = _quals.get(num, 60)

    content = [{"type": "text", "text": prompt}]
# حساب إجمالي عدد الصور لتمريره لدالة الضغط
total_images = len(images_bytes_list)

for img_bytes in images_bytes_list:
    # تمرير عدد الصور فقط ليتم تحديد الجودة والحجم تلقائياً من داخل الدالة
    compressed = compress_image(img_bytes, num_images=total_images)
    b64 = base64.b64encode(compressed).decode()
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    payload = {
        "model": "google/gemini-2.5-pro",
        "temperature": 0.2,
        "messages": [{"role": "user", "content": content}],
    }

    try:
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
        # مسافة 8 ضغطات Space هنا
        return json.loads(clean)
    except json.JSONDecodeError:
        # مسافة 8 ضغطات Space هنا
        log.error("JSON parse error: %s", raw[:300])
        return {
            "image_quality": "unusable", "quality_note": "تعذر تحليل الاستجابة.",
            "overall_score": 50, "verdict_title": "تحليل غير مكتمل",
            "verdict_status": "warning", "metrics": [], "observations": [],
            "summary_for_user": "حدث خطأ أثناء المعالجة. يُرجى إعادة المحاولة.",
        }


# ─── Report HTML ─────────────────────────────────────────────────────────────
def format_report_html(result, logo_b64=None):
    status    = result.get("verdict_status", "warning")
    color_map = {
        "success": {"badge_bg": "rgba(34,197,94,0.15)",  "border": "#22c55e", "text": "#4ade80"},
        "warning": {"badge_bg": "rgba(234,179,8,0.15)",  "border": "#eab308", "text": "#fde047"},
        "danger":  {"badge_bg": "rgba(239,68,68,0.15)",  "border": "#ef4444", "text": "#fca5a5"},
    }
    theme = color_map.get(status, color_map["warning"])

    quality_warning = ""
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة المرفوقة غير واضحة بشكل كافٍ.")
        if not result.get("observations") and not result.get("metrics"):
            return (f'<div dir="rtl" style="background:#0f172a;border:1px solid #334155;border-radius:12px;'
                    f'padding:20px;color:#f8fafc;font-family:system-ui,sans-serif;text-align:right;">'
                    f'<div style="background:rgba(239,68,68,0.15);border:1px solid #ef4444;border-radius:8px;'
                    f'padding:15px;color:#fca5a5;font-weight:600;text-align:center;">⚠️ تعذر الفحص الدقيق: {note}</div></div>')
        quality_warning = (f'<div style="background:rgba(234,179,8,0.15);border:1px solid #eab308;'
                           f'border-radius:8px;padding:10px 14px;color:#fde047;font-size:12px;margin-bottom:16px;">⚠️ {note}</div>')

    metrics_html = ""
    for m in result.get("metrics", []):
        score = min(max(int(m.get("score", 50)), 0), 100)
        metrics_html += (
            f'<div style="margin-bottom:12px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:4px;">'
            f'<span>{m.get("name","معيار الفحص")}</span>'
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
            f'<div style="background:#1e293b;border-right:3px solid {oc};padding:10px 12px;'
            f'border-radius:4px 8px 8px 4px;margin-bottom:8px;">'
            f'<div style="font-size:14px;font-weight:bold;color:#f8fafc;margin-bottom:2px;">{icon} {o.get("title","ملاحظة")}</div>'
            f'<div style="font-size:13px;color:#94a3b8;line-height:1.4;">{o.get("description","")}</div></div>'
        )

    if not obs_html:
        obs_html = '<div style="font-size:13px;color:#94a3b8;text-align:center;">لم يتم تسجيل أي عيوب أو ملاحظات سلبية ظاهرة.</div>'

    score_val = min(max(int(result.get("overall_score", 70)), 0), 100)

    logo_html = ""
    if logo_b64:
        logo_html = (
            f'<div style="text-align:center;margin-bottom:16px;padding:12px;background:#1e293b;'
            f'border-radius:10px;border:1px solid #334155;">'
            f'<img src="data:image/png;base64,{logo_b64}" '
            f'style="max-height:56px;max-width:180px;object-fit:contain;" alt="شعار المتجر"></div>'
        )

    return (
        f'<div id="report-content" dir="rtl" style="background:#0f172a;border:1px solid #1e293b;border-radius:16px;'
        f'padding:24px;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;max-width:650px;'
        f'margin:auto;text-align:right;box-shadow:0 10px 25px rgba(0,0,0,0.3);">'
        f'{logo_html}{quality_warning}'
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'border-bottom:1px solid #1e293b;padding-bottom:18px;margin-bottom:20px;">'
        f'<div><span style="font-size:12px;font-weight:600;color:#94a3b8;">تقرير الفحص الذكي الاحترافي</span>'
        f'<h3 style="margin:4px 0 0 0;font-size:18px;color:{theme["text"]};font-weight:bold;">{result.get("verdict_title","نتيجة الفحص")}</h3></div>'
        f'<div style="background:{theme["badge_bg"]};border:1px solid {theme["border"]};border-radius:12px;padding:8px 16px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:bold;color:{theme["text"]};line-height:1;">{score_val}'
        f'<span style="font-size:13px;color:#94a3b8;">/100</span></div>'
        f'<div style="font-size:10px;color:#94a3b8;margin-top:2px;">التقييم العام</div></div></div>'
        f'<div style="margin-bottom:20px;background:#182234;padding:14px;border-radius:12px;border:1px solid #1e293b;">'
        f'<div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:12px;">📊 مؤشرات الجودة التفصيلية:</div>'
        f'{metrics_html}</div>'
        f'<div style="margin-bottom:20px;">'
        f'<div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:10px;">🔍 التحليل والملاحظات المرصودة:</div>'
        f'{obs_html}</div>'
        f'<div style="background:{theme["badge_bg"]};border:1px dashed {theme["border"]};border-radius:12px;padding:14px;margin-top:16px;">'
        f'<div style="font-size:13px;font-weight:bold;color:{theme["text"]};margin-bottom:4px;">💡 التوصية النهائية الشاملة:</div>'
        f'<div style="font-size:13px;color:#e2e8f0;line-height:1.5;">{result.get("summary_for_user","")}</div></div>'
        f'<div style="font-size:10px;color:#64748b;text-align:center;margin-top:16px;border-top:1px solid #1e293b;padding-top:10px;">'
        f'تحليل استرشادي آلي — القرار النهائي يعود إليك.</div></div>'
    )
    
    # ─── Background Job System ───────────────────────────────────────────────────

def _cleanup_old_jobs():
    now = time.time()
    with _jobs_lock:
        expired = [k for k, v in _jobs.items() if now - v.get("ts", now) > _JOB_TTL]
        for k in expired:
            del _jobs[k]


def _run_analysis_job(job_id, email_addr, images_bytes_list, description, cost, logo_bytes):
    with app.app_context():
        try:
            result = analyze_image(images_bytes_list, description, description)

            # معالجة الشعار
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

            report = format_report_html(result, logo_b64=custom_logo_b64)

            # --- الخصم يحدث هنا حصراً بعد نجاح التحليل التام ---
            if not is_exempt(email_addr):
                db = get_db()
                db.execute("UPDATE users SET credits=credits-?, updated_at=? WHERE email=?",
                           (cost, datetime.now(timezone.utc).isoformat(), email_addr))
                db.commit()

            user = get_or_create_user(email_addr)
            with _jobs_lock:
                _jobs[job_id].update({
                    "status":    "done",
                    "report":    report,
                    "credits":   user["credits"],
                    "cost":      cost,
                    "plan":      user["plan"],
                    "is_exempt": is_exempt(email_addr),
                })
            log.info("Job %s done for %s", job_id, email_addr)

        except Exception as e:
            log.exception("Job %s failed", job_id)
            # في حال حدوث أي خطأ في التحليل، فلن يتم خصم أي رصيد نهائياً
            with _jobs_lock:
                _jobs[job_id].update({"status": "error", "error": str(e)})



# ─── Routes ─────────────────────────────────────────────────────────────────
@app.route("/credits", methods=["GET"])
def credits_check():
    email_addr = request.args.get("email", "").strip().lower()
    if not email_addr:
        return jsonify({"error": "email required"}), 400
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
    image_files       = request.files.getlist("images") or request.files.getlist("image")

    if not email_addr:
        return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400
    if not image_files or all(f.filename == "" for f in image_files):
        return jsonify({"error": "لم يتم رفع أي صورة"}), 400

    if is_exempt(email_addr):
        if not secret_code_input:
            return jsonify({"error": "secret_required", "message": "أدخل الرمز السري"}), 401
        if not ADMIN_SECRET_CODE or secret_code_input != ADMIN_SECRET_CODE:
            log.warning("Bad secret for %s", email_addr)
            return jsonify({"error": "invalid_secret", "message": "الرمز السري غير صحيح"}), 403

    valid_files = [f for f in image_files if f.filename != ""]
    num_images  = len(valid_files)

    user      = get_or_create_user(email_addr)
    photo_lim = PHOTO_LIMIT_MAP.get(user["plan"], 1)
    
    # التأكد من عدم تجاوز حد الصور المسموح به
    if num_images > photo_lim:
        return jsonify({"error": f"باقتك تسمح بـ {photo_lim} صور كحد أقصى"}), 400

    cost = max(1, math.ceil(num_images / 2))
    
    # التأكد من وجود رصيد كافٍ قبل البدء
    if not is_exempt(email_addr) and user["credits"] < cost:
        return jsonify({"error": "نفد رصيدك", "credits": user["credits"]}), 402

    images_bytes = [f.read() for f in valid_files]
    logo_bytes   = None
    lf = request.files.get("custom_logo")
    if lf and lf.filename:
        logo_bytes = lf.read()

    # إنشاء معرف العملية لتتواصل معه الجافاسكريبت
    job_id = str(uuid.uuid4())
    _cleanup_old_jobs()
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "ts": time.time()}

    # تشغيل التحليل في الخلفية
    threading.Thread(
        target=_run_analysis_job,
        args=(job_id, email_addr, images_bytes, description, cost, logo_bytes),
        daemon=True,
    ).start()

    est = {1: 40, 2: 55, 3: 75, 4: 105}.get(num_images, 60)
    
    # إعادة job_id لمنع خطأ undefined
    return jsonify({
        "status":            "processing",
        "job_id":            job_id,
        "estimated_seconds": est,
        "num_images":        num_images,
    })



@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    _cleanup_old_jobs()
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return jsonify({"status": "expired"}), 200
    return jsonify(job), 200


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
