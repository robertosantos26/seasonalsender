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


_orig_read_attachment = main.read_attachment


def category_for(config, cat_id):
    for c in config.get('categories', main.DEFAULT_CATEGORIES):
        if c.get('id') == cat_id:
            return c
    return None


def attachment_filename(category, kind):
    try:
        row = main.db_query(
            'SELECT filename FROM attachments WHERE category=%s AND kind=%s ORDER BY created_at DESC LIMIT 1',
            (category, kind), fetch='one')
        return row.get('filename', '') if row else ''
    except Exception:
        return ''


def build_email_content(job, config):
    cat_id = job.get('category', 'general')
    cat = category_for(config, cat_id) or {}
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

    # O anexo agora vem diretamente da tabela attachments.
    # Assim o envio nao depende do config.json/config do banco ter salvo o nome do PDF.
    cv = attachment_filename(cat_id, 'cv')
    cover_letter = attachment_filename(cat_id, 'cover_letter')

    # Compatibilidade com configuracoes antigas.
    if not cv:
        cv = cat.get('cv', '')
    if not cover_letter:
        cover_letter = cat.get('cover_letter', '')

    # Cartas de referência aplicáveis a esta categoria vão "penduradas" no nome da
    # cover letter (str com atributo extra), sem estado global.
    cover_letter = _Files(cover_letter or '')
    cover_letter.extra_kinds = tuple(reference_kinds_for(config, cat_id))

    return subject, message, cv, cover_letter


def read_attachment(folder, filename, config=None):
    if not filename:
        return None

    row = main.db_query(
        'SELECT content FROM attachments WHERE filename=%s ORDER BY created_at DESC LIMIT 1',
        (filename,), fetch='one')

    if row and row.get('content') is not None:
        return base64.b64encode(bytes(row['content'])).decode()

    return _orig_read_attachment(folder, filename, config)


# ── Cartas de referência ───────────────────────────────────
REF_CATEGORY = 'references'      # valor da coluna category na tabela attachments
REF_MAX_SLOTS = 5
REF_MAX_FILE = 5 * 1024 * 1024   # 5 MB por carta
REF_MAX_TOTAL = 12 * 1024 * 1024 # 12 MB no total (limite do Gmail é 25 MB)


class _Files(str):
    """Nome da cover letter + lista de cartas de referência a anexar (extra_kinds)."""
    extra_kinds = ()


def _ref_rows():
    return main.db_query(
        'SELECT kind, filename, length(content) AS size FROM attachments '
        'WHERE category=%s ORDER BY kind', (REF_CATEGORY,), fetch='all') or []


def reference_kinds_for(config, cat_id):
    """Quais cartas (kind) vão neste e-mail. Sem cartas enviadas = lista vazia."""
    if not config.get('attach_references', True):
        return []
    apply_map = config.get('reference_apply') or {}
    out = []
    for r in _ref_rows():
        slot = r['kind'].replace('reference_', '')
        cats = apply_map.get(slot) or ['all']
        if 'all' in cats or cat_id in cats:
            out.append(r['kind'])
    return out


def _merge_config(patch):
    """Atualiza só estas chaves da config (merge atômico no banco)."""
    main.db_query(
        "INSERT INTO config (key, value) VALUES ('main', %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = config.value || EXCLUDED.value",
        (json.dumps(patch),))


def send_email_smtp(to_email, subject, body, cv_file, config, cover_letter_file="", tracking_id=None):
    """Envia via relay do Google Apps Script (igual ao app.py) e, além do currículo e da
    cover letter, anexa as cartas de referência. Substitui main.send_email_smtp."""
    gas_url = os.environ.get("GAS_WEBHOOK_URL", "")
    sender_email = config.get("email", "")
    sender_name = config.get("name", "SeasonalSender")
    if not gas_url:
        return False, "GAS_WEBHOOK_URL não configurada. Siga as instruções de configuração."
    if not sender_email:
        return False, "Email não configurado em Configurações."

    html_body = body.replace("\n", "<br>")
    base_url = main.get_base_url(config)
    if tracking_id and base_url:
        html_body += (f'<img src="{base_url}/api/track/{tracking_id}.gif" width="1" height="1" '
                      f'style="display:none" alt="">')

    payload = {"to": to_email, "subject": subject, "body": body, "html_body": html_body,
               "bcc": sender_email, "sender_name": sender_name}

    attachments = []
    for fname in [cv_file, cover_letter_file]:
        if fname:
            data = read_attachment("curriculos", str(fname), config)
            if data:
                attachments.append({"filename": main.display_attachment_name(str(fname), config),
                                    "content": data})

    # Cartas de referência: se deveriam ir e não puderem ser lidas, NÃO envia sem elas.
    for kind in getattr(cover_letter_file, "extra_kinds", ()):
        try:
            row = main.db_query(
                'SELECT filename, content FROM attachments WHERE category=%s AND kind=%s',
                (REF_CATEGORY, kind), fetch='one')
        except Exception as e:
            return False, f"Erro ao ler carta de referência ({kind}): {e}"
        if not row or row.get('content') is None:
            return False, f"Carta de referência não encontrada ({kind}). Reenvie o arquivo."
        attachments.append({
            "filename": main.display_attachment_name(row['filename'], config),
            "content": base64.b64encode(bytes(row['content'])).decode()})

    if attachments:
        payload["attachments"] = attachments

    try:
        resp = main.requests.post(gas_url, json=payload, timeout=60)
        data = resp.json() if resp.content else {}
        if data.get("ok"):
            return True, "Enviado"
        return False, data.get("message", f"Erro {resp.status_code}")
    except Exception as e:
        return False, str(e)


