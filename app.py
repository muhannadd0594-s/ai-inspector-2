import os
import json
import base64
import logging
import email.utils
import sqlite3
import hashlib
import hmac
import io
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g
import requests
from dotenv import load_dotenv
from PIL import Image

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

    if is_exempt(email_addr):
        if row is None or row["credits"] != EXEMPT_CREDITS or row["plan"] != "exempt":
            db.execute("""INSERT INTO users (email, credits, plan, updated_at) VALUES (?, ?, ?, ?)
                          ON CONFLICT(email) DO UPDATE SET credits=excluded.credits,
                          plan=excluded.plan, updated_at=excluded.updated_at""",
                       (email_addr, EXEMPT_CREDITS, "exempt", datetime.utcnow().isoformat()))
            db.commit()
        return {"email": email_addr, "credits": EXEMPT_CREDITS, "plan": "exempt"}

    if row:
        return dict(row)

    db.execute("INSERT INTO users (email, credits, plan, updated_at) VALUES (?, ?, ?, ?)",
               (email_addr, FREE_CREDITS, "free", datetime.utcnow().isoformat()))
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
               (datetime.utcnow().isoformat(), email_addr))
    db.commit()
    return True

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    db = get_db()
    get_or_create_user(email_addr)

    if is_exempt(email_addr):
        return

    db.execute("UPDATE users SET credits=credits+?, plan=?, updated_at=? WHERE email=?",
               (amount, plan, datetime.utcnow().isoformat(), email_addr))
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
    base = """You are a world-class AI Product Quality Inspector and Forensic E-commerce Authenticator.
Analyze the product image and seller caption with extreme precision.
Return ONLY a valid raw JSON object — no markdown, no code fences:
{
  "image_quality": "good|poor|unusable",
  "quality_note": "سبب باللغة العربية حصراً إذا كانت الصورة سيئة، أو اتركها فارغة",
  "observations": [{"type": "damage|discrepancy|inconsistency|note", "description": "باللغة العربية حصراً"}],
  "seller_claim_check": "matches|contradicts|cannot_confirm",
  "summary_for_user": "ملخص 2-3 جمل عربية، يبدأ بـ المنتج يبدو... أو نلاحظ...، ينتهي بتوصية واضحة"
}
CRITICAL: ALL text values MUST be in Arabic ONLY. No English in values. If no issues, add 1-2 positive note observations."""

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

    user = f'\n\nSeller caption:\n"{caption}"\nInspect accordingly.' if caption else \
           "\n\nNo caption. Inspect based on category focus."
    return base + cat + user

def analyze_image(image_bytes, caption, subject):
    if not OPENROUTER_API_KEY:
        log.error("OPENROUTER_API_KEY is missing from environment variables!")
        return {
            "image_quality": "unusable",
            "observations": [],
            "seller_claim_check": "cannot_confirm",
            "summary_for_user": "خطأ في الإعدادات: مفتاح OpenRouter غير معرف."
        }

    compressed = compress_image(image_bytes)
    b64        = base64.b64encode(compressed).decode()
    prompt     = get_dynamic_prompt(subject, caption)

    # قائمة بالنماذج الممتازة الرائعة في قراءة الصور (يتم المحاولة بترتيب القائمة)
    payload = {
        "models": [
            "anthropic/claude-3.5-sonnet",
            "anthropic/claude-3.5-sonnet:beta",
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

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://editchecker.com",
            "X-Title": "AI Product Inspector"
        },
        timeout=45
    )

    if resp.status_code != 200:
        log.error("OpenRouter API Error [%d]: %s", resp.status_code, resp.text)
        resp.raise_for_status()

    raw   = resp.json()["choices"][0]["message"]["content"]
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("JSON parse error: %s", raw)
        return {
            "image_quality": "unusable",
            "observations": [],
            "seller_claim_check": "cannot_confirm",
            "summary_for_user": "حدث خطأ تقني أثناء تحليل التقرير. يرجى إعادة الإرسال."
        }

