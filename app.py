from flask import Flask, render_template, request, jsonify
import os, smtplib, schedule, time, threading, requests, traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

app = Flask(__name__)

DATA = {"jobs": [], "sent_ids": [], "logs": []}

RSS_URL  = "https://seasonaljobs.dol.gov/job_rss.xml"

AGRI_KW = [
    'farm','harvest','pick','fruit','vegetable','crop','orchard','field',
    'agriculture','farming','grape','strawberry','apple','packing','farmworker',
    'horticulture','dairy','poultry','livestock','greenhouse','nursery',
    'tobacco','irrigation','tractor','equipment operator','ranch','melon',
    'blueberry','potato','corn','wheat','sugar beet','horse groom'
]

def get_config():
    return {
        "name":               os.environ.get("SENDER_NAME", ""),
        "email":              os.environ.get("SENDER_EMAIL", ""),
        "email_password":     os.environ.get("SENDER_PASSWORD", ""),
        "smtp_server":        os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":          int(os.environ.get("SMTP_PORT", "587")),
        "phone":              os.environ.get("SENDER_PHONE", ""),
        "cv_agricultural":    os.environ.get("CV_AGRI", "curriculo_agricola.pdf"),
        "cv_non_agricultural":os.environ.get("CV_NON_AGRI", "curriculo_geral.pdf"),
        "email_subject":      os.environ.get("EMAIL_SUBJECT", "Application for {job_title} - {your_name}"),
        "email_body":         os.environ.get("EMAIL_BODY",
            "Dear Hiring Manager,\n\nI am writing to apply for the position of {job_title} at {company}.\n\n"
            "I am a motivated and hardworking individual available to start immediately. "
            "Please find my CV attached for your consideration.\n\nBest regards,\n{your_name}\n{your_phone}"),
        "send_time":          os.environ.get("SEND_TIME", "08:00"),
        "keywords":           os.environ.get("SEARCH_KEYWORDS", ""),
        "job_type":           os.environ.get("JOB_TYPE", "all"),
    }

def is_agricultural(job):
    text = (job.get('title','') + ' ' + job.get('description','')).lower()
    return any(k in text for k in AGRI_KW)

def add_log(text, type_="found"):
    DATA['logs'].insert(0, {"type": type_, "text": text, "time": datetime.now().isoformat()})
    if len(DATA['logs']) > 200:
        DATA['logs'] = DATA['logs'][:200]

