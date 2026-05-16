"""
QG Barrios PRO V3 - Interface Streamlit
========================================

Melhorias sobre V2:
1. Ligas expandidas (35 ligas) + botão 'Forçar Busca' para ligas avulsas
2. Bug de banca corrigido: banca_inicial é imutável (denominador do ROI),
   depósitos/retiradas manuais ficam em banco.depositos
3. Top 5 Entradas do Dia + Consultora Gemini opcional
4. Calibração simplificada: um único botão 'Calibrar TODAS' (sem incremental)

Stack: Streamlit, motor.py, dados.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import streamlit as st

from motor import (
    ParametrosLiga,
    prever_jogo,
    comparar_com_mercado,
    filtrar_gatilho,
    MERCADOS_PRODUCAO,
    EV_MIN_POR_MERCADO,
)
from dados import (
    BancoQG,
    DadosManager,
    LIGAS_SUPORTADAS,
    LIGAS_TEMPORADA_ANO_ATUAL,
    INTERVALO_RECALIBRACAO_DIAS,
    SALDO_MINIMO_EMERGENCIA,
    SALDO_MIN_PARA_CALIBRACAO,
    CUSTO_ESTIMADO_ODDS_JOGO,
    CUSTO_ESTIMADO_FIXTURES_DIA,
    CUSTO_ESTIMADO_HISTORICO_LIGA,
    CUSTO_ESTIMADO_XG_LIGA,
    CUSTO_ESTIMADO_XG_FIXTURE,
    PESO_XG_PRODUCAO,
    TIMEOUT_CALIBRACAO_SEGUNDOS,
    CreditosInsuficientesError,
    APIError,
    criar_dados_manager_de_secrets,
)


st.set_page_config(page_title="QG Barrios PRO V3", layout="wide", page_icon="👑")

# =========================================================================
# 1. CONSTANTES DE NEGÓCIO
# =========================================================================

PISO_KELLY_PADRAO        = 2.0
TETO_PCT_BANCA_PADRAO    = 0.10
ODD_MIN_SAVE             = 1.50
LIMITE_DIVERGENCIA_PP    = 20.0
MARGEM_BOOKMAKER_DEFAULT = 1.05

# Ranking de qualidade — sem número fixo
SCORE_MINIMO_RANKING = 35   # picks abaixo disso são filtrados mesmo com EV positivo
# Pesos do score composto (devem somar 1.0)
PESO_EV          = 0.35
PESO_DIVERGENCIA = 0.30
PESO_PROB        = 0.20
PESO_KELLY       = 0.15


# =========================================================================
# 2. INICIALIZAÇÃO DO MANAGER
# =========================================================================

@st.cache_resource
def get_manager() -> DadosManager:
    return criar_dados_manager_de_secrets(st.secrets, diretorio_local=".")


try:
    dm = get_manager()
except Exception as e:
    st.error(f"Falha ao inicializar manager: {e}")
    st.stop()

if "banco" not in st.session_state:
    st.session_state["banco"] = dm.carregar_banco()

banco: BancoQG = st.session_state["banco"]


# =========================================================================
# 3. FUNÇÕES UTILITÁRIAS
# =========================================================================

def calcular_stake_final(kelly_fracao: float, banca: float,
                          piso: float = PISO_KELLY_PADRAO,
                          teto_pct_banca: float = TETO_PCT_BANCA_PADRAO) -> float:
    """Piso R$2 / teto 10% da banca / descarta se acima do teto."""
    if kelly_fracao <= 0 or banca <= 0:
        return 0.0
    stake_kelly = kelly_fracao * banca
    stake_final = max(piso, stake_kelly)
    if stake_final > banca * teto_pct_banca:
        return 0.0
    return round(stake_final, 2)


def calcular_estado_banca(picks: list, banca_inicial: float,
                           depositos: list) -> tuple[float, float, float, float]:
    """
    Retorna (lucro_picks, total_depositos, banca_atual, roi_pct).

    SEPARAÇÃO CORRETA:
    - lucro_picks  = P/L puro das apostas (sem depósitos)
    - ROI          = lucro_picks / banca_inicial   ← denominador NUNCA muda
    - banca_atual  = banca_inicial + depositos + lucro_picks
    """
    lucro_picks = 0.0
    for p in picks:
        status = p.get("status", "Pendente")
        stake  = float(p.get("stake", 0))
        odd    = float(p.get("odd", 1.0))
        if status == "Green":
            lucro_picks += stake * (odd - 1.0)
        elif status == "Red":
            lucro_picks -= stake

    total_dep = sum(float(d.get("valor", 0)) for d in depositos)
    banca_atual = banca_inicial + total_dep + lucro_picks
    roi = (lucro_picks / banca_inicial * 100) if banca_inicial > 0 else 0.0
    return lucro_picks, total_dep, banca_atual, roi


def detectar_temporada_atual() -> int:
    hoje = dt.date.today()
    return hoje.year if hoje.month >= 7 else hoje.year - 1


def filtrar_jogos_calibrados(agenda: list[dict],
                              params_ligas: dict) -> tuple[list, list]:
    calibrados, sem_cal = [], []
    for j in agenda:
        l_id = j.get("league", {}).get("id")
        if str(l_id) in params_ligas:
            calibrados.append(j)
        else:
            sem_cal.append(j)
    return calibrados, sem_cal


def calcular_score_qualidade(
    ev_pct: float,
    divergencia_pp: float,
    prob_modelo: float,
    kelly_fracao: float,
    odd: float,
    cobertura_ok: bool,
) -> float:
    """
    Score composto de qualidade 0–100 para rankeamento de picks.

    Componentes (normalizados a 0–1, depois ponderados):
      EV          (35%) — cap em 20% EV = max, logarítmico acima de 10%
      Divergência (30%) — cap em 12pp = max
      Prob modelo (20%) — cap em 55% = max (acima é "plenamente confiante")
      Kelly frac  (15%) — cap em 6% Kelly = max

    Penalidades multiplicativas:
      cobertura_ok=False : ×0.80  (dados insuficientes do time)
      odd > 5.0          : ×0.85  (alta variância = resultado de placares exóticos)
      odd < 1.55         : ×0.90  (juice alto consome a edge em odds baixas)
    """
    # Normalização cap linear com suavização logarítmica na cauda de EV
    if ev_pct >= 10:
        s_ev = min(0.75 + 0.25 * (ev_pct - 10) / 10, 1.0)
    else:
        s_ev = min(ev_pct / 10 * 0.75, 0.75)

    s_div  = min(divergencia_pp / 12.0, 1.0)
    s_prob = min(prob_modelo    / 55.0, 1.0)
    s_kel  = min(kelly_fracao   / 0.06, 1.0)

    score = (
        PESO_EV          * s_ev  +
        PESO_DIVERGENCIA * s_div +
        PESO_PROB        * s_prob +
        PESO_KELLY       * s_kel
    ) * 100.0

    # Penalidades multiplicativas (aplicadas em sequência)
    if not cobertura_ok:
        score *= 0.80   # dados insuficientes do time

    # Penalidade por probabilidade absoluta baixa
    # Prob < 15%: altíssima variância, muito ruído — kills UNDER_05 extremos
    # Prob < 20%: cautela extra mesmo com EV positivo
    if prob_modelo < 15.0:
        score *= 0.65
    elif prob_modelo < 20.0:
        score *= 0.85

    # Penalidade por odd alta (variância aumenta exponencialmente)
    if odd > 7.0:
        score *= 0.70   # odds acima de 7: ruído puro, ignore
    elif odd > 5.0:
        score *= 0.80   # odd alta: penalidade reforçada (era 0.85)
    elif odd < 1.55:
        score *= 0.90   # juice alto consome a edge

    return round(score, 1)


def consultar_gemini(picks_aprovados: list[dict]) -> str:
    """Chama Gemini Flash para análise tática dos picks aprovados."""
    try:
        import google.generativeai as genai
        api_key = st.secrets.get("GEMINI_API_KEY", "")
        if not api_key:
            return "⚠️ GEMINI_API_KEY não configurada em secrets."
        genai.configure(api_key=api_key)
        model = None
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                if "flash" in m.name.lower():
                    model = genai.GenerativeModel(m.name)
                    break
        if model is None:
            model = genai.GenerativeModel("gemini-pro")
    except ImportError:
        return "⚠️ Biblioteca google-generativeai não instalada. Execute: pip install google-generativeai"

    # Todos os candidatos aprovados, ordenados por score desc, cap em 30 para tokens
    todos = sorted(picks_aprovados, key=lambda x: x.get("score", 0), reverse=True)[:30]
    linhas = []
    for i, p in enumerate(todos, 1):
        linhas.append(
            f"{i}. [{p.get('score', 0):.0f}/100] {p['jogo']} | {p['mercado']} | "
            f"Liga: {p.get('liga', '?')} | "
            f"Odd {p['odd']:.2f} | Modelo {p['prob_modelo']:.1f}% vs Mercado {p.get('prob_mercado', 0):.1f}% | "
            f"Δ {p.get('divergencia', 0):+.1f}pp | EV {p['ev']:+.1f}% | Stake R$ {p['stake']:.2f}"
        )

    prompt = f"""Você é um analista quantitativo de apostas esportivas especializado em modelos Dixon-Coles.
Recebeu {len(todos)} candidatos aprovados (Score ≥ mínimo + EV positivo). \
Cada linha é um mercado independente — pode haver múltiplos mercados do mesmo jogo. \
Score 0-100 pondera EV, divergência modelo×mercado, probabilidade absoluta e critério de Kelly.

=== ESTRUTURA DE ENTREGA — SIGA RIGOROSAMENTE ===

## 1. SINAIS DO DIA (Entradas Executivas)

REGRA DE ESCALABILIDADE: Avalie e entregue UMA recomendação para CADA jogo único presente nos dados. \
Se há 10 jogos distintos, entregue 10 sinais. Não limite a um Top 3 ou Top 5 arbitrário.

REGRA DE DEDUPLICAÇÃO: Escolha APENAS UM mercado por partida — aquele com a melhor combinação \
de Score e segurança estatística (prefira prob_modelo > 55%, menor variância, odd entre 1.50 e 2.50). \
É ESTRITAMENTE PROIBIDO listar o mesmo jogo duas vezes.

