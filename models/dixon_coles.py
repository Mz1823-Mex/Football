#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dixon_coles.py
Modelo estadístico Dixon-Coles calibrado exclusivamente con métricas In-Play de iSportsAPI.
"""

from typing import Any, Dict, Tuple

import numpy as np
import scipy.stats as stats


class DixonColesModel:
    def __init__(self, rho: float = -0.13):
        self.rho = rho

    def tau(self, x: int, y: int, lambda_h: float, mu_a: float) -> float:
        """Ajuste Tau de Dixon-Coles para dependencia en marcadores bajos (0-0, 1-0, 0-1, 1-1)."""
        if x == 0 and y == 0:
            return 1.0 - (lambda_h * mu_a * self.rho)
        elif x == 1 and y == 0:
            return 1.0 + (mu_a * self.rho)
        elif x == 0 and y == 1:
            return 1.0 + (lambda_h * self.rho)
        elif x == 1 and y == 1:
            return 1.0 - self.rho
        return 1.0

    def calcular_xg_restante_isports(
        self,
        stats_live: Dict[str, Dict[str, float]],
        minuto: int,
        home_red: int,
        away_red: int,
    ) -> Tuple[float, float]:
        """
        Calcula el xG proyectado para los minutos restantes basándose UNICAMENTE
        en la tasa de volumen de juego por minuto entregada por iSportsAPI.
        """
        minuto_actual = max(1, minuto)
        minutos_restantes = max(0, 90 - minuto_actual)

        if minutos_restantes <= 0:
            return 0.0, 0.0

        # Extractores directos de iSportsAPI
        def _get_stat(nombre: str, lado: str) -> float:
            return float(stats_live.get(nombre, {}).get(lado, 0.0))

        # 1. Proyección de Expectativa de Gol por minuto acumulado (iSportsAPI)
        # Pesos probabilísticos por tipo de acción recibida en la API:
        # - Tiros a puerta: 0.30 xG
        # - Tiros dentro del área: 0.15 xG
        # - Tiros fuera de área/puerta: 0.04 xG
        # - Ataques peligrosos: 0.015 xG
        # - Tiros de esquina: 0.03 xG

        xg_acumulado_home = (
            (_get_stat("tiros_puerta", "home") * 0.30)
            + (_get_stat("tiros_dentro_area", "home") * 0.15)
            + (_get_stat("tiros_fuera", "home") * 0.04)
            + (_get_stat("ataques_peligrosos", "home") * 0.015)
            + (_get_stat("corners", "home") * 0.03)
        )

        xg_acumulado_away = (
            (_get_stat("tiros_puerta", "away") * 0.30)
            + (_get_stat("tiros_dentro_area", "away") * 0.15)
            + (_get_stat("tiros_fuera", "away") * 0.04)
            + (_get_stat("ataques_peligrosos", "away") * 0.015)
            + (_get_stat("corners", "away") * 0.03)
        )

        # 2. Ritmo por minuto (Tasa xG/minuto real generada en el partido actual)
        ritmo_xg_home = xg_acumulado_home / minuto_actual
        ritmo_xg_away = xg_acumulado_away / minuto_actual

        # 3. Factor de tarjetas rojas en vivo (Métricas directos de iSportsAPI)
        # Si un equipo tiene tarjeta roja, su generación ofensiva cae y la del rival sube
        factor_roja_home = 1.0 - (0.25 * home_red) + (0.15 * away_red)
        factor_roja_away = 1.0 - (0.25 * away_red) + (0.15 * home_red)

        # 4. Cálculo de Lambda / Mu para el tiempo restante
        lambda_h = max(
            0.01, ritmo_xg_home * minutos_restantes * max(0.2, factor_roja_home)
        )
        mu_a = max(0.01, ritmo_xg_away * minutos_restantes * max(0.2, factor_roja_away))

        return min(3.5, lambda_h), min(3.5, mu_a)

    def evaluar_probabilidad_over(
        self, goles_actuales: int, linea: float, lambda_h: float, mu_a: float
    ) -> float:
        """Matriz de probabilidad Dixon-Coles basada en Poisson Modificado."""
        goles_necesarios = int(np.ceil(linea - goles_actuales))
        if goles_necesarios <= 0:
            return 95.0

        prob_under = 0.0
        for h in range(7):
            for a in range(7):
                if (h + a) < goles_necesarios:
                    p_h = stats.poisson.pmf(h, lambda_h)
                    p_a = stats.poisson.pmf(a, mu_a)
                    t_adj = self.tau(h, a, lambda_h, mu_a)
                    prob_under += p_h * p_a * t_adj

        prob_over = max(0.0, min(0.95, 1.0 - prob_under))
        return round(prob_over * 100, 2)
