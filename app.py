from flask import Flask, render_template, request, jsonify
import os, smtplib, schedule, time, threading, requests, json, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

app = Flask(__name__)

# ── MongoDB ───────────────────────────────────────────────
MONGO_URI = os.environ.get("MONGO_URI", "")

_mongo_client = None
_db = None

def get_db():
    global _mongo_client, _db
    if _db is None and MONGO_URI:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = _mongo_client["seasonalsender"]
    return _db

def db_jobs():
    return get_db()["jobs"] if get_db() is not None else None

def db_config():
    return get_db()["config"] if get_db() is not None else None

def db_logs():
    return get_db()["logs"] if get_db() is not None else None

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
        col = db_config()
        if col is not None:
            doc = col.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                cfg.update(doc)
    except:
        pass
    return cfg

def save_config(data):
    try:
        col = db_config()
        if col is None:
            return False, "MongoDB não conectado"
        doc = {k: v for k, v in data.items()}
        doc["_id"] = "main"
        col.replace_one({"_id": "main"}, doc, upsert=True)
        return True, "Salvo!"
    except Exception as e:
        return False, str(e)

# ── Logs ─────────────────────────────────────────────────
def add_log(text, type_="found"):
    try:
        col = db_logs()
        if col is not None:
            col.insert_one({"type": type_, "text": text, "time": datetime.now().isoformat()})
            # Mantém só os últimos 200
            count = col.count_documents({})
            if count > 200:
                oldest = list(col.find().sort("time", 1).limit(count - 200))
                col.delete_many({"_id": {"$in": [d["_id"] for d in oldest]}})
    except:
        pass

# ── Jobs ─────────────────────────────────────────────────
def load_jobs():
    try:
        col = db_jobs()
        if col is None:
            return []
        jobs = list(col.find({}, {"_id": 0}))
        return jobs
    except:
        return []

def upsert_jobs(jobs):
    """Insere vagas novas, ignora as que já existem."""
    try:
        col = db_jobs()
        if col is None or not jobs:
            return 0
        ops = [UpdateOne({"id": j["id"]}, {"$setOnInsert": j}, upsert=True) for j in jobs]
        result = col.bulk_write(ops, ordered=False)
        return result.upserted_count
    except Exception as e:
        add_log(f"Erro upsert: {e}", "error")
        return 0

def update_job(job):
    try:
        col = db_jobs()
        if col is None:
            return
        col.update_one({"id": job["id"]}, {"$set": job})
    except:
        pass

# ── Scraping ─────────────────────────────────────────────
RSS_URL = "https://seasonaljobs.dol.gov/job_rss.xml"

AGRI_KW = [
    'farm','harvest','pick','fruit','vegetable','crop','orchard','field',
    'agriculture','farming','grape','strawberry','apple','packing','farmworker',
    'horticulture','dairy','poultry','livestock','greenhouse','nursery',
    'tobacco','irrigation','tractor','equipment operator','ranch','melon',
    'blueberry','potato','corn','wheat','sugar beet','horse groom'
]

def is_agricultural(job):
    text = (job.get('title','') + ' ' + job.get('description','')).lower()
    return any(k in text for k in AGRI_KW)

def fetch_rss_jobs(keywords="", job_type="all"):
    jobs = []
    try:
        resp = requests.get(RSS_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find('channel')
        items = channel.findall('item') if channel else []
        kw_list = [k.strip().lower() for k in keywords.split(',') if k.strip()] if keywords else []

        for item in items:
            title = (item.findtext('title') or '').strip()
            link  = (item.findtext('link')  or '').strip()
            desc  = (item.findtext('description') or '').strip()
            pub   = (item.findtext('pubDate') or '').strip()
            if not title:
                continue
            if kw_list and not any(k in (title+' '+desc).lower() for k in kw_list):
                continue
            agri = is_agricultural({"title": title, "description": desc})
            if job_type == 'agricultural' and not agri:
                continue
            if job_type == 'non-agricultural' and agri:
                continue
            case_num = link.split('/')[-1] if '/' in link else link
            try:
                dt = datetime.strptime(pub, '%a, %d %b %Y %H:%M:%S %Z')
                date_str = dt.strftime('%d/%m/%Y')
                timestamp = int(dt.timestamp())
            except:
                dt = datetime.now()
                date_str = dt.strftime('%d/%m/%Y')
                timestamp = int(dt.timestamp())
            jobs.append({
                "id": f"dol_{case_num}", "title": title, "company": "",
                "location": "", "salary": "", "date": date_str,
                "timestamp": timestamp, "contactEmail": "", "contactPhone": "",
                "description": desc[:600], "url": link,
                "status": "pending", "isNew": True, "agri": agri, "source": "dol.gov",
            })
        add_log(f"RSS: {len(items)} vagas no feed, {len(jobs)} carregadas.", "found")
        return jobs, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        add_log(f"Erro RSS: {msg}", "error")
        return [], msg

def fetch_job_detail(url):
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        contact_email = contact_phone = salary = location = company = ""
        a = soup.find('a', href=lambda h: h and h.startswith('mailto:'))
        if a:
            contact_email = a['href'].replace('mailto:','').strip()
        t = soup.find('a', href=lambda h: h and h.startswith('tel:'))
        if t:
            contact_phone = t.get_text(strip=True)
        for line in soup.get_text(separator='\n').split('\n'):
            ln = line.strip()
            if '$' in ln and any(w in ln.lower() for w in ['hour','week','per','rate']):
                salary = ln[:80]; break
        h1 = soup.find('h1') or soup.find('h2')
        if h1:
            for sib in list(h1.next_siblings)[:6]:
                t2 = sib.get_text(strip=True) if hasattr(sib,'get_text') else ''
                if t2 and not company:
                    company = t2
                elif t2 and not location and any(
                    s in t2 for s in [', CA',', TX',', FL',', WA',', OR',', NY',
                                      ', NC',', GA',', AZ',', CO',', ID',', MI',
                                      ', MN',', MO',', MT',', NE',', NV',', OH',
                                      ', PA',', VA',', WI']):
                    location = t2; break
        return {"contactEmail": contact_email, "contactPhone": contact_phone,
                "salary": salary, "company": company, "location": location}
    except:
        return {}

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
        return jsonify({"mongo": True, "jobs": db_jobs().count_documents({})})
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
            if k == 'email_password' and v in ('', '••••••••'):
                continue
            current[k] = v
        ok, msg = save_config(current)
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    jobs = load_jobs()
    jobs.sort(key=lambda j: j.get('timestamp', 0), reverse=True)
    return jsonify(jobs)

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    try:
        body     = request.json or {}
        keywords = body.get('keywords', '')
        job_type = body.get('job_type', 'all')
        new_rss, error = fetch_rss_jobs(keywords, job_type)
        if error and not new_rss:
            return jsonify({"success": False, "error": str(error), "scraped": 0, "new": 0})
        added = upsert_jobs(new_rss)
        return jsonify({"success": True, "scraped": len(new_rss), "new": added})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "scraped": 0, "new": 0})

