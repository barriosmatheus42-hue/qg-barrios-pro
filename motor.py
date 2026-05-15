"""
QG Barrios PRO - Motor V2 (Dixon-Coles real)
=============================================

Motor matemático puro. Standalone. SEM dependência de Streamlit ou API.

Implementa Dixon-Coles (1997) "Modelling Association Football Scores and
Inefficiencies in the Football Betting Market":

    P(X=x, Y=y) = tau(x,y; lambda, mu) * Pois(x; lambda) * Pois(y; mu)

onde:
    lambda = alpha_home * beta_away * gamma   (gols esperados time da casa)
    mu     = alpha_away * beta_home           (gols esperados time visitante)

alpha_i = força ofensiva do time i
beta_i  = força defensiva do time i (quanto MAIOR, pior defesa)
gamma   = vantagem de jogar em casa (por liga)
tau     = ajuste de correlação para placares baixos:
            tau(0,0) = 1 - lambda*mu*rho
            tau(0,1) = 1 + lambda*rho
            tau(1,0) = 1 + mu*rho
            tau(1,1) = 1 - rho
            tau(.,.) = 1                       (qualquer outro placar)

Restrições do MLE:
    soma(alpha_i) / n_times = 1.0    (identificabilidade do modelo)
    beta_i > 0, alpha_i > 0, gamma > 0
    rho em (-1, 1)

Decay temporal:
    L(theta) = soma_t  exp(-xi * dias_atras_t) * log P(jogo_t | theta)

Autor: QG Barrios PRO
Versão: 2.0.0
"""

from __future__ import annotations

import math
import json
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

try:
    from sklearn.isotonic import IsotonicRegression as _IsotonicRegression
    _SKLEARN_OK = True
except ImportError:
    _IsotonicRegression = None  # type: ignore
    _SKLEARN_OK = False


# =========================================================================
# 1. ESTRUTURAS DE DADOS
# =========================================================================

class MarketCalibrator:
    """
    Calibrador isotônico por mercado (Dossiê v8 — Passo 4).

    Problema que resolve: Dixon-Coles emite probabilidades brutas que podem
    ter viés sistemático em relação às frequências observadas. Ex.: o modelo
    emite 55% para UNDER_25, mas nos dados históricos jogos com ~55% bruto
    terminaram Under_25 em apenas 49% das vezes — viés de +6pp.

    Solução: IsotonicRegression(X=prob_bruta, Y=resultado_real) aprende a
    curva de calibração monotônica sem assumir forma paramétrica. Resultado:
    probabilidades calibradas ficam alinhadas com frequências empíricas,
    melhorando a estimativa de EV e Kelly.

    Ciclo de vida:
        1. treinar()    — offline, com pares (prob_bruta, resultado) históricos
        2. to_dict()    — serializado dentro de ParametrosLiga → banco JSON
        3. from_dict()  — reconstituído a cada carregar_banco()
        4. calibrar()   — chamado em prever_jogo() para cada mercado de produção

    Fallback seguro: se sklearn ausente ou n_amostras < 30, calibrar() retorna
    a probabilidade bruta sem alteração — o sistema continua funcionando.
    """

    def __init__(self, mercado: str):
        self.mercado:    str   = mercado
        self.n_amostras: int   = 0
        self._X:         list  = []
        self._Y:         list  = []
        self._iso:       object = None   # IsotonicRegression | None

    # ── Treinamento ──────────────────────────────────────────────────
    def treinar(self, probs_brutas_frac: list, resultados: list) -> None:
        """
        Ajusta o calibrador com dados históricos.

        Args:
            probs_brutas_frac: probabilidades brutas do modelo em fração [0, 1]
            resultados:        resultado real — 1 se evento ocorreu, 0 se não
        """
        if not _SKLEARN_OK:
            return   # fallback silencioso — prob bruta será usada
        if len(probs_brutas_frac) < 30:
            return   # dados insuficientes para calibração confiável

        self._X = [float(x) for x in probs_brutas_frac]
        self._Y = [int(y)   for y in resultados]
        iso = _IsotonicRegression(out_of_bounds="clip")
        iso.fit(self._X, self._Y)
        self._iso = iso
        self.n_amostras = len(self._X)

    # ── Inferência ───────────────────────────────────────────────────
    def calibrar(self, prob_bruta_pct: float) -> float:
        """
        Retorna probabilidade calibrada em porcentagem.

        Recebe prob em % (convenção do sistema), converte internamente para
        fração, aplica IsotonicRegression, devolve em %. Se calibrador não
        estiver treinado, devolve prob_bruta_pct sem alteração.
        """
        if self._iso is None or self.n_amostras < 30:
            return prob_bruta_pct
        p_frac = float(np.clip(prob_bruta_pct / 100.0, 0.0, 1.0))
        p_cal  = float(self._iso.predict([p_frac])[0])
        return float(np.clip(p_cal, 0.0, 1.0)) * 100.0

    # ── Serialização ─────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "mercado":    self.mercado,
            "n_amostras": self.n_amostras,
            "X":          self._X,
            "Y":          self._Y,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketCalibrator":
        obj = cls(mercado=d["mercado"])
        X = d.get("X", [])
        Y = d.get("Y", [])
        n = d.get("n_amostras", len(X))
        if _SKLEARN_OK and n >= 30 and X and Y:
            obj._X = [float(x) for x in X]
            obj._Y = [int(y)   for y in Y]
            iso = _IsotonicRegression(out_of_bounds="clip")
            iso.fit(obj._X, obj._Y)
            obj._iso = iso
            obj.n_amostras = n
        return obj

    def __repr__(self) -> str:
        treinado = f"n={self.n_amostras}" if self._iso else "não treinado"
        return f"MarketCalibrator(mercado={self.mercado!r}, {treinado})"


