from flask import Flask, render_template, request, jsonify
import json, os, smtplib, schedule, time, threading, requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from bs4 import BeautifulSoup

app = Flask(__name__)

# No Render, dados ficam em memória (reinicia ao deploy)
# Para persistência real, use um banco de dados externo
DATA = {"jobs": [], "sent_ids": [], "logs": []}

AGRICULTURAL_KEYWORDS = [
    'farm','harvest','pick','fruit','vegetable','crop','orchard',
    'field','agriculture','farming','grape','strawberry','apple',
    'packing','horticulture','dairy','poultry','livestock','greenhouse'
]

def get_config():
    """Lê config das variáveis de ambiente (seguras no Render)"""
    return {
        "name":            os.environ.get("SENDER_NAME", ""),
        "email":           os.environ.get("SENDER_EMAIL", ""),
        "email_password":  os.environ.get("SENDER_PASSWORD", ""),
        "smtp_server":     os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":       int(os.environ.get("SMTP_PORT", "587")),
        "phone":           os.environ.get("SENDER_PHONE", ""),
        "cv_agricultural": os.environ.get("CV_AGRI", "curriculo_agricola.pdf"),
        "cv_non_agricultural": os.environ.get("CV_NON_AGRI", "curriculo_geral.pdf"),
        "email_subject":   os.environ.get("EMAIL_SUBJECT", "Application for {job_title} - {your_name}"),
        "email_body":      os.environ.get("EMAIL_BODY",
            "Dear Hiring Manager,\n\nI am writing to apply for the position of {job_title} at {company}.\n\n"
            "I am a motivated and hardworking individual, available to start immediately. "
            "Please find my CV attached for your consideration.\n\nBest regards,\n{your_name}\n{your_phone}"),
        "send_time":       os.environ.get("SEND_TIME", "08:00"),
        "keywords":        os.environ.get("SEARCH_KEYWORDS", "farm, harvest, picking, seasonal"),
        "countries":       os.environ.get("SEARCH_COUNTRIES", "United Kingdom"),
    }

def is_agricultural(job):
    text = (job.get('title','') + ' ' + job.get('description','') + ' ' + job.get('category','')).lower()
    return any(k in text for k in AGRICULTURAL_KEYWORDS)

def scrape_seasonal_jobs(keywords="", country="United Kingdom"):
    jobs = []
    try:
        query = keywords.replace(' ', '+')
        url = f"https://www.seasonaljobs.co.uk/jobs?q={query}&location={country.replace(' ', '+')}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        job_cards = soup.find_all('div', class_=['job','job-listing','vacancy','job-result'])
        for card in job_cards[:20]:
            try:
                title_el = card.find(['h2','h3','a'], class_=['job-title','title','position'])
                company_el = card.find(class_=['company','employer','organisation'])
                location_el = card.find(class_=['location','place','area'])
                link_el = card.find('a', href=True)
                title = title_el.get_text(strip=True) if title_el else None
                if not title:
                    continue
                company = company_el.get_text(strip=True) if company_el else "Company"
                location = location_el.get_text(strip=True) if location_el else country
                href = link_el['href'] if link_el else ""
                link = ("https://www.seasonaljobs.co.uk" + href) if href.startswith('/') else href
                job_id = f"sj_{abs(hash(title+company+location))}"
                jobs.append({
                    "id": job_id, "title": title, "company": company,
                    "location": location, "salary": "See listing",
                    "date": datetime.now().strftime("%d/%m/%Y"),
                    "contactEmail": f"jobs@{company.lower().replace(' ','-').replace('/','-')}.com",
                    "description": "See full details at the link below.",
                    "url": link, "status": "pending", "isNew": True, "source": "scraped"
                })
            except:
                continue
    except Exception as e:
        add_log(f"Scraping error: {e}", "error")
    return jobs

def add_log(text, type_="found"):
    DATA['logs'].insert(0, {"type": type_, "text": text, "time": datetime.now().isoformat()})
    if len(DATA['logs']) > 100:
        DATA['logs'] = DATA['logs'][:100]

def send_email_smtp(to_email, subject, body, cv_file, config):
    try:
        msg = MIMEMultipart()
        msg['From'] = config['email']
        msg['To'] = to_email
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
        server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        server.starttls()
        server.login(config['email'], config['email_password'])
        server.sendmail(config['email'], to_email, msg.as_string())
        server.quit()
        return True, "Enviado"
    except Exception as e:
        return False, str(e)