def fetch_rss_jobs(keywords="", job_type="all"):
    """Lê RSS oficial do seasonaljobs.dol.gov"""
    jobs = []
    error_detail = ""
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; SeasonalSender/1.0)',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }
        resp = requests.get(RSS_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        
        # Verifica se é XML válido (não HTML de erro)
        content = resp.content
        if content[:5] not in [b'<?xml', b'<rss ']:
            # Tenta mesmo assim
            pass
        
        root = ET.fromstring(content)
        channel = root.find('channel')
        items = channel.findall('item') if channel else []

        kw_list = [k.strip().lower() for k in keywords.split(',') if k.strip()] if keywords else []

        for item in items:
            title = (item.findtext('title') or '').strip()
            link  = (item.findtext('link') or '').strip()
            desc  = (item.findtext('description') or '').strip()
            pub   = (item.findtext('pubDate') or '').strip()

            if not title:
                continue

            if kw_list:
                combined = (title + ' ' + desc).lower()
                if not any(k in combined for k in kw_list):
                    continue

            agri = is_agricultural({"title": title, "description": desc})

            if job_type == 'agricultural' and not agri:
                continue
            if job_type == 'non-agricultural' and agri:
                continue

            case_num = link.split('/')[-1] if '/' in link else link
            job_id = f"dol_{case_num}"

            try:
                dt = datetime.strptime(pub, '%a, %d %b %Y %H:%M:%S %Z')
                date_str = dt.strftime('%d/%m/%Y')
            except:
                date_str = datetime.now().strftime('%d/%m/%Y')

            jobs.append({
                "id": job_id, "title": title, "company": "", "location": "",
                "salary": "", "date": date_str, "contactEmail": "", "contactPhone": "",
                "description": desc[:600], "url": link,
                "status": "pending", "isNew": True, "agri": agri, "source": "dol.gov",
            })

        add_log(f"RSS OK: {len(items)} vagas no feed, {len(jobs)} passaram no filtro.", "found")
        return jobs, None

    except ET.ParseError as e:
        error_detail = f"XML inválido: {e}. Resposta do servidor: {resp.text[:200] if 'resp' in dir() else 'sem resposta'}"
    except requests.exceptions.ConnectionError as e:
        error_detail = f"Sem conexão com seasonaljobs.dol.gov: {e}"
    except requests.exceptions.Timeout:
        error_detail = "Timeout ao conectar com seasonaljobs.dol.gov (>20s)"
    except requests.exceptions.HTTPError as e:
        error_detail = f"Erro HTTP {resp.status_code}: {e}"
    except Exception as e:
        error_detail = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"

    add_log(f"Erro RSS: {error_detail}", "error")
    return [], error_detail

def fetch_job_detail(url):
    """Busca email/telefone/salário na página individual da vaga"""
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')
        contact_email, contact_phone, salary, location, company = "", "", "", "", ""

        mailto = soup.find('a', href=lambda h: h and h.startswith('mailto:'))
        if mailto:
            contact_email = mailto['href'].replace('mailto:', '').strip()

        tel = soup.find('a', href=lambda h: h and h.startswith('tel:'))
        if tel:
            contact_phone = tel.get_text(strip=True)

        full_text = soup.get_text(separator='\n')
        for line in full_text.split('\n'):
            ln = line.strip()
            if '$' in ln and any(w in ln.lower() for w in ['hour','week','per','rate']):
                salary = ln[:80]
                break

        h1 = soup.find('h1') or soup.find('h2')
        if h1:
            for sib in list(h1.next_siblings)[:6]:
                t = sib.get_text(strip=True) if hasattr(sib, 'get_text') else ''
                if t and not company:
                    company = t
                elif t and not location and any(s in t for s in [', CA', ', TX', ', FL', ', WA', ', OR', ', NY', ', NC', ', GA', ', AZ', ', CO', ', ID', ', MI', ', MN', ', MO', ', MT', ', NE', ', NV', ', OH', ', PA', ', VA', ', WI']):
                    location = t
                    break

        return {"contactEmail": contact_email, "contactPhone": contact_phone,
                "salary": salary, "company": company, "location": location}
    except:
        return {}

def build_email_content(job, config):
    agri    = job.get('agri', False)
    cv      = config['cv_agricultural'] if agri else config['cv_non_agricultural']
    company = job.get('company') or 'the company'
    subj    = (config['email_subject']
               .replace('{job_title}', job['title'])
               .replace('{company}',   company)
               .replace('{your_name}', config['name']))
    body    = (config['email_body']
               .replace('{job_title}', job['title'])
               .replace('{company}',   company)
               .replace('{your_name}', config['name'])
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
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{cv_file}"')
                msg.attach(part)
        srv = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        srv.starttls()
        srv.login(config['email'], config['email_password'])
        srv.sendmail(config['email'], to_email, msg.as_string())
        srv.quit()
        return True, "Enviado"
    except Exception as e:
        return False, str(e)

# ── ROTAS ──────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ping')
def ping():
    """Diagnóstico: testa conexão com DOL"""
    try:
        r = requests.get(RSS_URL, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        return jsonify({
            "status": "ok",
            "http_code": r.status_code,
            "content_type": r.headers.get('content-type',''),
            "first_bytes": r.text[:100]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/config', methods=['GET'])
def api_get_config():
    c = get_config()
    safe = {k: v for k, v in c.items() if k != 'email_password'}
    safe['has_password'] = bool(c.get('email_password'))
    return jsonify(safe)

@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    return jsonify(DATA['jobs'])

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    body     = request.json or {}
    keywords = body.get('keywords', '')
    job_type = body.get('job_type', 'all')

    new_rss, error = fetch_rss_jobs(keywords, job_type)

    if error and not new_rss:
        return jsonify({"success": False, "error": error, "scraped": 0, "new": 0, "jobs": []}), 200

    existing_ids = {j['id'] for j in DATA['jobs']}
    added = []
    for job in new_rss:
        if job['id'] not in existing_ids:
            job = enrich_job(job)
            DATA['jobs'].append(job)
            existing_ids.add(job['id'])
            added.append(job)

    return jsonify({"success": True, "scraped": len(new_rss), "new": len(added), "jobs": added})

def enrich_job(job):
    if job.get('url'):
        detail = fetch_job_detail(job['url'])
        job.update({k: v for k, v in detail.items() if v})
    return job

@app.route('/api/enrich/<job_id>', methods=['POST'])
def api_enrich(job_id):
    job = next((j for j in DATA['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({"success": False})
    job = enrich_job(job)
    return jsonify({"success": True, "job": job})

@app.route('/api/send/<job_id>', methods=['POST'])
def api_send(job_id):
    config = get_config()
    job    = next((j for j in DATA['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({"success": False, "message": "Vaga não encontrada"})

    subject, body, cv = build_email_content(job, config)
    to_email = job.get('contactEmail', '')

    if not to_email:
        return jsonify({"success": False, "no_email": True,
                        "message": "Vaga sem email. Candidate-se pelo link.",
                        "url": job.get('url', '')})

    if config.get('email_password'):
        success, msg = send_email_smtp(to_email, subject, body, cv, config)
        if success:
            job['status'] = 'sent'
            if job_id not in DATA['sent_ids']:
                DATA['sent_ids'].append(job_id)
            add_log(f"✓ Enviado: {job['title']} → {to_email}", "sent")
        else:
            add_log(f"✗ Erro: {job['title']} — {msg}", "error")
        return jsonify({"success": success, "message": msg})
    else:
        import urllib.parse
        mailto = f"mailto:{to_email}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body + chr(10)*2 + '[Anexar: ' + cv + ']')}"
        return jsonify({"success": True, "manual": True, "mailto": mailto,
                        "cv": cv, "subject": subject, "body": body, "to": to_email})

@app.route('/api/send-all', methods=['POST'])
def api_send_all():
    config  = get_config()
    pending = [j for j in DATA['jobs'] if j.get('status') == 'pending' and j.get('contactEmail')]
    results = []
    for job in pending:
        subject, body, cv = build_email_content(job, config)
        if config.get('email_password'):
            ok, msg = send_email_smtp(job['contactEmail'], subject, body, cv, config)
            if ok:
                job['status'] = 'sent'
                if job['id'] not in DATA['sent_ids']:
                    DATA['sent_ids'].append(job['id'])
                add_log(f"✓ Auto-enviado: {job['title']}", "sent")
            results.append({"title": job['title'], "success": ok})
    return jsonify({"sent": len([r for r in results if r['success']]), "results": results})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    return jsonify(DATA['logs'][:50])

@app.route('/api/stats', methods=['GET'])
def api_stats():
    jobs  = DATA['jobs']
    today = datetime.now().strftime('%d/%m/%Y')
    return jsonify({
        "total":            len(jobs),
        "sent":             len([j for j in jobs if j.get('status') == 'sent']),
        "pending":          len([j for j in jobs if j.get('status') == 'pending']),
        "today":            len([j for j in jobs if j.get('date','') == today]),
        "agricultural":     len([j for j in jobs if j.get('agri')]),
        "non_agricultural": len([j for j in jobs if not j.get('agri')]),
        "with_email":       len([j for j in jobs if j.get('contactEmail')]),
    })

def scheduler_loop():
    config = get_config()
    send_time = config.get('send_time', '08:00')
    def daily_job():
        jobs, _ = fetch_rss_jobs()
        existing = {j['id'] for j in DATA['jobs']}
        for job in jobs:
            if job['id'] not in existing:
                job = enrich_job(job)
                DATA['jobs'].append(job)
                existing.add(job['id'])
        # auto envia pendentes
        cfg = get_config()
        if cfg.get('email_password'):
            pending = [j for j in DATA['jobs'] if j.get('status') == 'pending' and j.get('contactEmail')]
            for job in pending:
                subj, body, cv = build_email_content(job, cfg)
                ok, _ = send_email_smtp(job['contactEmail'], subj, body, cv, cfg)
                if ok:
                    job['status'] = 'sent'
                    add_log(f"✓ Auto: {job['title']}", "sent")
    schedule.every().day.at(send_time).do(daily_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    os.makedirs('curriculos', exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("\n🌿 SeasonalSender — http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)
