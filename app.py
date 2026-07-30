import os, json, base64, logging, sqlite3, hashlib, hmac, io
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, g
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ai-inspector")

OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LEMONSQUEEZY_SECRET = os.environ.get("LEMONSQUEEZY_SECRET", "")
DB_PATH             = os.environ.get("DB_PATH", "/tmp/inspector.db")
FREE_CREDITS        = 3

EXEMPT_EMAILS = {"akashiiso04@gmail.com", "muhannadd0594@gmail.com", "mohammdlghmd@gmail.com"}

PLAN_CREDITS = {
    "e810b85b-5273-4da2-9477-f3cf62f9737d": ("basic", 10),
    "db680fa5-9ec4-4fed-81fe-0ad4928266c3": ("pro",   50),
    "ceff30c8-9ba9-4c2a-bfb8-0cd520a9c072": ("vip",  120),
}

# ── DB ────────────────────────────────────────────────────────────────────────
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
    if db: db.close()

def get_or_create_user(email_addr):
    email_addr = email_addr.strip().lower()
    if email_addr in EXEMPT_EMAILS:
        return {"email": email_addr, "credits": 999, "plan": "admin"}
    db  = get_db()
    row = db.execute("SELECT * FROM users WHERE email=?", (email_addr,)).fetchone()
    if row: return dict(row)
    db.execute("INSERT INTO users (email,credits,plan,updated_at) VALUES (?,?,?,?)",
               (email_addr, FREE_CREDITS, "free", datetime.utcnow().isoformat()))
    db.commit()
    return {"email": email_addr, "credits": FREE_CREDITS, "plan": "free"}

def deduct_credit(email_addr):
    email_addr = email_addr.strip().lower()
    if email_addr in EXEMPT_EMAILS: return True
    db = get_db()
    if get_or_create_user(email_addr)["credits"] <= 0: return False
    db.execute("UPDATE users SET credits=credits-1,updated_at=? WHERE email=?",
               (datetime.utcnow().isoformat(), email_addr))
    db.commit()
    return True

def add_credits(email_addr, plan, amount):
    email_addr = email_addr.strip().lower()
    if email_addr in EXEMPT_EMAILS: return
    db = get_db()
    get_or_create_user(email_addr)
    db.execute("UPDATE users SET credits=credits+?,plan=?,updated_at=? WHERE email=?",
               (amount, plan, datetime.utcnow().isoformat(), email_addr))
    db.commit()

# ── Image ────────────────────────────────────────────────────────────────────
def compress_image(image_bytes, max_size=(900, 900)):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA","P"): img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except Exception as e:
        log.error("compress_image: %s", e)
        return image_bytes

