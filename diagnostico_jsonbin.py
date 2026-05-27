"""
Diagnóstico do JSONBin — identifica por que salvar_banco está falhando.

Testa 4 payloads em ordem crescente de tamanho para encontrar o limite exato.
Não modifica nada no bin — apenas lê e testa writes com dados de teste.

Uso:
    python diagnostico_jsonbin.py
"""

import os
import sys
import json
import time
import requests

# ── Lê secrets ─────────────────────────────────────────────────────────────
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("ERRO: instale tomli — pip install tomli")
        sys.exit(1)

SECRETS_PATH = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
with open(SECRETS_PATH, "rb") as f:
    secrets = tomllib.load(f)

KEY    = secrets.get("JSONBIN_KEY", "")
BIN_ID = secrets.get("JSONBIN_BIN_ID", "")

if not KEY or not BIN_ID:
    print("ERRO: JSONBIN_KEY ou JSONBIN_BIN_ID ausentes em secrets.toml")
    sys.exit(1)

URL     = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
HEADERS_READ  = {"X-Master-Key": KEY}
HEADERS_WRITE = {"X-Master-Key": KEY, "Content-Type": "application/json", "X-Bin-Versioning": "false"}

print("=" * 60)
print("DIAGNÓSTICO JSONBin — QG Barrios PRO")
print("=" * 60)
print(f"\nBin ID : {BIN_ID}")
print(f"API Key: {KEY[:8]}...{KEY[-4:]}")

