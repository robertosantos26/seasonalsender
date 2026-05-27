from flask import Flask, render_template, request, jsonify
import os, smtplib, schedule, time, threading, requests, json, urllib.parse, base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from bs4 import BeautifulSoup
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

app = Flask(__name__)

# ── Supabase / PostgreSQL ─────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None

def get_pool():
    global _pool
    if _pool is None and DATABASE_URL:
        # Usa DATABASE_URL exatamente como configurada no Render
        # Garante só o sslmode=require no final
        url = DATABASE_URL.strip()
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        _pool = pool.SimpleConnectionPool(
            1, 5, url,
            connect_timeout=10,
        )
    return _pool

def get_conn():
    p = get_pool()
    if p is None:
        raise Exception("DATABASE_URL não configurada")
    return p.getconn()

def release_conn(conn):
    p = get_pool()
    if p:
        p.putconn(conn)

def db_query(sql, params=None, fetch=None):
    """Executa query e retorna resultado. fetch='one','all' ou None para writes."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            conn.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        release_conn(conn)

def init_db():
    """Cria as tabelas se não existirem."""
    db_query("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            timestamp BIGINT DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    db_query("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL
        )
    """)
    db_query("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            type TEXT,
            text TEXT,
            time TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Índice para ordenação rápida
    db_query("""
        CREATE INDEX IF NOT EXISTS idx_jobs_timestamp ON jobs(timestamp DESC)
    """)

# ── Config ────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "name": "", "email": "", "email_password": "",
    "smtp_server": "smtp.gmail.com", "smtp_port": 587, "phone": "",
    "cv_agricultural": "curriculo_agricola.pdf",
    "cv_non_agricultural": "curriculo_geral.pdf",
    "cover_letter_agricultural": "cover_letter_agricola.pdf",
    "cover_letter_non_agricultural": "cover_letter_geral.pdf",
    "email_subject": "Application for {job_title} - {your_name}",
    "email_body": (
        "Dear Hiring Manager,\n\n"
        "I am writing to apply for the position of {job_title} at {company}.\n\n"
        "I am a motivated and hardworking individual available to start immediately. "
        "Please find my CV and cover letter attached for your consideration.\n\n"
        "Best regards,\n{your_name}\n{your_phone}"
    ),
    "send_time": "08:00", "keywords": "", "job_type": "all",
    "followup_subject": "Follow-up: Application for {job_title} - {your_name}",
    "followup_body": (
        "Dear Hiring Manager,\n\n"
        "I hope this message finds you well. I am writing to follow up on my recent "
        "application for the position of {job_title} at {company}.\n\n"
        "I remain very interested in this opportunity and would love to discuss how "
        "my experience and dedication could be a great fit for your team. "
        "I am available to start immediately and am happy to provide any additional "
        "information you may need.\n\n"
        "Thank you for your time and consideration. I look forward to hearing from you.\n\n"
        "Best regards,\n{your_name}\n{your_phone}"
    ),
}

def load_config():
    try:
        row = db_query("SELECT value FROM config WHERE key = 'main'", fetch="one")
        if row:
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(row["value"])
            return cfg
    except:
        pass
    return DEFAULT_CONFIG.copy()

