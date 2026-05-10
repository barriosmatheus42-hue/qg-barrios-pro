# app.py — QG Barrios PRO V7.1
# Motor: Dixon-Coles (1997) com τ correto (ρ = -0.13)
# Fixes V7.1:
#   - τ Dixon-Coles corrigido (0-0 e 1-1 boosted, correto empiricamente)
#   - Separação casa/fora obrigatória no histórico
#   - xG puro sem mistura com SOG
#   - Kelly por score IA (25%/15%/10%)
#   - EV sem filtros arbitrários
#   - Remoção do ajuste manual de empate +6%
#   - Blending: ligas menores = mais peso ao modelo
#   - Diário de Bordo como st.dialog (popup flutuante)
#   - Relatório IA persistido no JSON (não some mais)
#   - Botão salvar direto no relatório IA
#   - Toggle "Só Aprovados pela IA"
#   - Salvar pick sem st.rerun() (sem piscar)

import streamlit as st
import requests
import datetime
import time
import math
import json
import os
import google.generativeai as genai
import re

st.set_page_config(
    page_title="QG Barrios PRO V7.1",
    layout="wide",
    page_icon="👑"
)

st.markdown("""<style>
[data-testid="stSidebar"] { background-color: #0c0c14; border-right: 1px solid #1a1a2e; }
.stApp { background-color: #08080f; color: #e0e0e0; }
div[data-testid="metric-container"] {
    background-color: #0f0f1a;
    border-radius: 8px;
    padding: 8px 12px;
    border: 1px solid #1a1a2e;
}
div[data-testid="stTabs"] button { color: #888 !important; }
div[data-testid="stTabs"] button[aria-selected="true"] {
    color: #fff !important;
    border-bottom: 2px solid #ffcc00 !important;
}
.stExpander { border: 1px solid #1a1a2e !important; background: #0a0a12 !important; }
button[kind="primary"] { background-color: #1a3a1a !important; border-color: #28a745 !important; }
</style>""", unsafe_allow_html=True)

# ==========================================
# 1. CONFIGURAÇÕES GLOBAIS
# ==========================================
API_KEY_PRO = "00374ab0590422053c950ddc399a0ccb"
BASE_URL    = "https://v3.football.api-sports.io"
HEADERS     = {'x-apisports-key': API_KEY_PRO}

JSONBIN_KEY    = st.secrets["JSONBIN_KEY"]
JSONBIN_BIN_ID = st.secrets["JSONBIN_BIN_ID"]
JSONBIN_URL    = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
JSONBIN_H      = {"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"}

API_KEY_GEMINI = st.secrets["GEMINI_API_KEY"]
genai.configure(api_key=API_KEY_GEMINI)

# Coeficiente de qualidade da liga (afeta cálculo do λ)
PESOS_LIGAS = {
    39: 1.00, 140: 0.97, 135: 0.97,
    78: 0.96,  61: 0.95,
    71: 0.88,  72: 0.83,  73: 0.75,
    2:  1.00,   3: 0.92,
}

# Vantagem em casa calibrada por liga (fonte: análise histórica 5+ anos)
HOME_ADV = {
    39: 1.16,   # Premier League
    140: 1.18,  # La Liga
    135: 1.17,  # Serie A
    78: 1.19,   # Bundesliga
    61: 1.15,   # Ligue 1
    71: 1.28,   # Brasileirão A
    72: 1.30,   # Brasileirão B
    73: 1.32,   # Brasileirão C
    2: 1.10,    # UCL
    3: 1.08,    # UEL
}
HOME_ADV_DEFAULT = 1.22

LIGAS_PRO = [
    39, 140, 135, 78, 61, 71, 72, 73, 2, 3,
    848, 13, 11, 40, 88, 307, 253, 94, 128, 203
]
CUSTO_POR_JOGO = 9   # créditos estimados por análise completa
RHO = -0.13          # parâmetro Dixon-Coles (negativo = boost 0-0 e 1-1)

# ==========================================
# 2. BANCO DE DADOS HÍBRIDO
# ==========================================
ARQUIVO_BANCO = "banco_barrios_v71.json"

def carregar_banco():
    if "banco_local" in st.session_state:
        return st.session_state["banco_local"]

    banco = {
        "datas": {},
        "picks": [],
        "banca_inicial": 29.0,
        "ia_cache": {}
    }

    # Carregar cache local (agenda + stats + ia_cache)
    if os.path.exists(ARQUIVO_BANCO):
        try:
            with open(ARQUIVO_BANCO, "r") as f:
                b = json.load(f)
                banco["datas"]    = b.get("datas", {})
                banco["ia_cache"] = b.get("ia_cache", {})
        except:
            pass

    # Carregar picks e banca da nuvem (fonte da verdade para P/L)
    try:
        res = requests.get(f"{JSONBIN_URL}/latest", headers=JSONBIN_H, timeout=10)
        if res.status_code == 200:
            nuvem = res.json().get("record", {})
            banco["picks"]        = nuvem.get("picks", [])
            banco["banca_inicial"] = nuvem.get("banca_inicial", 29.0)
    except:
        pass

    st.session_state["banco_local"] = banco
    return banco


def salvar_banco(dados, nuvem=True):
    st.session_state["banco_local"] = dados

    # Salvar local sempre
    try:
        with open(ARQUIVO_BANCO, "w") as f:
            json.dump(dados, f)
    except:
        pass

    # Salvar nuvem só quando necessário (picks / banca)
    if nuvem:
        try:
            payload = {
                "banca_inicial": dados.get("banca_inicial", 29.0),
                "picks": dados.get("picks", [])
            }
            h = {**JSONBIN_H, "X-Bin-Versioning": "false"}
            requests.put(JSONBIN_URL, headers=h, json=payload, timeout=10)
        except:
            pass


banco = carregar_banco()

# ==========================================
# 3. FUNÇÕES DE API
# ==========================================
def buscar_creditos():
    try:
        res = requests.get(f"{BASE_URL}/status", headers=HEADERS, timeout=5).json()
        if res.get('response'):
            r = res['response']['requests']
            return r['limit_day'] - r['current']
    except:
        pass
    return 7500


def buscar_xg_partida(fixture_id, team_id):
    """Retorna (xg_float, tem_xg_real). Nunca mistura com SOG."""
    try:
        res = requests.get(
            f"{BASE_URL}/fixtures/statistics",
            headers=HEADERS,
            params={'fixture': fixture_id, 'team': team_id}
        ).json()
        if res.get('response') and res['response']:
            for s in res['response'][0]['statistics']:
                if s['type'] == 'expected_goals' and s['value'] not in [None, 'None', '']:
                    return float(s['value']), True
    except:
        pass
    return None, False


