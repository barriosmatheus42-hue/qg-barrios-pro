import streamlit as st
import requests
import datetime
import time
import math
import json
import os
import google.generativeai as genai
import re

st.set_page_config(page_title="QG Barrios PRO - V6.1 Final Quant", layout="wide")

# ==========================================
# 0. CONFIGURAÇÕES E CHAVES
# ==========================================
API_KEY_PRO = "00374ab0590422053c950ddc399a0ccb"
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {'x-apisports-key': API_KEY_PRO}

# CONFIGURAÇÕES DO COFRE NA NUVEM (JSONBin)
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
# 1. GERENCIAMENTO DO BANCO (HÍBRIDO: NUVEM + LOCAL)
# ==========================================
ARQUIVO_BANCO = "banco_barrios_pro.json"

def carregar_banco():
    if "banco_local" in st.session_state:
        return st.session_state["banco_local"]
        
    # PASSO 1: Tenta carregar o cache pesado (Agenda e Análises) do arquivo antigo
    banco = {"datas": {}, "creditos_restantes": 7500, "picks": [], "banca_inicial": 30.0}
    if os.path.exists(ARQUIVO_BANCO):
        try:
            with open(ARQUIVO_BANCO, "r") as f:
                banco_lido = json.load(f)
                if "datas" in banco_lido:
                    banco["datas"] = banco_lido["datas"]
        except: pass
        
    # PASSO 2: Puxa o cofre na nuvem para garantir que o saldo e apostas estão protegidos
    try:
        res = requests.get(f"{JSONBIN_URL}/latest", headers=JSONBIN_HEADERS, timeout=10)
        if res.status_code == 200:
            banco_nuvem = res.json().get("record", {})
            banco["picks"] = banco_nuvem.get("picks", [])
            banco["banca_inicial"] = banco_nuvem.get("banca_inicial", 30.0)
    except Exception as e:
        pass
        
    st.session_state["banco_local"] = banco 
    return banco

def salvar_banco(dados):
    st.session_state["banco_local"] = dados
    
    # PASSO 1: Salva TUDO no arquivo local (Isso recria a memória do dia inteiro que você gostava)
    try:
        with open(ARQUIVO_BANCO, "w") as f:
            json.dump(dados, f)
    except: pass

    # PASSO 2: Salva só Dinheiro e Apostas na Nuvem (Foge do Erro 413)
    try:
        dados_nuvem = {
            "banca_inicial": dados.get("banca_inicial", 30.0),
            "picks": dados.get("picks", [])
        }
        headers_put = JSONBIN_HEADERS.copy()
        headers_put["X-Bin-Versioning"] = "false" 
        requests.put(JSONBIN_URL, headers=headers_put, json=dados_nuvem, timeout=10)
    except Exception as e:
        pass

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
# 2. MOTOR MATEMÁTICO (POISSON V6.1)
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
        prob_home, prob_draw, prob_away = (adj_h/novo_total)*total_1x2, (adj_d/novo_total)*total_1x2, (adj_a/novo_total)*total_1x2

    return {
        "HOME": {"prob": prob_home * 100}, "DRAW": {"prob": prob_draw * 100}, "AWAY": {"prob": prob_away * 100},
        "1X": {"prob": (prob_home + prob_draw) * 100}, "X2": {"prob": (prob_away + prob_draw) * 100},
        "BTTS": {"prob": prob_ambas * 100},
        "OVER_15": {"prob": prob_over_15 * 100}, "UNDER_15": {"prob": (1 - prob_over_15) * 100},
        "OVER_25": {"prob": prob_over_25 * 100}, "UNDER_25": {"prob": (1 - prob_over_25) * 100},
        "OVER_35": {"prob": prob_over_35 * 100}, "UNDER_35": {"prob": (1 - prob_over_35) * 100},
    }

