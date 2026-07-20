#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
buscador_mercados.py
═══════════════════════════════════════════════════════════════════════════════
Motor probabilístico en tiempo real para mercados de fútbol vía iSportsAPI.
Notificaciones Telegram mediante python-telegram-bot (MarkdownV2 seguro).

Requisitos:
    pip install requests python-dotenv python-telegram-bot

Variables de entorno:
    YOUR_API_KEY        →  iSportsAPI
    TELEGRAM_BOT_TOKEN  →  Bot de Telegram
    TELEGRAM_CHAT_ID    →  Chat/canal destino
"""

import os
import sys
import time
import math
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

import requests
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

load_dotenv()

API_KEY: str = os.getenv("YOUR_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL: str = "http://api.isportsapi.com"
MIRROR_URL: str = "http://api2.isportsapi.com"
OUTPUT_MD: str = "partidos_alta_probabilidad.md"

UMBRAL_PROBABILIDAD: float = 85.0
TOP_N_TELEGRAM: int = 5
TOP_N_MARKDOWN: int = 50          # ← Límite estricto para evitar archivos gigantes
RATE_LIMIT_SECONDS: int = 10
REQUEST_TIMEOUT: int = 30

STATUS_CODES: Dict[int, str] = {
    -1: "No iniciado", 0: "En calentamiento", 1: "1ª parte", 2: "Descanso",
    3: "2ª parte", 4: "Prórroga", 5: "Penaltis", 6: "Finalizado",
    7: "Suspendido", 8: "Interrumpido", 9: "Anulado", 10: "En espera",
    11: "Descanso (ET)", 12: "Penaltis (ET)", 13: "Descanso (Pen)",
}

STATS_TYPE_CODES: Dict[int, str] = {
    1: "posesion", 2: "tiros_total", 3: "tiros_puerta", 4: "tiros_fuera",
    5: "tiros_bloqueados", 6: "ataques_peligrosos", 7: "ataques", 8: "corners",
    9: "paradas", 10: "faltas", 11: "offsides", 12: "pases_total",
    13: "pases_completados", 14: "pases_fallidos", 15: "centros",
    16: "pases_largos", 17: "duelos_aereos", 18: "posesion_rival",
    19: "tiros_dentro_area", 20: "tiros_fuera_area", 21: "posesion_3er",
    22: "entradas", 23: "intercepciones", 24: "despejes", 25: "posesion_media",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("buscador_mercados")


# ═══════════════════════════════════════════════════════════════════════════════
# MODELOS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PartidoEnVivo:
    match_id: str
    league_id: str
    league_name: str
    home_name: str
    away_name: str
    status_code: int
    minute: int
    home_score: int
    away_score: int
    home_half_score: int
    away_half_score: int
    home_red: int
    away_red: int
    home_yellow: int
    away_yellow: int
    home_corner: int
    away_corner: int
    has_lineup: bool
    is_neutral: bool
    injury_time: int
    match_time_ts: int
    half_start_ts: int
    stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class MercadoProbabilidad:
    mercado: str
    seleccion: str
    probabilidad: float
    razonamiento: str


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTE iSportsAPI
# ═══════════════════════════════════════════════════════════════════════════════

class ISportsAPIClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("YOUR_API_KEY es obligatoria.")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        self.last_call_ts: float = 0.0

    def _wait_rate_limit(self) -> None:
        elapsed = time.time() - self.last_call_ts
        if elapsed < RATE_LIMIT_SECONDS:
            sleep_for = RATE_LIMIT_SECONDS - elapsed
            logger.info(f"Rate-limit: esperando {sleep_for:.1f}s...")
            time.sleep(sleep_for)
        self.last_call_ts = time.time()

    def _call(self, endpoint: str, params: Optional[Dict[str, Any]] = None, use_mirror: bool = False) -> Dict[str, Any]:
        base = MIRROR_URL if use_mirror else BASE_URL
        url = f"{base}{endpoint}"
        req_params = {"api_key": self.api_key}
        if params:
            req_params.update(params)
        self._wait_rate_limit()

        for attempt in range(1, 4):
            try:
                logger.info(f"GET {url} (intento {attempt}/3)")
                resp = self.session.get(url, params=req_params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("code") != 0:
                    msg = payload.get("message", "Error iSportsAPI")
                    logger.error(f"iSportsAPI error: {msg}")
                    return {"code": -1, "message": msg, "data": []}
                return payload
            except requests.exceptions.ConnectionError as exc:
                logger.warning(f"Error de conexión: {exc}")
                if not use_mirror and attempt == 3:
                    return self._call(endpoint, params, use_mirror=True)
                time.sleep(2 ** attempt)
            except requests.exceptions.Timeout as exc:
                logger.warning(f"Timeout: {exc}")
                time.sleep(2 ** attempt)
            except requests.exceptions.HTTPError as exc:
                logger.error(f"HTTP error: {exc}")
                return {"code": -1, "message": str(exc), "data": []}
        return {"code": -1, "message": "Máximo de reintentos", "data": []}

    def get_livescores(self) -> List[Dict[str, Any]]:
        return self._call("/sport/football/livescores").get("data", []) or []

    def get_stats(self) -> List[Dict[str, Any]]:
        return self._call("/sport/football/stats").get("data", []) or []


# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICADOR TELEGRAM (python-telegram-bot)
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.warning("Telegram no configurado.")

    async def _send_async(self, text: str) -> bool:
        bot = Bot(token=self.bot_token)
        try:
            await bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            logger.info("✅ Telegram: mensaje enviado correctamente.")
            return True
        except Exception as exc:
            logger.error(f"❌ Error enviando a Telegram: {exc}")
            return False
        finally:
            await bot.session.close()

    def enviar_top_mercados(self, hallazgos: List[Dict[str, Any]]) -> bool:
        if not self.enabled:
            logger.info("Telegram deshabilitado.")
            return False
        if not hallazgos:
            logger.info("No hay hallazgos para Telegram.")
            return False

        top = sorted(hallazgos, key=lambda x: x["probabilidad"], reverse=True)[:TOP_N_TELEGRAM]
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "🎯 *TOP 5 MERCADOS DE ALTA PROBABILIDAD*",
            f"📅 `{escape_markdown(ahora, version=2)}`",
            "",
        ]

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for i, h in enumerate(top):
            emoji = emojis[i] if i < len(emojis) else f"{i+1}."
            prob = h["probabilidad"]
            bar_len = int(prob / 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)

            partido = h["partido"][:50] + "..." if len(h["partido"]) > 50 else h["partido"]
            liga = h["liga"][:30] + "..." if len(h["liga"]) > 30 else h["liga"]

            lines.append(
                f"{emoji} *{escape_markdown(h['mercado'], version=2)}* \\- "
                f"`{escape_markdown(h['seleccion'], version=2)}`"
            )
            lines.append(f"   🏆 *{prob}%* {bar}")
            lines.append(f"   ⚽ {escape_markdown(partido, version=2)}")
            lines.append(
                f"   📊 {escape_markdown(liga, version=2)} | "
                f"{h['minuto']}' | {escape_markdown(h['marcador'], version=2)}"
            )
            lines.append("")

        lines.append("\\-\\-" + "\\-" * 28)
        lines.append("🔔 Umbral mínimo: ≥ 85% probabilidad matemática")
        lines.append("📡 Fuente: iSportsAPI en tiempo real")

        mensaje = "\n".join(lines)
        if len(mensaje) > 4000:
            mensaje = mensaje[:4000] + "\n\n... (truncado)"

        return asyncio.run(self._send_async(mensaje))


# ═══════════════════════════════════════════════════════════════════════════════
# MOTOR PROBABILÍSTICO
# ═══════════════════════════════════════════════════════════════════════════════

class MotorProbabilistico:
    def __init__(self):
        self.prob_conversion: Dict[str, float] = {
            "tiros_puerta": 0.32, "tiros_dentro_area": 0.25,
            "tiros_fuera_area": 0.05, "tiros_total": 0.10,
            "ataques_peligrosos": 0.08, "corners": 0.03,
        }

    @staticmethod
    def _tiempo_restante(minuto: int, status: int, injury_time: int = 0) -> int:
        if status in (6, 7, 8, 9):
            return 0
        if status == 2:
            return 45
        if status in (4, 5, 11, 12, 13):
            return 15
        if status == 1:
            return max(0, 45 - minuto) + 45
        if status == 3:
            return max(0, 90 - minuto + injury_time)
        return 90

    @staticmethod
    def _factor_campo(status: int) -> float:
        return 0.0 if status in (6, 7, 8, 9) else 0.35

    @staticmethod
    def _factor_tarjetas_rojas(home_red: int, away_red: int) -> Tuple[float, float]:
        return (-1.1 * home_red + 0.9 * away_red, -1.1 * away_red + 0.9 * home_red)

    @staticmethod
    def _poisson_prob(lam: float, k: int) -> float:
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return (lam ** k) * math.exp(-lam) / math.factorial(k)

    @staticmethod
    def _poisson_cdf(lam: float, max_k: int) -> float:
        if lam <= 0:
            return 1.0 if max_k >= 0 else 0.0
        prob = 0.0
        for k in range(max_k + 1):
            prob += (lam ** k) * math.exp(-lam) / math.factorial(k)
        return prob

    def _calcular_xg_restante(self, partido: PartidoEnVivo) -> Tuple[float, float]:
        stats = partido.stats
        minuto = partido.minute
        status = partido.status_code
        t_restante = self._tiempo_restante(minuto, status, partido.injury_time)
        if t_restante <= 0:
            return 0.0, 0.0

        minutos_jugados = max(1, minuto)
        factor_tiempo = t_restante / minutos_jugados if minutos_jugados > 0 else 1.0

        home_xg_stats = 0.0
        away_xg_stats = 0.0

        def _val(stat_name: str, side: str) -> float:
            if stat_name not in stats:
                return 0.0
            raw = stats[stat_name].get(side, 0)
            try:
                return float(raw)
            except (ValueError, TypeError):
                return 0.0

        home_xg_stats += _val("tiros_puerta", "home") * self.prob_conversion["tiros_puerta"]
        away_xg_stats += _val("tiros_puerta", "away") * self.prob_conversion["tiros_puerta"]
        home_xg_stats += _val("tiros_dentro_area", "home") * self.prob_conversion["tiros_dentro_area"]
        away_xg_stats += _val("tiros_dentro_area", "away") * self.prob_conversion["tiros_dentro_area"]
        home_xg_stats += _val("tiros_fuera_area", "home") * self.prob_conversion["tiros_fuera_area"]
        away_xg_stats += _val("tiros_fuera_area", "away") * self.prob_conversion["tiros_fuera_area"]
        home_xg_stats += _val("ataques_peligrosos", "home") * self.prob_conversion["ataques_peligrosos"]
        away_xg_stats += _val("ataques_peligrosos", "away") * self.prob_conversion["ataques_peligrosos"]
        home_xg_stats += partido.home_corner * self.prob_conversion["corners"]
        away_xg_stats += partido.away_corner * self.prob_conversion["corners"]

        if "posesion" in stats:
            poss_h = _val("posesion", "home")
            poss_a = _val("posesion", "away")
            if poss_h > 60:
                home_xg_stats += 0.15 * factor_tiempo
            if poss_a > 60:
                away_xg_stats += 0.15 * factor_tiempo

        delta_h, delta_a = self._factor_tarjetas_rojas(partido.home_red, partido.away_red)
        home_xg_stats += delta_h * factor_tiempo
        away_xg_stats += delta_a * factor_tiempo

        if not partido.is_neutral:
            home_xg_stats += self._factor_campo(status) * factor_tiempo

        if home_xg_stats < 0.1 and partido.home_score == 0:
            home_xg_stats = 0.05 * factor_tiempo
        if away_xg_stats < 0.1 and partido.away_score == 0:
            away_xg_stats = 0.05 * factor_tiempo

        return max(0.0, home_xg_stats), max(0.0, away_xg_stats)

    def evaluar_mercados(self, partido: PartidoEnVivo) -> List[MercadoProbabilidad]:
        resultados: List[MercadoProbabilidad] = []
        minuto = partido.minute
        status = partido.status_code
        t_restante = self._tiempo_restante(minuto, status, partido.injury_time)

        if t_restante <= 0:
            return resultados

        home_xg, away_xg = self._calcular_xg_restante(partido)
        total_xg = home_xg + away_xg
        home_goals = partido.home_score
        away_goals = partido.away_score
        total_goals = home_goals + away_goals

        # 1. OVER/UNDER
        for linea in [0.5, 1.5, 2.5, 3.5, 4.5]:
            goles_necesarios = int(linea - total_goals)
            if goles_necesarios < 0:
                prob_over, prob_under = 100.0, 0.0
            else:
                prob_over = (1.0 - self._poisson_cdf(total_xg, goles_necesarios)) * 100
                prob_under = self._poisson_cdf(total_xg, goles_necesarios) * 100

            if prob_over >= UMBRAL_PROBABILIDAD:
                resultados.append(MercadoProbabilidad(
                    mercado=f"Over/Under {linea} Goles",
                    seleccion=f"Over {linea}",
                    probabilidad=round(prob_over, 2),
                    razonamiento=f"xG={total_xg:.2f}, goles={total_goals}",
                ))
            if prob_under >= UMBRAL_PROBABILIDAD:
                resultados.append(MercadoProbabilidad(
                    mercado=f"Over/Under {linea} Goles",
                    seleccion=f"Under {linea}",
                    probabilidad=round(prob_under, 2),
                    razonamiento=f"xG={total_xg:.2f}, goles={total_goals}",
                ))

        # 2. BTTS
        prob_home_scores = 1.0 - self._poisson_prob(home_xg, 0)
        prob_away_scores = 1.0 - self._poisson_prob(away_xg, 0)
        prob_btts = prob_home_scores * prob_away_scores
        prob_btts_no = 1.0 - prob_btts

        if prob_btts * 100 >= UMBRAL_PROBABILIDAD:
            resultados.append(MercadoProbabilidad(
                mercado="Ambos Marcan (BTTS)", seleccion="Sí",
                probabilidad=round(prob_btts * 100, 2),
                razonamiento=f"P(home)={prob_home_scores*100:.1f}%, P(away)={prob_away_scores*100:.1f}%",
            ))
        if prob_btts_no * 100 >= UMBRAL_PROBABILIDAD:
            resultados.append(MercadoProbabilidad(
                mercado="Ambos Marcan (BTTS)", seleccion="No",
                probabilidad=round(prob_btts_no * 100, 2),
                razonamiento=f"P(≤1)={prob_btts_no*100:.1f}%",
            ))

        # 3. DOBLE OPORTUNIDAD
        if total_xg > 0:
            ratio_home = home_xg / total_xg
            ratio_away = away_xg / total_xg
        else:
            ratio_home = ratio_away = 0.5

        goal_diff = home_goals - away_goals
        red_diff = partido.away_red - partido.home_red
        momentum = (goal_diff + red_diff * 0.5) / max(1, minuto / 45)

        prob_1x = min(0.99, max(0.01, ratio_home + 0.15 + momentum * 0.1))
        prob_x2 = min(0.99, max(0.01, ratio_away + 0.15 - momentum * 0.1))
        prob_12 = 1.0 - (0.25 - abs(momentum) * 0.05)

        for sel, prob, desc in [
            ("1X", prob_1x, "Home gana o empata"),
            ("X2", prob_x2, "Away gana o empata"),
            ("12", prob_12, "Sin empate"),
        ]:
            pct = round(prob * 100, 2)
            if pct >= UMBRAL_PROBABILIDAD:
                resultados.append(MercadoProbabilidad(
                    mercado="Doble Oportunidad", seleccion=sel, probabilidad=pct,
                    razonamiento=f"{desc}, momentum={momentum:.2f}",
                ))

        # 4. HÁNDICAPS
        for handicap_line in [-1.5, -0.5, 0.5, 1.5]:
            effective_home = home_goals + handicap_line
            margin_home = effective_home - away_goals + home_xg - away_xg
            prob_home_hc = 1.0 / (1.0 + math.exp(-margin_home * 1.5))
            hc_pct = round(prob_home_hc * 100, 2)
            if hc_pct >= UMBRAL_PROBABILIDAD:
                sign = "+" if handicap_line > 0 else ""
                resultados.append(MercadoProbabilidad(
                    mercado=f"Hándicap Asiático ({sign}{handicap_line})",
                    seleccion=f"Home {sign}{handicap_line}", probabilidad=hc_pct,
                    razonamiento=f"margin={margin_home:.2f}",
                ))

            effective_away = away_goals + handicap_line
            margin_away = effective_away - home_goals + away_xg - home_xg
            prob_away_hc = 1.0 / (1.0 + math.exp(-margin_away * 1.5))
            hc_pct_a = round(prob_away_hc * 100, 2)
            if hc_pct_a >= UMBRAL_PROBABILIDAD:
                sign = "+" if handicap_line > 0 else ""
                resultados.append(MercadoProbabilidad(
                    mercado=f"Hándicap Asiático ({sign}{handicap_line})",
                    seleccion=f"Away {sign}{handicap_line}", probabilidad=hc_pct_a,
                    razonamiento=f"margin={margin_away:.2f}",
                ))

        # 5. SIGUIENTE GOL
        if total_xg > 0:
            prob_next_home = home_xg / total_xg
        else:
            prob_next_home = 0.5
        prob_next_home = min(0.95, max(0.05, prob_next_home + momentum * 0.05 + red_diff * 0.03))
        prob_next_away = 1.0 - prob_next_home

        nh_pct = round(prob_next_home * 100, 2)
        na_pct = round(prob_next_away * 100, 2)

        if nh_pct >= UMBRAL_PROBABILIDAD:
            resultados.append(MercadoProbabilidad(
                mercado="Siguiente Equipo en Anotar",
                seleccion=partido.home_name, probabilidad=nh_pct,
                razonamiento=f"xG ratio={prob_next_home*100:.1f}%",
            ))
        if na_pct >= UMBRAL_PROBABILIDAD:
            resultados.append(MercadoProbabilidad(
                mercado="Siguiente Equipo en Anotar",
                seleccion=partido.away_name, probabilidad=na_pct,
                razonamiento=f"xG ratio={prob_next_away*100:.1f}%",
            ))

        # 6. 1X2
        if total_xg > 0:
            p1_base = home_xg / total_xg
            p2_base = away_xg / total_xg
        else:
            p1_base = p2_base = 0.35

        px_base = max(0.15, min(0.35, 1.0 - p1_base - p2_base))
        total_p = p1_base + px_base + p2_base
        p1, px, p2 = p1_base / total_p, px_base / total_p, p2_base / total_p

        tiempo_factor = max(0.1, min(1.0, t_restante / 90))
        if home_goals > away_goals:
            p1 += (1 - tiempo_factor) * 0.15
            p2 -= (1 - tiempo_factor) * 0.10
        elif away_goals > home_goals:
            p2 += (1 - tiempo_factor) * 0.15
            p1 -= (1 - tiempo_factor) * 0.10
        else:
            px += (1 - tiempo_factor) * 0.10

        total_p = p1 + px + p2
        p1, px, p2 = p1 / total_p, px / total_p, p2 / total_p

        for sel, prob, label in [
            ("1", p1, f"{partido.home_name} gana"),
            ("X", px, "Empate"),
            ("2", p2, f"{partido.away_name} gana"),
        ]:
            pct = round(prob * 100, 2)
            if pct >= UMBRAL_PROBABILIDAD:
                resultados.append(MercadoProbabilidad(
                    mercado="Ganador del Encuentro (1X2)", seleccion=sel,
                    probabilidad=pct, razonamiento=label,
                ))

        return resultados


# ═══════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ═══════════════════════════════════════════════════════════════════════════════

class BuscadorMercados:
    def __init__(self, api_key: str):
        self.client = ISportsAPIClient(api_key)
        self.motor = MotorProbabilistico()
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    @staticmethod
    def _parse_livescore(raw: Dict[str, Any]) -> Optional[PartidoEnVivo]:
        try:
            extra = raw.get("extraExplain", {}) or {}
            minute = extra.get("minute", 0)
            if minute == 0 and raw.get("matchTime"):
                elapsed = int(time.time()) - int(raw["matchTime"])
                minute = min(90, max(1, elapsed // 60))

            return PartidoEnVivo(
                match_id=str(raw.get("matchId", "")),
                league_id=str(raw.get("leagueId", "")),
                league_name=str(raw.get("leagueName", "Desconocida")),
                home_name=str(raw.get("homeName", "Home")),
                away_name=str(raw.get("awayName", "Away")),
                status_code=int(raw.get("status", -1)),
                minute=minute,
                home_score=int(raw.get("homeScore", 0) or 0),
                away_score=int(raw.get("awayScore", 0) or 0),
                home_half_score=int(raw.get("homeHalfScore", 0) or 0),
                away_half_score=int(raw.get("awayHalfScore", 0) or 0),
                home_red=int(raw.get("homeRed", 0) or 0),
                away_red=int(raw.get("awayRed", 0) or 0),
                home_yellow=int(raw.get("homeYellow", 0) or 0),
                away_yellow=int(raw.get("awayYellow", 0) or 0),
                home_corner=int(raw.get("homeCorner", 0) or 0),
                away_corner=int(raw.get("awayCorner", 0) or 0),
                has_lineup=bool(raw.get("hasLineup", False)),
                is_neutral=bool(raw.get("neutral", False)),
                injury_time=int(raw.get("injuryTime", 0) or 0),
                match_time_ts=int(raw.get("matchTime", 0) or 0),
                half_start_ts=int(raw.get("halfStartTime", 0) or 0),
            )
        except Exception as exc:
            logger.warning(f"Error parseando partido: {exc}")
            return None

    @staticmethod
    def _merge_stats(partidos: List[PartidoEnVivo], stats_raw: List[Dict[str, Any]]) -> None:
        stats_by_match: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for entry in stats_raw:
            mid = str(entry.get("matchId", ""))
            if mid:
                stats_by_match[mid].extend(entry.get("stats", []))

        for p in partidos:
            for stat in stats_by_match.get(p.match_id, []):
                type_code = int(stat.get("type", 0))
                type_name = STATS_TYPE_CODES.get(type_code, f"stat_{type_code}")
                raw_home = stat.get("home", 0)
                raw_away = stat.get("away", 0)
                try:
                    val_home = float(raw_home)
                except (ValueError, TypeError):
                    val_home = 0.0
                try:
                    val_away = float(raw_away)
                except (ValueError, TypeError):
                    val_away = 0.0
                p.stats[type_name] = {"home": val_home, "away": val_away}

    def ejecutar(self) -> str:
        logger.info("═" * 70)
        logger.info("INICIANDO BÚSQUEDA DE MERCADOS DE ALTA PROBABILIDAD")
        logger.info("═" * 70)

        logger.info("[1/5] Descargando livescores desde iSportsAPI...")
        livescores_raw = self.client.get_livescores()
        if not livescores_raw:
            logger.warning("No se recibieron datos de livescores.")
            self.telegram.enviar_top_mercados([])
            return self._generar_md_sin_datos()
        logger.info(f"    → {len(livescores_raw)} partidos recibidos.")

        logger.info("[2/5] Parseando estructura de partidos...")
        partidos: List[PartidoEnVivo] = []
        for raw in livescores_raw:
            p = self._parse_livescore(raw)
            if p:
                partidos.append(p)
        logger.info(f"    → {len(partidos)} partidos válidos en vivo.")

        if not partidos:
            logger.warning("Ningún partido en estado jugable.")
            self.telegram.enviar_top_mercados([])
            return self._generar_md_sin_datos()

        logger.info("[3/5] Descargando estadísticas técnicas en vivo...")
        stats_raw = self.client.get_stats()
        if stats_raw:
            logger.info(f"    → {len(stats_raw)} registros de stats recibidos.")
            self._merge_stats(partidos, stats_raw)
        else:
            logger.info("    → Stats no disponibles; continuando con datos básicos.")

        logger.info("[4/5] Ejecutando motor probabilístico...")
        hallazgos: List[Dict[str, Any]] = []
        for p in partidos:
            mercados = self.motor.evaluar_mercados(p)
            if mercados:
                for m in mercados:
                    hallazgos.append({
                        "partido": f"{p.home_name} vs {p.away_name}",
                        "liga": p.league_name,
                        "minuto": p.minute,
                        "estado": STATUS_CODES.get(p.status_code, f"Código {p.status_code}"),
                        "marcador": f"{p.home_score}-{p.away_score}",
                        "mercado": m.mercado,
                        "seleccion": m.seleccion,
                        "probabilidad": m.probabilidad,
                        "razonamiento": m.razonamiento,
                    })

        logger.info(f"    → {len(hallazgos)} mercados superan el umbral de {UMBRAL_PROBABILIDAD}%.")

        # ── ENVIAR TOP 5 A TELEGRAM ──────────────────────────────────────────
        logger.info("[4.5/5] Enviando TOP 5 a Telegram...")
        self.telegram.enviar_top_mercados(hallazgos)

        logger.info("[5/5] Generando reporte Markdown (top 50)...")
        return self._generar_md(hallazgos)

    def _generar_md(self, hallazgos: List[Dict[str, Any]]) -> str:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not hallazgos:
            return self._generar_md_sin_datos()

        # ← LÍMITE ESTRICO: solo top 50 para evitar archivos de 17000+ líneas
        hallazgos_ordenados = sorted(hallazgos, key=lambda x: x["probabilidad"], reverse=True)[:TOP_N_MARKDOWN]

        lines: List[str] = [
            "# 🎯 Partidos de Alta Probabilidad — iSportsAPI",
            "",
            f"> **Generado:** `{ahora}`  ",
            f"> **Fuente:** iSportsAPI (http://api.isportsapi.com)  ",
            f"> **Umbral de filtro:** ≥ {UMBRAL_PROBABILIDAD}%  ",
            f"> **Mostrando:** top {len(hallazgos_ordenados)} de {len(hallazgos)} hallazgos",
            "",
            "---",
            "",
        ]

        for i, h in enumerate(hallazgos_ordenados, 1):
            prob_bar = "█" * int(h["probabilidad"] / 5) + "░" * (20 - int(h["probabilidad"] / 5))
            lines.extend([
                f"## {i}. {h['partido']}",
                "",
                f"| Campo | Valor |",
                f"|-------|-------|",
                f"| **Liga** | {h['liga']} |",
                f"| **Minuto / Estado** | {h['minuto']}' — {h['estado']} |",
                f"| **Marcador** | {h['marcador']} |",
                f"| **Mercado** | {h['mercado']} |",
                f"| **Selección** | `{h['seleccion']}` |",
                f"| **Probabilidad** | **{h['probabilidad']}%** {prob_bar} |",
                f"| **Razonamiento** | {h['razonamiento']} |",
                "",
                "---",
                "",
            ])

        contenido = "\n".join(lines)
        with open(OUTPUT_MD, "w", encoding="utf-8") as f:
            f.write(contenido)

        logger.info(f"    → Reporte guardado en: {OUTPUT_MD} ({len(hallazgos_ordenados)} hallazgos)")
        return contenido

    def _generar_md_sin_datos(self) -> str:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        contenido = (
            "# 🎯 Partidos de Alta Probabilidad — iSportsAPI\n"
            "\n"
            f"> **Generado:** `{ahora}`  \n"
            f"> **Fuente:** iSportsAPI (http://api.isportsapi.com)  \n"
            f"> **Umbral de filtro:** ≥ {UMBRAL_PROBABILIDAD}%\n"
            "\n"
            "---\n"
            "\n"
            "## ⚠️ Sin mercados detectados\n"
            "\n"
            "**No se detectaron mercados de alta probabilidad en este momento.**\n"
            "\n"
            "Posibles causas:\n"
            "- No hay partidos en vivo actualmente.\n"
            "- Ningún partido en curso supera el umbral matemático del 85%.\n"
            "- La suscripción de iSportsAPI no incluye datos de estadísticas técnicas.\n"
            "\n"
            "*Recomendación:* Ejecute el script nuevamente durante horarios de partidos.\n"
        )
        with open(OUTPUT_MD, "w", encoding="utf-8") as f:
            f.write(contenido)
        logger.info(f"    → Reporte vacío guardado en: {OUTPUT_MD}")
        return contenido


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        if not API_KEY:
            print(
                "\n❌ ERROR: Variable de entorno YOUR_API_KEY no configurada.\n"
                "   Ejecute: export YOUR_API_KEY='su_clave_de_isportsapi'\n"
                "   O cree un archivo .env con: YOUR_API_KEY=su_clave\n"
            )
            sys.exit(1)

        buscador = BuscadorMercados(API_KEY)
        buscador.ejecutar()
        print(f"\n✅ Proceso completado. Revisa el archivo: {OUTPUT_MD}\n")

    except KeyboardInterrupt:
        print("\n\n⛔ Interrumpido por el usuario.\n")
        sys.exit(0)
    except Exception as exc:
        logger.exception("Error fatal en la ejecución")
        print(f"\n❌ Error fatal: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