def _historico_generico(team_id, league_id, eh_casa):
    """Base para buscar histórico separado (casa ou fora)."""
    try:
        res = requests.get(
            f"{BASE_URL}/fixtures",
            headers=HEADERS,
            params={'team': team_id, 'last': 30, 'status': 'FT'}
        ).json()
        if not res.get('response'):
            return None

        # Filtrar apenas jogos no contexto correto
        if eh_casa:
            jogos = [j for j in res['response'] if j['teams']['home']['id'] == team_id]
        else:
            jogos = [j for j in res['response'] if j['teams']['away']['id'] == team_id]

        if len(jogos) < 3:
            return None

        jogos = jogos[:8]
        hoje = datetime.datetime.now()

        total_xg_f, total_xg_s, total_gf, total_gs, soma_pesos = 0, 0, 0, 0, 0
        forma = []
        tem_xg_real = False

        for idx, j in enumerate(jogos):
            data_j = datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d')
            dias = (hoje - data_j).days
            peso = math.exp(-0.005 * dias)
            if j['league']['id'] == league_id:
                peso *= 1.15

            if eh_casa:
                gf = j['goals']['home'] or 0
                gs = j['goals']['away'] or 0
                opp_id = j['teams']['away']['id']
            else:
                gf = j['goals']['away'] or 0
                gs = j['goals']['home'] or 0
                opp_id = j['teams']['home']['id']

            # xG puro: apenas nos 3 mais recentes para economizar créditos
            if idx < 3:
                xg_f, tem = buscar_xg_partida(j['fixture']['id'], team_id)
                xg_s_val, _ = buscar_xg_partida(j['fixture']['id'], opp_id)
                if tem:
                    tem_xg_real = True
                # Fallback: gols * 0.85 (sem mistura com SOG)
                xg_f = xg_f if xg_f is not None else gf * 0.85
                xg_s = xg_s_val if xg_s_val is not None else gs * 0.85
            else:
                xg_f = gf * 0.85
                xg_s = gs * 0.85

            total_xg_f += xg_f * peso
            total_xg_s += xg_s * peso
            total_gf   += gf * peso
            total_gs   += gs * peso
            soma_pesos += peso

            if idx < 5:
                forma.append("🟩" if gf > gs else "⬜" if gf == gs else "🟥")

        return {
            "xg_atk":      total_xg_f / soma_pesos,
            "xg_def":      total_xg_s / soma_pesos,
            "media_gols_f": total_gf / soma_pesos,
            "media_gols_s": total_gs / soma_pesos,
            "forma":       "".join(reversed(forma)),
            "n_jogos":     len(jogos),
            "tem_xg":      tem_xg_real,
        }
    except:
        return None


def buscar_historico_casa(team_id, league_id):
    """Histórico do mandante APENAS com jogos em casa."""
    return _historico_generico(team_id, league_id, eh_casa=True)


def buscar_historico_fora(team_id, league_id):
    """Histórico do visitante APENAS com jogos fora."""
    return _historico_generico(team_id, league_id, eh_casa=False)


def buscar_odds(fixture_id):
    odds = {
        "BTTS": 0, "OVER_15": 0, "UNDER_15": 0,
        "OVER_25": 0, "UNDER_25": 0, "OVER_35": 0, "UNDER_35": 0,
        "HOME": 0, "DRAW": 0, "AWAY": 0, "1X": 0, "X2": 0
    }
    try:
        res = requests.get(
            f"{BASE_URL}/odds",
            headers=HEADERS,
            params={'fixture': fixture_id}
        ).json()
        if not res.get('response'):
            return odds

        bookmakers = res['response'][0].get('bookmakers', [])
        bkm = None
        for tid in [8, 4, 1]:  # Bet365 > William Hill > Unibet
            bkm = next((b for b in bookmakers if b['id'] == tid), None)
            if bkm:
                break
        if not bkm and bookmakers:
            bkm = bookmakers[0]
        if not bkm:
            return odds

        for bet in bkm['bets']:
            nm = bet['name']
            if nm == 'Both Teams Score':
                for v in bet['values']:
                    if v['value'] == 'Yes':
                        odds['BTTS'] = float(v['odd'])
            elif nm == 'Goals Over/Under':
                for v in bet['values']:
                    val = v['value']
                    if val == 'Over 1.5':   odds['OVER_15']  = float(v['odd'])
                    elif val == 'Under 1.5': odds['UNDER_15'] = float(v['odd'])
                    elif val == 'Over 2.5':  odds['OVER_25']  = float(v['odd'])
                    elif val == 'Under 2.5': odds['UNDER_25'] = float(v['odd'])
                    elif val == 'Over 3.5':  odds['OVER_35']  = float(v['odd'])
                    elif val == 'Under 3.5': odds['UNDER_35'] = float(v['odd'])
            elif nm == 'Match Winner':
                for v in bet['values']:
                    if v['value'] == 'Home':   odds['HOME'] = float(v['odd'])
                    elif v['value'] == 'Draw': odds['DRAW'] = float(v['odd'])
                    elif v['value'] == 'Away': odds['AWAY'] = float(v['odd'])
            elif nm == 'Double Chance':
                for v in bet['values']:
                    if v['value'] == 'Home/Draw': odds['1X'] = float(v['odd'])
                    elif v['value'] == 'Draw/Away': odds['X2'] = float(v['odd'])
    except:
        pass
    return odds

# ==========================================
# 4. MOTOR DIXON-COLES
# ==========================================
def tau_dc(x, y, lam, mu, rho=RHO):
    """
    Correção Dixon-Coles para placares baixos.
    Com ρ = -0.13:
      τ(0,0) = 1 + 0.13·λ·μ  → boost 0-0  ✓
      τ(1,0) = 1 - 0.13·μ    → reduz 1-0  ✓
      τ(0,1) = 1 - 0.13·λ    → reduz 0-1  ✓
      τ(1,1) = 1 + 0.13       → boost 1-1  ✓
    """
    if   x == 0 and y == 0: return 1.0 - lam * mu * rho
    elif x == 1 and y == 0: return 1.0 + mu * rho
    elif x == 0 and y == 1: return 1.0 + lam * rho
    elif x == 1 and y == 1: return 1.0 - rho
    return 1.0


def calcular_lambdas(sh, sa, liga_id):
    """
    sh = stats do mandante com jogos em casa
    sa = stats do visitante com jogos fora
    Separação correta de contexto.
    """
    hadv = HOME_ADV.get(liga_id, HOME_ADV_DEFAULT)
    coef = PESOS_LIGAS.get(liga_id, 0.90)

    # λ: ataque do mandante em casa vs defesa do visitante fora
    lam = ((sh['xg_atk'] + sa['xg_def']) / 2.0) * hadv * coef

    # μ: ataque do visitante fora vs defesa do mandante em casa
    # visitante tem desvantagem proporcional à hadv
    away_disc = 1.0 - (hadv - 1.0) * 0.5
    mu = ((sa['xg_atk'] + sh['xg_def']) / 2.0) * away_disc * coef

    return max(lam, 0.15), max(mu, 0.10)


