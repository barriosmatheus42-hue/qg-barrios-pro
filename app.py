import streamlit as st
import requests
import datetime
import time
import math
import json
import os

st.set_page_config(page_title="QG Barrios PRO", layout="wide")

# ==========================================
# 0. CONFIGURAÇÕES E CHAVE PRO
# ==========================================
API_KEY_PRO = "00374ab0590422053c950ddc399a0ccb"
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {'x-apisports-key': API_KEY_PRO}
ARQUIVO_BANCO = "banco_barrios_pro.json"

def carregar_banco():
    if os.path.exists(ARQUIVO_BANCO):
        try:
            with open(ARQUIVO_BANCO, "r") as f: return json.load(f)
        except: pass
    return {"datas": {}, "creditos_restantes": 7500}

def salvar_banco(dados):
    with open(ARQUIVO_BANCO, "w") as f: json.dump(dados, f)

banco_local = carregar_banco()

def atualizar_saldo_realtime():
    try:
        res = requests.get(f"{BASE_URL}/status", headers=HEADERS, timeout=5).json()
        if res.get('response'):
            rem = res['response']['requests']['limit_day'] - res['response']['requests']['current']
            banco_local["creditos_restantes"] = rem
            return rem
    except: return banco_local.get("creditos_restantes", 0)

# ==========================================
# 2. MOTOR MATEMÁTICO (POISSON)
# ==========================================
def calcular_poisson(media_casa, media_fora):
    if media_casa <= 0.05 and media_fora <= 0.05: return None
    prob_ambas, prob_over_25 = 0, 0
    prob_home, prob_draw, prob_away = 0, 0, 0
    placares = []
    m_h, m_a = max(media_casa, 0.1), max(media_fora, 0.1)
    for gc in range(6):
        for gf in range(6):
            p_casa = (math.exp(-m_h) * (m_h**gc)) / math.factorial(gc)
            p_fora = (math.exp(-m_a) * (m_a**gf)) / math.factorial(gf)
            p_placar = p_casa * p_fora
            placares.append((gc, gf, p_placar * 100))
            if gc > 0 and gf > 0: prob_ambas += p_placar
            if (gc + gf) > 2.5: prob_over_25 += p_placar
            if gc > gf: prob_home += p_placar
            elif gc == gf: prob_draw += p_placar
            else: prob_away += p_placar
    prob_under_25 = 1 - prob_over_25
    placares.sort(key=lambda x: x[2], reverse=True)
    return {
        "BTTS": {"prob": prob_ambas * 100, "justa": 100/(prob_ambas * 100) if prob_ambas > 0.01 else 99},
        "OVER": {"prob": prob_over_25 * 100, "justa": 100/(prob_over_25 * 100) if prob_over_25 > 0.01 else 99},
        "UNDER": {"prob": prob_under_25 * 100, "justa": 100/(prob_under_25 * 100) if prob_under_25 > 0.01 else 99},
        "HOME": {"prob": prob_home * 100, "justa": 100/(prob_home * 100) if prob_home > 0.01 else 99},
        "DRAW": {"prob": prob_draw * 100, "justa": 100/(prob_draw * 100) if prob_draw > 0.01 else 99},
        "AWAY": {"prob": prob_away * 100, "justa": 100/(prob_away * 100) if prob_away > 0.01 else 99},
        "1X": {"prob": (prob_home + prob_draw) * 100, "justa": 100/((prob_home + prob_draw) * 100) if (prob_home + prob_draw) > 0.01 else 99},
        "X2": {"prob": (prob_away + prob_draw) * 100, "justa": 100/((prob_away + prob_draw) * 100) if (prob_away + prob_draw) > 0.01 else 99},
        "TOP": placares[:5]
    }

