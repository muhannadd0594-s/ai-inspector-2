import os
import json
import base64
import logging
import email.utils
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
import io
from PIL import Image

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector-web")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
RESEND_API_KEY     = os.environ.get("RESEND_API_KEY", "")
FROM_ADDRESS       = "inspector@inspector.editchecker.com"

# -------------------------------------------------------------
# ضغط الصور لتوفير الموارد
# -------------------------------------------------------------
def compress_image(image_bytes: bytes, max_size=(800, 800)) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        log.error(f"Image compression failed: {e}")
        return image_bytes

# -------------------------------------------------------------
# نظام الفئات والبرومبت الذكي
# -------------------------------------------------------------
def get_dynamic_prompt(subject: str, caption: str) -> str:
    combined_text = f"{subject} {caption}".lower()
    base_prompt = """You are an expert AI Product Quality Inspector.
Analyze the provided product image and caption/description.
Return ONLY a JSON object with this structure:
{
  "image_quality": "good|poor|unusable",
  "quality_note": "reason if poor/unusable",
  "observations": [
    {"type": "damage|discrepancy|inconsistency|note", "description": "Arabic text"}
  ],
  "seller_claim_check": "matches|contradicts|cannot_confirm",
  "summary_for_user": "Short Arabic summary of the overall item status"
}"""

    if any(word in combined_text for word in ["جوال", "الكترونيات", "ايفون", "لابتوب", "شاشة", "ايباد", "phone", "electronics"]):
        category_focus = "\n\nCategory Focus (Electronics & Phones): Strictly inspect for screen scratches, damaged corners, camera lens cleanliness, and back glass cracks or defects."
    elif any(word in combined_text for word in ["ساعة", "ماركة", "شنطة", "نظارة", "محفظة", "watch", "bag", "luxury"]):
        category_focus = "\n\nCategory Focus (Watches & Luxury): Strictly inspect logo accuracy, stitching quality, engravings, and wear/tear on leather or metal."
    elif any(word in combined_text for word in ["سيارة", "سيارات", "قطع", "صدام", "شمعة", "جنط", "car", "auto", "parts"]):
        category_focus = "\n\nCategory Focus (Car Parts & Autos): Strictly inspect for rust, cracks, paint resprays or color differences, and dents."
    elif any(word in combined_text for word in ["ملابس", "ازياء", "ثوب", "قميص", "فستان", "شوز", "حذاء", "clothes", "fashion"]):
        category_focus = "\n\nCategory Focus (Clothing & Fashion): Strictly inspect fabric condition, visible stains, loose threads, and tears."
    else:
        category_focus = "\n\nCategory Focus (General): Perform a general quality inspection on the product."

    if caption:
        user_text = f"\n\nSeller's caption / user notes:\n\"{caption}\"\n\nInspect the image according to these claims."
    else:
        user_text = "\n\nNo seller caption provided. Inspect the image based on the category focus."

    return base_prompt + category_focus + user_text

# -------------------------------------------------------------
# تحليل الصورة عبر الذكاء الاصطناعي
# -------------------------------------------------------------
def analyze_image(image_bytes: bytes, caption: str, subject: str) -> dict:
    compressed_bytes = compress_image(image_bytes)
    b64 = base64.b64encode(compressed_bytes).decode()
    final_prompt = get_dynamic_prompt(subject, caption)

    payload = {
        "model": "google/gemini-2.5-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": final_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0.2,
    }
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        timeout=45,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("Failed to parse model response: %s", raw)
        return {
            "image_quality": "unusable",
            "observations": [],
            "seller_claim_check": "cannot_confirm",
            "summary_for_user": "حدث خطأ تقني في تحليل الاستجابة. أعد الإرسال من فضلك.",
        }

