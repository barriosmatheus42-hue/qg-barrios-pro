"""
QG Barrios PRO — Passo 4c: Treinamento dos Calibradores Isotônicos
====================================================================

Treina um MarketCalibrator por mercado de produção para cada liga calibrada,
usando os fixtures históricos que já estão em banco.historico_ligas (populados
pelo Delta Fetch do Passo 3).

Por que calibrar?
    Dixon-Coles emite probabilidades brutas que podem ter viés sistemático.
    Ex: modelo diz 60% para UNDER_25 mas, nos dados históricos, jogos onde
    o modelo previa ~60% terminaram Under 2.5 em apenas 54% das vezes.
    A correção isotônica é monotônica, não-paramétrica, e corrige esse viés
    sem destruir a ordenação relativa (um jogo com 70% bruto continua > 60%).

Pré-requisitos:
    1. banco_barrios_pro.json deve ter `params_ligas` (calibração MLE feita)
    2. banco_barrios_pro.json deve ter `historico_ligas` (bootstrap Delta Fetch feito)

Uso:
    python treinar_calibradores.py                   # treina todas as ligas
    python treinar_calibradores.py --liga 71         # só Brasileirão Série A
    python treinar_calibradores.py --minimo 50       # exige >= 50 amostras

Também importável em app_v3.py ou outros scripts:
    from treinar_calibradores import treinar_calibradores
"""

from __future__ import annotations

import json
import sys
import datetime as dt
from pathlib import Path

import numpy as np
from scipy.stats import poisson

from motor import (
    ParametrosLiga,
    MarketCalibrator,
    aplicar_shrinkage,
    MERCADOS_PRODUCAO,
)

# =========================================================================
# 1. CONFIGURAÇÃO
# =========================================================================

ARQUIVO_BANCO_PADRAO = "banco_barrios_pro.json"
N_MIN_AMOSTRAS       = 30   # mínimo de fixtures para treinar um calibrador

# Índice de soma de gols (precomputado uma vez — reutilizado em toda execução)
_IDX11 = np.arange(11)
_SOMA_GOLS = np.add.outer(_IDX11, _IDX11)   # shape (11, 11)


# =========================================================================
# 2. HELPERS INTERNOS
# =========================================================================

def _probs_producao_rapido(
    params: ParametrosLiga,
    home_id: int,
    away_id: int,
) -> dict | None:
    """
    Calcula probabilidades brutas apenas para os 8 mercados de produção.

    Versão otimizada de prever_jogo() + derivar_mercados():
    - 2 chamadas scipy vetorizadas (em vez de 121 chamadas escalares)
    - Ignora AH, PE, 1X2 (não são calibrados)
    - Aplica shrinkage bayesiano (idêntico à produção)

    Retorna dict {mercado: prob_frac [0, 1]} ou None se times ausentes.
    """
    th = params.times.get(home_id)
    ta = params.times.get(away_id)
    if th is None or ta is None:
        return None

    alpha_h, beta_h = aplicar_shrinkage(th["alpha"], th["beta"], th["n_jogos"])
    alpha_a, beta_a = aplicar_shrinkage(ta["alpha"], ta["beta"], ta["n_jogos"])
    lam = alpha_h * beta_a * params.home_advantage
    mu  = alpha_a * beta_h
    rho = params.rho

    # Matriz de placares 11×11 (vetorizada)
    pmf_h = poisson.pmf(_IDX11, lam)
    pmf_a = poisson.pmf(_IDX11, mu)
    M = np.outer(pmf_h, pmf_a)

    # Ajuste tau Dixon-Coles nos placares baixos
    M[0, 0] = max(M[0, 0] * (1.0 - lam * mu * rho), 1e-12)
    M[0, 1] = max(M[0, 1] * (1.0 + lam * rho),       1e-12)
    M[1, 0] = max(M[1, 0] * (1.0 + mu  * rho),       1e-12)
    M[1, 1] = max(M[1, 1] * (1.0 - rho),             1e-12)
    M /= M.sum()

    btts = float(M[1:, 1:].sum())
    return {
        "OVER_15":  float(M[_SOMA_GOLS > 1.5].sum()),
        "OVER_25":  float(M[_SOMA_GOLS > 2.5].sum()),
        "OVER_35":  float(M[_SOMA_GOLS > 3.5].sum()),
        "UNDER_15": float(M[_SOMA_GOLS < 1.5].sum()),
        "UNDER_25": float(M[_SOMA_GOLS < 2.5].sum()),
        "UNDER_35": float(M[_SOMA_GOLS < 3.5].sum()),
        "BTTS_YES": btts,
        "BTTS_NO":  1.0 - btts,
    }


