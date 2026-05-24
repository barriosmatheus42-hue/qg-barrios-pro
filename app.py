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
EV_CONFIG_PROD  = EvConfig(ev_min_over=5.0,  ev_min_under=20.0)

# ── Filtros 1X2 / Dupla Chance (H1-HOME-Only, homologados no laboratório V6) ──
# Faixas de EV validadas por análise granular de 2349 picks / 12 janelas walk-forward.
# Isotônico aplicado APENAS em HOME — DRAW/AWAY usam probabilidade pura RAW do D-C.
_1X2_MERCADOS   = {"HOME", "DRAW", "AWAY", "1X", "X2", "12"}

_EV_MIN: dict   = {"HOME": 10.0, "DRAW": 28.0, "AWAY": 7.0,  "1X": 8.0,  "X2": 8.0,  "12": 8.0}
_EV_MAX: dict   = {"HOME": 15.0, "DRAW": 80.0, "AWAY": 22.0, "1X": 18.0, "X2": 18.0, "12": 18.0}
_PROB_MIN: dict = {"HOME": 45.0, "DRAW": 22.0, "AWAY": 28.0, "1X": 65.0, "X2": 65.0, "12": 65.0}
_ODD_MIN: dict  = {"HOME": 1.80, "DRAW": 2.80, "AWAY": 1.60, "1X": 1.25, "X2": 1.25, "12": 1.25}
# Teto de odd por mercado — controla variância sem cortar EV. DRAW=6.50 elimina bets
# tipo "loteria" (hit rate ~14%) onde o D-C está no limite de calibração confiável.
_ODD_MAX: dict  = {"HOME": 3.50, "DRAW": 6.50, "AWAY": 5.00, "1X": 2.50, "X2": 2.50, "12": 2.50}

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
        adj -= 6.0
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
                adj += 6.0
                notas.append(f"Empate recorrente: H {h_draws_last5}E, A {a_draws_last5}E em {h_n_last5}j")
            elif h_draws_last5 + a_draws_last5 >= 5:
                adj += 4.0
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
                    adj += 7.0
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
        "a_shots_on_avg": None, "a_shots_total_avg": None, "a_possession_avg": None,
        "a_corners_avg": None, "a_yellows_avg": None, "a_saves_avg": None, "a_fouls_avg": None,
        "a_shots_per_goal": None,
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
            }
            rename_a = {
                "a_shots_on": "shots_on", "a_shots_total": "shots_total",
                "a_possession": "possession", "a_corners": "corners",
                "a_yellows": "yellows", "a_saves": "saves", "a_fouls": "fouls",
            }

            cols_scout = ["shots_on", "shots_total", "possession", "corners", "yellows", "saves", "fouls"]

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
    "risk_ligas_bloqueadas":  [],
}
for _rk, _rv in _risk_defaults.items():
    if _rk not in st.session_state:
        st.session_state[_rk] = _rv

with st.sidebar:
    st.markdown("## 👑 QG Barrios PRO V3")
    st.caption("Motor: Dixon-Coles (MLE) · Sem incremental · regras-v2-HC23-29-hardblock")

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
        help="Informe o saldo atual da sua conta. Usado para calcular a stake pelo Kelly.",
    )
    st.session_state["_saldo_atual"] = banca_atual

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
    season        = st.number_input("Temporada (ano)", value=detectar_temporada_atual(), step=1)


# =========================================================================
# 5. CABEÇALHO
# =========================================================================

st.title("QG Barrios PRO V3")

n_calibradas = len(banco.params_ligas)

