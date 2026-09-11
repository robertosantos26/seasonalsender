from flask import Flask, render_template, request, jsonify, Response
import os, smtplib, schedule, time, threading, requests, json, urllib.parse, base64, uuid, random
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

# ── Fila de envio ─────────────────────────────────────────
import queue
_send_queue  = queue.Queue()
_queue_running = False
_queue_status  = {"total": 0, "sent": 0, "failed": 0, "running": False, "current": ""}

# ── Supabase / PostgreSQL ─────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None

def get_pool():
    global _pool
    if _pool is None and DATABASE_URL:
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
    db_query("""
        CREATE INDEX IF NOT EXISTS idx_jobs_timestamp ON jobs(timestamp DESC)
    """)
    db_query("""
        CREATE INDEX IF NOT EXISTS idx_jobs_tracking ON jobs((data->>'tracking_id'))
    """)

# ── Categorias de vaga → currículo ──────────────────────────
DEFAULT_CATEGORIES = [
    {"id": "farmworker",   "label": "🌾 Farm / Agricultura",
     "keywords": "farm,harvest,pick,fruit,vegetable,crop,orchard,field,agriculture,farming,grape,strawberry,apple,packing,farmworker,horticulture,dairy,poultry,livestock,greenhouse,nursery,tobacco,irrigation,tractor,equipment operator,ranch,melon,blueberry,potato,corn,wheat,sugar beet,cattle,ag equipment",
     "cv": "curriculo_farmworker.pdf", "cover_letter": "cover_farmworker.pdf"},
    {"id": "horse",        "label": "🐴 Cavalos / Equestre",
     "keywords": "horse,equine,equestrian,groom,barn hand,stable,thoroughbred,mare,foal,jockey,exercise rider,breeding assistant,farrier",
     "cv": "curriculo_horse.pdf", "cover_letter": "cover_horse.pdf"},
    {"id": "housekeeper",  "label": "🧹 Housekeeping / Hotel",
     "keywords": "housekeeper,housekeeping,room attendant,lodging,lodge attendant,hotel,resort,cleaning,maid,linens,dining room attendant",
     "cv": "curriculo_housekeeper.pdf", "cover_letter": "cover_housekeeper.pdf"},
    {"id": "server",       "label": "🍽️ Server / Food Service",
     "keywords": "server,waiter,waitress,dining,kitchen,food service,busser,banquet,cook,dishwasher,food preparation",
     "cv": "curriculo_server.pdf", "cover_letter": "cover_server.pdf"},
    {"id": "landscaping",  "label": "🌳 Landscaping / Jardinagem",
     "keywords": "landscap,groundskeeper,lawn,forestry,tree,grounds maintenance,irrigation laborer",
     "cv": "curriculo_landscaping.pdf", "cover_letter": "cover_landscaping.pdf"},
    {"id": "construction", "label": "🧱 Construção",
     "keywords": "construction,carpenter,carpentry,framing,roofing,laborer,concrete,mason,drywall,painter,general contractor,building,welder,electrician helper,plumber helper",
     "cv": "curriculo_construction.pdf", "cover_letter": "cover_construction.pdf"},
    {"id": "general",      "label": "📄 Geral / Outros",
     "keywords": "",
     "cv": "curriculo_geral.pdf", "cover_letter": "cover_letter_geral.pdf"},
]

def categorize_job(text, categories):
    """Escolhe a categoria com mais palavras-chave batendo no texto. 'general' é o fallback."""
    text_l = (text or "").lower()
    best_id, best_score = "general", 0
    for cat in categories:
        if cat.get("id") == "general":
            continue
        kws = [k.strip().lower() for k in (cat.get("keywords") or "").split(",") if k.strip()]
        score = sum(1 for k in kws if k in text_l)
        if score > best_score:
            best_score, best_id = score, cat.get("id")
    return best_id