def motor_dc(lam, mu):
    """
    Monta matriz Dixon-Coles 10x10, normaliza e extrai
    todas as probabilidades de mercado + top 6 placares.
    """
    MAX = 9
    matriz = {}

    for x in range(MAX + 1):
        for y in range(MAX + 1):
            p_x = math.exp(-lam) * (lam ** x) / math.factorial(x)
            p_y = math.exp(-mu)  * (mu  ** y) / math.factorial(y)
            matriz[(x, y)] = p_x * p_y * tau_dc(x, y, lam, mu)

    total = sum(matriz.values())
    if total <= 0:
        return None, [], lam, mu
    matriz = {k: v / total for k, v in matriz.items()}

    p_home = sum(p for (x, y), p in matriz.items() if x > y)
    p_draw = sum(p for (x, y), p in matriz.items() if x == y)
    p_away = sum(p for (x, y), p in matriz.items() if x < y)
    p_btts = sum(p for (x, y), p in matriz.items() if x > 0 and y > 0)
    p_o15  = sum(p for (x, y), p in matriz.items() if x + y > 1)
    p_o25  = sum(p for (x, y), p in matriz.items() if x + y > 2)
    p_o35  = sum(p for (x, y), p in matriz.items() if x + y > 3)

    top_placares = sorted(matriz.items(), key=lambda kv: kv[1], reverse=True)[:6]

    probs = {
        "HOME":     p_home * 100,
        "DRAW":     p_draw * 100,
        "AWAY":     p_away * 100,
        "1X":       (p_home + p_draw) * 100,
        "X2":       (p_away + p_draw) * 100,
        "BTTS":     p_btts * 100,
        "OVER_15":  p_o15 * 100,
        "UNDER_15": (1 - p_o15) * 100,
        "OVER_25":  p_o25 * 100,
        "UNDER_25": (1 - p_o25) * 100,
        "OVER_35":  p_o35 * 100,
        "UNDER_35": (1 - p_o35) * 100,
    }
    return probs, top_placares, lam, mu

# ==========================================
# 5. EV & KELLY
# ==========================================
PARES_ODDS = {
    "HOME":     ["HOME", "DRAW", "AWAY"],
    "DRAW":     ["HOME", "DRAW", "AWAY"],
    "AWAY":     ["HOME", "DRAW", "AWAY"],
    "OVER_15":  ["OVER_15", "UNDER_15"],
    "UNDER_15": ["OVER_15", "UNDER_15"],
    "OVER_25":  ["OVER_25", "UNDER_25"],
    "UNDER_25": ["OVER_25", "UNDER_25"],
    "OVER_35":  ["OVER_35", "UNDER_35"],
    "UNDER_35": ["OVER_35", "UNDER_35"],
    "1X":       ["1X", "X2"],
    "X2":       ["1X", "X2"],
    "BTTS":     ["BTTS"],
}

def prob_mercado_normalizada(odds, key):
    """Remove a margem da bookmaker. Probabilidade real implícita."""
    o = odds.get(key, 0)
    if o <= 1.0:
        return 0.0
    grupo = PARES_ODDS.get(key, [key])
    margem = sum(1.0 / odds[k] for k in grupo if odds.get(k, 0) > 1.0)
    if margem <= 0:
        return (1.0 / o) * 100
    return ((1.0 / o) / margem) * 100


def blended_prob(p_dc, odds, key, liga_id):
    """
    Combina modelo DC com mercado.
    CORRIGIDO: ligas MENOS eficientes → mais peso ao modelo.
    ligas MAIS eficientes → mais confiança no mercado.
    """
    p_mkt = prob_mercado_normalizada(odds, key)
    if p_mkt <= 0:
        return p_dc

    if liga_id in [39, 140, 135]:        # top 3: mercado muito eficiente
        w_m = 0.35
    elif liga_id in [78, 61, 2, 3]:      # grandes ligas / UCL
        w_m = 0.25
    elif liga_id in [71]:                 # Brasileirão A
        w_m = 0.15
    elif liga_id in [72, 73]:             # Brasileirão B/C
        w_m = 0.10
    else:
        w_m = 0.08                        # ligas menores: quase só modelo

    return p_dc * (1 - w_m) + p_mkt * w_m


def calcular_ev(prob_blend, odd):
    """EV sem filtros arbitrários."""
    if odd <= 1.0 or prob_blend <= 0:
        return -999.0
    return ((prob_blend / 100.0) * odd - 1.0) * 100.0


def calcular_kelly(prob_blend, odd, score_ia=None, kelly_frac=0.10):
    """
    Kelly com fração por score IA:
      Score ≥ 85 → 25% do Kelly puro
      Score 75-84 → 15%
      Sem score → kelly_frac (configurável, padrão 10%)
    Teto fixo: 5% da banca por aposta.
    """
    if odd <= 1.0 or prob_blend <= 0:
        return 0.0
    p = prob_blend / 100.0
    b = odd - 1.0
    kelly_puro = (b * p - (1 - p)) / b
    if kelly_puro <= 0:
        return 0.0

    if score_ia is not None and score_ia >= 85:
        frac = 0.25
    elif score_ia is not None and score_ia >= 75:
        frac = 0.15
    else:
        frac = kelly_frac

    return min(kelly_puro * frac, 0.05)

# ==========================================
# 6. ENGINE DE ANÁLISE
# ==========================================
def analisar(jogos_alvo, data_str, force=False):
    if "stats" not in banco["datas"][data_str]:
        banco["datas"][data_str]["stats"] = {}
    stats_data = banco["datas"][data_str]["stats"]

    n = len(jogos_alvo)
    creditos_est = n * CUSTO_POR_JOGO
    saldo = buscar_creditos()

    st.info(f"⚡ {n} jogos · ~{creditos_est} créditos necessários · {saldo} disponíveis")

    if saldo < 30:
        st.error("🚨 Créditos insuficientes! Freio de emergência ativado.")
        return

    bar = st.progress(0)
    status_txt = st.empty()

    for idx, j in enumerate(jogos_alvo):
        f_id  = str(j['fixture']['id'])
        home  = j['teams']['home']['name']
        away  = j['teams']['away']['name']

        if not force and f_id in stats_data and "erro" not in stats_data[f_id]:
            bar.progress((idx + 1) / n)
            continue

        status_txt.caption(f"🔍 Analisando {home} vs {away}...")

        h_id = j['teams']['home']['id']
        a_id = j['teams']['away']['id']
        l_id = j['league']['id']

        sh = buscar_historico_casa(h_id, l_id)
        sa = buscar_historico_fora(a_id, l_id)

        if sh and sa:
            odds    = buscar_odds(f_id)
            tem_xg  = sh.get('tem_xg', False) or sa.get('tem_xg', False)
            stats_data[f_id] = {
                "sh": sh, "sa": sa,
                "odds": odds,
                "l_id": l_id,
                "tem_xg": tem_xg
            }
        else:
            stats_data[f_id] = {"erro": "Histórico insuficiente"}

        salvar_banco(banco, nuvem=False)
        time.sleep(0.15)
        bar.progress((idx + 1) / n)

    bar.empty()
    status_txt.empty()
    salvar_banco(banco)
    st.rerun()


