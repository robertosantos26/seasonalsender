from flask import Flask, render_template, request, jsonify, Response
import os, smtplib, schedule, time, threading, requests, traceback, urllib.parse, json, imaplib, email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA_FILE = os.path.join(BASE_DIR, "data_store.json")
APP_CONFIG_FILE = os.path.join(BASE_DIR, "config_store.json")
STORE_LOCK = threading.Lock()

def load_json_file(path, default):
    def _read_json(p):
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    if not os.path.exists(path):
        bak = f"{path}.bak"
        if os.path.exists(bak):
            try:
                return _read_json(bak)
            except Exception:
                return default
        return default
    try:
        return _read_json(path)
    except Exception:
        bak = f"{path}.bak"
        if os.path.exists(bak):
            try:
                return _read_json(bak)
            except Exception:
                pass
        return default

def save_json_file(path, data):
    tmp_path = f"{path}.tmp"
    bak_path = f"{path}.bak"
    with STORE_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as src, open(bak_path, 'w', encoding='utf-8') as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
            except Exception:
                pass
        os.replace(tmp_path, path)

def save_data_store():
    save_json_file(APP_DATA_FILE, DATA)

def get_user_config():
    return load_json_file(APP_CONFIG_FILE, {})

def save_user_config(cfg):
    save_json_file(APP_CONFIG_FILE, cfg)

DATA = load_json_file(APP_DATA_FILE, {"jobs": [], "sent_ids": [], "logs": [], "sent_applications": []})
if "sent_applications" not in DATA:
    DATA["sent_applications"] = []
if "open_events" not in DATA:
    DATA["open_events"] = []

RSS_URL = "https://seasonaljobs.dol.gov/job_rss.xml"

AGRI_KW = [
    'farm','harvest','pick','fruit','vegetable','crop','orchard','field',
    'agriculture','farming','grape','strawberry','apple','packing','farmworker',
    'horticulture','dairy','poultry','livestock','greenhouse','nursery',
    'tobacco','irrigation','tractor','equipment operator','ranch','melon',
    'blueberry','potato','corn','wheat','sugar beet','horse groom'
]

def get_config():
    file_cfg = get_user_config()
    return {
        "name":               file_cfg.get("name") or os.environ.get("SENDER_NAME", ""),
        "email":              file_cfg.get("email") or os.environ.get("SENDER_EMAIL", ""),
        "email_password":     file_cfg.get("email_password") or os.environ.get("SENDER_PASSWORD", ""),
        "smtp_server":        file_cfg.get("smtp_server") or os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port":          int(file_cfg.get("smtp_port") or os.environ.get("SMTP_PORT", "587")),
        "phone":              file_cfg.get("phone") or os.environ.get("SENDER_PHONE", ""),
        "cv_agricultural":    file_cfg.get("cv_agricultural") or os.environ.get("CV_AGRI", "curriculo_agricola.pdf"),
        "cv_non_agricultural":file_cfg.get("cv_non_agricultural") or os.environ.get("CV_NON_AGRI", "curriculo_geral.pdf"),
        "cover_letter_agricultural": file_cfg.get("cover_letter_agricultural") or os.environ.get("COVER_LETTER_AGRI", "cover_letter_agricola.pdf"),
        "cover_letter_non_agricultural": file_cfg.get("cover_letter_non_agricultural") or os.environ.get("COVER_LETTER_NON_AGRI", "cover_letter_geral.pdf"),
        "imap_server":       file_cfg.get("imap_server") or os.environ.get("IMAP_SERVER", "imap.gmail.com"),
        "imap_port":         int(file_cfg.get("imap_port") or os.environ.get("IMAP_PORT", "993")),
        "email_subject":      file_cfg.get("email_subject") or os.environ.get("EMAIL_SUBJECT", "Application for {job_title} - {your_name}"),
        "email_body":         file_cfg.get("email_body") or os.environ.get("EMAIL_BODY",
            "Dear Hiring Manager,\n\nI am writing to apply for the position of {job_title} at {company}.\n\n"
            "I am a motivated and hardworking individual available to start immediately. "
            "Please find my CV attached for your consideration.\n\nBest regards,\n{your_name}\n{your_phone}"),
        "send_time":          file_cfg.get("send_time") or os.environ.get("SEND_TIME", "08:00"),
        "keywords":           file_cfg.get("keywords") or os.environ.get("SEARCH_KEYWORDS", ""),
        "job_type":           file_cfg.get("job_type") or os.environ.get("JOB_TYPE", "all"),
        "tracking_base_url":  file_cfg.get("tracking_base_url") or os.environ.get("TRACKING_BASE_URL", ""),
    }