# ── Config ────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "name": "", "email": "", "email_password": "",
    "smtp_server": "smtp.gmail.com", "smtp_port": 587, "phone": "",
    "app_base_url": "",
    "categories": DEFAULT_CATEGORIES,
    "email_subject": "Application for {job_title} - {your_name}",
    "email_body": (
        "Dear Hiring Manager,\n\n"
        "I am writing to apply for the position of {job_title} at {company}.\n\n"
        "I am a motivated and hardworking individual available to start immediately. "
        "Please find my CV and cover letter attached for your consideration.\n\n"
        "Best regards,\n{your_name}\n{your_phone}"
    ),
    "send_time": "08:00", "keywords": "", "category_filter": "all",
    "daily_send_limit": 50,
    "send_window_start": "07:00",
    "send_window_end": "21:00",
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
            if not cfg.get("categories"):
                cfg["categories"] = DEFAULT_CATEGORIES
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

def fetch_rss_jobs(keywords="", category_filter="all", categories=None):
    categories = categories or DEFAULT_CATEGORIES
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
            category = categorize_job(title + " " + desc, categories)
            if category_filter != "all" and category != category_filter:
                continue
            case_num = link.split("/")[-1] if "/" in link else link
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
                "category": category, "agri": category == "farmworker",
                "opened": False, "opened_at": None, "tracking_id": None,
                "source": "dol.gov",
            })
        add_log(f"RSS: {len(items)} vagas no feed, {len(jobs)} carregadas.", "found")
        return jobs, None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        add_log(f"Erro RSS: {msg}", "error")
        return [], msg

def upsert_jobs(jobs):
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

def get_category(config, cat_id):
    cats = config.get("categories") or DEFAULT_CATEGORIES
    for c in cats:
        if c.get("id") == cat_id:
            return c
    return cats[-1] if cats else DEFAULT_CATEGORIES[-1]

def build_email_content(job, config):
    cat = get_category(config, job.get("category", "general"))
    cv = cat.get("cv", "")
    cl = cat.get("cover_letter", "")
    company = job.get("company") or "the company"
    # Cada categoria pode ter seu próprio assunto/mensagem; se vazio, usa o padrão geral.
    subject_tpl = cat.get("subject") or config["email_subject"]
    body_tpl    = cat.get("message") or config["email_body"]
    subj = (subject_tpl
            .replace("{job_title}", job["title"])
            .replace("{company}",   company)
            .replace("{your_name}", config.get("name","")))
    body = (body_tpl
            .replace("{job_title}", job["title"])
            .replace("{company}",   company)
            .replace("{your_name}", config.get("name",""))
            .replace("{your_phone}",config.get("phone","")))
    return subj, body, cv, cl

def read_attachment(folder, filename, config=None):
    """Busca o PDF enviado pela página (salvo no banco) e, se não houver, cai para o disco (compatibilidade)."""
    if config and filename:
        for cat in (config.get("categories") or []):
            if cat.get("cv") == filename and cat.get("cv_data"):
                return cat["cv_data"]
            if cat.get("cover_letter") == filename and cat.get("cover_letter_data"):
                return cat["cover_letter_data"]
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return None

def get_base_url(config):
    return (config.get("app_base_url") or os.environ.get("RENDER_EXTERNAL_URL","") or "").rstrip("/")

def send_email_smtp(to_email, subject, body, cv_file, config, cover_letter_file="", tracking_id=None):
    """Envia email via Google Apps Script relay (HTTPS — funciona em qualquer plataforma)."""
    gas_url      = os.environ.get("GAS_WEBHOOK_URL", "")
    sender_email = config.get("email", "")
    sender_name  = config.get("name", "SeasonalSender")

    if not gas_url:
        return False, "GAS_WEBHOOK_URL não configurada. Siga as instruções de configuração."
    if not sender_email:
        return False, "Email não configurado em Configurações."

    html_body = body.replace("\n", "<br>")
    base_url = get_base_url(config)
    if tracking_id and base_url:
        html_body += f'<img src="{base_url}/api/track/{tracking_id}.gif" width="1" height="1" style="display:none" alt="">'

    payload = {
        "to":          to_email,
        "subject":     subject,
        "body":        body,
        "html_body":   html_body,
        "bcc":         sender_email,
        "sender_name": sender_name,
    }

    attachments = []
    for fname in [cv_file, cover_letter_file]:
        if fname:
            data = read_attachment("curriculos", fname, config)
            if data:
                attachments.append({"filename": fname, "content": data})
    if attachments:
        payload["attachments"] = attachments

    try:
        resp = requests.post(gas_url, json=payload, timeout=20)
        data = resp.json() if resp.content else {}
        if data.get("ok"):
            return True, "Enviado"
        return False, data.get("message", f"Erro {resp.status_code}")
    except Exception as e:
        return False, str(e)