col_h1, col_h2 = st.columns(2)
col_h1.metric("Ligas calibradas", f"{n_calibradas}/{len(LIGAS_SUPORTADAS)}")
col_h2.metric("Banca atual", f"R$ {banca_atual:.2f}")

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
    _n_ligas        = len(LIGAS_SUPORTADAS)   # fonte única — atualiza sozinho com a lista
    _custo_por_liga = CUSTO_ESTIMADO_HISTORICO_LIGA + CUSTO_ESTIMADO_XG_LIGA

    st.markdown(f"### Status das ligas ({_n_ligas} configuradas)")
    st.caption(
        f"Calibração manual: clique 'Calibrar TODAS' segunda e quinta. "
        f"Custo estimado por liga: ~{_custo_por_liga} créditos "
        f"(histórico + xG blend peso={PESO_XG_PRODUCAO})."
    )

    # Tabela de status
    rows_status = []
    for league_id, nome in LIGAS_SUPORTADAS.items():
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

    custo_total = _n_ligas * _custo_por_liga
    st.info(
        f"Custo estimado para calibrar todas as {_n_ligas} ligas: "
        f"~{custo_total} créditos (inclui xG via /fixtures/statistics, peso={PESO_XG_PRODUCAO})."
    )

    # ── Trava de Custo — Calibrar TODAS (2 fases) ────────────────────
    st.markdown("#### 🔄 Calibração com Delta Fetch")
    st.caption(
        "**Passo 1** analisa quantos jogos novos existem desde o último download "
        f"(custo: ~{_n_ligas * 2} créditos para as listas). "
        "**Passo 2** confirma o download de xG e executa o MLE. "
        "Nenhum crédito de xG é gasto antes da sua confirmação."
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
            st.session_state["delta_snapshot"] = preview
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
            com_novos = list(LIGAS_SUPORTADAS.keys())

        total_ops    = len(inativas) + len(com_novos)
        progress_bar = st.progress(0)
        status_box   = st.empty()
        erros: list[str] = []
        timeouts: list[str] = []
        tocadas = 0
        op_idx  = 0

        # Passo 2a: renova timestamps das ligas inativas (sem MLE, sem créditos)
        for lid in inativas:
            nome = LIGAS_SUPORTADAS.get(lid, f"Liga {lid}")
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
            nome = LIGAS_SUPORTADAS.get(lid, f"Liga {lid}")
            status_box.info(f"[{op_idx+1}/{total_ops}] ⚙️ Calibrando **{nome}**…")
            try:
                dm.obter_params_liga(lid, season, forcar_recalibracao=True)
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

        # Relatório final
        _erros_cal  = [e for e in erros if "(touch)" not in e]
        _n_ok_cal   = len(com_novos) - len(timeouts) - len(_erros_cal)
        _partes_rel = []
        if tocadas:
            _partes_rel.append(f"{tocadas} liga(s) inativa(s) marcadas como Frescas")
        if _n_ok_cal > 0:
            _partes_rel.append(f"{_n_ok_cal} liga(s) recalibrada(s) com sucesso")
        if aguardando:
            _partes_rel.append(
                f"{len(aguardando)} liga(s) aguardando início (sem dados na API — créditos preservados)"
            )

        if timeouts:
            st.warning(
                f"⏱️ {len(timeouts)} liga(s) com timeout (MLE > {TIMEOUT_CALIBRACAO_SEGUNDOS}s):\n"
                + "\n".join(f"• {n}" for n in timeouts)
            )
        if erros:
            st.warning("⚠️ Falhas:\n" + "\n".join(f"• {e}" for e in erros))
        if _partes_rel:
            st.success("✅ " + " · ".join(_partes_rel) + ".")
        elif not erros and not timeouts:
            st.info("Nenhuma ação executada.")

        try:
            st.session_state["banco"] = dm.banco_em_memoria()
        except Exception:
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
                        if dm.ultimo_save_jsonbin_ok:
                            st.success(f"{l_nome} calibrada e salva na nuvem! Recarregando...")
                        else:
                            st.warning(
                                f"⚠️ {l_nome} calibrada mas **falhou ao salvar no JSONBin**. "
                                "Os params estão no arquivo local — serão perdidos no próximo restart. "
                                "Verifique a quota do JSONBin ou tente novamente."
                            )
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
        params = ParametrosLiga.from_dict(banco.params_ligas[str(l_id)])
        h_id   = j["teams"]["home"]["id"]
        a_id   = j["teams"]["away"]["id"]
        if f_id not in previsoes:
            prev = prever_jogo(params, h_id, a_id, aplicar_shrink=True, cobertura_minima=10)
            previsoes[f_id] = {k: prev.get(k) for k in
                               ("lambda", "mu", "xg_total", "mercados", "flags",
                                "cobertura_ok", "erro")}
        # dc_ctx sempre recalculado dos params atuais (nunca fica stale por recalibração)
        h_data = params.times.get(h_id, {})
        a_data = params.times.get(a_id, {})
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
        previsoes[f_id]["dc_ctx"] = dc_ctx
    banco.datas[data_str]["previsoes"] = previsoes
    dm.salvar_banco(banco)

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
                if odd_val <= 1.0 or odd_val < odd_min_save:
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

                if score < SCORE_MINIMO_RANKING:
                    continue

                candidatos.append({
                    "fixture_id":   f_id,
                    "jogo":         jogo_nome,
                    "liga":         liga_nome_j,
                    "mercado":      mercado,
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
                params_j   = ParametrosLiga.from_dict(banco.params_ligas[str(l_id)])
                cal_home   = params_j.calibradores.get("1X2_HOME")
                p_home_raw = prev["mercados"]["HOME"]
                p_home     = cal_home.calibrar(p_home_raw) if cal_home else p_home_raw
                p_draw     = prev["mercados"]["DRAW"]
                p_away     = prev["mercados"]["AWAY"]

                # Odds de Dupla Chance derivadas do mercado 1X2
                odd_1x = (1.0 / (1.0/odd_h + 1.0/odd_d)) if odd_h > 1 and odd_d > 1 else 0.0
                odd_x2 = (1.0 / (1.0/odd_d + 1.0/odd_a)) if odd_d > 1 and odd_a > 1 else 0.0
                odd_12 = (1.0 / (1.0/odd_h + 1.0/odd_a)) if odd_h > 1 and odd_a > 1 else 0.0

                candidatos_jogo = [
                    ("HOME", p_home, odd_h),
                    ("DRAW", p_draw, odd_d),
                    ("AWAY", p_away, odd_a),
                    ("1X",  prev["mercados"]["1X"],  odd_1x),
                    ("X2",  prev["mercados"]["X2"],  odd_x2),
                    ("12",  prev["mercados"]["12"],  odd_12),
                ]

                for mercado, prob_pct, odd_mkt in candidatos_jogo:
                    if odd_mkt <= 1.0:
                        continue
                    comp = comparar_com_mercado(prob_pct, odd_mkt, overround)
                    if "erro" in comp:
                        continue

                    ev   = comp["ev_pct"]
                    ev_min  = _EV_MIN[mercado]
                    ev_max  = _EV_MAX[mercado]
                    prob_mn = _PROB_MIN[mercado]
                    # Piso efetivo: máximo entre o mínimo do backtest e o slider global
                    eff_odd_mn = max(_ODD_MIN[mercado], odd_min_save)
                    odd_mx     = _ODD_MAX[mercado]

                    if not (ev_min <= ev <= ev_max
                            and prob_pct >= prob_mn
                            and eff_odd_mn <= odd_mkt <= odd_mx):
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

                    candidatos_mo.append({
                        "fixture_id":   f_id,
                        "jogo":         jogo_nome,
                        "liga":         liga_nome_j,
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
                    })

            # Deduplicação: um mercado por jogo (maior EV dentro da faixa aprovada)
            melhor_mo: dict[str, dict] = {}
            for c in candidatos_mo:
                fid = c["fixture_id"]
                if fid not in melhor_mo or c["ev"] > melhor_mo[fid]["ev"]:
                    melhor_mo[fid] = c
            ranking_mo = sorted(melhor_mo.values(), key=lambda x: x["ev"], reverse=True)

            if ranking_mo:
                st.markdown(
                    f"### 📊 Sinais de Resultado ({len(ranking_mo)} entrada"
                    f"{'s' if len(ranking_mo) > 1 else ''})"
                )
                st.caption(
                    f"H1-HOME-Only · Phantom Draw EV>28% · Teto AWAY 22% · "
                    f"Overround real por jogo · {len(candidatos_mo)} candidatos antes da dedup"
                )

                _cor_mercado = {
                    "HOME": "#17a2b8", "DRAW": "#ffc107", "AWAY": "#6f42c1",
                    "1X": "#28a745",   "X2": "#fd7e14",   "12": "#dc3545",
                }

                for i, p in enumerate(ranking_mo, 1):
                    mkt = p.get("mercado", "—")
                    cor = _cor_mercado.get(mkt, "#888")
                    cal_badge = " · Cal✓" if mkt == "HOME" and banco.params_ligas.get(
                        str(next((j["league"]["id"] for j in jogos_com_odds
                                  if str(j["fixture"]["id"]) == p["fixture_id"]), 0)), {}
                    ).get("calibradores", {}).get("1X2_HOME") else ""
                    cob_ico = "✅" if p.get("cobertura_ok") else "⚠️"

                    st.markdown(
                        f"<div style='border-left:4px solid {cor};padding:10px 14px;"
                        f"margin-bottom:4px;background:#0e1117;border-radius:4px;'>"
                        f"<div style='display:flex;justify-content:space-between;"
                        f"font-size:11px;color:#888;'>"
                        f"<span>#{i} · {p.get('liga', '—')} · {cob_ico}{cal_badge}</span>"
                        f"<span style='color:{cor};font-weight:bold;'>"
                        f"{mkt} &nbsp;·&nbsp; OR: {p.get('overround', 1.0):.3f}</span>"
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
                        st.info(f"🔬 Contexto OK — sem regras de forma disparadas")

            elif jogos_com_odds:
                st.info(
                    "Nenhum sinal de resultado aprovado hoje. "
                    "Verifique se há odds de 1X2 disponíveis para os jogos carregados."
                )

# (tab_auditoria removed — motor diagnostics live in Calibração tab)
