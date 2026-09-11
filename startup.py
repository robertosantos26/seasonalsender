import os, json, base64, uuid
from flask import render_template, request, jsonify
from psycopg2 import Binary

import app as main


def ensure_tables():
    main.init_db()
    main.db_query('''CREATE TABLE IF NOT EXISTS attachments (
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        content BYTEA NOT NULL,
        content_type TEXT DEFAULT 'application/pdf',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(category, kind)
    )''')


def category_for(config, cat_id):
    for c in config.get('categories', main.DEFAULT_CATEGORIES):
        if c.get('id') == cat_id:
            return c
    return None


def build_email_content(job, config):
    cat = category_for(config, job.get('category', 'general')) or {}
    company = job.get('company') or 'the company'
    subject = cat.get('subject') or config.get('email_subject', 'Application for {job_title} - {your_name}')
    message = cat.get('message') or config.get('email_body', '')
    replacements = {
        '{job_title}': job.get('title', ''),
        '{company}': company,
        '{your_name}': config.get('name', ''),
        '{your_phone}': config.get('phone', ''),
    }
    for old, new in replacements.items():
        subject = subject.replace(old, str(new))
        message = message.replace(old, str(new))
    return subject, message, cat.get('cv', ''), cat.get('cover_letter', '')


def read_attachment(folder, filename):
    if not filename:
        return None
    row = main.db_query(
        'SELECT content FROM attachments WHERE filename=%s ORDER BY created_at DESC LIMIT 1',
        (filename,), fetch='one')
    if row and row.get('content') is not None:
        return base64.b64encode(bytes(row['content'])).decode()
    return main.read_attachment(folder, filename)


# Replace the functions used by the existing queue without rewriting app.py.
main.build_email_content = build_email_content
main.read_attachment = read_attachment

app = main.app

@app.route('/configuracoes')
def configuracoes():
    return render_template('configuracoes.html')

@app.route('/api/web-config', methods=['GET'])
def web_config_get():
    ensure_tables()
    cfg = main.load_config()
    safe = dict(cfg)
    safe.pop('email_password', None)
    safe['has_password'] = bool(cfg.get('email_password'))
    rows = main.db_query('SELECT category, kind, filename FROM attachments ORDER BY category, kind', fetch='all') or []
    safe['attachments'] = [dict(r) for r in rows]
    return jsonify(safe)

@app.route('/api/web-config', methods=['POST'])
def web_config_save():
    try:
        ensure_tables()
        data = request.json or {}
        current = main.load_config()
        for key in ('name','email','email_password','smtp_server','smtp_port','phone','send_time','keywords','category_filter','daily_send_limit','send_window_start','send_window_end','followup_subject','followup_body'):
            if key in data:
                if key == 'email_password' and data[key] in ('', '••••••••'):
                    continue
                current[key] = data[key]
        if isinstance(data.get('categories'), list):
            current['categories'] = data['categories']
        ok = main.save_config(current)
        return jsonify({'success': ok, 'message': 'Configurações salvas.' if ok else 'Erro ao salvar.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/upload-curriculo', methods=['POST'])
def upload_curriculo():
    try:
        ensure_tables()
        file = request.files.get('file')
        category = (request.form.get('category') or '').strip()
        kind = (request.form.get('kind') or 'cv').strip()
        valid = {c.get('id') for c in main.DEFAULT_CATEGORIES}
        if not file: return jsonify({'success': False, 'message': 'Selecione um PDF.'}), 400
        if category not in valid: return jsonify({'success': False, 'message': 'Categoria inválida.'}), 400
        if kind not in ('cv', 'cover_letter'): return jsonify({'success': False, 'message': 'Tipo de arquivo inválido.'}), 400
        filename = os.path.basename(file.filename or 'arquivo.pdf')
        if not filename.lower().endswith('.pdf'): return jsonify({'success': False, 'message': 'Somente PDF é aceito.'}), 400
        content = file.read()
        if len(content) > 8 * 1024 * 1024: return jsonify({'success': False, 'message': 'O arquivo deve ter no máximo 8 MB.'}), 400
        stored = ('curriculo_' if kind == 'cv' else 'cover_') + category + '.pdf'
        main.db_query('''INSERT INTO attachments(id,category,kind,filename,content,content_type)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(category,kind) DO UPDATE SET filename=EXCLUDED.filename, content=EXCLUDED.content,
            content_type=EXCLUDED.content_type, created_at=NOW()''',
            (f'{category}_{kind}', category, kind, stored, Binary(content), file.mimetype or 'application/pdf'))
        cfg = main.load_config()
        cat = category_for(cfg, category)
        if cat:
            cat['cv' if kind == 'cv' else 'cover_letter'] = stored
            main.save_config(cfg)
        return jsonify({'success': True, 'filename': stored, 'message': 'Arquivo salvo no Supabase.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/test-email', methods=['POST'])
def test_email():
    try:
        ensure_tables()
        cfg = main.load_config()
        target = (request.json or {}).get('to') or cfg.get('email')
        if not target: return jsonify({'success': False, 'message': 'Informe um e-mail para teste.'}), 400
        fake = {'title': 'TEST POSITION', 'company': 'Test Company', 'category': 'general', 'contactEmail': target}
        subject, body, cv, cl = build_email_content(fake, cfg)
        ok, msg = main.send_email_smtp(target, '[TESTE] ' + subject, body, cv, cfg, cl)
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

try:
    ensure_tables()
except Exception as exc:
    print(f'Banco ainda não disponível no startup: {exc}')