def perfil_jogo(probs):
    if probs["UNDER_25"] > 60:
        return "🧱 TRAVADO", "#6c757d"
    elif probs["OVER_25"] > 55:
        return "🧨 ABERTO", "#dc3545"
    return "⚖️ EQUILIBRADO", "#ffc107"

# ==========================================
# 7. TRACKER — ESTATÍSTICAS
# ==========================================
def calcular_stats_tracker(picks):
    resolvidos = [p for p in picks if p.get('status') in ['Green', 'Red']]
    greens     = [p for p in resolvidos if p.get('status') == 'Green']
    total      = len(resolvidos)
    lucro      = 0.0
    stake_tot  = 0.0

    for p in resolvidos:
        s     = p.get('status')
        stake = p.get('stake', 1.0)
        odd   = p.get('odd', 1.0)
        stake_tot += stake
        if s == 'Green':
            lucro += stake * (odd - 1.0)
        else:
            lucro -= stake

    return {
        "total":      total,
        "greens":     len(greens),
        "reds":       total - len(greens),
        "pendentes":  len([p for p in picks if p.get('status') == 'Pendente']),
        "hit_rate":   (len(greens) / total * 100) if total > 0 else 0.0,
        "lucro":      lucro,
        "stake_total": stake_tot,
        "yield_pct":  (lucro / stake_tot * 100) if stake_tot > 0 else 0.0,
    }

# ==========================================
# 8. INTELIGÊNCIA ARTIFICIAL
# ==========================================
@st.cache_resource
def get_modelo_ia():
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                if 'flash' in m.name.lower():
                    return genai.GenerativeModel(m.name)
    except:
        pass
    return genai.GenerativeModel('gemini-pro')


def chamar_ia(textos, modo="GOLS"):
    if modo == "GOLS":
        prompt = """Você é um Auditor Quantitativo Profissional em apostas esportivas — mercado de gols.

## FILTROS ABSOLUTOS (descarte sumário, sem exceção):
- Odd < 1.70 → DESCARTADO
- EV < 5% → DESCARTADO
- Kelly > 30% → DESCARTADO (anomalia no modelo)
- λ_total < 1.5 com Over → DESCARTADO (mercado sem gols)
- λ_total > 3.5 com Under → DESCARTADO (incoerente)
- ⚠️ Sem xG → penalizar -5 pts no score

## SCORE (0-100):
- EV alto + odd boa + xG coerente + forma consistente = score alto
- Mínimo aprovação: 75 | Alto: ≥ 80 | Elite: ≥ 85

## REGRA ASSIMÉTRICA:
- Under: exige EV > 8% E λ_total confirmando pouca criação
- Over / BTTS: aceita EV > 5% se xG confirmar

## ORDENAÇÃO OBRIGATÓRIA: maior score primeiro.

## FORMATO (para cada pick aprovado):
[ID: XXXXXX] Time A vs Time B
Mercado: X | Odd: X.XX | Score: XX/100 | Perfil: Conservador/Equilibrado/Agressivo
📊 EV: +X.X% | Prob DC: XX% | λ_total: X.XX | Kelly: X.X%
🧠 [Justificativa objetiva — 2 linhas máximo]
⚠️ [Risco principal]
---

Se nenhum jogo passar em todos os filtros, responda exatamente: NENHUM PICK APROVADO"""

    else:
        prompt = """Você é um Analista Quantitativo Sênior — mercados de resultado (1X2 e Dupla Chance).

## FILTROS ABSOLUTOS:
- Odd < 1.60 → DESCARTADO
- EV < 4% → DESCARTADO
- Kelly > 25% → DESCARTADO
- ZERO narrativas subjetivas (peso da camisa, tradição, pressão da torcida etc.)

## SCORE (0-100): Mínimo 75 para aprovação.

## ORDENAÇÃO OBRIGATÓRIA: maior score primeiro.

## FORMATO:
[ID: XXXXXX] Time A vs Time B
Mercado: X | Odd: X.XX | Score: XX/100
📊 EV: +X.X% | Prob DC: XX% | λ casa: X.XX | λ fora: X.XX
🧠 [Justificativa quantitativa — 2 linhas]
⚠️ [Risco]
---

Se nenhum jogo passar: NENHUM PICK APROVADO"""

    try:
        cfg = genai.types.GenerationConfig(temperature=0.0)
        m = get_modelo_ia()
        return m.generate_content(
            prompt + "\n\n📋 DADOS PARA ANÁLISE:\n\n" + textos,
            generation_config=cfg
        ).text
    except Exception as e:
        return f"🚨 Erro na IA: {e}"


def montar_texto_gols(jogos, stats_data):
    linhas = ""
    for j in jogos:
        f_id = str(j['fixture']['id'])
        d = stats_data.get(f_id)
        if not d or "erro" in d:
            continue
        lam, mu = calcular_lambdas(d['sh'], d['sa'], d['l_id'])
        probs, _, _, _ = motor_dc(lam, mu)
        if not probs:
            continue

        xg_flag = "" if d.get('tem_xg') else " [SEM xG REAL]"

        def _bp(k): return blended_prob(probs[k], d['odds'], k, d['l_id'])
        def _ev(k): return calcular_ev(_bp(k), d['odds'].get(k, 0))
        def _k(k):  return calcular_kelly(_bp(k), d['odds'].get(k, 0)) * 100

        linhas += f"""
[ID: {f_id}] {j['teams']['home']['name']} vs {j['teams']['away']['name']}{xg_flag}
- λ_casa: {lam:.2f} | λ_fora: {mu:.2f} | λ_total: {lam+mu:.2f}
- Forma Casa: {d['sh']['forma']} | xG Atk: {d['sh']['xg_atk']:.2f} | xG Def: {d['sh']['xg_def']:.2f}
- Forma Fora: {d['sa']['forma']} | xG Atk: {d['sa']['xg_atk']:.2f} | xG Def: {d['sa']['xg_def']:.2f}
- Over 1.5  → Odd:{d['odds'].get('OVER_15',0):.2f} | DC:{_bp('OVER_15'):.1f}% | EV:{_ev('OVER_15'):.1f}% | K:{_k('OVER_15'):.1f}%
- Under 2.5 → Odd:{d['odds'].get('UNDER_25',0):.2f} | DC:{_bp('UNDER_25'):.1f}% | EV:{_ev('UNDER_25'):.1f}% | K:{_k('UNDER_25'):.1f}%
- Over 2.5  → Odd:{d['odds'].get('OVER_25',0):.2f} | DC:{_bp('OVER_25'):.1f}% | EV:{_ev('OVER_25'):.1f}% | K:{_k('OVER_25'):.1f}%
- Under 3.5 → Odd:{d['odds'].get('UNDER_35',0):.2f} | DC:{_bp('UNDER_35'):.1f}% | EV:{_ev('UNDER_35'):.1f}% | K:{_k('UNDER_35'):.1f}%
- Over 3.5  → Odd:{d['odds'].get('OVER_35',0):.2f} | DC:{_bp('OVER_35'):.1f}% | EV:{_ev('OVER_35'):.1f}% | K:{_k('OVER_35'):.1f}%
- BTTS      → Odd:{d['odds'].get('BTTS',0):.2f}    | DC:{_bp('BTTS'):.1f}% | EV:{_ev('BTTS'):.1f}% | K:{_k('BTTS'):.1f}%
"""
    return linhas


