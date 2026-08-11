# -*- coding: utf-8 -*-
"""
Column Generation para o corte de chapas (cutting stock 2D guilhotinado).

Ideia central: em vez de decidir "chapa por chapa" (heurística gulosa),
o problema vira "quais PADRÕES de corte usar, e quantas vezes cada um":

  MESTRE (LP):  minimizar nº de chapas
                sujeito a: para cada tipo de peça, a soma das vezes que
                ele aparece nos padrões escolhidos >= demanda daquele tipo

  PRICING:      dado o "preço-sombra" (dual) de cada tipo de peça vindo
                do mestre, gera um NOVO padrão de corte (1 chapa) que
                maximize o valor coberto - se esse valor for maior que o
                custo de 1 chapa, vale a pena adicionar esse padrão à
                lista e resolver o mestre de novo.

Isso dá visão GLOBAL do lote inteiro (ao contrário da heurística, que
decide uma chapa e nunca mais reconsidera).

100% gratuito: GLOP (LP) e CP-SAT (arredondamento final) já vêm dentro
do OR-Tools, que já está no requirements.txt do projeto.
"""
import copy
import time
from dataclasses import dataclass

from ortools.linear_solver import pywraplp
from ortools.sat.python import cp_model

from optimizer import (PieceType, PlacedItem, SheetResult, _pack_rect,
                        _refine_last_sheet_cpsat, KERF_MM)


@dataclass
class Pattern:
    counts: dict          # piece_key -> quantidade nesse padrão
    items: list            # PlacedItem já posicionados (pra desenhar depois)
    used_area: float
    repeticoes: int = 0    # quantas chapas usam ESTE padrão

    @property
    def aproveitamento(self) -> float:
        return 100.0 * self.used_area / self._area_chapa if getattr(self, '_area_chapa', 0) else 0.0


def _pattern_signature(counts: dict) -> tuple:
    return tuple(sorted(counts.items()))


def _generate_pattern(piece_info: dict, demand_cap: dict, sheet_w: int, sheet_h: int,
                       strategy: str = 'area', value_dict: dict | None = None,
                       exact: bool = False, max_shelves: int = 12, time_limit_s: float = 4.0,
                       max_depth: int | None = None, kerf: float = KERF_MM,
                       estagios: int = 3) -> Pattern:
    """
    Gera 1 padrão de corte (1 chapa).
    - exact=False: empacotamento guilhotinado recursivo (rápido, heurístico).
                   max_depth limita quantos re-cortes empilhados são
                   permitidos (2 = faixa + corte dentro da faixa, sem
                   nenhum nível a mais; None = sem limite).
    - exact=True : CP-SAT (modelo de faixas), sempre 2 estágios por
                   natureza do modelo (faixa horizontal + linha única
                   dentro dela) - resolve o pricing de forma exata dado
                   o value_dict, encontra padrões que a heurística sozinha
                   não acha.
    """
    pool = []
    for key, cap in demand_cap.items():
        if cap <= 0:
            continue
        base = piece_info[key]
        pool.append(PieceType(key=base.key, cod=base.cod, desc=base.desc,
                               w=base.w, h=base.h, qty_total=cap,
                               pode_girar=base.pode_girar))

    if exact:
        placed, qty_used = _refine_last_sheet_cpsat(pool, sheet_w, sheet_h, max_shelves, time_limit_s,
                                                      value_dict=value_dict, kerf=kerf,
                                                      estagios=estagios)
    else:
        min_piece_area = min((p.w * p.h for p in pool), default=1)
        placed, qty_used = [], {}
        _pack_rect(pool, 0, 0, sheet_w, sheet_h, placed, qty_used, min_piece_area,
                   strategy=strategy, value_dict=value_dict, max_depth=max_depth, kerf=kerf)
    used_area = sum(it.w * it.h for it in placed) / 1_000_000
    return Pattern(counts=qty_used, items=placed, used_area=used_area)


def _solve_master_lp(patterns: list, demand: dict, piece_keys: list):
    """Resolve o LP relaxado (GLOP): minimiza nº de chapas, atende a demanda."""
    solver = pywraplp.Solver.CreateSolver('GLOP')
    x = [solver.NumVar(0, solver.infinity(), f'x{j}') for j in range(len(patterns))]

    constraints = {}
    for key in piece_keys:
        c = solver.Constraint(demand[key], solver.infinity(), f'demand_{key}')
        for j, pat in enumerate(patterns):
            qtd = pat.counts.get(key, 0)
            if qtd:
                c.SetCoefficient(x[j], qtd)
        constraints[key] = c

    solver.Minimize(solver.Sum(x))
    status = solver.Solve()
    if status != pywraplp.Solver.OPTIMAL:
        return None, None

    duals = {key: constraints[key].dual_value() for key in piece_keys}
    return x, duals