@app.route('/api/enrich-all', methods=['POST'])
def api_enrich_all():
    try:
        limit = int((request.json or {}).get('limit', 50))
        col = db_jobs()
        if col is None:
            return jsonify({"success": False, "error": "MongoDB não conectado"})

        sem_email = list(col.find(
            {"contactEmail": "", "url": {"$ne": ""}},
            {"_id": 0}
        ).limit(limit))

        if not sem_email:
            return jsonify({"success": True, "enriched": 0, "remaining": 0})

        def enrich_one(job):
            detail = fetch_job_detail(job['url'])
            if any(v for v in detail.values()):
                job.update({k: v for k, v in detail.items() if v})
                col.update_one({"id": job["id"]}, {"$set": detail})
                return bool(detail.get('contactEmail'))
            return False

        enriched = 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(enrich_one, j): j for j in sem_email}
            for f in as_completed(futures, timeout=55):
                try:
                    if f.result():
                        enriched += 1
                except:
                    pass

        remaining = col.count_documents({"contactEmail": "", "url": {"$ne": ""}})
        return jsonify({"success": True, "enriched": enriched,
                        "total_processed": len(sem_email), "remaining": remaining})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "enriched": 0, "remaining": 0})

@app.route('/api/enrich/<job_id>', methods=['POST'])
def api_enrich(job_id):
    try:
        col = db_jobs()
        job = col.find_one({"id": job_id}, {"_id": 0}) if col else None
        if not job:
            return jsonify({"success": False})
        if job.get('url'):
            detail = fetch_job_detail(job['url'])
            job.update({k: v for k, v in detail.items() if v})
            if col:
                col.update_one({"id": job_id}, {"$set": detail})
        return jsonify({"success": True, "job": job})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/send/<job_id>', methods=['POST'])
def api_send(job_id):
    try:
        config = load_config()
        col    = db_jobs()
        job    = col.find_one({"id": job_id}, {"_id": 0}) if col else None
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
                col.update_one({"id": job_id}, {"$set": {"status": "sent"}})
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
        col    = db_jobs()
        if not col:
            return jsonify({"sent": 0, "error": "MongoDB não conectado"})
        pending = list(col.find({"status": "pending", "contactEmail": {"$ne": ""}}, {"_id": 0}))
        sent = 0
        for job in pending:
            subject, body, cv = build_email_content(job, config)
            if config.get('email_password'):
                ok, msg = send_email_smtp(job['contactEmail'], subject, body, cv, config)
                if ok:
                    col.update_one({"id": job['id']}, {"$set": {"status": "sent"}})
                    add_log(f"✓ Auto: {job['title']}", "sent")
                    sent += 1
        return jsonify({"sent": sent})
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e)})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        col = db_logs()
        if col is None:
            return jsonify([])
        logs = list(col.find({}, {"_id": 0}).sort("time", -1).limit(50))
        return jsonify(logs)
    except:
        return jsonify([])

@app.route('/api/stats', methods=['GET'])
def api_stats():
    try:
        col   = db_jobs()
        today = datetime.now().strftime('%d/%m/%Y')
        if col is None:
            return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                            "agricultural":0,"non_agricultural":0,"with_email":0})
        return jsonify({
            "total":            col.count_documents({}),
            "sent":             col.count_documents({"status": "sent"}),
            "pending":          col.count_documents({"status": "pending"}),
            "today":            col.count_documents({"date": today}),
            "agricultural":     col.count_documents({"agri": True}),
            "non_agricultural": col.count_documents({"agri": False}),
            "with_email":       col.count_documents({"contactEmail": {"$ne": ""}}),
        })
    except Exception as e:
        return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                        "agricultural":0,"non_agricultural":0,"with_email":0})

def scheduler_loop():
    def daily():
        try:
            config = load_config()
            jobs, _ = fetch_rss_jobs(config.get('keywords',''), config.get('job_type','all'))
            upsert_jobs(jobs)
            if config.get('email_password'):
                col = db_jobs()
                if col:
                    pending = list(col.find({"status":"pending","contactEmail":{"$ne":""}},{"_id":0}))
                    for job in pending:
                        s, b, cv = build_email_content(job, config)
                        ok, _ = send_email_smtp(job['contactEmail'], s, b, cv, config)
                        if ok:
                            col.update_one({"id": job['id']}, {"$set": {"status": "sent"}})
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