# ── Prompt ───────────────────────────────────────────────────────────────────
def get_dynamic_prompt(subject, caption):
    combined = f"{subject} {caption}".lower()
    base = """أنت خبير متخصص في فحص جودة المنتجات وتوثيق حالتها بدقة عالية.
قم بتحليل صورة المنتج والوصف المقدم بعناية شديدة، ثم أصدر تقرير فحص شاملاً واحترافياً.
يجب أن ترجع إجابتك ككائن JSON خام فقط بدون أي markdown أو ```json:
{
  "image_quality": "good|poor|unusable",
  "quality_note": "سبب عدم وضوح الصورة بالعربية فقط، أو اتركه فارغاً",
  "overall_score": 75,
  "verdict_title": "عنوان موجز يصف الحالة العامة للمنتج",
  "verdict_status": "success|warning|danger",
  "metrics": [
    {"name": "النظافة وخلو السطح من العيوب الظاهرة", "score": 70},
    {"name": "سلامة الهيكل والتشطيبات", "score": 80},
    {"name": "مطابقة الحالة لوصف البائع", "score": 75},
    {"name": "مستوى الاستخدام والتآكل", "score": 65}
  ],
  "observations": [
    {"type": "damage|discrepancy|note", "title": "عنوان قصير للملاحظة", "description": "شرح تفصيلي دقيق بالعربية"}
  ],
  "pros": ["ميزة إيجابية 1", "ميزة إيجابية 2"],
  "cons": ["نقطة سلبية 1", "نقطة سلبية 2"],
  "summary_for_user": "توصية نهائية شاملة من 2-3 جمل توضح هل المنتج يستحق الشراء أم لا وبأي سعر."
}
قواعد صارمة:
1. جميع النصوص داخل JSON بالعربية حصراً.
2. التقييمات واقعية ودقيقة (ليست كلها 90+).
3. قدم على الأقل 2-3 ملاحظات حتى لو المنتج جيد (نقاط إيجابية كـ type: note).
4. إذا كان overall_score أقل من 60، يكون verdict_status=danger؛ بين 60-79=warning؛ 80+=success."""

    cats = {
        ("جوال","ايفون","لابتوب","شاشة","ايباد","phone","electronics"): "\n\nالتركيز (إلكترونيات): الشاشة والخدوش والزوايا والكاميرا والزجاج الخلفي وحالة المنافذ.",
        ("ساعة","ماركة","شنطة","نظارة","محفظة","watch","bag","luxury"):   "\n\nالتركيز (ماركات وأكسسوارات): دقة الشعار والخياطة والنقوش وتآكل الجلد أو المعدن.",
        ("سيارة","سيارات","قطع","صدام","جنط","car","auto"):              "\n\nالتركيز (سيارات): الصدأ والشقوق وإعادة الطلاء وفروق الألوان والدبلات والبدلات.",
        ("ملابس","ثوب","قميص","فستان","حذاء","clothes","fashion"):       "\n\nالتركيز (ملابس): حالة القماش والبقع والخيوط المنفصلة والتمزقات والتلوين.",
    }
    cat = "\n\nالتركيز (عام): فحص شامل للحالة العامة والجودة الظاهرة."
    for keywords, focus in cats.items():
        if any(w in combined for w in keywords):
            cat = focus
            break

    user = f'\n\nوصف البائع:\n"{caption}"\nقارن الوصف مع ما تراه في الصورة.' if caption else ""
    return base + cat + user

# ── AI ───────────────────────────────────────────────────────────────────────
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