def is_agricultural(job):
    text = (job.get('title','') + ' ' + job.get('description','')).lower()
    return any(k in text for k in AGRI_KW)

def add_log(text, type_="found"):
    DATA['logs'].insert(0, {"type": type_, "text": text, "time": datetime.now().isoformat()})
    if len(DATA['logs']) > 200:
        DATA['logs'] = DATA['logs'][:200]

def fetch_rss_jobs(keywords="", job_type="all"):
    """Le RSS do seasonaljobs.dol.gov. Retorna (lista, erro_ou_None)."""
    jobs = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; SeasonalSender/1.0)',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }
        resp = requests.get(RSS_URL, headers=headers, timeout=20)
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
                "id": f"dol_{case_num}",
                "title": title, "company": "", "location": "",
                "salary": "", "date": date_str, "timestamp": timestamp,
                "contactEmail": "", "contactPhone": "",
                "description": desc[:600], "url": link,
                "status": "pending", "isNew": True,
                "agri": agri, "source": "dol.gov",
            })

        add_log(f"RSS OK: {len(items)} vagas no feed, {len(jobs)} carregadas.", "found")
        return jobs, None

    except ET.ParseError as e:
        msg = f"XML invalido do DOL: {e}"
    except requests.exceptions.ConnectionError as e:
        msg = f"Sem conexao com seasonaljobs.dol.gov: {e}"
    except requests.exceptions.Timeout:
        msg = "Timeout ao conectar com seasonaljobs.dol.gov (>20s)"
    except requests.exceptions.HTTPError as e:
        msg = f"Erro HTTP {resp.status_code if 'resp' in dir() else '?'}: {e}"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"

    add_log(f"Erro RSS: {msg}", "error")
    return [], msg

def fetch_job_detail(url):
    """Busca email/telefone/salario na pagina individual da vaga."""
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')
        contact_email = contact_phone = salary = location = company = ""

        a = soup.find('a', href=lambda h: h and h.startswith('mailto:'))
        if a:
            contact_email = a['href'].replace('mailto:', '').strip()

        t = soup.find('a', href=lambda h: h and h.startswith('tel:'))
        if t:
            contact_phone = t.get_text(strip=True)

        for line in soup.get_text(separator='\n').split('\n'):
            ln = line.strip()
            if '$' in ln and any(w in ln.lower() for w in ['hour','week','per','rate']):
                salary = ln[:80]
                break

        h1 = soup.find('h1') or soup.find('h2')
        if h1:
            for sib in list(h1.next_siblings)[:6]:
                t2 = sib.get_text(strip=True) if hasattr(sib, 'get_text') else ''
                if t2 and not company:
                    company = t2
                elif t2 and not location and any(
                    s in t2 for s in [', CA',', TX',', FL',', WA',', OR',', NY',
                                      ', NC',', GA',', AZ',', CO',', ID',', MI',
                                      ', MN',', MO',', MT',', NE',', NV',', OH',
                                      ', PA',', VA',', WI']):
                    location = t2
                    break

        return {"contactEmail": contact_email, "contactPhone": contact_phone,
                "salary": salary, "company": company, "location": location}
    except:
        return {}

def build_email_content(job, config):
    agri    = job.get('agri', False)
    cv      = config['cv_agricultural'] if agri else config['cv_non_agricultural']
    cover   = config['cover_letter_agricultural'] if agri else config['cover_letter_non_agricultural']
    company = job.get('company') or 'the company'
    subj = (config['email_subject']
            .replace('{job_title}', job['title'])
            .replace('{company}',   company)
            .replace('{your_name}', config['name']))
    body = (config['email_body']
            .replace('{job_title}', job['title'])
            .replace('{company}',   company)
            .replace('{your_name}', config['name'])
            .replace('{your_phone}',config.get('phone','')))
    return subj, body, cv, cover

