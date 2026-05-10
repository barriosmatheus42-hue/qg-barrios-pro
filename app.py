import streamlit as st
import requests
import datetime
import time
import math
import json
import os
import google.generativeai as genai
import re

st.set_page_config(page_title="QG Barrios PRO", layout="wide", page_icon="⚽")

# ==========================================
# 0. CONFIGURAÇÕES E CHAVES
# ==========================================
API_KEY_PRO = "00374ab0590422053c950ddc399a0ccb"
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {'x-apisports-key': API_KEY_PRO}

JSONBIN_KEY = st.secrets["JSONBIN_KEY"]
JSONBIN_BIN_ID = st.secrets["JSONBIN_BIN_ID"]
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_HEADERS = {
    "X-Master-Key": JSONBIN_KEY,
    "Content-Type": "application/json"
}

API_KEY_GEMINI = st.secrets["GEMINI_API_KEY"]
genai.configure(api_key=API_KEY_GEMINI)

PESOS_LIGAS = {
    39: 1.0, 140: 0.95, 135: 0.95, 78: 0.95, 61: 0.95,
    71: 0.85, 72: 0.80, 73: 0.70,
    2: 1.0, 3: 0.90,
}

def conectar_modelo_ia():
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            if 'flash' in m.name.lower():
                return genai.GenerativeModel(m.name)
    return genai.GenerativeModel('gemini-pro')

model_ia = conectar_modelo_ia()

# ==========================================
# 1. GERENCIAMENTO DO BANCO
# ==========================================
ARQUIVO_BANCO = "banco_barrios_pro.json"

def carregar_banco():
    if "banco_local" in st.session_state:
        return st.session_state["banco_local"]
    banco = {"datas": {}, "creditos_restantes": 7500, "picks": [], "banca_inicial": 30.0}
    if os.path.exists(ARQUIVO_BANCO):
        try:
            with open(ARQUIVO_BANCO, "r") as f:
                banco_lido = json.load(f)
                if "datas" in banco_lido:
                    banco["datas"] = banco_lido["datas"]
        except: pass
    try:
        res = requests.get(f"{JSONBIN_URL}/latest", headers=JSONBIN_HEADERS, timeout=10)
        if res.status_code == 200:
            banco_nuvem = res.json().get("record", {})
            banco["picks"] = banco_nuvem.get("picks", [])
            banco["banca_inicial"] = banco_nuvem.get("banca_inicial", 30.0)
    except: pass
    st.session_state["banco_local"] = banco
    return banco

def salvar_banco(dados):
    st.session_state["banco_local"] = dados
    try:
        with open(ARQUIVO_BANCO, "w") as f:
            json.dump(dados, f)
    except: pass
    try:
        dados_nuvem = {
            "banca_inicial": dados.get("banca_inicial", 30.0),
            "picks": dados.get("picks", [])
        }
        headers_put = JSONBIN_HEADERS.copy()
        headers_put["X-Bin-Versioning"] = "false"
        requests.put(JSONBIN_URL, headers=headers_put, json=dados_nuvem, timeout=10)
    except: pass

banco_local = carregar_banco()

def atualizar_saldo_realtime():
    try:
        res = requests.get(f"{BASE_URL}/status", headers=HEADERS, timeout=5).json()
        if res.get('response'):
            rem = res['response']['requests']['limit_day'] - res['response']['requests']['current']
            banco_local["creditos_restantes"] = rem
            return rem
    except: pass
    return banco_local.get("creditos_restantes", 0) or 0

# ==========================================
# 2. MOTOR MATEMÁTICO — POISSON + DIXON-COLES
# ==========================================
def calcular_poisson(media_casa, media_fora):
    if media_casa <= 0.05 and media_fora <= 0.05: return None
    prob_ambas = 0
    prob_over_15, prob_over_25, prob_over_35 = 0, 0, 0
    prob_home, prob_draw, prob_away = 0, 0, 0
    m_h, m_a = max(media_casa, 0.1), max(media_fora, 0.1)
    rho = 0.10
    matriz_prob = {}
    total_prob = 0
    for gc in range(10):
        for gf in range(10):
            p_casa = (math.exp(-m_h) * (m_h**gc)) / math.factorial(gc)
            p_fora = (math.exp(-m_a) * (m_a**gf)) / math.factorial(gf)
            p_placar = p_casa * p_fora
            if gc == 0 and gf == 0: p_placar *= (1 - rho)
            elif gc == 1 and gf == 1: p_placar *= (1 + rho)
            matriz_prob[(gc, gf)] = p_placar
            total_prob += p_placar
    for (gc, gf), p_placar in matriz_prob.items():
        p_placar /= total_prob
        if gc > 0 and gf > 0: prob_ambas += p_placar
        if (gc + gf) > 1.5: prob_over_15 += p_placar
        if (gc + gf) > 2.5: prob_over_25 += p_placar
        if (gc + gf) > 3.5: prob_over_35 += p_placar
        if gc > gf: prob_home += p_placar
        elif gc == gf: prob_draw += p_placar
        else: prob_away += p_placar
    total_1x2 = prob_home + prob_draw + prob_away
    if total_1x2 > 0:
        adj_h, adj_d, adj_a = prob_home * 0.97, prob_draw * 1.06, prob_away * 0.97
        novo_total = adj_h + adj_d + adj_a
        prob_home = (adj_h / novo_total) * total_1x2
        prob_draw = (adj_d / novo_total) * total_1x2
        prob_away = (adj_a / novo_total) * total_1x2
    return {
        "HOME": {"prob": prob_home * 100}, "DRAW": {"prob": prob_draw * 100}, "AWAY": {"prob": prob_away * 100},
        "1X": {"prob": (prob_home + prob_draw) * 100}, "X2": {"prob": (prob_away + prob_draw) * 100},
        "BTTS": {"prob": prob_ambas * 100},
        "OVER_15": {"prob": prob_over_15 * 100}, "UNDER_15": {"prob": (1 - prob_over_15) * 100},
        "OVER_25": {"prob": prob_over_25 * 100}, "UNDER_25": {"prob": (1 - prob_over_25) * 100},
        "OVER_35": {"prob": prob_over_35 * 100}, "UNDER_35": {"prob": (1 - prob_over_35) * 100},
    }