main.send_email_smtp = send_email_smtp


# Substitui as funcoes usadas pela fila de envio sem precisar reescrever app.py.
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

    rows = main.db_query(
        'SELECT category, kind, filename FROM attachments ORDER BY category, kind',
        fetch='all') or []
    safe['attachments'] = [dict(r) for r in rows]

    # Faz a tela mostrar os arquivos realmente salvos no banco.
    categories = [dict(c) for c in safe.get('categories', main.DEFAULT_CATEGORIES)]
    for cat in categories:
        cat_id = cat.get('id')
        cv = attachment_filename(cat_id, 'cv')
        cover = attachment_filename(cat_id, 'cover_letter')
        if cv:
            cat['cv'] = cv
        if cover:
            cat['cover_letter'] = cover
    safe['categories'] = categories

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
        return jsonify({
            'success': ok,
            'message': 'Configurações salvas.' if ok else 'Erro ao salvar.'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/upload-curriculo', methods=['POST'])
def upload_curriculo():
    try:
        ensure_tables()

        file = request.files.get('file')
        category = (request.form.get('category') or '').strip()
        kind = (request.form.get('kind') or 'cv').strip()

        if not file:
            return jsonify({'success': False, 'message': 'Selecione um PDF.'}), 400

        if kind not in ('cv', 'cover_letter'):
            return jsonify({'success': False, 'message': 'Tipo de arquivo inválido.'}), 400

        filename = os.path.basename(file.filename or 'arquivo.pdf')
        if not filename.lower().endswith('.pdf'):
            return jsonify({'success': False, 'message': 'Somente PDF é aceito.'}), 400

        content = file.read()
        if len(content) > 8 * 1024 * 1024:
            return jsonify({'success': False, 'message': 'O arquivo deve ter no máximo 8 MB.'}), 400

        # Aceita categorias padrao e categorias criadas pela tela.
        cfg = main.load_config()
        cat = category_for(cfg, category)
        if not cat:
            return jsonify({'success': False, 'message': f'Categoria inválida: {category}'}), 400

        stored = f"{main.safe_display_name(cfg.get('name'))}{category}_{kind}.pdf"

        main.db_query('''INSERT INTO attachments(id,category,kind,filename,content,content_type)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(category,kind) DO UPDATE SET
                filename=EXCLUDED.filename,
                content=EXCLUDED.content,
                content_type=EXCLUDED.content_type,
                created_at=NOW()''',
            (
                f'{category}_{kind}',
                category,
                kind,
                stored,
                Binary(content),
                file.mimetype or 'application/pdf'
            ))

        # Mantem o config atualizado, mas o envio nao depende disso.
        try:
            cat['cv' if kind == 'cv' else 'cover_letter'] = stored
            main.save_config(cfg)
        except Exception as save_error:
            print(f'PDF salvo no banco, mas nao foi possivel atualizar config: {save_error}')

        return jsonify({
            'success': True,
            'filename': stored,
            'message': 'Currículo salvo com sucesso.'
        })

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/test-email', methods=['POST'])
def test_email():
    try:
        ensure_tables()
        cfg = main.load_config()
        target = (request.json or {}).get('to') or cfg.get('email')

        if not target:
            return jsonify({'success': False, 'message': 'Informe um e-mail para teste.'}), 400

        fake = {
            'title': 'TEST POSITION',
            'company': 'Test Company',
            'category': 'general',
            'contactEmail': target
        }

        subject, body, cv, cl = build_email_content(fake, cfg)
        ok, msg = main.send_email_smtp(target, '[TESTE] ' + subject, body, cv, cfg, cl)

        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/references', methods=['GET'])
def references_get():
    try:
        ensure_tables()
        cfg = main.load_config()
        apply_map = cfg.get('reference_apply') or {}
        slots = []
        by_slot = {r['kind'].replace('reference_', ''): r for r in _ref_rows()}
        for n in range(1, REF_MAX_SLOTS + 1):
            r = by_slot.get(str(n))
            slots.append({'slot': n,
                          'filename': r['filename'] if r else '',
                          'size_kb': round(r['size'] / 1024) if r else 0,
                          'categories': apply_map.get(str(n)) or ['all']})
        cats = [{'id': c.get('id'), 'label': c.get('label', c.get('id'))}
                for c in cfg.get('categories', main.DEFAULT_CATEGORIES)]
        return jsonify({'success': True, 'enabled': bool(cfg.get('attach_references', True)),
                        'slots': slots, 'categories': cats})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/references/upload', methods=['POST'])
def references_upload():
    try:
        ensure_tables()
        file = request.files.get('file')
        try:
            slot = int(request.form.get('slot', ''))
        except ValueError:
            slot = 0
        if not file or not (1 <= slot <= REF_MAX_SLOTS):
            return jsonify({'success': False, 'message': 'Selecione um PDF e uma posição válida.'}), 400
        content = file.read()
        if not content.startswith(b'%PDF'):
            return jsonify({'success': False, 'message': 'Somente PDF é aceito.'}), 400
        if len(content) > REF_MAX_FILE:
            return jsonify({'success': False, 'message': 'Cada carta deve ter no máximo 5 MB.'}), 400
        kind = f'reference_{slot}'
        others = sum(r['size'] for r in _ref_rows() if r['kind'] != kind)
        if others + len(content) > REF_MAX_TOTAL:
            return jsonify({'success': False,
                            'message': 'Total das cartas passa de 12 MB (o Gmail limita a 25 MB por e-mail).'}), 400
        cfg = main.load_config()
        stored = f"{main.safe_display_name(cfg.get('name'))}Reference_Letter_{slot}.pdf"
        main.db_query(
            """INSERT INTO attachments(id,category,kind,filename,content,content_type)
               VALUES(%s,%s,%s,%s,%s,'application/pdf')
               ON CONFLICT(category,kind) DO UPDATE SET filename=EXCLUDED.filename,
                 content=EXCLUDED.content, created_at=NOW()""",
            (f'{REF_CATEGORY}_{kind}', REF_CATEGORY, kind, stored, Binary(content)))
        return jsonify({'success': True, 'filename': stored, 'message': 'Carta salva.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/references/delete', methods=['POST'])
def references_delete():
    try:
        ensure_tables()
        slot = int((request.json or {}).get('slot', 0))
        main.db_query('DELETE FROM attachments WHERE category=%s AND kind=%s',
                      (REF_CATEGORY, f'reference_{slot}'))
        apply_map = dict(main.load_config().get('reference_apply') or {})
        if apply_map.pop(str(slot), None) is not None:
            _merge_config({'reference_apply': apply_map})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/references/settings', methods=['POST'])
def references_settings():
    """Corpo: {enabled: bool, apply: {"1": ["all"] | ["server","general"], ...}} (ambos opcionais)."""
    try:
        ensure_tables()
        data = request.json or {}
        patch = {}
        if 'enabled' in data:
            patch['attach_references'] = bool(data['enabled'])
        if isinstance(data.get('apply'), dict):
            current = dict(main.load_config().get('reference_apply') or {})
            for slot, cats in data['apply'].items():
                if str(slot).isdigit() and isinstance(cats, list):
                    current[str(slot)] = [str(c) for c in cats] or ['all']
            patch['reference_apply'] = current
        if patch:
            _merge_config(patch)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/references/test', methods=['POST'])
def references_test():
    """Envia para o próprio e-mail um teste idêntico a um envio real da categoria escolhida."""
    try:
        ensure_tables()
        cfg = main.load_config()
        target = cfg.get('email')
        if not target:
            return jsonify({'success': False, 'message': 'Salve seu e-mail em Configurações.'}), 400
        category = (request.json or {}).get('category') or 'general'
        fake = {'title': 'TEST POSITION', 'company': 'Test Company',
                'category': category, 'contactEmail': target}
        subject, body, cv, cl = build_email_content(fake, cfg)
        ok, msg = send_email_smtp(target, '[TESTE] ' + subject, body, cv, cfg, cl)
        names = [str(x) for x in (cv, cl) if x] + list(getattr(cl, 'extra_kinds', ()))
        return jsonify({'success': ok, 'message': msg, 'anexos': names})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


try:
    ensure_tables()
except Exception as exc:
    print(f'Banco ainda não disponível no startup: {exc}')
