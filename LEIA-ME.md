# 🌿 SeasonalSender — Guia de Instalação

Plataforma de envio automático de candidaturas para vagas do Seasonal Jobs.

---

## ✅ PRÉ-REQUISITOS

- Python 3.9 ou superior instalado no seu computador
- Conta Gmail (recomendado para envio de emails)
- Seus currículos em PDF (um agrícola, um geral)

---

## 🚀 PASSO A PASSO DE INSTALAÇÃO

### PASSO 1 — Instalar o Python

Se ainda não tem Python instalado:
- **Windows**: Baixe em https://python.org/downloads → marque "Add to PATH" durante a instalação
- **Mac**: Já vem instalado. Se precisar atualizar: https://python.org/downloads
- **Linux**: `sudo apt install python3 python3-pip`

Para verificar: abra o terminal e digite:
```
python --version
```

---

### PASSO 2 — Extrair a pasta do SeasonalSender

Coloque a pasta `seasonalsender` em um local fixo no seu computador.
Exemplo: `C:\Users\SeuNome\seasonalsender` (Windows) ou `~/seasonalsender` (Mac/Linux)

---

### PASSO 3 — Instalar as dependências

Abra o terminal (Command Prompt no Windows ou Terminal no Mac/Linux).

Entre na pasta do projeto:
```
cd C:\Users\SeuNome\seasonalsender
```
(adapte o caminho para onde você colocou a pasta)

Instale as dependências:
```
pip install -r requirements.txt
```

Aguarde a instalação terminar.

---

### PASSO 4 — Colocar seus currículos

Dentro da pasta `seasonalsender`, crie uma pasta chamada `curriculos`:
```
mkdir curriculos
```

Coloque seus dois currículos PDF dentro dessa pasta:
- `curriculos/curriculo_agricola.pdf`   → para vagas de farm, harvest, picking, etc.
- `curriculos/curriculo_geral.pdf`      → para outros tipos de trabalho

Os nomes podem ser alterados depois na tela de Configurações.

---

### PASSO 5 — Criar uma Senha de App no Gmail

Para o sistema enviar emails pelo seu Gmail, você precisa criar uma "Senha de App":

1. Acesse: https://myaccount.google.com/apppasswords
2. Faça login na sua conta Google
3. Em "Selecionar app" escolha "Outro (nome personalizado)" → escreva "SeasonalSender"
4. Clique em "Gerar"
5. Copie a senha de 16 caracteres gerada (ex: abcd efgh ijkl mnop)

⚠️ Guarde essa senha! Você vai precisar dela na Configuração.

---

### PASSO 6 — Iniciar a plataforma

No terminal, dentro da pasta do projeto, execute:
```
python app.py
```

Você verá a mensagem:
```
🌿 SeasonalSender rodando em http://localhost:5000
```

---

### PASSO 7 — Acessar a plataforma

Abra seu navegador e acesse:
```
http://localhost:5000
```

A plataforma estará funcionando!

---

### PASSO 8 — Configurar seus dados

1. Clique na aba **Configurações**
2. Preencha:
   - **Nome completo**: seu nome
   - **Email**: seu Gmail (ex: joao@gmail.com)
   - **Senha do email**: a Senha de App criada no Passo 5 (16 caracteres)
   - **Telefone**: seu número com código do país
   - **Currículos**: confirme os nomes dos arquivos
3. Personalize o modelo de email se quiser
4. Clique em **Salvar Configurações**

---

### PASSO 9 — Buscar vagas e enviar

1. Clique em **Buscar Vagas** (botão no topo)
2. Defina palavras-chave e país
3. Clique em **Buscar Agora**
4. As vagas serão listadas na aba **Vagas**
5. Para enviar todos os pendentes de uma vez: clique **Enviar Pendentes**
6. Para enviar individualmente: clique **Enviar** em cada vaga

---

## ⏰ ENVIO AUTOMÁTICO DIÁRIO

O sistema já vem configurado para buscar e enviar emails automaticamente.

Para funcionar, o programa precisa estar rodando (`python app.py`).

**Para iniciar automaticamente com o Windows:**
1. Pressione `Win + R`, escreva `shell:startup`, pressione Enter
2. Crie um arquivo `iniciar_seasonal.bat` com o conteúdo:
```
cd C:\Users\SeuNome\seasonalsender
python app.py
```
3. Coloque esse arquivo na pasta que abriu

---

## 📁 ESTRUTURA DE ARQUIVOS

```
seasonalsender/
├── app.py              ← Programa principal
├── requirements.txt    ← Dependências
├── config.json         ← Suas configurações (criado automaticamente)
├── jobs_data.json      ← Banco de dados de vagas (criado automaticamente)
├── curriculos/
│   ├── curriculo_agricola.pdf    ← SEU CURRÍCULO AGRÍCOLA
│   └── curriculo_geral.pdf       ← SEU CURRÍCULO GERAL
└── templates/
    └── index.html      ← Interface da plataforma
```

---

## ❓ PROBLEMAS COMUNS

**"python não é reconhecido"**
→ Reinstale o Python marcando "Add to PATH"

**"No module named flask"**
→ Execute novamente: `pip install -r requirements.txt`

**Email não está sendo enviado**
→ Verifique se a Senha de App está correta (16 caracteres sem espaços)
→ Verifique se a verificação em 2 etapas está ativa no Gmail

**Site não abre**
→ Certifique-se que `python app.py` está rodando
→ Tente: http://127.0.0.1:5000

---

## 📞 SUPORTE

Se tiver dúvidas, copie a mensagem de erro e peça ajuda ao Claude!
