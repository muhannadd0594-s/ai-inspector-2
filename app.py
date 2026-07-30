import os
import json
import base64
import logging
import email.utils
import hashlib
import hmac
import io
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g
import requests
from dotenv import load_dotenv
from PIL import Image
import psycopg2

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector")

# ─── Configuration & Credentials ───────────────────────────────────────────
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
RESEND_API_KEY      = os.environ.get("RESEND_API_KEY", "")
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")
FROM_ADDRESS        = "inspector@inspector.editchecker.com"
ADMIN_EMAIL         = "akashiiso04@gmail.com"
SITE_URL            = "editchecker.com"
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
    "1962077": ("basic", 10),
    "1962093": ("pro",   50),
    "1962096": ("vip",  120),
}

# ─── Database Helpers (Supabase) ────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                credits INTEGER DEFAULT 0,
                plan TEXT DEFAULT 'free',
                updated_at TEXT
            )""")
        conn.commit()

def is_exempt(email_addr):
    return email_addr.strip().lower() in EXEMPT_EMAILS

def get_or_create_user(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO users (email, credits, plan, updated_at) VALUES (%s, %s, %s, %s)
                               ON CONFLICT(email) DO UPDATE SET credits=EXCLUDED.credits,
                               plan=EXCLUDED.plan, updated_at=EXCLUDED.updated_at""",
                            (email_addr, EXEMPT_CREDITS, "exempt", datetime.utcnow().isoformat()))
            conn.commit()
        return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "exempt"}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email_addr,))
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute("INSERT INTO users (email, credits, plan, updated_at) VALUES (%s, %s, %s, %s)",
                        (email_addr, FREE_CREDITS, "free", datetime.utcnow().isoformat()))
        conn.commit()
    return {"email": email_addr, "credits": FREE_CREDITS, "plan": "free"}

def deduct_credit(email_addr):
    email_addr = email_addr.strip().lower()
    if is_exempt(email_addr):
        get_or_create_user(email_addr)
        return True
    user = get_or_create_user(email_addr)
    if user["credits"] <= 0:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits=credits-1, updated_at=%s WHERE email=%s",
                        (datetime.utcnow().isoformat(), email_addr))
        conn.commit()
    return True

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    get_or_create_user(email_addr)
    if is_exempt(email_addr):
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits=credits+%s, plan=%s, updated_at=%s WHERE email=%s",
                        (amount, plan, datetime.utcnow().isoformat(), email_addr))
        conn.commit()
        
# ─── Init DB on startup ─────────────────────────────────────────────────────
with app.app_context():
    try:
        init_db()
        log.info("Turso DB initialized successfully")
    except Exception as e:
        log.error("Turso DB init error: %s", e)

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
        log.error("OPENROUTER_API_KEY is missing!")
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
        "model": "google/gemini-2.5-pro",
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
            "quality_note": "تعذر تحليل الاستجابة.",
            "overall_score": 50,
            "verdict_title": "تحليل غير مكتمل",
            "verdict_status": "warning",
            "metrics": [],
            "observations": [],
            "summary_for_user": "حدث خطأ أثناء المعالجة. يُرجى إعادة المحاولة."
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
        note = result.get("quality_note", "الصورة غير واضحة بشكل كافٍ.")
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
        obs_html = '<div style="font-size:13px; color:#94a3b8; text-align:center;">لم يتم تسجيل أي عيوب ظاهرة.</div>'

    score_val = min(max(int(result.get("overall_score", 70)), 0), 100)

    return f"""
    <div dir="rtl" style="background:#0f172a; border:1px solid #1e293b; border-radius:16px; padding:24px; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; max-width:650px; margin:auto; text-align:right; box-shadow:0 10px 25px rgba(0,0,0,0.3);">
        <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1e293b; padding-bottom:18px; margin-bottom:20px;">
            <div>
                <span style="font-size:12px; font-weight:600; color:#94a3b8;">تقرير الفحص الذكي</span>
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
            <div style="font-size:13px; font-weight:bold; color:#f8fafc; margin-bottom:12px;">📊 مؤشرات الجودة:</div>
            {metrics_html}
        </div>
        <div style="margin-bottom:20px;">
            <div style="font-size:13px; font-weight:bold; color:#f8fafc; margin-bottom:10px;">🔍 الملاحظات:</div>
            {obs_html}
        </div>
        <div style="background:{theme['badge_bg']}; border:1px dashed {theme['border']}; border-radius:12px; padding:14px; margin-top:16px;">
            <div style="font-size:13px; font-weight:bold; color:{theme['text']}; margin-bottom:4px;">💡 التوصية النهائية:</div>
            <div style="font-size:13px; color:#e2e8f0; line-height:1.5;">
                {result.get('summary_for_user', '')}
            </div>
        </div>
        <div style="font-size:10px; color:#64748b; text-align:center; margin-top:16px; border-top:1px solid #1e293b; padding-top:10px;">
            تحليل استرشادي آلي — القرار النهائي يعود إليك.
        </div>
    </div>
    """