# ── Envio automático escalonado ──────────────────────────
def compute_interval_seconds(config):
    """Calcula o intervalo entre emails para distribuir o limite diário na janela configurada."""
    limit = max(1, int(config.get("daily_send_limit", 50) or 50))
    start = config.get("send_window_start", "07:00") or "07:00"
    end   = config.get("send_window_end", "21:00") or "21:00"
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        window_min = (eh*60+em) - (sh*60+sm)
        if window_min <= 0:
            window_min = 12*60
    except:
        window_min = 12*60
    return max(90, int((window_min*60) / limit))

def count_sent_today():
    today = datetime.now().strftime("%d/%m/%Y")
    try:
        row = db_query("SELECT COUNT(*) as c FROM jobs WHERE data->>'sent_date' = %s", (today,), fetch="one")
        return row["c"] if row else 0
    except:
        return 0

def start_send_queue(jobs_to_send, interval_seconds):
    global _queue_running
    if _queue_running or not jobs_to_send:
        return False
    while not _send_queue.empty():
        try: _send_queue.get_nowait()
        except: pass
    for job in jobs_to_send:
        _send_queue.put(job["id"])
    _queue_status.update({
        "total": len(jobs_to_send), "sent": 0, "failed": 0,
        "running": True, "current": "Iniciando...",
        "interval_min": round(interval_seconds/60, 1)
    })
    t = threading.Thread(target=queue_worker, args=(interval_seconds,), daemon=True)
    t.start()
    return True

def queue_worker(interval_seconds=300):
    global _queue_running
    _queue_running = True
    _queue_status["running"] = True

    while not _send_queue.empty():
        try:
            job_id = _send_queue.get(timeout=1)
            config = load_config()

            row = db_query("SELECT data FROM jobs WHERE id = %s", (job_id,), fetch="one")
            if not row:
                _send_queue.task_done()
                continue

            job = row["data"]
            to_email = job.get("contactEmail", "")
            if not to_email:
                _send_queue.task_done()
                continue

            subject, body, cv, cl = build_email_content(job, config)
            tid = uuid.uuid4().hex
            _queue_status["current"] = f"{job['title']} → {to_email}"

            success, msg = send_email_smtp(to_email, subject, body, cv, config, cl, tracking_id=tid)

            if success:
                job.update({
                    "status": "sent",
                    "sent_date": datetime.now().strftime("%d/%m/%Y"),
                    "sent_at": datetime.now().isoformat(),
                    "tracking_id": tid,
                    "opened": False,
                })
                db_query("UPDATE jobs SET data = %s, status = 'sent' WHERE id = %s",
                         (json.dumps(job), job_id))
                add_log(f"✓ Fila: {job['title']} → {to_email}", "sent")
                _queue_status["sent"] += 1
            else:
                add_log(f"✗ Fila erro: {job['title']} — {msg}", "error")
                _queue_status["failed"] += 1

            _send_queue.task_done()

            if not _send_queue.empty():
                mins = round(interval_seconds/60, 1)
                _queue_status["current"] = f"Aguardando ~{mins} min... ({_send_queue.qsize()} restantes)"
                time.sleep(interval_seconds * random.uniform(0.75, 1.3))

        except queue.Empty:
            break
        except Exception as e:
            add_log(f"Erro worker: {e}", "error")
            _send_queue.task_done()

    _queue_running = False
    _queue_status["running"] = False
    _queue_status["current"] = ""