def montar_texto_resultado(jogos, stats_data):
    linhas = ""
    for j in jogos:
        f_id = str(j['fixture']['id'])
        d = stats_data.get(f_id)
        if not d or "erro" in d:
            continue
        lam, mu = calcular_lambdas(d['sh'], d['sa'], d['l_id'])
        probs, _, _, _ = motor_dc(lam, mu)
        if not probs:
            continue

        xg_flag = "" if d.get('tem_xg') else " [SEM xG REAL]"

        def _bp(k): return blended_prob(probs[k], d['odds'], k, d['l_id'])
        def _ev(k): return calcular_ev(_bp(k), d['odds'].get(k, 0))

        linhas += f"""
[ID: {f_id}] {j['teams']['home']['name']} vs {j['teams']['away']['name']}{xg_flag}
- λ_casa: {lam:.2f} | λ_fora: {mu:.2f}
- Forma Casa: {d['sh']['forma']} | xG Atk: {d['sh']['xg_atk']:.2f} | xG Def: {d['sh']['xg_def']:.2f}
- Forma Fora: {d['sa']['forma']} | xG Atk: {d['sa']['xg_atk']:.2f} | xG Def: {d['sa']['xg_def']:.2f}
- Vitória Casa → Odd:{d['odds'].get('HOME',0):.2f} | DC:{_bp('HOME'):.1f}% | EV:{_ev('HOME'):.1f}%
- Empate       → Odd:{d['odds'].get('DRAW',0):.2f} | DC:{_bp('DRAW'):.1f}% | EV:{_ev('DRAW'):.1f}%
- Vitória Fora → Odd:{d['odds'].get('AWAY',0):.2f} | DC:{_bp('AWAY'):.1f}% | EV:{_ev('AWAY'):.1f}%
- Dupla 1X     → Odd:{d['odds'].get('1X',0):.2f}   | DC:{_bp('1X'):.1f}%   | EV:{_ev('1X'):.1f}%
- Dupla X2     → Odd:{d['odds'].get('X2',0):.2f}   | DC:{_bp('X2'):.1f}%   | EV:{_ev('X2'):.1f}%
"""
    return linhas

# ==========================================
# 9. RENDERIZAÇÃO DE MERCADOS E CARDS
# ==========================================
def render_mercado(col, titulo, prob_blend, odd, ev, kelly_frac, banca_atual, score_ia=None):
    frac_k  = calcular_kelly(prob_blend, odd, score_ia, kelly_frac)
    stake   = frac_k * banca_atual
    justa   = 100 / prob_blend if prob_blend > 0 else 0

    tem_valor  = 3 < ev < 200
    valor_alto = ev >= 12

    cor = "#28a745" if valor_alto else "#ffc107" if tem_valor else "#aaaaaa"
    bg  = "#0d1f13" if valor_alto else "#1a1800" if tem_valor else "#111111"
    icone = "🔥" if valor_alto else "✅" if tem_valor else ""

    badge = (f'<div style="color:{cor};font-size:10px;font-weight:bold;margin-top:2px;">'
             f'{icone} VALOR (+{ev:.1f}%)</div>') if tem_valor else ""
    k_html = (f'<div style="color:#17a2b8;font-size:10px;margin-top:2px;">'
              f'🎯 R$ {stake:.2f} ({frac_k*100:.1f}%)</div>') if frac_k > 0 and tem_valor else ""

    html = f"""<div style="border:1px solid {cor if tem_valor else '#2a2a2a'};
                background:{bg};padding:8px 6px;border-radius:7px;
                text-align:center;margin-bottom:6px;">
        <div style="font-size:9px;color:#888;font-weight:bold;letter-spacing:.5px;">{titulo}</div>
        <div style="font-size:19px;font-weight:bold;color:{cor};">{prob_blend:.0f}%</div>
        <div style="font-size:10px;color:#666;">J:{justa:.2f} | O:{odd if odd > 0 else "–"}</div>
        {badge}{k_html}
    </div>"""

    with col:
        st.markdown(html, unsafe_allow_html=True)