def resolve_attachment_path(filename):
    if not filename:
        return None
    candidates = [
        os.path.join("curriculos", filename),
        os.path.join("curriculo", filename),
        filename
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

def attach_file(msg, filename):
    path = resolve_attachment_path(filename)
    if not path:
        return False
    with open(path, "rb") as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filename)}"')
        msg.attach(part)
    return True

def _build_tracking_url(config, tracking_token):
    base = (config.get("tracking_base_url") or "").strip()
    if not base:
        return ""
    return f"{base.rstrip('/')}/api/track-open/{tracking_token}"

def send_email_smtp(to_email, subject, body, cv_file, cover_file, config, tracking_token=None):
    try:
        msg = MIMEMultipart()
        msg['From']    = config['email']
        msg['To']      = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        if tracking_token:
            track_url = _build_tracking_url(config, tracking_token)
            if track_url:
                html_body = (
                    f"<html><body>"
                    f"{body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace(chr(10), '<br>')}"
                    f"<img src=\"{track_url}\" width=\"1\" height=\"1\" style=\"display:none;\" alt=\"\" />"
                    f"</body></html>"
                )
                msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        attached_files = []
        if cv_file and attach_file(msg, cv_file):
            attached_files.append(cv_file)
        if cover_file and attach_file(msg, cover_file):
            attached_files.append(cover_file)
        if not attached_files:
            return False, "Nenhum anexo encontrado (CV/Cover Letter)."
        srv = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        srv.starttls()
        srv.login(config['email'], config['email_password'])
        srv.sendmail(config['email'], to_email, msg.as_string())
        srv.quit()
        return True, "Enviado", msg.get('Message-ID', '')
    except Exception as e:
        return False, str(e), ''

