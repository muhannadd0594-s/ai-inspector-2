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

# === أضف هذا المسار ليعرض ملف templates/index.html عند زيارة الموقع ===
@app.route('/')
def home():
    return render_template('index.html')

# ─── Configuration & Credentials ───────────────────────────────────────────
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")
SITE_URL            = "editchecker.com"
DB_PATH             = os.environ.get("DB_PATH", "/tmp/inspector.db")
FREE_CREDITS        = 3

# ─── Exempt emails: unlimited-credit accounts ─────────────────────────────
EXEMPT_CREDITS = 999
EXEMPT_EMAILS = {
    "akashiiso04@gmail.com",
    "muhannadd0594@gmail.com",
    "mohammdlghmd@gmail.com",
}

# ─── LemonSqueezy Variant IDs → (plan_name, credits) ───────────────────────
PLAN_CREDITS = {
    "e810b85b-5273-4da2-9477-f3cf62f9737d": ("basic", 10),
    "db680fa5-9ec4-4fed-81fe-0ad4928266c3": ("pro",   50),
    "ceff30c8-9ba9-4c2a-bfb8-0cd520a9c072": ("vip",  120),
}

# ─── Database Helpers ───────────────────────────────────────────────────────
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
        if row is None or row["credits"] != EXEMPT_CREDITS or row["plan"] != "exempt":
            db.execute("""INSERT INTO users (email, credits, plan, updated_at) VALUES (?, ?, ?, ?)
                          ON CONFLICT(email) DO UPDATE SET credits=excluded.credits,
                          plan=excluded.plan, updated_at=excluded.updated_at""",
                       (email_addr, EXEMPT_CREDITS, "exempt", now_iso))
            db.commit()
        return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "exempt"}

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
    """Add 120 VIP credits with 200 cap (rollover logic)."""
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        return
    get_or_create_user(email_addr)
    db = get_db()
    db.execute("""
        UPDATE users
        SET credits    = MIN(credits + 120, 200),
            plan       = 'vip',
            updated_at = ?
        WHERE email = ?
    """, (datetime.now(timezone.utc).isoformat(), email_addr))
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

# ─── Image & AI Logic ───────────────────────────────────────────────────────
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

def get_dynamic_prompt(subject, caption):
    combined = f"{subject} {caption}".lower()
    base = """أنت خبير واخصائي فحص جودة المنتجات وتوثيق حالة السلع.
قم بتحليل صورة المنتج والوصف بدقة وإصدار تقرير فحص احترافي.
يجب أن ترجع إجابتك ككائن JSON خام فقط (بدون markdown أو ```json):
{
  "image_quality": "good|poor|unusable",
  "quality_note": "شرح مختصر جداً باللغة العربية إذا كانت الصورة غير واضحة، وإلا اتركها فارغة",
  "overall_score": 75,
  "verdict_title": "حالة جيدة مع ملاحظات طفيفة",
  "verdict_status": "success|warning|danger",
  "metrics": [
    {"name": "النظافة وخلو السطح من العيوب", "score": 70},
    {"name": "سلامة الهيكل والقماش/المعدن", "score": 80},
    {"name": "التطابق مع وصف البائع", "score": 75}
  ],
  "observations": [
    {"type": "damage|discrepancy|note", "title": "عنوان الملاحظة", "description": "شرح مختصر ومباشر باللغة العربية"}
  ],
  "summary_for_user": "توصية نهائية موجزة جداً (سطران كحد أقصى) توضح هل المنتج يستحق الشراء أم لا."
}
CRITICAL: 
1. جميع النصوص داخل JSON تكون باللغة العربية حصراً.
2. التقييمات تكون واقعية وموزونة (بين 30% إلى 95%).
3. حافظ على الاختصار الشديد والتركيز في الملاحظات."""

    if any(w in combined for w in ["جوال", "ايفون", "لابتوب", "شاشة", "ايباد", "phone", "electronics"]):
        cat = "\n\nFocus (Electronics): screen scratches, damaged corners, camera, back glass."
    elif any(w in combined for w in ["ساعة", "ماركة", "شنطة", "نظارة", "محفظة", "watch", "bag", "luxury"]):
        cat = "\n\nFocus (Luxury): logo accuracy, stitching, engravings, leather/metal wear."
    elif any(w in combined for w in ["سيارة", "سيارات", "قطع", "صدام", "جنط", "car", "auto"]):
        cat = "\n\nFocus (Auto): rust, cracks, paint resprays, color differences, dents."
    elif any(w in combined for w in ["ملابس", "ثوب", "قميص", "فستان", "حذاء", "clothes", "fashion"]):
        cat = "\n\nFocus (Fashion): fabric condition, stains, loose threads, tears."
    else:
        cat = "\n\nFocus (General): comprehensive quality inspection."

    user = f'\n\nSeller caption:\n"{caption}"' if caption else ""
    return base + cat + user

