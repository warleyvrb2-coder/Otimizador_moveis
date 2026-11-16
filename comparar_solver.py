# -*- coding: utf-8 -*-
"""
Compara o Motor (o que vai para produção) com o solver matemático (Column
Generation) em Kambans de verdade. É só um TERMÔMETRO: o solver não entra em plano
nenhum, mas mostra se o Motor está longe do que a matemática alcança e quanto o
solver mistura peças (ele não segue a regra de peça junta).

Uso:  python comparar_solver.py [segundos_por_grupo] kambam1.pdf [kambam2.pdf ...]
Ex.:  python comparar_solver.py 120 "Kambam ( LOTE 1447 ).pdf"
Roda em minutos: o solver usa o tempo todo que você der a cada cor/espessura.
"""
import os
import sys
import time
from collections import Counter

import pipeline
import sequencial
from column_generation import optimize_group_cg
from optimizer import PieceType

CHAPA_W, CHAPA_H, KERF, ESTAGIOS = 2750, 1860, 4.4, 3
AREA_CHAPA = CHAPA_W * CHAPA_H / 1e6


def resumo_mistura(padroes: list) -> str:
    """Texto com quantas chapas têm 1, 2 ou 3+ tipos de peça (o quanto o plano mistura)."""
    por_tipos = Counter(min(len(p.counts), 3) for p in padroes for _ in range(p.repeticoes))
    return f"1 tipo={por_tipos[1]} 2 tipos={por_tipos[2]} 3+ tipos={por_tipos[3]}"


def comparar_grupo(nome: str, cor: str, pecas: dict, segundos: float) -> dict:
    """
    Roda Motor e solver no mesmo grupo (cor e espessura) e imprime uma linha.
    Devolve {'motor': chapas, 'solver': chapas} para o total.
    """
    novas = lambda: [PieceType(**{**p.__dict__}) for p in pecas.values()]
    area_minima = sum(p.w * p.h * p.qty_total for p in pecas.values()) / 1e6 / AREA_CHAPA

    t = time.time()
    chapas_motor, _, padroes_motor = sequencial.optimize_group_sequencial(
        novas(), CHAPA_W, CHAPA_H, KERF, ESTAGIOS)
    seg_motor = time.time() - t

    t = time.time()
    chapas_solver, _, padroes_solver = optimize_group_cg(
        novas(), CHAPA_W, CHAPA_H, time_budget_s=segundos, kerf=KERF, estagios=ESTAGIOS)
    seg_solver = time.time() - t

    print(f"{nome[:34]:34} {cor:12} {len(pecas):2} tipos | mínimo por área {area_minima:6.0f} | "
          f"MOTOR {len(chapas_motor):5} ({len(padroes_motor)} padrões; {resumo_mistura(padroes_motor)}; {seg_motor:.0f}s) | "
          f"SOLVER {len(chapas_solver):5} ({len(padroes_solver)} padrões; {resumo_mistura(padroes_solver)}; {seg_solver:.0f}s)",
          flush=True)
    return {'motor': len(chapas_motor), 'solver': len(chapas_solver)}


def main(argumentos: list[str]) -> int:
    """Lê os argumentos (segundos opcionais + PDFs), compara cada grupo e imprime a soma."""
    if not argumentos:
        print(__doc__)
        return 1
    segundos = 120.0
    if argumentos[0].replace('.', '', 1).isdigit():
        segundos, argumentos = float(argumentos[0]), argumentos[1:]
    soma = Counter()
    for caminho in argumentos:
        nome = os.path.basename(caminho)
        pecas, _ = pipeline.ler_kambans([(caminho, nome)])
        for (cor, esp), grupo in pipeline.agrupar(pecas, True).items():
            if len(grupo) < 3:      # grupo trivial: não diz nada sobre o Motor
                continue
            soma.update(comparar_grupo(nome, cor, grupo, segundos))
    if soma:
        dif = 100 * (soma['solver'] - soma['motor']) / soma['motor']
        print(f"SOMA: MOTOR {soma['motor']} x SOLVER {soma['solver']} chapas "
              f"({dif:+.1f}% do solver contra o Motor)")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