def save_config(data):
    try:
        db_query("""
            INSERT INTO config (key, value) VALUES ('main', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(data),))
        return True
    except Exception as e:
        add_log(f"Erro ao salvar config: {e}", "error")
        return False

# ── Logs ─────────────────────────────────────────────────
def add_log(text, type_="found"):
    try:
        db_query("INSERT INTO logs (type, text, time) VALUES (%s, %s, %s)",
                 (type_, text, datetime.now().isoformat()))
        # Mantém só os últimos 200
        db_query("""
            DELETE FROM logs WHERE id NOT IN (
                SELECT id FROM logs ORDER BY id DESC LIMIT 200
            )
        """)
    except:
        pass

# ── RSS Feed ─────────────────────────────────────────────
import xml.etree.ElementTree as ET

RSS_URL = "https://seasonaljobs.dol.gov/job_rss.xml"

AGRI_KW = [
    'farm','harvest','pick','fruit','vegetable','crop','orchard','field',
    'agriculture','farming','grape','strawberry','apple','packing','farmworker',
    'horticulture','dairy','poultry','livestock','greenhouse','nursery',
    'tobacco','irrigation','tractor','equipment operator','ranch','melon',
    'blueberry','potato','corn','wheat','sugar beet','horse groom'
]

def is_agricultural(job):
    text = (job.get("title","") + " " + job.get("description","")).lower()
    return any(k in text for k in AGRI_KW)

def fetch_rss_jobs(keywords="", job_type="all"):
    jobs = []
    try:
        resp = requests.get(RSS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        items = channel.findall("item") if channel else []
        kw_list = [k.strip().lower() for k in keywords.split(",") if k.strip()] if keywords else []

        for item in items:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            if kw_list and not any(k in (title+" "+desc).lower() for k in kw_list):
                continue
            agri = is_agricultural({"title": title, "description": desc})
            if job_type == "agricultural" and not agri:
                continue
            if job_type == "non-agricultural" and agri:
                continue
            case_num = link.split("/")[-1] if "/" in link else link
            # Garante que o link aponta para a vaga no DOL, não para o próprio site
            if not link.startswith("http"):
                link = f"https://seasonaljobs.dol.gov/jobs/{case_num}"
            try:
                dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                date_fmt  = dt.strftime("%d/%m/%Y")
                timestamp = int(dt.timestamp())
            except:
                dt = datetime.now()
                date_fmt  = dt.strftime("%d/%m/%Y")
                timestamp = int(dt.timestamp())
            jobs.append({
                "id": f"dol_{case_num}", "title": title,
                "company": "", "location": "", "salary": "",
                "date": date_fmt, "timestamp": timestamp,
                "contactEmail": "", "contactPhone": "",
                "description": desc[:600], "url": link,
                "status": "pending", "isNew": True,
                "agri": agri, "source": "dol.gov",
            })
        add_log(f"RSS: {len(items)} vagas no feed, {len(jobs)} carregadas.", "found")
        return jobs, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        add_log(f"Erro RSS: {msg}", "error")
        return [], msg

def upsert_jobs(jobs):
    """Insere vagas novas, ignora duplicatas. Retorna quantas foram inseridas."""
    if not jobs:
        return 0
    added = 0
    for job in jobs:
        try:
            result = db_query("""
                INSERT INTO jobs (id, data, timestamp, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
            """, (job["id"], json.dumps(job), job.get("timestamp", 0), job.get("status","pending")),
            fetch="one")
            if result:
                added += 1
        except:
            pass
    return added

def fetch_job_detail(url):
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        detail = {}
        a = soup.find("a", href=lambda h: h and h.startswith("mailto:"))
        if a:
            detail["contactEmail"] = a["href"].replace("mailto:", "").strip()
        t = soup.find("a", href=lambda h: h and h.startswith("tel:"))
        if t:
            detail["contactPhone"] = t.get_text(strip=True)
        for line in soup.get_text(separator="\n").split("\n"):
            ln = line.strip()
            if "$" in ln and any(w in ln.lower() for w in ["hour","week","per","rate"]):
                detail["salary"] = ln[:80]
                break
        h1 = soup.find("h1") or soup.find("h2")
        if h1:
            company = location = ""
            for sib in list(h1.next_siblings)[:6]:
                t2 = sib.get_text(strip=True) if hasattr(sib,"get_text") else ""
                if t2 and not company:
                    company = t2
                elif t2 and not location and any(
                    s in t2 for s in [", CA",", TX",", FL",", WA",", OR",", NY",
                                      ", NC",", GA",", AZ",", CO",", ID",", MI",
                                      ", MN",", MO",", MT",", NE",", NV",", OH",
                                      ", PA",", VA",", WI"]):
                    location = t2
                    break
            if company:  detail["company"]  = company
            if location: detail["location"] = location
        return detail
    except:
        return {}

def build_email_content(job, config):
    agri   = job.get("agri", False)
    cv     = config["cv_agricultural"] if agri else config["cv_non_agricultural"]
    cl     = config.get("cover_letter_agricultural","") if agri else config.get("cover_letter_non_agricultural","")
    company = job.get("company") or "the company"
    subj = (config["email_subject"]
            .replace("{job_title}", job["title"])
            .replace("{company}",   company)
            .replace("{your_name}", config.get("name","")))
    body = (config["email_body"]
            .replace("{job_title}", job["title"])
            .replace("{company}",   company)
            .replace("{your_name}", config.get("name",""))
            .replace("{your_phone}",config.get("phone","")))
    return subj, body, cv, cl

def read_attachment(folder, filename):
    """Lê arquivo e retorna base64 para anexo."""
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return None

def send_email_smtp(to_email, subject, body, cv_file, config, cover_letter_file=""):
    """Envia email via SendGrid API (funciona no Render gratuito)."""
    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "")
    
    if not sendgrid_key:
        return False, "SENDGRID_API_KEY não configurada. Adicione no Render → Environment."

    sender_email = config["email"]
    sender_name  = config.get("name", "SeasonalSender")

    payload = {
        "personalizations": [{
            "to": [{"email": to_email}],
            "bcc": [{"email": sender_email}],  # cópia automática para o remetente
        }],
        "from":     {"email": sender_email, "name": sender_name},
        "reply_to": {"email": sender_email, "name": sender_name},
        "subject":  subject,
        "content":  [{"type": "text/plain", "value": body}],
    }

    # Adiciona anexos se existirem
    attachments = []
    for fname in [cv_file, cover_letter_file]:
        if fname:
            data = read_attachment("curriculos", fname)
            if data:
                attachments.append({
                    "content": data,
                    "filename": fname,
                    "type": "application/pdf",
                    "disposition": "attachment"
                })
    if attachments:
        payload["attachments"] = attachments

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {sendgrid_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15
        )
        if resp.status_code in (200, 202):
            return True, "Enviado"
        else:
            err = resp.json() if resp.content else {}
            errors = err.get("errors", [{}])
            msg = errors[0].get("message", f"Erro {resp.status_code}") if errors else f"Erro {resp.status_code}"
            return False, msg
    except Exception as e:
        return False, str(e)

# ── ROTAS ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    status = {}
    # Verifica banco
    if not DATABASE_URL:
        status["banco"] = "❌ DATABASE_URL não configurada"
    else:
        try:
            init_db()
            row = db_query("SELECT COUNT(*) as total FROM jobs", fetch="one")
            status["banco"] = f"✅ Supabase OK — {row['total'] if row else 0} vagas"
        except Exception as e:
            status["banco"] = f"❌ Erro banco: {str(e)[:80]}"
    # Verifica SendGrid
    sg_key = os.environ.get("SENDGRID_API_KEY","")
    if not sg_key:
        status["email"] = "❌ SENDGRID_API_KEY não configurada"
    else:
        status["email"] = "✅ SendGrid configurado"
    status["ok"] = "❌" not in str(status.values())
    return jsonify(status)

@app.route("/api/config", methods=["GET"])
def api_get_config():
    c = load_config()
    safe = {k: v for k, v in c.items() if k != "email_password"}
    safe["has_password"] = bool(c.get("email_password"))
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        data    = request.json or {}
        current = load_config()
        for k, v in data.items():
            if k == "email_password" and v in ("","••••••••"):
                continue
            current[k] = v
        ok = save_config(current)
        return jsonify({"success": ok, "message": "Salvo!" if ok else "Erro ao salvar"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/api/jobs", methods=["GET"])
def api_jobs():
    try:
        rows = db_query("SELECT data FROM jobs ORDER BY timestamp DESC", fetch="all")
        jobs = [row["data"] for row in rows] if rows else []
        return jsonify(jobs)
    except Exception as e:
        return jsonify([])

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    try:
        body     = request.json or {}
        keywords = body.get("keywords","")
        job_type = body.get("job_type","all")
        new_rss, error = fetch_rss_jobs(keywords, job_type)
        if error and not new_rss:
            return jsonify({"success": False, "error": error, "scraped": 0, "new": 0})
        added = upsert_jobs(new_rss)
        row = db_query("SELECT COUNT(*) as total FROM jobs", fetch="one")
        total = row["total"] if row else 0
        return jsonify({"success": True, "scraped": len(new_rss),
                        "new": added, "total": total})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "scraped": 0, "new": 0})

@app.route("/api/followup/<job_id>", methods=["POST"])
def api_followup(job_id):
    try:
        config = load_config()
        row    = db_query("SELECT data FROM jobs WHERE id = %s", (job_id,), fetch="one")
        if not row:
            return jsonify({"success": False, "message": "Vaga não encontrada"})
        job = row["data"]
        company = job.get("company") or "the company"
        subj = (config.get("followup_subject", "Follow-up: Application for {job_title} - {your_name}")
                .replace("{job_title}", job["title"])
                .replace("{company}",   company)
                .replace("{your_name}", config.get("name","")))
        body = (config.get("followup_body", "")
                .replace("{job_title}", job["title"])
                .replace("{company}",   company)
                .replace("{your_name}", config.get("name",""))
                .replace("{your_phone}",config.get("phone","")))
        to_email = job.get("contactEmail","")
        if not to_email:
            return jsonify({"success": False, "no_email": True,
                            "message": "Sem email de contato.",
                            "url": job.get("url","")})
        if config.get("email_password"):
            # Follow-up sem anexo
            success, msg = send_email_smtp(to_email, subj, body, "", config, "")
            if success:
                job["status"] = "followup"
                db_query("UPDATE jobs SET data = %s, status = 'followup' WHERE id = %s",
                         (json.dumps(job), job_id))
                add_log(f"↩ Follow-up: {job['title']} → {to_email}", "sent")
            else:
                add_log(f"✗ Erro follow-up: {job['title']} — {msg}", "error")
            return jsonify({"success": success, "message": msg})
        else:
            mailto = (f"mailto:{to_email}"
                      f"?subject={urllib.parse.quote(subj)}"
                      f"&body={urllib.parse.quote(body)}")
            return jsonify({"success": True, "manual": True, "mailto": mailto,
                            "subject": subj, "body": body, "to": to_email,
                            "type": "followup"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/enrich/<job_id>", methods=["POST"])
def api_enrich(job_id):
    try:
        row = db_query("SELECT data FROM jobs WHERE id = %s", (job_id,), fetch="one")
        if not row:
            return jsonify({"success": False, "message": "Vaga não encontrada"})
        job = row["data"]
        if not job.get("url"):
            return jsonify({"success": False, "message": "Sem URL"})
        detail = fetch_job_detail(job["url"])
        if detail:
            job.update({k: v for k, v in detail.items() if v})
            db_query("UPDATE jobs SET data = %s WHERE id = %s",
                     (json.dumps(job), job_id))
        return jsonify({"success": True, "job": job})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/api/send/<job_id>", methods=["POST"])
def api_send(job_id):
    try:
        config = load_config()
        row    = db_query("SELECT data FROM jobs WHERE id = %s", (job_id,), fetch="one")
        if not row:
            return jsonify({"success": False, "message": "Vaga não encontrada"})
        job = row["data"]
        subject, body, cv, cl = build_email_content(job, config)
        to_email = job.get("contactEmail","")
        if not to_email:
            return jsonify({"success": False, "no_email": True,
                            "message": "Sem email. Candidate-se pelo link.",
                            "url": job.get("url","")})
        if config.get("email_password"):
            success, msg = send_email_smtp(to_email, subject, body, cv, config, cl)
            if success:
                job["status"] = "sent"
                db_query("UPDATE jobs SET data = %s, status = 'sent' WHERE id = %s",
                         (json.dumps(job), job_id))
                add_log(f"✓ Enviado: {job['title']} → {to_email}", "sent")
            else:
                add_log(f"✗ Erro: {job['title']} — {msg}", "error")
            return jsonify({"success": success, "message": msg})
        else:
            anexos = cv + (f", {cl}" if cl else "")
            mailto = (f"mailto:{to_email}"
                      f"?subject={urllib.parse.quote(subject)}"
                      f"&body={urllib.parse.quote(body + chr(10)*2 + '[Anexar: ' + anexos + ']')}")
            return jsonify({"success": True, "manual": True, "mailto": mailto,
                            "cv": cv, "cl": cl, "subject": subject, "body": body, "to": to_email})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/api/send-all", methods=["POST"])
def api_send_all():
    try:
        config = load_config()
        rows   = db_query("""
            SELECT data FROM jobs
            WHERE status = 'pending'
            AND data->>'contactEmail' != ''
            AND data->>'contactEmail' IS NOT NULL
        """, fetch="all")
        pending = [r["data"] for r in rows] if rows else []
        sent = 0
        for job in pending:
            subject, body, cv, cl = build_email_content(job, config)
            if config.get("email_password"):
                ok, _ = send_email_smtp(job["contactEmail"], subject, body, cv, config, cl)
                if ok:
                    job["status"] = "sent"
                    db_query("UPDATE jobs SET data = %s, status = 'sent' WHERE id = %s",
                             (json.dumps(job), job["id"]))
                    add_log(f"✓ Auto: {job['title']}", "sent")
                    sent += 1
        return jsonify({"sent": sent})
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e)})

@app.route("/api/logs", methods=["GET"])
def api_logs():
    try:
        rows = db_query("""
            SELECT type, text, time FROM logs
            ORDER BY id DESC LIMIT 50
        """, fetch="all")
        return jsonify([dict(r) for r in rows] if rows else [])
    except:
        return jsonify([])

@app.route("/api/stats", methods=["GET"])
def api_stats():
    try:
        today = datetime.now().strftime("%d/%m/%Y")
        row = db_query("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'sent') as sent,
                COUNT(*) FILTER (WHERE status = 'pending') as pending,
                COUNT(*) FILTER (WHERE data->>'date' = %s) as today,
                COUNT(*) FILTER (WHERE (data->>'agri')::boolean = true) as agricultural,
                COUNT(*) FILTER (WHERE (data->>'agri')::boolean = false) as non_agricultural,
                COUNT(*) FILTER (WHERE data->>'contactEmail' != '' AND data->>'contactEmail' IS NOT NULL) as with_email
            FROM jobs
        """, (today,), fetch="one")
        return jsonify(dict(row)) if row else jsonify({
            "total":0,"sent":0,"pending":0,"today":0,
            "agricultural":0,"non_agricultural":0,"with_email":0})
    except Exception as e:
        return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                        "agricultural":0,"non_agricultural":0,"with_email":0})