def analyze_image(image_bytes, caption, subject):
    if not OPENROUTER_API_KEY:
        log.error("OPENROUTER_API_KEY is missing from environment variables!")
        return {
            "image_quality": "unusable",
            "quality_note": "مفتاح التشغيل غير متوفر.",
            "overall_score": 0,
            "verdict_title": "خطأ إعدادات",
            "verdict_status": "danger",
            "metrics": [],
            "observations": [],
            "summary_for_user": "تعذر إجراء الفحص بسبب خطأ في الإعدادات."
        }

    compressed = compress_image(image_bytes)
    b64        = base64.b64encode(compressed).decode()
    prompt     = get_dynamic_prompt(subject, caption)

    payload = {
        "models": [
            "anthropic/claude-3.5-sonnet",
            "google/gemini-2.0-flash-001",
            "openai/gpt-4o-mini"
        ],
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }
        ]
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
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

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

def format_report_html(result):
    status = result.get("verdict_status", "warning")
    color_map = {
        "success": {"badge_bg": "rgba(34, 197, 94, 0.15)", "border": "#22c55e", "text": "#4ade80"},
        "warning": {"badge_bg": "rgba(234, 179, 8, 0.15)", "border": "#eab308", "text": "#fde047"},
        "danger":  {"badge_bg": "rgba(239, 68, 68, 0.15)", "border": "#ef4444", "text": "#fca5a5"}
    }
    theme = color_map.get(status, color_map["warning"])

    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة المرفوقة غير واضحة بشكل كافٍ لإعطاء تقرير دقيق.")
        return f"""
        <div dir="rtl" style="background:#0f172a; border:1px solid #334155; border-radius:12px; padding:20px; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; text-align:right;">
            <div style="background:rgba(239, 68, 68, 0.15); border:1px solid #ef4444; border-radius:8px; padding:15px; color:#fca5a5; font-weight:600; text-align:center;">
                ⚠️ تعذر الفحص الدقيق: {note}
            </div>
        </div>
        """

    metrics_html = ""
    for m in result.get("metrics", []):
        score = min(max(int(m.get("score", 50)), 0), 100)
        metrics_html += f"""
        <div style="margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; font-size:13px; color:#cbd5e1; margin-bottom:4px;">
                <span>{m.get('name', 'معيار الفحص')}</span>
                <span style="font-weight:bold; color:#f8fafc;">{score}%</span>
            </div>
            <div style="background:#334155; height:6px; border-radius:3px; overflow:hidden;">
                <div style="background:{theme['border']}; width:{score}%; height:100%; border-radius:3px;"></div>
            </div>
        </div>
        """

    icons = {
        "damage": ("❌", "#ef4444"),
        "discrepancy": ("⚠️", "#eab308"),
        "note": ("💡", "#3b82f6")
    }

    obs_html = ""
    for o in result.get("observations", []):
        o_type = o.get("type", "note")
        icon, o_color = icons.get(o_type, ("📌", "#94a3b8"))
        obs_html += f"""
        <div style="background:#1e293b; border-right:3px solid {o_color}; padding:10px 12px; border-radius:4px 8px 8px 4px; margin-bottom:8px;">
            <div style="font-size:14px; font-weight:bold; color:#f8fafc; margin-bottom:2px;">
                {icon} {o.get('title', 'ملاحظة')}
            </div>
            <div style="font-size:13px; color:#94a3b8; line-height:1.4;">
                {o.get('description', '')}
            </div>
        </div>
        """

    if not obs_html:
        obs_html = '<div style="font-size:13px; color:#94a3b8; text-align:center;">لم يتم تسجيل أي عيوب أو ملاحظات سلبية ظاهرة.</div>'

    score_val = min(max(int(result.get("overall_score", 70)), 0), 100)

    return f"""
    <div dir="rtl" style="background:#0f172a; border:1px solid #1e293b; border-radius:16px; padding:24px; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; max-width:650px; margin:auto; text-align:right; box-shadow:0 10px 25px rgba(0,0,0,0.3);">
        
        <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1e293b; padding-bottom:18px; margin-bottom:20px;">
            <div>
                <span style="font-size:12px; font-weight:600; color:#94a3b8; text-transform:uppercase; letter-spacing:0.5px;">تقرير الفحص الذكي</span>
                <h3 style="margin:4px 0 0 0; font-size:18px; color:{theme['text']}; font-weight:bold;">
                    {result.get('verdict_title', 'نتيجة الفحص')}
                </h3>
            </div>
            <div style="background:{theme['badge_bg']}; border:1px solid {theme['border']}; border-radius:12px; padding:8px 16px; text-align:center;">
                <div style="font-size:22px; font-weight:bold; color:{theme['text']}; line-height:1;">{score_val}<span style="font-size:13px; color:#94a3b8;">/100</span></div>
                <div style="font-size:10px; color:#94a3b8; margin-top:2px;">التقييم العام</div>
            </div>
        </div>

        <div style="margin-bottom:20px; background:#182234; padding:14px; border-radius:12px; border:1px solid #1e293b;">
            <div style="font-size:13px; font-weight:bold; color:#f8fafc; margin-bottom:12px;">📊 مؤشرات الجودة التفصيلية:</div>
            {metrics_html}
        </div>

        <div style="margin-bottom:20px;">
            <div style="font-size:13px; font-weight:bold; color:#f8fafc; margin-bottom:10px;">🔍 الملاحظات المرصودة:</div>
            {obs_html}
        </div>

        <div style="background:{theme['badge_bg']}; border:1px dashed {theme['border']}; border-radius:12px; padding:14px; margin-top:16px;">
            <div style="font-size:13px; font-weight:bold; color:{theme['text']}; margin-bottom:4px;">💡 التوصية النهائية:</div>
            <div style="font-size:13px; color:#e2e8f0; line-height:1.5;">
                {result.get('summary_for_user', '')}
            </div>
        </div>

        <div style="font-size:10px; color:#64748b; text-align:center; margin-top:16px; border-top:1px solid #1e293b; padding-top:10px;">
            هذا التقرير الصادر من الذكاء الاصطناعي هو تحليل استرشادي بناءً على معالجة الصورة المرفقة.
        </div>
    </div>
    """