# ─── Formatting & Email Logic ───────────────────────────────────────────────
def format_report_html(result):
    if result.get("image_quality") in ("poor", "unusable"):
        return (f'<div dir="rtl" style="font-family:Arial;padding:20px;background:#fce4e4;color:#cc0000;border-radius:8px;">'
                f'<h3>⚠️ الصورة غير واضحة</h3><p>{result.get("quality_note", "نعتذر، لم نتمكن من قراءة التفاصيل.")}</p></div>')

    sc  = "#27ae60"
    st  = "يبدو المنتج في حالة جيدة"
    obs = result.get("observations", [])

    if result.get("seller_claim_check") == "contradicts":
        sc, st = "#e74c3c", "⚠️ تعارض محتمل مع وصف البائع!"
    elif any(o["type"] in ("damage", "discrepancy") for o in obs):
        sc, st = "#f39c12", "تم رصد بعض الملاحظات"

    icons = {
        "damage": "❌ [تلف]",
        "discrepancy": "⚠️ [تعارض]",
        "inconsistency": "🔍 [ملاحظة]",
        "note": "💡 [معلومة]"
    }

    obs_html = "".join(
        f"<li style='margin-bottom:10px'><strong>{icons.get(o['type'], '📌')}</strong> {o['description']}</li>"
        for o in obs
    ) or "<li>لم يلاحظ النظام أي مشاكل ظاهرة.</li>"

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Tahoma,sans-serif;background:#f4f7f6;margin:0;padding:20px;">
<div style="max-width:600px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 15px rgba(0,0,0,.05);">
<div style="background:#2c3e50;color:#fff;padding:20px;text-align:center;"><h2 style="margin:0">🔍 تقرير فحص المنتج</h2></div>
<div style="padding:30px;">
<div style="background:{sc};color:white;padding:10px 15px;border-radius:6px;font-weight:bold;margin-bottom:20px;text-align:center">{st}</div>
<h3 style="color:#2c3e50;border-bottom:2px solid #ecf0f1;padding-bottom:8px">الخلاصة:</h3>
<p style="color:#34495e;line-height:1.6;font-size:16px">{result.get("summary_for_user","")}</p>
<h3 style="color:#2c3e50;border-bottom:2px solid #ecf0f1;padding-bottom:8px;margin-top:25px">التفاصيل:</h3>
<ul style="color:#34495e;line-height:1.6;font-size:15px;padding-right:20px">{obs_html}</ul>
<p style="color:#95a5a6;font-size:11px;margin-top:20px;border-top:1px solid #ecf0f1;padding-top:15px;text-align:center">
هذا تحليل آلي استرشادي غير ملزم. القرار النهائي يعود لك.</p>
</div></div></body></html>"""

def send_reply(to_address, subject, html_body):
    resp = requests.post(
        "[https://api.resend.com/emails](https://api.resend.com/emails)",
        json={
            "from": f"AI Product Inspector <{FROM_ADDRESS}>",
            "to": [to_address],
            "subject": f"تقرير فحص منتجك: Re: {subject}",
            "html": html_body
        },
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        },
        timeout=20
    )
    resp.raise_for_status()
    log.info("Reply sent successfully to %s", to_address)

def forward_to_admin(sender, subject, body):
    requests.post(
        "[https://api.resend.com/emails](https://api.resend.com/emails)",
        json={
            "from": f"AI Inspector Bot <{FROM_ADDRESS}>",
            "to": [ADMIN_EMAIL],
            "subject": f"[دعم فني] من {sender}: {subject}",
            "text": f"المرسل: {sender}\n\n{body}"
        },
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        },
        timeout=20
    )

def fetch_image_from_resend(email_id, attachments_meta):
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
    for att in attachments_meta:
        att_id = att.get("id")
        if not att_id or not att.get("content_type", "").startswith("image/"):
            continue

        r = requests.get(
            f"[https://api.resend.com/emails/receiving/](https://api.resend.com/emails/receiving/){email_id}/attachments/{att_id}",
            headers=headers,
            timeout=15
        )
        if r.status_code != 200:
            continue

        dl = r.json().get("download_url")
        if not dl:
            continue

        img = requests.get(dl, timeout=20)
        if img.status_code == 200:
            return img.content
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
            body_resp = requests.get(f"[https://api.resend.com/emails/receiving/](https://api.resend.com/emails/receiving/){email_id}",
                                     headers=headers, timeout=15)
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

# ─── Main Execution ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