def scheduler_loop():
    def daily():
        try:
            config = load_config()
            jobs, _ = fetch_rss_jobs(config.get("keywords",""), config.get("job_type","all"))
            added = upsert_jobs(jobs)
            if added:
                add_log(f"Scheduler: {added} novas vagas", "found")
            if config.get("email_password"):
                rows = db_query("""
                    SELECT data FROM jobs WHERE status='pending'
                    AND data->>'contactEmail' != ''
                """, fetch="all")
                for r in (rows or []):
                    job = r["data"]
                    s, b, cv, cl = build_email_content(job, config)
                    ok, _ = send_email_smtp(job["contactEmail"], s, b, cv, config, cl)
                    if ok:
                        db_query("UPDATE jobs SET status='sent', data=%s WHERE id=%s",
                                 (json.dumps({**job,"status":"sent"}), job["id"]))
                        add_log(f"Auto: {job['title']}", "sent")
        except Exception as e:
            add_log(f"Erro scheduler: {e}", "error")

    config = load_config()
    schedule.every().day.at(config.get("send_time","08:00")).do(daily)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Inicializa tabelas ao subir
try:
    init_db()
    print("✓ Banco de dados inicializado")
except Exception as e:
    print(f"⚠ Banco não disponível ainda: {e}")

if __name__ == "__main__":
    os.makedirs("curriculos", exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("\n🌿 SeasonalSender — http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)