def build_email_content(job, config):
    agri = is_agricultural(job)
    cv = config['cv_agricultural'] if agri else config['cv_non_agricultural']
    subject = config['email_subject'].replace('{job_title}', job['title']).replace('{company}', job['company']).replace('{your_name}', config['name'])
    body = config['email_body'].replace('{job_title}', job['title']).replace('{company}', job['company']).replace('{your_name}', config['name']).replace('{your_phone}', config.get('phone',''))
    return subject, body, cv, agri

def auto_send_pending():
    config = get_config()
    if not config.get('email') or not config.get('email_password'):
        return
    sent_ids = set(DATA.get('sent_ids', []))
    pending = [j for j in DATA['jobs'] if j['id'] not in sent_ids and j.get('status') == 'pending']
    for job in pending:
        subject, body, cv, _ = build_email_content(job, config)
        success, msg = send_email_smtp(job['contactEmail'], subject, body, cv, config)
        if success:
            sent_ids.add(job['id'])
            job['status'] = 'sent'
            add_log(f"✓ Email enviado: {job['title']} @ {job['company']}", "sent")
        else:
            add_log(f"✗ Erro: {job['title']} — {msg}", "error")
    DATA['sent_ids'] = list(sent_ids)

# ── ROTAS ──────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

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
    config = get_config()
    keywords = request.json.get('keywords', config.get('keywords',''))
    country  = request.json.get('country', 'United Kingdom')
    jobs = scrape_seasonal_jobs(keywords, country)
    existing_ids = {j['id'] for j in DATA['jobs']}
    new_jobs = [j for j in jobs if j['id'] not in existing_ids]
    for job in new_jobs:
        job['agri'] = is_agricultural(job)
        DATA['jobs'].append(job)
        add_log(f"Nova vaga: {job['title']} @ {job['company']}", "found")
    return jsonify({"scraped": len(jobs), "new": len(new_jobs), "jobs": new_jobs})

@app.route('/api/jobs/add', methods=['POST'])
def api_add_jobs():
    new_jobs = request.json.get('jobs', [])
    existing_ids = {j['id'] for j in DATA['jobs']}
    added = 0
    for job in new_jobs:
        if job['id'] not in existing_ids:
            job['agri'] = is_agricultural(job)
            job['status'] = 'pending'
            DATA['jobs'].append(job)
            add_log(f"Nova vaga: {job['title']} @ {job['company']}", "found")
            added += 1
    return jsonify({"added": added, "total": len(DATA['jobs'])})

@app.route('/api/send/<job_id>', methods=['POST'])
def api_send(job_id):
    config = get_config()
    job = next((j for j in DATA['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({"success": False, "message": "Vaga não encontrada"})
    subject, body, cv, agri = build_email_content(job, config)
    if config.get('email_password'):
        success, msg = send_email_smtp(job['contactEmail'], subject, body, cv, config)
        if success:
            job['status'] = 'sent'
            if job['id'] not in DATA['sent_ids']:
                DATA['sent_ids'].append(job['id'])
            add_log(f"✓ Enviado: {job['title']} @ {job['company']}", "sent")
        else:
            add_log(f"✗ Erro: {job['title']} — {msg}", "error")
        return jsonify({"success": success, "message": msg})
    else:
        mailto = f"mailto:{job['contactEmail']}?subject={subject}&body={body}"
        return jsonify({"success": True, "manual": True, "mailto": mailto,
                        "cv": cv, "subject": subject, "body": body, "to": job['contactEmail']})

@app.route('/api/send-all', methods=['POST'])
def api_send_all():
    auto_send_pending()
    return jsonify({"message": "Concluído"})

@app.route('/api/logs', methods=['GET'])
def api_logs():
    return jsonify(DATA.get('logs', [])[:50])

@app.route('/api/stats', methods=['GET'])
def api_stats():
    jobs = DATA['jobs']
    today = datetime.now().strftime("%d/%m/%Y")
    return jsonify({
        "total":           len(jobs),
        "sent":            len([j for j in jobs if j.get('status') == 'sent']),
        "pending":         len([j for j in jobs if j.get('status') == 'pending']),
        "today":           len([j for j in jobs if j.get('date','') == today]),
        "agricultural":    len([j for j in jobs if j.get('agri')]),
        "non_agricultural":len([j for j in jobs if not j.get('agri')]),
    })

def run_scheduler():
    config = get_config()
    send_time = config.get('send_time', '08:00')
    schedule.every().day.at(send_time).do(auto_send_pending)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    os.makedirs('curriculos', exist_ok=True)
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    print("\n🌿 SeasonalSender em http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)