@app.route("/credits", methods=["GET"])
def credits_check():
    email_addr = request.args.get("email", "").strip().lower()
    if not email_addr:
        return jsonify({"error": "email required"}), 400
    user = get_or_create_user(email_addr)
    return jsonify({"credits": user["credits"], "plan": user["plan"]})

@app.route("/upload", methods=["POST"])
def direct_upload():
    email_addr  = request.form.get("email", "").strip().lower()
    description = request.form.get("description", "")
    image_file  = request.files.get("image")

    if not email_addr:
        return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400
    if not image_file:
        return jsonify({"error": "لم يتم رفع أي صورة"}), 400

    if not deduct_credit(email_addr):
        return jsonify({"error": "نفد رصيدك", "credits": 0}), 402

    try:
        result = analyze_image(image_file.read(), description, description)
        user   = get_or_create_user(email_addr)
        return jsonify({
            "status": "success",
            "report": format_report_html(result),
            "credits": user["credits"]
        })
    except Exception as e:
        log.exception("Upload analysis error")
        return jsonify({"error": str(e)}), 500

@app.route("/lemonsqueezy/webhook", methods=["POST"])
def lemonsqueezy_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Signature", "")

    if LEMONSQUEEZY_SECRET:
        expected = hmac.new(
            LEMONSQUEEZY_SECRET.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            log.warning("LemonSqueezy: invalid signature")
            return jsonify({"error": "invalid signature"}), 401
    else:
        log.warning("LemonSqueezy: LEMONSQUEEZY_SECRET not set — skipping signature check")

    try:
        payload    = request.get_json(force=True) or {}
        event_name = payload.get("meta", {}).get("event_name", "")

        attrs          = payload.get("data", {}).get("attributes", {})
        customer_email = attrs.get("user_email", "").strip().lower()

        if not customer_email:
            log.error("LemonSqueezy: no customer email in payload")
            return jsonify({"error": "no email"}), 400

        # ── One-time purchase (Basic / Pro) ────────────────────────────────
        if event_name == "order_created":
            variant_id = None
            for item in payload.get("included", []):
                if item.get("type") == "order-items":
                    raw_vid    = item.get("attributes", {}).get("variant_id", "")
                    variant_id = str(raw_vid).strip()
                    break
            if not variant_id:
                log.error("LemonSqueezy: variant_id not found in payload")
                return jsonify({"status": "no_variant"}), 200
            plan_info = PLAN_CREDITS.get(variant_id)
            if not plan_info:
                log.warning("LemonSqueezy: unknown variant_id=%s", variant_id)
                return jsonify({"status": "unknown_plan", "variant_id": variant_id}), 200
            plan_name, credits = plan_info
            add_credits(customer_email, plan_name, credits)
            log.info("Granted %d credits (%s) to %s via variant %s",
                     credits, plan_name, customer_email, variant_id)
            return jsonify({"status": "success", "plan": plan_name, "credits": credits}), 200

        # ── VIP: first subscription ────────────────────────────────────────
        elif event_name == "subscription_created":
            add_vip_credits(customer_email)
            log.info("VIP created for %s", customer_email)
            return jsonify({"status": "vip_created"}), 200

        # ── VIP: monthly renewal ───────────────────────────────────────────
        elif event_name == "subscription_payment_success":
            add_vip_credits(customer_email)
            log.info("VIP renewed for %s", customer_email)
            return jsonify({"status": "renewed"}), 200

        # ── VIP: cancelled / expired / failed ─────────────────────────────
        elif event_name in ("subscription_cancelled", "subscription_expired",
                            "subscription_payment_failed"):
            log.info("VIP ended for %s — event: %s", customer_email, event_name)
            return jsonify({"status": "cancelled"}), 200

        else:
            return jsonify({"status": "ignored", "event": event_name}), 200

    except Exception as e:
        log.exception("LemonSqueezy webhook error")
        return jsonify({"error": str(e)}), 500

# ─── Main Execution ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