# ==========================================
# 3. BUSCAS DE API (HISTÓRICO, ODDS, STANDINGS E H2H)
# ==========================================
def buscar_historico_global(team_id, last_n=30):
    url = f"{BASE_URL}/fixtures"
    params = {'team': team_id, 'last': last_n, 'status': 'FT'}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response'):
            jogos = res['response']
            gols_feitos, gols_sofridos = 0, 0
            competicoes = {}
            forma = []
            
            for j in jogos:
                l_name = j['league']['name']
                competicoes[l_name] = competicoes.get(l_name, 0) + 1
                if j['teams']['home']['id'] == team_id:
                    gf, gs = j['goals']['home'], j['goals']['away']
                else:
                    gf, gs = j['goals']['away'], j['goals']['home']
                gols_feitos += gf
                gols_sofridos += gs
                
            for j in jogos[:5]:
                if j['teams']['home']['id'] == team_id: gf, gs = j['goals']['home'], j['goals']['away']
                else: gf, gs = j['goals']['away'], j['goals']['home']
                if gf > gs: forma.append("🟩")
                elif gf == gs: forma.append("⬜")
                else: forma.append("🟥")

            total = len(jogos)
            if total == 0: return None
            return {
                "media_feita": gols_feitos / total, 
                "media_sofrida": gols_sofridos / total, 
                "total_jogos": total, 
                "detalhe_ligas": competicoes,
                "forma": "".join(forma[::-1])
            }
    except: return None
    return None

def buscar_odds_vips(fixture_id):
    url = f"{BASE_URL}/odds"
    params = {'fixture': fixture_id, 'bookmaker': 8}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response') and len(res['response']) > 0:
            bookies = res['response'][0].get('bookmakers', [])
            odds = {"BTTS": 0, "OVER": 0, "UNDER": 0, "HOME": 0, "DRAW": 0, "AWAY": 0, "1X": 0, "X2": 0}
            for bkm in bookies:
                if bkm['id'] == 8:
                    for bet in bkm['bets']:
                        if bet['name'] == 'Both Teams Score': odds['BTTS'] = float(bet['values'][0]['odd'])
                        elif bet['name'] == 'Goals Over/Under':
                            for v in bet['values']:
                                if v['value'] == 'Over 2.5': odds['OVER'] = float(v['odd'])
                                if v['value'] == 'Under 2.5': odds['UNDER'] = float(v['odd'])
                        elif bet['name'] == 'Match Winner':
                            for v in bet['values']:
                                if v['value'] == 'Home': odds['HOME'] = float(v['odd'])
                                elif v['value'] == 'Draw': odds['DRAW'] = float(v['odd'])
                                elif v['value'] == 'Away': odds['AWAY'] = float(v['odd'])
                        elif bet['name'] == 'Double Chance':
                            for v in bet['values']:
                                if v['value'] == 'Home/Draw': odds['1X'] = float(v['odd'])
                                elif v['value'] == 'Draw/Away': odds['X2'] = float(v['odd'])
            return odds
    except: return None
    return None

def buscar_standings(league_id, season, h_id, a_id):
    url = f"{BASE_URL}/standings"
    params = {'league': league_id, 'season': season}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        dados_h, dados_a = None, None
        if res.get('response') and len(res['response']) > 0:
            para_grupos = res['response'][0]['league']['standings']
            for grupo in para_grupos:
                # SEGREDO PARA A ARGENTINA/MÉXICO: Pular os grupos de "Promedio" ou Agregados
                if grupo and "promedio" in str(grupo[0].get('group', '')).lower(): continue
                if grupo and "aggregate" in str(grupo[0].get('group', '')).lower(): continue
                
                for time in grupo:
                    if time['team']['id'] == h_id and dados_h is None: dados_h = time
                    elif time['team']['id'] == a_id and dados_a is None: dados_a = time
        return {"h": dados_h, "a": dados_a}
    except: return None
    return None

def buscar_h2h(h_id, a_id):
    url = f"{BASE_URL}/fixtures/headtohead"
    params = {'h2h': f"{h_id}-{a_id}", 'last': 3}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response'): return res['response']
    except: return None
    return []