def render_card(j, stats_data, ids_rank, kelly_frac, banca_atual, mercados, modo, data_str):
    f_id = str(j['fixture']['id'])
    d    = stats_data.get(f_id)
    hora = j['fixture']['date'][11:16]
    liga = j['league']['name']
    home = j['teams']['home']['name']
    away = j['teams']['away']['name']

    # Card sem análise
    if not d or "erro" in d:
        err_msg = (d.get("erro", "") if d else "") or "Não analisado"
        st.markdown(
            f"""<div style='border:1px solid #1e1e1e;border-radius:8px;padding:10px;
                background:#0a0a0a;margin-bottom:6px;border-left:3px solid #333;'>
                <div style='color:#444;font-size:11px;'>🕒 {hora} • {liga}</div>
                <div style='font-size:15px;font-weight:bold;color:#555;margin-top:4px;'>
                    {home} <span style='color:#333;'>vs</span> {away}
                </div>
                <div style='color:#333;font-size:10px;margin-top:4px;'>{err_msg}</div>
            </div>""",
            unsafe_allow_html=True
        )
        if st.button("📊 Analisar", key=f"mini_{modo}_{f_id}", use_container_width=True):
            analisar([j], data_str, force=True)
        return

    lam, mu = calcular_lambdas(d['sh'], d['sa'], d['l_id'])
    probs, top_placares, _, _ = motor_dc(lam, mu)
    if not probs:
        return

    perf_txt, perf_cor = perfil_jogo(probs)
    confianca = min(100, (d['sh'].get('n_jogos', 4) + d['sa'].get('n_jogos', 4)) / 16 * 100)

    # Badge de ranking IA
    pos = ids_rank.index(f_id) if f_id in ids_rank else None
    cores_rank = ["#FFD700", "#C0C0C0", "#CD7F32"]
    borda_cor  = "#333333"
    badge_rank = ""
    score_ia_val = None

    if pos is not None:
        borda_cor  = cores_rank[min(pos, 2)]
        badge_rank = (f'<span style="background:{borda_cor};color:#000;padding:2px 7px;'
                      f'border-radius:4px;font-size:10px;font-weight:bold;">TOP {pos+1}</span> ')
        score_ia_val = 88 if pos == 0 else 83 if pos <= 2 else 77

    sem_xg  = not d.get('tem_xg', True)
    xg_warn = ' <span style="color:#ff9800;font-size:10px;">⚠️ Sem xG</span>' if sem_xg else ''

    st.markdown(
        f"""<div style='border:2px solid {borda_cor};border-radius:10px;padding:12px;
            background:#0e1117;margin-bottom:4px;'>
            <div style='display:flex;justify-content:space-between;align-items:center;'>
                <span style='color:#777;font-size:11px;'>{badge_rank}🕒 {hora} • {liga}</span>
                <span style='color:{perf_cor};font-size:11px;font-weight:bold;'>{perf_txt}</span>
            </div>
            <div style='font-size:17px;font-weight:bold;color:#f0f0f0;margin:7px 0 3px;'>
                {home} <span style='color:#333;font-size:12px;'>vs</span> {away}{xg_warn}
            </div>
            <div style='font-size:10px;color:#444;'>
                λ {lam:.2f} vs {mu:.2f} · Σ {lam+mu:.2f} ·
                Forma: {d['sh']['forma']} — {d['sa']['forma']} ·
                Confiança: {confianca:.0f}%
            </div>
        </div>""",
        unsafe_allow_html=True
    )

    cols = st.columns(3)
    for i, mk in enumerate(mercados.keys()):
        pb  = blended_prob(probs[mk], d['odds'], mk, d['l_id'])
        odd = d['odds'].get(mk, 0)
        ev  = calcular_ev(pb, odd)
        render_mercado(cols[i % 3], mercados[mk], pb, odd, ev, kelly_frac, banca_atual, score_ia_val)

    with st.expander(f"📊 Detalhes & Salvar — {home} vs {away}"):
        # Top 6 placares
        st.markdown("**🎯 Placares mais prováveis (Dixon-Coles):**")
        pc = st.columns(6)
        for i, ((x, y), p) in enumerate(top_placares):
            pc[i].metric(f"{x}–{y}", f"{p*100:.1f}%")

        st.write("---")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**🏠 {home}** (casa)")
            st.write(f"Forma: {d['sh']['forma']}")
            st.write(f"xG Atk: {d['sh']['xg_atk']:.2f} | xG Def: {d['sh']['xg_def']:.2f}")
            st.write(f"Média Gols: {d['sh']['media_gols_f']:.2f} F / {d['sh']['media_gols_s']:.2f} S")
        with c2:
            st.markdown(f"**✈️ {away}** (fora)")
            st.write(f"Forma: {d['sa']['forma']}")
            st.write(f"xG Atk: {d['sa']['xg_atk']:.2f} | xG Def: {d['sa']['xg_def']:.2f}")
            st.write(f"Média Gols: {d['sa']['media_gols_f']:.2f} F / {d['sa']['media_gols_s']:.2f} S")

        st.write("---")

        cs, cst, cb, cu = st.columns([2, 1, 1, 1])
        mk_sel  = cs.selectbox("Mercado:", list(mercados.keys()),
                               format_func=lambda x: mercados[x],
                               key=f"sel_{modo}_{f_id}", label_visibility="collapsed")
        pb_sel  = blended_prob(probs[mk_sel], d['odds'], mk_sel, d['l_id'])
        odd_sel = d['odds'].get(mk_sel, 0)
        ev_sel  = calcular_ev(pb_sel, odd_sel)
        stk_sug = calcular_kelly(pb_sel, odd_sel, score_ia_val, kelly_frac) * banca_atual

        stk_inp = cst.number_input(
            "R$", value=float(max(1.0, round(stk_sug, 2))),
            step=0.5, key=f"stk_{modo}_{f_id}", label_visibility="collapsed"
        )

        # SALVAR SEM RERUN — sem piscar
        if cb.button("✅ Salvar", key=f"save_{modo}_{f_id}"):
            banco["picks"].append({
                "data":    data_str,
                "jogo":    f"{home} v {away}",
                "liga":    liga,
                "mercado": mercados[mk_sel],
                "odd":     odd_sel,
                "prob":    round(pb_sel, 1),
                "ev":      round(ev_sel, 1),
                "status":  "Pendente",
                "stake":   stk_inp,
            })
            salvar_banco(banco)
            st.success(f"✅ Pick salvo: {mercados[mk_sel]} @ {odd_sel}")

        if cu.button("🔄", key=f"upd_{modo}_{f_id}", help="Atualizar dados"):
            analisar([j], data_str, force=True)

# ==========================================
# 10. DIÁRIO DE BORDO — DIALOG (popup)
# ==========================================
@st.dialog("📋 Diário de Bordo", width="large")
def dialog_tracker():
    picks = banco["picks"]
    stats = calcular_stats_tracker(picks)

    # Métricas no topo
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Apostas", f"{stats['total']}")
    m2.metric("Hit Rate", f"{stats['hit_rate']:.1f}%", f"{stats['greens']}V/{stats['reds']}D")
    m3.metric("Yield",   f"{stats['yield_pct']:.1f}%")
    m4.metric("P/L",     f"R$ {stats['lucro']:.2f}")
    m5.metric("Pendentes", f"{stats['pendentes']}")

    st.write("---")

    if not picks:
        st.info("Nenhum pick registrado ainda.")
        return

    filtro = st.selectbox("Filtrar por status:", ["Todos", "Pendente", "Green", "Red", "Devolvida"], index=0)

    for i, p in enumerate(reversed(picks)):
        real_idx = len(picks) - 1 - i
        st_atual = p.get("status", "Pendente")

        if filtro != "Todos" and st_atual != filtro:
            continue

        icon = {"Pendente": "⏳", "Green": "✅", "Red": "❌", "Devolvida": "➖"}.get(st_atual, "⏳")
        cor_exp = {"Green": "#0d1f13", "Red": "#1f0d0d"}.get(st_atual, "#0f0f1a")

        with st.expander(f"{icon} {p.get('data','')} · {p.get('jogo','')} · {p.get('mercado','')}"):
            st.markdown(
                f"**Stake:** R$ {p.get('stake',1):.2f} · "
                f"**Odd:** {p.get('odd','–')} · "
                f"**Prob:** {p.get('prob','–')}% · "
                f"**EV:** {p.get('ev','–')}% · "
                f"**Liga:** {p.get('liga','–')}"
            )
            if st_atual == "Pendente":
                ca, cb, cc = st.columns(3)
                if ca.button("✅ Green", key=f"g_{real_idx}", type="primary"):
                    banco["picks"][real_idx]["status"] = "Green"
                    salvar_banco(banco)
                    st.rerun()
                if cb.button("❌ Red", key=f"r_{real_idx}"):
                    banco["picks"][real_idx]["status"] = "Red"
                    salvar_banco(banco)
                    st.rerun()
                if cc.button("➖ Anular", key=f"v_{real_idx}"):
                    banco["picks"][real_idx]["status"] = "Devolvida"
                    salvar_banco(banco)
                    st.rerun()
            else:
                if st.button("↩️ Desfazer", key=f"undo_{real_idx}"):
                    banco["picks"][real_idx]["status"] = "Pendente"
                    salvar_banco(banco)
                    st.rerun()

# ==========================================
# 11. INTERFACE PRINCIPAL
# ==========================================