@dataclass
class ParametrosLiga:
    """Parâmetros calibrados de uma liga via MLE."""
    league_id: int
    season: int
    times: dict[int, dict[str, float]]   # {team_id: {"alpha": x, "beta": y, "n_jogos": z}}
    home_advantage: float                 # gamma
    rho: float                            # ajuste D-C para placares baixos
    xi: float                             # decay temporal
    media_liga_gols: float                # média de gols/jogo da liga (para shrinkage)
    calibrado_em: str = field(default_factory=lambda: dt.datetime.now().isoformat())
    n_jogos_calibracao: int = 0
    log_likelihood: float = 0.0
    nomes_times: dict = field(default_factory=dict)    # {team_id: "Nome do Time"}
    seasons_incluidas: list = field(default_factory=list)
    raio_x_times: dict = field(default_factory=dict)
    # raio_x_times: {team_id: {n_atual, n_historico, n_total, ultimo_jogo,
    #                           na_temporada_atual, no_modelo}}
    # "na_temporada_atual" = False → time aparece só no histórico (ex: rebaixado)
    # "no_modelo"          = False → filtrado pelo min_aparicoes ou por não ser da temporada atual
    xg_ativo: bool = False          # True se calibração usou blend xG (Dossiê v8 — Parte 3)
    calibradores: dict = field(default_factory=dict)
    # {mercado_str: MarketCalibrator} — treinados offline (Dossiê v8 — Passo 4).
    # Chave ausente = mercado sem calibrador (usa prob bruta).
    # Não é serializado por asdict() — to_dict() o trata manualmente.

    def to_dict(self) -> dict:
        # Construção manual (substitui asdict) para serializar calibradores
        # corretamente sem expor objetos sklearn ao JSON.
        return {
            "league_id":          self.league_id,
            "season":             self.season,
            "times":              self.times,
            "home_advantage":     self.home_advantage,
            "rho":                self.rho,
            "xi":                 self.xi,
            "media_liga_gols":    self.media_liga_gols,
            "calibrado_em":       self.calibrado_em,
            "n_jogos_calibracao": self.n_jogos_calibracao,
            "log_likelihood":     self.log_likelihood,
            "nomes_times":        self.nomes_times,
            "seasons_incluidas":  self.seasons_incluidas,
            "raio_x_times":       self.raio_x_times,
            "xg_ativo":           self.xg_ativo,
            "calibradores":       {k: v.to_dict() for k, v in self.calibradores.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ParametrosLiga":
        times = {int(k): v for k, v in d["times"].items()}
        nomes_raw = d.get("nomes_times", {})
        nomes_times = {int(k): v for k, v in nomes_raw.items()} if nomes_raw else {}
        raio_x_raw = d.get("raio_x_times", {})
        raio_x_times = {int(k): v for k, v in raio_x_raw.items()} if raio_x_raw else {}
        # Backward-compatible: entradas antigas sem calibradores retornam dict vazio
        calibradores = {
            k: MarketCalibrator.from_dict(v)
            for k, v in d.get("calibradores", {}).items()
        }
        return cls(
            league_id=d["league_id"],
            season=d["season"],
            times=times,
            home_advantage=d["home_advantage"],
            rho=d["rho"],
            xi=d["xi"],
            media_liga_gols=d["media_liga_gols"],
            calibrado_em=d.get("calibrado_em", ""),
            n_jogos_calibracao=d.get("n_jogos_calibracao", 0),
            log_likelihood=d.get("log_likelihood", 0.0),
            nomes_times=nomes_times,
            seasons_incluidas=d.get("seasons_incluidas", []),
            raio_x_times=raio_x_times,
            xg_ativo=d.get("xg_ativo", False),
            calibradores=calibradores,
        )


# =========================================================================
# 2. AJUSTE DIXON-COLES (TAU) — IMPLEMENTAÇÃO CORRETA
# =========================================================================

def tau_dixon_coles(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """
    Ajuste de correlação D-C para placares baixos.

    BUG FIXADO DO V6.1: a versão anterior aplicava (1 - rho) em (0,0) e (1 + rho)
    em (1,1), que é EXATAMENTE o oposto do paper original. Resultado: empates
    1x1 inflados, 0x0 esmagados, todas as probs de Under/BTTS distorcidas.

    A direção correta (Dixon & Coles 1997):
        (0,0): 1 - lambda*mu*rho      -> AUMENTA prob de 0x0 (rho > 0)
        (0,1): 1 + lambda*rho         -> DIMINUI prob de 0x1
        (1,0): 1 + mu*rho             -> DIMINUI prob de 1x0
        (1,1): 1 - rho                -> DIMINUI prob de 1x1
        outros: 1
    """
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def prob_placar(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Probabilidade do placar (x, y) sob Dixon-Coles."""
    p = poisson.pmf(x, lam) * poisson.pmf(y, mu) * tau_dixon_coles(x, y, lam, mu, rho)
    return max(p, 1e-12)   # evita log(0) no MLE


# =========================================================================
# 3. LIKELIHOOD E CALIBRAÇÃO MLE
# =========================================================================

def _neg_log_likelihood(
    params: np.ndarray,
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    pesos: np.ndarray,
    n_times: int,
) -> float:
    """
    Função objetivo do MLE.

    params layout:
        [0 : n_times]            -> alpha_i (ataque)
        [n_times : 2*n_times]    -> beta_i  (defesa)
        [2*n_times]              -> gamma   (home advantage)
        [2*n_times + 1]          -> rho     (D-C tau parameter)
    """
    alphas = params[:n_times]
    betas = params[n_times:2 * n_times]
    gamma = params[2 * n_times]
    rho = params[2 * n_times + 1]

    # Penalidades para sair do domínio (alpha, beta > 0; rho em (-0.35, 0.35))
    if np.any(alphas <= 0) or np.any(betas <= 0) or gamma <= 0:
        return 1e10
    if rho <= -0.35 or rho >= 0.35:
        return 1e10

    lam = alphas[home_idx] * betas[away_idx] * gamma   # gols esperados casa
    mu = alphas[away_idx] * betas[home_idx]             # gols esperados fora

    # Log-likelihood ponderada de cada partida
    log_p_home = home_goals * np.log(lam) - lam - np.array([math.lgamma(g + 1) for g in home_goals])
    log_p_away = away_goals * np.log(mu) - mu - np.array([math.lgamma(g + 1) for g in away_goals])

    # Ajuste tau (só relevante em placares baixos)
    log_tau = np.zeros(len(home_idx))
    for i, (x, y) in enumerate(zip(home_goals, away_goals)):
        log_tau[i] = math.log(max(tau_dixon_coles(int(x), int(y), lam[i], mu[i], rho), 1e-12))

    log_lik = pesos * (log_p_home + log_p_away + log_tau)
    return -np.sum(log_lik)


def calibrar_liga(
    df_jogos: pd.DataFrame,
    league_id: int,
    season: int,
    xi: float = 0.0019,
    data_referencia: Optional[dt.datetime] = None,
    min_aparicoes_time: int = 3,
    max_times: int = 80,
    nomes_times: Optional[dict] = None,
    seasons_incluidas: Optional[list] = None,
    peso_xg: float = 0.0,
) -> ParametrosLiga:
    """
    Calibra parâmetros Dixon-Coles para uma liga via MLE.

    df_jogos colunas esperadas:
        - home_id, away_id (int)
        - home_goals, away_goals (int)
        - date (datetime ou string ISO)

    xi padrão = 0.0019/dia => peso 0.5 em ~365 dias (paper original).

    min_aparicoes_time (padrão=3):
        Times com menos de N aparições são excluídos do MLE.
        Crítico para copas nacionais (FA Cup, Copa do Brasil, DFB Pokal, etc.)
        que têm 300-400 times nas fases preliminares — sem este filtro o SLSQP
        recebe 800+ parâmetros e trava por horas.

        Times eliminados nas primeiras fases têm ≤ 2 jogos e não oferecem
        informação estatística relevante para análise das fases principais.

    max_times (padrão=80):
        Teto absoluto de times no MLE. Acima disso, mantém os N com mais jogos.
        Protege contra casos extremos (ex: qualificação europeia multilateral).
    """
    if data_referencia is None:
        data_referencia = dt.datetime.now()

    df = df_jogos.copy()
    df = df.dropna(subset=["home_id", "away_id", "home_goals", "away_goals"])
    df["home_id"] = df["home_id"].astype(int)
    df["away_id"] = df["away_id"].astype(int)
    df["home_goals"] = pd.to_numeric(df["home_goals"], errors="coerce")
    df["away_goals"] = pd.to_numeric(df["away_goals"], errors="coerce")

    # ── xG BLEND (Dossiê v8 — Parte 3, peso_xg validado = 0.5) ──────────────
    # effective_goals = (1 - peso_xg) * real_goals + peso_xg * xg
    # Aplicado apenas em linhas onde xg_home E xg_away estão presentes (cobertura completa).
    # Ligas sem xG na API retornam colunas all-NaN → blend não ativa → gols reais usados.
    xg_ativo = False
    if peso_xg > 0.0 and "xg_home" in df.columns and "xg_away" in df.columns:
        mask_xg = df["xg_home"].notna() & df["xg_away"].notna()
        n_xg = int(mask_xg.sum())
        if n_xg >= 20:
            df.loc[mask_xg, "home_goals"] = (
                (1.0 - peso_xg) * df.loc[mask_xg, "home_goals"] +
                peso_xg         * df.loc[mask_xg, "xg_home"]
            )
            df.loc[mask_xg, "away_goals"] = (
                (1.0 - peso_xg) * df.loc[mask_xg, "away_goals"] +
                peso_xg         * df.loc[mask_xg, "xg_away"]
            )
            xg_ativo = True
            print(f"[motor.py] Liga {league_id}: xG blend ativo (peso={peso_xg}) "
                  f"— {n_xg}/{len(df)} jogos com cobertura de xG.")
        else:
            print(f"[motor.py] Liga {league_id}: xG blend solicitado mas cobertura "
                  f"insuficiente ({n_xg} jogos com xG). Usando gols reais.")

    # Preserva df completo para raio-x (antes de qualquer filtro de times)
    df_para_raio_x = df.copy()

    # Identifica times que aparecem na temporada ATUAL (season_year == season).
    # Isso é usado para post-filtrar o modelo — times rebaixados ficam fora.
    has_season_col = "season_year" in df.columns
    if has_season_col:
        df_atual_mask = df["season_year"] == season
        times_na_temporada_atual: set = (
            set(df.loc[df_atual_mask, "home_id"]) |
            set(df.loc[df_atual_mask, "away_id"])
        )
    else:
        times_na_temporada_atual = set()   # sem coluna → sem filtro (liga europeia)

    # ── FILTRO DE TIMES RAROS (fix do bug FA Cup / Copa do Brasil) ────────
    # Conta aparições totais (casa + fora) de cada time
    aparicoes = pd.concat([df["home_id"], df["away_id"]]).value_counts()
    times_validos = set(aparicoes[aparicoes >= min_aparicoes_time].index)

    # Se o teto max_times for atingido, mantém os N mais frequentes
    if len(times_validos) > max_times:
        times_validos = set(aparicoes.nlargest(max_times).index)

    # Remove partidas onde algum dos times foi filtrado
    n_jogos_antes = len(df)
    df = df[df["home_id"].isin(times_validos) & df["away_id"].isin(times_validos)]
    n_jogos_filtrados = n_jogos_antes - len(df)

    if len(df) < 20:
        raise ValueError(
            f"Liga {league_id}: após filtrar times com < {min_aparicoes_time} aparições, "
            f"restaram apenas {len(df)} jogos (eram {n_jogos_antes}). "
            f"Dados insuficientes para MLE confiável."
        )

    if n_jogos_filtrados > 0:
        print(f"[motor.py] Liga {league_id}: {n_jogos_filtrados} jogos de fases iniciais "
              f"removidos. {len(df)} jogos / {len(times_validos)} times no MLE.")

    # Pesos temporais
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        dias_atras = (data_referencia - df["date"]).dt.days.clip(lower=0)
        df["peso"] = np.exp(-xi * dias_atras)
    else:
        df["peso"] = 1.0

    # Indexação dos times
    times_unicos = sorted(set(df["home_id"]) | set(df["away_id"]))
    idx_map = {t: i for i, t in enumerate(times_unicos)}
    n_times = len(times_unicos)

    if n_times < 4:
        raise ValueError(f"Liga {league_id} tem apenas {n_times} times após filtro — insuficiente para MLE")

    home_idx = df["home_id"].map(idx_map).to_numpy()
    away_idx = df["away_id"].map(idx_map).to_numpy()
    home_goals = df["home_goals"].to_numpy()
    away_goals = df["away_goals"].to_numpy()
    pesos = df["peso"].to_numpy()

    # Chute inicial: alpha=1, beta=1, gamma=1.3 (vantagem casa típica), rho=-0.05
    x0 = np.concatenate([
        np.ones(n_times),         # alphas
        np.ones(n_times),         # betas
        np.array([1.3]),          # gamma
        np.array([-0.05]),        # rho
    ])

    # Constraint: média de alphas = 1 (identificabilidade)
    def constraint_alpha_mean(p):
        return np.mean(p[:n_times]) - 1.0

    constraints = [{"type": "eq", "fun": constraint_alpha_mean}]

    # Bounds
    bounds = (
        [(0.1, 3.0)] * n_times +     # alphas
        [(0.1, 3.0)] * n_times +     # betas
        [(0.8, 2.0)] +                # gamma
        [(-0.35, 0.35)]               # rho
    )

    # maxiter proporcional ao tamanho do problema: mais times = menos iterações por parâmetro
    # Para ligas normais (20 times): 300 iters. Para copas (60 times): ~180 iters.
    maxiter = max(150, 300 - max(0, (n_times - 20)) * 2)

    result = minimize(
        _neg_log_likelihood,
        x0,
        args=(home_idx, away_idx, home_goals, away_goals, pesos, n_times),
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"maxiter": maxiter, "ftol": 1e-6},
    )

    if not result.success:
        print(f"[motor.py] AVISO: MLE não convergiu para liga {league_id}: {result.message}")

    alphas = result.x[:n_times]
    betas = result.x[n_times:2 * n_times]
    gamma = float(result.x[2 * n_times])
    rho   = float(result.x[2 * n_times + 1])

    # ── SHRINKAGE BAYESIANO NO RHO ─────────────────────────────────────────
    # Com poucos jogos o SLSQP tende a empurrar rho para os extremos do bound
    # (-0.35 ou +0.35) porque o gradiente do tau é estimado de poucos placares
    # baixos. Shrinkage global (sem regras por liga) puxa rho em direção ao
    # prior neutro (0.0) com intensidade inversamente proporcional ao n:
    #
    #   peso = n / (n + forca_rho)
    #   rho_final = peso * rho_mle   [prior=0 → termo some]
    #
    # forca_rho=200: 380 jogos → peso=0.655 | 760→0.792 | 1500→0.882
    # Efeito: Bundesliga Cal 1 (rho=-0.21) → rho_final=-0.138 (estabiliza).
    #         BRA Cal 8 (n=3040, rho=-0.05) → rho_final=-0.047 (quase intocado).
    _n_treino = len(df)
    _forca_rho = 100.0  # v7: 200→100; preserva mais sinal em ligas estáveis (ITA, BRA)
    rho *= _n_treino / (_n_treino + _forca_rho)

    # Conta jogos por time (importante para shrinkage)
    jogos_por_time = pd.concat([df["home_id"], df["away_id"]]).value_counts().to_dict()

    times_dict = {}
    for t, i in idx_map.items():
        times_dict[int(t)] = {
            "alpha": float(alphas[i]),
            "beta": float(betas[i]),
            "n_jogos": int(jogos_por_time.get(t, 0)),
        }

    # ── POST-FILTRO: expulsa times que NÃO aparecem na temporada atual ─────
    # Ex: Série A 2026 — Sport Recife (38 jogos em 2025, 0 em 2026) → removido
    #                    Remo (0 em 2025, ≥14 em 2026)               → mantido
    # Para ligas europeias (sem season_year) this block is a no-op.
    if has_season_col and times_na_temporada_atual:
        times_dict = {
            tid: tv for tid, tv in times_dict.items()
            if tid in times_na_temporada_atual
        }

    media_liga = float((df["home_goals"].sum() + df["away_goals"].sum()) / (2 * len(df)))

    # ── RAIO-X: estatísticas por time (ALL times, incluindo rebaixados) ─────
    # Permite ao usuário ver exatamente o que entrou e o que foi filtrado.
    raio_x: dict[int, dict] = {}
    todos_times_raio_x = set(df_para_raio_x["home_id"]) | set(df_para_raio_x["away_id"])

    for t in sorted(todos_times_raio_x):
        t = int(t)
        mask_t = (df_para_raio_x["home_id"] == t) | (df_para_raio_x["away_id"] == t)
        jogos_t = df_para_raio_x[mask_t]

        if has_season_col and "season_year" in jogos_t.columns:
            n_atual = int((jogos_t["season_year"] == season).sum())
            n_hist  = int((jogos_t["season_year"] != season).sum())
        else:
            n_atual = len(jogos_t)
            n_hist  = 0

        try:
            ultimo = str(jogos_t["date"].max())[:10]
        except Exception:
            ultimo = "?"

        raio_x[t] = {
            "n_atual":            n_atual,
            "n_historico":        n_hist,
            "n_total":            len(jogos_t),
            "ultimo_jogo":        ultimo,
            "na_temporada_atual": t in times_na_temporada_atual if has_season_col else True,
            "no_modelo":          t in times_dict,
        }

    # Nomes: só os times que passaram no filtro final
    nomes_filtrados: dict = {}
    if nomes_times:
        for tid in times_dict:
            if tid in nomes_times:
                nomes_filtrados[tid] = nomes_times[tid]

    return ParametrosLiga(
        league_id=league_id,
        season=season,
        times=times_dict,
        home_advantage=gamma,
        rho=rho,
        xi=xi,
        media_liga_gols=media_liga,
        n_jogos_calibracao=len(df),
        log_likelihood=-float(result.fun),
        nomes_times=nomes_filtrados,
        seasons_incluidas=seasons_incluidas or [season],
        raio_x_times=raio_x,
        xg_ativo=xg_ativo,
    )


# =========================================================================
# 4. SHRINKAGE PARA MÉDIA DA LIGA
# =========================================================================

def aplicar_shrinkage(
    alpha: float,
    beta: float,
    n_jogos: int,
    n_minimo: int = 10,
    forca_shrinkage: float = 12.0,
) -> tuple[float, float]:
    """
    Shrinkage Bayesiano para média da liga.

    Times com poucos jogos têm α/β puxados para 1.0 (média).
    Times com muitos jogos mantêm os valores estimados.

    Fórmula: α_ajustado = (n * α + forca_shrinkage * 1.0) / (n + forca_shrinkage)

    forca_shrinkage=12.0 (dobro do original 6.0):
      - 20 jogos: peso MLE cai de 76.9% para 62.5%
      - 34 jogos: cai de 85.0% para 73.9%
    Reduz sobreconfiança do MLE em amostras de 1-2 temporadas.
    """
    if n_jogos < n_minimo:
        # Shrinkage agressivo para times com pouco histórico
        peso_amostra = n_jogos / (n_jogos + forca_shrinkage * 2)
    else:
        peso_amostra = n_jogos / (n_jogos + forca_shrinkage)

    peso_prior = 1.0 - peso_amostra
    alpha_ajustado = peso_amostra * alpha + peso_prior * 1.0
    beta_ajustado = peso_amostra * beta + peso_prior * 1.0
    return alpha_ajustado, beta_ajustado


# =========================================================================
# 5. ATUALIZAÇÃO INCREMENTAL (após calibração principal)
# =========================================================================

def atualizar_incremental(
    params: ParametrosLiga,
    home_id: int,
    away_id: int,
    home_goals: int,
    away_goals: int,
    taxa_aprendizado: float = 0.05,
) -> ParametrosLiga:
    """
    Online gradient update para um único jogo novo.

    Atualiza α/β dos dois times envolvidos via gradiente do log-likelihood.
    Útil entre calibrações semanais completas — barato e mantém parâmetros vivos.

    NÃO altera gamma, rho ou xi (esses precisam de re-MLE completo).
    """
    if home_id not in params.times or away_id not in params.times:
        return params   # time novo precisa de calibração completa

    th = params.times[home_id]
    ta = params.times[away_id]

    lam = th["alpha"] * ta["beta"] * params.home_advantage
    mu = ta["alpha"] * th["beta"]

    # Gradientes de log P(x,y) com respeito a α/β (ignora τ — efeito 2ª ordem)
    # d log Pois(x; lambda) / d lambda = x/lambda - 1
    d_log_p_home = home_goals / lam - 1.0
    d_log_p_away = away_goals / mu - 1.0

    # d lambda / d alpha_home = beta_away * gamma
    # d mu     / d alpha_away = beta_home
    # d lambda / d beta_away  = alpha_home * gamma
    # d mu     / d beta_home  = alpha_away

    grad_alpha_home = d_log_p_home * ta["beta"] * params.home_advantage
    grad_alpha_away = d_log_p_away * th["beta"]
    grad_beta_home = d_log_p_away * ta["alpha"]
    grad_beta_away = d_log_p_home * th["alpha"] * params.home_advantage

    novo_alpha_home = np.clip(th["alpha"] + taxa_aprendizado * grad_alpha_home, 0.1, 3.0)
    novo_alpha_away = np.clip(ta["alpha"] + taxa_aprendizado * grad_alpha_away, 0.1, 3.0)
    novo_beta_home = np.clip(th["beta"] + taxa_aprendizado * grad_beta_home, 0.1, 3.0)
    novo_beta_away = np.clip(ta["beta"] + taxa_aprendizado * grad_beta_away, 0.1, 3.0)

    params.times[home_id] = {
        "alpha": float(novo_alpha_home),
        "beta": float(novo_beta_home),
        "n_jogos": th["n_jogos"] + 1,
    }
    params.times[away_id] = {
        "alpha": float(novo_alpha_away),
        "beta": float(novo_beta_away),
        "n_jogos": ta["n_jogos"] + 1,
    }
    return params


# =========================================================================
# 6. MATRIZ DE PLACARES E PREVISÃO
# =========================================================================

def matriz_placar(
    lam: float,
    mu: float,
    rho: float,
    max_gols: int = 10,
) -> np.ndarray:
    """
    Retorna matriz [max_gols+1, max_gols+1] de P(home=x, away=y) sob D-C.
    Normalizada para somar 1 (truncamento corrigido).
    """
    n = max_gols + 1
    m = np.zeros((n, n))
    for x in range(n):
        for y in range(n):
            m[x, y] = prob_placar(x, y, lam, mu, rho)
    return m / m.sum()


def prever_jogo(
    params: ParametrosLiga,
    home_id: int,
    away_id: int,
    aplicar_shrink: bool = True,
    cobertura_minima: int = 10,
) -> dict:
    """
    Calcula todas as probabilidades de mercados para um jogo.

    Retorna dict com:
        - lambda, mu (gols esperados)
        - matriz: np.ndarray 11x11
        - mercados: dict {nome_mercado: prob_em_porcentagem}
        - flags: lista de anomalias detectadas
        - cobertura_ok: True/False (se os dois times têm >= cobertura_minima jogos)
    """
    flags = []

    if home_id not in params.times:
        return {"erro": f"Time casa (id={home_id}) não está nos parâmetros da liga", "flags": ["TIME_DESCONHECIDO"]}
    if away_id not in params.times:
        return {"erro": f"Time fora (id={away_id}) não está nos parâmetros da liga", "flags": ["TIME_DESCONHECIDO"]}

    th = params.times[home_id]
    ta = params.times[away_id]

    # Cobertura
    n_h, n_a = th["n_jogos"], ta["n_jogos"]
    cobertura_ok = n_h >= cobertura_minima and n_a >= cobertura_minima
    if not cobertura_ok:
        flags.append(f"COBERTURA_BAIXA(casa={n_h}, fora={n_a})")

    # Shrinkage
    if aplicar_shrink:
        alpha_h, beta_h = aplicar_shrinkage(th["alpha"], th["beta"], n_h)
        alpha_a, beta_a = aplicar_shrinkage(ta["alpha"], ta["beta"], n_a)
    else:
        alpha_h, beta_h = th["alpha"], th["beta"]
        alpha_a, beta_a = ta["alpha"], ta["beta"]

    lam = alpha_h * beta_a * params.home_advantage
    mu = alpha_a * beta_h

    # Sanity checks de gols esperados
    xg_total = lam + mu
    if xg_total < 0.8:
        flags.append(f"XG_TOTAL_BAIXO({xg_total:.2f})")
    if xg_total > 5.5:
        flags.append(f"XG_TOTAL_ALTO({xg_total:.2f})")

    M = matriz_placar(lam, mu, params.rho, max_gols=10)
    mercados = derivar_mercados(M, lam, mu)

    # Calibração isotônica (Dossiê v8 — Passo 4).
    # Aplica MarketCalibrator em cada mercado que possui calibrador treinado.
    # Mercados sem calibrador (AH, PE, 1X2, exóticos) ficam com prob bruta.
    # Fallback implícito: calibrar() retorna prob_bruta_pct se _iso=None ou n<30.
    calibracao_aplicada = False
    if params.calibradores:
        for mercado, cal in params.calibradores.items():
            if mercado in mercados:
                mercados[mercado] = cal.calibrar(mercados[mercado])
        calibracao_aplicada = True

    return {
        "home_id":            home_id,
        "away_id":            away_id,
        "lambda":             float(lam),
        "mu":                 float(mu),
        "xg_total":           float(xg_total),
        "rho":                params.rho,
        "matriz":             M,
        "mercados":           mercados,
        "cobertura_ok":       cobertura_ok,
        "n_jogos_casa":       n_h,
        "n_jogos_fora":       n_a,
        "flags":              flags,
        "calibracao_aplicada": calibracao_aplicada,
    }


# =========================================================================
# 7. DERIVAÇÃO DE MERCADOS A PARTIR DA MATRIZ
# =========================================================================

def derivar_mercados(M: np.ndarray, lam: float, mu: float) -> dict:
    """
    A partir da matriz placar normalizada, calcula probs (em %) de todos os
    mercados ampliados.

    Vantagem: mercados derivados são INTERNAMENTE COERENTES (somam 1 onde
    devem somar 1) — fim da incoerência V6.1 onde Over + Under não fechavam.
    """
    n = M.shape[0]

    # 1X2
    p_home = float(np.tril(M, -1).sum())
    p_draw = float(np.trace(M))
    p_away = float(np.triu(M, 1).sum())

    # Dupla Chance
    p_1x = p_home + p_draw
    p_x2 = p_draw + p_away
    p_12 = p_home + p_away

    # BTTS
    p_btts_yes = float(M[1:, 1:].sum())
    p_btts_no = 1.0 - p_btts_yes

    # Totais (Over/Under N.5)
    soma_gols = np.add.outer(np.arange(n), np.arange(n))   # matriz com gols totais
    overunder = {}
    for linha in [0.5, 1.5, 2.5, 3.5, 4.5]:
        overunder[f"OVER_{int(linha*10):02d}"] = float(M[soma_gols > linha].sum())
        overunder[f"UNDER_{int(linha*10):02d}"] = float(M[soma_gols < linha].sum())

    # Handicap Asiático (formato europeu — sem push split, simplificado)
    # AH casa -0.5: casa vence por 1+. AH casa -1: casa vence por 2+, empate vira push.
    diff = np.subtract.outer(np.arange(n), np.arange(n))   # home - away
    ah = {}
    # -0.5 (casa precisa vencer)
    ah["AH_CASA_-05"] = float(M[diff >= 1].sum())
    ah["AH_FORA_+05"] = float(M[diff <= 0].sum())
    # -1.0 (casa precisa vencer por 2+, diff==1 é push)
    p_casa_2plus = float(M[diff >= 2].sum())
    p_push_1 = float(M[diff == 1].sum())
    ah["AH_CASA_-10"] = p_casa_2plus / (1.0 - p_push_1) if p_push_1 < 0.999 else 0.0
    ah["AH_FORA_+10"] = float(M[diff <= 0].sum()) / (1.0 - p_push_1) if p_push_1 < 0.999 else 0.0
    # -1.5
    ah["AH_CASA_-15"] = float(M[diff >= 2].sum())
    ah["AH_FORA_+15"] = float(M[diff <= 1].sum())
    # +0.5 (fora vence ou empata)
    # já calculado acima como AH_FORA_+05
    # +1.0 (AH linha cheia — casa começa com +1 gol de vantagem)
    # Casa ganha AH +1.0: diff (home - away) >= 0 (casa vence ou empata)
    # Push AH +1.0:       diff == -1 (casa perde por exatamente 1)
    # Casa perde AH +1.0: diff <= -2 (casa perde por 2+)
    p_push_menos1 = float(M[diff == -1].sum())
    p_casa_ganha_ah_plus1 = float(M[diff >= 0].sum())
    ah["AH_CASA_+10"] = (
        p_casa_ganha_ah_plus1 / (1.0 - p_push_menos1)
        if p_push_menos1 < 0.999 else 0.0
    )
    # +1.5
    ah["AH_CASA_+15"] = float(M[diff <= 1].sum())

    # Placar Exato top-N (retorna top 10 placares mais prováveis)
    placares = []
    for x in range(n):
        for y in range(n):
            placares.append(((x, y), float(M[x, y])))
    placares.sort(key=lambda z: z[1], reverse=True)
    top_placares = {f"PE_{p[0][0]}-{p[0][1]}": p[1] * 100 for p in placares[:10]}

    # Monta dict final em PORCENTAGEM (consistente com convenção do app)
    mercados = {
        "HOME": p_home * 100,
        "DRAW": p_draw * 100,
        "AWAY": p_away * 100,
        "1X": p_1x * 100,
        "X2": p_x2 * 100,
        "12": p_12 * 100,
        "BTTS_YES": p_btts_yes * 100,
        "BTTS_NO": p_btts_no * 100,
    }
    for k, v in overunder.items():
        mercados[k] = v * 100
    for k, v in ah.items():
        mercados[k] = v * 100
    mercados.update(top_placares)

    return mercados


# =========================================================================
# 8. THRESHOLDS POR MERCADO + DETECTOR DE ANOMALIAS (Dossiê v8 — Seção 5.2)
# =========================================================================

# Mercados de produção validados. Match Odds (HOME/DRAW/AWAY) e Dupla
# Chance foram descontinuados — o edge em gols com xG é autossuficiente
# (Dossiê v8, Parte 9). OVER/UNDER 0.5 e 4.5 também excluídos: odds
# muito baixas ou variância excessiva para pouco edge incremental.
MERCADOS_PRODUCAO: frozenset = frozenset({
    "OVER_15", "OVER_25", "OVER_35",
    "UNDER_15", "UNDER_25", "UNDER_35",
    "BTTS_YES", "BTTS_NO",
})

EV_MIN_POR_MERCADO: dict = {
    "OVER_15":  5.0,
    "OVER_25":  5.0,
    "OVER_35":  5.0,
    "UNDER_15": 5.0,
    "UNDER_25": 10.0,   # mercado estrela — exige convicção maior (Seção 5.2)
    "UNDER_35": 8.0,
    "BTTS_YES": 5.0,
    "BTTS_NO":  8.0,
}

PROB_MIN_POR_MERCADO: dict = {
    "OVER_15":  45.0,
    "OVER_25":  45.0,
    "OVER_35":  40.0,
    "UNDER_15": 45.0,
    "UNDER_25": 45.0,
    "UNDER_35": 40.0,
    "BTTS_YES": 40.0,
    "BTTS_NO":  40.0,
}


def comparar_com_mercado(
    prob_modelo_pct: float,
    odd_mercado: float,
    margem_bookmaker: float = 1.05,
    limite_divergencia_pp: float = 20.0,
) -> dict:
    """
    Compara probabilidade do modelo com probabilidade implícita do mercado.

    Detecta anomalias do tipo "modelo diz 80%, mercado diz 51%" (bug clássico
    do V6.1).

    Retorna:
        - prob_mercado_pct: prob implícita ajustada pela margem
        - divergencia_pp: pontos percentuais de diferença
        - ev_pct: valor esperado em %
        - kelly_fracao: fração de Kelly (0.0 a 1.0)
        - anomalia: True se divergência > limite_divergencia_pp
        - flag_aprovado: True se não houver anomalia E EV > 3%
    """
    if odd_mercado <= 1.0:
        return {"erro": "Odd inválida", "anomalia": True, "flag_aprovado": False}

    prob_implicita = 100.0 / odd_mercado
    prob_mercado_pct = prob_implicita / margem_bookmaker   # remove vig

    divergencia = prob_modelo_pct - prob_mercado_pct
    ev_pct = (prob_modelo_pct / 100.0 * odd_mercado - 1.0) * 100.0

    # Fifth-Kelly: 20% do Kelly ótimo (Dossiê v8 — Seção 5.4).
    # Qualquer erro de estimativa de probabilidade leva a apostas excessivas
    # com frações maiores. 20% é o balanço validado no backtest de 6816 apostas.
    p = prob_modelo_pct / 100.0
    b = odd_mercado - 1.0
    if b > 0:
        kelly_puro = (b * p - (1 - p)) / b
        kelly_fracao = max(0.0, kelly_puro * 0.20)  # Fifth-Kelly
    else:
        kelly_fracao = 0.0

    anomalia = abs(divergencia) > limite_divergencia_pp
    # Prob mínima absoluta de 15%: rejeita apostas com probabilidade modelo muito baixa.
    # Evita UNDER_05 @ 10.00 (prob 11.8%) e similares que passavam pelo EV positivo
    # mas têm variância inaceitável para bankroll pequena.
    flag_aprovado = (
        not anomalia
        and ev_pct > 3.0
        and odd_mercado >= 1.50
        and prob_modelo_pct >= 15.0
    )

    return {
        "prob_modelo_pct": prob_modelo_pct,
        "prob_mercado_pct": prob_mercado_pct,
        "divergencia_pp": divergencia,
        "ev_pct": ev_pct,
        "kelly_fracao": kelly_fracao,
        "anomalia": anomalia,
        "flag_aprovado": flag_aprovado,
    }


def filtrar_gatilho(
    mercado: str,
    ev_pct: float,
    prob_modelo: float,
    divergencia_pp: float,
    odd: float,
) -> bool:
    """
    Filtro de gatilho por mercado (Dossiê v8 — Seções 5.1 e 5.2).

    Retorna True somente se TODAS as condições forem satisfeitas:
        1. Mercado em MERCADOS_PRODUCAO (gols + BTTS — sem Match Odds)
        2. EV >= EV_MIN_POR_MERCADO[mercado]
        3. Prob modelo >= PROB_MIN_POR_MERCADO[mercado]
        4. Divergência modelo > mercado (delta positivo)
        5. |Divergência| <= 20pp (proteção contra anomalias)
        6. Odd >= 1.50
    """
    if mercado not in MERCADOS_PRODUCAO:
        return False
    return (
        ev_pct        >= EV_MIN_POR_MERCADO[mercado]
        and prob_modelo   >= PROB_MIN_POR_MERCADO[mercado]
        and divergencia_pp > 0.0
        and abs(divergencia_pp) <= 20.0
        and odd           >= 1.50
    )


# =========================================================================
# 9. SERIALIZAÇÃO PARA CACHE (JSONBin / arquivo local)
# =========================================================================

def salvar_params_json(params: ParametrosLiga, caminho: str) -> None:
    """Salva ParametrosLiga em arquivo JSON."""
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(params.to_dict(), f, indent=2)


def carregar_params_json(caminho: str) -> ParametrosLiga:
    """Carrega ParametrosLiga de arquivo JSON."""
    with open(caminho, "r", encoding="utf-8") as f:
        return ParametrosLiga.from_dict(json.load(f))


# =========================================================================
# 10. SELF-TEST (rodar com: python motor.py)
# =========================================================================

def _gerar_liga_sintetica(n_times: int = 12, n_rodadas: int = 24, seed: int = 42) -> tuple[pd.DataFrame, dict]:
    """Gera uma liga sintética com α/β/γ CONHECIDOS para validar o MLE."""
    rng = np.random.default_rng(seed)
    alphas_reais = rng.uniform(0.6, 1.5, n_times)
    alphas_reais = alphas_reais / alphas_reais.mean()   # normaliza pra média 1
    betas_reais = rng.uniform(0.7, 1.4, n_times)
    gamma_real = 1.35
    rho_real = -0.08

    jogos = []
    data_base = dt.datetime.now() - dt.timedelta(days=n_rodadas * 7)
    for rodada in range(n_rodadas):
        # cada time joga 1x por rodada (turno único)
        ordem = rng.permutation(n_times)
        for k in range(0, n_times, 2):
            h, a = int(ordem[k]), int(ordem[k + 1])
            lam = alphas_reais[h] * betas_reais[a] * gamma_real
            mu = alphas_reais[a] * betas_reais[h]
            # Amostragem aproximada (ignora tau para simplicidade da geração)
            hg = rng.poisson(lam)
            ag = rng.poisson(mu)
            jogos.append({
                "home_id": h,
                "away_id": a,
                "home_goals": int(hg),
                "away_goals": int(ag),
                "date": data_base + dt.timedelta(days=rodada * 7),
            })

    return pd.DataFrame(jogos), {
        "alphas": alphas_reais,
        "betas": betas_reais,
        "gamma": gamma_real,
        "rho": rho_real,
    }


def _self_test() -> None:
    print("=" * 70)
    print("MOTOR V2 - SELF TEST")
    print("=" * 70)

    # Teste 1: tau D-C na direção correta
    print("\n[TESTE 1] Ajuste tau Dixon-Coles na direção correta")
    rho_pos = 0.10
    t00 = tau_dixon_coles(0, 0, 1.2, 1.0, rho_pos)
    t11 = tau_dixon_coles(1, 1, 1.2, 1.0, rho_pos)
    t01 = tau_dixon_coles(0, 1, 1.2, 1.0, rho_pos)
    t10 = tau_dixon_coles(1, 0, 1.2, 1.0, rho_pos)
    print(f"  tau(0,0) = {t00:.4f}  (deve ser < 1 se rho>0: {t00 < 1})")
    print(f"  tau(1,1) = {t11:.4f}  (deve ser < 1 se rho>0: {t11 < 1})")
    print(f"  tau(0,1) = {t01:.4f}  (deve ser > 1 se rho>0: {t01 > 1})")
    print(f"  tau(1,0) = {t10:.4f}  (deve ser > 1 se rho>0: {t10 > 1})")
    # Para rho NEGATIVO (caso típico no futebol), inverte: 0x0 e 1x1 ficam INFLADOS
    rho_neg = -0.10
    assert tau_dixon_coles(0, 0, 1.2, 1.0, rho_neg) > 1.0, "tau(0,0) deveria > 1 com rho negativo"
    assert tau_dixon_coles(1, 1, 1.2, 1.0, rho_neg) > 1.0, "tau(1,1) deveria > 1 com rho negativo"
    print("  OK: direção do ajuste D-C está correta (oposta ao V6.1)")

    # Teste 2: matriz de placares normaliza
    print("\n[TESTE 2] Matriz de placares soma 1")
    M = matriz_placar(1.4, 1.1, -0.08, max_gols=10)
    soma = M.sum()
    print(f"  Soma da matriz = {soma:.6f} (deve ser 1.000000)")
    assert abs(soma - 1.0) < 1e-9, "Matriz não normalizada"
    print("  OK")

    # Teste 3: mercados derivados são coerentes
    print("\n[TESTE 3] Mercados derivados internamente coerentes")
    merc = derivar_mercados(M, 1.4, 1.1)
    s_1x2 = merc["HOME"] + merc["DRAW"] + merc["AWAY"]
    s_ou25 = merc["OVER_25"] + merc["UNDER_25"]
    s_btts = merc["BTTS_YES"] + merc["BTTS_NO"]
    print(f"  HOME+DRAW+AWAY = {s_1x2:.4f}% (deve ser 100)")
    print(f"  OVER_25+UNDER_25 = {s_ou25:.4f}% (deve ser 100)")
    print(f"  BTTS_YES+BTTS_NO = {s_btts:.4f}% (deve ser 100)")
    assert abs(s_1x2 - 100) < 1e-6
    assert abs(s_ou25 - 100) < 1e-6
    assert abs(s_btts - 100) < 1e-6
    print("  OK: mercados são internamente coerentes")

    # Teste 4: MLE recupera parâmetros conhecidos em liga sintética
    print("\n[TESTE 4] MLE recupera parâmetros de liga sintética")
    df, reais = _gerar_liga_sintetica(n_times=12, n_rodadas=30, seed=42)
    print(f"  Gerada liga sintética: {len(df)} jogos, {df['home_id'].nunique()} times")
    print(f"  Calibrando... (pode levar alguns segundos)")
    params = calibrar_liga(df, league_id=9999, season=2026)
    print(f"  Convergiu. Log-likelihood = {params.log_likelihood:.2f}")
    print(f"  gamma estimado = {params.home_advantage:.3f}  (real = {reais['gamma']:.3f})")
    print(f"  media_liga_gols = {params.media_liga_gols:.2f}")
    erro_gamma = abs(params.home_advantage - reais["gamma"])
    print(f"  Erro absoluto em gamma = {erro_gamma:.3f}")
    assert erro_gamma < 0.20, "MLE não recuperou gamma razoavelmente"
    print("  OK: gamma recuperado com erro < 0.20")

    # Teste 5: previsão de um jogo + comparação com mercado
    print("\n[TESTE 5] Previsão de jogo + comparação com mercado")
    h_id = list(params.times.keys())[0]
    a_id = list(params.times.keys())[1]
    prev = prever_jogo(params, h_id, a_id)
    print(f"  Jogo: time {h_id} (casa) vs time {a_id} (fora)")
    print(f"  Lambda = {prev['lambda']:.2f}, Mu = {prev['mu']:.2f}, xG total = {prev['xg_total']:.2f}")
    print(f"  HOME = {prev['mercados']['HOME']:.1f}% | DRAW = {prev['mercados']['DRAW']:.1f}% | AWAY = {prev['mercados']['AWAY']:.1f}%")
    print(f"  OVER 2.5 = {prev['mercados']['OVER_25']:.1f}% | BTTS = {prev['mercados']['BTTS_YES']:.1f}%")
    print(f"  Top placar exato: ", end="")
    placares_exatos = {k: v for k, v in prev["mercados"].items() if k.startswith("PE_")}
    top_pe = max(placares_exatos.items(), key=lambda x: x[1])
    print(f"{top_pe[0]} = {top_pe[1]:.1f}%")
    print(f"  Flags: {prev['flags']}")

    # Teste 6: detector de anomalia
    print("\n[TESTE 6] Detector de anomalia (caso Mirassol x Chape do relatório)")
    # Reproduzir cenário: modelo diz 80.7%, mercado oferece 1.95 (51.3% implícita)
    resultado = comparar_com_mercado(prob_modelo_pct=80.7, odd_mercado=1.95)
    print(f"  Modelo: 80.7% | Mercado: {resultado['prob_mercado_pct']:.1f}%")
    print(f"  Divergência: {resultado['divergencia_pp']:.1f}pp")
    print(f"  EV: +{resultado['ev_pct']:.1f}%")
    print(f"  Anomalia detectada: {resultado['anomalia']}")
    print(f"  Aprovado: {resultado['flag_aprovado']}")
    assert resultado["anomalia"] is True, "Falhou em detectar anomalia óbvia"
    assert resultado["flag_aprovado"] is False, "Aprovou pick suicida"
    print("  OK: detector REJEITA a pick suicida (V6.1 aprovaria com Score 100/100)")

    # Teste 7: caso coerente passa
    print("\n[TESTE 7] Caso coerente (Burnley x Aston Villa do relatório)")
    resultado2 = comparar_com_mercado(prob_modelo_pct=60.6, odd_mercado=1.91)
    print(f"  Modelo: 60.6% | Mercado: {resultado2['prob_mercado_pct']:.1f}%")
    print(f"  Divergência: {resultado2['divergencia_pp']:.1f}pp")
    print(f"  EV: +{resultado2['ev_pct']:.1f}%")
    print(f"  Anomalia: {resultado2['anomalia']} | Aprovado: {resultado2['flag_aprovado']}")
    assert resultado2["anomalia"] is False, "Marcou como anomalia caso normal"
    assert resultado2["flag_aprovado"] is True, "Rejeitou pick legítima"
    print("  OK: detector aprova pick legítima")

    # Teste 8: atualização incremental
    print("\n[TESTE 8] Atualização incremental")
    alpha_antes = params.times[h_id]["alpha"]
    params_atualizado = atualizar_incremental(params, h_id, a_id, home_goals=3, away_goals=0)
    alpha_depois = params_atualizado.times[h_id]["alpha"]
    print(f"  alpha casa antes: {alpha_antes:.4f}, depois (3x0): {alpha_depois:.4f}")
    assert alpha_depois > alpha_antes, "Vitória em casa deveria aumentar alpha"
    print("  OK: gradiente atualiza na direção correta")

    # Teste 9: MarketCalibrator — treino, calibração e serialização
    print("\n[TESTE 9] MarketCalibrator")
    rng_cal = np.random.default_rng(7)
    # Simula modelo com viés sistemático: prob_bruta ~ N(0.55, 0.10)
    # mas a frequência real é prob_bruta - 0.06 (modelo sobreestima 6pp)
    n_amostras = 200
    probs_brutas = np.clip(rng_cal.normal(0.55, 0.10, n_amostras), 0.05, 0.95).tolist()
    # Resultado real: Bernoulli com prob = prob_bruta - 0.06 (viés)
    resultados = [
        int(rng_cal.random() < (p - 0.06)) for p in probs_brutas
    ]

    cal = MarketCalibrator(mercado="UNDER_25")
    cal.treinar(probs_brutas, resultados)
    print(f"  Calibrador treinado: n={cal.n_amostras}, sklearn={_SKLEARN_OK}")

    if _SKLEARN_OK:
        prob_teste_pct = 55.0
        prob_calibrada = cal.calibrar(prob_teste_pct)
        print(f"  Prob bruta: {prob_teste_pct:.1f}% -> calibrada: {prob_calibrada:.1f}%")
        # Com viés de -6pp, o calibrador deve empurrar para baixo
        assert prob_calibrada < prob_teste_pct, (
            f"Calibrador deveria reduzir prob com viés negativo "
            f"(bruta={prob_teste_pct}, calibrada={prob_calibrada})"
        )
        print("  OK: calibrador corrigiu viés na direção esperada")

        # Fallback: n_amostras < 30 deve devolver prob bruta
        cal_vazio = MarketCalibrator(mercado="OVER_25")
        assert cal_vazio.calibrar(60.0) == 60.0, "Fallback falhou"
        print("  OK: fallback (sem treino) retorna prob bruta sem alteração")

        # Serialização round-trip
        d = cal.to_dict()
        cal2 = MarketCalibrator.from_dict(d)
        diff = abs(cal2.calibrar(55.0) - cal.calibrar(55.0))
        assert diff < 0.001, f"Round-trip com erro {diff:.4f}"
        print(f"  OK: serialização round-trip — erro={diff:.6f}")

        # Integração com prever_jogo
        params_cal = calibrar_liga(df, league_id=9999, season=2026)
        params_cal.calibradores["OVER_25"] = cal
        prev_cal = prever_jogo(params_cal, h_id, a_id)
        assert prev_cal["calibracao_aplicada"] is True
        print(f"  OK: prever_jogo aplicou calibrador (calibracao_aplicada=True)")
    else:
        print("  AVISO: scikit-learn não instalado — testes de calibração pulados")

    print("\n" + "=" * 70)
    print("TODOS OS TESTES PASSARAM")
    print("=" * 70)


if __name__ == "__main__":
    _self_test()