# ── ROTAS ──────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ping')
def ping():
    try:
        r = requests.get(RSS_URL, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        return jsonify({"status": "ok", "http_code": r.status_code,
                        "content_type": r.headers.get('content-type',''),
                        "preview": r.text[:80]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/config', methods=['GET'])
def api_get_config():
    c = get_config()
    safe = {k: v for k, v in c.items() if k != 'email_password'}
    safe['has_password'] = bool(c.get('email_password'))
    return jsonify(safe)


@app.route('/api/config', methods=['POST'])
def api_save_config():
    try:
        body = request.json or {}
        allowed = {
            "name", "email", "email_password", "smtp_server", "smtp_port", "phone",
            "cv_agricultural", "cv_non_agricultural", "cover_letter_agricultural", "cover_letter_non_agricultural", "email_subject", "email_body",
            "send_time", "keywords", "job_type", "imap_server", "imap_port", "tracking_base_url"
        }
        current = get_user_config()
        for k, v in body.items():
            if k in allowed:
                current[k] = v
        save_user_config(current)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    sorted_jobs = sorted(DATA['jobs'], key=lambda j: j.get('timestamp', 0), reverse=True)
    return jsonify(sorted_jobs)

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    """Busca vagas do RSS sem enrich para evitar timeout."""
    try:
        body     = request.json or {}
        keywords = body.get('keywords', '')
        job_type = body.get('job_type', 'all')

        new_rss, error = fetch_rss_jobs(keywords, job_type)

        if error and not new_rss:
            return jsonify({"success": False, "error": str(error),
                            "scraped": 0, "new": 0, "jobs": []})

        existing_ids = {j['id'] for j in DATA['jobs']}
        added = []
        for job in new_rss:
            if job['id'] not in existing_ids:
                DATA['jobs'].append(job)
                existing_ids.add(job['id'])
                added.append(job)
        save_data_store()

        return jsonify({"success": True, "scraped": len(new_rss),
                        "new": len(added), "jobs": added})
    except Exception as e:
        return jsonify({"success": False, "error": f"Erro interno: {str(e)}",
                        "scraped": 0, "new": 0, "jobs": []})

@app.route('/api/enrich-all', methods=['POST'])
def api_enrich_all():
    """Enriquece todas as vagas sem email em paralelo."""
    try:
        sem_email = [j for j in DATA['jobs'] if not j.get('contactEmail') and j.get('url')]
        if not sem_email:
            return jsonify({"success": True, "enriched": 0})

        def enrich_one(job):
            try:
                detail = fetch_job_detail(job['url'])
                job.update({k: v for k, v in detail.items() if v})
                return job['id'], True
            except:
                return job['id'], False

        enriched = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(enrich_one, j): j for j in sem_email}
            for f in as_completed(futures, timeout=60):
                try:
                    jid, ok = f.result()
                    if ok:
                        enriched += 1
                except:
                    pass

        save_data_store()
        return jsonify({"success": True, "enriched": enriched, "total": len(sem_email)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "enriched": 0})


@app.route('/api/enrich/<job_id>', methods=['POST'])
def api_enrich(job_id):
    """Carrega contato de uma vaga especifica (chamado sob demanda)."""
    try:
        job = next((j for j in DATA['jobs'] if j['id'] == job_id), None)
        if not job:
            return jsonify({"success": False, "message": "Vaga nao encontrada"})
        if job.get('url'):
            detail = fetch_job_detail(job['url'])
            job.update({k: v for k, v in detail.items() if v})
        save_data_store()
        return jsonify({"success": True, "job": job})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/send/<job_id>', methods=['POST'])
def api_send(job_id):
    try:
        config = get_config()
        job    = next((j for j in DATA['jobs'] if j['id'] == job_id), None)
        if not job:
            return jsonify({"success": False, "message": "Vaga nao encontrada"})

        subject, body, cv, cover = build_email_content(job, config)
        to_email = job.get('contactEmail', '')

        if not to_email:
            return jsonify({"success": False, "no_email": True,
                            "message": "Sem email. Candidate-se pelo link.",
                            "url": job.get('url', '')})

        if config.get('email_password'):
            tracking_token = f"{job_id}_{int(time.time())}"
            success, msg, message_id = send_email_smtp(to_email, subject, body, cv, cover, config, tracking_token)
            if success:
                job['status'] = 'sent'
                if job_id not in DATA['sent_ids']:
                    DATA['sent_ids'].append(job_id)
                DATA["sent_applications"].append({
                    "job_id": job_id,
                    "to": to_email.lower(),
                    "subject": subject,
                    "message_id": message_id,
                    "sent_at": datetime.now().isoformat(),
                    "tracking_token": tracking_token,
                    "open_count": 0,
                    "opened_at": None,
                })
                add_log(f"Enviado: {job['title']} -> {to_email}", "sent")
                save_data_store()
            else:
                add_log(f"Erro: {job['title']} - {msg}", "error")
                save_data_store()
            return jsonify({"success": success, "message": msg})
        else:
            attachment_note = f"[Anexar: {cv}]" + (f"\n[Anexar: {cover}]" if cover else "")
            mailto = (f"mailto:{to_email}"
                      f"?subject={urllib.parse.quote(subject)}"
                      f"&body={urllib.parse.quote(body + chr(10)*2 + attachment_note)}")
            return jsonify({"success": True, "manual": True, "mailto": mailto,
                            "cv": cv, "cover_letter": cover, "subject": subject, "body": body, "to": to_email})
    except Exception as e:
        return jsonify({"success": False, "message": f"Erro: {str(e)}"})

@app.route('/api/send-all', methods=['POST'])
def api_send_all():
    try:
        config  = get_config()
        pending = [j for j in DATA['jobs']
                   if j.get('status') == 'pending' and j.get('contactEmail')]
        results = []
        for job in pending:
            subject, body, cv, cover = build_email_content(job, config)
            if config.get('email_password'):
                tracking_token = f"{job['id']}_{int(time.time())}"
                ok, msg, message_id = send_email_smtp(job['contactEmail'], subject, body, cv, cover, config, tracking_token)
                if ok:
                    job['status'] = 'sent'
                    if job['id'] not in DATA['sent_ids']:
                        DATA['sent_ids'].append(job['id'])
                    DATA["sent_applications"].append({
                        "job_id": job["id"],
                        "to": job["contactEmail"].lower(),
                        "subject": subject,
                        "message_id": message_id,
                        "sent_at": datetime.now().isoformat(),
                        "tracking_token": tracking_token,
                        "open_count": 0,
                        "opened_at": None,
                    })
                    add_log(f"Auto-enviado: {job['title']}", "sent")
                results.append({"title": job['title'], "success": ok})
        if results:
            save_data_store()
        return jsonify({"sent": len([r for r in results if r['success']]),
                        "results": results})
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e), "results": []})