# ---------- SIDEBAR ----------
with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO")
    st.caption("V7.1 · Dixon-Coles · Casa/Fora")

    saldo_api = buscar_creditos()
    pct_api   = min(saldo_api / 7500, 1.0)
    st.metric("Créditos API", f"{saldo_api} / 7500")
    st.progress(pct_api)

    st.write("---")
    st.markdown("### 💰 Banca")

    lucro_total = 0.0
    for p in banco["picks"]:
        s, stk, odd = p.get("status"), p.get("stake", 1.0), p.get("odd", 1.0)
        if s == "Green": lucro_total += stk * (odd - 1.0)
        elif s == "Red": lucro_total -= stk

    banca_input = st.number_input(
        "Banca Total (R$)",
        value=float(banco.get("banca_inicial", 29.0)),
        step=5.0
    )
    if banca_input != banco.get("banca_inicial"):
        banco["banca_inicial"] = banca_input
        salvar_banco(banco)

    banca_atual = banco["banca_inicial"] + lucro_total
    st.metric("Saldo Atual", f"R$ {banca_atual:.2f}",
              f"P/L: R$ {lucro_total:+.2f}",
              delta_color="normal" if lucro_total >= 0 else "inverse")

    st.write("---")
    st.markdown("### ⚙️ Kelly")
    kelly_opcao = st.select_slider(
        "Fração do Kelly",
        options=["1/10 (Defensivo)", "1/6 (Conservador)", "1/4 (Padrão)", "1/3 (Moderado)"],
        value="1/4 (Padrão)"
    )
    kelly_map = {
        "1/10 (Defensivo)":  0.10,
        "1/6 (Conservador)": 0.167,
        "1/4 (Padrão)":      0.25,
        "1/3 (Moderado)":    0.333,
    }
    kelly_frac = kelly_map[kelly_opcao]
    st.caption("Score IA ≥ 85 → 25% | 75-84 → 15% | Sem score → fração acima")

    st.write("---")
    if st.button("📋 Diário de Bordo", use_container_width=True):
        dialog_tracker()

    st.write("---")
    data_consulta = st.date_input("📅 Data do Scanner", datetime.date.today())
    data_str = data_consulta.strftime("%Y-%m-%d")

    tipo_filtro = st.radio(
        "Filtro de Ligas:",
        ["🏆 Só PRO", "🌍 PRO + Confiáveis", "🗑️ Mundo Todo"],
        index=1
    )

    st.write("---")
    if st.button("🗑️ Limpar Cache do Dia", use_container_width=True):
        if data_str in banco["datas"]:
            del banco["datas"][data_str]
        if data_str in banco.get("ia_cache", {}):
            del banco["ia_cache"][data_str]
        # Limpar session_state da IA
        for k in ["ia_gols", "ids_gols", "ia_res", "ids_res"]:
            st.session_state.pop(k, None)
        salvar_banco(banco)
        st.rerun()

# ---------- CONTEÚDO PRINCIPAL ----------
if data_str not in banco["datas"]:
    banco["datas"][data_str] = {"agenda": [], "stats": {}}

agenda    = banco["datas"][data_str].get("agenda", [])
stats_data = banco["datas"][data_str].get("stats", {})

# Carregar IA do cache persistido (não some mais ao recarregar)
ia_cache = banco.get("ia_cache", {})
ia_dia   = ia_cache.get(data_str, {})

if "ia_gols" not in st.session_state and "ia_gols" in ia_dia:
    st.session_state["ia_gols"] = ia_dia["ia_gols"]
    st.session_state["ids_gols"] = ia_dia.get("ids_gols", [])
if "ia_res" not in st.session_state and "ia_res" in ia_dia:
    st.session_state["ia_res"] = ia_dia["ia_res"]
    st.session_state["ids_res"] = ia_dia.get("ids_res", [])

# Botão carregar agenda
if st.button("🔄  1. Carregar Agenda do Dia", use_container_width=True, type="primary"):
    with st.spinner("Buscando agenda..."):
        res = requests.get(
            f"{BASE_URL}/fixtures?date={data_str}&timezone=America/Sao_Paulo",
            headers=HEADERS
        ).json()
        if res.get('response'):
            banco["datas"][data_str]["agenda"] = res['response']
            salvar_banco(banco, nuvem=False)
            st.rerun()
        else:
            st.error(f"Nenhum jogo retornado: {res.get('errors', 'Grade vazia')}")

