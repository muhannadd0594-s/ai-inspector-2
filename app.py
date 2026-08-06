import os
import json
import base64
import logging
import sqlite3
import hashlib
import hmac
import io
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, g
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector")

# === المسار الرئيسي لعرض واجهة المستخدم ===
@app.route('/')
def home():
    return render_template('index.html')

# ─── Configuration & Credentials ───────────────────────────────────────────
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")
ADMIN_SECRET_CODE   = os.environ.get("ADMIN_SECRET_CODE", "").strip()
SITE_URL            = "editchecker.com"
DB_PATH             = os.environ.get("DB_PATH", "/tmp/inspector.db")
FREE_CREDITS        = 3

# ─── Exempt emails ─────────────────────────────────────────────────────────
EXEMPT_CREDITS = 999
EXEMPT_EMAILS = {
    "akashiiso04@gmail.com",
    "muhannadd0594@gmail.com",
    "mohammdlghmd@gmail.com",
}

# ─── LemonSqueezy Variant IDs ───────────────────────────────────────────────
PLAN_CREDITS = {
    "e810b85b-5273-4da2-9477-f3cf62f9737d": ("basic", 10),
    "db680fa5-9ec4-4fed-81fe-0ad4928266c3": ("pro",   50),
    "ceff30c8-9ba9-4c2a-bfb8-0cd520a9c072": ("vip",  120),
}

# ─── Database ───────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("""CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            credits INTEGER DEFAULT 0,
            plan TEXT DEFAULT 'free',
            updated_at TEXT
        )""")
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
    now_iso = datetime.now(timezone.utc).isoformat()

    if is_exempt(email_addr):
        if row is None or row["credits"] != EXEMPT_CREDITS or row["plan"] != "vip":
            db.execute("""INSERT INTO users (email, credits, plan, updated_at) VALUES (?, ?, ?, ?)
                          ON CONFLICT(email) DO UPDATE SET credits=excluded.credits,
                          plan=excluded.plan, updated_at=excluded.updated_at""",
                       (email_addr, EXEMPT_CREDITS, "vip", now_iso))
            db.commit()
        return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "vip"}

    if row:
        return dict(row)

    db.execute("INSERT INTO users (email, credits, plan, updated_at) VALUES (?, ?, ?, ?)",
               (email_addr, FREE_CREDITS, "free", now_iso))
    db.commit()
    return {"email": email_addr, "credits": FREE_CREDITS, "plan": "free"}

def deduct_credit(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        get_or_create_user(email_addr)
        return True
    db = get_db()
    user = get_or_create_user(email_addr)
    if user["credits"] <= 0:
        return False
    db.execute("UPDATE users SET credits=credits-1, updated_at=? WHERE email=?",
               (datetime.now(timezone.utc).isoformat(), email_addr))
    db.commit()
    return True

VIP_MAX_CREDITS = 200

def add_vip_credits(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        return
    get_or_create_user(email_addr)
    db = get_db()
    db.execute("""UPDATE users SET credits=MIN(credits+120, 200), plan='vip', updated_at=?
                  WHERE email=?""",
               (datetime.now(timezone.utc).isoformat(), email_addr))
    db.commit()
    log.info("VIP credits added for %s (cap 200)", email_addr)

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    db = get_db()
    get_or_create_user(email_addr)
    if is_exempt(email_addr):
        return
    db.execute("UPDATE users SET credits=credits+?, plan=?, updated_at=? WHERE email=?",
               (amount, plan, datetime.now(timezone.utc).isoformat(), email_addr))
    db.commit()

# ─── Image helpers ──────────────────────────────────────────────────────────
def compress_image(image_bytes, max_size=(800, 800)):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        log.error("compress_image error: %s", e)
        return image_bytes

# ─── Prompt builder ─────────────────────────────────────────────────────────
def get_dynamic_prompt(subject, caption):
    combined = f"{subject} {caption}".lower()
    base = """أنت خبير محترف ومعتمد في فحص جودة المنتجات وتوثيق حالة السلع بدقة فائقة.