def _solve_master_ip(patterns: list, demand: dict, piece_keys: list, time_limit_s: float = 15.0):
    """Arredonda a solução final: quantas vezes usar cada padrão, em número INTEIRO."""
    model = cp_model.CpModel()
    ub = int(sum(demand.values())) + 1
    x = [model.NewIntVar(0, ub, f'x{j}') for j in range(len(patterns))]

    for key in piece_keys:
        terms = [x[j] * pat.counts.get(key, 0) for j, pat in enumerate(patterns) if pat.counts.get(key, 0)]
        if terms:
            model.Add(sum(terms) >= int(demand[key]))

    model.Minimize(sum(x))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return [solver.Value(v) for v in x]


def optimize_group_cg(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                       max_iters: int = 80, time_budget_s: float = 40.0,
                       max_depth: int | None = None, kerf: float = KERF_MM,
                       estagios: int = 3) -> tuple:
    """
    Otimiza um grupo (mesma cor+espessura) via column generation.
    Retorna (sheets, sobras) no mesmo formato que optimizer.optimize_group,
    pra plugar direto no app.py sem mudar o resto do pipeline.

    max_depth: limite de estágios de corte permitidos nos padrões gerados
    pela heurística (2 = só faixa + linha dentro da faixa; 3 = mais um
    nível; None = sem limite). Os padrões vindos do CP-SAT exato já são
    sempre 2 estágios por natureza do modelo, então não precisam desse
    parâmetro.
    """
    t_start = time.time()
    piece_info = {p.key: p for p in pieces}
    demand = {p.key: p.qty_total for p in pieces}
    piece_keys = list(demand.keys())
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)

    # 1) padrões iniciais: heurística gulosa em 3 variações, pra já começar
    #    com uma solução viável decente (evita o LP começar "no zero")
    patterns = []
    seen = set()
    for strategy in ('area', 'width', 'height'):
        remaining = {k: v for k, v in demand.items()}
        guard = 0
        while sum(remaining.values()) > 0 and guard < 3000:
            guard += 1
            pat = _generate_pattern(piece_info, remaining, sheet_w_mm, sheet_h_mm, strategy=strategy,
                                     max_depth=max_depth, kerf=kerf, estagios=estagios)
            if not pat.items:
                break
            sig = _pattern_signature(pat.counts)
            if sig not in seen:
                seen.add(sig)
                patterns.append(pat)
            for k, q in pat.counts.items():
                remaining[k] -= q

    # 2) column generation: LP relaxado + pricing guiado pelos duais
    for _ in range(max_iters):
        if time.time() - t_start > time_budget_s:
            break
        x, duals = _solve_master_lp(patterns, demand, piece_keys)
        if duals is None:
            break

        # pricing: gera 1 padrão novo maximizando valor (dual) coberto,
        # usando como teto a própria demanda (não faz sentido um padrão
        # ter mais peças de um tipo do que o total ainda precisado).
        # Exato via CP-SAT - encontra combinações que a heurística gulosa
        # não acha sozinha, o que é o ponto principal do método.
        # kerf e estagios PRECISAM ser repassados aqui: é este o caminho
        # que gera os padrões que o CG de fato adiciona à solução, e sem
        # eles o resultado final saía com corte de espessura zero e mais
        # estágios do que a máquina foi configurada pra fazer.
        new_pat = _generate_pattern(piece_info, demand, sheet_w_mm, sheet_h_mm,
                                     value_dict=duals, exact=True, time_limit_s=4.0,
                                     kerf=kerf, estagios=estagios)
        if not new_pat.items:
            break

        reduced_cost = sum(duals.get(k, 0) * q for k, q in new_pat.counts.items()) - 1.0
        sig = _pattern_signature(new_pat.counts)
        if reduced_cost <= 1e-4 or sig in seen:
            break  # nenhum padrão novo melhora o mestre -> convergiu

        seen.add(sig)
        patterns.append(new_pat)

    # 3) arredondamento final: quantas vezes usar cada padrão (inteiro)
    usage = _solve_master_ip(patterns, demand, piece_keys,
                              time_limit_s=max(5.0, time_budget_s - (time.time() - t_start)))
    if usage is None:
        return [], list(pieces)

    # Os padrões (não as chapas) são a unidade que interessa pro operador:
    # ele prepara a máquina UMA vez e corta N chapas iguais. Um lote de 600
    # chapas costuma ser umas poucas dezenas de padrões repetidos.
    usados = []
    for pat, n in zip(patterns, usage):
        if n <= 0:
            continue
        pat.repeticoes = n
        pat.used_area = sum((it.w / 1000) * (it.h / 1000) for it in pat.items)
        pat._area_chapa = sheet_area
        usados.append(pat)
    usados.sort(key=lambda p: -p.repeticoes)

    sheets = []
    idx = 1
    for pat in usados:
        for _ in range(pat.repeticoes):
            sheets.append(SheetResult(index=idx, items=pat.items,
                                       used_area=pat.used_area, sheet_area=sheet_area))
            idx += 1

    produced = {}
    for pat in usados:
        for k, q in pat.counts.items():
            produced[k] = produced.get(k, 0) + q * pat.repeticoes

    sobras = []
    for p in pieces:
        falta = p.qty_total - produced.get(p.key, 0)
        if falta > 0:
            sobras.append(PieceType(key=p.key, cod=p.cod, desc=p.desc, w=p.w, h=p.h, qty_total=falta))

    return sheets, sobras, usados
