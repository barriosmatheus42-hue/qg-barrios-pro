"""
Setup do GitHub Gist para persistência do QG Barrios PRO.

O que faz:
  1. Cria um Gist privado com o arquivo banco_barrios.json
  2. Migra os dados existentes do JSONBin para o novo Gist
  3. Imprime os secrets que você deve colar no Streamlit Cloud

Uso:
    python setup_gist.py ghp_SEU_TOKEN_AQUI

O token precisa ter o escopo 'gist'.
Crie em: https://github.com/settings/tokens/new
  - Note: "QG Barrios Gist"
  - Expiration: No expiration
  - Scopes: [x] gist
"""

import sys
import json
import requests

# ── Argumentos ─────────────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

GITHUB_TOKEN = sys.argv[1].strip()
FILENAME     = "banco_barrios.json"

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github.v3+json",
    "Content-Type":  "application/json",
}

print("=" * 60)
print("SETUP GITHUB GIST — QG Barrios PRO")
print("=" * 60)

# ── 1. Valida token ─────────────────────────────────────────────────────────
print("\n[1] Validando token GitHub...")
r = requests.get("https://api.github.com/user", headers=HEADERS, timeout=10)
if r.status_code != 200:
    print(f"ERRO: token inválido ou sem permissão (HTTP {r.status_code})")
    print(f"  Resposta: {r.text[:200]}")
    sys.exit(1)
user = r.json().get("login", "?")
print(f"    OK — autenticado como: {user}")

# ── 2. Lê dados atuais do JSONBin (para migrar) ─────────────────────────────
dados_migrar = {}
print("\n[2] Tentando ler dados do JSONBin para migração...")
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

if tomllib:
    import os
    SECRETS_PATH = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
    try:
        with open(SECRETS_PATH, "rb") as f:
            secrets = tomllib.load(f)
        jb_key = secrets.get("JSONBIN_KEY", "")
        jb_id  = secrets.get("JSONBIN_BIN_ID", "")
        if jb_key and jb_id:
            r_jb = requests.get(
                f"https://api.jsonbin.io/v3/b/{jb_id}/latest",
                headers={"X-Master-Key": jb_key},
                timeout=15,
            )
            if r_jb.status_code == 200:
                dados_migrar = r_jb.json().get("record", {}) or {}
                pl_str = json.dumps(dados_migrar)
                print(f"    OK — {len(pl_str)/1024:.1f} KB lidos do JSONBin")
                if "params_ligas" in dados_migrar:
                    print(f"    Ligas no JSONBin: {len(dados_migrar['params_ligas'])}")
            else:
                print(f"    Aviso: JSONBin retornou HTTP {r_jb.status_code} — começando Gist vazio")
        else:
            print("    Sem credenciais JSONBin — Gist começará vazio")
    except Exception as e:
        print(f"    Aviso: não foi possível ler JSONBin ({e}) — Gist começará vazio")
else:
    print("    tomllib/tomli não disponível — Gist começará vazio")

conteudo_inicial = json.dumps(dados_migrar) if dados_migrar else "{}"

# ── 3. Cria o Gist ──────────────────────────────────────────────────────────
print("\n[3] Criando Gist privado...")
payload_gist = {
    "description": "QG Barrios PRO — banco de dados (banca, picks, calibrações)",
    "public": False,
    "files": {
        FILENAME: {"content": conteudo_inicial}
    }
}
r_create = requests.post(
    "https://api.github.com/gists",
    headers=HEADERS,
    json=payload_gist,
    timeout=30,
)
if r_create.status_code != 201:
    print(f"ERRO ao criar Gist: HTTP {r_create.status_code}")
    print(f"  Resposta: {r_create.text[:300]}")
    sys.exit(1)

gist = r_create.json()
GIST_ID  = gist["id"]
GIST_URL = gist["html_url"]
print(f"    OK — Gist criado!")
print(f"    ID  : {GIST_ID}")
print(f"    URL : {GIST_URL}")
print(f"    Dados migrados: {len(conteudo_inicial)/1024:.1f} KB")

# ── 4. Instruções finais ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("PROXIMOS PASSOS")
print("=" * 60)
print()
print("Cole estes dois secrets no Streamlit Cloud:")
print("  App → Settings → Secrets")
print()
print("─" * 40)
print(f'GITHUB_TOKEN = "{GITHUB_TOKEN}"')
print(f'GIST_ID      = "{GIST_ID}"')
print("─" * 40)
print()
print("Pode MANTER os secrets do JSONBin — o app usará o Gist")
print("automaticamente quando GITHUB_TOKEN + GIST_ID estiverem presentes.")
print()
print("Após salvar os secrets, o Streamlit Cloud reinicia e")
print("passa a usar o GitHub Gist como backend. Sem limite de tamanho!")
print()
print("=" * 60)
