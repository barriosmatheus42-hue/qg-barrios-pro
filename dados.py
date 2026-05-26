"""
QG Barrios PRO - Camada de Dados V2
====================================

Responsabilidades:
1. Wrapper limpo da API-Sports v3 (com trava de créditos)
2. Persistência no JSONBin (picks, banca, parâmetros de ligas)
3. Cache local + cache semanal de parâmetros calibrados das ligas
4. Calibração híbrida: re-MLE semanal + atualização incremental por jogo novo

Princípios:
- Zero lógica matemática aqui (delegada para motor.py)
- Zero código Streamlit (apresentação fica em app.py)
- Toda chamada de API passa pela trava de saldo
- Erros explícitos (não engole exceções com try/except: pass)
"""

from __future__ import annotations

import os
import json
import time
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests
import pandas as pd

from motor import (
    ParametrosLiga,
    calibrar_liga,
    calibrar_copa_mundo,
)

# =========================================================================
# 1. CONFIGURAÇÃO
# =========================================================================

BASE_URL = "https://v3.football.api-sports.io"
TIMEOUT_API = 12
INTERVALO_RECALIBRACAO_DIAS = 7
SALDO_MINIMO_EMERGENCIA = 50      # abaixo disso, NENHUMA chamada nova
SALDO_MIN_PARA_CALIBRACAO = 200   # calibração custa ~10-30 créditos (paginação)
CUSTO_ESTIMADO_HISTORICO_LIGA = 30
TIMEOUT_CALIBRACAO_SEGUNDOS = 90   # MLE com muitos times pode travar; mata após 90s
CUSTO_ESTIMADO_ODDS_JOGO    = 1
CUSTO_ESTIMADO_FIXTURES_DIA = 1
CUSTO_ESTIMADO_XG_FIXTURE   = 1    # 1 crédito por chamada GET /fixtures/statistics
CUSTO_ESTIMADO_XG_LIGA      = 400  # estimativa conservadora: ~380 partidas/temporada
PESO_XG_PRODUCAO            = 0.5  # blend validado no backtest (Dossiê v8 — Seção 3.1)

# Bookmakers preferidos (ordem de prioridade)
BOOKMAKERS_PRIORIDADE = [8, 4, 1, 6, 2]   # Bet365, Pinnacle, 10Bet, Bwin, Marathon

# Ligas suportadas (calibração cobre todas essas)
LIGAS_SUPORTADAS = {
    # ── Top 5 Europa ──────────────────────────────────────────────────
    39:  "Premier League",
    140: "La Liga",
    135: "Serie A",
    78:  "Bundesliga",
    61:  "Ligue 1",
    # ── Segundas Divisões Europa ──────────────────────────────────────
    40:  "Championship (Inglaterra)",
    41:  "League One (Inglaterra)",
    42:  "League Two (Inglaterra)",
    79:  "2. Bundesliga",
    136: "Serie B (Itália)",
    141: "Segunda División (Espanha)",
    62:  "Ligue 2 (França)",
    # ── Copas Europeias ───────────────────────────────────────────────
    2:   "Champions League",
    3:   "Europa League",
    848: "Conference League",
    556: "Copa del Rey",
    137: "Coppa Italia",
    529: "DFB Pokal",
    66:  "Coupe de France",
    45:  "FA Cup",
    48:  "League Cup (EFL)",
    # ── Outras Europeias (inverno) ────────────────────────────────────
    88:  "Eredivisie",
    89:  "Eerste Divisie (Holanda)",
    94:  "Primeira Liga",
    95:  "Segunda Liga (Portugal)",
    203: "Süper Lig",
    179: "Scottish Premiership",
    180: "Scottish Championship",
    144: "Belgian Pro League",
    106: "Ekstraklasa (Polônia)",
    197: "Super League (Grécia)",
    218: "Bundesliga (Áustria)",
    207: "Super League (Suíça)",
    210: "HNL (Croácia)",
    271: "Ligat ha'Al (Israel)",
    235: "Russian Premier League",
    283: "Liga I (Romênia)",
    286: "Super Liga (Sérvia)",
    345: "Czech Liga",
    # ── Europeias de verão (ano-calendário) ───────────────────────────
    103: "Eliteserien (Noruega)",
    113: "Allsvenskan (Suécia)",
    119: "Superliga (Dinamarca)",
    244: "Veikkausliiga (Finlândia)",
    164: "Úrvalsdeild (Islândia)",
    # ── Brasil ────────────────────────────────────────────────────────
    71:  "Brasileirão Série A",
    72:  "Brasileirão Série B",
    75:  "Brasileirão Série C",
    73:  "Copa do Brasil",
    # ── Américas ──────────────────────────────────────────────────────
    13:  "Copa Libertadores",
    11:  "Copa Sudamericana",
    128: "Liga Argentina",
    253: "MLS",
    262: "Liga MX",
    239: "Primera A (Colômbia)",
    265: "Primera División (Chile)",
    268: "Primera División (Uruguai)",
    240: "Liga Pro (Equador)",
    281: "Liga 1 (Peru)",
    # ── Oceania / Oriente Médio / Ásia ───────────────────────────────
    188: "A-League (Austrália)",
    307: "Saudi Pro League",
    435: "UAE Pro League",
    98:  "J1 League (Japão)",
    292: "K League 1 (Coreia do Sul)",
    169: "Super League (China)",
    # ── Copa do Mundo / Eliminatórias ─────────────────────────────────
    1:   "Copa do Mundo 2026",
    9:   "Copa América",
    29:  "Eliminatórias CAF (África)",
    30:  "Eliminatórias AFC (Ásia)",
    31:  "Eliminatórias CONCACAF",
    32:  "Eliminatórias UEFA",
    34:  "Eliminatórias CONMEBOL",
    6:   "UEFA Nations League",
}

# Ligas que usam ano-calendário como temporada (Brasil, Américas, Japão).
# Para estas, o motor combina season_atual + season_anterior automaticamente,
# garantindo dados de 2026 (recentes) + 2025 (contexto) com decay temporal.
LIGAS_TEMPORADA_ANO_ATUAL = {
    71,   # Brasileirão Série A
    72,   # Brasileirão Série B
    75,   # Brasileirão Série C
    73,   # Copa do Brasil
    13,   # Copa Libertadores
    11,   # Copa Sudamericana
    128,  # Liga Argentina
    253,  # MLS
    262,  # Liga MX
    239,  # Primera A (Colômbia)
    265,  # Primera División (Chile)
    268,  # Primera División (Uruguai)
    240,  # Liga Pro (Equador)
    281,  # Liga 1 (Peru)
    98,   # J1 League (Japão)
    292,  # K League 1 (Coreia do Sul)
    169,  # Super League (China)
    103,  # Eliteserien (Noruega)  — verão europeu, inicia em abril
    113,  # Allsvenskan (Suécia)   — verão europeu, inicia em abril
    119,  # Superliga (Dinamarca)  — verão europeu, inicia em março
    244,  # Veikkausliiga (Finlândia) — verão europeu, inicia em abril
    164,  # Úrvalsdeild (Islândia)    — verão europeu, inicia em maio
}

# Competições de seleções: usam calibrar_copa_mundo() com decay agressivo e
# prior Bayesiano de ranking FIFA. Tratamento totalmente separado das ligas domésticas.
LIGAS_COPA_MUNDO: frozenset = frozenset({
    1,   # Copa do Mundo
    9,   # Copa América
    6,   # UEFA Nations League
    29,  # Eliminatórias CAF
    30,  # Eliminatórias AFC
    31,  # Eliminatórias CONCACAF
    32,  # Eliminatórias UEFA
    34,  # Eliminatórias CONMEBOL
})