# ── ROTAS ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

TRACKING_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")

@app.route("/api/track/<tid>.gif")
def track_pixel(tid):
    try:
        row = db_query("SELECT id, data FROM jobs WHERE data->>'tracking_id' = %s", (tid,), fetch="one")
        if row and not row["data"].get("opened"):
            job = row["data"]
            job["opened"] = True
            job["opened_at"] = datetime.now().isoformat()
            db_query("UPDATE jobs SET data = %s WHERE id = %s", (json.dumps(job), row["id"]))
            add_log(f"👁 Email aberto: {job.get('title','')} — {job.get('contactEmail','')}", "opened")
    except:
        pass
    return Response(TRACKING_GIF, mimetype="image/gif")

@app.route("/api/status")
def api_status():
    status = {}
    if not DATABASE_URL:
        status["banco"] = "❌ DATABASE_URL não configurada"
    else:
        try:
            init_db()
            row = db_query("SELECT COUNT(*) as total FROM jobs", fetch="one")
            status["banco"] = f"✅ Supabase OK — {row['total'] if row else 0} vagas"
        except Exception as e:
            status["banco"] = f"❌ Erro banco: {str(e)[:80]}"
    cfg = load_config()
    if cfg.get("email") and cfg.get("email_password"):
        status["email"] = f"✅ Email configurado: {cfg['email']}"
    else:
        status["email"] = "❌ Email ou senha não configurados em Configurações"
    if not os.environ.get("GAS_WEBHOOK_URL"):
        status["relay"] = "❌ GAS_WEBHOOK_URL não configurada (necessária para enviar)"
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

@app.route("/api/web-config", methods=["GET"])
def api_get_web_config():
    return api_get_config()

@app.route("/api/web-config", methods=["POST"])
def api_save_web_config():
    return api_save_config()