# ==========================================
# 4. MOTOR DE ANÁLISE COMPLETO
# ==========================================
def acao_analisar(jogos_alvo, data_str, force=False):
    if "stats" not in banco_local["datas"][data_str]: banco_local["datas"][data_str]["stats"] = {}
    progresso_bar = st.progress(0)
    for idx, jogo in enumerate(jogos_alvo):
        f_id = str(jogo['fixture']['id'])
        if force and f_id in banco_local["datas"][data_str]["stats"]: del banco_local["datas"][data_str]["stats"][f_id]
        if f_id not in banco_local["datas"][data_str]["stats"]:
            h_id, a_id = jogo['teams']['home']['id'], jogo['teams']['away']['id']
            l_id, season = jogo['league']['id'], jogo['league']['season']
            
            odds = buscar_odds_vips(f_id)
            s_h, s_a = buscar_historico_global(h_id), buscar_historico_global(a_id)
            standings = buscar_standings(l_id, season, h_id, a_id)
            h2h = buscar_h2h(h_id, a_id)
            
            if s_h and s_a: 
                banco_local["datas"][data_str]["stats"][f_id] = {
                    "odds": odds if odds else {"BTTS":0, "OVER":0, "UNDER":0, "HOME":0, "DRAW":0, "AWAY":0, "1X":0, "X2":0}, 
                    "h": s_h, "a": s_a,
                    "standings": standings,
                    "h2h": h2h
                }
            else: 
                banco_local["datas"][data_str]["stats"][f_id] = {"erro": "Sem histórico suficiente"}
            time.sleep(0.2)
        progresso_bar.progress((idx + 1) / len(jogos_alvo))
    progresso_bar.empty()
    salvar_banco(banco_local)
    st.rerun()

def get_ev(dados, p_dict, key):
    casa = dados['odds'].get(key, 0)
    justa = p_dict[key]['justa']
    return ((casa / justa) - 1) * 100 if (casa > justa and justa > 0) else -100

def calcular_ranking_dinamico(f_id, data_str, mercado_alvo):
    dados = banco_local["datas"][data_str]["stats"].get(f_id)
    if not dados or "erro" in dados or "h" not in dados or "a" not in dados: return -999
    m_h = (dados['h']['media_feita'] + dados['a']['media_sofrida']) / 2
    m_a = (dados['a']['media_feita'] + dados['h']['media_sofrida']) / 2
    p = calcular_poisson(m_h, m_a)
    if not p: return -999
    if mercado_alvo == "Resultado (1X2)": return max(get_ev(dados, p, "HOME"), get_ev(dados, p, "DRAW"), get_ev(dados, p, "AWAY"))
    elif mercado_alvo == "Dupla Chance (1X, X2)": return max(get_ev(dados, p, "1X"), get_ev(dados, p, "X2"))
    elif mercado_alvo == "Ambas Marcam (BTTS)": return get_ev(dados, p, "BTTS")
    elif mercado_alvo == "Over 2.5": return get_ev(dados, p, "OVER")
    elif mercado_alvo == "Under 2.5": return get_ev(dados, p, "UNDER")
    else: return max([get_ev(dados, p, k) for k in ["HOME", "DRAW", "AWAY", "1X", "X2", "BTTS", "OVER", "UNDER"]])

def renderizar_mercado(col, titulo, p_dict, key, odds_dict):
    prob, justa, casa = p_dict[key]['prob'], p_dict[key]['justa'], odds_dict.get(key, 0)
    ev = ((casa / justa) - 1) * 100 if (casa > justa and justa > 0) else 0
    icone_fogo = "🔥" if ev > 20 else ""
    badge_html = f'<div style="color:#28a745; font-size:11px; font-weight:bold; margin-top:3px;">VALOR {icone_fogo} (+{ev:.1f}%)</div>' if ev > 3 else ''
    estilo = "border:1px solid #28a745; background-color:#1a2b1f;" if ev > 3 else "border:1px solid #333; background-color:#111;"
    html = f'<div style="{estilo} padding:8px; border-radius:6px; text-align:center; margin-bottom:8px;"><div style="font-size:10px; color:#aaa; margin-bottom:2px; font-weight:bold;">{titulo}</div><div style="font-size:16px; font-weight:bold; color:{'#28a745' if ev > 3 else '#fff'};">{prob:.0f}%</div><div style="font-size:11px; color:#FFFFFF; margin-top:4px;">J: {justa:.2f} | O: {casa if casa > 0 else '-'}</div>{badge_html}</div>'
    with col: st.markdown(html, unsafe_allow_html=True)