if not agenda:
    st.markdown(
        '<div style="text-align:center;padding:80px 20px;">'
        '<div style="font-size:52px;">👑</div>'
        '<div style="font-size:22px;font-weight:bold;color:#333;margin:12px 0;">QG Barrios PRO V7.1</div>'
        '<div style="font-size:12px;color:#222;">Dixon-Coles · Casa/Fora · Kelly por Score IA · IA Persistida</div>'
        '<div style="font-size:11px;margin-top:16px;color:#1a1a1a;">← Selecione uma data e clique em Carregar Agenda</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

# Filtrar jogos visíveis
palavras_proibidas = [
    'u19','u20','u21','u23','youth','women','feminino',
    'reserve','amateur','regional','state'
]
paises_confiaveis = [
    'Brazil','Argentina','USA','Mexico','Netherlands','Portugal','Turkey',
    'Saudi-Arabia','Switzerland','Japan','Colombia','Chile','South-Korea',
    'Scotland','Greece','Belgium','Uruguay','Ecuador','Paraguay','Bolivia',
    'Peru','Venezuela'
]

jogos_visiveis = []
for j in agenda:
    l_id      = j['league']['id']
    l_name    = str(j['league']['name']).lower()
    l_country = str(j['league']['country'])

    if tipo_filtro == "🏆 Só PRO":
        ok = l_id in LIGAS_PRO
    elif tipo_filtro == "🌍 PRO + Confiáveis":
        ok = l_id in LIGAS_PRO or (
            l_country in paises_confiaveis and
            not any(p in l_name for p in palavras_proibidas)
        )
    else:
        ok = not any(p in l_name for p in palavras_proibidas)

    if ok and j not in jogos_visiveis:
        jogos_visiveis.append(j)

# Contadores
analisados = len([
    j for j in jogos_visiveis
    if str(j['fixture']['id']) in stats_data and "erro" not in stats_data.get(str(j['fixture']['id']), {})
])
pendentes_n = len(jogos_visiveis) - analisados

st.markdown(
    f"**{len(jogos_visiveis)} jogos** · ✅ {analisados} analisados · "
    f"⏳ {pendentes_n} pendentes · ~{pendentes_n * CUSTO_POR_JOGO} créditos"
)

# Botões de análise
ba1, ba2 = st.columns(2)
with ba1:
    if st.button(f"🚀  2. Analisar TODOS ({len(jogos_visiveis)})", type="primary", use_container_width=True):
        analisar(jogos_visiveis, data_str)
with ba2:
    if st.button(f"⚡  Só Pendentes ({pendentes_n})", use_container_width=True):
        pendentes_lista = [
            j for j in jogos_visiveis
            if str(j['fixture']['id']) not in stats_data
            or "erro" in stats_data.get(str(j['fixture']['id']), {})
        ]
        analisar(pendentes_lista, data_str)

st.write("---")

# ==========================================
# 12. INTELIGÊNCIA ARTIFICIAL
# ==========================================
st.markdown("### 🤖 Auditoria IA")

MK_GOLS = {
    "OVER_15": "Over 1.5",  "OVER_25": "Over 2.5", "OVER_35": "Over 3.5",
    "UNDER_25": "Under 2.5", "UNDER_35": "Under 3.5", "BTTS": "BTTS"
}
MK_RES = {
    "HOME": "Vitória Casa", "DRAW": "Empate", "AWAY": "Vitória Fora",
    "1X": "Dupla 1X", "X2": "Dupla X2"
}

ci1, ci2 = st.columns(2)

with ci1:
    if st.button("🧠  IA — GOLS", use_container_width=True):
        textos = montar_texto_gols(jogos_visiveis, stats_data)
        if textos.strip():
            with st.spinner("IA analisando gols..."):
                resp = chamar_ia(textos, "GOLS")
                ids  = re.findall(r'\[ID:\s*(\d+)\]', resp)
                st.session_state["ia_gols"] = resp
                st.session_state["ids_gols"] = ids
                if "ia_cache" not in banco: banco["ia_cache"] = {}
                if data_str not in banco["ia_cache"]: banco["ia_cache"][data_str] = {}
                banco["ia_cache"][data_str]["ia_gols"]  = resp
                banco["ia_cache"][data_str]["ids_gols"] = ids
                salvar_banco(banco, nuvem=False)
        else:
            st.warning("Analise os jogos antes de chamar a IA.")

with ci2:
    if st.button("⚔️  IA — RESULTADO", use_container_width=True):
        textos = montar_texto_resultado(jogos_visiveis, stats_data)
        if textos.strip():
            with st.spinner("IA analisando resultados..."):
                resp = chamar_ia(textos, "RESULTADO")
                ids  = re.findall(r'\[ID:\s*(\d+)\]', resp)
                st.session_state["ia_res"] = resp
                st.session_state["ids_res"] = ids
                if "ia_cache" not in banco: banco["ia_cache"] = {}
                if data_str not in banco["ia_cache"]: banco["ia_cache"][data_str] = {}
                banco["ia_cache"][data_str]["ia_res"]  = resp
                banco["ia_cache"][data_str]["ids_res"] = ids
                salvar_banco(banco, nuvem=False)
        else:
            st.warning("Analise os jogos antes de chamar a IA.")

ids_gols = st.session_state.get("ids_gols", [])
ids_res  = st.session_state.get("ids_res",  [])


def _salvar_pick_rapido(f_id, jogo_info, mercados_dict, stats_data, probs_cache, modo_key):
    """Bloco de save rápido dentro do relatório IA."""
    d = stats_data.get(f_id)
    if not d or "erro" in d or not jogo_info:
        return
    home = jogo_info['teams']['home']['name']
    away = jogo_info['teams']['away']['name']
    lam, mu = calcular_lambdas(d['sh'], d['sa'], d['l_id'])
    probs, _, _, _ = motor_dc(lam, mu)
    if not probs:
        return

    sc1, sc2, sc3, sc4 = st.columns([3, 2, 1, 1])
    sc1.write(f"**{home} v {away}**")
    mk_q  = sc2.selectbox("", list(mercados_dict.keys()),
                          format_func=lambda x: mercados_dict[x],
                          key=f"iamk_{modo_key}_{f_id}", label_visibility="collapsed")
    pb_q  = blended_prob(probs[mk_q], d['odds'], mk_q, d['l_id'])
    odd_q = d['odds'].get(mk_q, 0)
    ev_q  = calcular_ev(pb_q, odd_q)
    stk_q = calcular_kelly(pb_q, odd_q, 82, kelly_frac) * banca_atual
    stk_i = sc3.number_input("R$", value=float(max(1.0, round(stk_q, 2))),
                              step=0.5, key=f"iastk_{modo_key}_{f_id}", label_visibility="collapsed")
    if sc4.button("✅", key=f"iasave_{modo_key}_{f_id}"):
        banco["picks"].append({
            "data":    data_str,
            "jogo":    f"{home} v {away}",
            "liga":    jogo_info['league']['name'],
            "mercado": mercados_dict[mk_q],
            "odd":     odd_q,
            "prob":    round(pb_q, 1),
            "ev":      round(ev_q, 1),
            "status":  "Pendente",
            "stake":   stk_i,
        })
        salvar_banco(banco)
        st.success("✅ Salvo!")


# Exibir relatório GOLS (persistido)
if "ia_gols" in st.session_state:
    with st.expander("🔥 Relatório IA — GOLS", expanded=True):
        st.markdown(st.session_state["ia_gols"])
        if ids_gols:
            st.write("---")
            st.markdown("**💾 Salvar pick direto do relatório:**")
            for f_id in ids_gols[:6]:
                jogo_info = next((j for j in jogos_visiveis if str(j['fixture']['id']) == f_id), None)
                _salvar_pick_rapido(f_id, jogo_info, MK_GOLS, stats_data, {}, "g")

# Exibir relatório RESULTADO (persistido)
if "ia_res" in st.session_state:
    with st.expander("⚔️ Relatório IA — RESULTADO", expanded=False):
        st.markdown(st.session_state["ia_res"])
        if ids_res:
            st.write("---")
            st.markdown("**💾 Salvar pick direto do relatório:**")
            for f_id in ids_res[:6]:
                jogo_info = next((j for j in jogos_visiveis if str(j['fixture']['id']) == f_id), None)
                _salvar_pick_rapido(f_id, jogo_info, MK_RES, stats_data, {}, "r")

st.write("---")

# Toggle "Só Aprovados"
col_tog, _ = st.columns([1, 3])
so_aprovados = col_tog.toggle("🎯 Só Aprovados pela IA", value=False)

# ==========================================
# 13. CARDS — ABAS GOLS / RESULTADO
# ==========================================
def sort_jogos(jogos, ids_rank):
    return sorted(
        jogos,
        key=lambda j: ids_rank.index(str(j['fixture']['id']))
        if str(j['fixture']['id']) in ids_rank else 999999
    )

jgs_gols = sort_jogos(jogos_visiveis, ids_gols)
jgs_res  = sort_jogos(jogos_visiveis, ids_res)

if so_aprovados:
    jgs_gols = [j for j in jgs_gols if str(j['fixture']['id']) in ids_gols]
    jgs_res  = [j for j in jgs_res  if str(j['fixture']['id']) in ids_res]

tab_g, tab_r = st.tabs(["🔥  MODO GOLS", "⚔️  MODO RESULTADO"])

with tab_g:
    if not jgs_gols:
        st.info("Nenhum jogo visível nesta aba.")
    for j in jgs_gols:
        render_card(j, stats_data, ids_gols, kelly_frac, banca_atual, MK_GOLS, "g", data_str)

with tab_r:
    if not jgs_res:
        st.info("Nenhum jogo visível nesta aba.")
    for j in jgs_res:
        render_card(j, stats_data, ids_res, kelly_frac, banca_atual, MK_RES, "r", data_str)