# -------------------------------------------------------------
# تنسيق تقرير HTML باللغة العربية
# -------------------------------------------------------------
def format_report_html(result: dict) -> str:
    status_color = "#27ae60"
    status_text = "يبدو المنتج في حالة جيدة"

    if result.get("image_quality") in ("poor", "unusable"):
        return f"""
        <div dir="rtl" style="font-family: Arial, sans-serif; padding: 20px; background-color: #fce4e4; color: #cc0000; border-radius: 8px;">
            <h3>⚠️ الصورة غير واضحة</h3>
            <p>{result.get('quality_note', 'نعتذر، لم نتمكن من فحص المنتج بوضوح.')}</p>
        </div>
        """

    if result.get("seller_claim_check") == "contradicts":
        status_color = "#e74c3c"
        status_text = "⚠️ تنبيه: يوجد تعارض محتمل مع وصف البائع!"
    elif result.get("observations") and any(o['type'] in ('damage', 'discrepancy') for o in result.get("observations")):
        status_color = "#f39c12"
        status_text = "تم رصد بعض الملاحظات على المنتج"

    obs_html = ""
    icons = {"damage": "❌ [تلف]", "discrepancy": "⚠️ [تعارض]", "inconsistency": "🔍 [ملاحظة]", "note": "💡 [معلومة]"}
    for o in result.get("observations", []):
        obs_html += f"<li style='margin-bottom: 10px;'><strong>{icons.get(o['type'], '📌')}</strong> {o['description']}</li>"

    if not obs_html:
        obs_html = "<li>لم يلاحظ النظام أي مشاكل ظاهرة على المنتج.</li>"

    return f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"></head>
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7f6; margin: 0; padding: 20px;">
        <div style="max-width: 600px; margin: auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
            <div style="background-color: #2c3e50; color: #ffffff; padding: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">🔍 التقرير الآلي لفحص المنتج</h2>
            </div>
            <div style="padding: 30px;">
                <div style="background-color: {status_color}; color: white; padding: 10px 15px; border-radius: 6px; font-weight: bold; margin-bottom: 20px; text-align: center;">
                    {status_text}
                </div>
                <h3 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 8px;">الخلاصة:</h3>
                <p style="color: #34495e; line-height: 1.6; font-size: 16px;">{result.get('summary_for_user', 'لا توجد خلاصة متاحة.')}</p>
                <h3 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 8px; margin-top: 25px;">التفاصيل والملاحظات:</h3>
                <ul style="color: #34495e; line-height: 1.6; font-size: 15px; padding-right: 20px;">{obs_html}</ul>
            </div>
        </div>
    </body>
    </html>
    """

# -------------------------------------------------------------
# إرسال الرد عبر Resend API (تم إصلاح الرابط الخاطئ هنا)
# -------------------------------------------------------------
def send_reply(to_address: str, subject: str, html_body: str):
    resp = requests.post(
        "https://api.resend.com/emails",
        json={
            "from": f"AI Product Inspector <{FROM_ADDRESS}>",
            "to": [to_address],
            "subject": f"تقرير فحص منتجك: Re: {subject}",
            "html": html_body,
        },
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    log.info("Reply sent successfully to %s", to_address)

# -------------------------------------------------------------
# نقطة استقبال الإشعار من السحاب (Webhook)
# -------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        event = request.get_json(force=True, silent=True) or {}
        data = event.get("data", event)

        # Resend's webhook payload nests the real email id under "email_id"
        email_id = data.get("email_id") or data.get("id")

        raw_from = data.get("from", "")
        if isinstance(raw_from, list) and raw_from:
            raw_from = raw_from[0]
        _, sender = email.utils.parseaddr(str(raw_from))
        if not sender:
            sender = str(raw_from)

        subject = data.get("subject", "")
        attachments_meta = data.get("attachments", [])

        image_bytes = None
        caption = ""

        if email_id and RESEND_API_KEY:
            headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}

            # Step 1: fetch the email body (webhooks only carry metadata, not content)
            body_resp = requests.get(
                f"https://api.resend.com/emails/receiving/{email_id}",
                headers=headers, timeout=15,
            )
            if body_resp.status_code == 200:
                body_data = body_resp.json()
                caption = body_data.get("text") or body_data.get("html") or ""
            else:
                log.warning("Could not fetch email body: %s %s", body_resp.status_code, body_resp.text)

            # Step 2: for each image attachment, get its signed download_url, then fetch the bytes
            for att in attachments_meta:
                att_id = att.get("id")
                content_type = att.get("content_type", "")
                if not att_id or not content_type.startswith("image/"):
                    continue

                att_resp = requests.get(
                    f"https://api.resend.com/emails/receiving/{email_id}/attachments/{att_id}",
                    headers=headers, timeout=15,
                )
                if att_resp.status_code != 200:
                    log.warning("Could not fetch attachment metadata: %s %s", att_resp.status_code, att_resp.text)
                    continue

                download_url = att_resp.json().get("download_url")
                if not download_url:
                    continue

                file_resp = requests.get(download_url, timeout=20)
                if file_resp.status_code == 200:
                    image_bytes = file_resp.content
                    break
                else:
                    log.warning("Could not download attachment file: %s", file_resp.status_code)

        if image_bytes and sender:
            log.info("Processing email from: %s subject: %s", sender, subject)
            result = analyze_image(image_bytes, caption, subject)
            report_html = format_report_html(result)
            send_reply(sender, subject, report_html)
            return jsonify({"status": "success", "message": "Analyzed and replied"}), 200

        log.info("No valid image attachment retrieved for email_id=%s", email_id)
        return jsonify({"status": "ignored", "message": "No valid image found"}), 200

    except Exception as e:
        log.exception("Webhook processing error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return "AI Inspector Bot is running 24/7!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