# Mínimo de jogos finalizados para calibração (override do padrão de 20).
# Copas nacionais têm muitos times em fases iniciais — filtro de min_aparicoes
# derruba jogos, reduzindo o total. Seleções jogam poucos jogos por natureza.
LIGAS_MIN_JOGOS: dict[int, int] = {
    45:  15,   # FA Cup
    48:  15,   # League Cup
    66:  15,   # Coupe de France
    73:  15,   # Copa do Brasil
    137: 15,   # Coppa Italia
    529: 15,   # DFB Pokal
    556: 15,   # Copa del Rey
    1:   10,   # Copa do Mundo
    9:   10,   # Copa América
    6:   12,   # UEFA Nations League
    29:  10,   # Eliminatórias CAF
    30:  10,   # Eliminatórias AFC
    31:  10,   # Eliminatórias CONCACAF
    32:  15,   # Eliminatórias UEFA
    34:  15,   # Eliminatórias CONMEBOL
}

# min_aparicoes_time por liga (override do padrão de 3).
# Copas e competições de seleções aceitam times/seleções com menos aparições.
LIGAS_MIN_APARICOES: dict[int, int] = {
    45: 2, 48: 2, 66: 2, 73: 2, 137: 2, 529: 2, 556: 2,  # copas nacionais
    1: 2, 6: 2, 9: 2, 29: 2, 30: 2, 31: 2, 32: 2, 34: 2,  # seleções
}

ARQUIVO_BANCO_LOCAL = "banco_barrios_pro.json"
ARQUIVO_PARAMS_LOCAL = "params_ligas.json"

log = logging.getLogger("dados")


# =========================================================================
# 2. EXCEÇÕES ESPECÍFICAS
# =========================================================================

class CreditosInsuficientesError(Exception):
    """Lançada quando o saldo está abaixo do mínimo de segurança."""
    pass


class APIError(Exception):
    """Erro genérico da API-Sports."""
    pass


# =========================================================================
# 3. CLIENT API-SPORTS
# =========================================================================

