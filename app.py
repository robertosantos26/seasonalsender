from flask import Flask, render_template, request, jsonify
import os, smtplib, schedule, time, threading, requests, json, urllib.parse, zipfile, io
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from pymongo import MongoClient, UpdateOne

app = Flask(__name__)

# ── MongoDB ───────────────────────────────────────────────
MONGO_URI = os.environ.get("MONGO_URI", "")
_db = None

def get_db():
    global _db
    if _db is None and MONGO_URI:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = client["seasonalsender"]
    return _db

def col_jobs():   return get_db()["jobs"]   if get_db() is not None else None
def col_config(): return get_db()["config"] if get_db() is not None else None
def col_logs():   return get_db()["logs"]   if get_db() is not None else None

# ── Config ────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "name": "", "email": "", "email_password": "",
    "smtp_server": "smtp.gmail.com", "smtp_port": 587, "phone": "",
    "cv_agricultural": "curriculo_agricola.pdf",
    "cv_non_agricultural": "curriculo_geral.pdf",
    "email_subject": "Application for {job_title} - {your_name}",
    "email_body": (
        "Dear Hiring Manager,\n\n"
        "I am writing to apply for the position of {job_title} at {company}.\n\n"
        "I am a motivated and hardworking individual available to start immediately. "
        "Please find my CV attached for your consideration.\n\n"
        "Best regards,\n{your_name}\n{your_phone}"
    ),
    "send_time": "08:00", "keywords": "", "job_type": "all",
}

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    try:
        c = col_config()
        if c is not None:
            doc = c.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                cfg.update(doc)
    except: pass
    return cfg

def save_config(data):
    try:
        c = col_config()
        if c is None: return False, "MongoDB não conectado"
        doc = dict(data); doc["_id"] = "main"
        c.replace_one({"_id": "main"}, doc, upsert=True)
        return True, "Salvo!"
    except Exception as e:
        return False, str(e)

# ── Logs ─────────────────────────────────────────────────
def add_log(text, type_="found"):
    try:
        c = col_logs()
        if c:
            c.insert_one({"type": type_, "text": text, "time": datetime.now().isoformat()})
            if c.count_documents({}) > 300:
                oldest = list(c.find().sort("time",1).limit(100))
                c.delete_many({"_id": {"$in": [d["_id"] for d in oldest]}})
    except: pass

# ── DOL JSON Feed ─────────────────────────────────────────
# URLs dos feeds JSON oficiais — data no formato YYYY-MM-DD
# O DOL atualiza diariamente à meia-noite EST
# Tentamos hoje e os últimos 5 dias até achar um ZIP válido

def make_feed_urls(date_str):
    base = "https://api.seasonaljobs.dol.gov/datahub-search/sjCaseData/zip"
    return {
        "h2a": f"{base}/h2a/{date_str}",   # agrícola
        "h2b": f"{base}/h2b/{date_str}",   # não-agrícola
        "jo":  f"{base}/jo/{date_str}",    # job orders
    }