# ==========================================
# 5. INTERFACE
# ==========================================
with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO")
    saldo = atualizar_saldo_realtime()
    st.metric("Créditos Disponíveis", f"{saldo}/7500")
    st.progress(saldo / 7500)
    st.write("---")
    data_consulta = st.date_input("Data do Scanner", datetime.date.today())
    data_str = data_consulta.strftime("%Y-%m-%d")
    LIGAS_PRO = [39, 140, 135, 78, 61, 71, 72, 73, 2, 3, 848, 13, 11, 40, 88, 307, 253, 94, 128, 203]
    filtro_pro = st.checkbox("⭐ Apenas Ligas PRO", value=False)
    st.write("---")
    st.markdown("### 🎯 Filtrar por Mercado")
    mercado_filtro = st.selectbox("Destacar Valor em:", ["Geral (Todos)", "Resultado (1X2)", "Dupla Chance (1X, X2)", "Ambas Marcam (BTTS)", "Over 2.5", "Under 2.5"])
    st.write("---")
    if st.button("🗑️ Limpar Cache do Dia"):
        if data_str in banco_local["datas"]:
            del banco_local["datas"][data_str]
            salvar_banco(banco_local)
            st.rerun()

if data_str not in banco_local["datas"]: banco_local["datas"][data_str] = {"agenda": [], "stats": {}}
agenda = banco_local["datas"][data_str]["agenda"]

if st.button("🔄 1. Carregar Agenda do Dia", use_container_width=True):
    res = requests.get(f"{BASE_URL}/fixtures?date={data_str}&timezone=America/Sao_Paulo", headers=HEADERS).json()
    if res.get('response'):
        banco_local["datas"][data_str]["agenda"] = res['response']
        salvar_banco(banco_local)
        st.rerun()