class ApiSportsClient:
    """Wrapper da API-Sports v3 com trava de créditos."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("api_key vazia")
        self.api_key = api_key
        self.headers = {"x-apisports-key": api_key}
        self._saldo_cache: Optional[int] = None
        self._saldo_cache_em: Optional[dt.datetime] = None

    # ----------------------------------------------------------------
    # Saldo / créditos
    # ----------------------------------------------------------------
    def saldo(self, cache_segundos: int = 30) -> int:
        """Retorna créditos disponíveis. Usa cache curto para evitar spam."""
        agora = dt.datetime.now()
        if (
            self._saldo_cache is not None
            and self._saldo_cache_em is not None
            and (agora - self._saldo_cache_em).total_seconds() < cache_segundos
        ):
            return self._saldo_cache

        try:
            res = requests.get(f"{BASE_URL}/status", headers=self.headers, timeout=TIMEOUT_API)
            data = res.json()
            req_info = data.get("response", {}).get("requests", {})
            limite = req_info.get("limit_day", 7500)
            usados = req_info.get("current", 0)
            saldo = limite - usados
            self._saldo_cache = saldo
            self._saldo_cache_em = agora
            return saldo
        except Exception as e:
            log.warning(f"Falha ao consultar saldo: {e}. Retornando 0 por segurança.")
            return 0

    def trava_saldo(self, custo_estimado: int, saldo_minimo: int = SALDO_MINIMO_EMERGENCIA) -> None:
        """Bloqueia chamada se não houver saldo suficiente."""
        saldo_atual = self.saldo()
        if saldo_atual < saldo_minimo:
            raise CreditosInsuficientesError(
                f"Saldo {saldo_atual} < mínimo de emergência {saldo_minimo}. Bloqueando."
            )
        if saldo_atual < custo_estimado + saldo_minimo:
            raise CreditosInsuficientesError(
                f"Saldo {saldo_atual} insuficiente para custo estimado {custo_estimado} "
                f"+ buffer {saldo_minimo}. Bloqueando."
            )

    # ----------------------------------------------------------------
    # Endpoint: agenda do dia
    # ----------------------------------------------------------------
    def buscar_agenda_dia(self, data_str: str, timezone: str = "America/Sao_Paulo") -> list[dict]:
        """Busca fixtures agendadas para uma data específica."""
        self.trava_saldo(CUSTO_ESTIMADO_FIXTURES_DIA)
        params = {"date": data_str, "timezone": timezone}
        try:
            res = requests.get(f"{BASE_URL}/fixtures", headers=self.headers, params=params, timeout=TIMEOUT_API)
            data = res.json()
            if data.get("errors"):
                raise APIError(f"API retornou erros: {data['errors']}")
            return data.get("response", [])
        except requests.RequestException as e:
            raise APIError(f"Falha de rede em buscar_agenda_dia: {e}") from e

    # ----------------------------------------------------------------
    # Endpoint: histórico completo de uma liga (para calibração)
    # ----------------------------------------------------------------
    def buscar_historico_liga(
        self,
        league_id: int,
        season: int,
        com_xg: bool = False,
    ) -> tuple[pd.DataFrame, dict]:
        """
        Busca TODOS os jogos finalizados de uma liga/temporada.

        com_xg=True: após buscar os fixtures, chama /fixtures/statistics para cada
        partida e adiciona colunas xg_home e xg_away ao DataFrame.
        Custo adicional: ~1 crédito por partida (CUSTO_ESTIMADO_XG_FIXTURE).
        Ligas sem cobertura de xG retornam colunas all-NaN — calibrar_liga()
        detecta isso e usa gols reais automaticamente.

        Retorna:
            (DataFrame com colunas: home_id, away_id, home_goals, away_goals, date,
             [xg_home, xg_away se com_xg=True],
             dict {team_id: "Nome do Time"} para exibição na auditoria)
        """
        self.trava_saldo(CUSTO_ESTIMADO_HISTORICO_LIGA, saldo_minimo=SALDO_MIN_PARA_CALIBRACAO)
        # Sem filtro status=FT na API — aceita também AET (extra time) e PEN (pênaltis).
        # Copas eliminatórias têm muitos jogos que terminam em prorrogação/pênaltis;
        # filtrar só por FT descarta a maioria dos jogos decisivos.
        # O filtro local abaixo aceita todos os resultados definitivos.
        _STATUS_FINALIZADOS = {"FT", "AET", "PEN", "AWD", "WO"}
        params = {"league": league_id, "season": season}
        try:
            res = requests.get(f"{BASE_URL}/fixtures", headers=self.headers, params=params, timeout=TIMEOUT_API)
            data = res.json()
            if data.get("errors"):
                raise APIError(f"API errors em buscar_historico_liga({league_id}, {season}): {data['errors']}")
            jogos = data.get("response", [])
        except requests.RequestException as e:
            raise APIError(f"Falha de rede em buscar_historico_liga: {e}") from e

        vazio = pd.DataFrame(columns=["home_id", "away_id", "home_goals", "away_goals", "date"])
        if not jogos:
            return vazio, {}

        registros = []
        nomes: dict[int, str] = {}
        for j in jogos:
            try:
                _st = j.get("fixture", {}).get("status", {}).get("short", "")
                if _st not in _STATUS_FINALIZADOS:
                    continue  # ignora jogos futuros, suspensos, cancelados, etc.
                gh = j["goals"]["home"]
                ga = j["goals"]["away"]
                if gh is None or ga is None:
                    continue
                h_id = j["teams"]["home"]["id"]
                a_id = j["teams"]["away"]["id"]
                nomes[h_id] = j["teams"]["home"]["name"]
                nomes[a_id] = j["teams"]["away"]["name"]
                registros.append({
                    "fixture_id": j["fixture"]["id"],
                    "home_id":    h_id,
                    "away_id":    a_id,
                    "home_goals": int(gh),
                    "away_goals": int(ga),
                    "date":       j["fixture"]["date"][:10],
                })
            except (KeyError, TypeError, ValueError) as e:
                log.debug(f"Jogo ignorado por dados inconsistentes: {e}")
                continue

        df = pd.DataFrame(registros)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])

        # ── Enriquecimento com xG via /fixtures/statistics ────────────────────
        if com_xg and not df.empty:
            custo_xg = len(df) * CUSTO_ESTIMADO_XG_FIXTURE
            try:
                self.trava_saldo(custo_xg, saldo_minimo=SALDO_MIN_PARA_CALIBRACAO)
            except CreditosInsuficientesError as e:
                log.warning(
                    f"Liga {league_id} s{season}: créditos insuficientes para xG "
                    f"({custo_xg} necessários — {e}). Calibração usará gols reais."
                )
                df["xg_home"] = None
                df["xg_away"] = None
                return df, nomes

            xg_home_list, xg_away_list = [], []
            for _, row in df.iterrows():
                xg_h, xg_a = self.buscar_xg_fixture(
                    int(row["fixture_id"]), int(row["home_id"]), int(row["away_id"])
                )
                xg_home_list.append(xg_h)
                xg_away_list.append(xg_a)

            df["xg_home"] = xg_home_list
            df["xg_away"] = xg_away_list
            n_xg = int(df["xg_home"].notna().sum())
            log.info(
                f"Liga {league_id} s{season}: xG disponível em "
                f"{n_xg}/{len(df)} jogos ({n_xg / len(df) * 100:.1f}%)."
            )

        return df, nomes

    # ----------------------------------------------------------------
    # Endpoint: xG de um fixture via /fixtures/statistics
    # ----------------------------------------------------------------
    def buscar_xg_fixture(
        self,
        fixture_id: int,
        home_team_id: int,
        away_team_id: int,
    ) -> tuple:
        """
        Retorna (xg_home, xg_away) para um fixture via /fixtures/statistics.

        Mapeia por team_id para garantir home/away corretos independente da ordem
        da resposta. Retorna (None, None) se a liga não tem cobertura de xG.
        NÃO chama trava_saldo — o caller faz o check único antes do loop.
        """
        try:
            res = requests.get(
                f"{BASE_URL}/fixtures/statistics",
                headers=self.headers,
                params={"fixture": fixture_id},
                timeout=TIMEOUT_API,
            )
            data = res.json()
        except requests.RequestException as e:
            log.debug(f"Falha de rede em buscar_xg_fixture({fixture_id}): {e}")
            return None, None

        response = data.get("response", [])
        xg_map: dict = {}
        for entry in response:
            team_id = entry.get("team", {}).get("id")
            if team_id is None:
                continue
            for stat in entry.get("statistics", []):
                if stat.get("type") == "Expected Goals":
                    val = stat.get("value")
                    if val is not None:
                        try:
                            xg_map[team_id] = float(val)
                        except (TypeError, ValueError):
                            pass
                    break

        return xg_map.get(home_team_id), xg_map.get(away_team_id)

    # ----------------------------------------------------------------
    # Endpoint: xG + estatísticas de scout via /fixtures/statistics
    # ----------------------------------------------------------------
    def buscar_stats_fixture(
        self,
        fixture_id: int,
        home_team_id: int,
        away_team_id: int,
    ) -> dict:
        """
        Busca xG + estatísticas de scout via /fixtures/statistics.
        Substitui buscar_xg_fixture — extrai mais campos sem custo adicional.
        NÃO chama trava_saldo — o caller faz o check único antes do loop.

        Retorna dict com xg_home, xg_away e stats de scout por time (h_/a_).
        Qualquer campo indisponível retorna None.
        """
        _none = {
            "xg_home": None, "xg_away": None,
            "h_shots_on": None, "h_shots_total": None,
            "h_possession": None, "h_corners": None,
            "h_yellows": None, "h_saves": None, "h_fouls": None,
            "a_shots_on": None, "a_shots_total": None,
            "a_possession": None, "a_corners": None,
            "a_yellows": None, "a_saves": None, "a_fouls": None,
        }
        try:
            res = requests.get(
                f"{BASE_URL}/fixtures/statistics",
                headers=self.headers,
                params={"fixture": fixture_id},
                timeout=TIMEOUT_API,
            )
            data = res.json()
        except requests.RequestException as e:
            log.debug(f"Falha de rede em buscar_stats_fixture({fixture_id}): {e}")
            return _none

        response = data.get("response", [])
        if not response:
            return _none

        def _parse(stats_list: list) -> dict:
            d: dict = {}
            for s in stats_list:
                t = s.get("type", "")
                v = s.get("value")
                if v is None:
                    continue
                try:
                    if t == "Expected Goals":
                        d["xg"] = float(v)
                    elif t == "Shots on Goal":
                        d["shots_on"] = int(v)
                    elif t == "Total Shots":
                        d["shots_total"] = int(v)
                    elif t == "Ball Possession":
                        d["possession"] = float(str(v).replace("%", "").strip())
                    elif t == "Corner Kicks":
                        d["corners"] = int(v)
                    elif t == "Yellow Cards":
                        d["yellows"] = int(v)
                    elif t == "Goalkeeper Saves":
                        d["saves"] = int(v)
                    elif t == "Fouls":
                        d["fouls"] = int(v)
                except (TypeError, ValueError):
                    pass
            return d

        team_stats: dict = {}
        for entry in response:
            tid = entry.get("team", {}).get("id")
            if tid is None:
                continue
            team_stats[tid] = _parse(entry.get("statistics", []))

        h = team_stats.get(home_team_id, {})
        a = team_stats.get(away_team_id, {})

        return {
            "xg_home":       h.get("xg"),
            "xg_away":       a.get("xg"),
            "h_shots_on":    h.get("shots_on"),
            "h_shots_total": h.get("shots_total"),
            "h_possession":  h.get("possession"),
            "h_corners":     h.get("corners"),
            "h_yellows":     h.get("yellows"),
            "h_saves":       h.get("saves"),
            "h_fouls":       h.get("fouls"),
            "a_shots_on":    a.get("shots_on"),
            "a_shots_total": a.get("shots_total"),
            "a_possession":  a.get("possession"),
            "a_corners":     a.get("corners"),
            "a_yellows":     a.get("yellows"),
            "a_saves":       a.get("saves"),
            "a_fouls":       a.get("fouls"),
        }

    # ----------------------------------------------------------------
    # Endpoint: odds de um jogo
    # ----------------------------------------------------------------
    def buscar_odds_jogo(self, fixture_id: int) -> dict:
        """
        Retorna dict de odds normalizado para um jogo.
        Chaves: HOME, DRAW, AWAY, 1X, X2, 12, BTTS_YES, BTTS_NO,
                OVER_05..OVER_45, UNDER_05..UNDER_45.
        Valor 0 indica odd indisponível.
        """
        self.trava_saldo(CUSTO_ESTIMADO_ODDS_JOGO)

        odds_default = {
            "HOME": 0, "DRAW": 0, "AWAY": 0,
            "1X": 0, "X2": 0, "12": 0,
            "BTTS_YES": 0, "BTTS_NO": 0,
        }
        for linha in ["05", "15", "25", "35", "45"]:
            odds_default[f"OVER_{linha}"] = 0
            odds_default[f"UNDER_{linha}"] = 0

        try:
            res = requests.get(
                f"{BASE_URL}/odds",
                headers=self.headers,
                params={"fixture": fixture_id},
                timeout=TIMEOUT_API,
            )
            data = res.json()
        except requests.RequestException as e:
            log.warning(f"Falha de rede em buscar_odds_jogo({fixture_id}): {e}")
            return odds_default

        response = data.get("response", [])
        if not response:
            return odds_default

        bookmakers = response[0].get("bookmakers", [])
        bkm = None
        for target_id in BOOKMAKERS_PRIORIDADE:
            bkm = next((b for b in bookmakers if b["id"] == target_id), None)
            if bkm:
                break
        if not bkm and bookmakers:
            bkm = bookmakers[0]   # fallback: qualquer um
        if not bkm:
            return odds_default

        odds = odds_default.copy()
        for bet in bkm.get("bets", []):
            nome = bet.get("name", "")
            valores = bet.get("values", [])

            if nome == "Match Winner":
                for v in valores:
                    val = v.get("value", "")
                    try:
                        if val == "Home":
                            odds["HOME"] = float(v["odd"])
                        elif val == "Draw":
                            odds["DRAW"] = float(v["odd"])
                        elif val == "Away":
                            odds["AWAY"] = float(v["odd"])
                    except (KeyError, ValueError):
                        continue
            elif nome == "Double Chance":
                for v in valores:
                    val = v.get("value", "")
                    try:
                        if val == "Home/Draw":
                            odds["1X"] = float(v["odd"])
                        elif val == "Draw/Away":
                            odds["X2"] = float(v["odd"])
                        elif val == "Home/Away":
                            odds["12"] = float(v["odd"])
                    except (KeyError, ValueError):
                        continue
            elif nome == "Both Teams Score":
                for v in valores:
                    val = v.get("value", "")
                    try:
                        if val == "Yes":
                            odds["BTTS_YES"] = float(v["odd"])
                        elif val == "No":
                            odds["BTTS_NO"] = float(v["odd"])
                    except (KeyError, ValueError):
                        continue
            elif nome == "Goals Over/Under":
                for v in valores:
                    val = v.get("value", "")
                    try:
                        odd_val = float(v["odd"])
                    except (KeyError, ValueError):
                        continue
                    for linha_str, linha_num in [("0.5", "05"), ("1.5", "15"), ("2.5", "25"), ("3.5", "35"), ("4.5", "45")]:
                        if val == f"Over {linha_str}":
                            odds[f"OVER_{linha_num}"] = odd_val
                        elif val == f"Under {linha_str}":
                            odds[f"UNDER_{linha_num}"] = odd_val

        return odds


# =========================================================================
# 4. CLIENT JSONBIN (banco + parâmetros de ligas)
# =========================================================================

class JSONBinClient:
    """Wrapper do JSONBin para persistência de banco e parâmetros."""

    def __init__(self, key: str, bin_id: str):
        if not key or not bin_id:
            raise ValueError("key/bin_id do JSONBin vazios")
        self.key = key
        self.bin_id = bin_id
        self.url = f"https://api.jsonbin.io/v3/b/{bin_id}"
        self.headers = {"X-Master-Key": key, "Content-Type": "application/json"}

    def ler(self, timeout: int = 10) -> dict:
        try:
            res = requests.get(f"{self.url}/latest", headers=self.headers, timeout=timeout)
            if res.status_code == 200:
                return res.json().get("record", {}) or {}
        except requests.RequestException as e:
            log.warning(f"Falha ao ler JSONBin: {e}")
        return {}

    def escrever(self, dados: dict, timeout: int = 10) -> bool:
        h = self.headers.copy()
        h["X-Bin-Versioning"] = "false"
        try:
            res = requests.put(self.url, headers=h, json=dados, timeout=timeout)
            return res.status_code == 200
        except requests.RequestException as e:
            log.warning(f"Falha ao escrever JSONBin: {e}")
            return False


# =========================================================================
# 5. MANAGER PRINCIPAL (orquestrador)
# =========================================================================

@dataclass
class BancoQG:
    """
    Estado completo do banco QG Barrios.

    SEPARAÇÃO CRÍTICA (fix do bug de ROI):
    - banca_inicial: capital original investido — NUNCA muda. É o denominador do ROI.
    - depositos:     lista de entradas/saídas manuais [{data, valor, nota}].
                     valor > 0 = depósito, valor < 0 = retirada.
    - banca_atual é calculada em runtime: banca_inicial + Σ depositos + P/L picks

    DELTA FETCH (Passo 3):
    - historico_ligas: cache local de fixtures enriquecidos com xG.
                       Salvo APENAS no arquivo local — nunca sincronizado com JSONBin.
                       Schema: {liga_id_str: {"registros": [...], "atualizado_em": str}}
    """
    picks: list = None
    banca_inicial: float = 30.0
    depositos: list = None           # [{data, valor, nota, registrado_em}]
    params_ligas: dict = None        # {league_id_str: {ParametrosLiga.to_dict()}}
    datas: dict = None               # cache de análises por data (agenda, odds, previsões)
    historico_ligas: dict = None     # cache local de treino com xG — NÃO vai pro JSONBin
    historico_ids: dict = None       # {liga_id_str: [fixture_id, ...]} — VAI pro JSONBin
                                     # Persiste quais fixtures já foram baixados para que
                                     # o delta fetch não os re-baixe após restart do Cloud.

    def __post_init__(self):
        if self.picks is None:
            self.picks = []
        if self.depositos is None:
            self.depositos = []
        if self.params_ligas is None:
            self.params_ligas = {}
        if self.datas is None:
            self.datas = {}
        if self.historico_ligas is None:
            self.historico_ligas = {}
        if self.historico_ids is None:
            self.historico_ids = {}

    def to_dict(self) -> dict:
        return {
            "picks": self.picks,
            "banca_inicial": self.banca_inicial,
            "depositos": self.depositos,
            "params_ligas": self.params_ligas,
            "datas": self.datas,
            "historico_ligas": self.historico_ligas,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BancoQG":
        return cls(
            picks=d.get("picks", []),
            banca_inicial=float(d.get("banca_inicial", 30.0)),
            depositos=d.get("depositos", []),
            params_ligas=d.get("params_ligas", {}),
            datas=d.get("datas", {}),
            historico_ligas=d.get("historico_ligas", {}),
            historico_ids=d.get("historico_ids", {}),
        )


class DadosManager:
    """Orquestrador: API-Sports + JSONBin + cache local + motor."""

    def __init__(
        self,
        api_key: str,
        jsonbin_key: str,
        jsonbin_id: str,
        diretorio_local: str = ".",
    ):
        self.api = ApiSportsClient(api_key)
        self.jsonbin = JSONBinClient(jsonbin_key, jsonbin_id)
        self.dir = Path(diretorio_local)
        self._banco: Optional[BancoQG] = None
        self.ultimo_save_jsonbin_ok: bool = True  # False quando JSONBin rejeita o payload

    # ----------------------------------------------------------------
    # Banco completo
    # ----------------------------------------------------------------
    def carregar_banco(self, força_recarregar: bool = False) -> BancoQG:
        """Carrega banco da nuvem (JSONBin) + cache local."""
        if self._banco is not None and not força_recarregar:
            return self._banco

        # 1. Local (rápido, fallback offline)
        banco_local = {}
        arquivo_local = self.dir / ARQUIVO_BANCO_LOCAL
        if arquivo_local.exists():
            try:
                with open(arquivo_local, "r", encoding="utf-8") as f:
                    banco_local = json.load(f)
            except Exception as e:
                log.warning(f"Falha ao ler banco local: {e}")

        # 2. Nuvem (autoritativa para picks/banca)
        banco_nuvem = self.jsonbin.ler()

        # 3. Mescla: nuvem manda em picks/banca/depositos; local manda em params_ligas/datas
        banco = BancoQG()
        banco.picks = banco_nuvem.get("picks", banco_local.get("picks", []))
        banco.banca_inicial = float(banco_nuvem.get("banca_inicial", banco_local.get("banca_inicial", 30.0)))
        banco.depositos = banco_nuvem.get("depositos", banco_local.get("depositos", []))
        # params_ligas: local sobrescreve nuvem por liga — calibrar_liga escreve em disco
        # ANTES de tentar o JSONBin, então local é sempre a cópia mais fresca após qualquer
        # calibração. A nuvem fornece params de sessões anteriores (Streamlit Cloud perde
        # o arquivo local a cada redeploy); o merge garante que nenhuma liga é perdida E
        # que o timestamp exibido na UI sempre reflete a última calibração real.
        _params_local_raw = banco_local.get("params_ligas", {})
        _params_nuvem_raw = banco_nuvem.get("params_ligas", {})
        # Local sobrescreve nuvem liga a liga — local tem X/Y brutos + breakpoints,
        # nuvem tem apenas breakpoints. Em ambos os casos from_dict() sabe reconstruir.
        banco.params_ligas = {**_params_nuvem_raw, **_params_local_raw}
        banco.datas = banco_local.get("datas", {})                      # cache do dia fica local
        banco.historico_ligas = banco_local.get("historico_ligas", {}) # xG cache — local apenas
        # historico_ids: union de nuvem + local. Nuvem tem IDs de sessões anteriores;
        # local pode ter IDs de fixtures baixados nesta sessão antes do próximo save.
        _ids_nuvem = banco_nuvem.get("historico_ids", {})
        _ids_local = banco_local.get("historico_ids", {})
        merged_ids: dict = {}
        for _k in set(_ids_nuvem) | set(_ids_local):
            merged_ids[_k] = list(set(_ids_nuvem.get(_k, [])) | set(_ids_local.get(_k, [])))
        banco.historico_ids = merged_ids

        self._banco = banco
        return banco

    def salvar_banco(self, banco: Optional[BancoQG] = None) -> bool:
        """Persiste banco em ambos (nuvem + local). Retorna True se JSONBin OK."""
        b = banco or self._banco
        if b is None:
            raise RuntimeError("Nenhum banco carregado para salvar")

        # Local (append-only merge — nunca sobrescreve dados históricos)
        self._merge_salvar_local(b)

        # Nuvem — sem `datas` (quota). Calibradores em forma compacta: apenas
        # breakpoints x_thresh/y_thresh (~10–50 pontos por mercado), sem os arrays
        # X/Y brutos (400+ pontos). Payload ~20x menor → JSONBin sempre aceita.
        # carregar_banco() reconstrói calibrar() via np.interp nos breakpoints.
        params_nuvem: dict = {}
        for lid_str, params_d in (b.params_ligas or {}).items():
            p = dict(params_d)
            if p.get("calibradores"):
                cals_compact: dict = {}
                for mercado, cal_d in p["calibradores"].items():
                    if not isinstance(cal_d, dict):
                        continue
                    cals_compact[mercado] = {
                        "mercado":    cal_d.get("mercado", mercado),
                        "n_amostras": cal_d.get("n_amostras", 0),
                        "x_thresh":   cal_d.get("x_thresh", []),
                        "y_thresh":   cal_d.get("y_thresh", []),
                    }
                p["calibradores"] = cals_compact
            params_nuvem[lid_str] = p

        # historico_ids: só salva ligas que já foram calibradas para limitar tamanho.
        # IDs como lista de ints → ~3KB por liga calibrada (300 fixtures × 8 chars).
        hist_ids_nuvem: dict = {}
        for lid_str, ids in (b.historico_ids or {}).items():
            hist_ids_nuvem[lid_str] = [int(i) for i in ids]

        nuvem = {
            "picks":         b.picks,
            "banca_inicial": b.banca_inicial,
            "depositos":     b.depositos,
            "params_ligas":  params_nuvem,
            "historico_ids": hist_ids_nuvem,
        }
        ok = self.jsonbin.escrever(nuvem)
        self.ultimo_save_jsonbin_ok = ok
        if not ok:
            log.error(
                "CRÍTICO — falha ao salvar params_ligas no JSONBin. "
                "Params calibrados NÃO foram persistidos na nuvem e serão "
                "perdidos no próximo restart do Streamlit Cloud."
            )
        self._banco = b
        return ok

    # ----------------------------------------------------------------
    # CALIBRAÇÃO (MLE full — sem incremental)
    # ----------------------------------------------------------------
    def _merge_salvar_local(self, banco: BancoQG) -> None:
        """
        Append-only write do arquivo local.

        Sempre lê o estado atual do disco antes de escrever — garante que nenhum
        dado histórico seja perdido por reinicializações ou atualizações de código.

        Regras de merge campo a campo:
        - picks / depositos  : lista maior vence (itens são imutáveis e nunca deletados)
        - params_ligas/datas : dict update (nova calibração sobrescreve mesma chave,
                               mas nunca apaga outras ligas)
        - historico_ligas    : append-only estrito por fixture_id — nunca remove fixtures
        - banca_inicial      : in-memory é autoritativo (veio da nuvem via carregar_banco)
        """
        arquivo = self.dir / ARQUIVO_BANCO_LOCAL
        disco: dict = {}
        if arquivo.exists():
            try:
                with open(arquivo, "r", encoding="utf-8") as f:
                    disco = json.load(f)
            except Exception as e:
                log.warning(f"Merge local: arquivo corrompido ({e}). Escrevendo do zero.")

        # Picks e depósitos: lista maior vence (itens nunca são deletados)
        picks_mem   = banco.picks   or []
        picks_disco = disco.get("picks", [])
        picks_final = picks_mem if len(picks_mem) >= len(picks_disco) else picks_disco

        dep_mem   = banco.depositos or []
        dep_disco = disco.get("depositos", [])
        dep_final = dep_mem if len(dep_mem) >= len(dep_disco) else dep_disco

        # Parâmetros e datas: dict update — nova calibração sobrescreve mesma liga,
        # mas nunca apaga ligas que estão no disco mas não em memória
        params_final = dict(disco.get("params_ligas", {}))
        params_final.update(banco.params_ligas or {})

        datas_final = dict(disco.get("datas", {}))
        datas_final.update(banco.datas or {})

        # Histórico de ligas: append-only estrito por fixture_id
        hist_final = dict(disco.get("historico_ligas", {}))
        for liga_str, liga_nova in (banco.historico_ligas or {}).items():
            if liga_str not in hist_final:
                hist_final[liga_str] = liga_nova
            else:
                ids_disco = {int(r["fixture_id"]) for r in hist_final[liga_str].get("registros", [])}
                novos = [
                    r for r in liga_nova.get("registros", [])
                    if int(r["fixture_id"]) not in ids_disco
                ]
                if novos:
                    hist_final[liga_str]["registros"].extend(novos)
                    hist_final[liga_str]["atualizado_em"] = liga_nova.get("atualizado_em", "")

        # historico_ids: union de IDs do disco e da memória por liga
        hist_ids_disco = disco.get("historico_ids", {})
        hist_ids_mem   = banco.historico_ids or {}
        hist_ids_final: dict = {}
        for _k in set(hist_ids_disco) | set(hist_ids_mem):
            _disco_set = set(int(i) for i in hist_ids_disco.get(_k, []))
            _mem_set   = set(int(i) for i in hist_ids_mem.get(_k, []))
            hist_ids_final[_k] = list(_disco_set | _mem_set)

        resultado = {
            "picks":           picks_final,
            "banca_inicial":   banco.banca_inicial,
            "depositos":       dep_final,
            "params_ligas":    params_final,
            "datas":           datas_final,
            "historico_ligas": hist_final,
            "historico_ids":   hist_ids_final,
        }
        try:
            with open(arquivo, "w", encoding="utf-8") as f:
                json.dump(resultado, f, indent=2, default=str)
        except Exception as e:
            log.warning(f"Falha ao salvar banco local (merge): {e}")

    def precisa_recalibrar(self, params_dict: dict) -> bool:
        """True se params estão velhos OU ausentes."""
        if not params_dict:
            return True
        calibrado_em = params_dict.get("calibrado_em", "")
        if not calibrado_em:
            return True
        try:
            data_calib = dt.datetime.fromisoformat(calibrado_em)
            dias = (dt.datetime.now() - data_calib).days
            return dias >= INTERVALO_RECALIBRACAO_DIAS
        except Exception:
            return True

    def _obter_historico_com_delta_xg(
        self,
        league_id: int,
        season_principal: int,
        season_anterior: Optional[int] = None,
    ) -> tuple[pd.DataFrame, dict]:
        """
        Delta Fetch: busca xG apenas para fixtures novos, usando cache local.

        Fluxo por season:
        1. GET /fixtures (barato — só gols) para obter a lista atual de IDs.
        2. Compara IDs com banco.historico_ligas[liga] → identifica novidades.
        3. GET /fixtures/statistics apenas para os IDs ausentes do cache.
        4. Mescla novos registros no cache e persiste só no arquivo local.
        5. Retorna DataFrame de todas as seasons solicitadas pronto para calibrar_liga().

        Custo típico: 1 crédito (lista) + N_novos créditos (xG) por season.
        Bootstrap (primeiro run): N_total créditos — dividir em 2 dias manualmente.
        """
        banco = self.carregar_banco()
        chave = str(league_id)
        cache_liga = banco.historico_ligas.get(chave, {"registros": [], "atualizado_em": ""})

        ids_cache: set[int] = {int(r["fixture_id"]) for r in cache_liga["registros"]}

        # Fallback: usa historico_ids do JSONBin quando cache local está vazio (após restart).
        # Evita re-download de fixtures já conhecidos de sessões anteriores.
        if not ids_cache:
            ids_jsonbin: set[int] = set(int(i) for i in banco.historico_ids.get(chave, []))
            if ids_jsonbin:
                ids_cache = ids_jsonbin
                log.info(
                    f"Liga {league_id}: cache local vazio — restaurando {len(ids_jsonbin)} "
                    "IDs do JSONBin (historico_ids). Só fixtures novos receberão xG."
                )

        nomes_global: dict[int, str] = {}
        houve_novidades = False
        seasons = [season_anterior, season_principal] if season_anterior is not None else [season_principal]

        for season in seasons:
            # Busca lista de fixtures sem xG (apenas gols reais — barato)
            try:
                df_api, nomes = self.api.buscar_historico_liga(league_id, season, com_xg=False)
            except Exception as e:
                log.warning(f"Liga {league_id} s{season}: falha ao buscar lista de fixtures: {e}")
                continue

            nomes_global.update(nomes)

            if df_api.empty:
                continue

            df_novos = df_api[~df_api["fixture_id"].isin(ids_cache)].copy()

            # Restart do Streamlit Cloud apaga historico local → ids_cache fica vazio e
            # todos os fixtures históricos aparecem como "novos", custando 17k+ créditos.
            # Solução: se cache vazio mas liga foi calibrada antes (calibrado_em no JSONBin),
            # adiciona fixtures pré-calibração sem xG (grátis) e só busca xG para os
            # fixtures realmente novos (jogos ocorridos APÓS a última calibração).
            if not ids_cache and not df_novos.empty and "date" in df_novos.columns:
                _params_liga = banco.params_ligas.get(str(league_id), {})
                _calibrado_em = _params_liga.get("calibrado_em", "")
                if _calibrado_em:
                    _cutoff = _calibrado_em[:10]  # YYYY-MM-DD
                    _df_pre  = df_novos[df_novos["date"] < _cutoff].copy()
                    df_novos = df_novos[df_novos["date"] >= _cutoff].copy()
                    if not _df_pre.empty:
                        # Fixtures pré-calibração: entram no cache sem xG/scout (já eram
                        # conhecidos na calibração anterior que gerou os params no JSONBin)
                        for _col in [
                            "xg_home", "xg_away",
                            "h_shots_on", "h_shots_total", "h_possession", "h_corners",
                            "h_yellows", "h_saves", "h_fouls",
                            "a_shots_on", "a_shots_total", "a_possession", "a_corners",
                            "a_yellows", "a_saves", "a_fouls",
                        ]:
                            _df_pre[_col] = None
                        _df_pre["season_year"] = season
                        _pre_records = _df_pre.to_dict("records")
                        cache_liga["registros"].extend(_pre_records)
                        ids_cache.update(int(r["fixture_id"]) for r in _pre_records)
                        houve_novidades = True
                        log.info(
                            f"Liga {league_id} s{season}: cache restaurado após restart — "
                            f"{len(_df_pre)} fixtures pré-{_cutoff} adicionados sem xG, "
                            f"{len(df_novos)} fixtures novos receberão xG."
                        )

            if df_novos.empty:
                log.info(f"Liga {league_id} s{season}: sem fixtures novos (cache 100%).")
                continue

            n_novos = len(df_novos)
            log.info(f"Liga {league_id} s{season}: {n_novos} fixtures novos — buscando xG + scout stats.")

            # Verifica créditos antes do loop de xG
            custo_xg = n_novos * CUSTO_ESTIMADO_XG_FIXTURE
            stats_list: list[dict] = []
            try:
                self.api.trava_saldo(custo_xg, saldo_minimo=SALDO_MIN_PARA_CALIBRACAO)
                for _, row in df_novos.iterrows():
                    s = self.api.buscar_stats_fixture(
                        int(row["fixture_id"]), int(row["home_id"]), int(row["away_id"])
                    )
                    stats_list.append(s)
            except CreditosInsuficientesError as e:
                log.warning(
                    f"Liga {league_id} s{season}: créditos insuficientes para xG "
                    f"({custo_xg} necessários — {e}). Fixtures entram no cache sem xG/scout."
                )
                stats_list = [{}] * n_novos

            df_novos = df_novos.copy()
            for col in [
                "xg_home", "xg_away",
                "h_shots_on", "h_shots_total", "h_possession", "h_corners",
                "h_yellows", "h_saves", "h_fouls",
                "a_shots_on", "a_shots_total", "a_possession", "a_corners",
                "a_yellows", "a_saves", "a_fouls",
            ]:
                df_novos[col] = [s.get(col) for s in stats_list]
            df_novos["season_year"] = season

            novos = df_novos.to_dict("records")
            cache_liga["registros"].extend(novos)
            ids_cache.update(int(r["fixture_id"]) for r in novos)
            houve_novidades = True

        # Persiste cache no arquivo local + atualiza historico_ids no JSONBin.
        # historico_ids é a âncora cloud que evita re-download após restart.
        if ids_cache:
            banco.historico_ids[chave] = list(ids_cache)
        if houve_novidades:
            cache_liga["atualizado_em"] = dt.datetime.now().isoformat()
            banco.historico_ligas[chave] = cache_liga
            self._merge_salvar_local(banco)

        # Monta DataFrame completo filtrado pelas seasons solicitadas
        todos = cache_liga["registros"]
        if not todos:
            return pd.DataFrame(columns=["home_id", "away_id", "home_goals", "away_goals", "date"]), nomes_global

        df_full = pd.DataFrame(todos)
        df_full["date"] = pd.to_datetime(df_full["date"])

        if "season_year" in df_full.columns:
            df_full = df_full[df_full["season_year"].isin(seasons)].reset_index(drop=True)

        n_xg = int(df_full["xg_home"].notna().sum()) if "xg_home" in df_full.columns else 0
        pct = n_xg / len(df_full) * 100 if len(df_full) > 0 else 0.0
        log.info(f"Liga {league_id}: cache final — {len(df_full)} jogos, xG em {n_xg} ({pct:.1f}%).")

        return df_full, nomes_global

    def obter_params_liga(
        self,
        league_id: int,
        season: int,
        forcar_recalibracao: bool = False,
    ) -> ParametrosLiga:
        """
        Retorna parâmetros calibrados de uma liga (MLE completo).
        - Se cache fresco (< INTERVALO_RECALIBRACAO_DIAS): retorna do cache
        - Senão (ou forcar_recalibracao=True): busca histórico na API + MLE + salva
        """
        banco = self.carregar_banco()
        chave = str(league_id)
        params_cache = banco.params_ligas.get(chave, {})

        if not forcar_recalibracao and not self.precisa_recalibrar(params_cache):
            try:
                return ParametrosLiga.from_dict(params_cache)
            except Exception as e:
                log.warning(f"Cache inválido para liga {league_id}: {e}. Recalibrando.")

        # ── Determina quais temporadas buscar ─────────────────────────────────
        # Ligas que usam ano-calendário (BR, Americas, JP): combina ano atual + ano anterior.
        # Isso garante que Brasileirão 2026 (em andamento) + 2025 (histórico completo)
        # entrem juntos no MLE. O decay temporal (xi) dá peso maior aos jogos de 2026.
        #
        # Ligas europeias (PL, La Liga…): usam season fornecida (ex: 2025 = temporada 2025-26).
        ano_atual = dt.date.today().year
        usar_ano_atual = league_id in LIGAS_TEMPORADA_ANO_ATUAL

        if usar_ano_atual:
            season_principal = ano_atual
            season_anterior  = ano_atual - 1
            seasons_label    = [season_anterior, season_principal]
            log.info(f"Liga {league_id} ({LIGAS_SUPORTADAS.get(league_id,'?')}): "
                     f"buscando temporadas {season_anterior} + {season_principal} (ano-calendário)")
        else:
            season_principal = season
            season_anterior  = None
            seasons_label    = [season_principal]
            log.info(f"Calibrando liga {league_id} (season {season_principal}) via MLE...")

        # Full Fetch bootstrap: liga europeia com cache vazio → busca também season anterior
        # para garantir volume mínimo de treino na primeira calibração. Após o bootstrap,
        # o cache terá registros e esta condição deixa de ser verdadeira (sem custo extra).
        if not usar_ano_atual and season_anterior is None:
            _hist_check = banco.historico_ligas.get(chave, {"registros": []})
            if not _hist_check.get("registros"):
                season_anterior = season_principal - 1
                seasons_label   = [season_anterior, season_principal]
                log.info(
                    f"Liga {league_id}: cache vazio → Full Fetch seasons "
                    f"{season_anterior}+{season_principal}"
                )

        # Delta Fetch: busca xG apenas para fixtures novos (cache local) — Dossiê v8 Seção 3.1
        df, nomes = self._obter_historico_com_delta_xg(league_id, season_principal, season_anterior)

        # Threshold mínimo de jogos — padrão 20, override para copas e seleções
        min_jogos = LIGAS_MIN_JOGOS.get(league_id, 20)
        if len(df) < min_jogos:
            raise ValueError(
                f"Liga {league_id} season {season_principal} tem apenas {len(df)} jogos finalizados. "
                f"Mínimo: {min_jogos}. Tente aguardar mais rodadas."
            )

        # Seleciona função de calibração: Copa do Mundo usa lógica adaptativa com prior FIFA
        min_aparicoes = LIGAS_MIN_APARICOES.get(league_id, 3)
        if league_id in LIGAS_COPA_MUNDO:
            calib_func = calibrar_copa_mundo
            log.info(f"Liga {league_id}: usando calibrar_copa_mundo (xi=0.003, prior_FIFA ativo)")
        else:
            calib_func = calibrar_liga

        # MLE em thread separada com timeout (proteção contra copas com 400+ times)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                calib_func, df, league_id, season_principal,
                nomes_times=nomes, seasons_incluidas=seasons_label,
                peso_xg=PESO_XG_PRODUCAO,
                min_aparicoes_time=min_aparicoes,
            )
            try:
                params = future.result(timeout=TIMEOUT_CALIBRACAO_SEGUNDOS)
            except FuturesTimeoutError:
                raise TimeoutError(
                    f"Liga {league_id} ({LIGAS_SUPORTADAS.get(league_id, '?')}): "
                    f"MLE excedeu {TIMEOUT_CALIBRACAO_SEGUNDOS}s. "
                    f"Possível: copa com muitas fases ou season com poucos jogos finalizados."
                )

        banco.params_ligas[chave] = params.to_dict()
        self.salvar_banco(banco)
        log.info(f"Liga {league_id} calibrada: {params.n_jogos_calibracao} jogos, "
                 f"seasons={seasons_label}, LL={params.log_likelihood:.2f}")
        return params

    def calcular_custo_delta(
        self,
        ligas: Optional[list[int]] = None,
        season: Optional[int] = None,
    ) -> dict:
        """
        Preview de custo do Delta Fetch SEM fazer download de xG.

        Para cada liga, busca apenas a lista de fixtures finalizados (1 crédito/chamada)
        e compara os IDs com o cache local. Retorna o delta: quantos fixtures faltam
        e quanto custará o download de xG.

        Args:
            ligas:  lista de league_ids (default: todas LIGAS_SUPORTADAS)
            season: season para ligas europeias (default: ano_atual - 1)

        Returns:
            {
                "n_novos_total":            int,
                "custo_estimado_creditos":  int,   # n_novos × CUSTO_ESTIMADO_XG_FIXTURE
                "ligas": [
                    {
                        "league_id":    int,
                        "nome":         str,
                        "n_novos_liga": int,
                        "seasons": [
                            {"season": int, "n_api": int, "n_cache": int,
                             "n_novos": int, "erro": str|None}
                        ],
                    }
                ],
            }
        """
        banco = self.carregar_banco()
        ano_atual = dt.date.today().year
        season_europeia = season or (ano_atual - 1)

        ligas_alvo = ligas or list(LIGAS_SUPORTADAS.keys())
        resultado_ligas = []
        n_novos_total = 0

        for league_id in ligas_alvo:
            chave = str(league_id)
            cache_liga = banco.historico_ligas.get(chave, {"registros": []})
            ids_local: set[int] = {int(r["fixture_id"]) for r in cache_liga.get("registros", [])}

            # Fallback: usa historico_ids do JSONBin quando cache local está vazio.
            ids_jsonbin: set[int] = set(int(i) for i in banco.historico_ids.get(chave, []))
            ids_cache: set[int] = ids_local | ids_jsonbin

            has_params = bool(banco.params_ligas.get(chave, {}).get("times"))

            usar_ano_atual = league_id in LIGAS_TEMPORADA_ANO_ATUAL
            if usar_ano_atual:
                seasons = [ano_atual - 1, ano_atual]
            else:
                seasons = [season_europeia]

            # NOTA: removido o skip automático para ligas nunca calibradas.
            # O skip causava bug: League One, Copa del Rey etc. retornavam 0 créditos
            # mesmo tendo centenas de jogos na API — a lista de fixtures (1 crédito/chamada)
            # é necessária para descobrir se a liga tem dados ou não.
            # Ligas verdadeiramente inativas (n_api=0) ainda vão para "aguardando" no Passo 2.

            detalhes_seasons = []
            n_novos_liga = 0

            for s in seasons:
                try:
                    df_api, _ = self.api.buscar_historico_liga(league_id, s, com_xg=False)
                    ids_api = set(df_api["fixture_id"].astype(int).tolist()) if not df_api.empty else set()
                    novos_ids = ids_api - ids_cache
                    n_novos = len(novos_ids)
                    detalhes_seasons.append({
                        "season":  s,
                        "n_api":   len(df_api),
                        "n_cache": len(df_api) - n_novos,
                        "n_novos": n_novos,
                        "erro":    None,
                    })
                    n_novos_liga += n_novos
                except Exception as e:
                    detalhes_seasons.append({
                        "season":  s,
                        "n_api":   0,
                        "n_cache": 0,
                        "n_novos": 0,
                        "erro":    str(e),
                    })

            n_cache_liga = sum(s.get("n_cache", 0) for s in detalhes_seasons if s.get("erro") is None)
            n_api_liga   = sum(s.get("n_api",   0) for s in detalhes_seasons if s.get("erro") is None)
            resultado_ligas.append({
                "league_id":    league_id,
                "nome":         LIGAS_SUPORTADAS.get(league_id, f"Liga {league_id}"),
                "n_novos_liga": n_novos_liga,
                "n_cache_liga": n_cache_liga,
                "n_api_liga":   n_api_liga,
                "seasons":      detalhes_seasons,
            })
            n_novos_total += n_novos_liga

        return {
            "n_novos_total":           n_novos_total,
            "custo_estimado_creditos": n_novos_total * CUSTO_ESTIMADO_XG_FIXTURE,
            "ligas":                   resultado_ligas,
        }

    def tocar_timestamp_liga(self, league_id: int) -> None:
        """
        Renova calibrado_em para agora sem re-executar MLE.

        Usado quando uma liga 'Velha' não tem jogos novos — ela está inativa
        entre fases do torneio, não desatualizada. O modelo D-C permanece
        válido; só o timestamp de controle é atualizado para evitar alarme
        falso de desatualização na UI. Lança ValueError se a liga nunca foi
        calibrada (sem modelo disponível para renovar).
        """
        banco = self.carregar_banco()
        chave = str(league_id)
        params_d = banco.params_ligas.get(chave)
        if not params_d or not params_d.get("calibrado_em"):
            raise ValueError(
                f"Liga {league_id} nunca foi calibrada — sem timestamp para renovar."
            )
        params_d["calibrado_em"] = dt.datetime.now().isoformat()
        banco.params_ligas[chave] = params_d
        self.salvar_banco(banco)
        log.info(
            f"Liga {league_id} ({LIGAS_SUPORTADAS.get(league_id, '?')}): "
            "timestamp renovado — liga inativa (0 jogos novos)."
        )

    def banco_em_memoria(self) -> BancoQG:
        """Retorna banco em memória sem re-consultar JSONBin.
        Sempre fresco após calibração ou tocar_timestamp_liga."""
        if self._banco is None:
            return self.carregar_banco()
        return self._banco

    def calibrar_liga_avulsa(self, league_id: int, season: int) -> ParametrosLiga:
        """
        Fallback: calibra qualquer liga (mesmo fora de LIGAS_SUPORTADAS) sob demanda.
        Usado pelo botão 'Forçar Busca' na UI quando um jogo tem liga desconhecida.
        Limiar menor (20 jogos) para aceitar copas em fase inicial.
        """
        return self.obter_params_liga(league_id, season, forcar_recalibracao=True)

    # ----------------------------------------------------------------
    # Wrappers de conveniência
    # ----------------------------------------------------------------
    def saldo_creditos(self) -> int:
        return self.api.saldo()

    def buscar_agenda_dia(self, data_str: str) -> list[dict]:
        return self.api.buscar_agenda_dia(data_str)

    def buscar_odds_jogo(self, fixture_id: int) -> dict:
        return self.api.buscar_odds_jogo(fixture_id)


# =========================================================================
# 6. FACTORY (para uso no Streamlit ou script)
# =========================================================================

def criar_dados_manager_de_secrets(secrets_dict: dict, diretorio_local: str = ".") -> DadosManager:
    """
    Constrói DadosManager a partir de um dict tipo st.secrets.

    Espera as chaves:
        - API_KEY_PRO (ou API_SPORTS_KEY)
        - JSONBIN_KEY
        - JSONBIN_BIN_ID
    """
    api_key = secrets_dict.get("API_KEY_PRO") or secrets_dict.get("API_SPORTS_KEY")
    jsonbin_key = secrets_dict.get("JSONBIN_KEY")
    jsonbin_id = secrets_dict.get("JSONBIN_BIN_ID")

    if not api_key:
        raise ValueError("Falta API_KEY_PRO (ou API_SPORTS_KEY) em secrets")
    if not jsonbin_key or not jsonbin_id:
        raise ValueError("Faltam JSONBIN_KEY e/ou JSONBIN_BIN_ID em secrets")

    return DadosManager(api_key, jsonbin_key, jsonbin_id, diretorio_local)


def criar_dados_manager_de_env(diretorio_local: str = ".") -> DadosManager:
    """Constrói DadosManager a partir de variáveis de ambiente (para testes CLI)."""
    return DadosManager(
        api_key=os.environ.get("API_SPORTS_KEY", ""),
        jsonbin_key=os.environ.get("JSONBIN_KEY", ""),
        jsonbin_id=os.environ.get("JSONBIN_BIN_ID", ""),
        diretorio_local=diretorio_local,
    )


# =========================================================================
# 7. SELF-TEST (sem chamadas reais à API)
# =========================================================================

def _self_test_offline() -> None:
    """Testes que NÃO consomem créditos da API."""
    print("=" * 70)
    print("DADOS.PY - SELF TEST OFFLINE")
    print("=" * 70)

    # Teste 1: BancoQG serializa/deserializa
    print("\n[TESTE 1] BancoQG round-trip")
    b1 = BancoQG(picks=[{"jogo": "A v B", "odd": 1.95}], banca_inicial=29.0)
    b1.params_ligas["39"] = {"league_id": 39, "season": 2025, "times": {"10": {"alpha": 1.1, "beta": 0.9, "n_jogos": 20}},
                              "home_advantage": 1.3, "rho": -0.05, "xi": 0.0019, "media_liga_gols": 2.7,
                              "calibrado_em": "2026-05-10T10:00:00", "n_jogos_calibracao": 200, "log_likelihood": -1500.0}
    d = b1.to_dict()
    b2 = BancoQG.from_dict(d)
    assert b2.picks == b1.picks
    assert b2.banca_inicial == 29.0
    assert "39" in b2.params_ligas
    print("  OK: BancoQG serializa/deserializa corretamente")

    # Teste 2: trava de saldo
    print("\n[TESTE 2] Trava de saldo")
    class _ApiMock(ApiSportsClient):
        def __init__(self, saldo_fake):
            self._fake = saldo_fake
        def saldo(self, cache_segundos=30):
            return self._fake

    api_mock_baixo = _ApiMock(30)
    try:
        api_mock_baixo.trava_saldo(custo_estimado=10)
        assert False, "Trava deveria ter disparado"
    except CreditosInsuficientesError as e:
        print(f"  OK: bloqueou com saldo 30 (msg: {str(e)[:60]}...)")

    api_mock_alto = _ApiMock(1000)
    try:
        api_mock_alto.trava_saldo(custo_estimado=10)
        print("  OK: permitiu com saldo 1000")
    except CreditosInsuficientesError:
        assert False, "Não deveria ter bloqueado"

    # Teste 3: precisa_recalibrar
    print("\n[TESTE 3] precisa_recalibrar")
    # Cria DadosManager fake (sem chamar API)
    class _DM(DadosManager):
        def __init__(self):
            pass
    dm = _DM()

    assert dm.precisa_recalibrar({}) is True
    assert dm.precisa_recalibrar({"calibrado_em": ""}) is True
    ontem = (dt.datetime.now() - dt.timedelta(days=1)).isoformat()
    semana = (dt.datetime.now() - dt.timedelta(days=8)).isoformat()
    assert dm.precisa_recalibrar({"calibrado_em": ontem}) is False
    assert dm.precisa_recalibrar({"calibrado_em": semana}) is True
    print("  OK: detecta cache fresco (1 dia) e velho (8 dias)")

    # Teste 4: ligas suportadas
    print("\n[TESTE 4] Ligas suportadas")
    print(f"  Total: {len(LIGAS_SUPORTADAS)} ligas configuradas")
    print(f"  Custo estimado calibração total (1x/semana): {len(LIGAS_SUPORTADAS) * CUSTO_ESTIMADO_HISTORICO_LIGA} créditos")
    print(f"  Custo diário típico (50 jogos): {50 * CUSTO_ESTIMADO_ODDS_JOGO + CUSTO_ESTIMADO_FIXTURES_DIA} créditos")

    print("\n" + "=" * 70)
    print("TODOS OS TESTES OFFLINE PASSARAM")
    print("=" * 70)
    print("\nNota: para teste online com API real, defina variáveis de ambiente:")
    print("  API_SPORTS_KEY, JSONBIN_KEY, JSONBIN_BIN_ID")
    print("E execute: python -c 'from dados import _self_test_online; _self_test_online()'")


def _self_test_online() -> None:
    """Testes que CONSOMEM créditos da API. Usar com moderação."""
    print("=" * 70)
    print("DADOS.PY - SELF TEST ONLINE (consome créditos!)")
    print("=" * 70)

    dm = criar_dados_manager_de_env()
    saldo = dm.saldo_creditos()
    print(f"\nSaldo inicial: {saldo}/7500")
    if saldo < 100:
        print("ABORTANDO: saldo muito baixo para testes online.")
        return

    print("\n[TESTE ONLINE 1] buscar_agenda_dia para hoje")
    hoje = dt.date.today().strftime("%Y-%m-%d")
    agenda = dm.buscar_agenda_dia(hoje)
    print(f"  Retornou {len(agenda)} jogos")
    print(f"  Saldo após: {dm.saldo_creditos()}/7500")

    print("\n[TESTE ONLINE 2] calibração Premier League (season 2025)")
    try:
        params = dm.obter_params_liga(39, 2025)
        print(f"  OK. {len(params.times)} times calibrados.")
        print(f"  gamma = {params.home_advantage:.3f}, rho = {params.rho:.3f}")
        print(f"  Saldo após: {dm.saldo_creditos()}/7500")
    except Exception as e:
        print(f"  ERRO: {e}")

    print("\nTeste online concluído.")


if __name__ == "__main__":
    import sys
    if "--online" in sys.argv:
        _self_test_online()
    else:
        _self_test_offline()