def download_and_parse_feed(url):
    """Baixa ZIP do DOL e extrai JSON de dentro."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        json_files = [n for n in names if n.endswith(".json")]
        if not json_files:
            return None, f"Nenhum JSON no ZIP (arquivos: {names})"
        data = json.loads(zf.read(json_files[0]))
        return data, None
    except Exception as e:
        return None, str(e)

def find_latest_feed(feed_type):
    """Tenta os últimos 7 dias até achar um feed válido."""
    for days_ago in range(0, 7):
        date_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        urls = make_feed_urls(date_str)
        url = urls.get(feed_type)
        data, err = download_and_parse_feed(url)
        if data is not None:
            add_log(f"Feed {feed_type} encontrado: {date_str} ({len(data) if isinstance(data,list) else '?'} registros)", "found")
            return data, date_str
    return None, None

def parse_h2a_job(record, date_str):
    """Converte registro H-2A em formato padrão."""
    try:
        # Campos principais do feed H-2A
        title       = record.get("JOB_TITLE") or record.get("OCCUPATION_TITLE") or "Farm Worker"
        company     = record.get("EMPLOYER_NAME") or record.get("TRADE_NAME_DBA") or ""
        city        = record.get("EMPLOYER_CITY") or record.get("WORKSITE_CITY") or ""
        state       = record.get("EMPLOYER_STATE") or record.get("WORKSITE_STATE") or ""
        location    = f"{city}, {state}".strip(", ")
        email       = record.get("EMPLOYER_EMAIL") or record.get("AGENT_ATTORNEY_EMAIL") or ""
        phone       = record.get("EMPLOYER_PHONE") or ""
        wage        = record.get("WAGE_RATE_OF_PAY_FROM") or record.get("PREVAILING_WAGE") or ""
        wage_unit   = record.get("WAGE_UNIT_OF_PAY") or "hour"
        salary      = f"${wage}/{wage_unit}" if wage else ""
        case_num    = record.get("CASE_NUMBER") or record.get("CASE_NO") or ""
        desc_parts  = [
            record.get("JOB_DESCRIPTION",""),
            record.get("DUTIES",""),
            record.get("SPECIFIC_REQUIREMENTS",""),
        ]
        description = " ".join(p for p in desc_parts if p)[:600]
        url         = f"https://seasonaljobs.dol.gov/jobs/{case_num}" if case_num else ""

        try:
            raw_date = record.get("CASE_RECEIVED_DATE") or record.get("BEGIN_DATE") or date_str
            dt = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            date_fmt  = dt.strftime("%d/%m/%Y")
            timestamp = int(dt.timestamp())
        except:
            date_fmt  = datetime.now().strftime("%d/%m/%Y")
            timestamp = int(datetime.now().timestamp())

        return {
            "id":           f"dol_{case_num}" if case_num else f"dol_h2a_{abs(hash(title+company))}",
            "title":        title,
            "company":      company,
            "location":     location,
            "salary":       salary,
            "date":         date_fmt,
            "timestamp":    timestamp,
            "contactEmail": email.lower().strip(),
            "contactPhone": phone,
            "description":  description,
            "url":          url,
            "status":       "pending",
            "isNew":        True,
            "agri":         True,
            "source":       "dol-h2a",
        }
    except:
        return None

def parse_h2b_job(record, date_str):
    """Converte registro H-2B em formato padrão."""
    try:
        title    = record.get("JOB_TITLE") or record.get("OCCUPATION_TITLE") or "Seasonal Worker"
        company  = record.get("EMPLOYER_NAME") or record.get("TRADE_NAME_DBA") or ""
        city     = record.get("EMPLOYER_CITY") or record.get("WORKSITE_CITY") or ""
        state    = record.get("EMPLOYER_STATE") or record.get("WORKSITE_STATE") or ""
        location = f"{city}, {state}".strip(", ")
        email    = record.get("EMPLOYER_EMAIL") or record.get("AGENT_ATTORNEY_EMAIL") or ""
        phone    = record.get("EMPLOYER_PHONE") or ""
        wage     = record.get("WAGE_RATE_OF_PAY_FROM") or record.get("PREVAILING_WAGE") or ""
        wage_unit= record.get("WAGE_UNIT_OF_PAY") or "hour"
        salary   = f"${wage}/{wage_unit}" if wage else ""
        case_num = record.get("CASE_NUMBER") or record.get("CASE_NO") or ""
        description = (record.get("JOB_DESCRIPTION","") or record.get("DUTIES",""))[:600]
        url      = f"https://seasonaljobs.dol.gov/jobs/{case_num}" if case_num else ""

        try:
            raw_date = record.get("CASE_RECEIVED_DATE") or record.get("BEGIN_DATE") or date_str
            dt = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            date_fmt  = dt.strftime("%d/%m/%Y")
            timestamp = int(dt.timestamp())
        except:
            date_fmt  = datetime.now().strftime("%d/%m/%Y")
            timestamp = int(datetime.now().timestamp())

        return {
            "id":           f"dol_{case_num}" if case_num else f"dol_h2b_{abs(hash(title+company))}",
            "title":        title,
            "company":      company,
            "location":     location,
            "salary":       salary,
            "date":         date_fmt,
            "timestamp":    timestamp,
            "contactEmail": email.lower().strip(),
            "contactPhone": phone,
            "description":  description,
            "url":          url,
            "status":       "pending",
            "isNew":        True,
            "agri":         False,
            "source":       "dol-h2b",
        }
    except:
        return None

def fetch_all_jobs(keywords="", job_type="all"):
    jobs = []
    errors = []

    # Baixa H-2A (agrícola)
    if job_type in ("all", "agricultural"):
        data, date_str = find_latest_feed("h2a")
        if data and isinstance(data, list):
            for r in data:
                j = parse_h2a_job(r, date_str)
                if j: jobs.append(j)
        elif data is None:
            errors.append("Não foi possível baixar feed H-2A")

    # Baixa H-2B (não-agrícola)
    if job_type in ("all", "non-agricultural"):
        data, date_str = find_latest_feed("h2b")
        if data and isinstance(data, list):
            for r in data:
                j = parse_h2b_job(r, date_str)
                if j: jobs.append(j)
        elif data is None:
            errors.append("Não foi possível baixar feed H-2B")

    # Filtro por palavra-chave
    if keywords:
        kw_list = [k.strip().lower() for k in keywords.split(',') if k.strip()]
        if kw_list:
            jobs = [j for j in jobs if any(
                k in (j['title']+' '+j['description']+' '+j['company']).lower()
                for k in kw_list
            )]

    add_log(f"Total parseado: {len(jobs)} vagas ({job_type})", "found")
    return jobs, ("; ".join(errors) if errors and not jobs else None)

def upsert_jobs(jobs):
    try:
        c = col_jobs()
        if c is None or not jobs: return 0
        ops = [UpdateOne({"id": j["id"]}, {"$setOnInsert": j}, upsert=True) for j in jobs]
        result = c.bulk_write(ops, ordered=False)
        return result.upserted_count
    except Exception as e:
        add_log(f"Erro upsert: {e}", "error")
        return 0

def build_email_content(job, config):
    agri    = job.get('agri', False)
    cv      = config['cv_agricultural'] if agri else config['cv_non_agricultural']
    company = job.get('company') or 'the company'
    subj = (config['email_subject']
            .replace('{job_title}', job['title'])
            .replace('{company}',   company)
            .replace('{your_name}', config.get('name','')))
    body = (config['email_body']
            .replace('{job_title}', job['title'])
            .replace('{company}',   company)
            .replace('{your_name}', config.get('name',''))
            .replace('{your_phone}',config.get('phone','')))
    return subj, body, cv

def send_email_smtp(to_email, subject, body, cv_file, config):
    try:
        msg = MIMEMultipart()
        msg['From']    = config['email']
        msg['To']      = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        cv_path = os.path.join("curriculos", cv_file)
        if os.path.exists(cv_path):
            with open(cv_path, 'rb') as f:
                part = MIMEBase('application','octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{cv_file}"')
                msg.attach(part)
        srv = smtplib.SMTP(config['smtp_server'], int(config['smtp_port']))
        srv.starttls()
        srv.login(config['email'], config['email_password'])
        srv.sendmail(config['email'], to_email, msg.as_string())
        srv.quit()
        return True, "Enviado"
    except Exception as e:
        return False, str(e)

# ── ROTAS ────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    try:
        db = get_db()
        if db is None:
            return jsonify({"mongo": False, "message": "MONGO_URI não configurado"})
        db.command("ping")
        return jsonify({"mongo": True, "jobs": col_jobs().count_documents({})})
    except Exception as e:
        return jsonify({"mongo": False, "message": str(e)})

@app.route('/api/config', methods=['GET'])
def api_get_config():
    c = load_config()
    safe = {k: v for k, v in c.items() if k != 'email_password'}
    safe['has_password'] = bool(c.get('email_password'))
    return jsonify(safe)

@app.route('/api/config', methods=['POST'])
def api_save_config():
    try:
        data = request.json or {}
        current = load_config()
        for k, v in data.items():
            if k == 'email_password' and v in ('', '••••••••'): continue
            current[k] = v
        ok, msg = save_config(current)
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    try:
        c = col_jobs()
        if c is None: return jsonify([])
        jobs = list(c.find({}, {"_id": 0}).sort("timestamp", -1))
        return jsonify(jobs)
    except Exception as e:
        return jsonify([])

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    try:
        body     = request.json or {}
        keywords = body.get('keywords', '')
        job_type = body.get('job_type', 'all')
        jobs, error = fetch_all_jobs(keywords, job_type)
        if error and not jobs:
            return jsonify({"success": False, "error": error, "scraped": 0, "new": 0})
        added = upsert_jobs(jobs)
        return jsonify({"success": True, "scraped": len(jobs), "new": added})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "scraped": 0, "new": 0})

@app.route('/api/send/<job_id>', methods=['POST'])
def api_send(job_id):
    try:
        config = load_config()
        c      = col_jobs()
        job    = c.find_one({"id": job_id}, {"_id": 0}) if c else None
        if not job:
            return jsonify({"success": False, "message": "Vaga não encontrada"})
        subject, body, cv = build_email_content(job, config)
        to_email = job.get('contactEmail', '')
        if not to_email:
            return jsonify({"success": False, "no_email": True,
                            "message": "Sem email. Candidate-se pelo link.",
                            "url": job.get('url','')})
        if config.get('email_password'):
            success, msg = send_email_smtp(to_email, subject, body, cv, config)
            if success:
                c.update_one({"id": job_id}, {"$set": {"status": "sent"}})
                add_log(f"✓ Enviado: {job['title']} → {to_email}", "sent")
            else:
                add_log(f"✗ Erro: {job['title']} — {msg}", "error")
            return jsonify({"success": success, "message": msg})
        else:
            mailto = (f"mailto:{to_email}"
                      f"?subject={urllib.parse.quote(subject)}"
                      f"&body={urllib.parse.quote(body + chr(10)*2 + '[Anexar: ' + cv + ']')}")
            return jsonify({"success": True, "manual": True, "mailto": mailto,
                            "cv": cv, "subject": subject, "body": body, "to": to_email})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/send-all', methods=['POST'])
def api_send_all():
    try:
        config = load_config()
        c      = col_jobs()
        if not c: return jsonify({"sent": 0})
        pending = list(c.find({"status":"pending","contactEmail":{"$nin":["",None]}},{"_id":0}))
        sent = 0
        for job in pending:
            subject, body, cv = build_email_content(job, config)
            if config.get('email_password'):
                ok, _ = send_email_smtp(job['contactEmail'], subject, body, cv, config)
                if ok:
                    c.update_one({"id": job['id']}, {"$set": {"status": "sent"}})
                    add_log(f"✓ Auto: {job['title']}", "sent")
                    sent += 1
        return jsonify({"sent": sent})
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e)})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        c = col_logs()
        if c is None: return jsonify([])
        return jsonify(list(c.find({},{"_id":0}).sort("time",-1).limit(50)))
    except: return jsonify([])

@app.route('/api/stats', methods=['GET'])
def api_stats():
    try:
        c     = col_jobs()
        today = datetime.now().strftime('%d/%m/%Y')
        if c is None:
            return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                            "agricultural":0,"non_agricultural":0,"with_email":0})
        return jsonify({
            "total":            c.count_documents({}),
            "sent":             c.count_documents({"status":"sent"}),
            "pending":          c.count_documents({"status":"pending"}),
            "today":            c.count_documents({"date":today}),
            "agricultural":     c.count_documents({"agri":True}),
            "non_agricultural": c.count_documents({"agri":False}),
            "with_email":       c.count_documents({"contactEmail":{"$nin":["",None]}}),
        })
    except:
        return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                        "agricultural":0,"non_agricultural":0,"with_email":0})

def scheduler_loop():
    def daily():
        try:
            config = load_config()
            jobs, _ = fetch_all_jobs(config.get('keywords',''), config.get('job_type','all'))
            upsert_jobs(jobs)
            if config.get('email_password'):
                c = col_jobs()
                if c:
                    for job in list(c.find({"status":"pending","contactEmail":{"$nin":["",None]}},{"_id":0})):
                        s, b, cv = build_email_content(job, config)
                        ok, _ = send_email_smtp(job['contactEmail'], s, b, cv, config)
                        if ok:
                            c.update_one({"id":job['id']},{"$set":{"status":"sent"}})
                            add_log(f"Auto: {job['title']}", "sent")
        except Exception as e:
            add_log(f"Erro scheduler: {e}", "error")

    config = load_config()
    schedule.every().day.at(config.get('send_time','08:00')).do(daily)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    os.makedirs('curriculos', exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("\n🌿 SeasonalSender — http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)