FORMATO OBRIGATÓRIO para cada sinal (uma linha por jogo):
[Liga] Time A v Time B | Mercado | Odd X.XX | Score ZZ/100 | justificativa de valor em 1 linha curta

## 2. RESUMO DE DESCARTES

Em 1 ou 2 parágrafos, cite quais mercados correlacionados foram descartados para evitar sobreposição \
de risco. Exemplo: "No jogo X, BTTS_NO e Under 2.5 foram descartados em favor do Under 3.5 \
(Score mais alto + menor exposição a variância ofensiva)."

## 3. ALERTAS DE RISCO

Mencione APENAS se houver: odds > 4.0, prob_modelo < 20%, divergência extrema ou flags do sistema. \
Se não houver alertas graves, escreva somente: "Nenhum alerta crítico."

=== FIM DA ESTRUTURA ===

Dados do dia — {len(todos)} candidatos ({len(picks_aprovados)} aprovados pelo filtro de EV):
{chr(10).join(linhas)}

Seja cirúrgico e objetivo. Use os números fornecidos. Não repita dados já listados — adicione interpretação."""

    try:
        resp = model.generate_content(prompt)
        return resp.text
    except Exception as e:
        return f"Erro ao consultar Gemini: {e}"


# =========================================================================
# 3b. CACHE GLOBAL DIÁRIO (compartilhado entre sessões e dispositivos)
# =========================================================================
# @st.cache_data armazena resultados no servidor, partilhados por TODAS as
# sessões ativas. No Streamlit Cloud isso significa: Device A carrega às 9h,
# Device B às 15h obtém o mesmo resultado sem nenhuma chamada à API.
# TTL de 86400s (24h) garante que o cache não ultrapassa o dia corrente.

@st.cache_data(ttl=86400, show_spinner=False)
def _agenda_do_dia_cached(_dm: DadosManager, data_str: str) -> list:
    """Busca agenda na API e armazena 24h no servidor. Underscore em _dm = não hasheado."""
    return _dm.buscar_agenda_dia(data_str)


@st.cache_data(ttl=86400, show_spinner=False)
def _gemini_do_dia_cached(data_str: str, candidatos: list) -> str:
    """Chama Gemini e armazena resposta 24h. Re-chama se os candidatos mudarem."""
    return consultar_gemini(candidatos)


# =========================================================================
# 4. SIDEBAR — BANCA (FIX DO BUG DE ROI)
# =========================================================================

with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO V3")
    st.caption("Motor: Dixon-Coles (MLE) · Sem incremental")

    # ── Créditos API ─────────────────────────────────────────────────
    try:
        saldo = dm.saldo_creditos()
    except Exception:
        saldo = 0
    cor = "🟢" if saldo > 1000 else ("🟡" if saldo > SALDO_MINIMO_EMERGENCIA else "🔴")
    st.metric(f"{cor} Créditos API", f"{saldo}/7500")
    st.progress(min(saldo / 7500, 1.0))
    if saldo < SALDO_MINIMO_EMERGENCIA:
        st.error(f"⚠️ Saldo abaixo de {SALDO_MINIMO_EMERGENCIA}. Trava ativa.")

    st.divider()

    # ── BANCA — separação banca_inicial / depósitos / P/L ────────────
    st.markdown("### 💰 Banca")

    lucro_picks, total_dep, banca_atual, roi_pct = calcular_estado_banca(
        banco.picks, banco.banca_inicial, banco.depositos
    )

    # Banca inicial: editável apenas se não há picks ainda (ou via reset explícito)
    with st.expander("⚙️ Redefinir banca inicial", expanded=False):
        st.caption("⚠️ Use somente ao começar uma nova banca do zero. Não afeta o histórico de picks.")
        nova_banca = st.number_input(
            "Nova banca inicial (R$)", value=float(banco.banca_inicial),
            step=1.0, min_value=1.0, key="nova_banca_inicial"
        )
        if st.button("💾 Confirmar nova banca inicial", use_container_width=True):
            banco.banca_inicial = nova_banca
            dm.salvar_banco(banco)
            st.success(f"Banca inicial redefinida para R$ {nova_banca:.2f}")
            st.rerun()

    with st.expander("🗑️ Reiniciar temporada (Hard Reset)", expanded=False):
        st.error(
            "**AÇÃO IRREVERSÍVEL.** Apaga todos os picks e depósitos registrados. "
            "Use apenas para iniciar uma nova temporada do zero."
        )
        n_picks_hr = len(banco.picks)
        n_dep_hr   = len(banco.depositos)
        st.caption(f"Serão apagados: **{n_picks_hr} pick(s)** e **{n_dep_hr} depósito(s)**.")
        nova_banca_hr = st.number_input(
            "Nova banca inicial após reset (R$)",
            value=float(banco.banca_inicial),
            step=1.0, min_value=1.0,
            key="hr_nova_banca",
        )
        confirmar_hr = st.checkbox(
            f"Confirmo que quero APAGAR os {n_picks_hr} picks e {n_dep_hr} depósitos permanentemente",
            key="hr_confirmar",
        )
        if st.button(
            "🗑️ Executar Hard Reset",
            type="primary",
            use_container_width=True,
            disabled=not confirmar_hr,
        ):
            banco.picks      = []
            banco.depositos  = []
            banco.banca_inicial = nova_banca_hr
            dm.salvar_banco(banco)
            st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
            st.success(
                f"✅ Hard Reset concluído. Banca inicial: R$ {nova_banca_hr:.2f}. "
                "Picks e depósitos apagados."
            )
            st.rerun()

    # Métricas da banca
    c1, c2 = st.columns(2)
    c1.metric("Capital inicial", f"R$ {banco.banca_inicial:.2f}")
    c2.metric("Depósitos/Retiradas", f"R$ {total_dep:+.2f}")
    st.metric(
        "Banca atual",
        f"R$ {banca_atual:.2f}",
        delta=f"P/L apostas: R$ {lucro_picks:+.2f} | ROI: {roi_pct:+.1f}%"
    )

    # Registrar depósito ou retirada
    with st.expander("➕ Depositar / ➖ Retirar dinheiro"):
        st.caption("Registra entrada/saída de dinheiro sem alterar o ROI das apostas.")
        tipo = st.radio("Tipo", ["Depósito", "Retirada"], horizontal=True)
        valor_dep = st.number_input("Valor (R$)", min_value=0.01, step=1.0, value=10.0)
        nota_dep  = st.text_input("Nota (opcional)", placeholder="Ex: recarga mensal")
        if st.button("✅ Registrar", use_container_width=True, type="primary"):
            valor_final = valor_dep if tipo == "Depósito" else -valor_dep
            banco.depositos.append({
                "data": dt.date.today().isoformat(),
                "valor": valor_final,
                "nota": nota_dep,
                "registrado_em": dt.datetime.now().isoformat(),
            })
            dm.salvar_banco(banco)
            st.success(f"{'Depósito' if valor_final > 0 else 'Retirada'} de R$ {abs(valor_final):.2f} registrado!")
            st.rerun()

    if banco.depositos:
        with st.expander(f"📋 Histórico de depósitos ({len(banco.depositos)})"):
            for d in reversed(banco.depositos[-10:]):
                sinal = "➕" if d["valor"] > 0 else "➖"
                st.caption(f"{sinal} R$ {abs(d['valor']):.2f} em {d['data']} — {d.get('nota', '—')}")

    st.divider()

    # ── Configurações Kelly ───────────────────────────────────────────
    with st.expander("⚙️ Gestão de Risco"):
        piso_kelly   = st.number_input("Piso de stake (R$)", value=PISO_KELLY_PADRAO, step=0.5, min_value=0.5)
        teto_pct     = st.slider("Teto % da banca", 5, 25, int(TETO_PCT_BANCA_PADRAO * 100)) / 100
        odd_min_save = st.number_input("Odd mínima p/ salvar", value=ODD_MIN_SAVE, step=0.05, min_value=1.01)
        # Teto em 20pp: acima disso o detector de anomalia é desligado efetivamente.
        # 20pp é o limiar documentado no paper de Dixon-Coles e nos bugs do V6.1.
        limite_div   = st.slider("Anomalia se divergência >", 10, 20, int(LIMITE_DIVERGENCIA_PP))

    st.divider()

    # ── Memória Diária ───────────────────────────────────────────────
    st.caption("**💾 Memória Diária** — agenda e IA cacheadas 24h no servidor.")
    if st.button("🔄 Limpar Memória do Dia", use_container_width=True,
                 help="Força re-fetch da agenda e re-geração da análise IA. Use se um jogo foi adiado."):
        # Limpa os caches globais de todas as datas (st.cache_data.clear é o modo oficial)
        _agenda_do_dia_cached.clear()
        _gemini_do_dia_cached.clear()
        # Limpa banco.datas para remover agendas da sessão atual (evita servir dado stale)
        banco.datas.clear()
        # Limpa flags de sessão
        st.session_state.pop("gemini_resposta", None)
        st.session_state["banco"] = banco
        st.success("Cache limpo. Próxima carga buscará dados frescos da API.")
        st.rerun()

    st.divider()

    # ── Data e temporada ─────────────────────────────────────────────
    data_consulta = st.date_input("Data do Scanner", dt.date.today())
    data_str      = data_consulta.strftime("%Y-%m-%d")
    season        = st.number_input("Temporada (ano)", value=detectar_temporada_atual(), step=1)


# =========================================================================
# 5. CABEÇALHO
# =========================================================================

st.title("QG Barrios PRO V3")

n_calibradas       = len(banco.params_ligas)
n_picks_pendentes  = sum(1 for p in banco.picks if p.get("status") == "Pendente")
n_picks_total      = len(banco.picks)

col_h1, col_h2, col_h3, col_h4 = st.columns(4)
col_h1.metric("Ligas calibradas", f"{n_calibradas}/{len(LIGAS_SUPORTADAS)}")
col_h2.metric("Picks salvos", n_picks_total)
col_h3.metric("Picks pendentes", n_picks_pendentes)
col_h4.metric("Banca atual", f"R$ {banca_atual:.2f}")

st.divider()


# =========================================================================
# 6. ABAS PRINCIPAIS
# =========================================================================

tab_analise, tab_calibracao, tab_tracker, tab_auditoria = st.tabs([
    "🎯 Análise Diária",
    "⚙️ Calibração de Ligas",
    "📋 Tracker (Diário de Bordo)",
    "🔬 Auditoria do Motor",
])


# =========================================================================
# 6.1 ABA CALIBRAÇÃO — botão único, sem incremental
# =========================================================================

with tab_calibracao:
    st.markdown("### Status das ligas")
    _custo_por_liga = CUSTO_ESTIMADO_HISTORICO_LIGA + CUSTO_ESTIMADO_XG_LIGA
    st.caption(
        f"Calibração manual: clique 'Calibrar TODAS' segunda e quinta. "
        f"Custo estimado por liga: ~{_custo_por_liga} créditos "
        f"(histórico + xG blend peso={PESO_XG_PRODUCAO})."
    )

    # Tabela de status
    rows_status = []
    for league_id, nome in LIGAS_SUPORTADAS.items():
        params_d = banco.params_ligas.get(str(league_id), {})
        if not params_d:
            status, n_times, n_jogos = "❌ Nunca calibrada", 0, 0
        else:
            try:
                data_cal = dt.datetime.fromisoformat(params_d.get("calibrado_em", ""))
                dias = (dt.datetime.now() - data_cal).days
                status = f"🟡 Velha ({dias}d)" if dias >= INTERVALO_RECALIBRACAO_DIAS else f"🟢 Fresca ({dias}d)"
            except Exception:
                status = "⚠️ Cache inválido"
            n_times = len(params_d.get("times", {}))
            n_jogos = params_d.get("n_jogos_calibracao", 0)
        rows_status.append({"Liga": f"{nome} (ID {league_id})", "Status": status,
                             "Times": n_times, "Jogos usados": n_jogos})

    st.dataframe(rows_status, use_container_width=True, hide_index=True)

    custo_total = len(LIGAS_SUPORTADAS) * (CUSTO_ESTIMADO_HISTORICO_LIGA + CUSTO_ESTIMADO_XG_LIGA)
    st.info(
        f"Custo estimado para calibrar todas as {len(LIGAS_SUPORTADAS)} ligas: "
        f"~{custo_total} créditos (inclui xG via /fixtures/statistics, peso={PESO_XG_PRODUCAO})."
    )

    # ── Trava de Custo — Calibrar TODAS (2 fases) ────────────────────
    st.markdown("#### 🔄 Calibração com Delta Fetch")
    st.caption(
        "**Passo 1** analisa quantos jogos novos existem desde o último download "
        f"(custo: ~{len(LIGAS_SUPORTADAS) * 2} créditos para as listas). "
        "**Passo 2** confirma o download de xG e executa o MLE. "
        "Nenhum crédito de xG é gasto antes da sua confirmação."
    )

    # ── FASE 1: botão de análise ──────────────────────────────────────
    if st.button(
        f"🔍 Passo 1 — Analisar Custo de Download ({len(LIGAS_SUPORTADAS)} ligas)",
        use_container_width=True,
    ):
        with st.spinner(
            f"Consultando listas de fixtures para {len(LIGAS_SUPORTADAS)} ligas "
            f"(~{len(LIGAS_SUPORTADAS) * 2} créditos)…"
        ):
            try:
                preview = dm.calcular_custo_delta(season=season)
                st.session_state["delta_preview"] = preview
                st.session_state.pop("delta_confirmado", None)
            except CreditosInsuficientesError as e:
                st.error(f"Saldo insuficiente para análise: {e}")
            except Exception as e:
                st.error(f"Falha ao calcular custo: {e}")
        st.rerun()

    # ── Exibe preview (se calculado) ──────────────────────────────────
    preview = st.session_state.get("delta_preview")
    if preview:
        n_total = preview["n_novos_total"]
        custo   = preview["custo_estimado_creditos"]
        saldo_ok = custo <= saldo - SALDO_MINIMO_EMERGENCIA

        if n_total == 0:
            st.success(
                "✅ **Cache 100% atualizado.** Nenhum fixture novo para baixar. "
                "Pode avançar para calibração diretamente."
            )
        elif saldo_ok:
            st.warning(
                f"📊 **{n_total} jogos novos detectados** · "
                f"Custo estimado de xG: **{custo} créditos** · "
                f"Saldo disponível: {saldo} créditos"
            )
        else:
            st.error(
                f"🔴 **Saldo insuficiente.** São necessários {custo + SALDO_MINIMO_EMERGENCIA} "
                f"créditos ({custo} xG + {SALDO_MINIMO_EMERGENCIA} buffer), "
                f"mas o saldo é {saldo}. Faça bootstrap parcial ou aguarde renovação."
            )

        # Tabela de detalhes por liga
        rows_delta = []
        for liga_info in preview["ligas"]:
            n_novos_l = liga_info["n_novos_liga"]
            if n_novos_l == 0:
                st_icon = "✅"
            elif n_novos_l > 150:
                st_icon = "🔴 bootstrap"
            else:
                st_icon = "🟡"
            season_detalhe = " | ".join(
                f"s{s['season']}: +{s['n_novos']} ({s['n_cache']} cache)"
                if s.get("erro") is None
                else f"s{s['season']}: ERRO"
                for s in liga_info["seasons"]
            )
            rows_delta.append({
                "Liga":              liga_info["nome"],
                "Novos fixtures":    n_novos_l,
                "Créditos xG":       n_novos_l * CUSTO_ESTIMADO_XG_FIXTURE,
                "Detalhe seasons":   season_detalhe,
                "Status":            st_icon,
            })

        with st.expander("📋 Ver detalhes por liga"):
            st.dataframe(rows_delta, use_container_width=True, hide_index=True)

        # ── FASE 2: botão de confirmação ──────────────────────────────
        btn_confirma_label = (
            f"🚀 Calibrar TODAS (cache ok — 0 créditos de xG)"
            if n_total == 0
            else f"✅ Passo 2 — Confirmar Download de xG ({custo} créditos) e Calibrar TODAS"
        )
        if st.button(
            btn_confirma_label,
            type="primary",
            use_container_width=True,
            disabled=(not saldo_ok and n_total > 0),
            help=(
                "Executa o Delta Fetch e recalibra via MLE."
                if saldo_ok or n_total == 0
                else f"Saldo insuficiente: {saldo} < {custo + SALDO_MINIMO_EMERGENCIA} necessários."
            ),
        ):
            st.session_state["delta_confirmado"] = True
            st.session_state.pop("delta_preview", None)
            st.rerun()

    # ── Executa calibração após confirmação ───────────────────────────
    if st.session_state.get("delta_confirmado"):
        st.session_state.pop("delta_confirmado", None)
        progress_bar = st.progress(0)
        status_box   = st.empty()
        erros, timeouts = [], []
        total = len(LIGAS_SUPORTADAS)

        for i, (lid, nome) in enumerate(LIGAS_SUPORTADAS.items()):
            status_box.info(f"[{i+1}/{total}] Calibrando **{nome}**…")
            try:
                dm.obter_params_liga(lid, season, forcar_recalibracao=True)
            except CreditosInsuficientesError as e:
                status_box.error(f"🔴 Saldo insuficiente. Parando em **{nome}**: {e}")
                break
            except TimeoutError:
                timeouts.append(nome)
            except Exception as e:
                erros.append(f"**{nome}**: {e}")
            progress_bar.progress((i + 1) / total)

        status_box.empty()
        if timeouts:
            st.warning(
                f"⏱️ {len(timeouts)} liga(s) com timeout (MLE > {TIMEOUT_CALIBRACAO_SEGUNDOS}s):\n"
                + "\n".join(f"• {n}" for n in timeouts)
            )
        if erros:
            st.warning("⚠️ Ligas com falha:\n" + "\n".join(f"• {e}" for e in erros))
        if not timeouts and not erros:
            st.success("✅ Todas as ligas calibradas com sucesso!")

        st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
        st.rerun()

    # ── Calibrar ligas em lote ────────────────────────────────────────
    with st.expander("⚙️ Calibrar ligas em lote"):
        # Inicializa chave do multiselect antes de renderizar os atalhos
        if "cal_lote_multiselect" not in st.session_state:
            st.session_state["cal_lote_multiselect"] = []

        # Atalhos de seleção rápida
        col_qs1, col_qs2, col_qs3 = st.columns(3)
        if col_qs1.button("✅ Todas as ligas", key="qs_todas", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = list(LIGAS_SUPORTADAS.keys())
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()
        if col_qs2.button("❌ Não calibradas", key="qs_nunca", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = [
                lid for lid in LIGAS_SUPORTADAS if str(lid) not in banco.params_ligas
            ]
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()
        if col_qs3.button("🗑️ Limpar seleção", key="qs_limpar", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = []
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()

        ligas_lote_sel = st.multiselect(
            "Ligas a calibrar",
            options=list(LIGAS_SUPORTADAS.keys()),
            format_func=lambda x: f"{LIGAS_SUPORTADAS[x]} (ID {x})",
            key="cal_lote_multiselect",
        )

        if not ligas_lote_sel:
            st.info("Selecione ao menos uma liga. Use os atalhos acima para seleção rápida.")
        else:
            st.caption(
                f"**{len(ligas_lote_sel)} liga(s) selecionada(s).** "
                "O lote será processado em sequência de forma autônoma."
            )

            # Fase 1: análise de custo do lote
            if st.button(
                f"🔍 Analisar custo do lote ({len(ligas_lote_sel)} liga(s))",
                use_container_width=True,
                key="btn_analisa_lote",
            ):
                with st.spinner(
                    f"Consultando listas de fixtures para {len(ligas_lote_sel)} liga(s) "
                    f"(~{len(ligas_lote_sel) * 2} créditos)…"
                ):
                    try:
                        _prev_lote = dm.calcular_custo_delta(ligas=ligas_lote_sel, season=season)
                        st.session_state["delta_preview_lote"] = _prev_lote
                        st.session_state["delta_lote_ids"]     = sorted(ligas_lote_sel)
                        st.session_state.pop("delta_confirmado_lote", None)
                    except CreditosInsuficientesError as e:
                        st.error(f"Saldo insuficiente para análise: {e}")
                    except Exception as e:
                        st.error(f"Falha ao calcular custo: {e}")
                st.rerun()

            _prev_lote = st.session_state.get("delta_preview_lote")
            # Invalida preview se a seleção mudou desde o cálculo
            if _prev_lote and st.session_state.get("delta_lote_ids") != sorted(ligas_lote_sel):
                _prev_lote = None
                st.session_state.pop("delta_preview_lote", None)

            if _prev_lote:
                _n_tot_l = _prev_lote["n_novos_total"]
                _custo_l = _prev_lote["custo_estimado_creditos"]
                _ok_l    = _custo_l <= saldo - SALDO_MINIMO_EMERGENCIA

                if _n_tot_l == 0:
                    st.success(
                        f"✅ **Cache 100% atualizado** para as {len(ligas_lote_sel)} liga(s). "
                        "Pode calibrar sem gastar créditos de xG."
                    )
                elif _ok_l:
                    st.warning(
                        f"📊 **{_n_tot_l} fixtures novos** · "
                        f"Custo xG estimado: **{_custo_l} créditos** · Saldo disponível: {saldo}"
                    )
                else:
                    st.error(
                        f"🔴 Saldo insuficiente: precisam de {_custo_l + SALDO_MINIMO_EMERGENCIA} "
                        f"({_custo_l} xG + {SALDO_MINIMO_EMERGENCIA} buffer), saldo={saldo}. "
                        "Reduza o lote ou aguarde renovação."
                    )

                # Tabela por liga
                _rows_lote = []
                for _li in _prev_lote["ligas"]:
                    _nn = _li["n_novos_liga"]
                    _rows_lote.append({
                        "Liga":           _li["nome"],
                        "Novos fixtures": _nn,
                        "Créditos xG":    _nn * CUSTO_ESTIMADO_XG_FIXTURE,
                        "Status":         "✅" if _nn == 0 else ("🔴 bootstrap" if _nn > 150 else "🟡"),
                    })
                with st.expander("📋 Detalhes por liga"):
                    st.dataframe(_rows_lote, use_container_width=True, hide_index=True)

                _btn_lote_lbl = (
                    f"🚀 Calibrar lote ({len(ligas_lote_sel)} liga(s) — 0 créditos xG)"
                    if _n_tot_l == 0
                    else f"✅ Confirmar Download ({_custo_l} créditos) e Calibrar {len(ligas_lote_sel)} liga(s)"
                )
                if st.button(
                    _btn_lote_lbl,
                    type="primary",
                    use_container_width=True,
                    key="btn_confirma_lote",
                    disabled=(not _ok_l and _n_tot_l > 0),
                    help=(
                        "Executa Delta Fetch e recalibra via MLE para cada liga em sequência."
                        if _ok_l or _n_tot_l == 0
                        else f"Saldo insuficiente: {saldo} < {_custo_l + SALDO_MINIMO_EMERGENCIA}."
                    ),
                ):
                    st.session_state["delta_confirmado_lote"] = list(ligas_lote_sel)
                    st.session_state.pop("delta_preview_lote", None)
                    st.rerun()

        # Execução autônoma do lote após confirmação
        if st.session_state.get("delta_confirmado_lote"):
            _ligas_batch  = st.session_state.pop("delta_confirmado_lote")
            _prog_lote    = st.progress(0)
            _stat_lote    = st.empty()
            _erros_lote   = []
            _timeouts_lote = []
            _total_lote   = len(_ligas_batch)

            for _i_l, _lid_l in enumerate(_ligas_batch):
                _nome_l = LIGAS_SUPORTADAS.get(_lid_l, f"Liga {_lid_l}")
                _stat_lote.info(f"[{_i_l+1}/{_total_lote}] Calibrando **{_nome_l}**…")
                try:
                    dm.obter_params_liga(_lid_l, season, forcar_recalibracao=True)
                except CreditosInsuficientesError as e:
                    _stat_lote.error(f"🔴 Saldo insuficiente. Parando em **{_nome_l}**: {e}")
                    break
                except TimeoutError:
                    _timeouts_lote.append(_nome_l)
                except Exception as e:
                    _erros_lote.append(f"**{_nome_l}**: {e}")
                _prog_lote.progress((_i_l + 1) / _total_lote)

            _stat_lote.empty()
            if _timeouts_lote:
                st.warning(
                    f"⏱️ {len(_timeouts_lote)} liga(s) com timeout (MLE > {TIMEOUT_CALIBRACAO_SEGUNDOS}s):\n"
                    + "\n".join(f"• {n}" for n in _timeouts_lote)
                )
            if _erros_lote:
                st.warning("⚠️ Ligas com falha:\n" + "\n".join(f"• {e}" for e in _erros_lote))
            if not _timeouts_lote and not _erros_lote:
                st.success(f"✅ {_total_lote} liga(s) calibrada(s) com sucesso!")

            st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
            st.rerun()

    # ── Calibradores Isotônicos ────────────────────────────────────────
    st.divider()
    st.markdown("#### 📐 Calibradores Isotônicos")
    st.caption(
        "Corrige viés sistemático do Dixon-Coles por mercado usando o histórico local. "
        "**Pré-requisito:** bootstrap do Delta Fetch concluído (≥30 fixtures por liga). "
        "Após treinar, cada previsão passa pela correção isotônica automaticamente."
    )

    rows_cal_status = []
    for _lid, _nome in LIGAS_SUPORTADAS.items():
        _pd = banco.params_ligas.get(str(_lid), {})
        _n_cal = len(_pd.get("calibradores", {}))
        _n_max = len(MERCADOS_PRODUCAO)
        if not _pd:
            _st_cal = "❌ MLE ausente"
        elif _n_cal == _n_max:
            _st_cal = f"🟢 {_n_cal}/{_n_max} mercados"
        elif _n_cal > 0:
            _st_cal = f"🟡 {_n_cal}/{_n_max} mercados"
        else:
            _st_cal = "⚪ Params OK — sem calibradores"
        rows_cal_status.append({"Liga": f"{_nome} (ID {_lid})", "Calibradores": _st_cal})
    st.dataframe(rows_cal_status, use_container_width=True, hide_index=True)

    try:
        from treinar_calibradores import treinar_calibradores as _treinar_calibradores_fn
        _tc_ok = True
    except ImportError:
        _tc_ok = False

    if not _tc_ok:
        st.warning("⚠️ `treinar_calibradores.py` não encontrado na pasta do projeto.")
    else:
        col_tc1, col_tc2 = st.columns([4, 1])
        with col_tc1:
            st.caption("O treinamento leva ~5s por liga. Todas com ≥ mínimo de amostras são processadas.")
        with col_tc2:
            tc_min = st.number_input("Min. amostras", value=30, min_value=10, step=5, key="tc_min")

        if st.button("📐 Treinar Calibradores (todas as ligas)", type="primary", use_container_width=True):
            with st.spinner("Treinando calibradores isotônicos..."):
                try:
                    _rel_cal = _treinar_calibradores_fn(verbose=False, n_min_amostras=int(tc_min))
                    st.session_state["relatorio_calibradores"] = _rel_cal
                    st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
                    banco = st.session_state["banco"]
                except FileNotFoundError as _e:
                    st.error(f"Arquivo não encontrado: {_e}")
                    _rel_cal = None
                except ValueError as _e:
                    st.error(f"Pré-requisito ausente: {_e}")
                    _rel_cal = None
                except Exception as _e:
                    st.error(f"Erro ao treinar calibradores: {_e}")
                    _rel_cal = None
            if _rel_cal is not None:
                st.rerun()

        _rel_cal = st.session_state.get("relatorio_calibradores")
        if _rel_cal:
            st.success(f"✅ Calibradores treinados para {len(_rel_cal)} liga(s).")
            _rows_rel = []
            for _ls, _mkts in sorted(_rel_cal.items(), key=lambda x: int(x[0])):
                _nome_r = LIGAS_SUPORTADAS.get(int(_ls), f"Liga {_ls}")
                for _mkt, _info in sorted(_mkts.items()):
                    _v = _info["vies_pp"]
                    _icon = "🟢" if abs(_v) < 2 else ("🟡" if abs(_v) < 5 else "🔴")
                    _rows_rel.append({
                        "Liga":         _nome_r,
                        "Mercado":      _mkt,
                        "N fixtures":   _info["n"],
                        "Real %":       f"{_info['freq_real']:.1f}%",
                        "Bruta %":      f"{_info['prob_media_bruta']:.1f}%",
                        "Viés (pp)":    f"{_icon} {'+' if _v >= 0 else ''}{_v:.1f}",
                        "Corr. ±(pp)":  f"±{_info['correcao_media_pp']:.1f}",
                    })
            with st.expander("📊 Relatório detalhado por liga/mercado", expanded=True):
                st.dataframe(_rows_rel, use_container_width=True, hide_index=True)


# =========================================================================
# 6.2 ABA ANÁLISE DIÁRIA
# =========================================================================

with tab_analise:

    cache_dia = banco.datas.get(data_str, {})
    agenda    = cache_dia.get("agenda", [])

    # O st.date_input é o mestre da consulta: cada data tem sua própria entrada no
    # @st.cache_data (chave = data_str). O botão abaixo é a única porta de entrada —
    # sem auto-load silencioso para evitar gastos de crédito ao navegar entre datas.
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        _tem_cache = bool(agenda)
        _btn_label = (
            f"🔄 Recarregar Agenda ({data_str})" if _tem_cache
            else f"📅 1. Carregar Agenda ({data_str})"
        )
        _btn_help = (
            "Agenda já em memória. 0 créditos se cache ativo, 1 crédito se expirado."
            if _tem_cache
            else f"Busca jogos do dia na API. Custo: {CUSTO_ESTIMADO_FIXTURES_DIA} crédito."
        )
        if st.button(_btn_label, use_container_width=True, help=_btn_help):
            try:
                with st.spinner("Buscando agenda..."):
                    # Usa cache global de 24h: 2ª chamada (qualquer device) retorna instantâneo
                    agenda = _agenda_do_dia_cached(dm, data_str)
                banco.datas.setdefault(data_str, {})
                banco.datas[data_str]["agenda"]    = agenda
                banco.datas[data_str].setdefault("odds", {})
                banco.datas[data_str].setdefault("previsoes", {})
                dm.salvar_banco(banco)
                cache_dia = banco.datas[data_str]
                st.success(f"Agenda carregada: {len(agenda)} jogos.")
                st.rerun()
            except CreditosInsuficientesError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Erro: {e}")

    if not agenda:
        st.info(f"Clique em 'Carregar Agenda' para carregar os jogos de {data_str}.")
        st.stop()

    # ── Separar calibrados / descartados ────────────────────────────
    calibrados, sem_cal = filtrar_jogos_calibrados(agenda, banco.params_ligas)

    st.markdown(f"### {len(calibrados)} jogos analisáveis (de {len(agenda)} na agenda)")

    if sem_cal:
        # Agrupa por liga
        ligas_desc: dict[tuple, list] = {}
        for j in sem_cal:
            l = j.get("league", {})
            key = (l.get("id", 0), l.get("name", "?"), l.get("country", "?"))
            ligas_desc.setdefault(key, []).append(j)

        with st.expander(f"⚠️ {len(sem_cal)} jogos descartados (ligas não calibradas) — clique para ver"):
            for (l_id, l_nome, l_pais), jogos in sorted(ligas_desc.items(), key=lambda x: -len(x[1])):
                col_desc1, col_desc2 = st.columns([3, 1])
                col_desc1.write(f"**{l_nome}** ({l_pais}, ID {l_id}) — {len(jogos)} jogo(s)")
                # Botão de fallback: calibrar essa liga avulsa na hora
                if col_desc2.button(
                    "⚡ Calibrar agora",
                    key=f"fallback_{l_id}",
                    help=f"Busca o histórico dessa liga na API e calibra (máx {TIMEOUT_CALIBRACAO_SEGUNDOS}s)."
                ):
                    try:
                        with st.spinner(f"Calibrando {l_nome} (ID {l_id})..."):
                            dm.calibrar_liga_avulsa(l_id, season)
                        st.success(f"{l_nome} calibrada! Recarregando...")
                        st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
                        st.rerun()
                    except TimeoutError as e:
                        st.error(f"⏱️ Timeout: {e}")
                    except Exception as e:
                        st.error(f"Não foi possível calibrar {l_nome}: {e}")

    if not calibrados:
        st.warning("Nenhuma liga calibrada cobre os jogos do dia. Vá para 'Calibração'.")
        st.stop()

    # ── Buscar odds ──────────────────────────────────────────────────
    odds_cache    = cache_dia.get("odds", {})
    sem_odds      = [j for j in calibrados if str(j["fixture"]["id"]) not in odds_cache]

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        custo_odds = len(sem_odds) * CUSTO_ESTIMADO_ODDS_JOGO
        if st.button(
            f"💰 2. Buscar odds dos {len(sem_odds)} pendentes",
            type="primary", use_container_width=True,
            disabled=len(sem_odds) == 0,
            help=f"Custo: ~{custo_odds} créditos"
        ):
            progress = st.progress(0)
            for i, j in enumerate(sem_odds):
                f_id = str(j["fixture"]["id"])
                try:
                    odds_cache[f_id] = dm.buscar_odds_jogo(int(f_id))
                except CreditosInsuficientesError as e:
                    st.error(f"Parando: {e}")
                    break
                except Exception as e:
                    st.warning(f"Falha em {f_id}: {e}")
                progress.progress((i + 1) / max(1, len(sem_odds)))
            banco.datas[data_str]["odds"] = odds_cache
            dm.salvar_banco(banco)
            st.rerun()
    with col_b2:
        if st.button("🧹 Limpar odds do dia", use_container_width=True):
            banco.datas[data_str]["odds"]     = {}
            banco.datas[data_str]["previsoes"] = {}
            dm.salvar_banco(banco)
            st.rerun()

    st.divider()

    # ── Pré-calcular previsões (0 créditos) ─────────────────────────
    jogos_com_odds = [j for j in calibrados if str(j["fixture"]["id"]) in odds_cache]
    previsoes      = cache_dia.get("previsoes", {})

    for j in jogos_com_odds:
        f_id   = str(j["fixture"]["id"])
        l_id   = j["league"]["id"]
        params = ParametrosLiga.from_dict(banco.params_ligas[str(l_id)])
        h_id   = j["teams"]["home"]["id"]
        a_id   = j["teams"]["away"]["id"]
        if f_id not in previsoes:
            prev = prever_jogo(params, h_id, a_id, aplicar_shrink=True, cobertura_minima=10)
            previsoes[f_id] = {k: prev.get(k) for k in
                               ("lambda", "mu", "xg_total", "mercados", "flags",
                                "cobertura_ok", "erro")}
    banco.datas[data_str]["previsoes"] = previsoes
    dm.salvar_banco(banco)

    # =========================================================================
    # RANKING DE QUALIDADE (sem número fixo)
    # =========================================================================

    # 1. Coleta TODOS os mercados aprovados de TODOS os jogos
    candidatos = []
    MERCADOS_VARREDURA = [
        "OVER_15", "OVER_25", "OVER_35",
        "UNDER_15", "UNDER_25", "UNDER_35",
        "BTTS_YES", "BTTS_NO",
    ]
    for j in jogos_com_odds:
        f_id   = str(j["fixture"]["id"])
        prev   = previsoes[f_id]
        if prev.get("erro"):
            continue
        odds_j       = odds_cache[f_id]
        cobertura_ok = prev.get("cobertura_ok", False)
        jogo_nome    = f"{j['teams']['home']['name']} v {j['teams']['away']['name']}"
        liga_nome_j  = j["league"]["name"]

        for mercado in MERCADOS_VARREDURA:
            prob_modelo = prev["mercados"].get(mercado, 0)
            odd_val     = odds_j.get(mercado, 0)
            if odd_val <= 1.0 or odd_val < odd_min_save:
                continue
            comp  = comparar_com_mercado(prob_modelo, odd_val,
                                         MARGEM_BOOKMAKER_DEFAULT, limite_div)
            stake = calcular_stake_final(comp.get("kelly_fracao", 0), banca_atual,
                                         piso_kelly, teto_pct)
            # Filtro de gatilho por mercado: EV_MIN + PROB_MIN + delta > 0 (Dossiê v8 Seção 5.1)
            if not (filtrar_gatilho(mercado, comp["ev_pct"], prob_modelo,
                                    comp["divergencia_pp"], odd_val)
                    and stake > 0 and not comp["anomalia"]):
                continue

            score = calcular_score_qualidade(
                ev_pct        = comp["ev_pct"],
                divergencia_pp= comp["divergencia_pp"],
                prob_modelo   = prob_modelo,
                kelly_fracao  = comp.get("kelly_fracao", 0),
                odd           = odd_val,
                cobertura_ok  = cobertura_ok,
            )
            if score < SCORE_MINIMO_RANKING:
                continue

            candidatos.append({
                "fixture_id":  f_id,
                "jogo":        jogo_nome,
                "liga":        liga_nome_j,
                "mercado":     mercado,
                "odd":         odd_val,
                "prob_modelo": prob_modelo,
                "prob_mercado":comp["prob_mercado_pct"],
                "ev":          comp["ev_pct"],
                "divergencia": comp["divergencia_pp"],
                "kelly":       comp.get("kelly_fracao", 0),
                "stake":       stake,
                "score":       score,
                "cobertura_ok":cobertura_ok,
            })

    # 2. Deduplicação por jogo: mantém apenas o mercado de maior score por fixture
    #    → elimina clustering (Under 0.5 / 1.5 / 2.5 do mesmo jogo competem entre si)
    melhor_por_jogo: dict[str, dict] = {}
    for c in candidatos:
        fid = c["fixture_id"]
        if fid not in melhor_por_jogo or c["score"] > melhor_por_jogo[fid]["score"]:
            melhor_por_jogo[fid] = c

    # 3. Ranking final: score desc
    ranking = sorted(melhor_por_jogo.values(), key=lambda x: x["score"], reverse=True)

    # ── Exibe ranking ────────────────────────────────────────────────
    n_total_aprovados = len(candidatos)  # antes da dedup, para info

    if ranking:
        st.markdown(f"### 🏆 Ranking de Qualidade do Dia ({len(ranking)} entrada{'s' if len(ranking) > 1 else ''})")
        st.caption(
            f"Score ≥ {SCORE_MINIMO_RANKING} · 1 mercado/jogo (melhor score) · "
            f"EV mínimo por mercado (UNDER_25 ≥ {EV_MIN_POR_MERCADO['UNDER_25']:.0f}%) · "
            f"{n_total_aprovados} candidatos antes da filtragem"
        )

        for i, p in enumerate(ranking, 1):
            score     = p["score"]
            # Cor por nível de score
            if score >= 70:
                cor, badge = "#28a745", "🟢 Alta"
            elif score >= 50:
                cor, badge = "#17a2b8", "🔵 Média"
            else:
                cor, badge = "#ffc107", "🟡 Marginal"

            # Barra de score visual (█ preenchidos proporcionalmente)
            barras  = int(score / 10)
            bar_str = "█" * barras + "░" * (10 - barras)

            cob_icon = "✅" if p["cobertura_ok"] else "⚠️ dados parciais"

            st.markdown(
                f"""<div style='border-left:4px solid {cor};padding:10px 14px;
                              margin-bottom:10px;background:#0e1117;border-radius:4px;'>
                  <div style='display:flex;justify-content:space-between;
                              font-size:11px;color:#888;'>
                    <span>#{i} · {p['liga']} · {cob_icon}</span>
                    <span style='color:{cor};font-weight:bold;'>{badge} &nbsp;
                      <span style='font-family:monospace;letter-spacing:1px;'>{bar_str}</span>
                      &nbsp;{score:.0f}/100
                    </span>
                  </div>
                  <div style='font-size:17px;font-weight:bold;color:white;margin:5px 0 3px;'>
                    {p['jogo']}
                  </div>
                  <div style='font-size:13px;color:#ccc;'>
                    <b>{p['mercado']}</b> &nbsp;·&nbsp;
                    Odd <b>{p['odd']:.2f}</b> &nbsp;·&nbsp;
                    Modelo <b>{p['prob_modelo']:.1f}%</b> vs Mercado {p['prob_mercado']:.1f}%
                    &nbsp;·&nbsp; Δ <b>{p['divergencia']:+.1f}pp</b>
                  </div>
                  <div style='font-size:12px;color:#aaa;margin-top:2px;'>
                    EV <span style='color:{cor};font-weight:bold;'>{p['ev']:+.1f}%</span>
                    &nbsp;·&nbsp; Kelly {p['kelly']*100:.1f}%
                    &nbsp;·&nbsp; 💵 Stake: <b>R$ {p['stake']:.2f}</b>
                  </div>
                </div>""",
                unsafe_allow_html=True,
            )

        # ── Consultora Gemini ────────────────────────────────────────
        st.markdown("#### 🤖 Consultora IA (Gemini)")

        # Resposta do dia: verifica sessão → banco.datas → cache global
        _gemini_salvo = cache_dia.get("gemini_resposta", {})
        _gemini_texto = (
            st.session_state.get("gemini_resposta")
            or (_gemini_salvo.get("texto") if isinstance(_gemini_salvo, dict) else None)
        )

        st.caption(
            f"Analisa **todos os {len(candidatos)} candidatos** com EV aprovado "
            f"({len(ranking)} jogo(s) únicos). "
            + ("✅ Análise do dia já disponível — clique para expandir."
               if _gemini_texto else
               "Resposta cacheada 24h — não reprocessa se já analisou hoje.")
        )
        usar_gemini = st.toggle("Ativar Consultora Gemini", value=bool(_gemini_texto))
        if usar_gemini:
            _col_g1, _col_g2 = st.columns([4, 1])
            if _col_g1.button("📡 Analisar com Gemini", type="primary",
                               help="Retorna do cache se já foi chamado hoje com os mesmos candidatos."):
                with st.spinner("Consultando Gemini (pode retornar do cache instantaneamente)..."):
                    _gemini_texto = _gemini_do_dia_cached(data_str, candidatos)
                # Persiste na sessão e no banco.datas
                st.session_state["gemini_resposta"] = _gemini_texto
                banco.datas[data_str]["gemini_resposta"] = {
                    "texto":       _gemini_texto,
                    "gerado_em":   dt.datetime.now().isoformat(),
                    "n_candidatos": len(candidatos),
                }
                dm.salvar_banco(banco)
            if _gemini_texto and _col_g2.button("🔄 Novo", help="Força nova chamada ignorando cache."):
                _gemini_do_dia_cached.clear()
                st.session_state.pop("gemini_resposta", None)
                banco.datas[data_str].pop("gemini_resposta", None)
                dm.salvar_banco(banco)
                st.rerun()
            if _gemini_texto:
                if isinstance(_gemini_salvo, dict) and _gemini_salvo.get("gerado_em"):
                    st.caption(
                        f"🕐 Análise gerada em: {_gemini_salvo['gerado_em'][:16].replace('T', ' ')} "
                        f"| {_gemini_salvo.get('n_candidatos', '?')} candidatos"
                    )
                st.markdown("---")
                st.markdown(_gemini_texto)

        st.divider()

    elif jogos_com_odds:
        st.info(
            f"📭 Nenhum pick atingiu o score mínimo de {SCORE_MINIMO_RANKING} hoje. "
            f"O motor encontrou {n_total_aprovados} candidatos com EV positivo mas nenhum com qualidade suficiente."
        )
        st.divider()

    # =========================================================================
    # CARDS INDIVIDUAIS
    # =========================================================================

    st.markdown(f"#### {len(jogos_com_odds)} jogos prontos para análise")

    def render_mercado(col, label, mercado, prob_modelo_pct, odd_mercado,
                       banca, piso, teto_pct, lim_div):
        if odd_mercado <= 1.0:
            col.markdown(f"**{label}**\n\n_(sem odd)_")
            return None
        comp  = comparar_com_mercado(prob_modelo_pct, odd_mercado,
                                     MARGEM_BOOKMAKER_DEFAULT, lim_div)
        stake = calcular_stake_final(comp.get("kelly_fracao", 0), banca, piso, teto_pct)

        aprovado = filtrar_gatilho(mercado, comp["ev_pct"], prob_modelo_pct,
                                   comp["divergencia_pp"], odd_mercado)
        if comp["anomalia"]:
            badge, cor = "🚨 ANOMALIA", "#dc3545"
        elif aprovado and stake > 0:
            badge, cor = "✅ APROVADO", "#28a745"
        elif mercado not in MERCADOS_PRODUCAO:
            badge, cor = "📊 referência", "#6c757d"
        elif comp["ev_pct"] > 0:
            badge, cor = "🟡 marginal", "#ffc107"
        else:
            badge, cor = "—", "#6c757d"

        col.markdown(
            f"""<div style='border-left:4px solid {cor};padding:6px 10px;
                           margin-bottom:6px;background:#0e1117;'>
              <div style='font-size:11px;color:#aaa;font-weight:bold;'>
                {label}<span style='float:right;color:{cor};'>{badge}</span>
              </div>
              <div style='font-size:14px;color:#fff;margin-top:2px;'>
                Modelo:<b>{prob_modelo_pct:.1f}%</b> | Mercado:{comp['prob_mercado_pct']:.1f}% |
                Δ:{comp['divergencia_pp']:+.1f}pp
              </div>
              <div style='font-size:13px;color:#ccc;'>
                Odd:<b>{odd_mercado:.2f}</b> | EV:{comp['ev_pct']:+.1f}% |
                Kelly:{comp.get('kelly_fracao', 0)*100:.1f}%
              </div>
              <div style='font-size:12px;color:#17a2b8;margin-top:2px;'>
                💵 {'R$ ' + str(stake) if stake > 0 else 'DESCARTAR'}
              </div>
            </div>""",
            unsafe_allow_html=True,
        )
        return comp

    for j in jogos_com_odds:
        f_id   = str(j["fixture"]["id"])
        prev   = previsoes[f_id]
        odds_j = odds_cache[f_id]

        if prev.get("erro"):
            st.error(
                f"{j['teams']['home']['name']} vs {j['teams']['away']['name']}: {prev['erro']}"
            )
            continue

        hora      = j["fixture"]["date"][11:16]
        liga_nome = j["league"]["name"]
        flags_str = " | ".join(prev["flags"]) if prev.get("flags") else "—"
        cobertura = "✅" if prev.get("cobertura_ok") else "⚠️ dados insuficientes (usando média)"

        with st.container():
            st.markdown(
                f"""<div style='background:#0e1117;padding:10px;border-radius:6px;border:1px solid #333;'>
                  <div style='display:flex;justify-content:space-between;color:#888;font-size:11px;'>
                    <span>🕒 {hora} · {liga_nome}</span>
                    <span>xG total: <b>{prev['xg_total']:.2f}</b> · {cobertura} · Flags: {flags_str}</span>
                  </div>
                  <div style='font-size:18px;font-weight:bold;color:white;margin:6px 0;'>
                    {j['teams']['home']['name']} <span style='color:#666;font-size:13px;'>vs</span>
                    {j['teams']['away']['name']}
                  </div>
                </div>""",
                unsafe_allow_html=True,
            )

            sub = st.tabs(["🔢 Gols", "🤝 BTTS", "🎯 Placar Exato", "💾 Salvar Pick"])

            with sub[0]:
                cols_o = st.columns(5)
                cols_u = st.columns(5)
                for col, l in zip(cols_o, ["05", "15", "25", "35", "45"]):
                    mk = f"OVER_{l}"
                    render_mercado(col, f"Over {l[0]}.{l[1]}", mk,
                                   prev["mercados"][mk], odds_j.get(mk, 0),
                                   banca_atual, piso_kelly, teto_pct, limite_div)
                for col, l in zip(cols_u, ["05", "15", "25", "35", "45"]):
                    mk = f"UNDER_{l}"
                    render_mercado(col, f"Under {l[0]}.{l[1]}", mk,
                                   prev["mercados"][mk], odds_j.get(mk, 0),
                                   banca_atual, piso_kelly, teto_pct, limite_div)

            with sub[1]:
                cols = st.columns(2)
                for col, key, label in zip(cols, ["BTTS_YES", "BTTS_NO"], ["Ambas marcam", "Não ambas"]):
                    render_mercado(col, label, key, prev["mercados"][key], odds_j.get(key, 0),
                                   banca_atual, piso_kelly, teto_pct, limite_div)

            with sub[2]:
                pe = sorted(
                    [(k, v) for k, v in prev["mercados"].items() if k.startswith("PE_")],
                    key=lambda x: x[1], reverse=True
                )[:8]
                cols = st.columns(4)
                for i, (k, v) in enumerate(pe):
                    cols[i % 4].metric(k.replace("PE_", ""), f"{v:.1f}%")

            with sub[3]:
                todos_mk = [
                    "OVER_15", "OVER_25", "OVER_35",
                    "UNDER_15", "UNDER_25", "UNDER_35",
                    "BTTS_YES", "BTTS_NO",
                ]
                mk_sel    = st.selectbox("Mercado", todos_mk, key=f"sel_{f_id}")
                odd_atual = odds_j.get(mk_sel, 0)
                prob_mod  = prev["mercados"][mk_sel]
                comp      = comparar_com_mercado(prob_mod, odd_atual, MARGEM_BOOKMAKER_DEFAULT, limite_div)
                stake_sug = calcular_stake_final(comp.get("kelly_fracao", 0), banca_atual, piso_kelly, teto_pct)

                c1, c2, c3 = st.columns(3)
                c1.metric("Odd", f"{odd_atual:.2f}")
                c2.metric("EV", f"{comp['ev_pct']:+.1f}%")
                c3.metric("Stake sugerida", f"R$ {stake_sug:.2f}" if stake_sug > 0 else "DESCARTAR")

                stake_input = st.number_input("Stake final (R$)", value=stake_sug,
                                              step=0.5, min_value=0.0, key=f"stk_{f_id}")
                bloqueado   = odd_atual < odd_min_save or comp["anomalia"] or stake_input <= 0

                if bloqueado:
                    motivos = []
                    if odd_atual < odd_min_save:   motivos.append(f"odd {odd_atual:.2f} < {odd_min_save}")
                    if comp["anomalia"]:            motivos.append(f"anomalia (Δ {comp['divergencia_pp']:+.1f}pp)")
                    if stake_input <= 0:            motivos.append("stake zero")
                    st.warning("⚠️ Save bloqueado: " + " | ".join(motivos))

                if st.button("💾 Salvar pick", disabled=bloqueado,
                             key=f"save_{f_id}", type="primary"):
                    banco.picks.append({
                        "data":         data_str,
                        "jogo":         f"{j['teams']['home']['name']} v {j['teams']['away']['name']}",
                        "liga_id":      j["league"]["id"],
                        "fixture_id":   f_id,
                        "mercado":      mk_sel,
                        "odd":          odd_atual,
                        "prob_modelo":  round(prob_mod, 2),
                        "prob_mercado": round(comp["prob_mercado_pct"], 2),
                        "divergencia_pp": round(comp["divergencia_pp"], 2),
                        "ev":           round(comp["ev_pct"], 2),
                        "kelly_frac":   round(comp.get("kelly_fracao", 0), 4),
                        "stake":        stake_input,
                        "status":       "Pendente",
                        "salvo_em":     dt.datetime.now().isoformat(),
                    })
                    dm.salvar_banco(banco)
                    st.success("Pick salva! ✅")
                    st.rerun()


# =========================================================================
# 6.3 ABA TRACKER
# =========================================================================

with tab_tracker:
    st.markdown("### 📋 Diário de Bordo")

    if not banco.picks:
        st.info("Nenhuma pick salva ainda.")
    else:
        n_green    = sum(1 for p in banco.picks if p.get("status") == "Green")
        n_red      = sum(1 for p in banco.picks if p.get("status") == "Red")
        n_pend     = sum(1 for p in banco.picks if p.get("status") == "Pendente")
        n_resolv   = n_green + n_red
        taxa       = (n_green / n_resolv * 100) if n_resolv else 0

        cs = st.columns(6)
        cs[0].metric("Total", len(banco.picks))
        cs[1].metric("✅ Green", n_green)
        cs[2].metric("❌ Red", n_red)
        cs[3].metric("⏳ Pendente", n_pend)
        cs[4].metric("Taxa acerto", f"{taxa:.1f}%")
        cs[5].metric("ROI apostas", f"{roi_pct:+.1f}%")

        st.caption(f"Banca inicial: R$ {banco.banca_inicial:.2f} | "
                   f"P/L apostas: R$ {lucro_picks:+.2f} | "
                   f"Depósitos/Retiradas: R$ {total_dep:+.2f} | "
                   f"Banca atual: R$ {banca_atual:.2f}")

        st.divider()

        for i, p in enumerate(reversed(banco.picks)):
            real_idx = len(banco.picks) - 1 - i
            status   = p.get("status", "Pendente")
            icone    = {"Pendente": "⏳", "Green": "✅", "Red": "❌",
                        "Devolvida": "➖", "Anulada": "➖"}.get(status, "❓")

            with st.expander(
                f"{icone} {p.get('data','?')} | {p.get('jogo','?')} | "
                f"{p.get('mercado','?')} | Odd {p.get('odd','-')}"
            ):
                c1, c2, c3, c4 = st.columns(4)
                c1.write(f"**Stake:** R$ {p.get('stake', 0):.2f}")
                c2.write(f"**Odd:** {p.get('odd', '-')}")
                c3.write(f"**Prob modelo:** {p.get('prob_modelo', '-')}%")
                c4.write(f"**EV:** {p.get('ev', '-')}%")
                if "divergencia_pp" in p:
                    st.caption(f"Δ vs mercado: {p['divergencia_pp']:+.1f}pp | "
                               f"Kelly: {p.get('kelly_frac', 0)*100:.1f}%")

                if status == "Pendente":
                    ca, cb, cc = st.columns(3)
                    if ca.button("✅ Green",    key=f"green_{real_idx}", type="primary"):
                        banco.picks[real_idx]["status"] = "Green"
                        dm.salvar_banco(banco); st.rerun()
                    if cb.button("❌ Red",      key=f"red_{real_idx}"):
                        banco.picks[real_idx]["status"] = "Red"
                        dm.salvar_banco(banco); st.rerun()
                    if cc.button("➖ Devolvida", key=f"void_{real_idx}"):
                        banco.picks[real_idx]["status"] = "Devolvida"
                        dm.salvar_banco(banco); st.rerun()
                else:
                    if st.button("↩️ Desfazer", key=f"undo_{real_idx}"):
                        banco.picks[real_idx]["status"] = "Pendente"
                        dm.salvar_banco(banco); st.rerun()


# =========================================================================
# 6.4 ABA AUDITORIA
# =========================================================================

with tab_auditoria:
    st.markdown("### 🔬 Auditoria do Motor")

    if not banco.params_ligas:
        st.info("Nenhuma liga calibrada ainda.")
    else:
        liga_aud = st.selectbox(
            "Liga", options=list(banco.params_ligas.keys()),
            format_func=lambda x: f"{LIGAS_SUPORTADAS.get(int(x), f'Liga {x}')} (ID {x})",
        )
        params_d = banco.params_ligas[liga_aud]
        params   = ParametrosLiga.from_dict(params_d)

        # Helper para resolver nome do time
        def nome_time(tid: int) -> str:
            t = int(tid)
            # JSON serializa chaves como strings; tenta str primeiro, depois int
            return params.nomes_times.get(str(t), params.nomes_times.get(t, f"ID {t}"))

        # Métricas do motor
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("γ (vantagem casa)",   f"{params.home_advantage:.3f}")
        c2.metric("ρ (Dixon-Coles tau)", f"{params.rho:.3f}")
        c3.metric("Média gols/jogo",     f"{params.media_liga_gols:.2f}")
        c4.metric("Jogos usados",        params.n_jogos_calibracao)

        seasons_str = " + ".join(str(s) for s in params.seasons_incluidas) if params.seasons_incluidas else str(params.season)
        tipo_season = "ano-calendário" if int(liga_aud) in LIGAS_TEMPORADA_ANO_ATUAL else "europeia"
        xg_label = f"xG ✅ blend={PESO_XG_PRODUCAO}" if params.xg_ativo else "xG ❌ gols reais"
        cal_n = len(params.calibradores)
        cal_label = (
            f"📐 {cal_n}/{len(MERCADOS_PRODUCAO)} mkt calibrados"
            if cal_n > 0 else "📐 sem calibradores"
        )
        st.caption(
            f"Calibrada em: {params.calibrado_em[:16]} | "
            f"Temporadas: **{seasons_str}** ({tipo_season}) | "
            f"Log-likelihood: {params.log_likelihood:.2f} | "
            f"{len(params.nomes_times)} nomes resolvidos | {xg_label} | {cal_label}"
        )

        # ── Raio-X da base bruta ─────────────────────────────────────────
        if params.raio_x_times:
            season_atual_str  = params.seasons_incluidas[-1] if params.seasons_incluidas else params.season
            season_hist_str   = params.seasons_incluidas[0]  if len(params.seasons_incluidas) > 1 else "—"
            tem_multi = len(params.seasons_incluidas) > 1

            n_no_modelo    = sum(1 for v in params.raio_x_times.values() if v.get("no_modelo"))
            n_filtrados    = sum(1 for v in params.raio_x_times.values() if not v.get("no_modelo"))
            n_rebaixados   = sum(1 for v in params.raio_x_times.values()
                                 if not v.get("na_temporada_atual") and tem_multi)

            with st.expander(
                f"🔬 Raio-X da Base Bruta — {len(params.raio_x_times)} times detectados · "
                f"{n_no_modelo} no modelo · {n_filtrados} filtrados"
                + (f" · {n_rebaixados} rebaixados/ausentes de {season_atual_str}" if n_rebaixados else ""),
                expanded=False,
            ):
                st.caption(
                    "**Verde** = no modelo | **Amarelo** = passou no MLE mas removido por ser só do histórico "
                    "| **Vermelho** = excluído (poucos jogos ou não está na temporada atual)"
                )

                linhas_raio_x = []
                for tid, rx in sorted(
                    params.raio_x_times.items(),
                    key=lambda x: (not x[1].get("no_modelo"), -x[1].get("n_total", 0)),
                ):
                    nome = params.nomes_times.get(int(tid), f"ID {tid}")
                    no_mod   = rx.get("no_modelo", False)
                    na_atual = rx.get("na_temporada_atual", True)

                    if no_mod:
                        status = "✅ No modelo"
                    elif na_atual:
                        status = "🟡 Poucos jogos (filtrado)"
                    else:
                        status = f"🔴 Rebaixado / não está em {season_atual_str}"

                    linha = {
                        "Time":                         nome,
                        f"Jogos {season_atual_str}":    rx.get("n_atual", 0),
                        "Jogos histórico":              rx.get("n_historico", 0) if tem_multi else "—",
                        "Total":                        rx.get("n_total", 0),
                        "Último jogo":                  rx.get("ultimo_jogo", "?"),
                        "Status":                       status,
                    }
                    if not tem_multi:
                        del linha["Jogos histórico"]
                    linhas_raio_x.append(linha)

                st.dataframe(linhas_raio_x, use_container_width=True, hide_index=True)

                # Alerta sobre times rebaixados encontrados
                rebaixados_lista = [
                    params.nomes_times.get(int(tid), f"ID {tid}")
                    for tid, rx in params.raio_x_times.items()
                    if not rx.get("na_temporada_atual") and tem_multi
                ]
                if rebaixados_lista:
                    st.warning(
                        f"⚠️ **{len(rebaixados_lista)} time(s) do histórico {season_hist_str} "
                        f"excluídos do modelo** (não aparecem em {season_atual_str}):\n" +
                        ", ".join(rebaixados_lista[:15]) +
                        ("..." if len(rebaixados_lista) > 15 else "")
                    )
                else:
                    st.success(f"✅ Todos os times do modelo aparecem em {season_atual_str}.")
        else:
            st.info("💡 Raio-X disponível após recalibrar. Clique em 'Calibrar' para gerar.")

        # ── Calibradores Isotônicos ──────────────────────────────────────────
        if cal_n > 0:
            with st.expander(
                f"📐 Calibradores por mercado ({cal_n}/{len(MERCADOS_PRODUCAO)} ativos)",
                expanded=False,
            ):
                st.caption(
                    "Efeito da calibração isotônica: prob bruta → prob calibrada. "
                    "Valores abaixo de 50% podem subir ou descer dependendo do viés histórico."
                )
                _rows_cal_aud = []
                for _mkt_a in sorted(MERCADOS_PRODUCAO):
                    if _mkt_a in params.calibradores:
                        _cal_obj = params.calibradores[_mkt_a]
                        _rows_cal_aud.append({
                            "Mercado":          _mkt_a,
                            "Amostras":         _cal_obj.n_amostras,
                            "40% -> cal":       f"{_cal_obj.calibrar(40.0):.1f}%",
                            "50% -> cal":       f"{_cal_obj.calibrar(50.0):.1f}%",
                            "60% -> cal":       f"{_cal_obj.calibrar(60.0):.1f}%",
                            "70% -> cal":       f"{_cal_obj.calibrar(70.0):.1f}%",
                        })
                    else:
                        _rows_cal_aud.append({
                            "Mercado":          _mkt_a,
                            "Amostras":         "—",
                            "40% -> cal":       "—",
                            "50% -> cal":       "—",
                            "60% -> cal":       "—",
                            "70% -> cal":       "—",
                        })
                st.dataframe(_rows_cal_aud, use_container_width=True, hide_index=True)
        else:
            st.info(
                "💡 **Calibradores não treinados** para esta liga. "
                "Vá em **Calibração → 📐 Calibradores Isotônicos** e clique em 'Treinar Calibradores'."
            )

        # Ranking de ataques
        times_ord = sorted(params.times.items(), key=lambda x: -x[1]["alpha"])

        st.markdown("#### 🔴 Top 5 ataques (α maior = melhor ofensivo)")
        st.dataframe(
            [{"Time": nome_time(int(k)), "Ataque (α)": round(v["alpha"], 3),
              "Defesa (β)": round(v["beta"], 3), "Jogos": v["n_jogos"]}
             for k, v in times_ord[:5]],
            use_container_width=True, hide_index=True,
        )

        st.markdown("#### 📉 Bottom 5 ataques (piores ofensivos)")
        st.dataframe(
            [{"Time": nome_time(int(k)), "Ataque (α)": round(v["alpha"], 3),
              "Defesa (β)": round(v["beta"], 3), "Jogos": v["n_jogos"]}
             for k, v in times_ord[-5:]],
            use_container_width=True, hide_index=True,
        )

        st.markdown("#### 🛡️ Top 5 defesas (β MENOR = melhor defensivo)")
        times_def = sorted(params.times.items(), key=lambda x: x[1]["beta"])
        st.dataframe(
            [{"Time": nome_time(int(k)), "Ataque (α)": round(v["alpha"], 3),
              "Defesa (β)": round(v["beta"], 3), "Jogos": v["n_jogos"]}
             for k, v in times_def[:5]],
            use_container_width=True, hide_index=True,
        )

        st.markdown("#### 📋 Todos os times calibrados")
        with st.expander(f"Ver todos os {len(params.times)} times"):
            todos = [
                {"Time": nome_time(int(k)), "ID": int(k),
                 "Ataque (α)": round(v["alpha"], 3),
                 "Defesa (β)": round(v["beta"], 3),
                 "Jogos": v["n_jogos"]}
                for k, v in sorted(params.times.items(), key=lambda x: -x[1]["alpha"])
            ]
            st.dataframe(todos, use_container_width=True, hide_index=True)

    st.divider()

    # =========================================================================
    # DIAGNÓSTICO COMPLETO
    # =========================================================================
    st.markdown("#### 🩺 Diagnóstico do Sistema")
    if st.button("🔍 Rodar diagnóstico completo", type="primary"):
        import json as _json
        from pathlib import Path as _Path

        linhas = []

        # ── Seção 1: Arquivo local ─────────────────────────────────────────
        linhas.append("═" * 55)
        linhas.append("SEÇÃO 1 — SAÚDE DO ARQUIVO LOCAL")
        linhas.append("═" * 55)
        arq = _Path("banco_barrios_pro.json")
        if arq.exists():
            size_kb = arq.stat().st_size / 1024
            try:
                with open(arq, "r", encoding="utf-8") as f:
                    _json.load(f)
                linhas.append(f"✅ {arq.name} existe e é JSON válido ({size_kb:.1f} KB)")
            except Exception as e:
                linhas.append(f"❌ {arq.name} existe mas está CORROMPIDO: {e}")
        else:
            linhas.append(f"⚠️  {arq.name} não encontrado — sistema rodando só em memória")

        try:
            saldo_api = dm.saldo_creditos()
            linhas.append(f"✅ Conexão API-Sports OK — saldo: {saldo_api}/7500 créditos")
        except Exception as e:
            linhas.append(f"❌ Conexão API-Sports FALHOU: {e}")

        # ── Seção 2: Completude das ligas calibradas ───────────────────────
        linhas.append("")
        linhas.append("═" * 55)
        linhas.append("SEÇÃO 2 — COMPLETUDE DAS LIGAS")
        linhas.append("═" * 55)
        linhas.append(f"Calibradas: {len(banco.params_ligas)} / {len(LIGAS_SUPORTADAS)} suportadas")
        linhas.append("")

        hoje_dt = dt.datetime.now()
        for chave, pd_raw in banco.params_ligas.items():
            nome_l = LIGAS_SUPORTADAS.get(int(chave), f"Liga {chave}")
            n_times_l = len(pd_raw.get("times", {}))
            n_jogos_l = pd_raw.get("n_jogos_calibracao", 0)
            cal_em_s  = pd_raw.get("calibrado_em", "")[:16]
            seasons_l = pd_raw.get("seasons_incluidas", [pd_raw.get("season", "?")])
            tem_nomes = len(pd_raw.get("nomes_times", {}))

            try:
                dias_l = (hoje_dt - dt.datetime.fromisoformat(pd_raw.get("calibrado_em",""))).days
                fresh  = "✅" if dias_l <= 7 else ("🟡" if dias_l <= 14 else "🔴")
            except Exception:
                dias_l = -1
                fresh  = "⚠️"

            seasons_info = "+".join(str(s) for s in seasons_l)
            nomes_info   = f"{tem_nomes} nomes" if tem_nomes else "sem nomes (recalibrar)"
            linhas.append(
                f"  {fresh} {nome_l}: {n_times_l} times | {n_jogos_l} jogos | "
                f"seasons={seasons_info} | {cal_em_s} ({dias_l}d) | {nomes_info}"
            )

        # ── Seção 3: Integridade dos IDs ───────────────────────────────────
        linhas.append("")
        linhas.append("═" * 55)
        linhas.append("SEÇÃO 3 — INTEGRIDADE DOS IDs DE TIMES")
        linhas.append("═" * 55)

        total_prob = 0
        for chave, pd_raw in banco.params_ligas.items():
            nome_l = LIGAS_SUPORTADAS.get(int(chave), f"Liga {chave}")
            times_raw = pd_raw.get("times", {})
            problemas = []

            ids_vistos: set = set()
            for tid_str, tv in times_raw.items():
                # ID não-inteiro
                try:
                    tid_int = int(tid_str)
                except (ValueError, TypeError):
                    problemas.append(f"ID não-inteiro: '{tid_str}'")
                    continue

                # Duplicata
                if tid_int in ids_vistos:
                    problemas.append(f"ID duplicado: {tid_int}")
                ids_vistos.add(tid_int)

                # α/β fora dos bounds razoáveis
                alpha = tv.get("alpha")
                beta  = tv.get("beta")
                if alpha is None or beta is None:
                    problemas.append(f"α ou β ausente no time {tid_int}")
                elif not (0.05 < float(alpha) < 5.0) or not (0.05 < float(beta) < 5.0):
                    problemas.append(
                        f"α/β fora dos bounds: time {tid_int} "
                        f"(α={alpha:.3f}, β={beta:.3f})"
                    )

            if problemas:
                total_prob += len(problemas)
                linhas.append(f"  ❌ {nome_l}: {len(problemas)} problema(s)")
                for p in problemas[:5]:
                    linhas.append(f"     • {p}")
            else:
                linhas.append(f"  ✅ {nome_l}: {len(times_raw)} times OK")

        if total_prob == 0:
            linhas.append("")
            linhas.append("✅ Nenhum ID corrompido ou duplicado encontrado.")
        else:
            linhas.append(f"\n❌ Total de problemas encontrados: {total_prob}")

        # ── Resumo de picks ────────────────────────────────────────────────
        linhas.append("")
        linhas.append("═" * 55)
        linhas.append("SEÇÃO 4 — PICKS E BANCA")
        linhas.append("═" * 55)
        n_sem_odd  = sum(1 for p in banco.picks if not p.get("odd"))
        n_sem_stat = sum(1 for p in banco.picks if not p.get("status"))
        linhas.append(f"Total de picks: {len(banco.picks)}")
        linhas.append(f"Pendentes: {sum(1 for p in banco.picks if p.get('status')=='Pendente')}")
        linhas.append(f"Picks sem odd registrada: {n_sem_odd}")
        linhas.append(f"Picks sem status: {n_sem_stat}")
        linhas.append(f"Banca inicial: R$ {banco.banca_inicial:.2f}")
        linhas.append(f"Depósitos registrados: {len(banco.depositos)}")
        linhas.append(f"P/L apostas: R$ {lucro_picks:+.2f} | ROI: {roi_pct:+.1f}%")
        linhas.append(f"Banca atual: R$ {banca_atual:.2f}")

        st.code("\n".join(linhas), language=None)