قم بتحليل صورة المنتج والوصف بعمق شديد، وقدم تقرير فحص هندسي/تجاري احترافي، شامل، ومفصل تماماً.
يجب أن ترجع إجابتك ككائن JSON خام فقط (بدون markdown أو ```json):
{
  "image_quality": "good|poor|unusable",
  "quality_note": "شرح تفصيلي لحالة الصورة إن كانت بحاجة لتحسين، وإلا تركها فارغة",
  "overall_score": 85,
  "verdict_title": "عنوان احترافي يصف الحالة بدقة",
  "verdict_status": "success|warning|danger",
  "metrics": [
    {"name": "النظافة العامة وخلو السطح من الخدوش والعيوب", "score": 85},
    {"name": "سلامة الهيكل والمكونات الأساسية", "score": 90},
    {"name": "التطابق الدقيق مع وصف البائع والمعايير", "score": 88}
  ],
  "observations": [
    {"type": "damage|discrepancy|note", "title": "عنوان الملاحظة التفصيلي", "description": "شرح وافٍ ومفصل للملاحظة مع تحليل تأثيرها على قيمة المنتج."}
  ],
  "summary_for_user": "توصية نهائية احترافية، تحليلية ومفصلة توجه المشتري بوضوح تام حول جدوى الشراء، المخاطر المحتملة، والقيمة مقابل السعر."
}
CRITICAL:
1. جميع النصوص داخل JSON تكون باللغة العربية الفصحى الاحترافية حصراً.
2. التقييمات تكون واقعية، دقيقة، ومبنية على تحليل بصري عميق (بين 30% إلى 95%).
3. تقديم تفاصيل غنية وملاحظات تحليلية دقيقة في كل خانة.
4. ضع image_quality="good" دائماً إلا إذا كانت الصورة سوداء أو ضبابية تماماً بحيث يستحيل رؤية أي شيء. أي صورة تظهر المنتج يجب أن تكون "good".
5. يجب أن تحتوي metrics على 4 معايير على الأقل مع تقييم رقمي دقيق لكل منها.
6. يجب أن تحتوي observations على 3 ملاحظات على الأقل (إيجابية ك note أو سلبية)."""

    if any(w in combined for w in ["جوال","ايفون","لابتوب","شاشة","ايباد","phone","electronics"]):
        cat = "\n\nFocus (Electronics): screen scratches, damaged corners, camera module condition, back glass integrity, and hardware wear."
    elif any(w in combined for w in ["ساعة","ماركة","شنطة","نظارة","محفظة","watch","bag","luxury"]):
        cat = "\n\nFocus (Luxury): logo accuracy, stitching precision, material engravings, leather/metal wear, and authenticity indicators."
    elif any(w in combined for w in ["سيارة","سيارات","قطع","صدام","جنط","car","auto"]):
        cat = "\n\nFocus (Auto): rust patterns, structural cracks, paint resprays, color mismatches, and physical dents."
    elif any(w in combined for w in ["ملابس","ثوب","قميص","فستان","حذاء","clothes","fashion"]):
        cat = "\n\nFocus (Fashion): fabric texture/condition, stains, loose threads, tears, and overall finishing quality."
    else:
        cat = "\n\nFocus (General): comprehensive and exhaustive quality inspection."

    user = f'\n\nSeller caption:\n"{caption}"' if caption else ""
    return base + cat + user

# ─── AI analysis — THE FIX: always uses get_dynamic_prompt ─────────────────
def analyze_image(images_bytes_list, caption, subject):
    num = len(images_bytes_list)
    prompt = get_dynamic_prompt(subject, caption)

    # إضافة تعليمات للـ AI عند وجود صور متعددة
    if num > 1:
        prompt += f"""

IMPORTANT — MULTI-IMAGE INSPECTION:
The user has uploaded {num} images of the SAME product from different angles.
Your job is to analyze ALL {num} images together as one comprehensive inspection.
In your observations, clearly specify which image revealed which defect.
Use labels like: "الصورة الأولى:", "الصورة الثانية:", "الصورة الثالثة:", "الصورة الرابعة:"
Give a unified overall_score that reflects all images combined."""

    content = [{"type": "text", "text": prompt}]
    for img_bytes in images_bytes_list:
        compressed = compress_image(img_bytes)
        b64 = base64.b64encode(compressed).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    payload = {
        "model": "google/gemini-2.5-pro",
        "temperature": 0.2,
        "messages": [{"role": "user", "content": content}]
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": f"https://{SITE_URL}",
                "X-Title": "AI Product Inspector"
            },
            timeout=45
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
    start = clean.find("{")
    end   = clean.rfind("}") + 1
    if start != -1 and end > start:
        clean = clean[start:end]
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("JSON parse error: %s", raw)
        return {
            "image_quality": "unusable",
            "quality_note": "تعذر تحليل الاستجابة بشكل صحيح.",
            "overall_score": 50,
            "verdict_title": "تحليل غير مكتمل",
            "verdict_status": "warning",
            "metrics": [],
            "observations": [],
            "summary_for_user": "حدث خطأ أثناء معالجة بيانات الفحص. يُرجى إعادة المحاولة."
        }

# ─── Report HTML ────────────────────────────────────────────────────────────
def format_report_html(result, logo_b64=None):
    status = result.get("verdict_status", "warning")
    color_map = {
        "success": {"badge_bg": "rgba(34,197,94,0.15)",  "border": "#22c55e", "text": "#4ade80"},
        "warning": {"badge_bg": "rgba(234,179,8,0.15)",  "border": "#eab308", "text": "#fde047"},
        "danger":  {"badge_bg": "rgba(239,68,68,0.15)",  "border": "#ef4444", "text": "#fca5a5"},
    }
    theme = color_map.get(status, color_map["warning"])

    quality_warning = ""
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة المرفوقة غير واضحة بشكل كافٍ.")
        # إضافة كلاود: لا يتم حجب التقرير إلا إذا كانت المصفوفات فارغة تماماً
        if not result.get("observations") and not result.get("metrics"):
            return f"""
            <div dir="rtl" style="background:#0f172a;border:1px solid #334155;border-radius:12px;padding:20px;
                 color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;text-align:right;">
              <div style="background:rgba(239,68,68,0.15);border:1px solid #ef4444;border-radius:8px;
                   padding:15px;color:#fca5a5;font-weight:600;text-align:center;">
                ⚠️ تعذر الفحص الدقيق: {note}
              </div>
            </div>"""
        quality_warning = f'<div style="background:rgba(234,179,8,0.15);border:1px solid #eab308;border-radius:8px;padding:10px 14px;color:#fde047;font-size:12px;margin-bottom:16px;">⚠️ {note}</div>'

    metrics_html = ""
    for m in result.get("metrics", []):
        score = min(max(int(m.get("score", 50)), 0), 100)
        metrics_html += f"""
        <div style="margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:4px;">
            <span>{m.get('name','معيار الفحص')}</span>
            <span style="font-weight:bold;color:#f8fafc;">{score}%</span>
          </div>
          <div style="background:#334155;height:6px;border-radius:3px;overflow:hidden;">
            <div style="background:{theme['border']};width:{score}%;height:100%;border-radius:3px;"></div>
          </div>
        </div>"""

    icons = {
        "damage":      ("❌", "#ef4444"),
        "discrepancy": ("⚠️", "#eab308"),
        "note":        ("💡", "#3b82f6"),
    }
    obs_html = ""
    for o in result.get("observations", []):
        icon, oc = icons.get(o.get("type","note"), ("📌","#94a3b8"))
        obs_html += f"""
        <div style="background:#1e293b;border-right:3px solid {oc};padding:10px 12px;
             border-radius:4px 8px 8px 4px;margin-bottom:8px;">
          <div style="font-size:14px;font-weight:bold;color:#f8fafc;margin-bottom:2px;">{icon} {o.get('title','ملاحظة')}</div>
          <div style="font-size:13px;color:#94a3b8;line-height:1.4;">{o.get('description','')}</div>
        </div>"""

    if not obs_html:
        obs_html = '<div style="font-size:13px;color:#94a3b8;text-align:center;">لم يتم تسجيل أي عيوب أو ملاحظات سلبية ظاهرة.</div>'

    score_val = min(max(int(result.get("overall_score", 70)), 0), 100)

    # إضافة كلاود: دمج الشعار (اللوغو) داخل واجهة التقرير المرفوع
    logo_html = ""
    if logo_b64:
        logo_html = f'<div style="text-align:center;margin-bottom:16px;padding:12px;background:#1e293b;border-radius:10px;border:1px solid #1e293b;"><img src="data:image/png;base64,{logo_b64}" style="max-height:56px;max-width:180px;object-fit:contain;" alt="شعار المتجر"></div>'

    return f"""
    <div dir="rtl" style="background:#0f172a;border:1px solid #1e293b;border-radius:16px;padding:24px;
         color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;max-width:650px;margin:auto;
         text-align:right;box-shadow:0 10px 25px rgba(0,0,0,0.3);">
      {logo_html}
      {quality_warning}
      <div style="display:flex;justify-content:space-between;align-items:center;
           border-bottom:1px solid #1e293b;padding-bottom:18px;margin-bottom:20px;">
        <div>
          <span style="font-size:12px;font-weight:600;color:#94a3b8;">تقرير الفحص الذكي الاحترافي</span>
          <h3 style="margin:4px 0 0 0;font-size:18px;color:{theme['text']};font-weight:bold;">{result.get('verdict_title','نتيجة الفحص')}</h3>
        </div>
        <div style="background:{theme['badge_bg']};border:1px solid {theme['border']};border-radius:12px;padding:8px 16px;text-align:center;">
          <div style="font-size:22px;font-weight:bold;color:{theme['text']};line-height:1;">{score_val}<span style="font-size:13px;color:#94a3b8;">/100</span></div>
          <div style="font-size:10px;color:#94a3b8;margin-top:2px;">التقييم العام</div>
        </div>
      </div>
      <div style="margin-bottom:20px;background:#182234;padding:14px;border-radius:12px;border:1px solid #1e293b;">
        <div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:12px;">📊 مؤشرات الجودة التفصيلية:</div>
        {metrics_html}
      </div>
      <div style="margin-bottom:20px;">
        <div style="font-size:13px;font-weight:bold;color:#f8fafc;margin-bottom:10px;">🔍 التحليل والملاحظات المرصودة:</div>
        {obs_html}
      </div>
      <div style="background:{theme['badge_bg']};border:1px dashed {theme['border']};border-radius:12px;padding:14px;margin-top:16px;">
        <div style="font-size:13px;font-weight:bold;color:{theme['text']};margin-bottom:4px;">💡 التوصية النهائية الشاملة:</div>
        <div style="font-size:13px;color:#e2e8f0;line-height:1.5;">{result.get('summary_for_user','')}</div>
      </div>
      <div style="font-size:10px;color:#64748b;text-align:center;margin-top:16px;border-top:1px solid #1e293b;padding-top:10px;">
        هذا التقرير الاحترافي صادر من الذكاء الاصطناعي بناءً على الفحص البصري والتحليل المتقدم للصورة المرفقة.
      </div>
    </div>"""

# ─── Endpoints ──────────────────────────────────────────────────────────────
@app.route("/credits", methods=["GET"])
def credits_check():
    email_addr = request.args.get("email", "").strip().lower()
    if not email_addr:
        return jsonify({"error": "email required"}), 400
    user = get_or_create_user(email_addr)
    photo_limit_map = {"free":1, "basic":1, "pro":2, "vip":4, "exempt":4}
    return jsonify({
        "credits":     user["credits"],
        "plan":        user["plan"],
        "photo_limit": photo_limit_map.get(user["plan"], 1),
        "is_exempt":   is_exempt(email_addr)
    })

@app.route("/upload", methods=["POST"])
def direct_upload():
    email_addr        = request.form.get("email", "").strip().lower()
    description       = request.form.get("description", "")
    secret_code_input = request.form.get("secret_code", "").strip()
    image_files       = request.files.getlist("image")

    if not email_addr:
        return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400
    if not image_files or all(f.filename == "" for f in image_files):
        return jsonify({"error": "لم يتم رفع أي صورة"}), 400

    # التحقق من الرمز السري للإيميلات المحمية
    if is_exempt(email_addr):
        log.info("Exempt email: %s | Code loaded: %s", email_addr, bool(ADMIN_SECRET_CODE))
        if not secret_code_input:
            return jsonify({"error": "secret_required", "message": "هذا الحساب محمي، أدخل الرمز السري"}), 401
        if not ADMIN_SECRET_CODE or secret_code_input != ADMIN_SECRET_CODE:
            log.warning("Bad secret code for %s", email_addr)
            return jsonify({"error": "invalid_secret", "message": "الرمز السري غير صحيح"}), 403

    num_images = len([f for f in image_files if f.filename != ""])
    cost       = max(1, num_images // 2)

    user = get_or_create_user(email_addr)
    if not is_exempt(email_addr) and user["credits"] < cost:
        return jsonify({"error": "نفد رصيدك", "credits": user["credits"]}), 402

    if not is_exempt(email_addr):
        db = get_db()
        db.execute("UPDATE users SET credits=credits-?, updated_at=? WHERE email=?",
                   (cost, datetime.now(timezone.utc).isoformat(), email_addr))
        db.commit()

       try:
       valid_files = [f for f in image_files if f.filename != ""]
        images_bytes_list = [f.read() for f in valid_files]
        result = analyze_image(images_bytes_list, description, description)
        
        # إضافة كلاود: استقبال ومعالجة الشعار 
        custom_logo_b64 = None
        logo_file = request.files.get("custom_logo")
        if logo_file and logo_file.filename:
            try:
                logo_bytes = logo_file.read()
                logo_img = Image.open(io.BytesIO(logo_bytes))
                if logo_img.mode in ("P",): logo_img = logo_img.convert("RGBA")
                logo_img.thumbnail((240, 90), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                logo_img.save(out, format="PNG")
                custom_logo_b64 = base64.b64encode(out.getvalue()).decode()
            except Exception as e:
                log.error("Logo processing error: %s", e)

        user   = get_or_create_user(email_addr)
        return jsonify({
            "status":    "success",
            "report":    format_report_html(result, logo_b64=custom_logo_b64), # تمرير الشعار 
            "credits":   user["credits"],
            "cost":      cost,
            "plan":      user["plan"],
            "is_exempt": is_exempt(email_addr)
        })
    except Exception as e:
        log.exception("Upload analysis error")
        return jsonify({"error": str(e)}), 500

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
            return jsonify({"status": "success", "plan": plan_name, "credits": credits}), 200

        elif event_name in ("subscription_created", "subscription_payment_success"):
            add_vip_credits(customer_email)
            return jsonify({"status": "vip_updated"}), 200

        elif event_name in ("subscription_cancelled", "subscription_expired", "subscription_payment_failed"):
            return jsonify({"status": "cancelled"}), 200

        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        log.exception("Webhook error")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