# ==========================================
# 3. BUSCAS DE API
# ==========================================
def buscar_stats_partida(fixture_id, team_id, gols_reais):
    url = f"{BASE_URL}/fixtures/statistics"
    params = {'fixture': fixture_id, 'team': team_id}
    sog, xg_api = 0, None
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response') and len(res['response']) > 0:
            stats = res['response'][0]['statistics']
            for s in stats:
                if s['type'] == 'expected_goals' and s['value']: xg_api = float(s['value'])
                if s['type'] == 'Shots on Goal' and s['value']: sog = int(s['value'])
    except: pass
    if xg_api is not None: return (xg_api * 0.90) + (sog * 0.05)
    return (gols_reais * 0.70) + (sog * 0.10)

def buscar_historico_global(team_id, current_league_id, last_n=12):
    """V6.2 — split home/away para projeção contextual correta."""
    url = f"{BASE_URL}/fixtures"
    params = {'team': team_id, 'last': 20, 'status': 'FT'}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if not res.get('response'): return None
        hoje = datetime.datetime.now()
        limite_6_meses = hoje - datetime.timedelta(days=180)
        jogos_validos = [j for j in res['response']
                         if datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d') > limite_6_meses]
        if len(jogos_validos) < 5: jogos_validos = res['response'][:8]
        else: jogos_validos = jogos_validos[:last_n]

        total_gols_f, total_gols_s, total_xg_f, total_xg_s, soma_pesos = 0, 0, 0, 0, 0
        home_xg_f, home_xg_s, home_peso = 0, 0, 0
        away_xg_f, away_xg_s, away_peso = 0, 0, 0
        forma = []

        for idx, j in enumerate(jogos_validos):
            f_id = j['fixture']['id']
            dias_atras = (hoje - datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d')).days
            peso_tempo = math.exp(-0.005 * dias_atras)
            peso_liga = 1.2 if j['league']['id'] == current_league_id else 1.0
            peso_final = peso_tempo * peso_liga
            is_home = j['teams']['home']['id'] == team_id
            gf = j['goals']['home'] if is_home else j['goals']['away']
            gs = j['goals']['away'] if is_home else j['goals']['home']
            opp_id = j['teams']['away']['id'] if is_home else j['teams']['home']['id']
            xg_f = buscar_stats_partida(f_id, team_id, gf) if idx < 6 else gf * 0.9
            xg_s = buscar_stats_partida(f_id, opp_id, gs) if idx < 6 else gs * 0.9
            total_gols_f += gf * peso_final
            total_gols_s += gs * peso_final
            total_xg_f += xg_f * peso_final
            total_xg_s += xg_s * peso_final
            soma_pesos += peso_final
            if is_home:
                home_xg_f += xg_f * peso_final; home_xg_s += xg_s * peso_final; home_peso += peso_final
            else:
                away_xg_f += xg_f * peso_final; away_xg_s += xg_s * peso_final; away_peso += peso_final
            if idx < 5:
                forma.append("🟩" if gf > gs else "⬜" if gf == gs else "🟥")

        if soma_pesos == 0: return None
        media_xg_f = total_xg_f / soma_pesos
        media_xg_s = total_xg_s / soma_pesos
        return {
            "media_feita": total_gols_f / soma_pesos,
            "media_sofrida": total_gols_s / soma_pesos,
            "media_xg_f": media_xg_f,
            "media_xg_s": media_xg_s,
            "home_xg_f": (home_xg_f / home_peso) if home_peso > 0 else media_xg_f,
            "home_xg_s": (home_xg_s / home_peso) if home_peso > 0 else media_xg_s,
            "away_xg_f": (away_xg_f / away_peso) if away_peso > 0 else media_xg_f,
            "away_xg_s": (away_xg_s / away_peso) if away_peso > 0 else media_xg_s,
            "forma": "".join(forma[::-1])
        }
    except: return None

def buscar_h2h(home_id, away_id, last_n=5):
    """
    V6.3 — Confronto direto. Custo: 1 crédito.
    Contexto para a IA, não altera o Poisson.
    """
    url = f"{BASE_URL}/fixtures/headtohead"
    params = {'h2h': f"{home_id}-{away_id}", 'last': last_n, 'status': 'FT'}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if not res.get('response'): return None
        jogos = res['response']
        if not jogos: return None
        wins_home, wins_away, draws, total_gols = 0, 0, 0, 0
        for j in jogos:
            gh = j['goals']['home'] or 0
            ga = j['goals']['away'] or 0
            total_gols += gh + ga
            jogo_home_id = j['teams']['home']['id']
            if gh > ga:
                if jogo_home_id == home_id: wins_home += 1
                else: wins_away += 1
            elif ga > gh:
                if jogo_home_id == away_id: wins_home += 1
                else: wins_away += 1
            else:
                draws += 1
        n = len(jogos)
        return {"wins_home": wins_home, "wins_away": wins_away, "draws": draws,
                "avg_goals": round(total_gols / n, 2), "n": n}
    except: return None

def buscar_odds_vips(fixture_id):
    url = f"{BASE_URL}/odds"
    params = {'fixture': fixture_id}
    odds = {"BTTS": 0, "OVER_15": 0, "UNDER_15": 0, "OVER_25": 0, "UNDER_25": 0,
            "OVER_35": 0, "UNDER_35": 0, "HOME": 0, "DRAW": 0, "AWAY": 0, "1X": 0, "X2": 0}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response') and len(res['response']) > 0:
            bookmakers = res['response'][0].get('bookmakers', [])
            bkm_to_use = None
            for target_id in [8, 4, 1]:
                bkm_to_use = next((b for b in bookmakers if b['id'] == target_id), None)
                if bkm_to_use: break
            if bkm_to_use:
                for bet in bkm_to_use['bets']:
                    if bet['name'] == 'Both Teams Score':
                        odds['BTTS'] = float(bet['values'][0]['odd'])
                    elif bet['name'] == 'Goals Over/Under':
                        for v in bet['values']:
                            if v['value'] == 'Over 1.5': odds['OVER_15'] = float(v['odd'])
                            if v['value'] == 'Under 1.5': odds['UNDER_15'] = float(v['odd'])
                            if v['value'] == 'Over 2.5': odds['OVER_25'] = float(v['odd'])
                            if v['value'] == 'Under 2.5': odds['UNDER_25'] = float(v['odd'])
                            if v['value'] == 'Over 3.5': odds['OVER_35'] = float(v['odd'])
                            if v['value'] == 'Under 3.5': odds['UNDER_35'] = float(v['odd'])
                    elif bet['name'] == 'Match Winner':
                        for v in bet['values']:
                            if v['value'] == 'Home': odds['HOME'] = float(v['odd'])
                            elif v['value'] == 'Draw': odds['DRAW'] = float(v['odd'])
                            elif v['value'] == 'Away': odds['AWAY'] = float(v['odd'])
                    elif bet['name'] == 'Double Chance':
                        for v in bet['values']:
                            if v['value'] == 'Home/Draw': odds['1X'] = float(v['odd'])
                            if v['value'] == 'Draw/Away': odds['X2'] = float(v['odd'])
            return odds
    except: pass
    return odds

# ==========================================
# 4. MOTOR DE ANÁLISE
# ==========================================
# Custo real auditado por jogo:
# /odds(1) + /fixtures casa(1) + /statistics x6x2(12)
# + /fixtures fora(1) + /statistics x6x2(12) + /h2h(1) = 28
CUSTO_POR_JOGO = 28

def acao_analisar(jogos_alvo, data_str, force=False):
    if "stats" not in banco_local["datas"][data_str]:
        banco_local["datas"][data_str]["stats"] = {}
    p_bar = st.progress(0)
    saldo_atual = atualizar_saldo_realtime()
    for idx, jogo in enumerate(jogos_alvo):
        if saldo_atual is not None and saldo_atual < CUSTO_POR_JOGO * 2:
            st.error("⚠️ FREIO DE EMERGÊNCIA! Créditos insuficientes.")
            time.sleep(3)
            break
        f_id = str(jogo['fixture']['id'])
        if force or f_id not in banco_local["datas"][data_str]["stats"]:
            h_id = jogo['teams']['home']['id']
            a_id = jogo['teams']['away']['id']
            l_id = jogo['league']['id']
            odds = buscar_odds_vips(f_id)
            s_h = buscar_historico_global(h_id, l_id)
            s_a = buscar_historico_global(a_id, l_id)
            h2h = buscar_h2h(h_id, a_id)
            if s_h and s_a:
                banco_local["datas"][data_str]["stats"][f_id] = {
                    "odds": odds, "h": s_h, "a": s_a, "l_id": l_id, "h2h": h2h
                }
                salvar_banco(banco_local)
            else:
                banco_local["datas"][data_str]["stats"][f_id] = {"erro": "Sem histórico suficiente"}
            if saldo_atual is not None: saldo_atual -= CUSTO_POR_JOGO
            time.sleep(0.2)
        p_bar.progress((idx + 1) / len(jogos_alvo))
    p_bar.empty()
    st.rerun()

def calcular_matematica_quant(d):
    """V6.2 — split home/away. H2H é contexto para IA, não altera Poisson."""
    coef_liga = PESOS_LIGAS.get(d.get('l_id', 0), 0.95)
    h_atk = d['h'].get('home_xg_f', d['h']['media_xg_f'])
    h_def = d['h'].get('home_xg_s', d['h']['media_xg_s'])
    a_atk = d['a'].get('away_xg_f', d['a']['media_xg_f'])
    a_def = d['a'].get('away_xg_s', d['a']['media_xg_s'])
    m_h = ((h_atk + a_def) / 2) * coef_liga * 1.05
    m_a = ((a_atk + h_def) / 2) * coef_liga * 0.95
    return m_h, m_a

def normalizar_prob_mercado(dados, key):
    odds = dados['odds']
    odd_alvo = odds.get(key, 0)
    if odd_alvo <= 1.0: return 0
    margem = 0
    if key in ["HOME", "DRAW", "AWAY"]:
        if odds.get("HOME") and odds.get("DRAW") and odds.get("AWAY"):
            margem = (1/odds["HOME"]) + (1/odds["DRAW"]) + (1/odds["AWAY"])
    elif key in ["OVER_15", "UNDER_15"]:
        if odds.get("OVER_15") and odds.get("UNDER_15"):
            margem = (1/odds["OVER_15"]) + (1/odds["UNDER_15"])
    elif key in ["OVER_25", "UNDER_25"]:
        if odds.get("OVER_25") and odds.get("UNDER_25"):
            margem = (1/odds["OVER_25"]) + (1/odds["UNDER_25"])
    elif key in ["OVER_35", "UNDER_35"]:
        if odds.get("OVER_35") and odds.get("UNDER_35"):
            margem = (1/odds["OVER_35"]) + (1/odds["UNDER_35"])
    elif key == "BTTS":
        return (1 / odd_alvo) * 100 * 0.95
    if margem > 0: return ((1 / odd_alvo) / margem) * 100
    return (1 / odd_alvo) * 100

def get_blended_prob(dados, p_dict, key):
    prob_nossa = p_dict[key]['prob']
    odd_casa = dados['odds'].get(key, 0)
    l_id = dados.get('l_id', 0)
    if odd_casa > 1.0:
        prob_mercado = normalizar_prob_mercado(dados, key)
        if prob_mercado > 0:
            if l_id in [39, 140, 135]: return (prob_nossa * 0.65) + (prob_mercado * 0.35)
            elif l_id in [78, 61, 2, 3]: return (prob_nossa * 0.75) + (prob_mercado * 0.25)
            elif l_id in [71, 72, 73]: return (prob_nossa * 0.80) + (prob_mercado * 0.20)
            else: return (prob_nossa * 0.90) + (prob_mercado * 0.10)
    return prob_nossa

def calcular_kelly(prob_blended, odd):
    if odd <= 1.0 or prob_blended <= 0: return 0
    p = prob_blended / 100.0
    b = odd - 1
    if b <= 0: return 0
    kelly_puro = (b * p - (1 - p)) / b
    return min(max(0, kelly_puro * 0.25), 0.05)

def get_ev(dados, p_dict, key):
    casa = dados['odds'].get(key, 0)
    prob_modelo = p_dict[key]['prob']
    if casa <= 1.0 or prob_modelo <= 0: return -100
    ev = ((prob_modelo / 100.0) * casa - 1) * 100
    if ev > 60 or (prob_modelo < 35 and key in ["HOME", "AWAY"]): return -999
    return ev

def avaliar_perfil_jogo(p_dict):
    if p_dict["UNDER_25"]["prob"] > 60: return "🧱 TRAVADO"
    elif p_dict["OVER_25"]["prob"] > 55: return "🧨 ABERTO"
    else: return "⚖️ EQUILIBRADO"

def formatar_h2h(h2h):
    if not h2h: return "Sem dados"
    return f"{h2h['n']} jogos | Casa {h2h['wins_home']}V/{h2h['draws']}E/{h2h['wins_away']}V Fora | Média gols: {h2h['avg_goals']:.1f}"

# ==========================================
# 4.1 SISTEMA DE CORES — 3 NÍVEIS
# ==========================================
def get_tier(ev):
    if ev > 10:
        return {"bg": "#0d2818", "border": "#28a745", "text": "#28a745", "badge": "🔥 FORTE", "badge_color": "#28a745"}
    elif ev > 3:
        return {"bg": "#0d1f31", "border": "#1a6b9a", "text": "#4db8ff", "badge": "✅ VALOR", "badge_color": "#4db8ff"}
    else:
        return {"bg": "#111", "border": "#2a2a2a", "text": "#666", "badge": "", "badge_color": "#444"}

def renderizar_mercado(col, titulo, p_dict, key, odds_dict, dados, banca_atual):
    prob_blended = get_blended_prob(dados, p_dict, key)
    casa = odds_dict.get(key, 0)
    ev = get_ev(dados, p_dict, key)
    justa = 100 / prob_blended if prob_blended > 0 else 0
    frac_kelly = calcular_kelly(prob_blended, casa)
    stake_raw = frac_kelly * banca_atual
    stake_sugerida = max(2.0, stake_raw) if (frac_kelly > 0 and ev > 3) else 0
    tier = get_tier(ev)
    badge_html = f'<div style="color:{tier["badge_color"]}; font-size:10px; font-weight:bold; margin-top:3px;">{tier["badge"]}</div>' if tier["badge"] else ''
    kelly_html = f'<div style="color:#17a2b8; font-size:10px; margin-top:4px;">🎯 R$ {stake_sugerida:.2f} ({frac_kelly*100:.1f}%)</div>' if stake_sugerida > 0 else ''
    with col:
        st.markdown(f'''
        <div style="border:1px solid {tier["border"]}; background:{tier["bg"]}; padding:8px; border-radius:6px; text-align:center; margin-bottom:8px;">
            <div style="font-size:10px; color:#777; margin-bottom:2px; font-weight:bold;">{titulo}</div>
            <div style="font-size:16px; font-weight:bold; color:{tier["text"]};">{prob_blended:.0f}%</div>
            <div style="font-size:11px; color:#aaa; margin-top:4px;">J:{justa:.2f} | O:{casa if casa > 0 else "—"}</div>
            {badge_html}{kelly_html}
        </div>
        ''', unsafe_allow_html=True)

# ==========================================
# 4.2 MÓDULO DE IA — PROMPTS ADAPTATIVOS V6.3
# ==========================================
REGRA_ADAPTATIVA = """
## 🚨 REGRA ABSOLUTA — QUANTIDADE ADAPTATIVA (NÃO NEGOCIÁVEL)
Você NÃO tem meta de picks. Siga os dados, não uma expectativa de relatório.
- Se NENHUM jogo qualifica: responda exatamente "🔇 Nenhum pick aprovado. Os dados não sustentam entradas hoje."
- Se 1 qualifica: retorne 1. Se 8 qualificam: retorne 8.
- NUNCA force pick para preencher relatório. Um pick fraco destrói a banca.
- Dias com poucos jogos (ex: segunda-feira) naturalmente terão menos picks. Isso é correto e esperado.
- Um dia com 2 picks de qualidade vale mais do que 12 picks mediocres.
"""

def chamar_ia_fabrica(textos_jogos, modo="GOLS"):
    if modo == "GOLS":
        prompt_sistema = f"""Você é um Auditor Quantitativo Profissional especializado em apostas esportivas no mercado de gols.
Seu objetivo é identificar APENAS oportunidades com vantagem estatística real. Qualidade absoluta sobre quantidade.

{REGRA_ADAPTATIVA}

---
## 🎯 ORDENAÇÃO
Retorne ranqueado do MELHOR (maior Score) para o PIOR.

---
## ⚙️ RÉGUA ASSIMÉTRICA DE EV
1. UNDER: Exija EV > 10% E xG Total baixíssimo. Seja rígido.
2. OVER / BTTS: Aceite EV > 3% se o xG confirmar tendência ofensiva.
3. NÃO force mercados. Use o H2H para validar: se o H2H mostra jogos historicamente com poucos gols, isso reforça Under.

---
## 💰 FILTRO DE ODDS
- Odds < 1.70 → DESCARTE IMEDIATO
- Odds 1.70–1.79 → Só se EV e xG forem perfeitos
- Odds ≥ 1.80 → Padrão mínimo | Odds ≥ 2.00 → Alto valor (se coerente)

---
## 📊 CRITÉRIOS
1. EV positivo real | 2. Coerência xG home/away | 3. Kelly > 20% = anomalia, penalize | 4. H2H valida tendência

---
## 🧮 SCORE (0–100) — Mínimo para aprovação: 75

---
## 📤 FORMATO (do maior Score para o menor)
[ID: XXXX]
Jogo: Time A vs Time B
Mercado: X | Odd: X.XX | Perfil: Conservador/Equilibrado/Agressivo

📊 Prob: XX% | EV: +X% | xG Total: X.XX | Kelly: XX% | Score: XX/100

🧠 Justificativa: [baseado em xG + EV + H2H se relevante]
⚠️ Risco: [fator principal]
"""
    else:
        prompt_sistema = f"""Você é um Analista Quantitativo Sênior especializado em mercados de resultado (1X2 e Dupla Chance).
Este é o mercado mais eficiente do futebol. As casas têm menor margem de erro aqui. Seja mais rigoroso do que no mercado de gols.

{REGRA_ADAPTATIVA}

---
## 🎯 ORDENAÇÃO
Retorne ranqueado da aposta de MAIOR confiança para a MENOR.

---
## ⚙️ THRESHOLDS OBRIGATÓRIOS — RESULTADO
Estes valores são intencionalmente mais altos do que no mercado de gols:
- Resultado Simples (HOME / AWAY): EV MÍNIMO de 8% + Odd ≥ 2.00
- Dupla Chance (1X / X2): EV MÍNIMO de 5% + Odd ≥ 1.50
- Empate (DRAW): APENAS com EV ≥ 12% — variância destrutiva, evitar
- Qualquer odd < 1.40: DESCARTE automático

---
## 📊 CRITÉRIOS ESPECÍFICOS
1. EV acima do threshold correspondente
2. H2H: valida domínio histórico entre os times — contradição com modelo = alerta
3. xG: diferença significativa Casa vs Fora reforça resultado simples
4. Forma recente: time em má fase anula vantagem matemática
5. PREFERÊNCIA: Dupla Chance > Resultado Simples (menor variância)

---
## 🚫 PROIBIDO
- Empate sem EV ≥ 12% | Resultado simples sem EV ≥ 8% | Ignorar H2H quando contradiz o modelo

---
## 📤 FORMATO
💎 APROVADOS:
[ID: XXXXXX] [JOGO] 🎯 **[MERCADO]**
* 📊 Prob: XX% | EV: +X% | Odd: X.XX | H2H: [resumo]
* 🧠 Lógica: [justificativa quantitativa com números]
* ⚠️ Risco: [fator de risco real]
"""
    try:
        configuracao = genai.types.GenerationConfig(temperature=0.0)
        return model_ia.generate_content(
            prompt_sistema + "\n\n📋 DADOS PARA ANÁLISE:\n\n" + textos_jogos,
            generation_config=configuracao
        ).text
    except Exception as e:
        return f"🚨 Erro na IA: {e}"

# ==========================================
# 5. TRACKER — MODAL
# ==========================================
@st.dialog("📋 Diário de Bordo", width="large")
def mostrar_tracker():
    lucro_total = 0.0
    greens, reds, pendentes = 0, 0, 0
    for p in banco_local["picks"]:
        s = p.get("status", "Pendente")
        stake = p.get("stake", 1.0)
        if s == "Green":
            lucro_total += stake * (p.get("odd", 1.0) - 1.0); greens += 1
        elif s == "Red":
            lucro_total -= stake; reds += 1
        elif s == "Pendente":
            pendentes += 1
    total_resolvido = greens + reds
    taxa = (greens / total_resolvido * 100) if total_resolvido > 0 else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("P/L Total", f"R$ {lucro_total:.2f}")
    c2.metric("✅ Greens", greens)
    c3.metric("❌ Reds", reds)
    c4.metric("Taxa de Acerto", f"{taxa:.0f}%")
    st.divider()
    if not banco_local["picks"]:
        st.info("Nenhum pick registrado ainda.")
        return
    for i, p in enumerate(reversed(banco_local["picks"])):
        real_idx = len(banco_local["picks"]) - 1 - i
        st_atual = p.get("status", "Pendente")
        icon = "⏳" if st_atual == "Pendente" else "✅" if st_atual == "Green" else "❌" if st_atual == "Red" else "➖"
        cor = "#0d2818" if st_atual == "Green" else "#2b1a1a" if st_atual == "Red" else "#111"
        st.markdown(f"""
        <div style="background:{cor}; border-radius:8px; padding:10px; margin-bottom:6px; border:1px solid #222;">
            <b>{icon} {p['data']} — {p['jogo']}</b><br>
            <span style="color:#aaa; font-size:12px;">🎯 {p['mercado']} | Odd: {p.get('odd','-')} | R$ {p.get('stake',1.0):.2f} | Prob: {p.get('prob','-')}% | EV: {p.get('ev','-')}%</span>
        </div>
        """, unsafe_allow_html=True)
        if st_atual == "Pendente":
            cb1, cb2, cb3 = st.columns(3)
            if cb1.button("✅ Green", key=f"dg_{real_idx}", type="primary"):
                banco_local["picks"][real_idx]["status"] = "Green"; salvar_banco(banco_local); st.rerun()
            if cb2.button("❌ Red", key=f"dr_{real_idx}"):
                banco_local["picks"][real_idx]["status"] = "Red"; salvar_banco(banco_local); st.rerun()
            if cb3.button("➖ Anular", key=f"dv_{real_idx}"):
                banco_local["picks"][real_idx]["status"] = "Devolvida"; salvar_banco(banco_local); st.rerun()
        else:
            if st.button("↩️ Desfazer", key=f"du_{real_idx}"):
                banco_local["picks"][real_idx]["status"] = "Pendente"; salvar_banco(banco_local); st.rerun()

# ==========================================
# 6. SIDEBAR
# ==========================================
with st.sidebar:
    st.markdown("""
    <div style="text-align:center; padding:14px 0 10px 0;">
        <div style="font-size:20px; font-weight:900; color:#fff; letter-spacing:1px;">⚽ QG BARRIOS PRO</div>
        <div style="font-size:10px; color:#444; margin-top:2px; letter-spacing:2px;">MOTOR QUANTITATIVO V6.3</div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    saldo = atualizar_saldo_realtime()
    saldo_val = saldo if saldo else 0
    pct = saldo_val / 7500
    jogos_possiveis = int(saldo_val / CUSTO_POR_JOGO)
    cor_saldo = "#28a745" if pct > 0.5 else "#ffc107" if pct > 0.2 else "#dc3545"
    st.markdown(f"""
    <div style="background:#111; border-radius:8px; padding:12px; margin-bottom:8px; border:1px solid #1e1e1e;">
        <div style="font-size:10px; color:#555; margin-bottom:4px; letter-spacing:1px;">CRÉDITOS API HOJE</div>
        <div style="font-size:22px; font-weight:bold; color:{cor_saldo};">{saldo_val} <span style="font-size:11px; color:#444;">/ 7500</span></div>
        <div style="font-size:10px; color:#555; margin-top:4px;">≈ {jogos_possiveis} jogos possíveis ({CUSTO_POR_JOGO} créd/jogo)</div>
        <div style="background:#1a1a1a; border-radius:4px; height:4px; margin-top:8px;">
            <div style="background:{cor_saldo}; width:{pct*100:.0f}%; height:4px; border-radius:4px;"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    st.markdown("**📈 Gestão de Risco**")
    banca_input = st.number_input("Banca Total (R$)", value=float(banco_local.get("banca_inicial", 30.0)), step=5.0, label_visibility="collapsed")
    if banca_input != banco_local.get("banca_inicial"):
        banco_local["banca_inicial"] = banca_input
        salvar_banco(banco_local)

    lucro_total_sb = 0.0
    for p in banco_local["picks"]:
        status = p.get("status", "Pendente")
        stake_usada = p.get("stake", 1.0)
        if status == "Green": lucro_total_sb += stake_usada * (p.get("odd", 1.0) - 1.0)
        elif status == "Red": lucro_total_sb -= stake_usada

    banca_atual = banco_local["banca_inicial"] + lucro_total_sb
    cor_pl = "#28a745" if lucro_total_sb >= 0 else "#dc3545"
    st.markdown(f"""
    <div style="background:#111; border-radius:8px; padding:12px; border:1px solid #1e1e1e;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <div style="font-size:10px; color:#555; letter-spacing:1px;">BANCA ATUAL</div>
                <div style="font-size:20px; font-weight:bold; color:#fff;">R$ {banca_atual:.2f}</div>
            </div>
            <div style="text-align:right;">
                <div style="font-size:10px; color:#555; letter-spacing:1px;">P/L</div>
                <div style="font-size:16px; font-weight:bold; color:{cor_pl};">{"+" if lucro_total_sb >= 0 else ""}R$ {lucro_total_sb:.2f}</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.divider()

    if st.button("📋 Diário de Bordo", use_container_width=True):
        mostrar_tracker()
    st.divider()

    st.markdown("**🔍 Scanner**")
    data_consulta = st.date_input("Data", datetime.date.today(), label_visibility="collapsed")
    data_str = data_consulta.strftime("%Y-%m-%d")
    LIGAS_PRO = [39, 140, 135, 78, 61, 71, 72, 73, 2, 3, 848, 13, 11, 40, 88, 307, 253, 94, 128, 203]
    tipo_filtro = st.radio("Ligas:", ["🏆 Só PRO", "🌍 PRO + Confiáveis", "🗑️ Tudo"], index=1)

    if st.button("🗑️ Limpar Cache do Dia", use_container_width=True):
        if data_str in banco_local["datas"]:
            del banco_local["datas"][data_str]
            salvar_banco(banco_local)
            st.rerun()

# ==========================================
# 7. ÁREA PRINCIPAL
# ==========================================
if data_str not in banco_local["datas"]:
    banco_local["datas"][data_str] = {"agenda": [], "stats": {}}
agenda = banco_local["datas"][data_str]["agenda"]

st.markdown(f"""
<div style="margin-bottom:16px; padding-bottom:12px; border-bottom:1px solid #1e1e1e;">
    <div style="font-size:12px; color:#444; margin-bottom:2px; letter-spacing:1px;">{data_consulta.strftime("%A, %d de %B de %Y").upper()}</div>
    <div style="font-size:26px; font-weight:900; color:#fff;">Scanner de Jogos</div>
</div>
""", unsafe_allow_html=True)

if st.button("🔄 Carregar Agenda do Dia", use_container_width=True):
    res = requests.get(f"{BASE_URL}/fixtures?date={data_str}&timezone=America/Sao_Paulo", headers=HEADERS).json()
    if res.get('response'):
        banco_local["datas"][data_str]["agenda"] = res['response']
        salvar_banco(banco_local)
        st.rerun()
    else:
        st.error(f"🚨 Nenhum jogo encontrado. Erro: {res.get('errors', 'Grade vazia.')}")

if agenda:
    jogos_visiveis = []
    palavras_proibidas = ['u19', 'u20', 'u21', 'u23', 'youth', 'women', 'feminino', 'reserve', 'amateur', 'regional', 'state']
    paises_confiaveis = ['Brazil', 'Argentina', 'USA', 'Mexico', 'Netherlands', 'Portugal', 'Turkey', 'Saudi-Arabia',
                         'Switzerland', 'Japan', 'Colombia', 'Chile', 'South-Korea', 'Scotland', 'Greece', 'Belgium',
                         'Uruguay', 'Ecuador', 'Paraguay', 'Bolivia', 'Peru', 'Venezuela']
    for j in agenda:
        l_id = j['league']['id']
        l_name = str(j['league']['name']).lower()
        l_country = str(j['league']['country'])
        if (tipo_filtro == "🏆 Só PRO" and l_id in LIGAS_PRO) or \
           (tipo_filtro == "🌍 PRO + Confiáveis" and (l_id in LIGAS_PRO or
            (l_country in paises_confiaveis and not any(p in l_name for p in palavras_proibidas)))) or \
           tipo_filtro == "🗑️ Tudo":
            if j not in jogos_visiveis:
                jogos_visiveis.append(j)

    col_btn1, col_cnt = st.columns([4, 1])
    with col_btn1:
        if st.button(f"🚀 Analisar TODOS os Visíveis ({len(jogos_visiveis)} jogos)", type="primary", use_container_width=True):
            acao_analisar(jogos_visiveis, data_str)
    with col_cnt:
        jogos_analisados = sum(
            1 for j in jogos_visiveis
            if str(j['fixture']['id']) in banco_local["datas"][data_str].get("stats", {})
            and "erro" not in banco_local["datas"][data_str]["stats"].get(str(j['fixture']['id']), {})
        )
        st.markdown(f"""
        <div style="background:#111; border-radius:8px; padding:10px; text-align:center; border:1px solid #1e1e1e;">
            <div style="font-size:10px; color:#555; letter-spacing:1px;">PRONTOS</div>
            <div style="font-size:18px; font-weight:bold; color:#4db8ff;">{jogos_analisados}<span style="font-size:12px; color:#444;">/{len(jogos_visiveis)}</span></div>
        </div>
        """, unsafe_allow_html=True)

    st.divider()
    st.markdown("### 🤖 Auditoria por Inteligência Artificial")
    col_ia1, col_ia2 = st.columns(2)

    with col_ia1:
        if st.button("🧠 Ranking IA — GOLS", use_container_width=True):
            textos = ""
            for j in jogos_visiveis:
                f_id = str(j['fixture']['id'])
                d = banco_local["datas"][data_str]["stats"].get(f_id)
                if d and "erro" not in d:
                    m_h, m_a = calcular_matematica_quant(d)
                    p = calcular_poisson(m_h, m_a)
                    if p:
                        h2h_txt = formatar_h2h(d.get('h2h'))
                        textos += f"""
ID: {f_id} | {j['teams']['home']['name']} vs {j['teams']['away']['name']}
- H2H: {h2h_txt}
- Forma Casa: {d['h']['forma']} | xG Casa: {m_h:.2f} | Atq: {d['h'].get('home_xg_f', d['h']['media_xg_f']):.2f} | Def: {d['h'].get('home_xg_s', d['h']['media_xg_s']):.2f}
- Forma Fora: {d['a']['forma']} | xG Fora: {m_a:.2f} | Atq: {d['a'].get('away_xg_f', d['a']['media_xg_f']):.2f} | Def: {d['a'].get('away_xg_s', d['a']['media_xg_s']):.2f}
- xG Total: {m_h + m_a:.2f}
- Over 1.5 -> Odd: {d['odds'].get('OVER_15',0)} | Prob: {p['OVER_15']['prob']:.1f}% | EV: {get_ev(d,p,'OVER_15'):.1f}% | Kelly: {calcular_kelly(get_blended_prob(d,p,'OVER_15'),d['odds'].get('OVER_15',0))*100:.1f}%
- Under 2.5 -> Odd: {d['odds'].get('UNDER_25',0)} | Prob: {p['UNDER_25']['prob']:.1f}% | EV: {get_ev(d,p,'UNDER_25'):.1f}% | Kelly: {calcular_kelly(get_blended_prob(d,p,'UNDER_25'),d['odds'].get('UNDER_25',0))*100:.1f}%
- Over 2.5 -> Odd: {d['odds'].get('OVER_25',0)} | Prob: {p['OVER_25']['prob']:.1f}% | EV: {get_ev(d,p,'OVER_25'):.1f}% | Kelly: {calcular_kelly(get_blended_prob(d,p,'OVER_25'),d['odds'].get('OVER_25',0))*100:.1f}%
- Under 3.5 -> Odd: {d['odds'].get('UNDER_35',0)} | Prob: {p['UNDER_35']['prob']:.1f}% | EV: {get_ev(d,p,'UNDER_35'):.1f}% | Kelly: {calcular_kelly(get_blended_prob(d,p,'UNDER_35'),d['odds'].get('UNDER_35',0))*100:.1f}%
- BTTS -> Odd: {d['odds'].get('BTTS',0)} | Prob: {p['BTTS']['prob']:.1f}% | EV: {get_ev(d,p,'BTTS'):.1f}% | Kelly: {calcular_kelly(get_blended_prob(d,p,'BTTS'),d['odds'].get('BTTS',0))*100:.1f}%
"""
            with st.spinner("IA analisando mercado de gols..."):
                resposta = chamar_ia_fabrica(textos, modo="GOLS")
                st.session_state["ia_gols"] = resposta
                st.session_state["ids_gols"] = re.findall(r'\[ID:\s*(\d+)\]', resposta)

    with col_ia2:
        if st.button("⚔️ Ranking IA — RESULTADO", use_container_width=True):
            textos = ""
            for j in jogos_visiveis:
                f_id = str(j['fixture']['id'])
                d = banco_local["datas"][data_str]["stats"].get(f_id)
                if d and "erro" not in d:
                    m_h, m_a = calcular_matematica_quant(d)
                    p = calcular_poisson(m_h, m_a)
                    if p:
                        h2h_txt = formatar_h2h(d.get('h2h'))
                        textos += f"""
ID: {f_id} | {j['teams']['home']['name']} vs {j['teams']['away']['name']}
- H2H: {h2h_txt}
- Forma Casa: {d['h']['forma']} | xG Casa: {m_h:.2f} | Atq: {d['h'].get('home_xg_f', d['h']['media_xg_f']):.2f} | Def: {d['h'].get('home_xg_s', d['h']['media_xg_s']):.2f}
- Forma Fora: {d['a']['forma']} | xG Fora: {m_a:.2f} | Atq: {d['a'].get('away_xg_f', d['a']['media_xg_f']):.2f} | Def: {d['a'].get('away_xg_s', d['a']['media_xg_s']):.2f}
- Casa -> Odd: {d['odds'].get('HOME',0)} | Prob: {p['HOME']['prob']:.1f}% | EV: {get_ev(d,p,'HOME'):.1f}%
- Empate -> Odd: {d['odds'].get('DRAW',0)} | Prob: {p['DRAW']['prob']:.1f}% | EV: {get_ev(d,p,'DRAW'):.1f}%
- Fora -> Odd: {d['odds'].get('AWAY',0)} | Prob: {p['AWAY']['prob']:.1f}% | EV: {get_ev(d,p,'AWAY'):.1f}%
- Dupla Casa (1X) -> Odd: {d['odds'].get('1X',0)} | Prob: {p['1X']['prob']:.1f}% | EV: {get_ev(d,p,'1X'):.1f}%
- Dupla Fora (X2) -> Odd: {d['odds'].get('X2',0)} | Prob: {p['X2']['prob']:.1f}% | EV: {get_ev(d,p,'X2'):.1f}%
"""
            with st.spinner("IA analisando mercado de resultados..."):
                resposta = chamar_ia_fabrica(textos, modo="RESULTADO")
                st.session_state["ia_resultado"] = resposta
                st.session_state["ids_res"] = re.findall(r'\[ID:\s*(\d+)\]', resposta)

    if "ia_gols" in st.session_state:
        with st.expander("🔥 Ver Ranking IA — GOLS", expanded=True):
            st.info(st.session_state["ia_gols"])
    if "ia_resultado" in st.session_state:
        with st.expander("⚔️ Ver Ranking IA — RESULTADO", expanded=True):
            st.info(st.session_state["ia_resultado"])

    st.divider()

    ids_gols_rank = st.session_state.get("ids_gols", [])
    jogos_gols_sorted = sorted(jogos_visiveis, key=lambda j: ids_gols_rank.index(str(j['fixture']['id'])) if str(j['fixture']['id']) in ids_gols_rank else 999999)
    ids_res_rank = st.session_state.get("ids_res", [])
    jogos_res_sorted = sorted(jogos_visiveis, key=lambda j: ids_res_rank.index(str(j['fixture']['id'])) if str(j['fixture']['id']) in ids_res_rank else 999999)

    tab_gols, tab_result = st.tabs(["🔥 MODO GOLS", "⚔️ MODO RESULTADO"])
    dict_m_gols = {"OVER_15": "Over 1.5", "OVER_25": "Over 2.5", "OVER_35": "Over 3.5", "UNDER_25": "Under 2.5", "UNDER_35": "Under 3.5", "BTTS": "Ambas Marcam"}
    dict_m_res = {"HOME": "Casa", "DRAW": "Empate", "AWAY": "Fora", "1X": "Dupla Casa", "X2": "Dupla Fora"}

    def renderizar_card(j, f_id, d, ids_rank, modo_dict, prefix):
        m_h, m_a = calcular_matematica_quant(d)
        p = calcular_poisson(m_h, m_a)
        if not p: return
        is_top = f_id in ids_rank
        borda = "border: 2px solid #ffc107;" if is_top else "border: 1px solid #1e1e1e;"
        rank_badge = f"<span style='background:#ffc107; color:#000; font-size:10px; font-weight:bold; padding:2px 7px; border-radius:4px; margin-right:6px;'>TOP {ids_rank.index(f_id)+1}</span>" if is_top else ""
        h2h_txt = formatar_h2h(d.get('h2h'))
        perfil_html = ""
        if prefix == "g":
            perfil = avaliar_perfil_jogo(p)
            cor_perfil = "#28a745" if "ABERTO" in perfil else "#dc3545" if "TRAVADO" in perfil else "#ffc107"
            perfil_html = f"<span style='color:{cor_perfil}; font-size:11px; font-weight:bold;'>{perfil}</span>"

        st.markdown(f"""<div style='{borda} border-radius:10px; padding:14px; background:#0e1117; margin-bottom:10px;'>
            <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;'>
                <span style='color:#444; font-size:11px;'>{rank_badge}🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span>
                {perfil_html}
            </div>
            <div style='font-size:17px; font-weight:bold; color:#fff; margin-bottom:4px;'>{j['teams']['home']['name']} <span style='color:#333; font-size:12px;'>vs</span> {j['teams']['away']['name']}</div>
            <div style='font-size:11px; color:#444;'>xG: <span style='color:#aaa;'>{m_h:.2f}</span> — <span style='color:#aaa;'>{m_a:.2f}</span> (Σ {m_h+m_a:.2f}) &nbsp;|&nbsp; H2H: <span style='color:#666;'>{h2h_txt}</span></div>
        """, unsafe_allow_html=True)

        cols = st.columns(3)
        for idx_col, m_key in enumerate(modo_dict.keys()):
            renderizar_mercado(cols[idx_col % 3], modo_dict[m_key], p, m_key, d['odds'], d, banca_atual)

        with st.expander("📊 Detalhes, Salvar & Atualizar"):
            ci1, ci2 = st.columns(2)
            with ci1:
                st.markdown(f"🏠 **{j['teams']['home']['name']}**")
                st.caption(f"Forma: {d['h']['forma']} | Atq (casa): {d['h'].get('home_xg_f', d['h']['media_xg_f']):.2f} | Def (casa): {d['h'].get('home_xg_s', d['h']['media_xg_s']):.2f}")
            with ci2:
                st.markdown(f"✈️ **{j['teams']['away']['name']}**")
                st.caption(f"Forma: {d['a']['forma']} | Atq (fora): {d['a'].get('away_xg_f', d['a']['media_xg_f']):.2f} | Def (fora): {d['a'].get('away_xg_s', d['a']['media_xg_s']):.2f}")
            st.divider()
            c_sel, c_stk, c_btn, c_upd = st.columns([2, 1, 1, 1])
            mk_sel = c_sel.selectbox("Mercado:", list(modo_dict.keys()), format_func=lambda x: modo_dict[x], key=f"s{prefix}_{f_id}", label_visibility="collapsed")
            frac = calcular_kelly(get_blended_prob(d, p, mk_sel), d['odds'].get(mk_sel, 0))
            ev_sel = get_ev(d, p, mk_sel)
            stake_sug = max(2.0, frac * banca_atual) if (frac > 0 and ev_sel > 3) else 2.0
            stk_input = c_stk.number_input("R$", value=float(round(stake_sug, 2)), step=0.5, key=f"stk{prefix}_{f_id}", label_visibility="collapsed")
            if c_btn.button("✅ Salvar", key=f"bs{prefix}_{f_id}"):
                banco_local["picks"].append({
                    "data": data_str, "jogo": f"{j['teams']['home']['name']} v {j['teams']['away']['name']}",
                    "mercado": modo_dict[mk_sel], "odd": d['odds'].get(mk_sel, 0),
                    "prob": round(get_blended_prob(d, p, mk_sel), 1), "ev": round(get_ev(d, p, mk_sel), 1),
                    "status": "Pendente", "stake": stk_input
                })
                salvar_banco(banco_local)
                st.toast(f"✅ Pick salvo — R$ {stk_input:.2f}")
            if c_upd.button("🔄", key=f"upd{prefix}_{f_id}", help="Atualizar análise"):
                acao_analisar([j], data_str, force=True)

        st.markdown("</div>", unsafe_allow_html=True)

    def renderizar_mini(j, f_id, prefix):
        cc, cb = st.columns([5, 1])
        with cc:
            st.markdown(f"""<div style='border:1px solid #1e1e1e; border-radius:8px; padding:10px; background:#0e1117; margin-bottom:4px;'>
                <span style='color:#444; font-size:11px;'>🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span><br>
                <span style='font-size:15px; font-weight:bold; color:#888;'>{j['teams']['home']['name']} vs {j['teams']['away']['name']}</span>
            </div>""", unsafe_allow_html=True)
        with cb:
            if st.button("📊", key=f"mini_{prefix}_{f_id}", help="Analisar este jogo"):
                acao_analisar([j], data_str, force=True)

    with tab_gols:
        for j in jogos_gols_sorted:
            f_id = str(j['fixture']['id'])
            d = banco_local["datas"][data_str]["stats"].get(f_id)
            if not d or "erro" in d: renderizar_mini(j, f_id, "g")
            else: renderizar_card(j, f_id, d, ids_gols_rank, dict_m_gols, "g")

    with tab_result:
        for j in jogos_res_sorted:
            f_id = str(j['fixture']['id'])
            d = banco_local["datas"][data_str]["stats"].get(f_id)
            if not d or "erro" in d: renderizar_mini(j, f_id, "r")
            else: renderizar_card(j, f_id, d, ids_res_rank, dict_m_res, "r")