def send_reply(to_address, subject, html_body):
    if not RESEND_API_KEY:
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            json={
                "from": f"AI Product Inspector <{FROM_ADDRESS}>",
                "to": [to_address],
                "subject": f"تقرير فحص منتجك: Re: {subject}",
                "html": html_body
            },
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            timeout=20
        )
        resp.raise_for_status()
        log.info("Reply sent to %s", to_address)
    except requests.RequestException as e:
        log.error("Failed to send reply: %s", e)

def forward_to_admin(sender, subject, body):
    if not RESEND_API_KEY:
        return
    try:
        requests.post(
            "https://api.resend.com/emails",
            json={
                "from": f"AI Inspector Bot <{FROM_ADDRESS}>",
                "to": [ADMIN_EMAIL],
                "subject": f"[دعم فني] من {sender}: {subject}",
                "text": f"المرسل: {sender}\n\n{body}"
            },
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            timeout=20
        )
    except requests.RequestException as e:
        log.error("Failed to forward to admin: %s", e)

def fetch_image_from_resend(email_id, attachments_meta):
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
    for att in attachments_meta:
        att_id = att.get("id")
        if not att_id or not att.get("content_type", "").startswith("image/"):
            continue
        try:
            r = requests.get(
                f"https://api.resend.com/emails/receiving/{email_id}/attachments/{att_id}",
                headers=headers, timeout=15
            )
            if r.status_code != 200:
                continue
            dl = r.json().get("download_url")
            if not dl:
                continue
            img = requests.get(dl, timeout=20)
            if img.status_code == 200:
                return img.content
        except requests.RequestException as e:
            log.error("Error fetching attachment: %s", e)
    return None

# ─── Routes ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

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

@app.route("/webhook", methods=["POST"])
def resend_webhook():
    try:
        event = request.get_json(force=True, silent=True) or {}
        data  = event.get("data", event)
        email_id = data.get("email_id") or data.get("id")

        raw_from = data.get("from", "")
        if isinstance(raw_from, list) and raw_from:
            raw_from = raw_from[0]

        _, sender = email.utils.parseaddr(str(raw_from))
        if not sender:
            sender = str(raw_from)

        subject          = data.get("subject", "")
        attachments_meta = data.get("attachments", [])
        caption          = ""

        if email_id and RESEND_API_KEY:
            headers   = {"Authorization": f"Bearer {RESEND_API_KEY}"}
            body_resp = requests.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers=headers, timeout=15
            )
            if body_resp.status_code == 200:
                bd = body_resp.json()
                caption = bd.get("text") or bd.get("html") or ""

        image_bytes = fetch_image_from_resend(email_id, attachments_meta) if email_id else None

        if image_bytes and sender:
            log.info("Analyzing inbound email from %s", sender)
            result = analyze_image(image_bytes, caption, subject)
            send_reply(sender, subject, format_report_html(result))
            return jsonify({"status": "success"}), 200

        if sender and caption:
            forward_to_admin(sender, subject, caption)

        return jsonify({"status": "ignored"}), 200
    except Exception as e:
        log.exception("Resend webhook error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/lemonsqueezy/webhook", methods=["POST"])
def lemonsqueezy_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Signature", "")

    if LEMONSQUEEZY_SECRET and signature:
        expected = hmac.new(
            LEMONSQUEEZY_SECRET.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            log.warning("LemonSqueezy: invalid signature")
            return jsonify({"error": "invalid signature"}), 401

    try:
        payload    = request.get_json(force=True) or {}
        event_name = payload.get("meta", {}).get("event_name", "")

        if event_name != "order_created":
            return jsonify({"status": "ignored", "event": event_name}), 200

        attrs          = payload.get("data", {}).get("attributes", {})
        customer_email = attrs.get("user_email", "").strip().lower()

        if not customer_email:
            log.error("LemonSqueezy: no customer email in payload")
            return jsonify({"error": "no email"}), 400

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

    except Exception as e:
        log.exception("LemonSqueezy webhook error")
        return jsonify({"error": str(e)}), 500

# ─── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