@app.route('/api/check-replies', methods=['POST'])
def api_check_replies():
    try:
        cfg = get_config()
        if not cfg.get('email') or not cfg.get('email_password'):
            return jsonify({"success": False, "message": "Configure email e senha para rastrear respostas."})

        mail = imaplib.IMAP4_SSL(cfg.get('imap_server'), int(cfg.get('imap_port')))
        mail.login(cfg.get('email'), cfg.get('email_password'))
        mail.select('INBOX')
        sent_apps = DATA.get("sent_applications", [])
        tracked_to = {a.get("to", "").lower() for a in sent_apps if a.get("to")}
        tracked_subjects = {a.get("subject", "").lower() for a in sent_apps if a.get("subject")}
        if not tracked_to and not tracked_subjects:
            mail.logout()
            return jsonify({"success": True, "replies_found": 0, "message": "Nenhuma candidatura enviada registrada para rastrear."})

        typ, data = mail.search(None, '(UNSEEN)')
        count = 0
        if typ == 'OK' and data and data[0]:
            for num in data[0].split()[:50]:
                _, msg_data = mail.fetch(num, '(RFC822)')
                if not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                em = email.message_from_bytes(raw)
                sender = em.get('From', 'desconhecido')
                subject = em.get('Subject', '(sem assunto)')
                from_l = sender.lower()
                sub_l = subject.lower()
                if any(t in from_l for t in tracked_to) or any(s in sub_l for s in tracked_subjects):
                    add_log(f"Resposta de candidatura: {sender} | {subject}", 'found')
                    count += 1
        mail.logout()
        if count:
            save_data_store()
        return jsonify({"success": True, "replies_found": count})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/track-open/<token>', methods=['GET'])
def api_track_open(token):
    try:
        match = next((a for a in DATA.get("sent_applications", []) if a.get("tracking_token") == token), None)
        if match:
            match["open_count"] = int(match.get("open_count", 0)) + 1
            if not match.get("opened_at"):
                match["opened_at"] = datetime.now().isoformat()
            DATA["open_events"].insert(0, {
                "token": token,
                "job_id": match.get("job_id"),
                "to": match.get("to"),
                "time": datetime.now().isoformat()
            })
            DATA["open_events"] = DATA["open_events"][:500]
            add_log(f"Email aberto (pixel): {match.get('to','destinatário')}", "found")
            save_data_store()
    except Exception:
        pass
    gif = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
        b"\x00\x02\x02D\x01\x00;"
    )
    return Response(gif, mimetype='image/gif')

@app.route('/api/logs', methods=['GET'])
def api_logs():
    return jsonify(DATA['logs'][:50])

@app.route('/api/stats', methods=['GET'])
def api_stats():
    try:
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
            "opened":           len([a for a in DATA.get("sent_applications", []) if a.get("open_count", 0) > 0]),
        })
    except Exception as e:
        return jsonify({"total":0,"sent":0,"pending":0,"today":0,
                        "agricultural":0,"non_agricultural":0,"with_email":0,"opened":0})

def scheduler_loop():
    config = get_config()
    send_time = config.get('send_time', '08:00')
    def daily():
        try:
            jobs, _ = fetch_rss_jobs()
            existing = {j['id'] for j in DATA['jobs']}
            for job in jobs:
                if job['id'] not in existing:
                    DATA['jobs'].append(job)
                    existing.add(job['id'])
            cfg = get_config()
            if cfg.get('email_password'):
                for job in DATA['jobs']:
                    if job.get('status') == 'pending' and job.get('contactEmail'):
                        s, b, cv, cover = build_email_content(job, cfg)
                        tracking_token = f"{job['id']}_{int(time.time())}"
                        ok, _, message_id = send_email_smtp(job['contactEmail'], s, b, cv, cover, cfg, tracking_token)
                        if ok:
                            job['status'] = 'sent'
                            DATA["sent_applications"].append({
                                "job_id": job["id"],
                                "to": job["contactEmail"].lower(),
                                "subject": s,
                                "message_id": message_id,
                                "sent_at": datetime.now().isoformat(),
                                "tracking_token": tracking_token,
                                "open_count": 0,
                                "opened_at": None,
                            })
                            add_log(f"Auto: {job['title']}", "sent")
                            save_data_store()
        except Exception as e:
            add_log(f"Erro scheduler: {e}", "error")

    schedule.every().day.at(send_time).do(daily)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == '__main__':
    os.makedirs('curriculos', exist_ok=True)
    os.makedirs('curriculo', exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("\n SeasonalSender -- http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)