@app.route("/api/upload-curriculo", methods=["POST"])
def api_upload_curriculo():
    """Salva currículo/cover letter na tabela attachments."""
    try:
        file = request.files.get("file")
        cat_id = (request.form.get("category") or "").strip()
        kind = (request.form.get("kind") or "cv").strip()

        if not file or not file.filename:
            return jsonify({
                "success": False,
                "message": "Nenhum arquivo enviado"
            })

        if not file.filename.lower().endswith(".pdf"):
            return jsonify({
                "success": False,
                "message": "Envie um arquivo PDF"
            })

        if kind not in ("cv", "cover_letter"):
            return jsonify({
                "success": False,
                "message": "Tipo de arquivo inválido"
            })

        if not cat_id:
            return jsonify({
                "success": False,
                "message": "Categoria não informada"
            })

        raw = file.read()

        if not raw:
            return jsonify({
                "success": False,
                "message": "Arquivo vazio"
            })

        if len(raw) > 8 * 1024 * 1024:
            return jsonify({
                "success": False,
                "message": "Arquivo muito grande (máx. 8MB)"
            })

        config = load_config()
        cats = config.get("categories") or DEFAULT_CATEGORIES

        target = next(
            (c for c in cats if c.get("id") == cat_id),
            None
        )

        if not target:
            return jsonify({
                "success": False,
                "message": "Categoria não encontrada"
            })

        fname = f"{cat_id}_{kind}.pdf"

        # Salva o arquivo REAL na tabela attachments
        db_query("""
            INSERT INTO attachments
                (id, category, kind, filename, content, content_type)
            VALUES
                (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (category, kind)
            DO UPDATE SET
                filename = EXCLUDED.filename,
                content = EXCLUDED.content,
                content_type = EXCLUDED.content_type,
                created_at = NOW()
        """, (
            str(uuid.uuid4()),
            cat_id,
            kind,
            fname,
            psycopg2.Binary(raw),
            "application/pdf"
        ))

        # Mantém somente o nome na configuração
        target[kind] = fname

        # Remove o PDF antigo que estava sendo armazenado dentro do config
        target.pop(f"{kind}_data", None)

        config["categories"] = cats

        if not save_config(config):
            return jsonify({
                "success": False,
                "message": "Arquivo foi enviado, mas não foi possível atualizar a configuração."
            })

        # CONFIRMA que realmente gravou
        saved = db_query("""
            SELECT id, filename, category, kind
            FROM attachments
            WHERE category = %s
              AND kind = %s
            LIMIT 1
        """, (cat_id, kind), fetch="one")

        if not saved:
            return jsonify({
                "success": False,
                "message": "O arquivo não foi encontrado após o salvamento."
            })

        return jsonify({
            "success": True,
            "message": "Currículo enviado e salvo no banco!",
            "filename": saved["filename"],
            "category": saved["category"],
            "kind": saved["kind"]
        })

    except Exception as e:
        print(f"ERRO UPLOAD CURRICULO: {type(e).__name__}: {e}")

        return jsonify({
            "success": False,
            "message": f"Erro ao salvar currículo: {str(e)}"
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    try:
        body_in = request.get_json(silent=True) or {}
        to = (body_in.get("to") or "").strip()
        config = load_config()
        if not to:
            to = config.get("email", "")
        if not to:
            return jsonify({"success": False, "message": "Informe um e-mail de destino"})
        subject = "Teste — SeasonalSender"
        body = f"Este é um e-mail de teste enviado pelo SeasonalSender.\n\nEnviado por: {config.get('name','')}"
        success, msg = send_email_smtp(to, subject, body, "", config, "")
        return jsonify({"success": success, "message": msg if not success else f"E-mail de teste enviado para {to}"})
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
        category_filter = body.get("category", body.get("job_type","all"))
        config = load_config()
        new_rss, error = fetch_rss_jobs(keywords, category_filter, config.get("categories"))
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
        payload_in = request.get_json(silent=True) or {}
        preview = request.args.get("preview") == "1" or bool(payload_in.get("preview"))
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
        if preview:
            return jsonify({"success": True, "manual": True, "preview": True,
                            "subject": subj, "body": body, "to": to_email})
        if config.get("email_password") or os.environ.get("GAS_WEBHOOK_URL"):
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

@app.route("/api/enrich-all", methods=["POST"])
def api_enrich_all():
    """Busca email/telefone/empresa em paralelo para todas as vagas que ainda não têm contato."""
    try:
        from concurrent.futures import ThreadPoolExecutor
        rows = db_query("""
            SELECT id, data FROM jobs
            WHERE (data->>'contactEmail' = '' OR data->>'contactEmail' IS NULL)
            AND data->>'url' != ''
        """, fetch="all")
        targets = rows or []

        def work(row):
            job = row["data"]
            detail = fetch_job_detail(job.get("url",""))
            if detail:
                job.update({k: v for k, v in detail.items() if v})
                try:
                    db_query("UPDATE jobs SET data = %s WHERE id = %s", (json.dumps(job), row["id"]))
                except:
                    pass
            return bool(detail.get("contactEmail")) if detail else False

        found = 0
        if targets:
            with ThreadPoolExecutor(max_workers=6) as ex:
                for ok in ex.map(work, targets):
                    if ok:
                        found += 1
        add_log(f"Enriquecimento em massa: {found}/{len(targets)} vagas com email encontrado.", "found")
        return jsonify({"success": True, "checked": len(targets), "found": found})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/send/<job_id>", methods=["POST"])
def api_send(job_id):
    try:
        payload_in = request.get_json(silent=True) or {}
        preview = request.args.get("preview") == "1" or bool(payload_in.get("preview"))
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
        if preview:
            return jsonify({"success": True, "manual": True, "preview": True,
                            "cv": cv, "cl": cl, "subject": subject, "body": body, "to": to_email})
        if config.get("email_password") or os.environ.get("GAS_WEBHOOK_URL"):
            tid = uuid.uuid4().hex
            success, msg = send_email_smtp(to_email, subject, body, cv, config, cl, tracking_id=tid)
            if success:
                job.update({"status":"sent","sent_date":datetime.now().strftime("%d/%m/%Y"),
                            "sent_at":datetime.now().isoformat(),"tracking_id":tid,"opened":False})
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
        if _queue_running:
            return jsonify({
                "success": False,
                "message": f"Fila já está rodando! {_send_queue.qsize()} emails restantes.",
                "status": _queue_status
            })

        config = load_config()
        limit = max(1, int(config.get("daily_send_limit", 50) or 50))
        sent_today = count_sent_today()
        remaining = max(0, limit - sent_today)
        if remaining <= 0:
            return jsonify({"success": False,
                             "message": f"Limite diário de {limit} emails já foi atingido hoje ({sent_today} enviados)."})

        rows = db_query("""
            SELECT data FROM jobs
            WHERE status = 'pending'
            AND data->>'contactEmail' != ''
            AND data->>'contactEmail' IS NOT NULL
            ORDER BY timestamp ASC
        """, fetch="all")
        pending = [r["data"] for r in rows] if rows else []
        pending = pending[:remaining]

        if not pending:
            return jsonify({"success": False, "message": "Nenhuma vaga pendente com email."})

        interval = compute_interval_seconds(config)
        ok = start_send_queue(pending, interval)
        return jsonify({
            "success": ok,
            "message": f"Fila iniciada! {len(pending)} emails serão enviados ao longo do dia "
                        f"(~1 a cada {round(interval/60,1)} min, limite diário {limit}).",
            "total": len(pending),
            "estimated_minutes": len(pending) * interval / 60
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/queue-status", methods=["GET"])
def api_queue_status():
    return jsonify({
        **_queue_status,
        "remaining": _send_queue.qsize()
    })

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
                COUNT(*) FILTER (WHERE data->>'sent_date' = %s) as sent_today,
                COUNT(*) FILTER (WHERE (data->>'opened')::boolean = true) as opened,
                COUNT(*) FILTER (WHERE data->>'contactEmail' != '' AND data->>'contactEmail' IS NOT NULL) as with_email
            FROM jobs
        """, (today, today), fetch="one")
        return jsonify(dict(row)) if row else jsonify({
            "total":0,"sent":0,"pending":0,"today":0,"sent_today":0,"opened":0,"with_email":0})
    except Exception as e:
        return jsonify({"total":0,"sent":0,"pending":0,"today":0,"sent_today":0,"opened":0,"with_email":0})

@app.route("/api/sent-log", methods=["GET"])
def api_sent_log():
    """Lista de emails enviados, para quem, e se já foram abertos — para o painel de rastreamento."""
    try:
        rows = db_query("""
            SELECT data FROM jobs
            WHERE status IN ('sent','followup') AND data->>'contactEmail' != ''
            ORDER BY (data->>'sent_at') DESC NULLS LAST
            LIMIT 300
        """, fetch="all")
        out = []
        for r in (rows or []):
            j = r["data"]
            out.append({
                "title": j.get("title"), "company": j.get("company"),
                "contactEmail": j.get("contactEmail"), "category": j.get("category"),
                "sent_date": j.get("sent_date"), "sent_at": j.get("sent_at"),
                "opened": bool(j.get("opened")), "opened_at": j.get("opened_at"),
                "status": j.get("status"),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify([])

@app.route("/api/cron/tick", methods=["GET", "POST"])
def api_cron_tick():
    """
    Endpoint para ser chamado por um cron externo (ex: cron-job.org, a cada 15-20 min).
    Isso é mais confiável que a thread interna em hospedagens que "dormem" com inatividade
    (ex: Render free tier): a própria chamada do cron acorda o serviço.
    Cada tick: busca vagas novas e envia NO MÁXIMO 1 email pendente, respeitando o
    limite diário e a janela de horário configurados. Chamadas repetidas a cada ~15 min
    resultam em ~50/dia distribuídos naturalmente ao longo do dia.
    """
    try:
        expected = os.environ.get("CRON_SECRET", "")
        if expected and request.args.get("secret", "") != expected:
            return jsonify({"success": False, "message": "unauthorized"}), 401

        config = load_config()
        jobs, _ = fetch_rss_jobs(config.get("keywords", ""), config.get("category_filter", "all"), config.get("categories"))
        added = upsert_jobs(jobs)

        now = datetime.now().strftime("%H:%M")
        start = config.get("send_window_start", "07:00") or "07:00"
        end   = config.get("send_window_end", "21:00") or "21:00"
        in_window = start <= now <= end

        note = ""
        if in_window and (config.get("email_password") or os.environ.get("GAS_WEBHOOK_URL")) and not _queue_running:
            limit = max(1, int(config.get("daily_send_limit", 50) or 50))
            sent_today = count_sent_today()
            if sent_today < limit:
                rows = db_query("""
                    SELECT data FROM jobs WHERE status='pending'
                    AND data->>'contactEmail' != '' ORDER BY timestamp ASC LIMIT 1
                """, fetch="all")
                pending = [r["data"] for r in (rows or [])]
                if pending:
                    job = pending[0]
                    subject, body, cv, cl = build_email_content(job, config)
                    tid = uuid.uuid4().hex
                    success, msg = send_email_smtp(job["contactEmail"], subject, body, cv, config, cl, tracking_id=tid)
                    if success:
                        job.update({"status": "sent",
                                    "sent_date": datetime.now().strftime("%d/%m/%Y"),
                                    "sent_at": datetime.now().isoformat(),
                                    "tracking_id": tid, "opened": False})
                        db_query("UPDATE jobs SET data = %s, status = 'sent' WHERE id = %s",
                                 (json.dumps(job), job["id"]))
                        add_log(f"✓ Cron: {job['title']} → {job['contactEmail']}", "sent")
                        note = f"Enviado: {job['title']}"
                    else:
                        add_log(f"✗ Cron erro: {job['title']} — {msg}", "error")
                        note = f"Erro ao enviar: {msg}"

        return jsonify({"success": True, "new_jobs": added, "in_window": in_window,
                        "sent_today": count_sent_today(), "note": note})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

def scheduler_loop():
    def daily():
        try:
            config = load_config()
            jobs, _ = fetch_rss_jobs(config.get("keywords",""), config.get("category_filter","all"), config.get("categories"))
            added = upsert_jobs(jobs)
            if added:
                add_log(f"Scheduler: {added} novas vagas", "found")

            if not (config.get("email_password") or os.environ.get("GAS_WEBHOOK_URL")):
                return

            limit = max(1, int(config.get("daily_send_limit", 50) or 50))
            sent_today = count_sent_today()
            remaining = max(0, limit - sent_today)
            if remaining <= 0:
                add_log(f"Scheduler: limite diário de {limit} já atingido.", "found")
                return

            rows = db_query("""
                SELECT data FROM jobs WHERE status='pending'
                AND data->>'contactEmail' != ''
                ORDER BY timestamp ASC
            """, fetch="all")
            pending = [r["data"] for r in (rows or [])][:remaining]
            if not pending:
                return
            interval = compute_interval_seconds(config)
            if start_send_queue(pending, interval):
                add_log(f"Scheduler: fila diária iniciada com {len(pending)} emails "
                        f"(~1 a cada {round(interval/60,1)} min).", "found")
        except Exception as e:
            add_log(f"Erro scheduler: {e}", "error")

    config = load_config()
    schedule.every().day.at(config.get("send_window_start", config.get("send_time","07:00"))).do(daily)
    while True:
        schedule.run_pending()
        time.sleep(60)

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
else:
    threading.Thread(target=scheduler_loop, daemon=True).start()
