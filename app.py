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
        "HOME": {"prob": prob_home * 100, "justa": 100/(prob_home * 100) if prob_home > 0.01 else 99},
        "DRAW": {"prob": prob_draw * 100, "justa": 100/(prob_draw * 100) if prob_draw > 0.01 else 99},
        "AWAY": {"prob": prob_away * 100, "justa": 100/(prob_away * 100) if prob_away > 0.01 else 99},
        "1X": {"prob": (prob_home + prob_draw) * 100, "justa": 100/((prob_home + prob_draw) * 100) if (prob_home + prob_draw) > 0.01 else 99},
        "X2": {"prob": (prob_away + prob_draw) * 100, "justa": 100/((prob_away + prob_draw) * 100) if (prob_away + prob_draw) > 0.01 else 99},
        "BTTS": {"prob": prob_ambas * 100, "justa": 100/(prob_ambas * 100) if prob_ambas > 0.01 else 99},
        "OVER": {"prob": prob_over_25 * 100, "justa": 100/(prob_over_25 * 100) if prob_over_25 > 0.01 else 99},
        "UNDER": {"prob": prob_under_25 * 100, "justa": 100/(prob_under_25 * 100) if prob_under_25 > 0.01 else 99},
        "TOP": placares[:5]
    }

# ==========================================
# 3. BUSCAS DE API E LÓGICA DE DADOS (NOVO MOTOR)
# ==========================================
def calcular_pseudo_xg(stats):
    sog = 0
    big_chances = 0
    for s in stats:
        if s['type'] == 'Shots on Goal': sog = int(s['value'] or 0)
        if s['type'] == 'Big Chances Created': big_chances = int(s['value'] or 0)
    return (sog * 0.15) + (big_chances * 0.35)

def buscar_stats_partida(fixture_id, team_id):
    url = f"{BASE_URL}/fixtures/statistics"
    params = {'fixture': fixture_id, 'team': team_id}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if res.get('response') and len(res['response']) > 0:
            stats = res['response'][0]['statistics']
            for s in stats:
                if s['type'] == 'expected_goals' and s['value']:
                    return float(s['value'])
            return calcular_pseudo_xg(stats)
    except: pass
    return 0