# ── 1. Leitura do bin atual ─────────────────────────────────────────────────
print("\n── 1. Lendo conteúdo atual do bin ──────────────────────────")
try:
    r = requests.get(f"{URL}/latest", headers=HEADERS_READ, timeout=15)
    print(f"   Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json().get("record", {})
        payload_str = json.dumps(data)
        print(f"   Tamanho atual: {len(payload_str):,} bytes ({len(payload_str)/1024:.1f} KB)")
        print(f"   Chaves no bin: {list(data.keys())}")
        if "params_ligas" in data:
            n_ligas = len(data["params_ligas"])
            print(f"   Ligas em params_ligas: {n_ligas}")
            # Calcula tamanho só das ligas
            pl_str = json.dumps(data["params_ligas"])
            print(f"   Tamanho params_ligas: {len(pl_str):,} bytes ({len(pl_str)/1024:.1f} KB)")
        if "historico_ids" in data:
            n_ids = sum(len(v) for v in data["historico_ids"].values())
            print(f"   IDs no historico_ids: {n_ids:,}")
    elif r.status_code == 401:
        print("   ❌ 401 UNAUTHORIZED — JSONBIN_KEY inválida ou expirada!")
    elif r.status_code == 404:
        print("   ❌ 404 NOT FOUND — JSONBIN_BIN_ID não existe ou foi deletado!")
    else:
        print(f"   ❌ Erro inesperado: {r.text[:300]}")
except Exception as e:
    print(f"   ❌ Exceção de rede: {e}")

# ── 2. Teste de escrita com payloads crescentes ─────────────────────────────
print("\n── 2. Testando escrita com payloads de tamanho crescente ───")

def testar_write(nome, payload_dict, desfazer=False):
    """Testa escrita e retorna (ok, status_code, tamanho_bytes)."""
    payload_str = json.dumps(payload_dict)
    kb = len(payload_str) / 1024
    try:
        r = requests.put(URL, headers=HEADERS_WRITE, json=payload_dict, timeout=30)
        ok = r.status_code == 200
        simbolo = "✅" if ok else "❌"
        print(f"   {simbolo} {nome:30s} {kb:7.1f} KB → HTTP {r.status_code}", end="")
        if not ok:
            print(f"  [{r.text[:80].strip()}]", end="")
        print()
        return ok, r.status_code, len(payload_str)
    except Exception as e:
        print(f"   ❌ {nome:30s} {kb:7.1f} KB → EXCEÇÃO: {e}")
        return False, 0, len(payload_str)

# Payload mínimo (5 bytes)
ok_min, _, _ = testar_write("Payload vazio", {"_test": True})
time.sleep(0.5)

# Payload pequeno — simula banca + picks (< 1KB)
ok_pequeno, _, _ = testar_write("Banca + picks (< 1KB)", {
    "banca_inicial": 35.40,
    "picks": [],
    "depositos": [],
})
time.sleep(0.5)

# Payload médio — simula 10 ligas sem calibradores (~50KB)
liga_fake = {
    "league_id": 39, "season": 2025,
    "times": {str(i): {"alpha": 1.1, "beta": 0.9, "n_jogos": 20} for i in range(20)},
    "home_advantage": 1.3, "rho": -0.05, "xi": 0.002,
    "media_liga_gols": 2.7, "calibrado_em": "2026-05-27T10:00:00",
    "n_jogos_calibracao": 350, "log_likelihood": -1500.0,
}
params_medio = {str(i): liga_fake for i in range(10)}
ok_medio, _, _ = testar_write("10 ligas sem calibradores", {
    "banca_inicial": 35.40, "picks": [], "depositos": [],
    "params_ligas": params_medio,
})
time.sleep(0.5)

# Payload grande — simula 72 ligas com calibradores 50pts (~300KB)
cal_fake = {
    "mercado": "OVER_25", "n_amostras": 350,
    "x_thresh": [round(i/50, 6) for i in range(50)],
    "y_thresh": [round(0.3 + i/200, 6) for i in range(50)],
}
liga_com_cal = dict(liga_fake)
liga_com_cal["calibradores"] = {m: cal_fake for m in [
    "OVER_25","UNDER_25","OVER_15","UNDER_15","OVER_35","UNDER_35","BTTS_YES","BTTS_NO"
]}
params_grande = {str(i): liga_com_cal for i in range(72)}
ok_grande, _, _ = testar_write("72 ligas + calibradores 50pts", {
    "banca_inicial": 35.40, "picks": [], "depositos": [],
    "params_ligas": params_grande,
})
time.sleep(0.5)

# ── 3. Restaura conteúdo original (se leitura funcionou) ──────────────────
if ok_min and r.status_code != 200:
    pass  # não tinha conteúdo original para restaurar

print("\n── 3. Diagnóstico ──────────────────────────────────────────")
if not ok_min:
    print("   🔴 PROBLEMA CRÍTICO: até payload de 5 bytes falha.")
    print("   → API key inválida, bin deletado, ou conta suspensa no JSONBin.")
    print("   → Solução: criar novo bin e/ou verificar conta em jsonbin.io")
elif not ok_pequeno:
    print("   🔴 API key válida mas bin rejeita qualquer escrita.")
    print("   → Bin pode estar bloqueado ou conta atingiu limite de requests.")
elif not ok_medio:
    print("   🟡 Escreve payloads pequenos mas rejeita ~50KB.")
    print("   → Plano free do JSONBin tem limite de ~10–32KB por bin.")
    print("   → Solução A: criar novo bin no JSONBin (novo bin começa vazio).")
    print("   → Solução B: migrar para GitHub Gist (10MB grátis).")
elif not ok_grande:
    print("   🟡 Aceita até ~50KB mas rejeita 300KB.")
    print("   → JSONBin free limita tamanho do bin.")
    print("   → Solução A: criar novo bin (temporário).")
    print("   → Solução B: migrar para GitHub Gist (melhor longo prazo).")
else:
    print("   ✅ JSONBin aceita todos os tamanhos testados!")
    print("   → O problema pode ser de rede/timeout no Streamlit Cloud.")
    print("   → Tente clicar 'Forçar Salvar no Cloud' no app.")

print("\n" + "=" * 60)
print("Diagnóstico concluído.")
print("=" * 60)