# ==========================================
# 3. BUSCAS DE API E LÓGICA DE DADOS
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
    url = f"{BASE_URL}/fixtures"
    params = {'team': team_id, 'last': 20, 'status': 'FT'}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if not res.get('response'): return None
        hoje = datetime.datetime.now()
        limite_6_meses = hoje - datetime.timedelta(days=180)
        jogos_validos = [j for j in res['response'] if datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d') > limite_6_meses]
        if len(jogos_validos) < 5: jogos_validos = res['response'][:8]
        else: jogos_validos = jogos_validos[:last_n]

        total_gols_f, total_gols_s, total_xg_f, total_xg_s, soma_pesos = 0, 0, 0, 0, 0
        forma = []
        for idx, j in enumerate(jogos_validos):
            f_id = j['fixture']['id']
            peso_final = math.exp(-0.005 * (hoje - datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d')).days) * (1.2 if j['league']['id'] == current_league_id else 1.0)
            is_home = j['teams']['home']['id'] == team_id
            gf, gs = (j['goals']['home'], j['goals']['away']) if is_home else (j['goals']['away'], j['goals']['home'])
            
            xg_f = buscar_stats_partida(f_id, team_id, gf) if idx < 6 else gf * 0.9
            xg_s = buscar_stats_partida(f_id, j['teams']['away']['id'] if is_home else j['teams']['home']['id'], gs) if idx < 6 else gs * 0.9
            
            total_gols_f += gf * peso_final; total_gols_s += gs * peso_final
            total_xg_f += xg_f * peso_final; total_xg_s += xg_s * peso_final
            soma_pesos += peso_final
            if idx < 5: forma.append("🟩" if gf > gs else "⬜" if gf == gs else "🟥")

        return {
            "media_feita": total_gols_f / soma_pesos, "media_sofrida": total_gols_s / soma_pesos, 
            "media_xg_f": total_xg_f / soma_pesos, "media_xg_s": total_xg_s / soma_pesos, 
            "forma": "".join(forma[::-1])
        }
    except: return None

def buscar_odds_vips(fixture_id):
    url = f"{BASE_URL}/odds"
    params = {'fixture': fixture_id} 
    odds = {"BTTS":0, "OVER_15":0, "UNDER_15":0, "OVER_25":0, "UNDER_25":0, "OVER_35":0, "UNDER_35":0, "HOME":0, "DRAW":0, "AWAY":0, "1X":0, "X2":0}
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
                    if bet['name'] == 'Both Teams Score': odds['BTTS'] = float(bet['values'][0]['odd'])
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
# 4. MOTOR DE ANÁLISE QUANTITATIVA
# ==========================================
def acao_analisar(jogos_alvo, data_str, force=False):
    if "stats" not in banco_local["datas"][data_str]: banco_local["datas"][data_str]["stats"] = {}
    p_bar = st.progress(0); saldo_atual = atualizar_saldo_realtime()
    for idx, jogo in enumerate(jogos_alvo):
        if saldo_atual is not None and saldo_atual < 40:
            st.error(f"⚠️ FREIO DE EMERGÊNCIA!"); time.sleep(3); break 
        f_id = str(jogo['fixture']['id'])
        if force or f_id not in banco_local["datas"][data_str]["stats"]:
            h_id, a_id, l_id = jogo['teams']['home']['id'], jogo['teams']['away']['id'], jogo['league']['id']
            odds = buscar_odds_vips(f_id)
            s_h = buscar_historico_global(h_id, l_id)
            s_a = buscar_historico_global(a_id, l_id)
            if s_h and s_a: 
                banco_local["datas"][data_str]["stats"][f_id] = {"odds": odds, "h": s_h, "a": s_a, "l_id": l_id}
                salvar_banco(banco_local)
            else: 
                banco_local["datas"][data_str]["stats"][f_id] = {"erro": "Sem histórico suficiente"}
            if saldo_atual is not None: saldo_atual -= 17 
            time.sleep(0.2)
        p_bar.progress((idx + 1) / len(jogos_alvo))
    p_bar.empty(); st.rerun()

def calcular_matematica_quant(d):
    coef_liga = PESOS_LIGAS.get(d.get('l_id', 0), 0.95) 
    m_h = ((d['h']['media_xg_f'] + d['a']['media_xg_s']) / 2) * coef_liga * 1.05
    m_a = ((d['a']['media_xg_f'] + d['h']['media_xg_s']) / 2) * coef_liga * 0.95 
    return m_h, m_a

def normalizar_prob_mercado(dados, key):
    odds = dados['odds']
    odd_alvo = odds.get(key, 0)
    if odd_alvo <= 1.0: return 0
    margem = 0
    if key in ["HOME", "DRAW", "AWAY"]:
        if odds.get("HOME") and odds.get("DRAW") and odds.get("AWAY"): margem = (1/odds["HOME"]) + (1/odds["DRAW"]) + (1/odds["AWAY"])
    elif key in ["OVER_15", "UNDER_15"]:
        if odds.get("OVER_15") and odds.get("UNDER_15"): margem = (1/odds["OVER_15"]) + (1/odds["UNDER_15"])
    elif key in ["OVER_25", "UNDER_25"]:
        if odds.get("OVER_25") and odds.get("UNDER_25"): margem = (1/odds["OVER_25"]) + (1/odds["UNDER_25"])
    elif key in ["OVER_35", "UNDER_35"]:
        if odds.get("OVER_35") and odds.get("UNDER_35"): margem = (1/odds["OVER_35"]) + (1/odds["UNDER_35"])
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
            if l_id in [39, 140, 135]:
                return (prob_nossa * 0.65) + (prob_mercado * 0.35)
            elif l_id in [78, 61, 2, 3]:
                return (prob_nossa * 0.75) + (prob_mercado * 0.25)
            elif l_id in [71, 72, 73]:
                return (prob_nossa * 0.80) + (prob_mercado * 0.20)
            else:
                return (prob_nossa * 0.90) + (prob_mercado * 0.10)
    return prob_nossa

def calcular_kelly(prob_blended, odd):
    if odd <= 1.0 or prob_blended <= 0:
        return 0
    p = prob_blended / 100.0
    q = 1 - p
    b = odd - 1
    if b <= 0: return 0
    kelly_puro = (b * p - q) / b
    kelly_ajustado = max(0, kelly_puro * 0.10)
    return min(kelly_ajustado, 0.03)

def get_ev(dados, p_dict, key):
    casa = dados['odds'].get(key, 0)
    prob_modelo = p_dict[key]['prob']
    if casa <= 1.0 or prob_modelo <= 0:
        return -100
    ev = ((prob_modelo / 100.0) * casa - 1) * 100
    if ev > 60 or (prob_modelo < 35 and key in ["HOME", "AWAY"]):
        return -999
    return ev

def avaliar_perfil_jogo(p_dict):
    if p_dict["UNDER_25"]["prob"] > 60: return "🧱 JOGO TRAVADO"
    elif p_dict["OVER_25"]["prob"] > 55: return "🧨 JOGO ABERTO"
    else: return "⚖️ JOGO EQUILIBRADO"

def renderizar_mercado(col, titulo, p_dict, key, odds_dict, dados, banca_atual):
    prob_blended = get_blended_prob(dados, p_dict, key)
    casa = odds_dict.get(key, 0)
    ev = get_ev(dados, p_dict, key)
    justa = 100 / prob_blended if prob_blended > 0 else 0
    frac_kelly = calcular_kelly(prob_blended, casa)
    stake_sugerida = max(0, frac_kelly * banca_atual)

    icone_fogo = "🔥" if 15 < ev < 60 else ""
    badge_html = f'<div style="color:#28a745; font-size:11px; font-weight:bold; margin-top:3px;">VALOR {icone_fogo} (+{ev:.1f}%)</div>' if 3 < ev < 60 else ''
    kelly_html = f'<div style="color:#17a2b8; font-size:10px; margin-top:4px;">🎯 Stake: R$ {stake_sugerida:.2f} ({(frac_kelly*100):.1f}%)</div>' if frac_kelly > 0 and ev > 3 else ''
    estilo = "border:1px solid #28a745; background-color:#1a2b1f;" if 3 < ev < 60 else "border:1px solid #333; background-color:#111;"

    html = f'''
    <div style="{estilo} padding:8px; border-radius:6px; text-align:center; margin-bottom:8px;">
        <div style="font-size:10px; color:#aaa; margin-bottom:2px; font-weight:bold;">{titulo}</div>
        <div style="font-size:16px; font-weight:bold; color:{"#28a745" if 3 < ev < 60 else "#fff"};">{prob_blended:.0f}%</div>
        <div style="font-size:11px; color:#FFFFFF; margin-top:4px;">J: {justa:.2f} | O: {casa if casa > 0 else "-"}</div>
        {badge_html}
        {kelly_html}
    </div>
    '''
    with col:
        st.markdown(html, unsafe_allow_html=True)

# ==========================================
# 4.1 MÓDULO DE INTELIGÊNCIA ARTIFICIAL
# ==========================================
def chamar_ia_fabrica(textos_jogos, modo="GOLS"):
    if modo == "GOLS":
        prompt_sistema = """Você é um Auditor Quantitativo Profissional especializado em apostas esportivas no mercado de gols.

Seu objetivo NÃO é listar muitas apostas, e sim identificar APENAS oportunidades com vantagem estatística real (value bets), priorizando qualidade sobre quantidade.

---
## 🎯 ORDENAÇÃO OBRIGATÓRIA E RANKING (TOP DOWN)
Você DEVE retornar a lista ranqueada do MELHOR jogo (maior Score/Valor/EV) para o PIOR. O card número 1 do seu relatório deve ser a aposta de maior segurança e valor do dia. A ordem dos resultados é fundamental para o painel do usuário.

---
## ⚙️ REGRAS GERAIS E RÉGUA ASSIMÉTRICA DE EV (CRÍTICO)
O mercado costuma inflacionar odds de Under e esmagar odds de Over. Portanto, aplique a seguinte régua:
1. Para aprovar UNDER: Exija um EV alto (ex: > 10%) E um xG Total projetado baixíssimo. Seja extremamente rigoroso com o contexto.
2. Para aprovar OVER ou BTTS: Aceite EVs menores (ex: > 3%). Se a odd tiver valor e o xG confirmar a tendência ofensiva das equipes, pode aprovar.
3. NÃO force mercados. Apenas siga os números.

---
## 💰 FILTRO DE ODDS (CRÍTICO)
- Odds < 1.70 → DESCARTAR SUMARIAMENTE (Proteção de banca para taxa de acerto)
- Odds 1.70–1.79 → Aceitável se o EV e xG forem perfeitos
- Odds ≥ 1.80 → Padrão mínimo ideal
- Odds ≥ 2.00 → ALTO VALOR (se coerente)

---
## 📊 CRITÉRIOS DE ANÁLISE
1. VALOR (CORE): EV (valor esperado) e Diferença entre probabilidade real vs implícita.
2. MODELAGEM: xG casa, xG fora, xG total e Coerência.
3. KELLY: Usar como indicador de vantagem. Kelly > 20% = FORTE PENALIZAÇÃO (risco de anomalia no modelo). Se for muito alto, rejeite ou exija cautela extrema.
4. CONTEXTO: Forma recente e variância.

---
## 🧮 SCORE DE QUALIDADE (0–100)
Baseado em: + Valor (EV), + Coerência xG, + Qualidade da odd, + Estabilidade.
Penalizações: Odd < 1.70 (Descarte automático), xG incompatível, Probabilidade inflada, Kelly > 20%.
✅ FILTRO FINAL: Score mínimo: 75 | Alta qualidade: ≥ 80 | Elite: ≥ 85

---
## 🎯 PERFIL DA APOSTA
Classificar cada pick:
- Conservador → prob alta + odd menor
- Equilibrado → boa relação risco/retorno
- Agressivo → odd alta + valor identificado

---
## 📤 FORMATO DE SAÍDA (Obrigatório para cada pick, do Maior Score para o Menor)
[ID: XXXX]
Jogo: Time A vs Time B
Mercado: (Over / Under / BTTS)
Odd: X.XX
Perfil: (Conservador / Equilibrado / Agressivo)

📊 Dados:
Probabilidade: XX%
EV: +X%
xG Total: X.XX
Kelly: XX%
Score: XX/100

🧠 Justificativa:
Explicação objetiva baseada em: Valor vs odd, Coerência do xG e a Régua Assimétrica.

⚠️ Risco:
Principal fator que pode quebrar a aposta.
"""
    else:
        prompt_sistema = """Você é um Analista Quantitativo Sênior. Sua missão é cruzar modelos matemáticos (xG, Poisson e EV) com o Momento Recente (Forma) das equipes para validar as melhores oportunidades em mercados de Resultado (Match Odds).

---
## 🎯 ORDENAÇÃO OBRIGATÓRIA E RANKING (TOP DOWN)
Você DEVE retornar a lista ranqueada da aposta de MAIOR confiança (melhor combinação de EV, xG e Forma) para a MENOR. O primeiro ID deve ser a aposta "premium" da rodada.

REGRAS DE OURO:
1. ANÁLISE MISTA: A decisão DEVE ser baseada em EV positivo (acima de 3.0). Use a "Forma" e os "Gols Pró/Sofridos" apenas para validar se o time sustenta a matemática.
2. ZERO ACHISMO: Proibido criar narrativas como "peso da camisa".
3. ALERTA DE VARIAÇÃO: Se a Forma for terrível, mas o modelo apontar valor, alerte sobre a ineficiência.

FORMATO OBRIGATÓRIO (Do melhor para o pior):
💎 APROVADOS PARA INVESTIMENTO:
[ID: XXXXXX] [NOME DO JOGO] 🎯 **[MERCADO SUGERIDO]**
* 📊 **Lógica Quantitativa:** [Justifique o cruzamento]
* ⚠️ **Ponto de Atenção:** [Destaque um risco real]
"""
   try:
        # 🎯 CADEADO DE TEMPERATURA ZERO (Respostas sempre 100% idênticas)
        configuracao = genai.types.GenerationConfig(temperature=0.0)
        return model_ia.generate_content(prompt_sistema + "\n\n📋 DADOS PARA ANÁLISE:\n\n" + textos_jogos, generation_config=configuracao).text
    except Exception as e: return f"🚨 Erro na IA: {e}"

# ==========================================
# 5. INTERFACE COMPLETA 
# ==========================================
with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO")
    saldo = atualizar_saldo_realtime()
    st.metric("Créditos Disponíveis", f"{saldo if saldo else 0}/7500")
    st.progress(max(0.0, min((saldo if saldo else 0) / 7500, 1.0)))
    st.write("---")
    
    st.markdown("### 📈 Gestão de Risco (Kelly)")
    banca_input = st.number_input("Banca Total (R$)", value=float(banco_local.get("banca_inicial", 30.0)), step=10.0)
    if banca_input != banco_local.get("banca_inicial"):
        banco_local["banca_inicial"] = banca_input
        salvar_banco(banco_local)
        
    lucro_total = 0.0
    for p in banco_local["picks"]:
        status = p.get("status", "Pendente")
        stake_usada = p.get("stake", 1.0) 
        if status == "Green": lucro_total += stake_usada * (p.get("odd", 1.0) - 1.0)
        elif status == "Red": lucro_total -= stake_usada
            
    banca_atual = banco_local["banca_inicial"] + lucro_total
    st.metric("Saldo Atual", f"R$ {banca_atual:.2f}", f"P/L: R$ {lucro_total:.2f}")

    if st.button("📊 Diário de Bordo (Tracker)", use_container_width=True): st.session_state['ver_picks'] = not st.session_state.get('ver_picks', False)
    st.write("---")
    
    data_consulta = st.date_input("Data do Scanner", datetime.date.today())
    data_str = data_consulta.strftime("%Y-%m-%d")
    
    LIGAS_PRO = [39, 140, 135, 78, 61, 71, 72, 73, 2, 3, 848, 13, 11, 40, 88, 307, 253, 94, 128, 203]
    tipo_filtro = st.radio("Filtro de Ligas:", ["🏆 Só Ligas PRO", "🌍 PRO + Confiáveis", "🗑️ O Mundo Todo"], index=1)
    st.write("---")
    if st.button("🗑️ Limpar Cache do Dia"):
        if data_str in banco_local["datas"]: del banco_local["datas"][data_str]; salvar_banco(banco_local); st.rerun()

# PAINEL DE RESOLUÇÃO
if st.session_state.get('ver_picks', False):
    st.markdown("### 📋 Diário de Bordo")
    if not banco_local["picks"]: st.info("Vazio.")
    else:
        for i, p in enumerate(reversed(banco_local["picks"])):
            real_idx = len(banco_local["picks"]) - 1 - i
            st_atual = p.get("status", "Pendente")
            icon = "⏳" if st_atual == "Pendente" else "✅" if st_atual == "Green" else "❌" if st_atual == "Red" else "➖"
            
            with st.expander(f"{icon} {p['data']} | {p['jogo']} | 🎯 {p['mercado']}"):
                st.write(f"**Stake:** R$ {p.get('stake', 1.0):.2f} | **Odd:** {p.get('odd','-')} | **Prob:** {p.get('prob','-')}% | **EV:** {p.get('ev','-')}%")
                if st_atual == "Pendente":
                    c1, c2, c3 = st.columns(3)
                    if c1.button("✅ Green", key=f"g_{real_idx}", type="primary"):
                        banco_local["picks"][real_idx]["status"] = "Green"; salvar_banco(banco_local); st.rerun()
                    if c2.button("❌ Red", key=f"r_{real_idx}"):
                        banco_local["picks"][real_idx]["status"] = "Red"; salvar_banco(banco_local); st.rerun()
                    if c3.button("➖ Anular", key=f"v_{real_idx}"):
                        banco_local["picks"][real_idx]["status"] = "Devolvida"; salvar_banco(banco_local); st.rerun()
                else:
                    if st.button("↩️ Desfazer", key=f"undo_{real_idx}"):
                        banco_local["picks"][real_idx]["status"] = "Pendente"; salvar_banco(banco_local); st.rerun()
    st.write("---")

if data_str not in banco_local["datas"]: banco_local["datas"][data_str] = {"agenda": [], "stats": {}}
agenda = banco_local["datas"][data_str]["agenda"]

if st.button("🔄 1. Carregar Agenda do Dia", use_container_width=True):
    res = requests.get(f"{BASE_URL}/fixtures?date={data_str}&timezone=America/Sao_Paulo", headers=HEADERS).json()
    if res.get('response'): 
        banco_local["datas"][data_str]["agenda"] = res['response']
        salvar_banco(banco_local)
        st.rerun()
    else:
        st.error(f"🚨 A API-Sports não retornou nenhum jogo! Motivo: {res.get('errors', 'A grade de jogos está vazia para esta data.')}")

if agenda:
    jogos_visiveis = []
    palavras_proibidas = ['u19', 'u20', 'u21', 'u23', 'youth', 'women', 'feminino', 'reserve', 'amateur', 'regional', 'state']
    paises_confiaveis = ['Brazil', 'Argentina', 'USA', 'Mexico', 'Netherlands', 'Portugal', 'Turkey', 'Saudi-Arabia', 'Switzerland', 'Japan', 'Colombia', 'Chile', 'South-Korea', 'Scotland', 'Greece', 'Belgium', 'Uruguay', 'Ecuador', 'Paraguay', 'Bolivia', 'Peru', 'Venezuela']
    
    for j in agenda:
        l_id, l_name, l_country = j['league']['id'], str(j['league']['name']).lower(), str(j['league']['country'])
        if (tipo_filtro == "🏆 Só Ligas PRO" and l_id in LIGAS_PRO) or (tipo_filtro == "🌍 PRO + Confiáveis" and (l_id in LIGAS_PRO or (l_country in paises_confiaveis and not any(p in l_name for p in palavras_proibidas)))) or tipo_filtro == "🗑️ O Mundo Todo":
            if j not in jogos_visiveis: jogos_visiveis.append(j)

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button(f"🚀 2. Analisar TODOS Visíveis ({len(jogos_visiveis)})", type="primary", use_container_width=True): acao_analisar(jogos_visiveis, data_str)
    
    st.write("---")

    # ==========================================================
    # 5.1 INTELIGÊNCIA ARTIFICIAL (Auditoria Final)
    # ==========================================================
    st.write("### 🤖 Inteligência Artificial (Auditoria Final)")
    
    col_ia1, col_ia2 = st.columns(2)
    
    with col_ia1:
        if st.button("🧠 Filtrar com IA (GOLS)", use_container_width=True):
            textos = ""
            for j in jogos_visiveis:
                f_id = str(j['fixture']['id'])
                d = banco_local["datas"][data_str]["stats"].get(f_id)
                
                if d and "erro" not in d:
                    m_h, m_a = calcular_matematica_quant(d)
                    p = calcular_poisson(m_h, m_a)
                    
                    if p:
                        xg_total = m_h + m_a
                        k_o15 = calcular_kelly(get_blended_prob(d, p, 'OVER_15'), d['odds'].get('OVER_15', 0)) * 100
                        k_u25 = calcular_kelly(get_blended_prob(d, p, 'UNDER_25'), d['odds'].get('UNDER_25', 0)) * 100
                        k_o25 = calcular_kelly(get_blended_prob(d, p, 'OVER_25'), d['odds'].get('OVER_25', 0)) * 100
                        k_u35 = calcular_kelly(get_blended_prob(d, p, 'UNDER_35'), d['odds'].get('UNDER_35', 0)) * 100
                        k_btts = calcular_kelly(get_blended_prob(d, p, 'BTTS'), d['odds'].get('BTTS', 0)) * 100

                        linha = f"""
ID: {f_id} | {j['teams']['home']['name']} vs {j['teams']['away']['name']}
- Forma Casa: {d['h']['forma']} | Gols Pró: {d['h']['media_feita']:.2f} | xG Casa: {m_h:.2f}
- Forma Fora: {d['a']['forma']} | Gols Pró: {d['a']['media_feita']:.2f} | xG Fora: {m_a:.2f}
- xG Total Projetado: {xg_total:.2f}
- Over 1.5 -> Odd: {d['odds'].get('OVER_15', 0)} | Prob: {p['OVER_15']['prob']:.1f}% | EV: {get_ev(d, p, 'OVER_15'):.1f}% | Kelly: {k_o15:.1f}%
- Under 2.5 -> Odd: {d['odds'].get('UNDER_25', 0)} | Prob: {p['UNDER_25']['prob']:.1f}% | EV: {get_ev(d, p, 'UNDER_25'):.1f}% | Kelly: {k_u25:.1f}%
- Over 2.5 -> Odd: {d['odds'].get('OVER_25', 0)} | Prob: {p['OVER_25']['prob']:.1f}% | EV: {get_ev(d, p, 'OVER_25'):.1f}% | Kelly: {k_o25:.1f}%
- Under 3.5 -> Odd: {d['odds'].get('UNDER_35', 0)} | Prob: {p['UNDER_35']['prob']:.1f}% | EV: {get_ev(d, p, 'UNDER_35'):.1f}% | Kelly: {k_u35:.1f}%
- BTTS (Ambas) -> Odd: {d['odds'].get('BTTS', 0)} | Prob: {p['BTTS']['prob']:.1f}% | EV: {get_ev(d, p, 'BTTS'):.1f}% | Kelly: {k_btts:.1f}%
"""
                        textos += linha + "\n"
            
            with st.spinner("IA Rankeando Melhores Apostas de Gols..."):
                resposta = chamar_ia_fabrica(textos, modo="GOLS")
                st.session_state["ia_gols"] = resposta
                st.session_state["ids_gols"] = re.findall(r'\[ID:\s*(\d+)\]', resposta)

    with col_ia2:
        if st.button("⚔️ Filtrar com IA (RESULTADO)", use_container_width=True):
            textos = ""
            for j in jogos_visiveis:
                f_id = str(j['fixture']['id'])
                d = banco_local["datas"][data_str]["stats"].get(f_id)
                
                if d and "erro" not in d:
                    m_h, m_a = calcular_matematica_quant(d)
                    p = calcular_poisson(m_h, m_a)
                    
                    if p:
                        linha = f"""
ID: {f_id} | {j['teams']['home']['name']} vs {j['teams']['away']['name']}
- Forma Casa: {d['h']['forma']} | Gols Pró: {d['h']['media_feita']:.2f} | Sofre: {d['h']['media_sofrida']:.2f} | xG Pró: {d['h']['media_xg_f']:.2f}
- Forma Fora: {d['a']['forma']} | Gols Pró: {d['a']['media_feita']:.2f} | Sofre: {d['a']['media_sofrida']:.2f} | xG Pró: {d['a']['media_xg_f']:.2f}
- Projeção do Jogo (Poisson): Casa {m_h:.2f} vs Fora {m_a:.2f}
- Vitória Casa -> Odd: {d['odds'].get('HOME', 0)} | Prob: {p['HOME']['prob']:.1f}% | EV: {get_ev(d, p, 'HOME'):.1f}%
- Empate -> Odd: {d['odds'].get('DRAW', 0)} | Prob: {p['DRAW']['prob']:.1f}% | EV: {get_ev(d, p, 'DRAW'):.1f}%
- Vitória Fora -> Odd: {d['odds'].get('AWAY', 0)} | Prob: {p['AWAY']['prob']:.1f}% | EV: {get_ev(d, p, 'AWAY'):.1f}%
- Dupla Casa (1X) -> Odd: {d['odds'].get('1X', 0)} | Prob: {p['1X']['prob']:.1f}% | EV: {get_ev(d, p, '1X'):.1f}%
- Dupla Fora (X2) -> Odd: {d['odds'].get('X2', 0)} | Prob: {p['X2']['prob']:.1f}% | EV: {get_ev(d, p, 'X2'):.1f}%
"""
                        textos += linha + "\n"
            
            with st.spinner("IA Rankeando Melhores Resultados..."):
                resposta = chamar_ia_fabrica(textos, modo="RESULTADO")
                st.session_state["ia_resultado"] = resposta
                st.session_state["ids_res"] = re.findall(r'\[ID:\s*(\d+)\]', resposta)

    st.write("---")
    
    if "ia_gols" in st.session_state:
        st.markdown("#### 🔥 Ranking IA - GOLS")
        if st.session_state["ia_gols"].strip() == "":
            st.warning("Nenhum jogo atendeu aos rigorosos critérios de valor para Gols.")
        else:
            st.info(st.session_state["ia_gols"])
            
    if "ia_resultado" in st.session_state:
        st.markdown("#### ⚔️ Ranking IA - RESULTADO")
        if st.session_state["ia_resultado"].strip() == "":
            st.warning("Nenhum jogo atendeu aos rigorosos critérios de valor para Resultado.")
        else:
            st.info(st.session_state["ia_resultado"])
    
    # ==========================================================
    # ORDENAÇÃO INTELIGENTE DOS CARDS (TOP DOWN RANKING)
    # ==========================================================
    ids_gols_rank = st.session_state.get("ids_gols", [])
    jogos_gols_sorted = sorted(jogos_visiveis, key=lambda j: ids_gols_rank.index(str(j['fixture']['id'])) if str(j['fixture']['id']) in ids_gols_rank else 999999)

    ids_res_rank = st.session_state.get("ids_res", [])
    jogos_res_sorted = sorted(jogos_visiveis, key=lambda j: ids_res_rank.index(str(j['fixture']['id'])) if str(j['fixture']['id']) in ids_res_rank else 999999)

    # ==========================================================
    # ABAS PRINCIPAIS DE CÁLCULO E RENDERIZAÇÃO
    # ==========================================================
    tab_gols, tab_result = st.tabs(["🔥 MODO GOLS", "⚔️ MODO RESULTADO"])
    
    dict_m_gols = {"OVER_15": "Over 1.5", "OVER_25": "Over 2.5", "OVER_35": "Over 3.5", "UNDER_25": "Under 2.5", "UNDER_35": "Under 3.5", "BTTS": "Ambas Marcam"}
    dict_m_res = {"HOME": "Vitória Casa", "DRAW": "Empate", "AWAY": "Vitória Fora", "1X": "Dupla Casa", "X2": "Dupla Fora"}

    # ============================== ABA GOLS
    with tab_gols:
        for j in jogos_gols_sorted:
            f_id = str(j['fixture']['id'])
            d = banco_local["datas"][data_str]["stats"].get(f_id)
            
            # SE AINDA NÃO FOI ANALISADO (CARD MINI)
            if not d or "erro" in d:
                st.markdown(f"""<div style='border: 1px solid #333; border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:5px; border-left: 4px solid #555;'>
                    <div style='color:#888; font-size:11px;'>🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</div>
                    <div style='font-size:16px; font-weight:bold; color:white; margin-top:4px;'>{j['teams']['home']['name']} <span style='color:#555; font-size:12px;'>vs</span> {j['teams']['away']['name']}</div>
                </div>""", unsafe_allow_html=True)
                if st.button("📊 Analisar Jogo", key=f"btn_mini_gols_{f_id}"):
                    acao_analisar([j], data_str, force=True)

            # SE JÁ FOI ANALISADO COM SUCESSO (CARD FULL)
            else:
                m_h, m_a = calcular_matematica_quant(d); p = calcular_poisson(m_h, m_a)
                if p:
                    perfil = avaliar_perfil_jogo(p)
                    cor_perfil = "#dc3545" if "ABERTO" in perfil else "#6c757d" if "TRAVADO" in perfil else "#ffc107"
                    
                    borda_rank = "border: 2px solid #ffcc00;" if f_id in ids_gols_rank else "border: 1px solid #333;"
                    posicao = f"<span style='color:#ffcc00; font-weight:bold; margin-right:8px;'>TOP {ids_gols_rank.index(f_id) + 1}</span>" if f_id in ids_gols_rank else ""

                    st.markdown(f"""<div style='{borda_rank} border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:10px;'>
                        <div style='display:flex; justify-content:space-between; color:#888; font-size:11px;'><span>{posicao} 🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span><span style='color:{cor_perfil}; font-weight:bold;'>{perfil}</span></div>
                        <div style='font-size:18px; font-weight:bold; color:white; margin: 8px 0;'>{j['teams']['home']['name']} <span style='color:#555; font-size:12px;'>vs</span> {j['teams']['away']['name']}</div>
                        """, unsafe_allow_html=True)
                    cols = st.columns(3); idx_col = 0
                    for m_key in dict_m_gols.keys(): 
                        renderizar_mercado(cols[idx_col % 3], dict_m_gols[m_key], p, m_key, d['odds'], d, banca_atual); idx_col += 1
                    
                    with st.expander("📊 Info Detalhada, Salvar & Atualizar"):
                        col_info_h, col_info_a = st.columns(2)
                        with col_info_h:
                            st.markdown(f"🏠 **{j['teams']['home']['name']}**")
                            st.markdown(f"**Forma Recente:** {d['h']['forma']}")
                            st.markdown(f"**xG Médio (Faz):** {d['h']['media_xg_f']:.2f}")
                            st.markdown(f"**xG Médio (Sofre):** {d['h']['media_xg_s']:.2f}")
                        with col_info_a:
                            st.markdown(f"✈️ **{j['teams']['away']['name']}**")
                            st.markdown(f"**Forma Recente:** {d['a']['forma']}")
                            st.markdown(f"**xG Médio (Faz):** {d['a']['media_xg_f']:.2f}")
                            st.markdown(f"**xG Médio (Sofre):** {d['a']['media_xg_s']:.2f}")
                        
                        st.write("---")
                        
                        c_sel, c_stk, c_btn, c_upd = st.columns([2, 1, 1, 1])
                        mk_sel = c_sel.selectbox("Mercado:", list(dict_m_gols.keys()), format_func=lambda x: dict_m_gols[x], key=f"sg_{f_id}", label_visibility="collapsed")
                        stake_sug = calcular_kelly(get_blended_prob(d, p, mk_sel), d['odds'].get(mk_sel,0)) * banca_atual
                        stk_input = c_stk.number_input("R$", value=float(max(1.0, round(stake_sug, 2))), step=1.0, key=f"stkg_{f_id}")
                        
                        if c_btn.button("✅ Salvar", key=f"bsg_{f_id}"):
                            banco_local["picks"].append({"data": data_str, "jogo": f"{j['teams']['home']['name']} v {j['teams']['away']['name']}", "mercado": dict_m_gols[mk_sel], "odd": d['odds'].get(mk_sel, 0), "prob": round(get_blended_prob(d, p, mk_sel),1), "ev": round(get_ev(d, p, mk_sel),1), "status": "Pendente", "stake": stk_input})
                            salvar_banco(banco_local); st.toast("Salvo!")
                            
                        if c_upd.button("🔄 Atualizar", key=f"upd_gols_{f_id}"):
                            acao_analisar([j], data_str, force=True)
                            
                    st.markdown("</div>", unsafe_allow_html=True)

    # ============================== ABA RESULTADO
    with tab_result:
        for j in jogos_res_sorted:
            f_id = str(j['fixture']['id'])
            d = banco_local["datas"][data_str]["stats"].get(f_id)
            
            # SE AINDA NÃO FOI ANALISADO (CARD MINI)
            if not d or "erro" in d:
                st.markdown(f"""<div style='border: 1px solid #333; border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:5px; border-left: 4px solid #555;'>
                    <div style='color:#888; font-size:11px;'>🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</div>
                    <div style='font-size:16px; font-weight:bold; color:white; margin-top:4px;'>{j['teams']['home']['name']} <span style='color:#555; font-size:12px;'>vs</span> {j['teams']['away']['name']}</div>
                </div>""", unsafe_allow_html=True)
                if st.button("📊 Analisar Jogo", key=f"btn_mini_res_{f_id}"):
                    acao_analisar([j], data_str, force=True)

            # SE JÁ FOI ANALISADO COM SUCESSO (CARD FULL)
            else:
                m_h, m_a = calcular_matematica_quant(d); p = calcular_poisson(m_h, m_a)
                if p:
                    borda_rank = "border: 2px solid #ffcc00;" if f_id in ids_res_rank else "border: 1px solid #333;"
                    posicao = f"<span style='color:#ffcc00; font-weight:bold; margin-right:8px;'>TOP {ids_res_rank.index(f_id) + 1}</span>" if f_id in ids_res_rank else ""

                    st.markdown(f"""<div style='{borda_rank} border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:10px;'>
                        <div style='display:flex; justify-content:space-between; color:#888; font-size:11px;'><span>{posicao} 🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span></div>
                        <div style='font-size:18px; font-weight:bold; color:white; margin: 8px 0;'>{j['teams']['home']['name']} <span style='color:#555; font-size:12px;'>vs</span> {j['teams']['away']['name']}</div>
                        """, unsafe_allow_html=True)
                    cols = st.columns(3); idx_col = 0
                    for m_key in dict_m_res.keys(): 
                        renderizar_mercado(cols[idx_col % 3], dict_m_res[m_key], p, m_key, d['odds'], d, banca_atual); idx_col += 1
                    
                    with st.expander("📊 Info Detalhada, Salvar & Atualizar"):
                        col_info_h, col_info_a = st.columns(2)
                        with col_info_h:
                            st.markdown(f"🏠 **{j['teams']['home']['name']}**")
                            st.markdown(f"**Forma Recente:** {d['h']['forma']}")
                            st.markdown(f"**xG Médio (Faz):** {d['h']['media_xg_f']:.2f}")
                            st.markdown(f"**xG Médio (Sofre):** {d['h']['media_xg_s']:.2f}")
                        with col_info_a:
                            st.markdown(f"✈️ **{j['teams']['away']['name']}**")
                            st.markdown(f"**Forma Recente:** {d['a']['forma']}")
                            st.markdown(f"**xG Médio (Faz):** {d['a']['media_xg_f']:.2f}")
                            st.markdown(f"**xG Médio (Sofre):** {d['a']['media_xg_s']:.2f}")
                        
                        st.write("---")
                        
                        c_sel, c_stk, c_btn, c_upd = st.columns([2, 1, 1, 1])
                        mk_sel = c_sel.selectbox("Mercado:", list(dict_m_res.keys()), format_func=lambda x: dict_m_res[x], key=f"sr_{f_id}", label_visibility="collapsed")
                        stake_sug = calcular_kelly(get_blended_prob(d, p, mk_sel), d['odds'].get(mk_sel,0)) * banca_atual
                        stk_input = c_stk.number_input("R$", value=float(max(1.0, round(stake_sug, 2))), step=1.0, key=f"stkr_{f_id}")
                        
                        if c_btn.button("✅ Salvar", key=f"bsr_{f_id}"):
                            banco_local["picks"].append({"data": data_str, "jogo": f"{j['teams']['home']['name']} v {j['teams']['away']['name']}", "mercado": dict_m_res[mk_sel], "odd": d['odds'].get(mk_sel, 0), "prob": round(get_blended_prob(d, p, mk_sel),1), "ev": round(get_ev(d, p, mk_sel),1), "status": "Pendente", "stake": stk_input})
                            salvar_banco(banco_local); st.toast("Salvo!")
                            
                        if c_upd.button("🔄 Atualizar", key=f"upd_res_{f_id}"):
                            acao_analisar([j], data_str, force=True)
                            
                    st.markdown("</div>", unsafe_allow_html=True)