def buscar_historico_global(team_id, current_league_id, last_n=12): 
    url = f"{BASE_URL}/fixtures"
    params = {'team': team_id, 'last': 25, 'status': 'FT'}
    try:
        res = requests.get(url, headers=HEADERS, params=params).json()
        if not res.get('response'): return None
        
        hoje = datetime.datetime.now()
        limite_6_meses = hoje - datetime.timedelta(days=180)
        
        jogos_validos = []
        for j in res['response']:
            data_jogo = datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d')
            if data_jogo > limite_6_meses:
                jogos_validos.append(j)
        
        if len(jogos_validos) < 5: jogos_validos = res['response'][:8]
        else: jogos_validos = jogos_validos[:last_n]

        total_gols_f, total_gols_s = 0, 0
        total_xg_f, total_xg_s = 0, 0
        soma_pesos = 0
        forma = []
        competicoes = {}
        
        for idx, j in enumerate(jogos_validos):
            f_id = j['fixture']['id']
            l_name = j['league']['name']
            competicoes[l_name] = competicoes.get(l_name, 0) + 1
            
            dias_atras = (hoje - datetime.datetime.strptime(j['fixture']['date'][:10], '%Y-%m-%d')).days
            peso_tempo = math.exp(-0.005 * dias_atras) 
            peso_liga = 1.2 if j['league']['id'] == current_league_id else 1.0
            peso_final = peso_tempo * peso_liga
            
            is_home = j['teams']['home']['id'] == team_id
            gf = j['goals']['home'] if is_home else j['goals']['away']
            gs = j['goals']['away'] if is_home else j['goals']['home']
            
            xg_f, xg_s = 0, 0
            if idx < 6: 
                xg_f = buscar_stats_partida(f_id, team_id)
                xg_s = buscar_stats_partida(f_id, j['teams']['away']['id'] if is_home else j['teams']['home']['id'])
            
            total_gols_f += gf * peso_final
            total_gols_s += gs * peso_final
            total_xg_f += (xg_f or gf) * peso_final 
            total_xg_s += (xg_s or gs) * peso_final
            soma_pesos += peso_final
            
            if idx < 5:
                if gf > gs: forma.append("🟩")
                elif gf == gs: forma.append("⬜")
                else: forma.append("🟥")

        return {
            "media_feita": total_gols_f / soma_pesos, # Mantido nome original para não quebrar interface
            "media_sofrida": total_gols_s / soma_pesos, # Mantido nome original para não quebrar interface
            "media_xg_f": total_xg_f / soma_pesos,
            "media_xg_s": total_xg_s / soma_pesos,
            "total_jogos": len(jogos_validos),
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
                if grupo and ("promedio" in str(grupo[0].get('group', '')).lower() or "aggregate" in str(grupo[0].get('group', '')).lower()): continue
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
            odds = buscar_odds_vips(f_id); s_h = buscar_historico_global(h_id, l_id); s_a = buscar_historico_global(a_id, l_id)
            standings = buscar_standings(l_id, season, h_id, a_id); h2h = buscar_h2h(h_id, a_id)
            if s_h and s_a: 
                banco_local["datas"][data_str]["stats"][f_id] = {"odds": odds if odds else {"BTTS":0, "OVER":0, "UNDER":0, "HOME":0, "DRAW":0, "AWAY":0, "1X":0, "X2":0}, "h": s_h, "a": s_a, "standings": standings, "h2h": h2h}
            else: banco_local["datas"][data_str]["stats"][f_id] = {"erro": "Sem histórico suficiente"}
            time.sleep(0.2)
        progresso_bar.progress((idx + 1) / len(jogos_alvo))
    progresso_bar.empty(); salvar_banco(banco_local); st.rerun()

def get_ev(dados, p_dict, key):
    casa = dados['odds'].get(key, 0)
    justa = p_dict[key]['justa']
    return ((casa / justa) - 1) * 100 if (casa > justa and justa > 0) else -100

def calcular_ranking_dinamico(f_id, data_str, mercado_alvo, mercados_ativos):
    dados = banco_local["datas"][data_str]["stats"].get(f_id)
    if not dados or "erro" in dados or "h" not in dados or "a" not in dados: return -999
    
    # Motor Novo: Ponderação Gols + xG
    m_h = (dados['h']['media_xg_f'] * 0.6 + dados['h']['media_feita'] * 0.4 + dados['a']['media_xg_s'] * 0.6 + dados['a']['media_sofrida'] * 0.4) / 2
    m_a = (dados['a']['media_xg_f'] * 0.6 + dados['a']['media_feita'] * 0.4 + dados['h']['media_xg_s'] * 0.6 + dados['h']['media_sofrida'] * 0.4) / 2
    
    p = calcular_poisson(m_h, m_a)
    if not p: return -999
    
    if mercado_alvo == "🛡️ Zagueiros":
        evs = [get_ev(dados, p, k) for k in mercados_ativos if p[k]['prob'] > 65 and get_ev(dados, p, k) > 0]
        return max(evs) if evs else -999
    elif mercado_alvo == "🎯 Meio-Campo":
        evs = [get_ev(dados, p, k) for k in mercados_ativos if 45 <= p[k]['prob'] <= 65 and get_ev(dados, p, k) > 3]
        return max(evs) if evs else -999
    elif mercado_alvo == "🚀 Atacantes":
        evs = [get_ev(dados, p, k) for k in mercados_ativos if dados['odds'].get(k, 0) >= 2.0 and get_ev(dados, p, k) > 5 and p[k]['prob'] >= 30]
        return max(evs) if evs else -999
    elif mercado_alvo in mercados_ativos: 
        return get_ev(dados, p, mercado_alvo)
    else: 
        evs = [get_ev(dados, p, k) for k in mercados_ativos]
        return max(evs) if evs else -999

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
    st.progress(saldo / 7500); st.write("---")
    
    data_consulta = st.date_input("Data do Scanner", datetime.date.today())
    data_str = data_consulta.strftime("%Y-%m-%d")
    
    LIGAS_PRO = [39, 140, 135, 78, 61, 71, 72, 73, 2, 3, 848, 13, 11, 40, 88, 307, 253, 94, 128, 203]
    filtro_pro = st.checkbox("⭐ Apenas Ligas PRO", value=False); st.write("---")
    
    st.markdown("### ⚙️ Mercados Ativos")
    todos_mercados = {"HOME": "Vitória Casa", "DRAW": "Empate", "AWAY": "Vitória Fora", "1X": "Dupla Casa", "X2": "Dupla Fora", "BTTS": "Ambas Marcam", "OVER": "Over 2.5", "UNDER": "Under 2.5"}
    mercados_ativos = st.multiselect("Habilitar:", list(todos_mercados.keys()), default=list(todos_mercados.keys()), format_func=lambda x: todos_mercados[x])
    
    st.write("---")
    st.markdown("### 🎯 Filtrar por Método")
    opcoes_filtro = ["Geral (Todos)", "🛡️ Zagueiros", "🎯 Meio-Campo", "🚀 Atacantes"] + list(todos_mercados.keys())
    mercado_filtro = st.selectbox("Ordenar por:", opcoes_filtro)
    
    st.write("---")
    if st.button("🗑️ Limpar Cache do Dia"):
        if data_str in banco_local["datas"]: del banco_local["datas"][data_str]; salvar_banco(banco_local); st.rerun()

if data_str not in banco_local["datas"]: banco_local["datas"][data_str] = {"agenda": [], "stats": {}}
agenda = banco_local["datas"][data_str]["agenda"]

if st.button("🔄 1. Carregar Agenda do Dia", use_container_width=True):
    res = requests.get(f"{BASE_URL}/fixtures?date={data_str}&timezone=America/Sao_Paulo", headers=HEADERS).json()
    if res.get('response'): banco_local["datas"][data_str]["agenda"] = res['response']; salvar_banco(banco_local); st.rerun()

if agenda:
    jogos_visiveis = [j for j in agenda if not filtro_pro or j['league']['id'] in LIGAS_PRO]
    jogos_visiveis.sort(key=lambda x: calcular_ranking_dinamico(str(x['fixture']['id']), data_str, mercado_filtro, mercados_ativos), reverse=True)
    
    if st.button(f"🚀 2. Analisar Visíveis ({len(jogos_visiveis)})", type="primary", use_container_width=True): acao_analisar(jogos_visiveis, data_str)

    for j in jogos_visiveis:
        f_id = str(j['fixture']['id']); d = banco_local["datas"][data_str]["stats"].get(f_id)
        st.markdown(f'<div style="border:1px solid #333; border-radius:8px; padding:12px; background-color:#0e1117; margin-bottom:10px;"><div style="display:flex; justify-content:space-between; color:#888; font-size:11px;"><span>🕒 {j['fixture']['date'][11:16]} • {j['league']['name']}</span><span style="color:#28a745;">{'● Em Cache' if d else ''}</span></div><div style="font-size:18px; font-weight:bold; color:white; margin: 8px 0;">{j['teams']['home']['name']} <span style="color:#555; font-size:12px;">vs</span> {j['teams']['away']['name']}</div>', unsafe_allow_html=True)
        
        if d:
            if "erro" in d or "h" not in d or "a" not in d: st.warning(f"⚠️ {d.get('erro', 'Dados corrompidos.')}")
            else:
                m_h = (d['h']['media_xg_f'] * 0.6 + d['h']['media_feita'] * 0.4 + d['a']['media_xg_s'] * 0.6 + d['a']['media_sofrida'] * 0.4) / 2
                m_a = (d['a']['media_xg_f'] * 0.6 + d['a']['media_feita'] * 0.4 + d['h']['media_xg_s'] * 0.6 + d['h']['media_sofrida'] * 0.4) / 2
                p = calcular_poisson(m_h, m_a)
                if p:
                    conf = d['h']['total_jogos'] + d['a']['total_jogos']
                    st.markdown(f"<div style='color:#28a745; font-size:11px; margin-bottom:10px;'>🛡️ Confiança: {'🟢🟢🟢' if conf > 30 else '🟡🟡⚪'} ({conf} jogos analisados)</div>", unsafe_allow_html=True)
                    
                    cols = st.columns(3); idx_col = 0
                    mapping_nomes = {"HOME": "Mandante (1)", "DRAW": "Empate (X)", "AWAY": "Visitante (2)", "1X": "Dupla Casa", "X2": "Dupla Fora", "BTTS": "BTTS (Ambas)", "OVER": "Over 2.5", "UNDER": "Under 2.5"}
                    for m_key in mercados_ativos:
                        renderizar_mercado(cols[idx_col % 3], mapping_nomes[m_key], p, m_key, d['odds']); idx_col += 1
                    
                    with st.expander("📊 Raio-X e Ferramentas"):
                        forma_h, forma_a = d['h'].get('forma', 'Sem dados'), d['a'].get('forma', 'Sem dados')
                        
                        resumo = f"Analise este jogo para mim:\n\n⚽ JOGO: {j['teams']['home']['name']} vs {j['teams']['away']['name']}\n🏆 LIGA: {j['league']['name']}\n🛡️ CONFIANÇA: {conf} jogos analisados\n\n📊 PROBABILIDADES E VALOR (POISSON):\n"
                        for mk in mercados_ativos:
                            ev = get_ev(d, p, mk)
                            resumo += f"- {mapping_nomes[mk]}: {p[mk]['prob']:.0f}% (Odd Justa: {p[mk]['justa']:.2f} | Casa: {d['odds'].get(mk, 0)}) | EV: {ev:.1f}%\n"
                        
                        resumo += f"\n🧠 INTELIGÊNCIA HUMANA (RAIO-X):\n🏠 {j['teams']['home']['name']}:\n   - Forma Recente: {forma_h}\n   - Médias Gols: {d['h']['media_feita']:.2f} Pró / {d['h']['media_sofrida']:.2f} Sofrida\n   - Médias xG: {d['h']['media_xg_f']:.2f} Pró / {d['h']['media_xg_s']:.2f} Sofrida\n"
                        if d.get("standings") and d["standings"].get("h"):
                            std_h = d["standings"]["h"]
                            resumo += f"   - Tabela: {std_h.get('rank', '-')}º lugar | SG: {std_h.get('goalsDiff', 0)} | Campanha: {std_h['all'].get('win', 0)}V-{std_h['all'].get('draw', 0)}E-{std_h['all'].get('lose', 0)}D\n"
                        
                        resumo += f"\n🚀 {j['teams']['away']['name']}:\n   - Forma Recente: {forma_a}\n   - Médias Gols: {d['a']['media_feita']:.2f} Pró / {d['a']['media_sofrida']:.2f} Sofrida\n   - Médias xG: {d['a']['media_xg_f']:.2f} Pró / {d['a']['media_xg_s']:.2f} Sofrida\n"
                        if d.get("standings") and d["standings"].get("a"):
                            std_a = d["standings"]["a"]
                            resumo += f"   - Tabela: {std_a.get('rank', '-')}º lugar | SG: {std_a.get('goalsDiff', 0)} | Campanha: {std_a['all'].get('win', 0)}V-{std_a['all'].get('draw', 0)}E-{std_a['all'].get('lose', 0)}D\n"
                        
                        resumo += "\n⚔️ CONFRONTO DIRETO (ÚLTIMOS 3):\n"
                        if d.get('h2h') and len(d['h2h']) > 0:
                            for h2h_match in d['h2h']:
                                resumo += f"   - {h2h_match['fixture']['date'][:10]}: {h2h_match['teams']['home']['name']} {h2h_match['goals']['home']} x {h2h_match['goals']['away']} {h2h_match['teams']['away']['name']}\n"
                        else: resumo += "   - Nenhum confronto recente encontrado.\n"
                        
                        st.info("Texto pronto! Copie o bloco abaixo (sem pop-ups chatos) e cole na nossa conversa:")
                        st.text_area("Copiar para Gemini:", value=resumo, height=200, label_visibility="collapsed")

                        colA, colB = st.columns(2)
                        with colA:
                            st.markdown(f"**🏠 {j['teams']['home']['name']}**")
                            st.write(f"Forma: {forma_h}")
                            st.write(f"Média Gols: **{d['h']['media_feita']:.2f}** Pró | **{d['h']['media_sofrida']:.2f}** Sofridos")
                            st.write(f"Média xG: **{d['h']['media_xg_f']:.2f}** Pró | **{d['h']['media_xg_s']:.2f}** Sofridos")
                            if d.get("standings") and d["standings"].get("h"):
                                std = d["standings"]["h"]
                                st.caption(f"Posição: {std.get('rank', '-')}º | SG: {std.get('goalsDiff', 0)} | {std['all'].get('win', 0)}V-{std['all'].get('draw', 0)}E-{std['all'].get('lose', 0)}D")
                        with colB:
                            st.markdown(f"**🚀 {j['teams']['away']['name']}**")
                            st.write(f"Forma: {forma_a}")
                            st.write(f"Média Gols: **{d['a']['media_feita']:.2f}** Pró | **{d['a']['media_sofrida']:.2f}** Sofridos")
                            st.write(f"Média xG: **{d['a']['media_xg_f']:.2f}** Pró | **{d['a']['media_xg_s']:.2f}** Sofridos")
                            if d.get("standings") and d["standings"].get("a"):
                                std = d["standings"]["a"]
                                st.caption(f"Posição: {std.get('rank', '-')}º | SG: {std.get('goalsDiff', 0)} | {std['all'].get('win', 0)}V-{std['all'].get('draw', 0)}E-{std['all'].get('lose', 0)}D")
                        st.markdown("---")
                        if d.get('h2h') and len(d['h2h']) > 0:
                            for h2h_match in d['h2h']:
                                data_jogo, casa_nome, fora_nome = h2h_match['fixture']['date'][:10], h2h_match['teams']['home']['name'], h2h_match['teams']['away']['name']
                                st.caption(f"📅 {data_jogo} | {casa_nome} {h2h_match['goals']['home']} x {h2h_match['goals']['away']} {fora_nome}")
            if st.button("🔄 Refazer", key=f"ref_{f_id}"): acao_analisar([j], data_str, force=True)
        else: st.button(f"🔍 Analisar", key=f"btn_{f_id}", on_click=acao_analisar, args=([j], data_str))
        st.markdown("</div>", unsafe_allow_html=True)