if agenda:
    jogos_visiveis = [j for j in agenda if not filtro_pro or j['league']['id'] in LIGAS_PRO]
    jogos_visiveis.sort(key=lambda x: calcular_ranking_dinamico(str(x['fixture']['id']), data_str, mercado_filtro), reverse=True)
    if st.button(f"🚀 2. Analisar Visíveis ({len(jogos_visiveis)})", type="primary", use_container_width=True):
        acao_analisar(jogos_visiveis, data_str)

    for j in jogos_visiveis:
        f_id = str(j['fixture']['id'])
        d = banco_local["datas"][data_str]["stats"].get(f_id)
        st.markdown(f'<div style="border:1px solid #333; border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:10px;"><div style="display:flex; justify-content:space-between; color:#888; font-size:11px;"><span>🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span><span style="color:#28a745;">{'● Em Cache' if d else ''}</span></div><div style="font-size:18px; font-weight:bold; color:white; margin: 8px 0;">{j['teams']['home']['name']} <span style="color:#555; font-size:12px;">vs</span> {j['teams']['away']['name']}</div>', unsafe_allow_html=True)
        
        if d:
            if "erro" in d or "h" not in d or "a" not in d: st.warning(f"⚠️ {d.get('erro', 'Dados corrompidos.')}")
            else:
                m_h, m_a = (d['h']['media_feita'] + d['a']['media_sofrida']) / 2, (d['a']['media_feita'] + d['h']['media_sofrida']) / 2
                p = calcular_poisson(m_h, m_a)
                if p:
                    conf = d['h']['total_jogos'] + d['a']['total_jogos']
                    st.markdown(f"<div style='color:#28a745; font-size:11px; margin-bottom:10px;'>🛡️ Confiança: {'🟢🟢🟢' if conf > 40 else '🟡🟡⚪'} ({conf} jogos globais)</div>", unsafe_allow_html=True)
                    c1, c2, c3 = st.columns(3)
                    renderizar_mercado(c1, "Mandante (1)", p, "HOME", d['odds']); renderizar_mercado(c2, "Empate (X)", p, "DRAW", d['odds']); renderizar_mercado(c3, "Visitante (2)", p, "AWAY", d['odds'])
                    c4, c5, c6 = st.columns(3)
                    renderizar_mercado(c4, "Dupla Casa (1X)", p, "1X", d['odds']); renderizar_mercado(c5, "Dupla Fora (X2)", p, "X2", d['odds']); renderizar_mercado(c6, "BTTS (Ambas)", p, "BTTS", d['odds'])
                    c7, c8, c9 = st.columns(3)
                    renderizar_mercado(c7, "Over 2.5", p, "OVER", d['odds']); renderizar_mercado(c8, "Under 2.5", p, "UNDER", d['odds'])
                    with c9:
                        st.markdown(f'<div style="font-size:10px; color:#aaa; margin-bottom:2px; font-weight:bold; text-align:center;">Placares TOP 3</div><div style="display:flex; flex-direction:column; gap:2px; align-items:center;">{' '.join([f'<div style="background:#222; padding:1px 6px; border-radius:3px; font-size:10px; color:#ccc; border:1px solid #444;">{plac[0]}x{plac[1]} ({plac[2]:.0f}%)</div>' for plac in p['TOP'][:3]])}</div>', unsafe_allow_html=True)
                    
                    with st.expander("📊 Raio-X do Jogo (Inteligência Humana)"):
                        colA, colB = st.columns(2)
                        forma_h = d['h'].get('forma', 'Sem dados')
                        forma_a = d['a'].get('forma', 'Sem dados')
                        
                        with colA:
                            st.markdown(f"**🏠 {j['teams']['home']['name']}**")
                            st.write(f"Últimos 5 jogos: {forma_h}")
                            if d.get("standings") and d["standings"].get("h"):
                                std = d["standings"]["h"]
                                rank, pts, sg = std.get('rank', '-'), std.get('points', 0), std.get('goalsDiff', 0)
                                v, e, derr = std['all'].get('win') or 0, std['all'].get('draw') or 0, std['all'].get('lose') or 0
                                st.caption(f"🏆 Posição: {rank}º | Pontos: {pts} | SG: {sg}")
                                st.caption(f"Campanha: {v}V - {e}E - {derr}D")
                            else:
                                st.caption("Tabela não aplicável (Copa/Mata-Mata)")
                            st.write(f"Média Pró: **{d['h']['media_feita']:.2f}** | Sofrida: **{d['h']['media_sofrida']:.2f}**")
                            
                        with colB:
                            st.markdown(f"**🚀 {j['teams']['away']['name']}**")
                            st.write(f"Últimos 5 jogos: {forma_a}")
                            if d.get("standings") and d["standings"].get("a"):
                                std = d["standings"]["a"]
                                rank, pts, sg = std.get('rank', '-'), std.get('points', 0), std.get('goalsDiff', 0)
                                v, e, derr = std['all'].get('win') or 0, std['all'].get('draw') or 0, std['all'].get('lose') or 0
                                st.caption(f"🏆 Posição: {rank}º | Pontos: {pts} | SG: {sg}")
                                st.caption(f"Campanha: {v}V - {e}E - {derr}D")
                            else:
                                st.caption("Tabela não aplicável (Copa/Mata-Mata)")
                            st.write(f"Média Pró: **{d['a']['media_feita']:.2f}** | Sofrida: **{d['a']['media_sofrida']:.2f}**")

                        st.markdown("---")
                        st.markdown("**⚔️ Confronto Direto (Últimos 3 Jogos H2H):**")
                        if d.get('h2h') and len(d['h2h']) > 0:
                            for h2h_match in d['h2h']:
                                data_jogo = h2h_match['fixture']['date'][:10]
                                casa_nome = h2h_match['teams']['home']['name']
                                fora_nome = h2h_match['teams']['away']['name']
                                placar_casa = h2h_match['goals']['home']
                                placar_fora = h2h_match['goals']['away']
                                st.caption(f"📅 {data_jogo} | {casa_nome} {placar_casa} x {placar_fora} {fora_nome}")
                        else:
                            st.caption("Nenhum confronto direto recente encontrado.")

            if st.button("🔄 Refazer", key=f"ref_{f_id}"): acao_analisar([j], data_str, force=True)
        else: st.button(f"🔍 Analisar", key=f"btn_{f_id}", on_click=acao_analisar, args=([j], data_str))
        st.markdown("</div>", unsafe_allow_html=True)