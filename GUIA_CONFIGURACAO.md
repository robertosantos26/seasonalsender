# 🌿 SeasonalSender — Guia de Configuração (v2)

Este guia cobre as 3 coisas que faltam pra voltar a rodar: **banco de dados**,
**envio de email** e **deploy**. O código já está pronto — falta só configurar
as contas externas.

---

## 1) Banco de dados — Supabase (grátis)

1. Acesse https://supabase.com → crie uma conta grátis → **New Project**.
2. Escolha uma senha forte para o banco (guarde ela).
3. Espere o projeto provisionar (~2 min).
4. Vá em **Project Settings → Database → Connection string** → aba **URI**.
5. Use a opção **Transaction pooler** (porta 6543) OU **Session pooler** (porta 5432) —
   qualquer uma funciona para este app, mas o **Session pooler** é mais simples e
   evita problemas com conexões persistentes do `psycopg2.pool`. Copie a string,
   algo como:
   ```
   postgresql://postgres.xxxxxxxx:[SUA-SENHA]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
6. Substitua `[SUA-SENHA]` pela senha do passo 2.
7. Essa é a sua `DATABASE_URL`. Guarde para o passo de deploy.

O app cria as tabelas sozinho (`init_db()`) na primeira execução — não precisa rodar SQL manualmente.

---

## 2) Envio de email — por que Gmail, e não Hotmail/Outlook

**Recomendação: continue com Gmail**, pelos seguintes motivos:

- **Outlook/Hotmail é mais agressivo bloqueando remetentes novos ou de alto volume**
  vindos de servidores/relays (Render, Google Apps Script etc.) — a taxa de
  rejeição tende a ser **pior**, não melhor, do que Gmail.
- O Gmail tem um limite generoso (~500 emails/dia por conta) e o relay via
  **Google Apps Script** (que você já usa) envia literalmente pelo `GmailApp` —
  ou seja, do ponto de vista dos servidores de quem recebe, o email vem de um
  Gmail de verdade, com SPF/DKIM/DMARC do próprio Google já configurados.
  Isso é a maior vantagem: você não precisa configurar autenticação de domínio.
- O problema das rejeições anteriores era **quase certamente volume/padrão**,
  não o provedor: o código antigo mandava tudo de uma vez (1 a cada 5 min sem parar,
  sem limite diário), sem rastrear se você já tinha batido o teto do dia, e o botão
  "Ver Email" na verdade **enviava de verdade** por engano (corrigido nesta versão).
  Esse comportamento é clássico gatilho de spam-flag.

**O que mudou no código para reduzir rejeição:**
- Limite diário configurável (padrão 50) espalhado numa janela de horário (padrão 07h–21h), com variação aleatória no intervalo — imita comportamento humano em vez de robô.
- "Ver Email" agora só mostra o preview, não envia mais.
- Corpo do email agora é HTML (mais parecido com cliente de email normal).

**Se quiser ir além:** considere personalizar um pouco o corpo por categoria
(um parágrafo específico pra "horse" vs "server" etc.) — mensagens muito
genéricas em massa são o principal gatilho de spam mesmo vindo de conta legítima.

### Atualize o Google Apps Script

O `app.py` agora manda também um campo `html_body` (com o pixel de rastreamento
de abertura embutido). Seu script atual provavelmente só usa `body` como texto
puro. Abra seu projeto em https://script.google.com, e troque a função que recebe
o `doPost` por esta versão (mantém compatibilidade com o que já existia, adiciona
HTML + pixel):

```javascript
function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);

    var options = {};
    if (data.html_body) {
      options.htmlBody = data.html_body;   // versão com pixel de rastreamento
    }
    if (data.bcc) {
      options.bcc = data.bcc;
    }
    if (data.sender_name) {
      options.name = data.sender_name;
    }
    if (data.attachments && data.attachments.length > 0) {
      options.attachments = data.attachments.map(function(a) {
        return Utilities.newBlob(Utilities.base64Decode(a.content), '', a.filename);
      });
    }

    GmailApp.sendEmail(data.to, data.subject, data.body || '', options);

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, message: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
```

Depois de colar: **Deploy → Manage deployments → editar (ícone de lápis) →
New version → Deploy**. A URL do webhook não muda, então não precisa
reconfigurar o `GAS_WEBHOOK_URL` no Render.

> Se você nunca publicou esse script antes ou perdeu o link: **Deploy → New
> deployment → tipo "Web app"** → Execute as: *Me* → Who has access: *Anyone*
> → copie a URL gerada → essa é a sua `GAS_WEBHOOK_URL`.

---

## 3) Onde hospedar: Render, Vercel ou outro?

**Fique no Render.** Não migre para Vercel. Motivo técnico:

- Este app é um **Flask de processo único e contínuo**, com uma thread de
  background (scheduler) e uma fila de envio em memória (`queue.Queue`) que
  precisa ficar viva entre requisições.
- **Vercel é serverless**: cada requisição roda uma função isolada com
  timeout curto (10–60s) e **sem estado entre chamadas** — a thread do
  scheduler e a fila em memória simplesmente não sobrevivem. Você teria que
  reescrever a arquitetura inteira (fila em banco, cron da própria Vercel,
  sem `threading`), o que não vale a pena para este caso.
- Render (ou alternativas equivalentes tipo Railway/Fly.io) suporta processo
  Python de longa duração igual ao seu `Procfile`/`render.yaml` já fazem —
  é a opção certa aqui.

**Único problema real do Render**: no **plano free**, o serviço "dorme" após
~15 min sem requisições, o que mata a thread do scheduler e o envio automático
para de rodar sozinho. Duas soluções, pode usar as duas juntas:

1. **Recomendado — cron externo grátis apontando pro novo endpoint `/api/cron/tick`.**
   Isso substitui a dependência da thread interna: cada chamada acorda o serviço
   e processa até 1 email (respeitando limite diário e janela de horário).
   - Crie conta grátis em https://cron-job.org (ou https://uptimerobot.com)
   - Configure para chamar, a cada **15 minutos**:
     ```
     https://SEU-APP.onrender.com/api/cron/tick?secret=SUA_SENHA_AQUI
     ```
   - Defina a mesma senha na variável de ambiente `CRON_SECRET` no Render.
   - Isso também mantém o serviço "acordado", então a interface web abre rápido
     quando você acessa.

2. **Alternativa — plano pago Starter (~US$7/mês)**: fica sempre ativo, a thread
   interna do scheduler roda normalmente sem depender de cron externo.

Se o orçamento permitir, plano pago é mais simples e robusto. Se não, o cron
externo (grátis) resolve bem.

---

## 4) Nomeando os currículos

Na aba **Configurações → Categorias e Currículos**, cada categoria tem um campo
de nome de arquivo. Suba os PDFs correspondentes para a pasta `/curriculos/`
no repositório com esses nomes exatos, por exemplo:

```
curriculos/
  curriculo_farmworker.pdf
  curriculo_horse.pdf
  curriculo_housekeeper.pdf
  curriculo_server.pdf
  curriculo_landscaping.pdf
  curriculo_geral.pdf
```

Você pode reaproveitar o mesmo PDF em mais de uma categoria (ex: usar o currículo
agrícola tanto em "farmworker" quanto em "landscaping") — é só repetir o mesmo
nome de arquivo nos dois campos.

---

## 5) Checklist final de deploy

1. Suba os arquivos atualizados (`app.py`, `templates/index.html`, `render.yaml`) para o GitHub.
2. No painel do Render → seu serviço → **Environment** → configure:
   - `DATABASE_URL` (do Supabase, passo 1)
   - `GAS_WEBHOOK_URL` (do Apps Script, passo 2)
   - `CRON_SECRET` (uma senha qualquer, se for usar cron externo)
3. Deploy manual ou automático (se conectado ao GitHub, o Render já redeploya sozinho).
4. Acesse `/api/status` no seu app pra conferir se banco e email estão OK.
5. Vá em Configurações, preencha nome/email/telefone, confira as categorias e
   nomes de currículo, salve.
6. Configure o cron externo (passo 3 acima) se estiver no plano free.
7. Clique em "Buscar Vagas" e depois "Enviar Pendentes" para testar manualmente
   antes de deixar rodando sozinho.