def _resultado_mercado(mercado: str, hg: int, ag: int) -> int:
    """Retorna 1 se o mercado 'ocorreu', 0 caso contrário."""
    total = hg + ag
    if mercado == "OVER_15":  return int(total > 1.5)
    if mercado == "OVER_25":  return int(total > 2.5)
    if mercado == "OVER_35":  return int(total > 3.5)
    if mercado == "UNDER_15": return int(total < 1.5)
    if mercado == "UNDER_25": return int(total < 2.5)
    if mercado == "UNDER_35": return int(total < 3.5)
    if mercado == "BTTS_YES": return int(hg > 0 and ag > 0)
    if mercado == "BTTS_NO":  return int(not (hg > 0 and ag > 0))
    return 0


# =========================================================================
# 3. FUNÇÃO PRINCIPAL
# =========================================================================

def treinar_calibradores(
    arquivo_banco: str = ARQUIVO_BANCO_PADRAO,
    n_min_amostras: int = N_MIN_AMOSTRAS,
    ligas_filtro: list[int] | None = None,
    verbose: bool = True,
) -> dict:
    """
    Treina calibradores isotônicos e os persiste em banco_barrios_pro.json.

    Args:
        arquivo_banco:  caminho para o banco JSON local
        n_min_amostras: mínimo de fixtures para treinar (default 30)
        ligas_filtro:   lista de league_ids para processar (None = todas)
        verbose:        se True, imprime progresso por liga e mercado

    Returns:
        Relatório por liga:
        {
            "39": {
                "OVER_25": {
                    "n":                 380,   # fixtures usados no treino
                    "freq_real":         53.2,  # % de vezes que o mercado ocorreu
                    "prob_media_bruta":  57.1,  # prob média bruta do modelo (%)
                    "vies_pp":           +3.9,  # viés sistemático (+ = sobreestima)
                    "correcao_media_pp": 2.1,   # correção média em módulo (pp)
                }
            }
        }

    Efeito colateral:
        Salva params_ligas atualizados (com calibradores) em arquivo_banco.
        Faz re-leitura do arquivo antes de escrever para minimizar race condition
        com o app Streamlit rodando em paralelo.
    """
    arquivo = Path(arquivo_banco)
    if not arquivo.exists():
        raise FileNotFoundError(
            f"'{arquivo}' não encontrado. Execute a calibração MLE e o Delta Fetch primeiro."
        )

    with open(arquivo, "r", encoding="utf-8") as f:
        banco = json.load(f)

    params_ligas    = banco.get("params_ligas", {})
    historico_ligas = banco.get("historico_ligas", {})

    if not params_ligas:
        raise ValueError("params_ligas está vazio — calibre as ligas (MLE) antes de treinar calibradores.")
    if not historico_ligas:
        raise ValueError(
            "historico_ligas está vazio — execute o bootstrap do Delta Fetch (Passo 3) primeiro."
        )

    ligas_comuns = set(params_ligas.keys()) & set(historico_ligas.keys())
    if ligas_filtro:
        ligas_comuns = ligas_comuns & {str(lid) for lid in ligas_filtro}

    if verbose:
        print("=" * 68)
        print("TREINAMENTO DE CALIBRADORES ISOTONICOS — QG Barrios PRO Passo 4c")
        print("=" * 68)
        print(f"Ligas com params E historico: {len(ligas_comuns)}/{len(params_ligas)}")
        print(f"Min amostras por mercado: {n_min_amostras}")
        if ligas_filtro:
            print(f"Filtro de ligas: {ligas_filtro}")
        print()

    t0 = dt.datetime.now()
    relatorio: dict = {}
    # Acumula novos params em memória — escrita única no final
    novos_params_ligas: dict = {}

    for liga_str in sorted(ligas_comuns, key=lambda x: int(x)):
        registros = historico_ligas[liga_str].get("registros", [])

        if len(registros) < n_min_amostras:
            if verbose:
                print(f"  [{liga_str:5s}] PULAR  : {len(registros)} fixtures (min={n_min_amostras})")
            continue

        try:
            params = ParametrosLiga.from_dict(params_ligas[liga_str])
        except Exception as e:
            if verbose:
                print(f"  [{liga_str:5s}] ERRO   : falha ao carregar params — {e}")
            continue

        # Remove calibradores antigos: precisamos das probs BRUTAS para treino
        params.calibradores = {}

        # ── Extração dos pares (prob_bruta_frac, resultado_real) ─────────────
        pares: dict[str, tuple[list, list]] = {m: ([], []) for m in MERCADOS_PRODUCAO}
        n_ok = n_skip = 0

        for reg in registros:
            try:
                home_id = int(reg["home_id"])
                away_id = int(reg["away_id"])
                hg      = int(float(reg["home_goals"]))
                ag      = int(float(reg["away_goals"]))
            except (KeyError, TypeError, ValueError):
                n_skip += 1
                continue

            if home_id not in params.times or away_id not in params.times:
                # Time rebaixado ou de copa — não está no modelo atual
                n_skip += 1
                continue

            probs = _probs_producao_rapido(params, home_id, away_id)
            if probs is None:
                n_skip += 1
                continue

            for mercado in MERCADOS_PRODUCAO:
                pares[mercado][0].append(probs[mercado])
                pares[mercado][1].append(_resultado_mercado(mercado, hg, ag))
            n_ok += 1

        # ── Treino por mercado ───────────────────────────────────────────────
        calibradores_liga: dict[str, MarketCalibrator] = {}
        relatorio_liga: dict = {}

        for mercado, (X, Y) in pares.items():
            if len(X) < n_min_amostras:
                continue

            freq_real  = float(np.mean(Y))
            prob_media = float(np.mean(X))
            vies_pp    = (prob_media - freq_real) * 100.0

            cal = MarketCalibrator(mercado=mercado)
            cal.treinar(X, Y)
            if cal.n_amostras < n_min_amostras:
                continue

            # Correção média: quanto o calibrador muda cada prob em módulo
            correcao_media = float(np.mean([
                abs(cal.calibrar(p * 100.0) - p * 100.0) for p in X
            ]))

            calibradores_liga[mercado] = cal
            relatorio_liga[mercado] = {
                "n":                 n_ok,
                "freq_real":         round(freq_real  * 100.0, 1),
                "prob_media_bruta":  round(prob_media * 100.0, 1),
                "vies_pp":           round(vies_pp, 1),
                "correcao_media_pp": round(correcao_media, 1),
            }

        if not calibradores_liga:
            if verbose:
                print(f"  [{liga_str:5s}] PULAR  : nenhum mercado com {n_min_amostras}+ amostras")
            continue

        # Injeta e acumula
        params.calibradores = calibradores_liga
        novos_params_ligas[liga_str] = params.to_dict()
        relatorio[liga_str] = relatorio_liga

        if verbose:
            n_cal = len(calibradores_liga)
            print(f"  [{liga_str:5s}] OK     : {n_ok} fixtures | "
                  f"{n_cal}/{len(MERCADOS_PRODUCAO)} calibradores treinados "
                  f"({n_skip} fixtures pulados — times fora do modelo)")
            for mkt in sorted(relatorio_liga):
                info = relatorio_liga[mkt]
                sinal = "+" if info["vies_pp"] >= 0 else ""
                icon = (
                    "🟢" if abs(info["vies_pp"]) < 2 else
                    "🟡" if abs(info["vies_pp"]) < 5 else "🔴"
                )
                print(
                    f"    {icon} {mkt:12s}  n={info['n']:4d}  "
                    f"real={info['freq_real']:5.1f}%  "
                    f"bruta={info['prob_media_bruta']:5.1f}%  "
                    f"vies={sinal}{info['vies_pp']:+.1f}pp  "
                    f"corr=+-{info['correcao_media_pp']:.1f}pp"
                )

    # ── Escrita final: re-lê arquivo antes de gravar ──────────────────────────
    # Minimiza janela de race condition com o app Streamlit rodando em paralelo.
    # Só atualiza params_ligas — picks, depositos, historico_ligas ficam intactos.
    if novos_params_ligas:
        with open(arquivo, "r", encoding="utf-8") as f:
            banco_final = json.load(f)
        for liga_str, params_dict in novos_params_ligas.items():
            if liga_str in banco_final.get("params_ligas", {}):
                banco_final["params_ligas"][liga_str] = params_dict
        with open(arquivo, "w", encoding="utf-8") as f:
            json.dump(banco_final, f, indent=2, default=str)

    elapsed = (dt.datetime.now() - t0).total_seconds()
    if verbose:
        print()
        print("=" * 68)
        if novos_params_ligas:
            print(f"CONCLUIDO: {len(novos_params_ligas)} ligas calibradas em {elapsed:.1f}s")
            print(f"Arquivo atualizado: {arquivo}")
        else:
            print("AVISO: nenhum calibrador foi treinado.")
            print("Verifique se historico_ligas tem dados (bootstrap Delta Fetch).")
        print("=" * 68)

    return relatorio


# =========================================================================
# 4. CLI
# =========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Treina calibradores isotônicos por mercado para todas as ligas."
    )
    parser.add_argument(
        "--banco",
        default=ARQUIVO_BANCO_PADRAO,
        help=f"Caminho do arquivo JSON (default: {ARQUIVO_BANCO_PADRAO})"
    )
    parser.add_argument(
        "--minimo",
        type=int,
        default=N_MIN_AMOSTRAS,
        help=f"Mínimo de fixtures por mercado (default: {N_MIN_AMOSTRAS})"
    )
    parser.add_argument(
        "--liga",
        type=int,
        nargs="+",
        default=None,
        help="IDs de ligas específicas (ex: --liga 71 72). Omitir = todas."
    )
    args = parser.parse_args()

    treinar_calibradores(
        arquivo_banco=args.banco,
        n_min_amostras=args.minimo,
        ligas_filtro=args.liga,
        verbose=True,
    )