def analyze_image(image_bytes, caption, subject):
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY غير محدد في متغيرات البيئة")

    compressed = compress_image(image_bytes)
    b64        = base64.b64encode(compressed).decode()
    prompt     = get_dynamic_prompt(subject, caption)

    payload = {
        "model": "anthropic/claude-3.5-sonnet",
        "temperature": 0.2,
        "messages": [{"role": "user", "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
    }

    resp = requests.post(
        OPENROUTER_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":  "application/json",
        },
        timeout=50,
    )
    resp.raise_for_status()

    raw   = resp.json()["choices"][0]["message"]["content"]
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("JSON parse failed: %s", raw[:300])
        return {
            "image_quality": "unusable", "quality_note": "تعذر تحليل الاستجابة.",
            "overall_score": 50, "verdict_title": "خطأ تقني", "verdict_status": "warning",
            "metrics": [], "observations": [], "pros": [], "cons": [],
            "summary_for_user": "حدث خطأ أثناء المعالجة. أعد المحاولة.",
        }

# ── Report HTML ───────────────────────────────────────────────────────────────
def format_report_html(result):
    if result.get("image_quality") in ("poor","unusable"):
        note = result.get("quality_note","الصورة غير واضحة بما يكفي لإجراء فحص دقيق.")
        return f"""<div id="report-content" dir="rtl" style="background:#0f172a;border:1px solid #ef444455;border-radius:14px;padding:24px;color:#f8fafc;font-family:system-ui,sans-serif;text-align:right;">
<div style="background:rgba(239,68,68,.15);border:1px solid #ef4444;border-radius:10px;padding:16px;text-align:center;color:#fca5a5;font-weight:700;font-size:15px;">
⚠️ تعذر إجراء الفحص<br><span style="font-weight:400;font-size:13px;color:#94a3b8;margin-top:6px;display:block;">{note}</span>
</div></div>"""

    status = result.get("verdict_status","warning")
    themes = {
        "success": {"bg":"rgba(34,197,94,.12)","border":"#22c55e","text":"#4ade80","bar":"#22c55e"},
        "warning": {"bg":"rgba(234,179,8,.12)", "border":"#eab308","text":"#fde047","bar":"#eab308"},
        "danger":  {"bg":"rgba(239,68,68,.12)", "border":"#ef4444","text":"#fca5a5","bar":"#ef4444"},
    }
    t = themes.get(status, themes["warning"])
    score = min(max(int(result.get("overall_score",70)),0),100)

    # Score ring color
    ring_color = t["bar"]

    # Metrics bars
    metrics_html = ""
    for m in result.get("metrics",[]):
        s = min(max(int(m.get("score",50)),0),100)
        bar_c = "#22c55e" if s>=80 else "#eab308" if s>=60 else "#ef4444"
        metrics_html += f"""<div style="margin-bottom:14px;">
<div style="display:flex;justify-content:space-between;font-size:13px;color:#cbd5e1;margin-bottom:5px;">
<span>{m.get('name','')}</span><span style="font-weight:700;color:#f8fafc;">{s}%</span></div>
<div style="background:#1e293b;height:7px;border-radius:4px;overflow:hidden;">
<div style="background:{bar_c};width:{s}%;height:100%;border-radius:4px;transition:width .4s;"></div></div></div>"""

    # Observations
    icons = {"damage":("❌","#ef4444"),"discrepancy":("⚠️","#f59e0b"),"note":("💡","#3b82f6")}
    obs_html = ""
    for o in result.get("observations",[]):
        ic, oc = icons.get(o.get("type","note"),("📌","#94a3b8"))
        obs_html += f"""<div style="background:#1e293b;border-right:4px solid {oc};padding:12px 14px;border-radius:4px 10px 10px 4px;margin-bottom:10px;">
<div style="font-size:14px;font-weight:700;color:#f8fafc;margin-bottom:3px;">{ic} {o.get('title','ملاحظة')}</div>
<div style="font-size:13px;color:#94a3b8;line-height:1.5;">{o.get('description','')}</div></div>"""
    if not obs_html:
        obs_html = '<div style="color:#64748b;font-size:13px;text-align:center;padding:10px;">لم يتم رصد عيوب ظاهرة</div>'

    # Pros / Cons
    def list_items(items, color, icon):
        if not items: return ""
        return "".join(f'<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:7px;"><span style="color:{color};font-size:13px;margin-top:1px;">{icon}</span><span style="font-size:13px;color:#cbd5e1;line-height:1.4;">{i}</span></div>' for i in items)

    pros_html = list_items(result.get("pros",[]), "#22c55e", "✓")
    cons_html = list_items(result.get("cons",[]), "#ef4444", "✗")

    pros_cons = ""
    if pros_html or cons_html:
        pros_col = f"""<div style="flex:1;background:#0d2a1a;border:1px solid #166534;border-radius:10px;padding:14px;">
<div style="font-size:12px;font-weight:700;color:#4ade80;margin-bottom:10px;letter-spacing:.5px;">✅ النقاط الإيجابية</div>{pros_html}</div>""" if pros_html else ""
        cons_col = f"""<div style="flex:1;background:#2a0d0d;border:1px solid #991b1b;border-radius:10px;padding:14px;">
<div style="font-size:12px;font-weight:700;color:#fca5a5;margin-bottom:10px;letter-spacing:.5px;">❌ النقاط السلبية</div>{cons_html}</div>""" if cons_html else ""
        pros_cons = f'<div style="display:flex;gap:12px;margin-bottom:20px;">{pros_col}{cons_col}</div>'

    now = datetime.utcnow().strftime("%Y/%m/%d %H:%M UTC")

    return f"""<div id="report-content" dir="rtl" style="background:#0f172a;border:1px solid #1e293b;border-radius:18px;padding:28px;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;text-align:right;box-shadow:0 20px 40px rgba(0,0,0,.4);">

<!-- Header -->
<div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1e293b;padding-bottom:20px;margin-bottom:24px;flex-wrap:wrap;gap:12px;">
<div>
<div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">🔍 تقرير الفحص الذكي للمنتج</div>
<div style="font-size:20px;font-weight:800;color:{t['text']};line-height:1.2;">{result.get('verdict_title','نتيجة الفحص')}</div>
<div style="font-size:11px;color:#475569;margin-top:4px;">{now}</div>
</div>
<div style="background:{t['bg']};border:2px solid {t['border']};border-radius:16px;padding:14px 20px;text-align:center;min-width:90px;">
<div style="font-size:32px;font-weight:900;color:{t['text']};line-height:1;">{score}</div>
<div style="font-size:10px;color:#94a3b8;margin-top:2px;">/ 100 تقييم</div>
</div>
</div>

<!-- Metrics -->
<div style="background:#182234;border:1px solid #1e293b;border-radius:12px;padding:18px;margin-bottom:20px;">
<div style="font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:14px;display:flex;align-items:center;gap:6px;">📊 مؤشرات الجودة التفصيلية</div>
{metrics_html}
</div>

<!-- Observations -->
<div style="margin-bottom:20px;">
<div style="font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:12px;display:flex;align-items:center;gap:6px;">🔎 الملاحظات المرصودة</div>
{obs_html}
</div>

<!-- Pros/Cons -->
{pros_cons}

<!-- Summary -->
<div style="background:{t['bg']};border:1px dashed {t['border']};border-radius:12px;padding:16px;margin-bottom:16px;">
<div style="font-size:12px;font-weight:700;color:{t['text']};margin-bottom:8px;letter-spacing:.5px;">💡 التوصية النهائية</div>
<div style="font-size:14px;color:#e2e8f0;line-height:1.6;">{result.get('summary_for_user','')}</div>
</div>

<!-- Footer -->
<div style="font-size:10px;color:#334155;text-align:center;border-top:1px solid #1e293b;padding-top:12px;">
هذا التقرير صادر عن نظام AI Inspector — تحليل استرشادي بناءً على معالجة الصورة بالذكاء الاصطناعي — غير ملزم قانونياً
</div>
</div>"""

# ── HTML Page (embedded — zero template folder issues) ────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Inspector – فحص المنتجات بالذكاء الاصطناعي</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://assets.lemonsqueezy.com/lemon.js" defer></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap" rel="stylesheet">
<style>
  body { font-family:'Tajawal',sans-serif; }
  @media print { body * { visibility:hidden; } #report-content, #report-content * { visibility:visible; } #report-content { position:fixed;top:0;left:0;width:100%; } }
</style>
</head>
<body class="bg-slate-950 text-slate-100 antialiased">

<!-- Navbar -->
<nav class="border-b border-slate-800 bg-slate-900/70 backdrop-blur-md fixed top-0 w-full z-50">
  <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-500 to-teal-400 flex items-center justify-center font-bold text-slate-950 text-xl shadow-lg shadow-emerald-500/20">AI</div>
      <span class="font-extrabold text-xl bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">AI Inspector</span>
    </div>
    <a href="#pricing" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-5 py-2 rounded-lg text-sm transition-all shadow-lg shadow-emerald-500/20">الباقات والأسعار</a>
  </div>
</nav>

<!-- Hero -->
<section class="pt-32 pb-16 px-4 max-w-6xl mx-auto text-center relative overflow-hidden">
  <div class="absolute inset-0 -z-10 flex items-center justify-center">
    <div class="w-[700px] h-[700px] bg-emerald-500/8 rounded-full blur-3xl"></div>
  </div>
  <span class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-semibold mb-6">
    🚀 مدعوم بـ Claude 3.5 Sonnet — أدق نموذج بصري في العالم
  </span>
  <h1 class="text-4xl sm:text-5xl font-extrabold text-white leading-tight max-w-3xl mx-auto mb-5">
    اكتشف أدق العيوب قبل الشراء
    <span class="bg-gradient-to-r from-emerald-400 to-teal-300 bg-clip-text text-transparent block mt-1">بتقرير احترافي في ثوانٍ</span>
  </h1>
  <p class="text-base text-slate-400 max-w-xl mx-auto mb-10">ارفع صورة المنتج مع وصف قصير، وسيقوم نظامنا بتحليلها وإصدار تقرير فحص شامل فوراً على الشاشة.</p>

  <!-- Upload Form -->
  <div class="max-w-lg mx-auto bg-slate-900 border border-slate-800 rounded-2xl p-7 text-right shadow-2xl">
    <h3 class="text-lg font-bold text-white mb-5 flex items-center gap-2">🔍 افحص منتجك الآن</h3>

    <div class="mb-4">
      <label class="text-sm text-slate-400 block mb-1.5">بريدك الإلكتروني <span class="text-emerald-400">*</span></label>
      <input id="upload-email" type="email" placeholder="example@gmail.com"
        class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-emerald-500 transition-colors">
    </div>

    <div class="mb-4">
      <label class="text-sm text-slate-400 block mb-1.5">وصف المنتج <span class="text-slate-600">(اختياري — يزيد دقة التقرير)</span></label>
      <input id="upload-desc" type="text" placeholder="مثال: آيفون 13 أسود ١٢٨ جيجا بدون كسر"
        class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-emerald-500 transition-colors">
    </div>

    <div class="mb-5">
      <label class="text-sm text-slate-400 block mb-1.5">صورة المنتج <span class="text-emerald-400">*</span></label>
      <input id="upload-image" type="file" accept="image/*"
        class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 text-sm text-slate-300 file:ml-3 file:py-1.5 file:px-4 file:rounded-lg file:border-0 file:text-xs file:font-bold file:bg-emerald-500 file:text-slate-950 cursor-pointer hover:border-slate-600 transition-colors">
    </div>

    <button id="upload-btn" onclick="submitUpload()"
      class="w-full bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold py-3.5 rounded-xl text-sm transition-all shadow-lg shadow-emerald-500/20 flex items-center justify-center gap-2">
      <span id="btn-text">فحص الصورة الآن ←</span>
      <svg id="btn-spinner" class="hidden animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
      </svg>
    </button>

    <p id="credits-info" class="text-xs text-slate-500 mt-2.5 text-center h-4"></p>
    <div id="upload-error" class="hidden mt-3 p-3 bg-red-950/50 border border-red-800 text-red-400 text-sm rounded-xl"></div>
  </div>

  <!-- Result -->
  <div id="result-wrapper" class="hidden max-w-2xl mx-auto mt-8 text-right">
    <div class="flex items-center justify-between mb-4">
      <h3 class="text-base font-bold text-white">📋 نتيجة الفحص</h3>
      <button onclick="downloadPDF()" class="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 font-semibold px-4 py-2 rounded-lg text-xs transition-all">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
        تحميل PDF
      </button>
    </div>
    <div id="result-area"></div>
  </div>
</section>

<!-- How It Works -->
<section class="py-20 bg-slate-900/40 border-y border-slate-800">
  <div class="max-w-6xl mx-auto px-4">
    <div class="text-center mb-14">
      <h2 class="text-3xl font-bold text-white mb-3">كيف يعمل النظام؟</h2>
      <p class="text-slate-400 text-sm">3 خطوات بسيطة، تقرير شامل واحترافي</p>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-8">
      <div class="bg-slate-900 p-8 rounded-2xl border border-slate-800 hover:border-emerald-500/30 transition-colors">
        <div class="w-12 h-12 bg-emerald-500/10 text-emerald-400 rounded-xl flex items-center justify-center font-bold text-xl mb-6 border border-emerald-500/20">1</div>
        <h3 class="text-lg font-bold text-white mb-3">ارفع الصورة</h3>
        <p class="text-slate-400 text-sm leading-relaxed">أدخل بريدك وارفع صورة المنتج مباشرة من الموقع. كلما كانت الصورة أوضح، كان التقرير أدق.</p>
      </div>
      <div class="bg-slate-900 p-8 rounded-2xl border border-emerald-500/25 shadow-lg shadow-emerald-500/5">
        <div class="w-12 h-12 bg-emerald-500/10 text-emerald-400 rounded-xl flex items-center justify-center font-bold text-xl mb-6 border border-emerald-500/20">2</div>
        <h3 class="text-lg font-bold text-white mb-3">تحليل ذكي فوري</h3>
        <p class="text-slate-400 text-sm leading-relaxed">يمسح النظام الصورة بكسلاً بكسل بحثاً عن الخدوش والتلفيات وأي تعارض مع وصف البائع.</p>
      </div>
      <div class="bg-slate-900 p-8 rounded-2xl border border-slate-800 hover:border-emerald-500/30 transition-colors">
        <div class="w-12 h-12 bg-emerald-500/10 text-emerald-400 rounded-xl flex items-center justify-center font-bold text-xl mb-6 border border-emerald-500/20">3</div>
        <h3 class="text-lg font-bold text-white mb-3">تقرير احترافي</h3>
        <p class="text-slate-400 text-sm leading-relaxed">خلال ثوانٍ يظهر تقرير مفصل بالمؤشرات والملاحظات والتوصية النهائية، قابل للتحميل كـ PDF.</p>
      </div>
    </div>
  </div>
</section>

<!-- Pricing -->
<section id="pricing" class="py-20 max-w-6xl mx-auto px-4">
  <div class="text-center mb-14">
    <h2 class="text-3xl font-bold text-white mb-3">الباقات والأسعار</h2>
    <p class="text-slate-400 text-sm">استثمر مبلغاً بسيطاً لتتجنب خسارة الآلاف لاحقاً</p>
  </div>
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">

    <div class="bg-slate-900 rounded-2xl border border-slate-800 p-6 flex flex-col">
      <h3 class="text-base font-bold text-white mb-1">التجريبية</h3>
      <p class="text-xs text-slate-500 mb-4">اختبر مجاناً</p>
      <div class="text-3xl font-extrabold text-white mb-5">مجاناً</div>
      <ul class="space-y-2 text-sm text-slate-400 mb-7 flex-1">
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> 3 فحوصات تجريبية</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> تقرير فوري على الشاشة</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> تحميل PDF</li>
      </ul>
      <button onclick="document.getElementById('upload-email').scrollIntoView({behavior:'smooth'})"
        class="w-full bg-slate-800 hover:bg-slate-700 text-white font-semibold py-2.5 rounded-xl text-sm transition-colors">ابدأ مجاناً</button>
    </div>

    <div class="bg-slate-900 rounded-2xl border border-slate-800 p-6 flex flex-col">
      <h3 class="text-base font-bold text-white mb-1">الأساسية</h3>
      <p class="text-xs text-slate-500 mb-4">للمشتري العرضي</p>
      <div class="text-3xl font-extrabold text-white mb-1">$4.99</div>
      <p class="text-xs text-emerald-400 mb-5">رصيد دائم لا ينتهي</p>
      <ul class="space-y-2 text-sm text-slate-400 mb-7 flex-1">
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> 10 فحوصات عالية الدقة</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> تقرير تفصيلي + PDF</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> مقارنة وصف البائع</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> صالح مدى الحياة</li>
      </ul>
      <a href="https://ainspector.lemonsqueezy.com/checkout/buy/e810b85b-5273-4da2-9477-f3cf62f9737d"
        class="lemonsqueezy-button w-full bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold py-2.5 rounded-xl text-center text-sm transition-colors block">اشترِ الآن</a>
    </div>

    <div class="bg-slate-900 rounded-2xl border-2 border-emerald-500/40 p-6 flex flex-col relative shadow-xl shadow-emerald-500/5">
      <span class="absolute -top-3 right-5 bg-emerald-500 text-slate-950 text-[10px] font-bold px-3 py-1 rounded-full">⭐ الأكثر مبيعاً</span>
      <h3 class="text-base font-bold text-white mb-1">المتقدمة</h3>
      <p class="text-xs text-slate-500 mb-4">للمتردد على مواقع المستعمل</p>
      <div class="text-3xl font-extrabold text-white mb-1">$14.99</div>
      <p class="text-xs text-emerald-400 mb-5">رصيد دائم لا ينتهي</p>
      <ul class="space-y-2 text-sm text-slate-400 mb-7 flex-1">
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> 50 فحصاً مفصلاً</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> كشف العيوب المخفية</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> أولوية في المعالجة</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> صالح مدى الحياة</li>
      </ul>
      <a href="https://ainspector.lemonsqueezy.com/checkout/buy/db680fa5-9ec4-4fed-81fe-0ad4928266c3"
        class="lemonsqueezy-button w-full bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold py-2.5 rounded-xl text-center text-sm transition-colors block">اشترِ الآن</a>
    </div>

    <div class="bg-slate-900 rounded-2xl border border-slate-800 p-6 flex flex-col">
      <h3 class="text-base font-bold text-white mb-1">الأعمال VIP</h3>
      <p class="text-xs text-slate-500 mb-4">للمعارض ومكاتب الفحص</p>
      <div class="text-3xl font-extrabold text-white mb-1">$49.99<span class="text-sm font-normal text-slate-500">/شهر</span></div>
      <p class="text-xs text-slate-500 mb-5">تجديد شهري آلي</p>
      <ul class="space-y-2 text-sm text-slate-400 mb-7 flex-1">
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> 120 فحصاً شهرياً</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> معالجة VIP فائقة</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> تقارير موسعة</li>
        <li class="flex items-center gap-2"><span class="text-emerald-400">✓</span> دعم فني مباشر</li>
      </ul>
      <a href="https://ainspector.lemonsqueezy.com/checkout/buy/ceff30c8-9ba9-4c2a-bfb8-0cd520a9c072"
        class="lemonsqueezy-button w-full bg-slate-700 hover:bg-slate-600 text-white font-semibold py-2.5 rounded-xl text-center text-sm transition-colors block">اشترك الآن</a>
    </div>

  </div>
</section>

<!-- Disclaimer -->
<section class="max-w-6xl mx-auto px-4 mb-14">
  <div class="bg-amber-950/20 border border-amber-800/30 rounded-xl p-5 text-center">
    <p class="text-xs text-amber-600/80 leading-relaxed">
      ⚠️ تقارير AI Inspector أداة استشارية تعتمد على التحليل البصري الآلي للصور. لا تُعتبر فحصاً ميكانيكياً أو قانونياً، ولا نتحمل مسؤولية قرارات الشراء أو البيع.
    </p>
  </div>
</section>

<footer class="border-t border-slate-800 py-8 text-center text-slate-600 text-xs">
  © 2026 AI Inspector — جميع الحقوق محفوظة
</footer>

<script>
async function submitUpload() {
  const email   = document.getElementById('upload-email').value.trim();
  const desc    = document.getElementById('upload-desc').value.trim();
  const file    = document.getElementById('upload-image').files[0];
  const errEl   = document.getElementById('upload-error');
  const wrapper = document.getElementById('result-wrapper');
  const resEl   = document.getElementById('result-area');
  const btn     = document.getElementById('upload-btn');
  const spinner = document.getElementById('btn-spinner');
  const btnText = document.getElementById('btn-text');

  errEl.classList.add('hidden');
  wrapper.classList.add('hidden');

  if (!email) { showError('يرجى إدخال بريدك الإلكتروني'); return; }
  if (!file)  { showError('يرجى اختيار صورة للفحص'); return; }

  btn.disabled = true;
  spinner.classList.remove('hidden');
  btnText.textContent = 'جاري التحليل...';

  const form = new FormData();
  form.append('email', email);
  form.append('description', desc);
  form.append('image', file);

  try {
    const resp = await fetch('/upload', { method: 'POST', body: form });
    const data = await resp.json();

    if (resp.status === 402) {
      showError('نفد رصيدك. اختر باقة مناسبة للاستمرار.');
      setTimeout(() => document.getElementById('pricing').scrollIntoView({ behavior:'smooth' }), 800);
      return;
    }
    if (!resp.ok || data.error) { showError(data.error || 'حدث خطأ، حاول مرة أخرى'); return; }

    resEl.innerHTML  = data.report;
    wrapper.classList.remove('hidden');
    wrapper.scrollIntoView({ behavior:'smooth', block:'start' });

    if (data.credits !== undefined) {
      document.getElementById('credits-info').textContent =
        data.credits === 999 ? '👑 حساب مميز — فحوصات غير محدودة' : `الرصيد المتبقي: ${data.credits} فحص`;
    }
  } catch(e) {
    showError('خطأ في الاتصال. تحقق من الشبكة وأعد المحاولة.');
  } finally {
    btn.disabled = false;
    spinner.classList.add('hidden');
    btnText.textContent = 'فحص الصورة الآن ←';
  }
}

function showError(msg) {
  const el = document.getElementById('upload-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function downloadPDF() {
  const el = document.getElementById('report-content');
  if (!el) return;
  const opt = {
    margin:      [8, 8, 8, 8],
    filename:    'ai-inspector-report.pdf',
    image:       { type:'jpeg', quality:0.95 },
    html2canvas: { scale:2, backgroundColor:'#0f172a', useCORS:true },
    jsPDF:       { unit:'mm', format:'a4', orientation:'portrait' },
    pagebreak:   { mode:'avoid-all' }
  };
  html2pdf().set(opt).from(el).save();
}

document.addEventListener('DOMContentLoaded', () => {
  const emailInput = document.getElementById('upload-email');
  emailInput.addEventListener('blur', async function() {
    const email = this.value.trim();
    if (!email || !email.includes('@')) return;
    try {
      const r = await fetch('/credits?email=' + encodeURIComponent(email));
      const d = await r.json();
      if (d.credits !== undefined) {
        document.getElementById('credits-info').textContent =
          d.credits === 999 ? '👑 حساب مميز — فحوصات غير محدودة' :
          `رصيدك الحالي: ${d.credits} فحص (باقة: ${d.plan})`;
      }
    } catch(e) {}
  });
});
</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return render_template_string(HTML_PAGE)

@app.route("/credits", methods=["GET"])
def credits_check():
    email_addr = request.args.get("email","").strip().lower()
    if not email_addr: return jsonify({"error":"email required"}), 400
    user = get_or_create_user(email_addr)
    return jsonify({"credits": user["credits"], "plan": user["plan"]})

@app.route("/upload", methods=["POST"])
def direct_upload():
    email_addr  = request.form.get("email","").strip().lower()
    description = request.form.get("description","")
    image_file  = request.files.get("image")

    if not email_addr: return jsonify({"error":"البريد الإلكتروني مطلوب"}), 400
    if not image_file: return jsonify({"error":"لم يتم رفع أي صورة"}), 400
    if not deduct_credit(email_addr):
        return jsonify({"error":"نفد رصيدك","credits":0}), 402

    try:
        result = analyze_image(image_file.read(), description, description)
        user   = get_or_create_user(email_addr)
        return jsonify({"status":"success","report":format_report_html(result),"credits":user["credits"]})
    except Exception as e:
        log.exception("Upload error")
        return jsonify({"error": str(e)}), 500

@app.route("/lemonsqueezy/webhook", methods=["POST"])
def lemonsqueezy_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Signature","")
    if LEMONSQUEEZY_SECRET:
        expected = hmac.new(LEMONSQUEEZY_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return jsonify({"error":"invalid signature"}), 401
    try:
        payload    = request.get_json(force=True) or {}
        event_name = payload.get("meta",{}).get("event_name","")
        if event_name != "order_created": return jsonify({"status":"ignored"}), 200
        attrs          = payload.get("data",{}).get("attributes",{})
        customer_email = attrs.get("user_email","").strip().lower()
        variant_uuid   = ""
        for item in payload.get("included",[]):
            if item.get("type") == "order-items":
                variant_uuid = str(item.get("attributes",{}).get("variant_id",""))
                break
        plan_info = PLAN_CREDITS.get(variant_uuid)
        if not plan_info: return jsonify({"status":"unknown_plan","variant":variant_uuid}), 200
        plan_name, credits = plan_info
        add_credits(customer_email, plan_name, credits)
        log.info("Granted %d credits (%s) to %s", credits, plan_name, customer_email)
        return jsonify({"status":"success"}), 200
    except Exception as e:
        log.exception("LemonSqueezy error")
        return jsonify({"error":str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
