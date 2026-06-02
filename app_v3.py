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
import json
import pandas as pd

import streamlit as st

from motor import (
    ParametrosLiga,
    prever_jogo,
    comparar_com_mercado,
    calcular_overround_1x2,
    filtrar_gatilho,
    MERCADOS_PRODUCAO,
    EV_MIN_POR_MERCADO,
    GapConfig,
    EvConfig,
    filtrar_gap,
    filtrar_ev_config,
)
from dados import (
    BancoQG,
    DadosManager,
    ARQUIVO_BANCO_LOCAL,
    LIGAS_SUPORTADAS,
    LIGAS_TEMPORADA_ANO_ATUAL,
    LIGAS_COPA_MUNDO,
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

PISO_KELLY_PADRAO        = 1.0
TETO_PCT_BANCA_PADRAO    = 0.10
ODD_MIN_SAVE             = 1.70
LIMITE_DIVERGENCIA_PP    = 20.0
MARGEM_BOOKMAKER_DEFAULT = 1.05

# Filtros P1+P2 — validados por backtest walk-forward (V4, 12 janelas)
# GapConfig: sem gap para Over (D-C biasado já filtra), gap 0.5 para Under
# EvConfig:  EV>=20% para Under elimina falsos positivos estruturais do D-C
GAP_CONFIG_PROD = GapConfig(gap_over_min=0.0, gap_under_min=0.5)
EV_CONFIG_PROD  = EvConfig(ev_min_over=3.0,  ev_min_under=20.0)

# ── Filtros 1X2 / Dupla Chance (H1-HOME-Only, homologados no laboratório V6) ──
# Faixas de EV validadas por análise granular de 2349 picks / 12 janelas walk-forward.
# Isotônico aplicado APENAS em HOME — DRAW/AWAY usam probabilidade pura RAW do D-C.
_1X2_MERCADOS   = {"HOME", "DRAW", "AWAY", "1X", "X2", "12"}

_EV_MIN: dict   = {"HOME": 5.0,  "DRAW": 28.0, "AWAY": 3.0,  "1X": 3.0,  "X2": 3.0,  "12": 3.0}
_EV_MAX: dict   = {"HOME": 50.0, "DRAW": 80.0, "AWAY": 22.0, "1X": 18.0, "X2": 18.0, "12": 18.0}
_PROB_MIN: dict = {"HOME": 45.0, "DRAW": 22.0, "AWAY": 28.0, "1X": 65.0, "X2": 65.0, "12": 65.0}
_ODD_MIN: dict  = {"HOME": 1.80, "DRAW": 2.80, "AWAY": 1.60, "1X": 1.25, "X2": 1.25, "12": 1.25}
# Teto de odd por mercado — controla variância sem cortar EV. DRAW=4.99 elimina bets
# com odd>=5 (backtest: 1/14 wins, -42.5% ROI) onde D-C está fora da faixa de calibração.
_ODD_MAX: dict  = {"HOME": 3.50, "DRAW": 4.99, "AWAY": 5.00, "1X": 2.50, "X2": 2.50, "12": 2.50}

# Ranking de qualidade — sem número fixo
SCORE_MINIMO_RANKING = 60   # limiar de qualidade mínima; 35-59 = marginal, geralmente ruído
# Pesos do score composto (devem somar 1.0)
PESO_EV          = 0.35
PESO_DIVERGENCIA = 0.30
PESO_PROB        = 0.20
PESO_KELLY       = 0.15


# =========================================================================
# 2. INICIALIZAÇÃO DO MANAGER
# =========================================================================
# Cache key usa o GIST_ID real (ou bin_id) para garantir miss automático
# quando as credenciais mudam. Assim nunca fica preso no manager antigo.
_backend_tipo  = (
    "gist" if (st.secrets.get("GITHUB_TOKEN") and st.secrets.get("GIST_ID"))
    else "jsonbin"
)
_cache_gist_id = str(st.secrets.get("GIST_ID", ""))
_cache_bin_id  = str(st.secrets.get("JSONBIN_BIN_ID", ""))
_cache_storage_id = _cache_gist_id or _cache_bin_id  # identifica o storage atual


@st.cache_resource
def get_manager(n_ligas: int, storage_id: str) -> DadosManager:  # noqa: ARG001
    """Cache keyed on nº de ligas + storage ID — miss automático ao trocar secrets."""
    return criar_dados_manager_de_secrets(st.secrets, diretorio_local=".")


try:
    dm = get_manager(len(LIGAS_SUPORTADAS), _cache_storage_id)
except Exception as e:
    st.error(f"Falha ao inicializar manager: {e}")
    st.stop()

if "banco" not in st.session_state:
    st.session_state["banco"] = dm.carregar_banco()

banco: BancoQG = st.session_state["banco"]

# Ligas ativas = LIGAS_SUPORTADAS base + ligas calibradas manualmente
# Leagues calibrated from sem_cal have "nome_liga" saved in params_ligas.
_ligas_custom: dict[int, str] = {
    int(k): v.get("nome_liga", f"Liga {k}")
    for k, v in (banco.params_ligas or {}).items()
    if v.get("nome_liga") and int(k) not in LIGAS_SUPORTADAS
}
_ligas_ativas: dict[int, str] = {**LIGAS_SUPORTADAS, **_ligas_custom}


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


def _estimar_params_adhoc(df_hist: "pd.DataFrame", team_id: int,
                           media_global: float = 1.35) -> dict:
    """
    Estima alpha/beta D-C de um time a partir do histórico bruto (df com colunas
    home_id, away_id, home_goals, away_goals).  Shrinkage s = n/(n+10).
    Retorna dict {"alpha": float, "beta": float, "n_jogos": int}.
    """
    if df_hist is None or df_hist.empty:
        return {"alpha": 1.0, "beta": 1.0, "n_jogos": 0}

    gols_marcados, gols_sofridos = [], []
    for _, r in df_hist.iterrows():
        if r["home_id"] == team_id:
            gols_marcados.append(r["home_goals"])
            gols_sofridos.append(r["away_goals"])
        elif r["away_id"] == team_id:
            gols_marcados.append(r["away_goals"])
            gols_sofridos.append(r["home_goals"])

    n = len(gols_marcados)
    if n == 0:
        return {"alpha": 1.0, "beta": 1.0, "n_jogos": 0}

    s = n / (n + 10)  # shrinkage: com 10 jogos = 50% peso real, 50% prior
    media_marc = sum(gols_marcados) / n
    media_sofr = sum(gols_sofridos) / n
    alpha = s * (media_marc / media_global) + (1 - s) * 1.0
    beta  = s * (media_sofr / media_global) + (1 - s) * 1.0  # nota: beta = força defensiva (quanto adversário sofre vs média)
    return {"alpha": max(0.2, alpha), "beta": max(0.2, beta), "n_jogos": n}


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


def render_mercado(col, label: str, mercado: str, prob_modelo_pct: float,
                   odd_mercado: float, banca: float, piso: float,
                   teto_pct: float, lim_div: float,
                   _extra_producao: frozenset = frozenset()):
    """Renderiza card de mercado no estilo escuro (usado no Sniper e no sem_cal).

    _extra_producao: mercados adicionais tratados como produção (ex: HOME/DRAW/AWAY
    no sem_cal, onde não há calibrador separado do Estrategista).
    """
    if odd_mercado <= 1.0:
        col.markdown(f"**{label}**\n\n_(sem odd)_")
        return None
    comp  = comparar_com_mercado(prob_modelo_pct, odd_mercado,
                                 MARGEM_BOOKMAKER_DEFAULT, lim_div)
    stake = calcular_stake_final(comp.get("kelly_fracao", 0), banca, piso, teto_pct)
    aprovado = filtrar_gatilho(mercado, comp["ev_pct"], prob_modelo_pct,
                               comp["divergencia_pp"], odd_mercado)
    _is_producao = mercado in MERCADOS_PRODUCAO or mercado in _extra_producao
    if comp["anomalia"]:
        badge, cor = "🚨 ANOMALIA", "#dc3545"
    elif aprovado and stake > 0:
        badge, cor = "✅ APROVADO", "#28a745"
    elif not _is_producao:
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


def avaliar_heuristicas(
    mercado: str,
    xg_lam: float,
    xg_mu: float,
    xg_total: float,
    ctx: dict | None = None,
) -> tuple[float, str]:
    """
    Matriz de bônus/penalidade contextual usando λ/μ do D-C e dados do historico_ligas.

    Retorna (ajuste_pts, nota) onde ajuste_pts pode ser positivo (bônus) ou negativo (penalidade).
    ctx esperado (D-C): alpha_h, beta_h, alpha_a, beta_a, n_jogos_h, n_jogos_a, rho, media_liga_gols.
    ctx esperado (forma): h/a_wins/losses/draws_last5, h/a_gf/ga_last5, h/a_n_last5,
                          h/a_win_streak, h/a_sem_marcar, h_cs_streak_casa,
                          h2h_h_wins, h2h_a_wins, h2h_total.

    Regras sem ctx (H1-H6)  : baseadas somente em λ/μ.
    Regras com ctx (HC1-HC10): parâmetros D-C internos (alpha/beta/rho).
    Regras com ctx (HC11-HC17): forma recente + H2H do historico_ligas (0 créditos extras).
    """
    adj = 0.0
    notas: list[str] = []
    ratio = xg_lam / max(xg_mu, 0.01)

    # ── H1: AWAY com mandante muito superior ──────────────────────────
    if mercado == "AWAY":
        if ratio > 3.0:
            adj -= 15.0
            notas.append(f"Mandante com domínio severo (λ/μ={ratio:.1f})")
        elif ratio > 2.2:
            adj -= 8.0
            notas.append(f"Mandante superior (λ/μ={ratio:.1f})")

    # ── H2: BTTS_YES com visitante pouco ofensivo ─────────────────────
    if mercado == "BTTS_YES":
        if xg_mu < 0.70:
            adj -= 12.0
            notas.append(f"Visitante com μ={xg_mu:.2f} — baixo potencial de marcar")
        elif xg_mu < 0.90:
            adj -= 6.0
            notas.append(f"Visitante abaixo da média ofensiva (μ={xg_mu:.2f})")

    # ── H3: BTTS_NO com ambos atacando ───────────────────────────────
    if mercado == "BTTS_NO":
        if xg_lam > 1.55 and xg_mu > 1.05:
            adj -= 10.0
            notas.append(f"Ambos ofensivos (λ={xg_lam:.2f}, μ={xg_mu:.2f}) — BTTS_NO fragilizado")

    # ── H4: DRAW com desequilíbrio extremo ───────────────────────────
    if mercado == "DRAW":
        if ratio > 3.0 or ratio < 0.33:
            adj -= 18.0
            notas.append(f"Desequilíbrio extremo (λ/μ={ratio:.1f}) — empate improvável")
        elif ratio > 2.2 or ratio < 0.45:
            adj -= 9.0
            notas.append(f"Desequilíbrio acentuado (λ/μ={ratio:.1f})")

    # ── H5: UNDER_25 na zona cinzenta ────────────────────────────────
    if mercado == "UNDER_25" and 1.85 <= xg_total < 2.00:
        adj -= 10.0
        notas.append(f"xG total próximo do limiar (λ+μ={xg_total:.2f})")

    # ── H6: OVER_25 com margem estreita ──────────────────────────────
    if mercado == "OVER_25" and 2.50 < xg_total <= 2.68:
        adj -= 6.0
        notas.append(f"xG total marginalmente acima do limiar (λ+μ={xg_total:.2f})")

    # ── H5b: UNDER_35 na zona cinzenta ───────────────────────────────
    if mercado == "UNDER_35" and 3.00 <= xg_total < 3.20:
        adj -= 5.0
        notas.append(f"xG próximo do limiar Under 3.5 (λ+μ={xg_total:.2f}) — incerteza alta")

    # ── H5c: UNDER_15 — regime por xG ────────────────────────────────
    if mercado == "UNDER_15":
        if xg_total < 1.20:
            adj += 10.0
            notas.append(f"xG muito baixo (λ+μ={xg_total:.2f}) — Under 1.5 com alta convicção")
        elif xg_total < 1.30:
            adj += 5.0
            notas.append(f"xG baixo (λ+μ={xg_total:.2f}) — Under 1.5 favorecido")
        elif 1.40 <= xg_total < 1.50:
            adj -= 4.0
            notas.append(f"xG próximo do limiar Under 1.5 (λ+μ={xg_total:.2f})")
        elif xg_total >= 1.50:
            adj -= 8.0
            notas.append(f"xG acima do limiar Under 1.5 (λ+μ={xg_total:.2f}) — risco alto")

    # ── H6b: OVER_35 — regime por xG ─────────────────────────────────
    if mercado == "OVER_35":
        if xg_total > 4.00:
            adj += 8.0
            notas.append(f"xG muito alto (λ+μ={xg_total:.2f}) — Over 3.5 com alta convicção")
        elif xg_total > 3.60:
            adj += 4.0
            notas.append(f"xG favorável para Over 3.5 (λ+μ={xg_total:.2f})")
        elif 3.50 < xg_total <= 3.60:
            adj -= 4.0
            notas.append(f"xG marginalmente acima do limiar Over 3.5 (λ+μ={xg_total:.2f})")

    # ── H6c: OVER_15 — visitante inofensivo penaliza ─────────────────
    if mercado == "OVER_15":
        if xg_mu < 0.50:
            adj -= 8.0
            notas.append(f"Visitante inofensivo (μ={xg_mu:.2f}) — Over 1.5 em risco")
        elif xg_mu < 0.70 and xg_lam < 0.80:
            adj -= 5.0
            notas.append(f"Ambos com baixo potencial ofensivo (λ={xg_lam:.2f}, μ={xg_mu:.2f})")

    # ── Regras com ctx (parâmetros D-C internos) ──────────────────────
    if ctx:
        alpha_h   = float(ctx.get("alpha_h",   1.0))
        beta_h    = float(ctx.get("beta_h",    1.0))
        alpha_a   = float(ctx.get("alpha_a",   1.0))
        beta_a    = float(ctx.get("beta_a",    1.0))
        n_jogos_h = int(ctx.get("n_jogos_h",   0))
        n_jogos_a = int(ctx.get("n_jogos_a",   0))
        rho       = float(ctx.get("rho",       -0.05))
        media     = float(ctx.get("media_liga_gols", 2.5))

        cbi       = min(xg_lam, xg_mu) / max(xg_lam, xg_mu, 0.01)
        force_h   = alpha_h / max(beta_a, 0.01)
        force_a   = alpha_a / max(beta_h, 0.01)
        fortress_h = alpha_h > 1.10 and beta_h < 0.90
        fragil_h   = alpha_h < 0.90 and beta_h > 1.10
        lethal_a   = alpha_a > 1.10 and beta_a < 0.90

        # HC1: HOME ─ Fortaleza do mandante
        if mercado == "HOME":
            if fortress_h:
                adj += 8.0
                notas.append(f"Fortaleza do mandante (αH={alpha_h:.2f}, βH={beta_h:.2f})")
            if force_h > 1.50 and force_a < 0.80:
                adj += 6.0
                notas.append(f"Superioridade estrutural (fH={force_h:.2f} vs fA={force_a:.2f})")
            if n_jogos_h < 12:
                adj -= 8.0
                notas.append(f"Mandante com poucos jogos ({n_jogos_h}j) — incerteza alta")

        # HC2: AWAY ─ Visitante letal
        if mercado == "AWAY":
            if lethal_a and fragil_h:
                adj += 10.0
                notas.append(f"Visitante letal vs mandante frágil (αA={alpha_a:.2f}, βH={beta_h:.2f})")
            elif force_a > force_h * 1.25:
                adj += 5.0
                notas.append(f"Visitante com vantagem estrutural (fA={force_a:.2f})")

        # HC3: DRAW ─ Liga propensa ao empate + equilíbrio
        if mercado == "DRAW":
            if rho < -0.10:
                adj += 7.0
                notas.append(f"Liga propensa ao empate (ρ={rho:.3f})")
            if cbi > 0.80:
                adj += 8.0
                notas.append(f"Jogo equilibrado (CBI={cbi:.2f})")

        # HC30: DRAW ─ Liga goleadora reduz empiricamente a taxa de empate
        # Ligas com média >2.80 gols/j têm abertura ofensiva que torna empates raros.
        # Penalidade complementa H4 (que age no desequilíbrio λ/μ).
        if mercado == "DRAW" and media > 2.80:
            adj -= 6.0
            notas.append(f"Liga goleadora (méd={media:.2f}g/j) — empate estruturalmente menos provável")

        # HC4: BTTS_YES ─ Contexto ofensivo mútuo
        if mercado == "BTTS_YES":
            if alpha_a > 0.95 and beta_h > 1.05:
                adj += 8.0
                notas.append(f"Visitante ataca bem vs defesa fraca do mandante (αA={alpha_a:.2f}, βH={beta_h:.2f})")
            if alpha_h > 0.95 and beta_a > 1.05:
                adj += 5.0
                notas.append(f"Mandante também ataca bem vs fraca defesa visitante (αH={alpha_h:.2f})")

        # HC5: BTTS_NO ─ Contexto defensivo dominante
        if mercado == "BTTS_NO":
            if fortress_h and alpha_a < 0.90:
                adj += 10.0
                notas.append(f"Fortaleza em casa vs visitante sem gol (αA={alpha_a:.2f})")
            if beta_h < 0.85 and beta_a < 0.85:
                adj += 8.0
                notas.append(f"Ambas as defesas sólidas (βH={beta_h:.2f}, βA={beta_a:.2f})")

        # HC6: OVER_25 ─ Liga goleadora vs propensa ao empate
        if mercado == "OVER_25":
            if media > 2.80:
                adj += 6.0
                notas.append(f"Liga goleadora (méd={media:.2f} gols/j)")
            if rho < -0.12:
                adj -= 6.0
                notas.append(f"Liga com tendência ao empate baixo (ρ={rho:.3f}) — penaliza Over")

        # HC7: UNDER_25 ─ Liga defensiva + defesas sólidas
        if mercado == "UNDER_25":
            if rho < -0.10:
                adj += 7.0
                notas.append(f"Liga propensa ao empate baixo (ρ={rho:.3f})")
            if media < 2.40:
                adj += 6.0
                notas.append(f"Liga defensiva (méd={media:.2f} gols/j)")
            if beta_h < 0.90 and beta_a < 0.90:
                adj += 5.0
                notas.append(f"Ambas as defesas sólidas (βH={beta_h:.2f}, βA={beta_a:.2f})")
            if n_jogos_h < 12 or n_jogos_a < 12:
                adj -= 4.0
                notas.append(f"Time com poucos jogos (H={n_jogos_h}j, A={n_jogos_a}j)")

        # HC8: 1X ─ Mando Invicto vs Ameaça Visitante
        if mercado == "1X":
            if fortress_h:
                adj += 12.0
                notas.append(f"Mando Invicto: mandante dominante (αH={alpha_h:.2f}, βH={beta_h:.2f})")
            if rho < -0.08:
                adj += 5.0
                notas.append(f"Liga propensa ao empate reforça 1X (ρ={rho:.3f})")
            if force_a > force_h * 1.25:
                adj -= 10.0
                notas.append(f"Visitante estruturalmente superior ameaça 1X (fA={force_a:.2f})")
            elif lethal_a and not fortress_h:
                adj -= 8.0
                notas.append(f"Visitante letal vs mandante sem fortaleza (αA={alpha_a:.2f})")

        # HC9: X2 ─ Zebra Descarada vs Dupla Chance Ilusória
        if mercado == "X2":
            if fragil_h and lethal_a:
                adj += 12.0
                notas.append(f"Zebra Descarada: casa frágil vs visitante letal (αA={alpha_a:.2f}, βH={beta_h:.2f})")
            elif alpha_a > 1.10:
                adj += 5.0
                notas.append(f"Visitante com forte ataque (αA={alpha_a:.2f})")
            if fortress_h and xg_lam > 1.60:
                adj -= 15.0
                notas.append(f"Dupla Chance Ilusória: mandante domina (αH={alpha_h:.2f}, λ={xg_lam:.2f})")
            elif force_h > 1.50:
                adj -= 10.0
                notas.append(f"Mandante com vantagem estrutural contradiz X2 (fH={force_h:.2f})")

        # HC10: 12 ─ Decisão Direta vs Empate Provável
        if mercado == "12":
            if cbi < 0.50:
                adj += 8.0
                notas.append(f"Resultado decisivo esperado (CBI={cbi:.2f})")
            if force_h > 1.30 or force_a > 1.30:
                adj += 5.0
                notas.append(f"Existe favorito claro (fMax={max(force_h, force_a):.2f})")
            if cbi > 0.80 and rho < -0.10:
                adj -= 12.0
                notas.append(f"Empate provável: equilíbrio em liga ρ-negativa (CBI={cbi:.2f}, ρ={rho:.3f})")
            elif fortress_h and xg_mu < 0.85:
                adj -= 8.0
                notas.append(f"Mandante domina, visitante sem gol — 12 perde sentido (μ={xg_mu:.2f})")

        # ── Forma recente + H2H (historico_ligas — 0 créditos extras) ───────
        h_win_streak     = int(ctx.get("h_win_streak",     0))
        a_win_streak     = int(ctx.get("a_win_streak",     0))
        h_losses_last5   = int(ctx.get("h_losses_last5",   0))
        a_losses_last5   = int(ctx.get("a_losses_last5",   0))
        h_wins_last5     = int(ctx.get("h_wins_last5",     0))
        a_wins_last5     = int(ctx.get("a_wins_last5",     0))
        h_draws_last5    = int(ctx.get("h_draws_last5",    0))
        a_draws_last5    = int(ctx.get("a_draws_last5",    0))
        h_gf_last5       = int(ctx.get("h_gf_last5",       0))
        a_gf_last5       = int(ctx.get("a_gf_last5",       0))
        h_n_last5        = int(ctx.get("h_n_last5",        0))
        a_n_last5        = int(ctx.get("a_n_last5",        0))
        h_sem_marcar     = int(ctx.get("h_sem_marcar",     0))
        a_sem_marcar     = int(ctx.get("a_sem_marcar",     0))
        h_cs_streak_casa = int(ctx.get("h_cs_streak_casa", 0))
        h2h_h_wins       = int(ctx.get("h2h_h_wins",       0))
        h2h_a_wins       = int(ctx.get("h2h_a_wins",       0))
        h2h_total        = int(ctx.get("h2h_total",        0))
        tem_forma = h_n_last5 >= 3 and a_n_last5 >= 3
        tem_h2h   = h2h_total >= 5

        # HC11: Cavalo Cansado — modelo favorece mandante mas forma recente contradiz
        if tem_forma and mercado in ("HOME", "1X"):
            if ratio > 1.20 and h_losses_last5 >= 3:
                adj -= 15.0
                notas.append(f"Cavalo Cansado: favorito em queda ({h_losses_last5}D em {h_n_last5}j)")
            elif ratio > 1.20 and h_losses_last5 >= 2 and h_wins_last5 == 0:
                adj -= 8.0
                notas.append(f"Mandante favorito sem vencer (0V/{h_losses_last5}D em {h_n_last5}j)")

        # HC12: Rolo Compressor — sequência ativa de vitórias
        if tem_forma:
            if h_win_streak >= 4:
                if mercado == "HOME":
                    adj += 12.0
                    notas.append(f"Rolo Compressor: mandante em {h_win_streak} vitórias seguidas")
                elif mercado == "1X":
                    adj += 8.0
                    notas.append(f"Mandante em {h_win_streak} vit. seguidas reforça 1X")
                elif mercado in ("AWAY", "X2"):
                    adj -= 10.0
                    notas.append(f"Mandante em {h_win_streak} vit. seguidas contradiz {mercado}")
                elif mercado in ("UNDER_25", "BTTS_NO"):
                    adj += 5.0
                    notas.append(f"Mandante dominante ({h_win_streak} vit. seg.) apoia Under/BTTS_NO")
            if a_win_streak >= 4:
                if mercado == "AWAY":
                    adj += 12.0
                    notas.append(f"Rolo Compressor visitante: {a_win_streak} vitórias seguidas")
                elif mercado == "X2":
                    adj += 8.0
                    notas.append(f"Visitante em {a_win_streak} vit. seguidas reforça X2")
                elif mercado in ("HOME", "1X"):
                    adj -= 8.0
                    notas.append(f"Visitante em {a_win_streak} vit. seguidas enfraquece {mercado}")

        # HC13: Freguesia Histórica — H2H com dominância clara (mín. 5 confrontos)
        if tem_h2h:
            h2h_h_pct = h2h_h_wins / h2h_total
            h2h_a_pct = h2h_a_wins / h2h_total
            if h2h_h_pct >= 0.70:
                if mercado == "HOME":
                    adj += 10.0
                    notas.append(f"Freguesia Histórica: mandante domina H2H ({h2h_h_wins}V/{h2h_total}j)")
                elif mercado == "1X":
                    adj += 6.0
                    notas.append(f"H2H favorece mandante ({h2h_h_wins}/{h2h_total} vitórias)")
                elif mercado in ("AWAY", "X2"):
                    adj -= 10.0
                    notas.append(f"H2H desfavorável ao visitante: mandante venceu {h2h_h_wins}/{h2h_total}")
            elif h2h_a_pct >= 0.70:
                if mercado == "AWAY":
                    adj += 10.0
                    notas.append(f"Freguesia Histórica: visitante domina H2H ({h2h_a_wins}V/{h2h_total}j)")
                elif mercado == "X2":
                    adj += 6.0
                    notas.append(f"H2H favorece visitante ({h2h_a_wins}/{h2h_total} vitórias)")
                elif mercado in ("HOME", "1X"):
                    adj -= 10.0
                    notas.append(f"H2H desfavorável ao mandante: visitante venceu {h2h_a_wins}/{h2h_total}")

        # HC14: Ataque Inoperante — seca de gols mina Over/BTTS_YES
        if tem_forma and mercado in ("OVER_25", "BTTS_YES"):
            if h_sem_marcar >= 3:
                adj -= 12.0
                notas.append(f"Ataque inoperante: mandante sem marcar há {h_sem_marcar}j")
            elif h_sem_marcar == 2:
                adj -= 5.0
                notas.append(f"Mandante sem marcar nos últimos {h_sem_marcar} jogos")
            if a_sem_marcar >= 3:
                adj -= 12.0
                notas.append(f"Ataque inoperante: visitante sem marcar há {a_sem_marcar}j")
            elif a_sem_marcar == 2:
                adj -= 5.0
                notas.append(f"Visitante sem marcar nos últimos {a_sem_marcar} jogos")

        # HC15: Muralha em Casa — clean sheets em casa favorece Under/BTTS_NO
        if tem_forma and mercado in ("UNDER_25", "BTTS_NO"):
            if h_cs_streak_casa >= 3:
                adj += 10.0
                notas.append(f"Muralha em Casa: {h_cs_streak_casa} clean sheets seguidas do mandante")
            elif h_cs_streak_casa == 2:
                adj += 5.0
                notas.append(f"Mandante com {h_cs_streak_casa} clean sheets em casa recentes")

        # HC16: Forma Goleadora — ambos marcando bem favorece Over/BTTS_YES
        if tem_forma and mercado in ("OVER_25", "BTTS_YES"):
            if h_gf_last5 >= 4 and a_gf_last5 >= 4:
                adj += 8.0
                notas.append(f"Forma goleadora: H {h_gf_last5}gols, A {a_gf_last5}gols em {h_n_last5}j")
            elif h_gf_last5 >= 3 and a_gf_last5 >= 3:
                adj += 4.0
                notas.append(f"Ambos marcando bem (H={h_gf_last5}, A={a_gf_last5} gols em {h_n_last5}j)")

        # HC17: Empate Recorrente — histórico de empates recentes corrobora DRAW
        if tem_forma and mercado == "DRAW":
            if h_draws_last5 >= 2 and a_draws_last5 >= 2:
                adj += 4.0
                notas.append(f"Empate recorrente: H {h_draws_last5}E, A {a_draws_last5}E em {h_n_last5}j")
            elif h_draws_last5 + a_draws_last5 >= 5:
                adj += 3.0
                notas.append(f"Times propensos ao empate (H={h_draws_last5}, A={a_draws_last5} em {h_n_last5}j)")

        # HC_BTTS_DEF v2: Bloqueio escalonado de BTTS_NO por ataque visitante letal
        # >=6g → hard block (-35pts força drop abaixo de 60); >=5g → gravemente fragilizado;
        # >=4g → bloqueado; >=3g → penalidade leve. D-C histórico subestima visitantes em form.
        if mercado == "BTTS_NO" and tem_forma:
            if a_gf_last5 >= 6:
                adj -= 35.0
                notas.append(f"HARD BLOCK — visitante EXPLOSIVO: {a_gf_last5}g em {a_n_last5}j — BTTS_NO inválido")
            elif a_gf_last5 >= 5:
                adj -= 25.0
                notas.append(f"Visitante em explosão ofensiva ({a_gf_last5}g em {a_n_last5}j) — BTTS_NO gravemente fragilizado")
            elif a_gf_last5 >= 4:
                adj -= 15.0
                notas.append(f"Ataque visitante LETAL: {a_gf_last5}g em {a_n_last5}j — BTTS_NO bloqueado")
            elif a_gf_last5 >= 3:
                adj -= 8.0
                notas.append(f"Visitante em forma ofensiva ({a_gf_last5}g em {a_n_last5}j)")
            if h_gf_last5 >= 4 and a_gf_last5 >= 3:
                adj -= 10.0
                notas.append(f"Ambos marcando bem (H={h_gf_last5}, A={a_gf_last5}g) — BTTS_NO fragilizado")
            if h_gf_last5 >= 5:
                adj -= 12.0
                notas.append(f"Mandante também goleador ({h_gf_last5}g em {h_n_last5}j) — BTTS_NO duplamente ameaçado")

        # HC18: Invencibilidade — sequência de 4+ jogos sem perder (V+E) favorece o time
        if tem_forma:
            h_unbeaten = h_wins_last5 + h_draws_last5
            a_unbeaten = a_wins_last5 + a_draws_last5
            if h_unbeaten >= 4 and h_n_last5 >= 4:
                if mercado in ("HOME", "1X"):
                    adj += 10.0
                    notas.append(f"Invencibilidade do mandante: {h_unbeaten} sem perder em {h_n_last5}j")
                elif mercado in ("AWAY", "X2"):
                    adj -= 8.0
                    notas.append(f"Mandante invicto há {h_unbeaten}j — enfraquece {mercado}")
            if a_unbeaten >= 4 and a_n_last5 >= 4:
                if mercado in ("AWAY", "X2"):
                    adj += 10.0
                    notas.append(f"Invencibilidade do visitante: {a_unbeaten} sem perder em {a_n_last5}j")
                elif mercado in ("HOME", "1X"):
                    adj -= 8.0
                    notas.append(f"Visitante invicto há {a_unbeaten}j — enfraquece {mercado}")

        # HC19: Defesa de Ferro — time sofreu ≤2 gols no somatório dos últimos 4 jogos
        if tem_forma:
            h_ga = int(ctx.get("h_ga_last5", 99))
            a_ga = int(ctx.get("a_ga_last5", 99))
            if h_ga <= 2 and h_n_last5 >= 4 and mercado in ("UNDER_25", "BTTS_NO"):
                adj += 12.0
                notas.append(f"Defesa de Ferro do mandante: só {h_ga}g sofridos em {h_n_last5}j")
            if a_ga <= 2 and a_n_last5 >= 4 and mercado in ("UNDER_25", "BTTS_NO"):
                adj += 12.0
                notas.append(f"Defesa de Ferro do visitante: só {a_ga}g sofridos em {a_n_last5}j")

        # HC20: Impulso Visitante — visitante ganhou 3+ dos últimos 5 jogos
        if tem_forma:
            if a_wins_last5 >= 3:
                if mercado in ("AWAY", "X2"):
                    adj += 8.0
                    notas.append(f"Impulso Visitante: {a_wins_last5}V em {a_n_last5}j")
                elif mercado in ("HOME", "1X"):
                    adj -= 5.0
                    notas.append(f"Visitante em alta ({a_wins_last5}V em {a_n_last5}j) — enfraquece {mercado}")

        # HC21: Defesas Mútuas Vulneráveis — ambos sofrendo muitos gols
        if tem_forma:
            h_ga_v = int(ctx.get("h_ga_last5", 0))
            a_ga_v = int(ctx.get("a_ga_last5", 0))
            if h_ga_v >= 4 and a_ga_v >= 4:
                if mercado in ("OVER_25", "BTTS_YES"):
                    adj += 8.0
                    notas.append(f"Defesas vazadas (H={h_ga_v}, A={a_ga_v}g sofridos) — Over/BTTS favoritizados")
                elif mercado in ("UNDER_25", "BTTS_NO"):
                    adj -= 10.0
                    notas.append(f"Defesas muito abertas (H={h_ga_v}, A={a_ga_v}g sofridos) — Under/BTTS_NO bloqueado")

        # HC22: Visitante em Seca Ofensiva — não marcou em 2+ jogos consecutivos
        if tem_forma:
            a_sem = int(ctx.get("a_sem_marcar", 0))
            if a_sem >= 2:
                if mercado in ("BTTS_NO", "UNDER_25"):
                    adj += 8.0
                    notas.append(f"Visitante em seca: {a_sem}j sem marcar — BTTS_NO/Under favorecidos")
                elif mercado in ("BTTS_YES", "AWAY"):
                    adj -= 8.0
                    notas.append(f"Visitante sem marcar há {a_sem}j — fragiliza BTTS_YES/AWAY")

        # HC23: OVER_35 com defesas mútuas muito abertas na forma recente
        if mercado == "OVER_35" and tem_forma:
            h_ga_35 = int(ctx.get("h_ga_last5", 0))
            a_ga_35 = int(ctx.get("a_ga_last5", 0))
            if h_ga_35 >= 6 and a_ga_35 >= 6:
                adj += 12.0
                notas.append(f"Defesas muito abertas (H sofreu {h_ga_35}, A sofreu {a_ga_35}g) — Over 3.5 potencializado")
            elif h_ga_35 >= 5 and a_ga_35 >= 5:
                adj += 6.0
                notas.append(f"Defesas vulneráveis (H={h_ga_35}, A={a_ga_35}g sofridos) — Over 3.5 favorecido")

        # HC24: UNDER_15 — estéril ofensiva na forma recente
        if mercado == "UNDER_15" and tem_forma:
            if h_gf_last5 + a_gf_last5 <= 2:
                adj += 8.0
                notas.append(f"Ambas equipes estéreis (H={h_gf_last5}+A={a_gf_last5}g em {h_n_last5}j)")
            elif h_gf_last5 <= 1 and a_gf_last5 <= 1:
                adj += 5.0
                notas.append(f"Produção ofensiva muito baixa (H={h_gf_last5}, A={a_gf_last5}g em {h_n_last5}j)")

        # HC25: Dupla Seca Ofensiva — ambos sem marcar reforça UNDER/BTTS_NO
        if mercado in ("UNDER_25", "BTTS_NO") and tem_forma:
            if h_sem_marcar >= 2 and a_sem_marcar >= 2:
                adj += 12.0
                notas.append(f"Dupla Seca: ambos sem marcar (H={h_sem_marcar}j, A={a_sem_marcar}j) — Under/BTTS_NO reforçados")

        # HC26: Liga super-goleadora reforça OVER_25 com maior convicção
        if mercado == "OVER_25" and media > 3.20:
            adj += 6.0
            notas.append(f"Liga super-goleadora (méd={media:.2f}g/j) — Over 2.5 estruturalmente favorecido")

        # HC27: DRAW — zona de risco moderado (λ/μ 1.6-2.2, não coberta por H4)
        if mercado == "DRAW" and 1.60 <= ratio < 2.20:
            adj -= 5.0
            notas.append(f"Desequilíbrio moderado (λ/μ={ratio:.1f}) — risco latente para o empate")

        # HC28: DRAW — forma equilibrada de ambos confirma empate estatisticamente
        if mercado == "DRAW" and tem_forma and ratio < 1.60:
            if h_draws_last5 >= 2 or a_draws_last5 >= 2:
                if (h_wins_last5 + h_draws_last5) >= 3 and (a_wins_last5 + a_draws_last5) >= 3:
                    adj += 4.0
                    notas.append(f"Forma equilibrada (H={h_wins_last5}V+{h_draws_last5}E, A={a_wins_last5}V+{a_draws_last5}E) — empate plausível")

        # HC29: HOME/1X/OVER_25 — mandante prolífico e invicto recente
        if mercado in ("HOME", "OVER_25", "1X") and tem_forma:
            if h_gf_last5 >= 5 and h_losses_last5 == 0:
                if mercado in ("HOME", "1X"):
                    adj += 7.0
                else:
                    adj += 5.0
                notas.append(f"Mandante prolífico e invicto ({h_gf_last5}g, 0D em {h_n_last5}j)")

    # ═══════════════════════════════════════════════════════════
    # SCOUT LAYER — Heurísticas Avançadas (dados de /fixtures/statistics)
    # Ativa apenas quando historico_ligas tem stats de scout disponíveis
    # ═══════════════════════════════════════════════════════════
    if ctx:
        h_sog   = ctx.get("h_shots_on_avg")    # shots on goal / game
        a_sog   = ctx.get("a_shots_on_avg")
        h_shots = ctx.get("h_shots_total_avg")  # total shots / game
        a_shots = ctx.get("a_shots_total_avg")
        h_poss  = ctx.get("h_possession_avg")   # possession %
        a_poss  = ctx.get("a_possession_avg")
        h_corn  = ctx.get("h_corners_avg")
        a_corn  = ctx.get("a_corners_avg")
        h_yell  = ctx.get("h_yellows_avg")
        a_yell  = ctx.get("a_yellows_avg")
        h_spg   = ctx.get("h_shots_per_goal")   # shots needed per goal
        a_spg   = ctx.get("a_shots_per_goal")

        tem_scout = h_sog is not None and a_sog is not None

        if tem_scout:
            # HSC1: Eficiência Ofensiva Oculta
            # D-C pode subestimar jogos onde ambos batem muito no alvo
            if h_sog >= 5.0 and a_sog >= 5.0:
                if mercado in ("OVER_25", "BTTS_YES"):
                    adj += 10.0
                    notas.append(f"Scout: alta produção no alvo (H={h_sog:.1f}, A={a_sog:.1f} sog/j) — Over/BTTS favorecido")
                elif mercado in ("UNDER_25", "BTTS_NO"):
                    adj -= 8.0
                    notas.append(f"Scout: ambos criam muito (H={h_sog:.1f}, A={a_sog:.1f} sog/j) — Under/BTTS_NO fragilizado")

            # HSC2: Mira Ruim — Ineficiência Ofensiva
            if h_spg is not None and a_spg is not None:
                if h_spg >= 15.0 and a_spg >= 15.0:
                    if mercado in ("UNDER_25", "BTTS_NO"):
                        adj += 8.0
                        notas.append(f"Scout: ambos ineficientes ({h_spg:.0f}/{a_spg:.0f} chutes/gol) — Under/BTTS_NO seguro")
                    elif mercado in ("OVER_25", "BTTS_YES"):
                        adj -= 6.0
                        notas.append(f"Scout: baixa conversão ofensiva ({h_spg:.0f}/{a_spg:.0f} ch/gol) — Over penalizado")
                elif h_spg is not None and h_spg >= 15.0 and mercado in ("HOME",):
                    adj -= 5.0
                    notas.append(f"Scout: mandante ineficiente ({h_spg:.0f} ch/gol) — HOME penalizado")

            # HSC3: Dominância Territorial — Posse Alta com D-C confirmando
            if h_poss is not None and a_poss is not None:
                if h_poss >= 58.0 and mercado in ("HOME", "1X"):
                    adj += 12.0
                    notas.append(f"Scout: dominância territorial do mandante ({h_poss:.0f}% posse) — HOME/1X reforçado")
                elif a_poss >= 58.0 and mercado in ("AWAY", "X2"):
                    adj += 12.0
                    notas.append(f"Scout: dominância territorial do visitante ({a_poss:.0f}% posse) — AWAY/X2 reforçado")
                elif h_poss >= 58.0 and mercado in ("AWAY", "X2"):
                    adj -= 8.0
                    notas.append(f"Scout: mandante domina posse ({h_poss:.0f}%) — AWAY/X2 fragilizado")
                elif a_poss >= 58.0 and mercado in ("HOME", "1X"):
                    adj -= 8.0
                    notas.append(f"Scout: visitante domina posse ({a_poss:.0f}%) — HOME/1X fragilizado")

            # HSC4: Pressão por Escanteios — Território de Gol
            if h_corn is not None and a_corn is not None:
                if h_corn >= 6.0 and a_corn >= 6.0 and mercado in ("OVER_25", "BTTS_YES"):
                    adj += 6.0
                    notas.append(f"Scout: alta pressão territorial (H={h_corn:.1f}, A={a_corn:.1f} escanteios/j)")
                elif h_corn >= 7.0 and mercado in ("HOME", "1X"):
                    adj += 6.0
                    notas.append(f"Scout: mandante cria muita pressão ({h_corn:.1f} escanteios/j)")

            # HSC5: Jogo Truncado — Cartões Travam o Ritmo
            if h_yell is not None and a_yell is not None:
                if h_yell >= 2.5 and a_yell >= 2.5:
                    if mercado in ("UNDER_25", "DRAW"):
                        adj += 5.0
                        notas.append(f"Scout: jogo físico e truncado (H={h_yell:.1f}, A={a_yell:.1f} cartões/j) — ritmo reduzido")
                    elif mercado in ("OVER_25",):
                        adj -= 4.0
                        notas.append(f"Scout: jogo truncado ({h_yell:.1f}+{a_yell:.1f} cartões) — fluidity reduzida")

            # HSC6: Equilíbrio de Posse — Sinal de Empate Estatístico
            if h_poss is not None and a_poss is not None:
                if 45.0 <= h_poss <= 55.0 and 45.0 <= a_poss <= 55.0:
                    if mercado in ("DRAW", "1X", "X2"):
                        adj += 6.0
                        notas.append(f"Scout: posse equilibrada (H={h_poss:.0f}%/A={a_poss:.0f}%) — Empate estatisticamente plausível")

            # HSC7: Goleiro Ativo — Muitos Remates Sofridos
            h_sav = ctx.get("h_saves_avg")
            a_sav = ctx.get("a_saves_avg")
            if h_sav is not None and a_sav is not None:
                if h_sav >= 4.5 and a_sav >= 4.5:
                    if mercado in ("OVER_25", "BTTS_YES"):
                        adj += 6.0
                        notas.append(f"Scout: goleiros muito ativos (H={h_sav:.1f}, A={a_sav:.1f} defesas/j) — jogo aberto")

            # HSC8: Goals Prevented — valor real do goleiro acima do xG
            # API entrega `goals_prevented` = gols evitados além da expectativa.
            # GK com média ≥0.8/j está sistematicamente salvando mais que xG prevê →
            # defesa real é melhor que o D-C estima só pelos gols sofridos.
            h_gprev = ctx.get("h_goals_prevented_avg")
            a_gprev = ctx.get("a_goals_prevented_avg")
            if h_gprev is not None and a_gprev is not None:
                if h_gprev >= 0.8 and mercado in ("UNDER_25", "BTTS_NO"):
                    adj += 8.0
                    notas.append(f"Scout: goleiro mandante de impacto ({h_gprev:.2f} gols prev/j) — Under/BTTS_NO reforçado")
                elif h_gprev >= 0.8 and mercado == "AWAY":
                    adj -= 5.0
                    notas.append(f"Scout: goleiro mandante ativo ({h_gprev:.2f} prev/j) — AWAY mais difícil")
                if a_gprev >= 0.8 and mercado in ("UNDER_25", "BTTS_NO"):
                    adj += 8.0
                    notas.append(f"Scout: goleiro visitante de impacto ({a_gprev:.2f} gols prev/j)")
                elif a_gprev >= 0.8 and mercado == "HOME":
                    adj -= 5.0
                    notas.append(f"Scout: goleiro visitante ativo ({a_gprev:.2f} prev/j) — HOME mais difícil")
                if h_gprev >= 0.6 and a_gprev >= 0.6 and mercado in ("UNDER_25", "BTTS_NO"):
                    adj += 5.0
                    notas.append(f"Scout: ambos goleiros acima do esperado (H={h_gprev:.2f}, A={a_gprev:.2f} prev/j)")

            # HSC9: Qualidade de Finalização — proporção de chutes dentro da área
            # Alta proporção (>60%) = mais finalizações de boa posição = mais gols prováveis.
            # Baixa proporção (<40%) = equipes chutando de longe = defesas mais seguras.
            h_sib = ctx.get("h_shots_insidebox_avg")
            a_sib = ctx.get("a_shots_insidebox_avg")
            h_tot_s = ctx.get("h_shots_total_avg")
            a_tot_s = ctx.get("a_shots_total_avg")
            if h_sib is not None and a_sib is not None and h_tot_s and a_tot_s:
                h_sib_r = h_sib / h_tot_s if h_tot_s > 0 else 0
                a_sib_r = a_sib / a_tot_s if a_tot_s > 0 else 0
                if h_sib_r >= 0.60 and a_sib_r >= 0.60:
                    if mercado in ("OVER_25", "BTTS_YES"):
                        adj += 7.0
                        notas.append(f"Scout: alta qualidade de finalizações (H={h_sib_r:.0%}/A={a_sib_r:.0%} inside box)")
                elif h_sib_r <= 0.40 and a_sib_r <= 0.40:
                    if mercado in ("UNDER_25", "BTTS_NO"):
                        adj += 5.0
                        notas.append(f"Scout: chutes de baixa qualidade (H={h_sib_r:.0%}/A={a_sib_r:.0%} inside box) — Under favorecido")

            # HSC10: Offsides — agressividade da linha ofensiva
            # Times com ≥3.5 impedimentos/j jogam com linha alta e tentam pressionar →
            # jogo mais aberto, mais oportunidades para ambos → Over/BTTS_YES sinal.
            h_off = ctx.get("h_offsides_avg")
            a_off = ctx.get("a_offsides_avg")
            if h_off is not None and a_off is not None:
                if h_off >= 3.5 and a_off >= 3.5:
                    if mercado in ("OVER_25", "BTTS_YES"):
                        adj += 6.0
                        notas.append(f"Scout: linhas ofensivas agressivas (H={h_off:.1f}/A={a_off:.1f} impedimentos/j) — jogo aberto")
                    elif mercado == "UNDER_25":
                        adj -= 5.0
                        notas.append(f"Scout: ambas equipes atacam alto (H={h_off:.1f}/A={a_off:.1f} offside/j) — Under em risco")
                elif h_off <= 1.5 and a_off <= 1.5:
                    if mercado in ("UNDER_25", "BTTS_NO"):
                        adj += 4.0
                        notas.append(f"Scout: jogo conservador (H={h_off:.1f}/A={a_off:.1f} impedimentos/j) — Under favorecido")

            # HSC11: Blocked Shots — defesas ativas bloqueando remates
            # Muitos bloqueios = defesa organizada, baixo xG convertido → Under/BTTS_NO sinal.
            # Poucos bloqueios = defesas passivas, mais chances perigosas chegam ao alvo.
            h_blk = ctx.get("h_blocked_shots_avg")
            a_blk = ctx.get("a_blocked_shots_avg")
            if h_blk is not None and a_blk is not None:
                blk_total = h_blk + a_blk
                if blk_total >= 7.0:
                    if mercado in ("UNDER_25", "BTTS_NO"):
                        adj += 5.0
                        notas.append(f"Scout: defesas bloqueando muito ({blk_total:.1f} bloqueios/j) — Under/BTTS_NO corroborado")
                    elif mercado in ("OVER_25", "BTTS_YES"):
                        adj -= 4.0
                        notas.append(f"Scout: defesas ativas ({blk_total:.1f} bloqueios/j) — Over penalizado")
                elif blk_total <= 2.5:
                    if mercado in ("OVER_25", "BTTS_YES"):
                        adj += 4.0
                        notas.append(f"Scout: poucas defesas com bloqueio ({blk_total:.1f}/j) — Over favorecido")

            # HSC12: Shots off Goal — chutes que erram o alvo (ineficiência ofensiva)
            # Alto índice = atacantes desperdiçando oportunidades → baixa conversão real.
            # Contexto: compare com shots_on para ver a eficiência de mira.
            h_sog_off = ctx.get("h_shots_offgoal_avg")
            a_sog_off = ctx.get("a_shots_offgoal_avg")
            if h_sog_off is not None and a_sog_off is not None and h_shots is not None and a_shots is not None:
                h_miss_rate = h_sog_off / h_shots if h_shots > 0 else None
                a_miss_rate = a_sog_off / a_shots if a_shots > 0 else None
                if h_miss_rate is not None and a_miss_rate is not None:
                    if h_miss_rate >= 0.50 and a_miss_rate >= 0.50:
                        if mercado in ("UNDER_25", "BTTS_NO"):
                            adj += 4.0
                            notas.append(f"Scout: alta taxa de erros de mira (H={h_miss_rate:.0%}/A={a_miss_rate:.0%}) — Under reforçado")
                        elif mercado in ("OVER_25", "BTTS_YES"):
                            adj -= 4.0
                            notas.append(f"Scout: atacantes imprecisos (H={h_miss_rate:.0%}/A={a_miss_rate:.0%} fora do alvo) — Over penalizado")

            # HSC13: Passes % — controle e qualidade de jogo
            # Time com alta taxa de passes (≥75%) domina o jogo mas pode ser conservador.
            # Desequilíbrio grande (≥20pp) indica dominância que pode converter em gols.
            h_pct = ctx.get("h_passes_pct_avg")
            a_pct = ctx.get("a_passes_pct_avg")
            if h_pct is not None and a_pct is not None:
                pct_gap = abs(h_pct - a_pct)
                if pct_gap >= 20.0:
                    dom_side = "mandante" if h_pct > a_pct else "visitante"
                    if mercado in ("HOME", "1X") and h_pct > a_pct:
                        adj += 4.0
                        notas.append(f"Scout: mandante domina a bola ({h_pct:.0f}% vs {a_pct:.0f}%) — HOME/1X favorecido")
                    elif mercado in ("AWAY", "X2") and a_pct > h_pct:
                        adj += 4.0
                        notas.append(f"Scout: visitante domina a bola ({a_pct:.0f}% vs {h_pct:.0f}%) — AWAY/X2 favorecido")
                    elif mercado == "DRAW":
                        adj -= 4.0
                        notas.append(f"Scout: desequilíbrio de posse ({pct_gap:.0f}pp) — empate menos provável")

    # ── Detecção de conflito de sinais ───────────────────────────────────────
    # Proxy: há notas que indicam bônus E notas que indicam penalidade (≥3 regras disparadas)
    _kw_pos = ("Fortaleza", "Rolo", "Invencib", "Ferro", "Muralha", "Impulso",
               "Dupla Seca", "xG muito baixo", "xG baixo", "super-gole", "prolífico",
               "propensa ao empate", "equilibrado", "equilibrada", "Goleador",
               "Eficiência", "Dominância", "Goleiro", "Escanteio", "Seca: ",
               "estéreis", "vulneráveis (H=", "Defesas mútuas vuln")
    _kw_neg = ("fragiliz", "BLOCK", "HARD", "Cansado", "Ilusória", "contradiz",
               "risco latente", "inoperante", "sem gol", "Desequilíbrio",
               "moderado (λ", "poucos jogos", "acima do limiar", "próximo do limiar",
               "inofensivo", "duplamente ameaçado", "gravemente fragilizado",
               "Defesas muito abertas", "defesas vuln")
    _has_pos = any(any(kw in n for kw in _kw_pos) for n in notas)
    _has_neg = any(any(kw in n for kw in _kw_neg) for n in notas)
    _conflito = _has_pos and _has_neg and len(notas) >= 3

    if adj > 0:
        nota_final = f"+{adj:.0f}pts — " + " · ".join(notas) if notas else f"+{adj:.0f}pts"
    elif adj < 0:
        nota_final = f"{adj:.0f}pts — " + " · ".join(notas) if notas else f"{adj:.0f}pts"
    else:
        nota_final = "Contexto OK"

    if _conflito:
        nota_final = f"[⚠️ CONFLITO DE SINAIS] {nota_final}"

    return adj, nota_final


def extrair_forma_times(
    df_liga: "pd.DataFrame",
    home_id: int,
    away_id: int,
    n_forma: int = 5,
) -> dict:
    """
    Extrai métricas de forma recente e H2H do historico_ligas local (0 créditos extras).

    df_liga : DataFrame ordenado por 'date' com colunas home_id, away_id, home_goals, away_goals.
    Retorna dict com chaves h_* / a_* (por time) e h2h_* (confronto direto).
    Todos os campos retornam 0 quando há dados insuficientes ou em caso de erro.
    """
    _vazio: dict = {
        "h_wins_last5":     0, "h_losses_last5":   0, "h_draws_last5":    0,
        "h_gf_last5":       0, "h_ga_last5":       0, "h_n_last5":        0,
        "a_wins_last5":     0, "a_losses_last5":   0, "a_draws_last5":    0,
        "a_gf_last5":       0, "a_ga_last5":       0, "a_n_last5":        0,
        "h_win_streak":     0, "a_win_streak":     0,
        "h_sem_marcar":     0, "a_sem_marcar":     0,
        "h_cs_streak_casa": 0,
        "h2h_h_wins":       0, "h2h_a_wins":       0,
        "h2h_draws":        0, "h2h_total":        0,
        "h_shots_on_avg": None, "h_shots_total_avg": None, "h_possession_avg": None,
        "h_corners_avg": None, "h_yellows_avg": None, "h_saves_avg": None, "h_fouls_avg": None,
        "h_shots_per_goal": None,
        "h_goals_prevented_avg": None, "h_shots_insidebox_avg": None, "h_offsides_avg": None,
        "h_blocked_shots_avg": None, "h_shots_offgoal_avg": None, "h_passes_pct_avg": None,
        "a_shots_on_avg": None, "a_shots_total_avg": None, "a_possession_avg": None,
        "a_corners_avg": None, "a_yellows_avg": None, "a_saves_avg": None, "a_fouls_avg": None,
        "a_shots_per_goal": None,
        "a_goals_prevented_avg": None, "a_shots_insidebox_avg": None, "a_offsides_avg": None,
        "a_blocked_shots_avg": None, "a_shots_offgoal_avg": None, "a_passes_pct_avg": None,
    }
    try:
        if df_liga is None or df_liga.empty:
            return _vazio
        needed = {"home_id", "away_id", "home_goals", "away_goals"}
        if not needed.issubset(df_liga.columns):
            return _vazio

        df = df_liga.copy()
        df["home_goals"] = pd.to_numeric(df["home_goals"], errors="coerce").fillna(0).astype(int)
        df["away_goals"] = pd.to_numeric(df["away_goals"], errors="coerce").fillna(0).astype(int)
        if "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)

        def _jogos_time(tid: int) -> pd.DataFrame:
            cols_base = ["date"] if "date" in df.columns else []
            home = df[df["home_id"] == tid][cols_base + ["home_goals", "away_goals"]].copy()
            home = home.rename(columns={"home_goals": "gf", "away_goals": "ga"})
            away = df[df["away_id"] == tid][cols_base + ["away_goals", "home_goals"]].copy()
            away = away.rename(columns={"away_goals": "gf", "home_goals": "ga"})
            todos = pd.concat([home, away], ignore_index=True)
            if "date" in todos.columns:
                todos = todos.sort_values("date").reset_index(drop=True)
            todos["res"] = todos.apply(
                lambda r: "W" if r["gf"] > r["ga"] else ("L" if r["gf"] < r["ga"] else "D"),
                axis=1,
            )
            return todos

        def _ultimos(jg: pd.DataFrame, n: int) -> dict:
            last = jg.tail(n)
            if last.empty:
                return {"W": 0, "L": 0, "D": 0, "gf": 0, "ga": 0, "n": 0}
            vc = last["res"].value_counts()
            return {
                "W": int(vc.get("W", 0)), "L": int(vc.get("L", 0)), "D": int(vc.get("D", 0)),
                "gf": int(last["gf"].sum()), "ga": int(last["ga"].sum()), "n": len(last),
            }

        def _streak_vit(jg: pd.DataFrame) -> int:
            seq = list(jg["res"].tail(10))[::-1]
            s = 0
            for r in seq:
                if r == "W": s += 1
                else: break
            return s

        def _streak_sem_gol(jg: pd.DataFrame) -> int:
            seq = list(jg["gf"].tail(10))[::-1]
            s = 0
            for g in seq:
                if int(g) == 0: s += 1
                else: break
            return s

        def _cs_casa(tid: int) -> int:
            hg = df[df["home_id"] == tid]
            if "date" in hg.columns:
                hg = hg.sort_values("date")
            seq = list(hg["away_goals"].fillna(0).astype(int).tail(10))[::-1]
            s = 0
            for g in seq:
                if g == 0: s += 1
                else: break
            return s

        # H2H: últimos 10 confrontos diretos (home ou away) entre os dois times
        mask_h2h = (
            ((df["home_id"] == home_id) & (df["away_id"] == away_id)) |
            ((df["home_id"] == away_id) & (df["away_id"] == home_id))
        )
        h2h = df[mask_h2h].tail(10)
        h2h_total = len(h2h)
        h2h_h = h2h_a = 0
        if h2h_total > 0:
            h2h_h = int(
                ((h2h["home_id"] == home_id) & (h2h["home_goals"] > h2h["away_goals"])).sum() +
                ((h2h["away_id"] == home_id) & (h2h["away_goals"] > h2h["home_goals"])).sum()
            )
            h2h_a = int(
                ((h2h["home_id"] == away_id) & (h2h["home_goals"] > h2h["away_goals"])).sum() +
                ((h2h["away_id"] == away_id) & (h2h["away_goals"] > h2h["home_goals"])).sum()
            )

        dh = _jogos_time(home_id)
        da = _jogos_time(away_id)
        uh = _ultimos(dh, n_forma)
        ua = _ultimos(da, n_forma)

        def _scout_avg(tid: int, prefix: str, uh_ua: dict) -> dict:
            """Retorna médias de scout dos últimos n_forma jogos do time (qualquer mando)."""
            home_rows = df[df["home_id"] == tid].copy()
            away_rows = df[df["away_id"] == tid].copy()

            rename_h = {
                "h_shots_on": "shots_on", "h_shots_total": "shots_total",
                "h_possession": "possession", "h_corners": "corners",
                "h_yellows": "yellows", "h_saves": "saves", "h_fouls": "fouls",
                "h_goals_prevented": "goals_prevented",
                "h_shots_insidebox": "shots_insidebox",
                "h_offsides": "offsides",
                "h_blocked_shots": "blocked_shots",
                "h_shots_offgoal": "shots_offgoal",
                "h_passes_pct": "passes_pct",
            }
            rename_a = {
                "a_shots_on": "shots_on", "a_shots_total": "shots_total",
                "a_possession": "possession", "a_corners": "corners",
                "a_yellows": "yellows", "a_saves": "saves", "a_fouls": "fouls",
                "a_goals_prevented": "goals_prevented",
                "a_shots_insidebox": "shots_insidebox",
                "a_offsides": "offsides",
                "a_blocked_shots": "blocked_shots",
                "a_shots_offgoal": "shots_offgoal",
                "a_passes_pct": "passes_pct",
            }

            cols_scout = [
                "shots_on", "shots_total", "possession", "corners", "yellows", "saves", "fouls",
                "goals_prevented", "shots_insidebox", "offsides",
                "blocked_shots", "shots_offgoal", "passes_pct",
            ]

            h_scout = home_rows.rename(columns={k: v for k, v in rename_h.items() if k in home_rows.columns})
            a_scout = away_rows.rename(columns={k: v for k, v in rename_a.items() if k in away_rows.columns})

            frames = []
            for fr in [h_scout, a_scout]:
                avail = [c for c in cols_scout if c in fr.columns]
                if avail:
                    date_cols = ["date"] if "date" in fr.columns else []
                    frames.append(fr[avail + date_cols])

            if not frames:
                result = {f"{prefix}_{c}_avg": None for c in cols_scout}
                result[f"{prefix}_shots_per_goal"] = None
                return result

            combined = pd.concat(frames, ignore_index=True)
            if "date" in combined.columns:
                combined = combined.sort_values("date")
            last = combined.tail(n_forma)

            result = {}
            for c in cols_scout:
                if c in last.columns:
                    vals = pd.to_numeric(last[c], errors="coerce").dropna()
                    result[f"{prefix}_{c}_avg"] = float(vals.mean()) if len(vals) > 0 else None
                else:
                    result[f"{prefix}_{c}_avg"] = None

            # Derived: shots per goal (efficiency — shots_total needed per goal scored)
            shots_avg = result.get(f"{prefix}_shots_total_avg")
            gf = uh_ua.get("gf", 0)
            if shots_avg is not None and gf and gf > 0:
                result[f"{prefix}_shots_per_goal"] = shots_avg * n_forma / gf
            else:
                result[f"{prefix}_shots_per_goal"] = None

            return result

        scout_h = _scout_avg(home_id, "h", uh)
        scout_a = _scout_avg(away_id, "a", ua)

        return {
            "h_wins_last5":     uh["W"],    "h_losses_last5":   uh["L"],
            "h_draws_last5":    uh["D"],    "h_gf_last5":       uh["gf"],
            "h_ga_last5":       uh["ga"],   "h_n_last5":        uh["n"],
            "a_wins_last5":     ua["W"],    "a_losses_last5":   ua["L"],
            "a_draws_last5":    ua["D"],    "a_gf_last5":       ua["gf"],
            "a_ga_last5":       ua["ga"],   "a_n_last5":        ua["n"],
            "h_win_streak":     _streak_vit(dh),
            "a_win_streak":     _streak_vit(da),
            "h_sem_marcar":     _streak_sem_gol(dh),
            "a_sem_marcar":     _streak_sem_gol(da),
            "h_cs_streak_casa": _cs_casa(home_id),
            "h2h_h_wins":       h2h_h,
            "h2h_a_wins":       h2h_a,
            "h2h_draws":        h2h_total - h2h_h - h2h_a,
            "h2h_total":        h2h_total,
            **scout_h,
            **scout_a,
        }
    except Exception:
        return _vazio


def _render_pick_contexto(p: dict) -> None:
    """
    Expander de análise de contexto por pick — usado no Sniper e no Estrategista.
    Não faz chamadas de API: usa apenas dados já presentes no pick dict.
    Renderiza apenas blocos com conteúdo real — sem espaço vazio quando não há histórico local.
    """
    with st.expander("🔍 Análise de Contexto", expanded=False):
        _ctx  = p.get("dc_ctx") or {}
        _liga = p.get("liga", "—")
        _lr   = p.get("league_round", "")
        _lt   = p.get("league_type", "League")
        _jogo = p.get("jogo", "H v A")

        if " v " in _jogo:
            home_name, away_name = _jogo.split(" v ", 1)
            home_name = home_name.strip()
            away_name = away_name.strip()
        else:
            home_name, away_name = "Casa", "Fora"

        # ── Pré-computar disponibilidade de cada bloco ───────────────
        _h_n5    = int(_ctx.get("h_n_last5", 0))
        _a_n5    = int(_ctx.get("a_n_last5", 0))
        _has_form = _h_n5 >= 3 or _a_n5 >= 3
        _h2h_tot = int(_ctx.get("h2h_total", 0))
        _has_h2h = _h2h_tot >= 3

        _hnota = p.get("heur_nota", "Contexto OK")
        if "·" in _hnota:
            _parts = [s.strip() for s in _hnota.split("·") if s.strip()]
        elif _hnota and _hnota not in ("Contexto OK", ""):
            _parts = [_hnota]
        else:
            _parts = []
        _has_heur = bool(_parts)

        _scout_map = [
            ("shots_on_avg",      "Chutes no alvo"),
            ("shots_total_avg",   "Chutes totais"),
            ("possession_avg",    "Posse %"),
            ("corners_avg",       "Escanteios"),
            ("blocked_shots_avg", "Bloqueados"),
            ("shots_offgoal_avg", "Fora do alvo"),
            ("saves_avg",         "Defesas GK"),
            ("fouls_avg",         "Faltas"),
        ]
        _scout_rows = [
            (label, _ctx.get(f"h_{k}"), _ctx.get(f"a_{k}"))
            for k, label in _scout_map
            if _ctx.get(f"h_{k}") is not None or _ctx.get(f"a_{k}") is not None
        ]
        _has_scout = bool(_scout_rows)

        # ── Pré-buscar standings (necessário para detectar mata-mata por grupos) ─
        _l_id_ctx  = p.get("league_id")
        _l_sea_ctx = p.get("league_season", detectar_temporada_atual())
        _stds = _buscar_standings_cached(int(_l_id_ctx), _l_sea_ctx) if _l_id_ctx else []
        _n_grupos = len(_stds)

        # ── Bloco 1: Competição e flags (sempre renderiza) ───────────
        _copa_kw = ("cup", "copa", "coupe", "pokal", "league cup", "fa cup",
                    "champions", "europa", "conference", "libertadores",
                    "sulamericana", "concacaf", "afc", "world cup",
                    "nations league", "eliminatória", "qualifier")
        _is_copa = (_lt == "Cup") or any(kw in _liga.lower() for kw in _copa_kw)

        # "final" sozinho pode ser a última rodada regular (ex: J1 "Final" = rodada 34).
        # Mas se a liga tem múltiplos grupos nos standings, a "Final" é o jogo
        # entre os vencedores de grupo — isso é mata-mata real.
        _lr_lower = _lr.lower()
        _is_knockout = any(kw in _lr_lower for kw in (
            "semi-final", "quarter-final", "quarter final", "round of",
            "knockout", "mata-mata", "oitavas", "quartas", "semifinal",
            "eliminação", "1/4", "1/8", "1/16",
        )) or (_is_copa and _lr_lower in ("final",)) or (
            _n_grupos > 1 and _lr_lower in ("final", "championship", "grand final")
        )
        _is_group = any(kw in _lr_lower for kw in (
            "group", "grupo", "fase de grupos", "fase de grupo",
        ))

        st.markdown(f"**🏆 {_liga}** · `{_lr or '—'}`")

        _flags = []
        if _is_copa:
            _flags.append("⚠️ COPA / TORNEIO")
        if not p.get("cobertura_ok", True):
            _flags.append("⚠️ DADOS PARCIAIS")
        if p.get("cal_marginal", False):
            _flags.append("⚠️ COBERTURA BAIXA (<40j)")
        if _flags:
            st.warning("  ·  ".join(_flags))

        if _is_knockout:
            st.info("🔄 **Fase eliminatória** — verifique o resultado do 1º jogo se aplicável")
        elif _is_group:
            st.caption("📋 Fase de grupos — considere a situação de classificação de cada time")

        # ── Bloco 2: Posição na tabela (standings) ──────────────────
        if _l_id_ctx:
            _home_row, _away_row, _home_gi, _away_gi = None, None, None, None
            for _gi, _grp in enumerate(_stds):
                for _row in _grp:
                    _tid = _row.get("team", {}).get("id")
                    if _tid == p.get("home_id"):
                        _home_row, _home_gi = _row, _gi
                    elif _tid == p.get("away_id"):
                        _away_row, _away_gi = _row, _gi
            if _home_row or _away_row:
                st.divider()
                _is_grp_copa = len(_stds) > 1
                _form_icons  = {"W": "🟢", "D": "🟡", "L": "🔴"}
                _desc_ignorar = {"main round", "main group", "regular season", ""}
                st.markdown("**📊 Posição na tabela**")
                _sc1, _sc2 = st.columns(2)
                for _scol, _row, _gi_val, _tname in (
                    (_sc1, _home_row, _home_gi, home_name),
                    (_sc2, _away_row, _away_gi, away_name),
                ):
                    with _scol:
                        if _row:
                            _rk   = _row.get("rank", "?")
                            _pts  = _row.get("points", 0)
                            _gd   = _row.get("goalsDiff", 0)
                            _form = _row.get("form", "")
                            _all  = _row.get("all", {})
                            _pld  = _all.get("played", 0)
                            _w    = _all.get("win",  0)
                            _d    = _all.get("draw", 0)
                            _l    = _all.get("lose", 0)
                            _gf   = (_all.get("goals") or {}).get("for",     0)
                            _ga   = (_all.get("goals") or {}).get("against", 0)
                            _grp_str = f" · Grupo {chr(65+_gi_val)}" if _is_grp_copa and _gi_val is not None else ""
                            st.markdown(f"**{_tname}**")
                            st.markdown(f"**{_rk}º lugar**{_grp_str} · **{_pts} pontos** · Saldo de gols: {_gd:+d}")
                            st.markdown(f"{_pld} jogos jogados: **{_w}** vitórias · **{_d}** empates · **{_l}** derrotas")
                            st.markdown(f"Gols: **{_gf}** marcados · **{_ga}** sofridos")
                            if _form:
                                _fi = "".join(_form_icons.get(c, c) for c in _form[-5:])
                                st.markdown(
                                    f"Últimas 5 partidas: {_fi}  \n"
                                    f"<small>🟢 vitória &nbsp;·&nbsp; 🟡 empate &nbsp;·&nbsp; 🔴 derrota</small>",
                                    unsafe_allow_html=True,
                                )
                            _desc = _row.get("description", "")
                            if _desc and _desc.lower() not in _desc_ignorar:
                                st.markdown(f"📌 _{_desc}_")
                        else:
                            st.markdown(f"*{_tname[:20]} — posição não disponível*")

        # ── Bloco 3: Forma + H2H (só se tiver dados) ────────────────
        if _has_form or _has_h2h:
            st.divider()
            col_h, col_a = st.columns(2)
            with col_h:
                st.markdown(f"**{home_name}**")
                if _h_n5 >= 3:
                    _hw  = int(_ctx.get("h_wins_last5",    0))
                    _hd  = int(_ctx.get("h_draws_last5",   0))
                    _hl  = int(_ctx.get("h_losses_last5",  0))
                    _hgf = int(_ctx.get("h_gf_last5",      0))
                    _hga = int(_ctx.get("h_ga_last5",      0))
                    _hws = int(_ctx.get("h_win_streak",    0))
                    _hsm = int(_ctx.get("h_sem_marcar",    0))
                    _hcs = int(_ctx.get("h_cs_streak_casa",0))
                    st.markdown(
                        f"Últimos **{_h_n5} jogos:** **{_hw}** vitórias · **{_hd}** empates · **{_hl}** derrotas  \n"
                        f"{_hgf} gols marcados · {_hga} gols sofridos"
                    )
                    if _hws >= 3:
                        st.success(f"🔥 {_hws} vitórias seguidas")
                    elif _hws >= 2:
                        st.info(f"📈 {_hws} vitórias seguidas")
                    if _hsm >= 2:
                        st.warning(f"❌ Seca: {_hsm}j sem marcar")
                    if _hcs >= 2:
                        st.success(f"🧱 {_hcs} clean sheets em casa")
                else:
                    st.markdown("*Sem dados de forma disponíveis*")
            with col_a:
                st.markdown(f"**{away_name}**")
                if _a_n5 >= 3:
                    _aw  = int(_ctx.get("a_wins_last5",   0))
                    _ad  = int(_ctx.get("a_draws_last5",  0))
                    _al  = int(_ctx.get("a_losses_last5", 0))
                    _agf = int(_ctx.get("a_gf_last5",     0))
                    _aga = int(_ctx.get("a_ga_last5",     0))
                    _aws = int(_ctx.get("a_win_streak",   0))
                    _asm = int(_ctx.get("a_sem_marcar",   0))
                    st.markdown(
                        f"Últimos **{_a_n5} jogos:** **{_aw}** vitórias · **{_ad}** empates · **{_al}** derrotas  \n"
                        f"{_agf} gols marcados · {_aga} gols sofridos"
                    )
                    if _aws >= 3:
                        st.success(f"🔥 {_aws} vitórias seguidas")
                    elif _aws >= 2:
                        st.info(f"📈 {_aws} vitórias seguidas")
                    if _asm >= 2:
                        st.warning(f"❌ Seca: {_asm}j sem marcar")
                else:
                    st.markdown("*Sem dados de forma disponíveis*")
            if _has_h2h:
                _h2h_h = int(_ctx.get("h2h_h_wins", 0))
                _h2h_a = int(_ctx.get("h2h_a_wins", 0))
                _h2h_e = _h2h_tot - _h2h_h - _h2h_a
                st.markdown(
                    f"**⚔️ Confronto direto ({_h2h_tot} partidas):** "
                    f"{home_name} **{_h2h_h}** vitórias · **{_h2h_e}** empates · "
                    f"**{_h2h_a}** vitórias {away_name}"
                )

        # ── Bloco 3: Argumentos (só se tiver regras disparadas) ──────
        if _has_heur:
            st.divider()
            st.markdown("**Argumentos do modelo**")
            _pos_kw = ("fortaleza", "dominan", "vitórias", "vit.", "rolo", "letal",
                       "compressor", "superior", "h2h favorece", "ρ negativo",
                       "goleadora", "sólid", "mando invicto", "zebra", "ataque bem",
                       "clean sheets", "liga propensa ao empate reforça", "alta convicção",
                       "favorecido", "muito alto", "muito baixo")
            _neg_kw = ("queda", "risco", "frágil", "inoperante", "seca", "desequilíbrio",
                       "cansado", "contradiz", "ameaça", "sem marcar", "poucos jogos",
                       "ilusória", "zona cinzenta", "limiar", "baixo potencial",
                       "fragilizado", "raramente marca", "incerteza", "penaliza")
            for _part in _parts:
                _pl = _part.lower()
                if any(kw in _pl for kw in _pos_kw):
                    st.success(f"✅ {_part}")
                elif any(kw in _pl for kw in _neg_kw):
                    st.warning(f"❌ {_part}")
                else:
                    st.info(f"🔬 {_part}")

        # ── Bloco 4: Scout stats (só se tiver dados) ─────────────────
        if _has_scout:
            st.divider()
            st.markdown("**Scout stats** (médias da temporada)")
            _tbl = [
                f"| Estatística | {home_name[:15]} | {away_name[:15]} |",
                "|:---|:---:|:---:|",
            ]
            for _slabel, _hv, _av in _scout_rows:
                _hv_s = f"{_hv:.1f}" if _hv is not None else "—"
                _av_s = f"{_av:.1f}" if _av is not None else "—"
                _tbl.append(f"| {_slabel} | {_hv_s} | {_av_s} |")
            st.markdown("\n".join(_tbl))

        # Aviso compacto único quando não há dados locais (forma + scout + heur)
        if not _has_form and not _has_h2h and not _has_heur and not _has_scout:
            st.caption("📂 Histórico local não carregado — forma, scout e H2H requerem dados locais")

        # ── Bloco 5: Modelo D-C (sempre renderiza) ───────────────────
        st.divider()
        _lam  = float(p.get("xg_lam")   or _ctx.get("alpha_h") or 1.0)
        _mu   = float(p.get("xg_mu")    or _ctx.get("alpha_a") or 1.0)
        _xgt  = float(p.get("xg_total") or (_lam + _mu))
        _rho  = float(_ctx.get("rho", -0.05))
        _nj_h = int(_ctx.get("n_jogos_h", 0))
        _nj_a = int(_ctx.get("n_jogos_a", 0))
        _media = float(_ctx.get("media_liga_gols", 0.0))
        st.markdown("**Modelo Dixon-Coles**")
        _dm1, _dm2, _dm3 = st.columns(3)
        _dm1.metric("λ (casa)", f"{_lam:.2f}")
        _dm2.metric("μ (fora)", f"{_mu:.2f}")
        _dm3.metric("xG total", f"{_xgt:.2f}")
        _dm1.markdown(f"ρ = **{_rho:.3f}**{'  · propensão a empate' if _rho < -0.08 else ''}")
        _dm2.markdown(f"Calibrado em **{_nj_h}** jogos (casa) / **{_nj_a}** jogos (fora)")
        _dm3.markdown(f"Média de gols da liga: **{_media:.2f}** por jogo" if _media > 0 else "—")


@st.cache_data(ttl=21600, show_spinner=False)
def _buscar_standings_cached(league_id: int, season: int) -> list:
    """
    Retorna standings da liga/copa. Cache de 6h (21600s) para refletir rodadas do dia.
    Retorna lista de grupos: ligas domésticas = 1 grupo; Copas com grupos = N grupos.
    Custo: 1 crédito por liga por cache miss (~4×/dia máximo).
    """
    try:
        return dm.buscar_standings(league_id, season)
    except Exception:
        return []


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

# ── Session state: Gestão de Risco (persiste entre abas e reloads) ──────────
_risk_defaults = {
    "risk_piso_kelly":        1.0,
    "risk_odd_min":           1.70,
    "risk_prob_min":          45.0,
    "risk_teto_pct_pct":      10,
    "risk_limite_div":        20,
    "risk_limite_risco_pct":  20,   # % máximo da banca exposta por dia (portfólio Kelly)
    "risk_ligas_bloqueadas":  [],
}
for _rk, _rv in _risk_defaults.items():
    if _rk not in st.session_state:
        st.session_state[_rk] = _rv

with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO V3")
    _backend_label = "☁️ Gist" if _backend_tipo == "gist" else "⚠️ JSONBin"
    st.caption(f"Motor: Dixon-Coles (MLE) · v5-kelly-portfolio · 72 ligas · {_backend_label}")

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

    # ── BANCA — input direto do saldo atual para cálculo de stake ────────
    st.markdown("### 💰 Banca")
    banca_atual = st.number_input(
        "Saldo Atual (R$)",
        value=float(st.session_state.get("_saldo_atual", banco.banca_inicial or 30.0)),
        min_value=0.01,
        step=0.50,
        help="Informe o saldo atual da sua conta. Salvo automaticamente no cloud ao alterar.",
    )
    st.session_state["_saldo_atual"] = banca_atual
    # Persiste no cloud para sobreviver ao reload/restart do Streamlit Cloud
    if abs(banca_atual - float(banco.banca_inicial or 0.0)) > 0.01:
        banco.banca_inicial = banca_atual
        st.session_state["banco"] = banco
        try:
            ok = dm.salvar_banco(banco)
            if not ok:
                st.warning("⚠️ Banca salva localmente, mas falhou no cloud. Use o botão abaixo.")
        except Exception as _e:
            dm.ultimo_save_jsonbin_ok = False
            st.warning(f"⚠️ Erro ao salvar banca: {_e}")

    # ── Novo Gist auto-criado — mostra ID para atualizar nos secrets ────
    _novo_gist = getattr(getattr(dm, "jsonbin", None), "novo_gist_id", "")
    if _novo_gist:
        st.warning(
            "⚠️ **Novo Gist criado automaticamente!**\n\n"
            "O Gist anterior não foi encontrado (404). Um novo foi criado. "
            "Para persistir após restart, atualize o secret `GIST_ID` no Streamlit Cloud:\n\n"
            f"```\nGIST_ID = \"{_novo_gist}\"\n```\n\n"
            "_(App → Settings → Secrets → editar GIST_ID → Save)_"
        )

    # ── Status de sincronização com o cloud ──────────────────────────────
    _cloud_ok  = dm.ultimo_save_jsonbin_ok
    _cloud_err = getattr(dm, "ultimo_save_erro", "")
    if _cloud_ok is False:
        _msg_erro = f"\n\n`{_cloud_err}`" if _cloud_err else ""
        st.error(
            "☁️❌ **Última gravação no cloud falhou.**\n\n"
            "Dados desta sessão podem ser perdidos ao reiniciar. "
            "Use o botão abaixo para tentar novamente." + _msg_erro
        )
    elif _cloud_ok is None:
        st.caption("☁️⏳ Não salvo nesta sessão — use o botão abaixo para sincronizar.")
    else:
        st.caption("☁️✅ Cloud sincronizado")
    if st.button(
        "💾 Salvar no Cloud" if _cloud_ok else "💾 Forçar Salvar no Cloud",
        use_container_width=True,
        help="Sincroniza banca, calibrações e picks com o cloud imediatamente.",
    ):
        with st.spinner("Sincronizando com cloud..."):
            try:
                _save_ok = dm.salvar_banco(banco)
            except Exception as _exc_save:
                _save_ok = False
                dm.ultimo_save_jsonbin_ok = False
                dm.ultimo_save_erro = f"{type(_exc_save).__name__}: {_exc_save}"
        if _save_ok:
            st.toast("✅ Cloud sincronizado com sucesso!", icon="☁️")
            st.rerun()
        else:
            _err_msg = getattr(dm, "ultimo_save_erro", "") or "erro desconhecido"
            st.error(f"❌ Falha ao sincronizar: `{_err_msg}`")

    # ── Limpar cache (força recriação do manager com secrets atuais) ─
    if st.button("🔄 Reiniciar Manager", use_container_width=True,
                 help="Força recriação do DadosManager com os secrets atuais. Use após trocar GIST_ID."):
        get_manager.clear()
        st.session_state.clear()
        st.rerun()

    # ── Teste de conexão Gist (diagnóstico) ──────────────────────────
    if st.button("🔍 Testar Conexão Cloud", use_container_width=True,
                 help="Envia payload mínimo ao cloud para diagnosticar falhas de rede/auth."):
        import json as _json
        # Mostra o que o app lê dos secrets (sem expor valores completos)
        _gh_tok = st.secrets.get("GITHUB_TOKEN", "")
        _gh_id  = st.secrets.get("GIST_ID", "")
        st.write(f"**Secrets lidos:**")
        st.write(f"- GITHUB_TOKEN: `{'✅ presente (' + _gh_tok[:8] + '...)' if _gh_tok else '❌ AUSENTE'}`")
        st.write(f"- GIST_ID: `{'✅ ' + _gh_id if _gh_id else '❌ AUSENTE'}`")
        st.write(f"- Backend detectado: `{_backend_tipo}`")
        st.divider()
        _client = getattr(dm, "jsonbin", None)
        if _client is None:
            st.error("❌ Sem client de cloud configurado.")
        else:
            _url    = getattr(_client, "url", "?")
            _hdrs   = getattr(_client, "headers", {})
            _fname  = getattr(_client, "FILENAME", "banco_barrios.json")
            st.caption(f"URL usada: `{_url}`")
            with st.spinner("Testando PATCH..."):
                try:
                    import requests as _req
                    _tb = _json.dumps({"files": {_fname: {"content": '{"_ping":true}'}}}).encode("utf-8")
                    _r  = _req.patch(_url, headers=_hdrs, data=_tb, timeout=15)
                    if _r.status_code == 200:
                        st.success(f"✅ HTTP {_r.status_code} — Cloud OK!")
                    else:
                        st.error(f"❌ HTTP {_r.status_code}: {_r.text[:300]}")
                except Exception as _e:
                    st.error(f"❌ Exceção: `{type(_e).__name__}: {_e}`")

    st.divider()

    # ── Configurações Kelly ───────────────────────────────────────────
    with st.expander("⚙️ Gestão de Risco"):
        piso_kelly   = st.number_input("Piso de stake (R$)", min_value=0.5, step=0.5, key="risk_piso_kelly")
        teto_pct     = st.slider("Teto % da banca", 5, 25, key="risk_teto_pct_pct") / 100
        odd_min_save = st.number_input("Odd mínima global (Gols + Resultados)", min_value=1.01, step=0.05, key="risk_odd_min")
        prob_min     = st.number_input("Prob. mínima do modelo (%)", min_value=0.0, max_value=99.0, step=5.0, key="risk_prob_min")
        # 20pp: limiar documentado no paper Dixon-Coles e nos bugs do V6.1.
        limite_div   = st.slider("Anomalia se divergência >", 10, 20, key="risk_limite_div")
        st.divider()
        # Kelly de Portfólio: distribui a banca entre TODAS as entradas do dia.
        # Em dias com poucas entradas, cada pick recebe stake maior.
        # Em dias com muitas, os stakes são escalonados para o limite.
        st.caption("**📊 Kelly Portfólio**")
        limite_risco_pct = st.slider(
            "Risco diário máximo (% da banca)",
            min_value=5, max_value=30,
            key="risk_limite_risco_pct",
            help=(
                "Caps o total exposto no dia. Ex.: 20% com banca R$37 = máx R$7,40 no dia. "
                "Com poucas entradas cada stake sobe. Com muitas, cada stake cai proporcionalmente."
            ),
        ) / 100.0
        st.divider()
        st.caption("**🚫 Bloquear ligas do scanner** — picks dessas ligas são suprimidos em Gols e Resultados.")
        ligas_bloqueadas_sel = st.multiselect(
            "Ligas bloqueadas",
            options=list(LIGAS_SUPORTADAS.keys()),
            format_func=lambda x: LIGAS_SUPORTADAS[x],
            key="risk_ligas_bloqueadas",
        )

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
    season        = detectar_temporada_atual()
    st.caption(f"📅 Temporada detectada automaticamente: **{season}** (muda em julho/agosto)")


# =========================================================================
# 5. CABEÇALHO
# =========================================================================

st.title("QG Barrios PRO V3")

n_calibradas = len(banco.params_ligas)

col_h1, col_h2 = st.columns(2)
col_h1.metric("Ligas calibradas", f"{n_calibradas}/{len(_ligas_ativas)}")
col_h2.metric("Banca atual", f"R$ {banca_atual:.2f}")

# ── Aviso de sessão potencialmente stale ─────────────────────────────────────
# Se o app acabou de carregar (dm nunca salvou nesta sessão), mostra um lembrete
# para o usuário confirmar que os dados estão corretos antes de operar.
if dm.ultimo_save_jsonbin_ok is None:
    st.warning(
        "⚠️ **Sessão nova carregada do cloud.** "
        f"Verifique se a banca (R$ {banca_atual:.2f}) e as {n_calibradas} ligas calibradas "
        "estão corretas. Se os valores parecerem antigos, ajuste a banca no sidebar e clique "
        "**'💾 Salvar no Cloud'** para sincronizar. "
        "_(Este aviso some após o primeiro save da sessão.)_"
    )

st.divider()


# =========================================================================
# 6. ABAS PRINCIPAIS
# =========================================================================

tab_analise, tab_calibracao = st.tabs([
    "🎯 Análise Diária",
    "⚙️ Calibração de Ligas",
])


# =========================================================================
# 6.1 ABA CALIBRAÇÃO — botão único, sem incremental
# =========================================================================

with tab_calibracao:
    _n_ligas        = len(_ligas_ativas)   # inclui ligas calibradas manualmente
    _custo_por_liga = CUSTO_ESTIMADO_HISTORICO_LIGA + CUSTO_ESTIMADO_XG_LIGA

    st.markdown(f"### Status das ligas ({_n_ligas} configuradas)")
    st.caption(
        f"🟢 **Fresca** = calibrada recentemente · "
        f"🟡 **Velha** = precisa atualizar · "
        f"❌ **Nunca calibrada** = ainda sem dados. "
        f"xG blend peso={PESO_XG_PRODUCAO}."
    )

    # Tabela de status
    rows_status = []
    for league_id, nome in _ligas_ativas.items():
        params_d   = banco.params_ligas.get(str(league_id), {})
        _tem_times = bool(params_d.get("times")) and params_d.get("n_jogos_calibracao", 0) > 0
        n_times    = len(params_d.get("times", {})) if _tem_times else 0
        n_jogos    = params_d.get("n_jogos_calibracao", 0)

        # Calcula data do último jogo a partir do raio-x por time
        raio_x          = params_d.get("raio_x_times", {})
        ultimo_jogo_str = "—"
        dias_sem_jogo   = None
        if raio_x:
            _datas = [
                str(rx.get("ultimo_jogo", ""))[:10]
                for rx in raio_x.values()
                if rx.get("ultimo_jogo") and str(rx.get("ultimo_jogo"))[:4].isdigit()
            ]
            if _datas:
                try:
                    ultimo_jogo_str = max(_datas)
                    dias_sem_jogo   = (dt.date.today() - dt.date.fromisoformat(ultimo_jogo_str)).days
                except Exception:
                    pass

        # Status da safra: leva em conta tanto a idade da calibração quanto a data do último jogo
        if not params_d or not _tem_times:
            if league_id in LIGAS_COPA_MUNDO:
                status = "🏆 Aguardando torneio"  # Copa/Eliminatórias sem dados na API — normal
            else:
                status = "❌ Nunca calibrada"
        else:
            try:
                data_cal = dt.datetime.fromisoformat(params_d.get("calibrado_em", ""))
                dias_cal = (dt.datetime.now() - data_cal).days
                if dias_sem_jogo is not None:
                    if dias_sem_jogo > 90:
                        status = f"❄️ Finalizada ({dias_sem_jogo}d sem jogo)"
                    elif dias_sem_jogo > 45:
                        status = f"⏸️ Congelada ({dias_sem_jogo}d sem jogo)"
                    elif dias_cal >= INTERVALO_RECALIBRACAO_DIAS:
                        status = f"🟡 Velha ({dias_cal}d)"
                    else:
                        status = f"🟢 Fresca ({dias_cal}d)"
                else:
                    status = (
                        f"🟡 Velha ({dias_cal}d)"
                        if dias_cal >= INTERVALO_RECALIBRACAO_DIAS
                        else f"🟢 Fresca ({dias_cal}d)"
                    )
            except Exception:
                status = "❌ Nunca calibrada"

        # Qualidade do calibrador baseada no volume de jogos usados no MLE
        n_jogos_temp = sum(
            1 for rx in raio_x.values() if rx.get("na_temporada_atual")
        ) if raio_x else 0
        if n_jogos >= 80:
            qualidade = "✅ Boa"
        elif n_jogos >= 40:
            qualidade = "🟡 Mínima"
        elif n_jogos >= 20:
            qualidade = "🔴 Marginal"
        else:
            qualidade = "❌ —"

        rows_status.append({
            "Liga":        f"{nome} (ID {league_id})",
            "Status":      status,
            "Último Jogo": ultimo_jogo_str,
            "Times":       n_times,
            "Jogos MLE":   n_jogos,
            "Temp. atual": n_jogos_temp if n_jogos_temp > 0 else "—",
            "Qualidade":   qualidade,
        })

    st.dataframe(rows_status, use_container_width=True, hide_index=True)

    # ── Resumo de status ──────────────────────────────────────────────
    _n_frescas     = sum(1 for r in rows_status if r["Status"].startswith("🟢"))
    _n_velhas      = sum(1 for r in rows_status if r["Status"].startswith("🟡"))
    _n_congeladas  = sum(1 for r in rows_status if r["Status"].startswith(("❄️", "⏸️")))
    _n_nunca       = sum(1 for r in rows_status if r["Status"].startswith("❌"))
    _n_aguardando  = sum(1 for r in rows_status if r["Status"].startswith("🏆"))
    _partes_res = []
    if _n_frescas:    _partes_res.append(f"🟢 {_n_frescas} frescas")
    if _n_velhas:     _partes_res.append(f"🟡 {_n_velhas} velhas")
    if _n_congeladas: _partes_res.append(f"⏸️ {_n_congeladas} congeladas/finalizadas")
    if _n_nunca:      _partes_res.append(f"❌ {_n_nunca} nunca calibradas")
    if _n_aguardando: _partes_res.append(f"🏆 {_n_aguardando} aguardando torneio")
    st.info(
        "**Resumo:** " + " · ".join(_partes_res) + "  \n"
        "💡 **Custo real de atualização** = apenas os jogos novos × 2 créditos (delta fetch) — "
        "não o bootstrap completo. Use **Passo 1** abaixo para ver o custo exato antes de gastar."
    )
    if _n_aguardando:
        st.caption(
            f"🏆 {_n_aguardando} liga(s) Copa/Eliminatórias marcadas como **Aguardando torneio** — "
            "sem dados disponíveis na API para a season atual. Isso é normal fora do período de jogos. "
            "Para seleções, use a seção **Bootstrap Copa 2026** abaixo."
        )

    # ── Trava de Custo — Calibrar TODAS (2 fases) ────────────────────
    st.markdown("#### 🔄 Atualizar Todas as Ligas")
    st.caption(
        f"**Passo 1** — lista fixtures novas ({_n_ligas} ligas × 2 = ~{_n_ligas * 2} créditos). "
        "Mostra quantos jogos novos há por liga e o custo exato de xG. "
        "**Passo 2** — confirma e executa: baixa xG dos jogos novos + recalibra MLE. "
        "⚠️ Ligas inativas (0 novos jogos) são marcadas como frescas **sem gastar créditos**."
    )

    # ── FASE 1: botão de análise ──────────────────────────────────────
    if st.button(
        f"🔍 Passo 1 — Analisar Custo de Download ({_n_ligas} ligas)",
        use_container_width=True,
    ):
        with st.spinner(
            f"Consultando listas de fixtures para {_n_ligas} ligas "
            f"(~{_n_ligas * 2} créditos)…"
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
        custo    = preview["custo_estimado_creditos"]
        saldo_ok = custo <= saldo - SALDO_MINIMO_EMERGENCIA

        # Categoriza cada liga em 3 grupos baseado no resultado do delta:
        #   inativas  — calibradas + 0 novos → renova timestamp grátis
        #   com_novos — tem jogos novos → recalibra via MLE (gasta créditos)
        #   aguardando — nunca calibradas + 0 API → torneio ainda não iniciou
        _inativas:   list[int] = []
        _com_novos:  list[int] = []
        _aguardando: list[int] = []
        for _li in preview["ligas"]:
            _lid = _li["league_id"]
            _nn  = _li["n_novos_liga"]
            _pd  = banco.params_ligas.get(str(_lid), {})
            _tem = bool(_pd.get("times")) and _pd.get("n_jogos_calibracao", 0) > 0
            if _nn > 0:
                _com_novos.append(_lid)
            elif _tem:
                _inativas.append(_lid)
            else:
                _aguardando.append(_lid)

        # ── Mensagem de resumo ──────────────────────────────────────────
        if _com_novos:
            if saldo_ok:
                st.warning(
                    f"📊 **{len(_com_novos)} liga(s) com jogos novos** — "
                    f"custo xG: **{custo} créditos** · saldo: {saldo} créditos"
                    + (f" · {len(_inativas)} liga(s) inativas serão marcadas grátis" if _inativas else "")
                )
            else:
                st.error(
                    f"🔴 Saldo insuficiente. Necessários {custo + SALDO_MINIMO_EMERGENCIA} "
                    f"({custo} xG + {SALDO_MINIMO_EMERGENCIA} buffer), saldo={saldo}."
                )
        else:
            _partes_msg = []
            if _inativas:
                _partes_msg.append(
                    f"**{len(_inativas)} liga(s) inativas** (entre fases — serão marcadas como ✅ Frescas grátis)"
                )
            if _aguardando:
                _partes_msg.append(
                    f"**{len(_aguardando)} liga(s) aguardando início do torneio** (sem dados na API)"
                )
            st.success(
                "✅ **Cache de fixtures em dia** — nenhum jogo novo para baixar. "
                + (" · ".join(_partes_msg) if _partes_msg else "Todas as ligas estão atualizadas.")
            )
            if _inativas:
                st.info(
                    f"💡 Clique em Passo 2 para marcar {len(_inativas)} liga(s) inativa(s) "
                    "como Frescas sem gastar créditos de xG."
                )

        # ── Tabela de detalhes por liga ────────────────────────────────
        rows_delta = []
        for liga_info in preview["ligas"]:
            _lid  = liga_info["league_id"]
            _nn   = liga_info["n_novos_liga"]
            if _lid in _inativas:
                _acao = "⏸️ inativa"
            elif _lid in _aguardando:
                _acao = "⚠️ aguardando"
            elif _nn > 150:
                _acao = "🔴 bootstrap"
            elif _nn > 0:
                _acao = "🟡 atualizar"
            else:
                _acao = "✅"
            rows_delta.append({
                "Liga":           liga_info["nome"],
                "Novos fixtures": _nn,
                "Créditos xG":    _nn * CUSTO_ESTIMADO_XG_FIXTURE,
                "Seasons":        " | ".join(
                    f"s{s['season']}: +{s['n_novos']} ({s['n_cache']} cache)"
                    if s.get("erro") is None else f"s{s['season']}: ERRO"
                    for s in liga_info["seasons"]
                ),
                "Ação": _acao,
            })

        with st.expander("📋 Ver detalhes por liga"):
            st.dataframe(rows_delta, use_container_width=True, hide_index=True)

        # ── FASE 2: botão de confirmação ──────────────────────────────
        _pode_tocar    = bool(_inativas)
        _pode_calibrar = bool(_com_novos) and saldo_ok
        _alguma_acao   = _pode_tocar or _pode_calibrar

        if _pode_calibrar and _pode_tocar:
            _btn_lbl = (
                f"✅ Passo 2 — Baixar xG ({custo} créditos) + "
                f"marcar {len(_inativas)} inativa(s) como Frescas"
            )
        elif _pode_calibrar:
            _btn_lbl = f"✅ Passo 2 — Confirmar Download de xG ({custo} créditos) e Calibrar"
        elif _pode_tocar:
            _btn_lbl = f"🟢 Passo 2 — Marcar {len(_inativas)} liga(s) inativa(s) como Frescas (0 créditos)"
        else:
            _btn_lbl = "⚠️ Nenhuma ação disponível"

        if st.button(
            _btn_lbl,
            type="primary",
            use_container_width=True,
            disabled=not _alguma_acao,
            help=(
                "Renova timestamps de ligas inativas (grátis) e recalibra ligas com novos jogos."
                if _alguma_acao
                else "Sem créditos suficientes ou apenas ligas aguardando início do torneio."
            ),
        ):
            # Armazena _pode_calibrar junto ao snapshot para garantir que a execução
            # respeite o que foi mostrado ao usuário: "0 créditos" = só toca inativas.
            st.session_state["delta_snapshot"] = {**preview, "_pode_calibrar": _pode_calibrar}
            st.session_state["delta_confirmado"] = True
            st.session_state.pop("delta_preview", None)
            st.session_state.pop("delta_cats", None)
            st.rerun()

    # ── Executa calibração após confirmação ───────────────────────────
    if st.session_state.get("delta_confirmado"):
        st.session_state.pop("delta_confirmado", None)
        snapshot  = st.session_state.pop("delta_snapshot", {})

        # Re-deriva categorias a partir do snapshot completo (evita race de session_state)
        inativas:  list[int] = []
        com_novos: list[int] = []
        aguardando: list[int] = []
        for _li in snapshot.get("ligas", []):
            _lid = _li["league_id"]
            _nn  = _li["n_novos_liga"]
            _pd  = banco.params_ligas.get(str(_lid), {})
            _tem_cal = bool(_pd.get("calibrado_em"))
            if _nn > 0:
                com_novos.append(_lid)
            elif _tem_cal:
                # Liga calibrada + 0 novos → renova timestamp (grátis, sem MLE)
                inativas.append(_lid)
            else:
                # Nunca calibrada + 0 novos: verifica se a API tem ALGUM dado histórico.
                # Se n_api == 0 o torneio ainda não iniciou (Copa del Rey em fase inicial,
                # eliminatória fora de janela, etc.) — não desperdiça créditos tentando MLE.
                _api_total = sum(s.get("n_api", 0) for s in _li.get("seasons", []))
                if _api_total > 0:
                    com_novos.append(_lid)   # tem dados históricos → bootstrap
                else:
                    aguardando.append(_lid)  # torneio não iniciou → skip silencioso

        # Fallback: snapshot perdido (session reiniciada entre Passo 1 e 2) → recalibra tudo
        if not snapshot.get("ligas"):
            com_novos = list(_ligas_ativas.keys())

        # SEGURANÇA: se o botão foi exibido como "0 créditos" (saldo insuficiente para ligas
        # com novos jogos), garantir que NÃO tentamos calibrar — apenas toca inativas.
        # _pode_calibrar=False quando o saldo não cobria o custo xG mostrado no Passo 1.
        if not snapshot.get("_pode_calibrar", True):
            com_novos = []

        total_ops    = len(inativas) + len(com_novos)
        progress_bar = st.progress(0)
        status_box   = st.empty()
        erros: list[str] = []
        timeouts: list[str] = []
        tocadas = 0
        op_idx  = 0
        _cal_detalhes: list[str] = []   # detalhes por liga calibrada com sucesso

        # Passo 2a: renova timestamps das ligas inativas (sem MLE, sem créditos)
        for lid in inativas:
            nome = _ligas_ativas.get(lid, f"Liga {lid}")
            status_box.info(f"[{op_idx+1}/{total_ops}] ⏸️ Renovando timestamp: **{nome}**…")
            try:
                dm.tocar_timestamp_liga(lid)
                tocadas += 1
            except Exception as e:
                erros.append(f"**{nome}** (touch): {e}")
            op_idx += 1
            progress_bar.progress(op_idx / max(total_ops, 1))

        # Passo 2b: recalibra ligas com jogos novos (MLE completo)
        for lid in com_novos:
            nome = _ligas_ativas.get(lid, f"Liga {lid}")
            status_box.info(f"[{op_idx+1}/{total_ops}] ⚙️ Calibrando **{nome}**…")
            try:
                _p = dm.obter_params_liga(lid, season, forcar_recalibracao=True)
                _rx = getattr(_p, "raio_x_times", {}) or {}
                _ultimo_j = max(
                    (str(v.get("ultimo_jogo", ""))[:10]
                     for v in _rx.values() if v.get("ultimo_jogo")),
                    default="—",
                )
                _cal_detalhes.append(
                    f"**{nome}** T{_p.season} · {_p.n_jogos_calibracao} jogos · último: {_ultimo_j}"
                )
            except CreditosInsuficientesError as e:
                status_box.error(f"🔴 Saldo insuficiente. Parando em **{nome}**: {e}")
                break
            except TimeoutError:
                timeouts.append(nome)
            except Exception as e:
                erros.append(f"**{nome}**: {e}")
            op_idx += 1
            progress_bar.progress(op_idx / max(total_ops, 1))

        status_box.empty()

        # Relatório final detalhado
        if timeouts:
            st.warning(
                f"⏱️ {len(timeouts)} liga(s) com timeout (MLE > {TIMEOUT_CALIBRACAO_SEGUNDOS}s):\n"
                + "\n".join(f"• {n}" for n in timeouts)
            )
        if erros:
            st.warning("⚠️ Falhas:\n" + "\n".join(f"• {e}" for e in erros))
        if aguardando:
            st.info(
                f"⏳ {len(aguardando)} liga(s) aguardando início (sem dados na API — créditos preservados):\n"
                + "\n".join(f"• {_ligas_ativas.get(l, str(l))}" for l in aguardando)
            )
        if tocadas:
            st.info(f"⏸️ {tocadas} liga(s) inativa(s) marcadas como Frescas (0 créditos).")
        if _cal_detalhes:
            st.success(
                f"✅ {len(_cal_detalhes)} liga(s) calibrada(s):\n"
                + "\n".join(f"• {d}" for d in _cal_detalhes)
            )
        if not _cal_detalhes and not erros and not timeouts and not tocadas and not aguardando:
            st.info("Nenhuma ação executada.")

        try:
            st.session_state["banco"] = dm.banco_em_memoria()
        except Exception:
            st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
        st.rerun()

    # ── Copa do Mundo 2026 — Bootstrap de Calibração ─────────────────
    with st.expander("🌍 Copa do Mundo 2026 — Bootstrap", expanded=False):
        _wc_params    = banco.params_ligas.get("1", {})
        _wc_calibrado = bool(_wc_params.get("times"))
        _is_bootstrap = "_bootstrap" in str(_wc_params.get("calibrado_em", ""))
        _wc_n_jogos   = _wc_params.get("n_jogos_calibracao", 0)
        _wc_n_times   = len(_wc_params.get("times", {}))

        if _wc_calibrado:
            _wc_tipo = " · ⚠️ Bootstrap pré-torneio" if _is_bootstrap else " · ✅ MLE do torneio"
            _wc_status_txt = (
                f"🟢 Calibrada ({_wc_n_jogos} jogos, {_wc_n_times} seleções){_wc_tipo}"
            )
        else:
            _wc_status_txt = "❌ Não calibrada — Bootstrap necessário antes de 11/06"

        st.info(
            f"**Status:** {_wc_status_txt}  \n"
            "O **Bootstrap** combina os jogos das 5 Eliminatórias (já no cache) para gerar "
            "alpha/beta iniciais de todas as seleções. `home_advantage` é forçado para **1.0** "
            "(campo neutro).  \n"
            "**Após cada rodada da Copa**, use Passo 1 → Passo 2 normalmente — o delta-fetch "
            "detecta os jogos novos e o MLE evolui automaticamente para dados do próprio torneio."
        )

        _eliminatorias_ids = {29: "CAF (África)", 30: "AFC (Ásia)",
                              31: "CONCACAF", 32: "UEFA", 34: "CONMEBOL"}
        _el_status_parts = []
        _n_el_ok = 0
        for _el_id, _el_nome in _eliminatorias_ids.items():
            _el_p  = banco.params_ligas.get(str(_el_id), {})
            _el_ok = bool(_el_p.get("times"))
            _el_n  = _el_p.get("n_jogos_calibracao", 0)
            if _el_ok:
                _el_status_parts.append(f"✅ {_el_nome} ({_el_n}j)")
                _n_el_ok += 1
            else:
                _el_status_parts.append(f"❌ {_el_nome}")
        st.caption("Eliminatórias: " + " · ".join(_el_status_parts))

        if _n_el_ok == 0:
            st.warning(
                "Nenhuma Eliminatória calibrada. Vá para **Calibrar ligas em lote**, "
                "selecione as Eliminatórias (IDs 29, 30, 31, 32, 34) e calibre primeiro."
            )

        _btn_tipo_wc = "primary" if not _wc_calibrado else "secondary"
        if st.button(
            "🌍 Executar Bootstrap Copa do Mundo 2026",
            use_container_width=True,
            type=_btn_tipo_wc,
            disabled=(_n_el_ok == 0),
            help="Combina Eliminatórias calibradas → alpha/beta das seleções → gamma=1.0. "
                 "Custo: 0 créditos (usa apenas cache local).",
        ):
            try:
                with st.spinner("Bootstrapping Copa do Mundo 2026 (0 créditos)…"):
                    _wc_res = dm.bootstrap_copa_mundo_2026(season=2026)
                st.success(
                    f"✅ Bootstrap concluído! {_wc_res.n_jogos_calibracao} jogos · "
                    f"{len(_wc_res.times)} seleções · "
                    f"gamma={_wc_res.home_advantage:.3f} (neutro)"
                )
                st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
                st.rerun()
            except Exception as _e_wc:
                st.error(f"Falha no bootstrap: {_e_wc}")

        st.markdown("---")
        st.markdown("#### 🔥 Bootstrap Forma Recente *(recomendado para Copa)*")
        st.info(
            "Ignora Eliminatórias históricas e calibra cada seleção pelos seus **últimos 20 jogos "
            "finalizados** (de qualquer competição).  \n"
            "Decay `xi=0.010` — jogo de 6 meses conta apenas 17%. Só os últimos 3–4 meses "
            "têm peso real.  \n"
            "**Custo:** ~1 cr. (lista de times) + 1 cr. × 32 seleções ≈ **33 créditos** no total."
        )
        _fr_is_bootstrap = "_forma_recente" in str(_wc_params.get("calibrado_em", ""))
        if _fr_is_bootstrap:
            st.success("✅ Forma Recente já executada — params ativos incluem dados recentes.")
        elif _wc_calibrado:
            st.warning("⚠️ Copa calibrada via Bootstrap Eliminatórias. Para melhor precisão, execute Forma Recente.")

        if st.button(
            "🔥 Bootstrap Forma Recente (~33 cr.)",
            use_container_width=True,
            type="primary" if not _fr_is_bootstrap else "secondary",
            key="btn_bootstrap_forma_recente",
            help="Busca os últimos 20 jogos de cada seleção qualificada e calibra com decay agressivo.",
        ):
            try:
                _fr_pb_area = st.empty()
                _fr_status  = st.empty()

                _fr_status.info("Buscando times da Copa 2026 na API (1 cr.)…")
                _fr_team_ids = dm.buscar_times_copa_2026(season=2026)
                _fr_total    = len(_fr_team_ids)

                if _fr_total == 0:
                    st.error(
                        "A API não retornou times para Copa 2026. "
                        "Tente novamente — os grupos podem ainda não estar publicados."
                    )
                    st.stop()

                _fr_records: dict[int, dict] = {}
                _fr_erros: list[str] = []

                for _fr_idx, _fr_tid in enumerate(_fr_team_ids):
                    _fr_pb_area.progress(
                        _fr_idx / _fr_total,
                        text=f"Buscando seleção {_fr_idx + 1}/{_fr_total} (ID {_fr_tid})…",
                    )
                    try:
                        _fr_df_t = dm.buscar_historico_time(_fr_tid, n=20)
                        for _, _fr_row in _fr_df_t.iterrows():
                            _fr_fid = int(_fr_row["fixture_id"])
                            if _fr_fid not in _fr_records:
                                _fr_records[_fr_fid] = {
                                    "fixture_id": _fr_fid,
                                    "home_id":    int(_fr_row["home_id"]),
                                    "away_id":    int(_fr_row["away_id"]),
                                    "home_goals": int(_fr_row["home_goals"]),
                                    "away_goals": int(_fr_row["away_goals"]),
                                    "date":       str(_fr_row["date"])[:10],
                                }
                    except Exception as _fr_e:
                        _fr_erros.append(str(_fr_tid))

                _fr_pb_area.progress(1.0, text="Calibrando modelo Copa 2026 (xi=0.010)…")

                _fr_df = pd.DataFrame(list(_fr_records.values()))
                _fr_res = dm.bootstrap_copa_forma_recente(_fr_df, season_copa=2026)

                _fr_pb_area.empty()
                _fr_status.empty()

                _aviso_erros = f" · ⚠️ {len(_fr_erros)} times sem dados" if _fr_erros else ""
                st.success(
                    f"✅ Forma Recente concluída!  \n"
                    f"**{_fr_res.n_jogos_calibracao}** jogos únicos · "
                    f"**{len(_fr_res.times)}** seleções calibradas{_aviso_erros}  \n"
                    f"xi=0.010 · home_advantage={_fr_res.home_advantage:.2f} (neutro)"
                )
                if _fr_erros:
                    st.caption(f"Times sem dados na API: {', '.join(_fr_erros)}")
                st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
                st.rerun()
            except CreditosInsuficientesError as _e_fr:
                st.error(f"Saldo insuficiente: {_e_fr}")
            except Exception as _e_fr:
                st.error(f"Falha no Bootstrap Forma Recente: {_e_fr}")

    # ── Calibrar ligas em lote ────────────────────────────────────────
    with st.expander("⚙️ Calibrar ligas em lote"):
        # ── Agenda do dia: jogos de hoje por liga ──────────────────────
        _agenda_cal = banco.datas.get(data_str, {}).get("agenda", [])
        if _agenda_cal:
            st.markdown(f"**📅 Jogos de hoje ({data_str}) — clique para adicionar ao lote:**")
            # Agrupa por liga e ordena por quantidade de jogos (desc)
            _ligas_hoje: dict[int, dict] = {}
            for _jh in _agenda_cal:
                _lh = _jh.get("league", {})
                _lid_h = _lh.get("id", 0)
                if _lid_h not in _ligas_hoje:
                    _pd_h = banco.params_ligas.get(str(_lid_h), {})
                    _cal_h = bool(_pd_h.get("times"))
                    try:
                        _dt_cal = dt.datetime.fromisoformat(_pd_h.get("calibrado_em", ""))
                        _dias_h = (dt.datetime.now() - _dt_cal).days
                        _status_h = f"🟢 {_dias_h}d" if _dias_h < INTERVALO_RECALIBRACAO_DIAS else f"🟡 {_dias_h}d"
                    except Exception:
                        _status_h = "❌ nunca" if not _cal_h else "❓"
                    _ligas_hoje[_lid_h] = {
                        "nome":    _lh.get("name", f"Liga {_lid_h}"),
                        "pais":    _lh.get("country", "?"),
                        "jogos":   [],
                        "status":  _status_h,
                        "suport":  _lid_h in _ligas_ativas,
                    }
                _hora_h = _jh["fixture"].get("date", "")[:16].replace("T", " ")[-5:]
                _jogo_h = (
                    f"`{_hora_h}` "
                    f"{_jh['teams']['home']['name']} × {_jh['teams']['away']['name']}"
                )
                _ligas_hoje[_lid_h]["jogos"].append(_jogo_h)

            _ligas_hoje_sorted = sorted(
                _ligas_hoje.items(), key=lambda x: -len(x[1]["jogos"])
            )
            for _lid_h, _info_h in _ligas_hoje_sorted:
                _col_la, _col_lb, _col_lc = st.columns([4, 1, 2])
                _n_j_h = len(_info_h["jogos"])
                _col_la.write(
                    f"**{_info_h['nome']}** ({_info_h['pais']}) — "
                    f"{_n_j_h} jogo{'s' if _n_j_h > 1 else ''}"
                )
                _col_lb.caption(_info_h["status"])
                _btn_add_key = f"add_lote_{_lid_h}"
                _ja_no_lote = _lid_h in st.session_state.get("cal_lote_multiselect", [])
                if not _ja_no_lote:
                    if _col_lc.button(
                        "+ Adicionar ao lote",
                        key=_btn_add_key,
                        use_container_width=True,
                    ):
                        _sel = list(st.session_state.get("cal_lote_multiselect", []))
                        if _lid_h not in _sel:
                            _sel.append(_lid_h)
                        st.session_state["cal_lote_multiselect"] = _sel
                        st.session_state.pop("delta_preview_lote", None)
                        st.rerun()
                else:
                    _col_lc.success("✅ no lote", icon=None)
                # Lista de jogos colapsável
                with st.expander(
                    f"Ver {_n_j_h} jogo(s) de {_info_h['nome']}",
                    expanded=False,
                ):
                    for _jg_h in _info_h["jogos"]:
                        st.caption(_jg_h)
            st.markdown("---")
        else:
            st.caption(
                "📅 Agenda do dia não carregada. Abra a aba **Análise Diária** e carregue a agenda "
                "para ver os jogos de hoje aqui."
            )

        # Inicializa chave do multiselect antes de renderizar os atalhos
        if "cal_lote_multiselect" not in st.session_state:
            st.session_state["cal_lote_multiselect"] = []

        # Atalhos de seleção rápida
        col_qs1, col_qs2, col_qs3 = st.columns(3)
        if col_qs1.button("✅ Todas as ligas", key="qs_todas", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = list(_ligas_ativas.keys())
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()
        if col_qs2.button("❌ Não calibradas", key="qs_nunca", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = [
                lid for lid in _ligas_ativas if str(lid) not in banco.params_ligas
            ]
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()
        if col_qs3.button("🗑️ Limpar seleção", key="qs_limpar", use_container_width=True):
            st.session_state["cal_lote_multiselect"] = []
            st.session_state.pop("delta_preview_lote", None)
            st.rerun()

        ligas_lote_sel = st.multiselect(
            "Ligas a calibrar",
            options=list(_ligas_ativas.keys()),
            format_func=lambda x: f"{_ligas_ativas[x]} (ID {x})",
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

                # Detecta ligas com erro de API (n_api=0 + erro != None)
                _ligas_com_erro = [
                    _li for _li in _prev_lote["ligas"]
                    if any(s.get("erro") for s in _li.get("seasons", []))
                ]
                _ligas_nunca_cal_sem_api = [
                    _li for _li in _prev_lote["ligas"]
                    if _li["n_novos_liga"] == 0
                    and sum(s.get("n_api", 0) for s in _li.get("seasons", [])) == 0
                    and not any(s.get("erro") for s in _li.get("seasons", []))
                ]
                if _ligas_com_erro:
                    st.error(
                        f"⚠️ **{len(_ligas_com_erro)} liga(s) com erro de API** — "
                        "verifique se seu plano API Football cobre essas ligas:\n"
                        + "\n".join(f"• {_li['nome']}: " + "; ".join(
                            s["erro"] for s in _li.get("seasons", []) if s.get("erro")
                        ) for _li in _ligas_com_erro)
                    )
                if _ligas_nunca_cal_sem_api:
                    st.warning(
                        f"⏳ **{len(_ligas_nunca_cal_sem_api)} liga(s) sem dados na API** "
                        "(temporada ainda não iniciou ou liga não está no seu plano):\n"
                        + "\n".join(f"• {_li['nome']}" for _li in _ligas_nunca_cal_sem_api)
                    )

                if _n_tot_l == 0 and not _ligas_com_erro and not _ligas_nunca_cal_sem_api:
                    st.success(
                        f"✅ **Cache 100% atualizado** para as {len(ligas_lote_sel)} liga(s). "
                        "Pode calibrar sem gastar créditos de xG."
                    )
                elif _n_tot_l == 0 and (_ligas_com_erro or _ligas_nunca_cal_sem_api):
                    st.info("ℹ️ Nenhum jogo novo encontrado — veja erros acima para ligas problemáticas.")
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
                    _nn    = _li["n_novos_liga"]
                    _n_api = sum(s.get("n_api", 0) for s in _li.get("seasons", []))
                    _erros = [s["erro"] for s in _li.get("seasons", []) if s.get("erro")]
                    if _erros:
                        _status = f"❌ ERRO API: {_erros[0][:60]}"
                    elif _nn == 0 and _n_api == 0:
                        _status = "⏳ sem dados API"
                    elif _nn == 0:
                        _status = "✅ atualizado"
                    elif _nn > 150:
                        _status = "🔴 bootstrap (~" + str(_nn) + " jogos)"
                    else:
                        _status = "🟡 atualizar (" + str(_nn) + " novos)"
                    _rows_lote.append({
                        "Liga":           _li["nome"],
                        "n_api":          _n_api,
                        "Novos fixtures": _nn,
                        "Créditos xG":    _nn * CUSTO_ESTIMADO_XG_FIXTURE,
                        "Status":         _status,
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

            _lote_detalhes: list[str] = []
            for _i_l, _lid_l in enumerate(_ligas_batch):
                _nome_l = _ligas_ativas.get(_lid_l, f"Liga {_lid_l}")
                _stat_lote.info(f"[{_i_l+1}/{_total_lote}] Calibrando **{_nome_l}**…")
                try:
                    _p_l = dm.obter_params_liga(_lid_l, season, forcar_recalibracao=True)
                    _rx_l = getattr(_p_l, "raio_x_times", {}) or {}
                    _ult_l = max(
                        (str(v.get("ultimo_jogo", ""))[:10]
                         for v in _rx_l.values() if v.get("ultimo_jogo")),
                        default="—",
                    )
                    _lote_detalhes.append(
                        f"**{_nome_l}** T{_p_l.season} · {_p_l.n_jogos_calibracao}j · último: {_ult_l}"
                    )
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
            if _lote_detalhes:
                st.success(
                    f"✅ {len(_lote_detalhes)} liga(s) calibrada(s):\n"
                    + "\n".join(f"• {d}" for d in _lote_detalhes)
                )

            try:
                st.session_state["banco"] = dm.banco_em_memoria()
            except Exception:
                st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
            st.rerun()


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

    # Índice cross-liga: todos os times calibrados em qualquer liga (0 créditos).
    # Usado para análise manual de amistosos/ligas não calibradas — evita chamadas de API.
    _times_todos_manual: dict = {}
    for _pld_m in banco.params_ligas.values():
        for _tid_str_m, _tdata_m in _pld_m.get("times", {}).items():
            _tid_int_m = int(_tid_str_m)
            if _tid_int_m not in _times_todos_manual:
                _times_todos_manual[_tid_int_m] = _tdata_m

    if sem_cal:
        if "analise_manual" not in st.session_state:
            st.session_state["analise_manual"] = {}
        _am = st.session_state["analise_manual"]

        _n_analisaveis_cross = sum(
            1 for j in sem_cal
            if j["teams"]["home"]["id"] in _times_todos_manual
            and j["teams"]["away"]["id"] in _times_todos_manual
        )
        st.markdown(
            f"### 🔭 {len(sem_cal)} jogos fora das ligas calibradas"
            f"  —  {_n_analisaveis_cross} com params cross-liga · restantes usam histórico da API"
        )
        st.caption(
            "✅ = ambos os times com params cross-liga (0+odds cr.)  "
            "⚠️ = 1 time sem params (1+odds cr.)  "
            "❌ = ambos desconhecidos — busca histórico da API (2+odds cr.)"
        )

        _sem_cal_sorted = sorted(sem_cal, key=lambda j: j["fixture"].get("date", ""))

        for j_m in _sem_cal_sorted:
            f_id_m      = str(j_m["fixture"]["id"])
            l_m         = j_m.get("league", {})
            l_id_m      = l_m.get("id", 0)
            l_nome_m    = l_m.get("name", "?")
            l_pais_m    = l_m.get("country", "?")
            h_id_m      = j_m["teams"]["home"]["id"]
            a_id_m      = j_m["teams"]["away"]["id"]
            h_nome_m    = j_m["teams"]["home"]["name"]
            a_nome_m    = j_m["teams"]["away"]["name"]
            hora_m      = j_m["fixture"].get("date", "")[:16].replace("T", " ")[-5:]
            jogo_str_m  = f"{h_nome_m} × {a_nome_m}"
            odds_m      = cache_dia.get("odds", {}).get(f_id_m)
            state_key_m = f"{data_str}_{f_id_m}"

            h_found_m = h_id_m in _times_todos_manual
            a_found_m = a_id_m in _times_todos_manual

            if h_found_m and a_found_m:
                _cob_icon = "✅"
                _cob_tip  = "Ambos com params cross-liga — resultado confiável"
            elif h_found_m or a_found_m:
                _cob_icon = "⚠️"
                _cob_tip  = "1 time sem params calibrados — resultado indicativo"
            else:
                _cob_icon = "❌"
                _cob_tip  = "Ambos desconhecidos — buscará últimos 10 jogos de cada time na API"

            custo_m  = (0 if odds_m else CUSTO_ESTIMADO_ODDS_JOGO)
            custo_m += (0 if h_found_m else 1)
            custo_m += (0 if a_found_m else 1)

            col_j1, col_j2, col_j3, col_j4 = st.columns([1, 5, 1, 2])
            col_j1.caption(f"`{hora_m}`")
            col_j2.write(f"**{jogo_str_m}** · _{l_nome_m}_ ({l_pais_m})")
            col_j3.markdown(f'<span title="{_cob_tip}">{_cob_icon}</span>', unsafe_allow_html=True)

            if col_j4.button(f"🔍 Analisar ({custo_m} cr.)", key=f"am_{f_id_m}", use_container_width=True):
                try:
                    if not odds_m:
                        with st.spinner("Buscando odds (1 cr.)…"):
                            odds_m = dm.buscar_odds_jogo(int(f_id_m))
                        banco.datas.setdefault(data_str, {}).setdefault("odds", {})[f_id_m] = odds_m
                        dm.salvar_banco(banco)

                    _times_mini_m: dict = {}
                    for _tid, _found, _nome_t in [
                        (h_id_m, h_found_m, h_nome_m),
                        (a_id_m, a_found_m, a_nome_m),
                    ]:
                        if _found:
                            _times_mini_m[_tid] = _times_todos_manual[_tid]
                        else:
                            with st.spinner(f"Buscando histórico de {_nome_t} (1 cr.)…"):
                                _df_hist = dm.buscar_historico_time(_tid, n=10)
                            _times_mini_m[_tid] = _estimar_params_adhoc(_df_hist, _tid)

                    _params_m = ParametrosLiga(
                        league_id=l_id_m, season=season,
                        times=_times_mini_m,
                        home_advantage=1.0,
                        rho=-0.05, xi=0.002, media_liga_gols=2.5,
                    )
                    _prev_m = prever_jogo(
                        _params_m, h_id_m, a_id_m,
                        aplicar_shrink=True, cobertura_minima=10,
                        times_fallback=_times_todos_manual,
                    )
                    _am[state_key_m] = {
                        "jogo":    jogo_str_m,
                        "liga":    l_nome_m,
                        "prev":    _prev_m,
                        "odds":    odds_m or {},
                        "h_found": h_found_m,
                        "a_found": a_found_m,
                    }
                    # Injeta no pipeline do Sniper para scoring automático
                    _scj = st.session_state.setdefault("sem_cal_jogos", {})
                    _scj[f_id_m] = {
                        "j":    j_m,
                        "prev": _prev_m,
                        "odds": odds_m or {},
                    }
                    st.rerun()
                except CreditosInsuficientesError as e:
                    st.error(f"Saldo insuficiente: {e}")
                except Exception as e:
                    st.error(f"Falha na análise de {jogo_str_m}: {e}")

            if state_key_m in _am:
                _res_m  = _am[state_key_m]
                _prev_r = _res_m["prev"]
                _odds_r = _res_m.get("odds") or {}
                _mktrs  = _prev_r.get("mercados", {})
                _lam_r  = float(_prev_r.get("lambda") or 1.3)
                _mu_r   = float(_prev_r.get("mu")     or 1.0)
                _cob_r  = _prev_r.get("cobertura_ok", False)
                _hf_r   = _res_m.get("h_found", False)
                _af_r   = _res_m.get("a_found", False)

                with st.container():
                    # Rótulo de fonte de dados
                    if _hf_r and _af_r:
                        _src_label = "✅ Análise cross-liga — params de torneios calibrados"
                    elif _hf_r or _af_r:
                        _t_sem = _res_m["jogo"].split("×")[0 if not _hf_r else 1].strip()
                        _src_label = f"⚠️ Análise parcial — {_t_sem} estimado do histórico API"
                    else:
                        _src_label = "🆕 Análise ad-hoc — alpha/beta dos últimos 10 jogos (shrinkage 50%)"

                    _xg_tot_r = _lam_r + _mu_r
                    _cob_str  = "✅" if _cob_r else "⚠️ dados parciais"
                    _flags_r  = " | ".join(_prev_r["flags"]) if _prev_r.get("flags") else "—"

                    # Header escuro igual ao Sniper
                    st.markdown(
                        f"""<div style='background:#0e1117;padding:10px;border-radius:6px;border:1px solid #333;'>
                          <div style='display:flex;justify-content:space-between;color:#888;font-size:11px;'>
                            <span>🕒 {hora_m} · {l_nome_m}</span>
                            <span>xG total: <b>{_xg_tot_r:.2f}</b> · {_cob_str} · Flags: {_flags_r}</span>
                          </div>
                          <div style='font-size:18px;font-weight:bold;color:white;margin:6px 0;'>
                            {h_nome_m} <span style='color:#666;font-size:13px;'>vs</span> {a_nome_m}
                          </div>
                          <div style='font-size:11px;color:#888;'>{_src_label}</div>
                        </div>""",
                        unsafe_allow_html=True,
                    )

                    # Score do melhor mercado — mesmo pipeline do Sniper + Estrategista
                    _mercados_score = [
                        "OVER_15","OVER_25","OVER_35",
                        "UNDER_15","UNDER_25","UNDER_35",
                        "BTTS_YES","BTTS_NO",
                        "HOME","DRAW","AWAY","1X","X2","12",
                    ]
                    _RESULTADO_MKS = frozenset({"HOME","DRAW","AWAY","1X","X2","12"})
                    _melhor_score, _melhor_mk = 0.0, ""
                    for _smk in _mercados_score:
                        _sp = _mktrs.get(_smk, 0)
                        _so = _odds_r.get(_smk, 0)
                        if _so <= 1.0 or _sp <= 0:
                            continue
                        _sc = comparar_com_mercado(_sp, _so, MARGEM_BOOKMAKER_DEFAULT, limite_div)
                        if not filtrar_gatilho(_smk, _sc["ev_pct"], _sp, _sc["divergencia_pp"], _so):
                            continue
                        _sv = calcular_score_qualidade(
                            ev_pct=_sc["ev_pct"], divergencia_pp=_sc["divergencia_pp"],
                            prob_modelo=_sp, kelly_fracao=_sc.get("kelly_fracao", 0),
                            odd=_so, cobertura_ok=_cob_r,
                        )
                        _heur_adj, _ = avaliar_heuristicas(
                            _smk, _lam_r, _mu_r, _xg_tot_r, _prev_r.get("dc_ctx")
                        )
                        _sv = round(max(0.0, _sv + _heur_adj), 1)
                        if _sv > _melhor_score:
                            _melhor_score, _melhor_mk = _sv, _smk
                    if _melhor_score >= SCORE_MINIMO_RANKING:
                        st.success(f"✅ **Score {_melhor_score:.0f}/100** em {_melhor_mk} — aparece no Sniper abaixo")
                    elif _melhor_score > 0:
                        st.info(f"📊 Score máximo: **{_melhor_score:.0f}/100** ({_melhor_mk}) — abaixo do limiar {SCORE_MINIMO_RANKING}")
                    else:
                        st.caption(f"📊 Nenhum mercado passou o gatilho EV/divergência do Sniper")

                    _sub_m = st.tabs(["🔢 Gols", "🤝 BTTS", "🏆 Resultado"])
                    with _sub_m[0]:
                        _cols_o = st.columns(5)
                        _cols_u = st.columns(5)
                        for _c, _l in zip(_cols_o, ["05", "15", "25", "35", "45"]):
                            _mk = f"OVER_{_l}"
                            render_mercado(_c, f"Over {_l[0]}.{_l[1]}", _mk,
                                           _mktrs.get(_mk, 0), _odds_r.get(_mk, 0),
                                           banca_atual, piso_kelly, teto_pct, limite_div)
                        for _c, _l in zip(_cols_u, ["05", "15", "25", "35", "45"]):
                            _mk = f"UNDER_{_l}"
                            render_mercado(_c, f"Under {_l[0]}.{_l[1]}", _mk,
                                           _mktrs.get(_mk, 0), _odds_r.get(_mk, 0),
                                           banca_atual, piso_kelly, teto_pct, limite_div)
                    with _sub_m[1]:
                        _cols_b = st.columns(2)
                        for _c, _k, _lbl in zip(_cols_b,
                                                 ["BTTS_YES", "BTTS_NO"],
                                                 ["Ambas marcam", "Não ambas"]):
                            render_mercado(_c, _lbl, _k,
                                           _mktrs.get(_k, 0), _odds_r.get(_k, 0),
                                           banca_atual, piso_kelly, teto_pct, limite_div)
                    with _sub_m[2]:
                        _ALL_RESULTADO = frozenset({"HOME","DRAW","AWAY","1X","X2","12"})
                        _cols_res = st.columns(3)
                        for _c, _k, _lbl in zip(
                            _cols_res,
                            ["HOME", "DRAW", "AWAY"],
                            [f"Casa ({h_nome_m})", "Empate", f"Fora ({a_nome_m})"],
                        ):
                            render_mercado(_c, _lbl, _k,
                                           _mktrs.get(_k, 0), _odds_r.get(_k, 0),
                                           banca_atual, piso_kelly, teto_pct, limite_div,
                                           _extra_producao=_ALL_RESULTADO)
                        # Dupla Chance derivada das odds 1X2
                        _odd_h_r = _odds_r.get("HOME", 0)
                        _odd_d_r = _odds_r.get("DRAW", 0)
                        _odd_a_r = _odds_r.get("AWAY", 0)
                        _odd_1x = (1.0/(1.0/_odd_h_r + 1.0/_odd_d_r)
                                   if _odd_h_r > 1 and _odd_d_r > 1 else 0.0)
                        _odd_x2 = (1.0/(1.0/_odd_d_r + 1.0/_odd_a_r)
                                   if _odd_d_r > 1 and _odd_a_r > 1 else 0.0)
                        _odd_12 = (1.0/(1.0/_odd_h_r + 1.0/_odd_a_r)
                                   if _odd_h_r > 1 and _odd_a_r > 1 else 0.0)
                        _p_home_r = _mktrs.get("HOME", 0)
                        _p_draw_r = _mktrs.get("DRAW", 0)
                        _p_away_r = _mktrs.get("AWAY", 0)
                        _cols_dc = st.columns(3)
                        for _c, _k, _lbl, _prob_dc, _odd_dc in zip(
                            _cols_dc,
                            ["1X", "X2", "12"],
                            ["Dupla 1X", "Dupla X2", "Dupla 12"],
                            [_p_home_r + _p_draw_r, _p_draw_r + _p_away_r, _p_home_r + _p_away_r],
                            [_odd_1x, _odd_x2, _odd_12],
                        ):
                            render_mercado(_c, _lbl, _k, _prob_dc, _odd_dc,
                                           banca_atual, piso_kelly, teto_pct, limite_div,
                                           _extra_producao=_ALL_RESULTADO)
                st.divider()

        # Botões de calibração por liga ao final
        st.markdown("---")
        st.caption("**Calibrar uma liga completa** (histórico permanente — mais caro):")
        _ligas_sem: dict[int, tuple[str, str]] = {}
        for _j_s in sem_cal:
            _l_s = _j_s.get("league", {})
            _lid_s = _l_s.get("id", 0)
            if _lid_s not in _ligas_sem:
                _ligas_sem[_lid_s] = (_l_s.get("name", "?"), _l_s.get("country", "?"))
        _ncols_cal = min(len(_ligas_sem), 3)
        if _ncols_cal:
            _cal_cols = st.columns(_ncols_cal)
            for _ci, (_lid_s, (_lnome_s, _lpais_s)) in enumerate(_ligas_sem.items()):
                with _cal_cols[_ci % _ncols_cal]:
                    if st.button(
                        f"⚡ Calibrar {_lnome_s}",
                        key=f"fallback_{_lid_s}",
                        use_container_width=True,
                        help=f"Busca histórico completo da {_lnome_s} e calibra D-C (máx {TIMEOUT_CALIBRACAO_SEGUNDOS}s).",
                    ):
                        try:
                            with st.spinner(f"Calibrando {_lnome_s} (ID {_lid_s})..."):
                                dm.calibrar_liga_avulsa(_lid_s, season)
                            # Salva nome/país para aparecer no menu principal de calibração
                            _banco_upd = dm.carregar_banco()
                            if str(_lid_s) in _banco_upd.params_ligas:
                                _banco_upd.params_ligas[str(_lid_s)]["nome_liga"] = _lnome_s
                                _banco_upd.params_ligas[str(_lid_s)]["pais_liga"]  = _lpais_s
                                dm.salvar_banco(_banco_upd)
                            if dm.ultimo_save_jsonbin_ok:
                                st.success(f"{_lnome_s} calibrada e salva! Agora aparece no menu principal.")
                            else:
                                _ce_s = getattr(dm, "ultimo_save_erro", "") or "verifique conexão"
                                st.warning(
                                    f"⚠️ {_lnome_s} calibrada mas falhou ao salvar no cloud ({_ce_s}). "
                                    "Use 'Forçar Salvar no Cloud' no sidebar."
                                )
                            st.session_state["banco"] = dm.carregar_banco(força_recarregar=True)
                            st.rerun()
                        except TimeoutError as e:
                            st.error(f"⏱️ Timeout: {e}")
                        except Exception as e:
                            st.error(f"Não foi possível calibrar {_lnome_s}: {e}")

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

    # Injeta jogos sem_cal analisados manualmente no pipeline de scoring.
    # Esses jogos NÃO aparecem nos cards do Sniper (já exibidos na seção acima).
    # _sem_cal_fids rastreia quais fixture_ids foram injetados via análise manual.
    _sem_cal_fids: set[str] = set()
    _ids_cal = {str(j["fixture"]["id"]) for j in jogos_com_odds}
    for _sc_fid, _sc_data in st.session_state.get("sem_cal_jogos", {}).items():
        if _sc_fid not in _ids_cal and _sc_data.get("odds"):
            jogos_com_odds.append(_sc_data["j"])
            previsoes[_sc_fid] = _sc_data["prev"]
            odds_cache[_sc_fid] = _sc_data["odds"]
            _ids_cal.add(_sc_fid)
            _sem_cal_fids.add(_sc_fid)

    # Cross-liga fallback: times de Copa/Libertadores que não estão nos params da competição
    # são buscados na liga doméstica (ex.: time do Brasileirão aparece na Libertadores).
    # Prioridade: params da liga alvo > fallback de outra liga > média global.
    # Construímos uma vez, mapeando team_id → params do primeiro match encontrado.
    _times_todos: dict = {}
    for _pld in banco.params_ligas.values():
        for _tid_str, _tdata in _pld.get("times", {}).items():
            _tid_int = int(_tid_str)
            if _tid_int not in _times_todos:
                _times_todos[_tid_int] = _tdata

    # Pré-compila DataFrames de historico por liga — 0 créditos, uma vez por liga
    _forma_dfs: dict[int, pd.DataFrame] = {}
    for _j_tmp in jogos_com_odds:
        _lid_tmp = _j_tmp["league"]["id"]
        if _lid_tmp not in _forma_dfs:
            _reg = banco.historico_ligas.get(str(_lid_tmp), {}).get("registros", [])
            if _reg:
                _df_tmp = pd.DataFrame(_reg)
                if "date" in _df_tmp.columns:
                    _df_tmp["date"] = pd.to_datetime(_df_tmp["date"], errors="coerce")
                    _df_tmp = _df_tmp.sort_values("date").reset_index(drop=True)
                _forma_dfs[_lid_tmp] = _df_tmp
            else:
                _forma_dfs[_lid_tmp] = pd.DataFrame()

    for j in jogos_com_odds:
        f_id   = str(j["fixture"]["id"])
        l_id   = j["league"]["id"]
        _params_raw = banco.params_ligas.get(str(l_id))
        if _params_raw is None:
            # Jogo sem_cal injetado — previsão já calculada, só o dc_ctx falta
            continue
        params = ParametrosLiga.from_dict(_params_raw)
        h_id   = j["teams"]["home"]["id"]
        a_id   = j["teams"]["away"]["id"]
        if f_id not in previsoes:
            prev = prever_jogo(
                params, h_id, a_id,
                aplicar_shrink=True, cobertura_minima=10,
                times_fallback=_times_todos,
            )
            previsoes[f_id] = {k: prev.get(k) for k in
                               ("lambda", "mu", "xg_total", "mercados", "flags",
                                "cobertura_ok", "erro")}
        # dc_ctx: usa fallback cross-liga quando time não está nos params da competição
        h_data = params.times.get(h_id) or _times_todos.get(h_id) or {}
        a_data = params.times.get(a_id) or _times_todos.get(a_id) or {}
        dc_ctx: dict = {
            "alpha_h":         h_data.get("alpha",  1.0),
            "beta_h":          h_data.get("beta",   1.0),
            "alpha_a":         a_data.get("alpha",  1.0),
            "beta_a":          a_data.get("beta",   1.0),
            "n_jogos_h":       h_data.get("n_jogos", 0),
            "n_jogos_a":       a_data.get("n_jogos", 0),
            "rho":             params.rho,
            "media_liga_gols": params.media_liga_gols,
        }
        # Forma recente + H2H: extraídos do cache local — 0 créditos extras
        dc_ctx.update(
            extrair_forma_times(_forma_dfs.get(l_id, pd.DataFrame()), h_id, a_id)
        )
        # Scout fallback: quando historico local está vazio (restart do Streamlit Cloud),
        # injeta médias pré-computadas do params.scout_medias para ativar HSC1-HSC10.
        # getattr: compatibilidade com params antigos do Gist sem o campo scout_medias.
        _sm = getattr(params, "scout_medias", {})
        if _sm:
            _scout_stats = [
                "shots_on_avg", "shots_total_avg", "possession_avg",
                "corners_avg", "yellows_avg", "saves_avg", "fouls_avg",
                "goals_prevented_avg", "shots_insidebox_avg", "offsides_avg",
                "blocked_shots_avg", "shots_offgoal_avg", "passes_pct_avg",
            ]
            for _stat in _scout_stats:
                if dc_ctx.get(f"h_{_stat}") is None:
                    dc_ctx[f"h_{_stat}"] = _sm.get(h_id, {}).get(_stat)
                if dc_ctx.get(f"a_{_stat}") is None:
                    dc_ctx[f"a_{_stat}"] = _sm.get(a_id, {}).get(_stat)
        previsoes[f_id]["dc_ctx"] = dc_ctx
    banco.datas[data_str]["previsoes"] = previsoes
    dm.salvar_banco(banco)

    # ── Kelly de Portfólio ───────────────────────────────────────────────────
    # Usa os Kelly totais da renderização anterior para escalar os stakes de forma
    # que o risco diário total não ultrapasse limite_risco_pct da banca.
    # Primeira renderização: fator=1.0 (sem dados prévios → Kelly individual).
    # Após qualquer interação do usuário: fator escalonado com base nos picks do dia.
    _kelly_dia_gols_prev = st.session_state.get("_kelly_dia_gols", 0.0)
    _kelly_dia_mo_prev   = st.session_state.get("_kelly_dia_mo",   0.0)
    _kelly_total_prev    = _kelly_dia_gols_prev + _kelly_dia_mo_prev
    if _kelly_total_prev > 1e-6:
        _fator_portfolio = min(1.0, limite_risco_pct / _kelly_total_prev)
    else:
        _fator_portfolio = 1.0  # sem picks anteriores: Kelly individual intacto

    aba_gols, aba_resultado = st.tabs(["🎯 Sniper de Gols", "📊 Estrategista de Resultados"])

    # =========================================================================
    # ABA GOLS — Over / Under / BTTS (RAW + Gap + EV, V4 validado)
    # =========================================================================
    with aba_gols:

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
        # Pisos de odd por mercado — independentes do slider global (odd_min_save).
        # Raciocínio profissional: odds abaixo de 1.50 exigem calibração de ±2pp que o D-C
        # ainda não tem validação suficiente. BTTS_YES/OVER_15 abertas mas com pisos conservadores.
        # DC (1X/X2/12) mantido desativado enquanto DRAW (componente) não tem backtest sólido.
        _GOLS_ODD_MIN = {
            "OVER_15":   1.28, "OVER_25":  1.50, "OVER_35":  1.55,
            "UNDER_15":  1.40, "UNDER_25": 1.50, "UNDER_35": 1.30,
            "BTTS_YES":  1.55, "BTTS_NO":  1.60,
        }
        # Mercados que respeitam o slider global (validados por backtest)
        _GOLS_USA_GLOBAL_SLIDER = {"OVER_25", "UNDER_25", "BTTS_NO", "OVER_35", "UNDER_35", "OVER_15", "BTTS_YES"}
        _ligas_bloq = set(st.session_state.get("risk_ligas_bloqueadas", []))
        for j in jogos_com_odds:
            f_id   = str(j["fixture"]["id"])
            l_id_j = j["league"]["id"]
            if l_id_j in _ligas_bloq:
                continue
            prev   = previsoes[f_id]
            if prev.get("erro"):
                continue
            odds_j       = odds_cache[f_id]
            cobertura_ok = prev.get("cobertura_ok", False)
            jogo_nome    = f"{j['teams']['home']['name']} v {j['teams']['away']['name']}"
            liga_nome_j  = j["league"]["name"]
            _n_jogos_liga = banco.params_ligas.get(str(l_id_j), {}).get("n_jogos_calibracao", 0)
            _cal_marginal = _n_jogos_liga < 40

            for mercado in MERCADOS_VARREDURA:
                prob_modelo = prev["mercados"].get(mercado, 0)
                odd_val     = odds_j.get(mercado, 0)
                # Piso efetivo: mercados no grupo "usa global slider" respeitam o slider;
                # OVER_15, BTTS_YES etc. usam apenas seu piso calibrado de backtest.
                _odd_piso = _GOLS_ODD_MIN.get(mercado, 1.50)
                if mercado in _GOLS_USA_GLOBAL_SLIDER:
                    _odd_piso = max(_odd_piso, odd_min_save)
                if odd_val <= 1.0 or odd_val < _odd_piso:
                    continue
                if prob_modelo < prob_min:
                    continue
                comp  = comparar_com_mercado(prob_modelo, odd_val,
                                             MARGEM_BOOKMAKER_DEFAULT, limite_div)
                stake = calcular_stake_final(comp.get("kelly_fracao", 0), banca_atual,
                                             piso_kelly, teto_pct)
                # Filtro de gatilho: EV_MIN + PROB_MIN + delta > 0
                xg_total_prev = prev.get("xg_total", 2.5)
                if not (filtrar_gatilho(mercado, comp["ev_pct"], prob_modelo,
                                        comp["divergencia_pp"], odd_val)
                        and filtrar_gap(mercado, xg_total_prev, GAP_CONFIG_PROD)       # P1
                        and filtrar_ev_config(mercado, comp["ev_pct"], EV_CONFIG_PROD) # EV override
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

                # Camada heurística contextual — λ/μ + parâmetros D-C internos
                heur_adj, heur_nota = avaliar_heuristicas(
                    mercado,
                    xg_lam   = float(prev.get("lambda") or 1.3),
                    xg_mu    = float(prev.get("mu")     or 1.0),
                    xg_total = xg_total_prev,
                    ctx      = prev.get("dc_ctx"),
                )
                score = round(max(0.0, score + heur_adj), 1)

                # Hard block: BTTS_NO descartado quando visitante marcou 6+ gols em 5j.
                # O D-C histórico não consegue capturar explosão ofensiva recente — o sinal
                # de forma é superior ao modelo neste caso extremo.
                if mercado == "BTTS_NO":
                    _ctx_hb = prev.get("dc_ctx", {})
                    if (_ctx_hb.get("a_gf_last5", 0) >= 6
                            and _ctx_hb.get("a_n_last5", 0) >= 4):
                        continue

                # ── Regra 1 — BTTS_YES: xG mínimo de AMBOS os times ──────────
                # Se o time mais fraco tem λ ou μ < 0.75, o próprio D-C diz que ele
                # raramente marca. Calibrador sobre probabilidade baixa = ruído.
                # Threshold derivado do modelo, não arbitrário: P(time marca) ≈ 1-e^(-λ);
                # λ=0.75 → P(marca) ≈ 53% — abaixo disso BTTS_YES é apostolar.
                _BTTS_MIN_XG_TIME = 0.75
                if mercado == "BTTS_YES":
                    _lam = float(prev.get("lambda") or 0.0)
                    _mu  = float(prev.get("mu")     or 0.0)
                    if min(_lam, _mu) < _BTTS_MIN_XG_TIME:
                        continue

                # ── Regra 2 — BTTS: divergência mínima real ──────────────────
                # EV de 5-8% com divergência < 4pp significa modelo e mercado
                # quase de acordo — sem borda real, só variância. Exige desacordo
                # genuíno entre o D-C e o bookmaker para justificar a entrada.
                _BTTS_MIN_DIV_PP = 4.0
                if mercado in ("BTTS_YES", "BTTS_NO"):
                    if comp.get("divergencia_pp", 0.0) < _BTTS_MIN_DIV_PP:
                        continue

                # ── Regra 3b — UNDER_25: zona morta EV 25-30% ───────────────
                # Backtest V4 (52 picks, EV>=20%): HR 34.6%, ROI -27.27% — pior
                # sub-faixa de EV para UNDER. Faixas vizinhas são lucrativas:
                # EV 20-25% ROI +0.7% e EV >30% ROI +18.0%. O D-C oscila entre
                # sinal e ruído exatamente nesta janela para mercados UNDER.
                if mercado == "UNDER_25" and 25.0 <= comp["ev_pct"] < 30.0:
                    continue

                # ── Regra 2b — BTTS_YES: λ mandante + suporte comportamental ─
                # Regra 1 já bloqueia min(λ,μ)<0.75. Esta regra acrescenta:
                # (a) mandante com ofensividade real: λ ≥ 0.85
                # (b) quando há dados de forma (≥3j): nenhum dos dois em seca
                #     de 2+ jogos e ambos com ≥3 gols marcados nos últimos 5j
                if mercado == "BTTS_YES":
                    _ctx_b2 = prev.get("dc_ctx") or {}
                    _lam_b2 = float(prev.get("lambda") or 0.0)
                    if _lam_b2 < 0.85:
                        continue
                    if _ctx_b2:
                        _h_n5_b2 = int(_ctx_b2.get("h_n_last5", 0))
                        _a_n5_b2 = int(_ctx_b2.get("a_n_last5", 0))
                        if _h_n5_b2 >= 3 and _a_n5_b2 >= 3:
                            if (int(_ctx_b2.get("h_sem_marcar", 0)) >= 2
                                    or int(_ctx_b2.get("a_sem_marcar", 0)) >= 2):
                                continue
                            if (int(_ctx_b2.get("h_gf_last5", 0)) < 3
                                    or int(_ctx_b2.get("a_gf_last5", 0)) < 3):
                                continue

                # ── Regra 2c — BTTS_NO: contexto defensivo obrigatório ───────
                # BTTS_NO sem contexto defensivo claro = aposta contra tendência.
                # xG_total ≥ 2.1: ambos ofensivos, BTTS_NO estruturalmente fraco.
                # Quando há forma (≥3j): exige ao menos 1 time com ≤2 gols em 5j
                # (ataque inoperante) OU mandante com ≥2 clean sheets em casa.
                if mercado == "BTTS_NO":
                    if xg_total_prev >= 2.1:
                        continue
                    _ctx_bn2 = prev.get("dc_ctx") or {}
                    if _ctx_bn2:
                        _h_n5_bn = int(_ctx_bn2.get("h_n_last5", 0))
                        _a_n5_bn = int(_ctx_bn2.get("a_n_last5", 0))
                        if _h_n5_bn >= 3 and _a_n5_bn >= 3:
                            _h_gf_bn = int(_ctx_bn2.get("h_gf_last5", 0))
                            _a_gf_bn = int(_ctx_bn2.get("a_gf_last5", 0))
                            _h_cs_bn = int(_ctx_bn2.get("h_cs_streak_casa", 0))
                            if not (_h_gf_bn <= 2 or _a_gf_bn <= 2 or _h_cs_bn >= 2):
                                continue

                # ── Regra 4 — OVER_15: condição estrutural ───────────────────
                # Odd piso 1.50→1.28 desbloqueia mercado (odds reais: 1.10-1.35).
                # Sem condição estrutural, qualquer EV positivo trivial dispararia.
                # xG_total ≥ 2.2: jogo com expectativa ofensiva clara.
                # Forma: ambos com ≥3 gols nos últimos 5j quando dados disponíveis.
                if mercado == "OVER_15":
                    if xg_total_prev < 2.2:
                        continue
                    _ctx_o15 = prev.get("dc_ctx") or {}
                    if _ctx_o15:
                        _h_n5_o15 = int(_ctx_o15.get("h_n_last5", 0))
                        _a_n5_o15 = int(_ctx_o15.get("a_n_last5", 0))
                        if _h_n5_o15 >= 3 and _a_n5_o15 >= 3:
                            if (int(_ctx_o15.get("h_gf_last5", 0)) < 3
                                    or int(_ctx_o15.get("a_gf_last5", 0)) < 3):
                                continue

                # ── Regra 4b — UNDER_15: condição estrutural ─────────────────
                # Odd piso 1.55→1.40. Under 1.5 só faz sentido quando o modelo
                # projeta jogo de baixíssima marcação: xG_total < 1.20.
                # H5c já penaliza score quando xG ≥ 1.40; este filtro hard-bloqueia
                # entradas onde o D-C não tem convicção de goleada defensiva.
                if mercado == "UNDER_15" and xg_total_prev >= 1.20:
                    continue

                if score < SCORE_MINIMO_RANKING:
                    continue

                candidatos.append({
                    "fixture_id":    f_id,
                    "jogo":          jogo_nome,
                    "liga":          liga_nome_j,
                    "league_id":     l_id_j,
                    "league_season": j["league"].get("season", detectar_temporada_atual()),
                    "league_type":   j["league"].get("type", "League"),
                    "league_round":  j["league"].get("round", ""),
                    "home_id":       j["teams"]["home"]["id"],
                    "away_id":       j["teams"]["away"]["id"],
                    "mercado":       mercado,
                    "odd":          odd_val,
                    "prob_modelo":  prob_modelo,
                    "prob_mercado": comp["prob_mercado_pct"],
                    "ev":           comp["ev_pct"],
                    "divergencia":  comp["divergencia_pp"],
                    "kelly":        comp.get("kelly_fracao", 0),
                    "stake":        stake,
                    "score":        score,
                    "cobertura_ok": cobertura_ok,
                    "cal_marginal": _cal_marginal,
                    "heur_adj":     heur_adj,
                    "heur_nota":    heur_nota,
                    "xg_lam":       float(prev.get("lambda")   or 0.0),
                    "xg_mu":        float(prev.get("mu")       or 0.0),
                    "xg_total":     float(prev.get("xg_total") or 0.0),
                    "dc_ctx":       prev.get("dc_ctx"),
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

        # Fixture IDs selecionados pelo Sniper — usados para dedup cross-aba
        # (Estrategista não exibirá o mesmo jogo que já aparece aqui)
        _sniper_fixture_ids = {p["fixture_id"] for p in ranking}

        # ── Kelly Portfólio: armazena total e reescala stakes ────────────
        _kelly_sum_gols = sum(p["kelly"] for p in ranking)
        st.session_state["_kelly_dia_gols"] = _kelly_sum_gols
        if _fator_portfolio < 0.999:
            for p in ranking:
                p["stake"] = max(piso_kelly, round(p["kelly"] * banca_atual * _fator_portfolio, 2))

        # ── Exibe ranking ────────────────────────────────────────────────
        n_total_aprovados = len(candidatos)  # antes da dedup, para info

        if ranking:
            st.markdown(f"### 🏆 Ranking de Qualidade do Dia ({len(ranking)} entrada{'s' if len(ranking) > 1 else ''})")
            st.caption(
                f"Score ≥ {SCORE_MINIMO_RANKING} · 1 mercado/jogo (melhor score) · "
                f"EV mínimo por mercado (UNDER_25 ≥ {EV_MIN_POR_MERCADO['UNDER_25']:.0f}%) · "
                f"{n_total_aprovados} candidatos antes da filtragem"
            )
            # Banner de portfólio: mostra exposição total do dia (gols + resultados)
            _n_mo_prev   = st.session_state.get("_n_picks_mo",   0)
            _stk_mo_prev = st.session_state.get("_stake_dia_mo", 0.0)
            _stk_gols    = sum(p["stake"] for p in ranking)
            _total_picks = len(ranking) + _n_mo_prev
            _total_stake = _stk_gols + _stk_mo_prev
            _pct_banca   = (_total_stake / banca_atual * 100) if banca_atual > 0 else 0
            _fator_str   = f" · escala {_fator_portfolio:.2f}×" if _fator_portfolio < 0.999 else ""
            st.info(
                f"💼 **Portfólio do dia:** {_total_picks} picks · "
                f"R$ {_total_stake:.2f} expostos ({_pct_banca:.1f}% da banca){_fator_str}"
            )

            for i, p in enumerate(ranking, 1):
                score    = p.get("score", 0)
                if score >= 70:
                    cor, badge = "#28a745", "🟢 Alta"
                elif score >= 50:
                    cor, badge = "#17a2b8", "🔵 Média"
                else:
                    cor, badge = "#ffc107", "🟡 Marginal"
                barras  = int(score / 10)
                bar_str = "█" * barras + "░" * (10 - barras)
                cob_icon = "✅" if p.get("cobertura_ok") else "⚠️ dados parciais"
                cal_icon = " · 🔴 cal. marginal (<40j)" if p.get("cal_marginal") else ""
                try:
                    st.markdown(
                        f"<div style='border-left:4px solid {cor};padding:10px 14px;"
                        f"margin-bottom:4px;background:#0e1117;border-radius:4px;'>"
                        f"<div style='display:flex;justify-content:space-between;"
                        f"font-size:11px;color:#888;'>"
                        f"<span>#{i} · {p.get('liga', '—')} · {cob_icon}{cal_icon}</span>"
                        f"<span style='color:{cor};font-weight:bold;'>{badge} &nbsp;"
                        f"<span style='font-family:monospace;letter-spacing:1px;'>{bar_str}</span>"
                        f"&nbsp;{score:.0f}/100</span>"
                        f"</div>"
                        f"<div style='font-size:17px;font-weight:bold;color:white;margin:5px 0 3px;'>"
                        f"{p.get('jogo', '—')}"
                        f"</div>"
                        f"<div style='font-size:13px;color:#ccc;'>"
                        f"<b>{p.get('mercado', '—')}</b> &nbsp;·&nbsp; "
                        f"Odd <b>{p.get('odd', 0):.2f}</b> &nbsp;·&nbsp; "
                        f"Modelo <b>{p.get('prob_modelo', 0):.1f}%</b>"
                        f" vs Mercado {p.get('prob_mercado', 0):.1f}% &nbsp;·&nbsp; "
                        f"Δ <b>{p.get('divergencia', 0):+.1f}pp</b>"
                        f"</div>"
                        f"<div style='font-size:12px;color:#aaa;margin-top:2px;'>"
                        f"EV <span style='color:{cor};font-weight:bold;'>{p.get('ev', 0):+.1f}%</span> &nbsp;·&nbsp; "
                        f"Kelly {p.get('kelly', 0)*100:.1f}% &nbsp;·&nbsp; "
                        f"💵 Stake: <b>R$ {p.get('stake', 0):.2f}</b>"
                        f"</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                except Exception:
                    st.caption(f"#{i} {p.get('jogo','?')} · {p.get('mercado','?')} · Score {score:.0f}")
                # Nota heurística — sempre renderiza, fora do try para não ser silenciada
                _hadj  = p.get("heur_adj",  0.0)
                _hnota = p.get("heur_nota", "Contexto OK")
                if _hadj > 0:
                    st.success(f"⬆️ {_hnota}")
                elif _hadj < 0:
                    st.warning(f"⬇️ {_hnota}")
                else:
                    st.info(f"🔬 Contexto OK — sem regras de forma disparadas")
                # Botão de análise de contexto por pick
                _render_pick_contexto(p)

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

        elif jogos_com_odds:
            st.info(
                f"📭 Nenhum pick atingiu o score mínimo de {SCORE_MINIMO_RANKING} hoje. "
                f"O motor encontrou {n_total_aprovados} candidatos com EV positivo mas nenhum com qualidade suficiente."
            )
            st.divider()

        # =========================================================================
        # CARDS INDIVIDUAIS
        # =========================================================================

        _n_cards = len(jogos_com_odds) - len(_sem_cal_fids)
        st.markdown(f"#### {_n_cards} jogos prontos para análise")

        for j in jogos_com_odds:
            f_id   = str(j["fixture"]["id"])
            # sem_cal: já exibido como card na seção acima — não duplicar aqui
            if f_id in _sem_cal_fids:
                continue
            prev   = previsoes[f_id]
            odds_j = odds_cache[f_id]

            if prev.get("erro"):
                st.error(
                    f"{j['teams']['home']['name']} vs {j['teams']['away']['name']}: {prev['erro']}"
                )
                continue

            try:
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

                # Forma recente — caption contextual (0 créditos, usa cache local)
                _ctx_j = prev.get("dc_ctx", {})
                _hn5   = _ctx_j.get("h_n_last5", 0)
                _an5   = _ctx_j.get("a_n_last5", 0)
                if _hn5 >= 3 and _an5 >= 3:
                    _hv = _ctx_j.get("h_wins_last5", 0)
                    _av = _ctx_j.get("a_wins_last5", 0)
                    _hgf = _ctx_j.get("h_gf_last5", 0)
                    _hga = _ctx_j.get("h_ga_last5", 0)
                    _agf = _ctx_j.get("a_gf_last5", 0)
                    _aga = _ctx_j.get("a_ga_last5", 0)
                    st.caption(
                        f"📊 Forma ({_hn5}j): "
                        f"Casa {_hv}V · {_hgf} GF · {_hga} GS  |  "
                        f"Fora {_av}V · {_agf} GF · {_aga} GS"
                    )

                sub = st.tabs(["🔢 Gols", "🤝 BTTS", "🎯 Placar Exato"])

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

            except Exception as _e:
                st.warning(f"⚠️ Erro ao renderizar jogo {f_id}: {_e}")
                continue


    # =========================================================================
    # ABA RESULTADO — Match Odds 1X2 / Dupla Chance (H1-HOME-Only, V6+)
    # =========================================================================
    with aba_resultado:

        if not jogos_com_odds:
            st.info("Carregue agenda e odds para ver sinais de resultado.")
        else:
            # ── Ranking 1X2/DC ─────────────────────────────────────────────
            candidatos_mo = []

            for j in jogos_com_odds:
                f_id   = str(j["fixture"]["id"])
                l_id   = j["league"]["id"]
                if l_id in _ligas_bloq:
                    continue
                prev   = previsoes[f_id]
                if prev.get("erro"):
                    continue
                odds_j        = odds_cache[f_id]
                cobertura_ok  = prev.get("cobertura_ok", False)
                jogo_nome     = f"{j['teams']['home']['name']} v {j['teams']['away']['name']}"
                liga_nome_j   = j["league"]["name"]

                odd_h = float(odds_j.get("HOME", 0) or 0)
                odd_d = float(odds_j.get("DRAW", 0) or 0)
                odd_a = float(odds_j.get("AWAY", 0) or 0)

                if any(o <= 1.0 for o in (odd_h, odd_d, odd_a)):
                    continue

                # Overround real do mercado 1X2 (substitui MARGEM fixa)
                overround = calcular_overround_1x2(odd_h, odd_d, odd_a)

                # H1-HOME-Only: calibrador isotônico em HOME; DRAW/AWAY ficam RAW
                # Para sem_cal (liga não calibrada): usa probabilidade bruta do D-C —
                # mesmo pipeline completo, sem isotônica (que não existe para essa liga).
                _params_raw_j = banco.params_ligas.get(str(l_id))
                if _params_raw_j is not None:
                    params_j = ParametrosLiga.from_dict(_params_raw_j)
                    cal_home = params_j.calibradores.get("1X2_HOME")
                else:
                    cal_home = None
                p_home_raw = prev["mercados"]["HOME"]
                p_home     = cal_home.calibrar(p_home_raw) if cal_home else p_home_raw
                p_draw     = prev["mercados"]["DRAW"]
                p_away     = prev["mercados"]["AWAY"]

                # Odds de Dupla Chance derivadas do mercado 1X2
                odd_1x = (1.0 / (1.0/odd_h + 1.0/odd_d)) if odd_h > 1 and odd_d > 1 else 0.0
                odd_x2 = (1.0 / (1.0/odd_d + 1.0/odd_a)) if odd_d > 1 and odd_a > 1 else 0.0
                odd_12 = (1.0 / (1.0/odd_h + 1.0/odd_a)) if odd_h > 1 and odd_a > 1 else 0.0

                # DC por Direção — ancoras de direção para 1X e X2
                # 1X: mandante deve ser favorito (P_HOME > 50%) → empate é seguro, não aposta cega
                # X2: visitante deve ter força real (P_AWAY > 32%) → seguro de empate com valor
                # 12: manter filtros normais (sem seguro de empate, variância não justifica DC)
                # Odd sweet spot: onde o seguro tem valor sem destruir o retorno
                _DC_SWEET = {
                    "1X": (1.18, 1.60),   # 1X fora do sweet spot perde sentido de cobertura
                    "X2": (1.22, 1.68),   # X2 barato demais = visitante esmagador (aposte direto)
                    "12": (1.18, 2.50),   # 12 mantém range normal
                }
                _DC_EV_MIN = {"1X": 3.0, "X2": 3.0, "12": 3.0}  # menor pq direção ancora o risco

                # Auto-conversão DRAW→DC ─────────────────────────────────────
                # DRAW removido como pick direto: D-C superestima empate quando λ/μ
                # imbalanced (phantom-draw). Quando o sinal seria forte, roteamos para
                # 1X (se mandante favorito) ou X2 (se visitante tem força), tornando a
                # aposta menos especulativa e mais ancorada em direção.
                _draw_comp_ac = comparar_com_mercado(p_draw, odd_d, overround)
                _draw_ev_ac   = _draw_comp_ac.get("ev_pct", 0)
                _draw_signal  = (
                    _draw_ev_ac >= 28.0
                    and p_draw >= 22.0
                    and 2.80 <= odd_d <= 4.99
                    and not (30.0 <= _draw_ev_ac < 40.0)
                )

                candidatos_jogo = [
                    ("HOME", p_home, odd_h),
                    ("AWAY", p_away, odd_a),
                    ("1X",  prev["mercados"]["1X"],  odd_1x),
                    ("X2",  prev["mercados"]["X2"],  odd_x2),
                    ("12",  prev["mercados"]["12"],  odd_12),
                ]

                for mercado, prob_pct, odd_mkt in candidatos_jogo:
                    if odd_mkt <= 1.0:
                        continue

                    # ── Filtros de DC por direção (1X e X2) ─────────────────────────
                    # ATENÇÃO: p_home / p_away estão em % (0-100), não decimais (0-1).
                    # Thresholds e labels devem usar a mesma escala.
                    _dc_label = ""  # label extra para UI
                    if mercado == "1X":
                        # Âncora normal: mandante favorito (P>50%).
                        # Auto-conversão DRAW: relaxa para P>40% quando sinal de empate forte.
                        _p_home_min = 40.0 if _draw_signal else 50.0
                        if p_home < _p_home_min:
                            continue
                        sw_lo, sw_hi = _DC_SWEET["1X"]
                        if not (sw_lo <= odd_mkt <= sw_hi):
                            continue
                        if _draw_signal and p_home < 50.0:
                            _dc_label = f"🔄 DRAW→1X (sinal empate convertido, P_home={p_home:.1f}%)"
                        else:
                            _dc_label = f"🏠→🛡️ Mandante+Seguro (P_home={p_home:.1f}%)"
                    elif mercado == "X2":
                        # Âncora normal: visitante com força real (P>32%).
                        # Auto-conversão DRAW: relaxa para P>28% quando sinal de empate forte.
                        _p_away_min = 28.0 if _draw_signal else 32.0
                        if p_away < _p_away_min:
                            continue
                        sw_lo, sw_hi = _DC_SWEET["X2"]
                        if not (sw_lo <= odd_mkt <= sw_hi):
                            continue
                        if _draw_signal and p_away < 32.0:
                            _dc_label = f"🔄 DRAW→X2 (sinal empate convertido, P_away={p_away:.1f}%)"
                        else:
                            _dc_label = f"✈️→🛡️ Visitante+Seguro (P_away={p_away:.1f}%)"

                    comp = comparar_com_mercado(prob_pct, odd_mkt, overround)
                    if "erro" in comp:
                        continue

                    ev   = comp["ev_pct"]
                    # DC usa EV_MIN menor (direção ancora) e sem override de slider global
                    if mercado in ("1X", "X2", "12"):
                        ev_min  = _DC_EV_MIN[mercado]
                        ev_max  = _EV_MAX[mercado]
                        prob_mn = _PROB_MIN[mercado]
                        sw_lo, sw_hi = _DC_SWEET[mercado]
                        eff_odd_mn = sw_lo   # sweet spot já é o piso efetivo
                        odd_mx     = sw_hi
                    else:
                        ev_min  = _EV_MIN[mercado]
                        ev_max  = _EV_MAX[mercado]
                        prob_mn = _PROB_MIN[mercado]
                        eff_odd_mn = max(_ODD_MIN[mercado], odd_min_save)
                        odd_mx     = _ODD_MAX[mercado]

                    if not (ev_min <= ev <= ev_max
                            and prob_pct >= prob_mn
                            and eff_odd_mn <= odd_mkt <= odd_mx):
                        continue

                    # ── Regra 3 — RF direto: exigência crescente para odds altas ──
                    # Para resultados raros (odd > 3.0), o calibrador tem menos amostras
                    # e os erros têm impacto maior. A margem de segurança cresce com a
                    # variância: cada tier exige EV maior E que o modelo seja ao menos
                    # 25% mais confiante que o mercado implícito.
                    if mercado in ("HOME", "DRAW", "AWAY"):
                        _prob_impl = (100.0 / odd_mkt) if odd_mkt > 0 else 100.0
                        _conf_ratio = prob_pct / _prob_impl if _prob_impl > 0 else 0.0
                        if odd_mkt > 4.0:
                            # odd > 4.0: EV ≥ 20% + modelo 25% acima do implícito
                            if ev < 20.0 or _conf_ratio < 1.25:
                                continue
                        elif odd_mkt > 3.0:
                            # odd 3.0–4.0: EV ≥ 15% + modelo 20% acima do implícito
                            if ev < 15.0 or _conf_ratio < 1.20:
                                continue

                    # ── Regra 5 — DRAW: zona morta EV 30-40% ──────────────────
                    # Backtest: 0 wins em 9 picks (30-35%: 7 picks; 35-40%: 2 picks).
                    # O D-C superestima probabilidade de empate quando há forte desequilíbrio
                    # λ/μ — o EV aparece alto por artefato, não por borda real.
                    # Faixas adjacentes (25-30% e >40%) têm ROI positivo confirmado.
                    if mercado == "DRAW" and 30.0 <= ev < 40.0:
                        continue

                    stake_mo = calcular_stake_final(
                        comp.get("kelly_fracao", 0), banca_atual, piso_kelly, teto_pct
                    )
                    if stake_mo <= 0:
                        continue

                    heur_adj_mo, heur_nota_mo = avaliar_heuristicas(
                        mercado,
                        xg_lam   = float(prev.get("lambda") or 1.3),
                        xg_mu    = float(prev.get("mu")     or 1.0),
                        xg_total = float(prev.get("xg_total") or 2.3),
                        ctx      = prev.get("dc_ctx"),
                    )
                    # DC: adiciona contexto de direção à nota heurística
                    if _dc_label and heur_nota_mo == "Contexto OK":
                        heur_nota_mo = _dc_label
                    elif _dc_label:
                        heur_nota_mo = f"{_dc_label} · {heur_nota_mo}"

                    candidatos_mo.append({
                        "fixture_id":    f_id,
                        "jogo":          jogo_nome,
                        "liga":          liga_nome_j,
                        "league_id":     l_id,
                        "league_season": j["league"].get("season", detectar_temporada_atual()),
                        "league_type":   j["league"].get("type", "League"),
                        "league_round":  j["league"].get("round", ""),
                        "home_id":       j["teams"]["home"]["id"],
                        "away_id":       j["teams"]["away"]["id"],
                        "mercado":      mercado,
                        "odd":          odd_mkt,
                        "prob_modelo":  prob_pct,
                        "prob_mercado": comp["prob_mercado_pct"],
                        "ev":           ev,
                        "divergencia":  comp["divergencia_pp"],
                        "kelly":        comp.get("kelly_fracao", 0),
                        "stake":        stake_mo,
                        "cobertura_ok": cobertura_ok,
                        "overround":    round(overround, 4),
                        "heur_adj":     heur_adj_mo,
                        "heur_nota":    heur_nota_mo,
                        "xg_lam":       float(prev.get("lambda")   or 0.0),
                        "xg_mu":        float(prev.get("mu")       or 0.0),
                        "xg_total":     float(prev.get("xg_total") or 0.0),
                        "dc_ctx":       prev.get("dc_ctx"),
                    })

            # Deduplicação — dois tracks independentes por fixture:
            #   • Direto  (HOME/DRAW/AWAY): melhor EV por jogo
            #   • DC      (1X/X2/12):       melhor EV por jogo
            # Um mesmo jogo pode ter UM pick em cada track — são mercados distintos
            # (HOME só ganha na vitória; 1X ganha em vitória OU empate). Tracks separados
            # garantem que um DC não seja silenciado por um HOME de maior EV do mesmo jogo.
            # Anti-phantom-DRAW: desconto de 15pp no EV de DRAW>40% para fins de dedup.
            _DC_MARKETS = {"1X", "X2", "12"}
            melhor_direto: dict[str, dict] = {}
            melhor_dc:     dict[str, dict] = {}
            for c in candidatos_mo:
                fid = c["fixture_id"]
                _ev_dedup = c["ev"]
                if c["mercado"] == "DRAW" and _ev_dedup > 40.0:
                    _ev_dedup -= 15.0
                c["_ev_dedup"] = _ev_dedup
                _track = melhor_dc if c["mercado"] in _DC_MARKETS else melhor_direto
                if fid not in _track or _ev_dedup > _track[fid].get("_ev_dedup", _track[fid]["ev"]):
                    _track[fid] = c
            ranking_mo = sorted(
                list(melhor_direto.values()) + list(melhor_dc.values()),
                key=lambda x: x["ev"], reverse=True,
            )

            # ── Regra 4 — Deduplicação cross-aba (Sniper ↔ Estrategista) ─
            # Um mesmo jogo não pode aparecer nas duas abas — dobra a exposição
            # ao mesmo resultado sem diversificação real. Sniper tem prioridade
            # (foi computado primeiro); Estrategista remove o fixture se já está lá.
            _sniper_ids = getattr(st.session_state, "_sniper_fixture_ids_local", None)
            # _sniper_fixture_ids é variável local definida na seção Sniper acima
            try:
                _cross_excluir = _sniper_fixture_ids  # noqa: F821
            except NameError:
                _cross_excluir = set()
            if _cross_excluir:
                _antes_cross = len(ranking_mo)
                ranking_mo = [p for p in ranking_mo
                              if p["fixture_id"] not in _cross_excluir]
                _removidos_cross = _antes_cross - len(ranking_mo)
                if _removidos_cross:
                    st.caption(
                        f"ℹ️ {_removidos_cross} jogo(s) removido(s) por duplicidade "
                        "com o Sniper de Gols (dedup cross-aba)."
                    )

            # ── Kelly Portfólio: armazena total e reescala stakes ────────
            _kelly_sum_mo = sum(p["kelly"] for p in ranking_mo)
            st.session_state["_kelly_dia_mo"] = _kelly_sum_mo
            _stk_mo_total = 0.0
            if _fator_portfolio < 0.999:
                for p in ranking_mo:
                    p["stake"] = max(piso_kelly, round(p["kelly"] * banca_atual * _fator_portfolio, 2))
            _stk_mo_total = sum(p["stake"] for p in ranking_mo)
            st.session_state["_stake_dia_mo"] = _stk_mo_total
            st.session_state["_n_picks_mo"]   = len(ranking_mo)

            if ranking_mo:
                # Separa mercados diretos de DC por direção para display distinto
                _ranking_direto = [p for p in ranking_mo if p.get("mercado") not in ("1X","X2","12")]
                _ranking_dc     = [p for p in ranking_mo if p.get("mercado") in ("1X","X2","12")]

                _n_direto = len(_ranking_direto)
                _n_dc     = len(_ranking_dc)
                st.markdown(
                    f"### 📊 Sinais de Resultado ({_n_direto} direto{'s' if _n_direto!=1 else ''}"
                    + (f" + {_n_dc} Dupla Chance" if _n_dc else "") + ")"
                )
                st.caption(
                    f"H1-HOME-Only · Phantom Draw EV>28% · Teto AWAY 22% · "
                    f"DC por direção: 1X âncora HOME>50%, X2 âncora AWAY>32% · "
                    f"Overround real por jogo · {len(candidatos_mo)} candidatos antes da dedup"
                )
                # Banner portfólio — esta aba
                _pct_b_mo  = (_stk_mo_total / banca_atual * 100) if banca_atual > 0 else 0
                _fator_str_mo = f" · escala {_fator_portfolio:.2f}×" if _fator_portfolio < 0.999 else " · Kelly individual"
                st.info(
                    f"💼 **Portfólio Resultados:** {len(ranking_mo)} picks · "
                    f"R$ {_stk_mo_total:.2f} ({_pct_b_mo:.1f}% da banca){_fator_str_mo}  \n"
                    f"📊 Veja aba Gols para total consolidado do dia"
                )

                _cor_mercado = {
                    "HOME": "#17a2b8", "DRAW": "#ffc107", "AWAY": "#6f42c1",
                    "1X": "#28a745",   "X2": "#fd7e14",   "12": "#dc3545",
                }

                def _render_pick_mo(i: int, p: dict, prefixo: str = "") -> None:
                    mkt = p.get("mercado", "—")
                    cor = _cor_mercado.get(mkt, "#888")
                    _e_dc = mkt in ("1X", "X2", "12")
                    cal_badge = " · Cal✓" if mkt == "HOME" and banco.params_ligas.get(
                        str(next((j["league"]["id"] for j in jogos_com_odds
                                  if str(j["fixture"]["id"]) == p["fixture_id"]), 0)), {}
                    ).get("calibradores", {}).get("1X2_HOME") else ""
                    cob_ico = "✅" if p.get("cobertura_ok") else "⚠️"
                    _dc_badge = " 🛡️ DC" if _e_dc else ""

                    st.markdown(
                        f"<div style='border-left:4px solid {cor};padding:10px 14px;"
                        f"margin-bottom:4px;background:#0e1117;border-radius:4px;"
                        + ("border:1px solid " + cor + "33;" if _e_dc else "") +
                        f"'>"
                        f"<div style='display:flex;justify-content:space-between;"
                        f"font-size:11px;color:#888;'>"
                        f"<span>{prefixo}#{i} · {p.get('liga', '—')} · {cob_ico}{cal_badge}</span>"
                        f"<span style='color:{cor};font-weight:bold;'>"
                        f"{mkt}{_dc_badge} &nbsp;·&nbsp; OR: {p.get('overround', 1.0):.3f}</span>"
                        f"</div>"
                        f"<div style='font-size:17px;font-weight:bold;color:white;margin:5px 0 3px;'>"
                        f"{p.get('jogo', '—')}"
                        f"</div>"
                        f"<div style='font-size:13px;color:#ccc;'>"
                        f"Odd <b>{p.get('odd', 0):.2f}</b> &nbsp;·&nbsp; "
                        f"Modelo <b>{p.get('prob_modelo', 0):.1f}%</b>"
                        f" vs Mercado {p.get('prob_mercado', 0):.1f}% &nbsp;·&nbsp; "
                        f"Delta <b>{p.get('divergencia', 0):+.1f}pp</b>"
                        f"</div>"
                        f"<div style='font-size:12px;color:#aaa;margin-top:2px;'>"
                        f"EV <span style='color:{cor};font-weight:bold;'>{p.get('ev', 0):+.1f}%</span> &nbsp;·&nbsp; "
                        f"Kelly {p.get('kelly', 0)*100:.1f}% &nbsp;·&nbsp; "
                        f"Stake: <b>R$ {p.get('stake', 0):.2f}</b>"
                        f"</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    _hadj_mo  = p.get("heur_adj",  0.0)
                    _hnota_mo = p.get("heur_nota", "Contexto OK")
                    if _hadj_mo > 0:
                        st.success(f"⬆️ {_hnota_mo}")
                    elif _hadj_mo < 0:
                        st.warning(f"⬇️ {_hnota_mo}")
                    else:
                        st.info(f"🔬 {_hnota_mo if _hnota_mo != 'Contexto OK' else 'Contexto OK — sem regras de forma disparadas'}")
                    # Botão de análise de contexto por pick
                    _render_pick_contexto(p)

                # Picks diretos (HOME/DRAW/AWAY)
                for i, p in enumerate(_ranking_direto, 1):
                    _render_pick_mo(i, p)

                # Dupla Chance por Direção — seção separada
                if _ranking_dc:
                    st.markdown("#### 🛡️ Dupla Chance por Direção")
                    st.caption(
                        "1X: modelo favorece mandante (P>50%) + odd sweet spot 1.18-1.60 · "
                        "X2: visitante com força real (P>32%) + odd sweet spot 1.22-1.68 · "
                        "Seguro de empate ancorado em direção — não depende da calibração de DRAW"
                    )
                    for i, p in enumerate(_ranking_dc, 1):
                        _render_pick_mo(i, p)

            elif jogos_com_odds:
                st.info(
                    "Nenhum sinal de resultado aprovado hoje. "
                    "Verifique se há odds de 1X2 disponíveis para os jogos carregados."
                )

# (tab_auditoria removed — motor diagnostics live in Calibração tab)
