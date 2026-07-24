# -*- coding: utf-8 -*-
"""
Otimizador de corte guilhotinado (2 estágios) para seccionadora, via CP-SAT.

Modelo por chapa (guillotine 2-stage / "shelf packing"):
  - 1º estágio: a chapa é dividida em faixas horizontais ("shelves").
  - 2º estágio: dentro de cada faixa, as peças ficam lado a lado.
  Isso é fisicamente compatível com uma seccionadora (corte de ponta a ponta).

Como a quantidade de peças de cada tipo pode ser grande (centenas/milhares),
não modelamos peça-a-peça: cada "tipo" de peça (mesma cor+espessura+dimensão)
tem uma variável inteira de QUANTIDADE por (faixa, orientação). Isso mantém o
modelo pequeno (poucos tipos) mesmo com milhares de unidades.

Para permitir que a sobra de uma chapa sirva peças de outro Kambam (pool
combinado), resolvemos chapa a chapa, sempre a partir do MESMO pool restante
(cor+espessura), até esgotar as peças ou atingir um limite de segurança.
"""
import copy
from dataclasses import dataclass, field
from ortools.sat.python import cp_model

SCALE = 1  # dimensões já em mm inteiros


@dataclass
class PieceType:
    key: str            # identificador único (cod+dimensões)
    cod: str
    desc: str
    w: int               # comprimento (mm)
    h: int               # largura (mm)
    qty_total: int
    origem: list = field(default_factory=list)  # quais kambans/lotes contribuíram


@dataclass
class PlacedItem:
    piece_key: str
    cod: str
    desc: str
    shelf: int
    x: float
    y: float
    w: float
    h: float
    rotated: bool


@dataclass
class SheetResult:
    index: int
    items: list
    used_area: float
    sheet_area: float

    @property
    def aproveitamento(self) -> float:
        return 100.0 * self.used_area / self.sheet_area if self.sheet_area else 0.0


def _best_grid_fit(pool, w, h, strategy: str = 'area', value_dict: dict | None = None):
    """
    Entre os tipos com estoque > 0, acha o que melhor preenche um
    retângulo w x h formando uma grade (nx colunas x ny linhas),
    testando as duas orientações. Retorna (piece, iw, ih, rotated, nx, ny, count)
    ou None. O critério de escolha varia pela estratégia:
      - 'area'  : maior área total coberta (padrão, guloso puro)
      - 'width' : maior aproveitamento da LARGURA do retângulo (nx*iw/w),
                  tentando deixar sobra de altura (tira embaixo) em vez de
                  sobra de largura (tira lateral) — bom quando a tira
                  lateral tende a ficar estreita demais pra reaproveitar.
      - 'height': o espelho de 'width', prioriza aproveitar a ALTURA.
    """
    best = None
    best_score = -1
    for p in pool:
        if p.qty_total <= 0:
            continue
        for rotated in (False, True):
            iw = p.h if rotated else p.w
            ih = p.w if rotated else p.h
            if iw <= 0 or ih <= 0 or iw > w or ih > h:
                continue
            nx = int(w // iw)
            ny = int(h // ih)
            count = min(p.qty_total, nx * ny)
            if count <= 0:
                continue
            if strategy == 'width':
                score = (nx * iw) / w
            elif strategy == 'height':
                score = (ny * ih) / h
            elif strategy == 'value':
                score = count * (value_dict.get(p.key, 0.0) if value_dict else 0.0)
            else:
                score = count * iw * ih
            if score > best_score:
                best_score = score
                best = (p, iw, ih, rotated, nx, ny, count)
    return best


def _pack_rect(pool, x0: float, y0: float, w: float, h: float,
               placed: list, qty_used: dict, min_piece_area: float,
               strategy: str = 'area', depth: int = 0, value_dict: dict | None = None,
               max_depth: int | None = None):
    """
    Empacotamento guilhotinado recursivo: preenche o retângulo (x0,y0,w,h)
    com uma grade do melhor tipo de peça que achar, depois corta o que
    sobrou em DUAS tiras retangulares (direita e embaixo do bloco usado)
    e chama a si mesma em cada uma - permitindo corte tanto no sentido
    longitudinal quanto no transversal, alternando conforme a sobra.

    max_depth limita quantos "re-cortes" empilhados um pedaço pode sofrer
    (cada nível de recursão = pegar o pedaço resultante do corte anterior
    e cortar ele de novo). None = sem limite (só a trava de segurança em
    depth>60). Isso existe pra respeitar quantos estágios de corte a
    seccionadora consegue fazer numa chapa sem reprogramar demais.
    """
    if w <= 0 or h <= 0 or depth > 60 or w * h < min_piece_area:
        return
    if max_depth is not None and depth >= max_depth:
        return  # não corta mais essa sobra - fica sem uso, mas dentro do nº de estágios permitido

    fit = _best_grid_fit(pool, w, h, strategy, value_dict)
    if fit is None:
        return
    p, iw, ih, rotated, nx, ny, count = fit

    used_w = nx * iw
    used_h = ny * ih

    placed_count = 0
    for r in range(ny):
        for c in range(nx):
            if placed_count >= count:
                break
            placed.append(PlacedItem(
                piece_key=p.key, cod=p.cod, desc=p.desc, shelf=0,
                x=x0 + c * iw, y=y0 + r * ih, w=iw, h=ih, rotated=rotated,
            ))
            placed_count += 1
        if placed_count >= count:
            break

    p.qty_total -= count
    qty_used[p.key] = qty_used.get(p.key, 0) + count

    # corte guilhotinado: uma tira à direita do bloco usado (altura toda)
    # e uma tira embaixo do bloco usado (só a largura que ele ocupou)
    _pack_rect(pool, x0 + used_w, y0, w - used_w, h, placed, qty_used, min_piece_area,
               strategy, depth + 1, value_dict, max_depth)
    _pack_rect(pool, x0, y0 + used_h, used_w, h - used_h, placed, qty_used, min_piece_area,
               strategy, depth + 1, value_dict, max_depth)


STRATEGIES = ('area', 'width', 'height')


def _pack_sheet_best_of(pool, sheet_w: int, sheet_h: int, min_piece_area: float):
    """
    "Beam search" leve: roda o empacotamento guilhotinado recursivo com
    várias estratégias de escolha (multi-start), cada uma numa CÓPIA do
    pool (sem mexer no pool real), e fica com o resultado de MAIOR área
    aproveitada. Só então aplica a baixa de estoque no pool de verdade.
    """
    import copy

    best_placed, best_qty_used, best_area = None, None, -1
    for strategy in STRATEGIES:
        pool_copy = [copy.copy(p) for p in pool]  # PieceType raso: qty_total é int, seguro copiar raso
        placed, qty_used = [], {}
        _pack_rect(pool_copy, 0, 0, sheet_w, sheet_h, placed, qty_used, min_piece_area, strategy)
        area = sum(it.w * it.h for it in placed)
        if area > best_area:
            best_area = area
            best_placed, best_qty_used = placed, qty_used

    for key, qty in best_qty_used.items():
        for p in pool:
            if p.key == key:
                p.qty_total -= qty
                break
    return best_placed, best_qty_used


def _refine_last_sheet_cpsat(pool, sheet_w: int, sheet_h: int, max_shelves: int, time_limit_s: float,
                              value_dict: dict | None = None):
    """
    Resolve o empacotamento de 1 chapa via CP-SAT (modelo de faixas),
    maximizando ÁREA por padrão (uso normal, refino da última chapa) ou,
    se value_dict for passado, maximizando o VALOR (usado como pricing
    exato do column generation, onde o valor é o dual de cada peça).
    """
    model = cp_model.CpModel()
    usable = [p for p in pool if p.qty_total > 0 and
              ((p.w <= sheet_w and p.h <= sheet_h) or (p.h <= sheet_w and p.w <= sheet_h))]
    if not usable:
        return [], {}

    shelves = range(max_shelves)
    orients = [0, 1]
    x = {}
    for p in usable:
        for s in shelves:
            for o in orients:
                w = p.w if o == 0 else p.h
                h = p.h if o == 0 else p.w
                if w <= sheet_w and h <= sheet_h:
                    x[(p.key, s, o)] = model.NewIntVar(0, int(p.qty_total), f'x_{p.key}_{s}_{o}')

    for p in usable:
        vars_p = [x[k] for k in x if k[0] == p.key]
        if vars_p:
            model.Add(sum(vars_p) <= int(p.qty_total))

    shelf_h = [model.NewIntVar(0, sheet_h, f'shelf_h_{s}') for s in shelves]
    for s in shelves:
        for p in usable:
            for o in orients:
                key = (p.key, s, o)
                if key not in x:
                    continue
                h = p.h if o == 0 else p.w
                b = model.NewBoolVar(f'used_{p.key}_{s}_{o}')
                model.Add(x[key] >= 1).OnlyEnforceIf(b)
                model.Add(x[key] == 0).OnlyEnforceIf(b.Not())
                model.Add(shelf_h[s] >= h).OnlyEnforceIf(b)
        width_terms = [x[(p.key, s, o)] * (p.w if o == 0 else p.h)
                       for p in usable for o in orients if (p.key, s, o) in x]
        if width_terms:
            model.Add(sum(width_terms) <= sheet_w)
    model.Add(sum(shelf_h) <= sheet_h)

    if value_dict is not None:
        # pricing exato: maximiza soma(dual_i * quantidade_i), valores em
        # float -> escala pra inteiro (CP-SAT não aceita coeficiente float)
        SCALE_V = 1000
        obj_terms = [x[(p.key, s, o)] * int(round(value_dict.get(p.key, 0.0) * SCALE_V))
                     for p in usable for s in shelves for o in orients if (p.key, s, o) in x]
    else:
        obj_terms = [x[(p.key, s, o)] * p.w * p.h
                     for p in usable for s in shelves for o in orients if (p.key, s, o) in x]
    model.Maximize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], {}

    placed = []
    qty_used = {}
    y_cursor = 0
    for s in shelves:
        h_s = solver.Value(shelf_h[s])
        if h_s == 0:
            continue
        x_cursor = 0
        for p in usable:
            for o in orients:
                key = (p.key, s, o)
                if key not in x:
                    continue
                qty = solver.Value(x[key])
                if qty <= 0:
                    continue
                w = p.w if o == 0 else p.h
                h = p.h if o == 0 else p.w
                for _ in range(qty):
                    placed.append(PlacedItem(piece_key=p.key, cod=p.cod, desc=p.desc,
                                              shelf=s, x=x_cursor, y=y_cursor, w=w, h=h, rotated=bool(o)))
                    x_cursor += w
                qty_used[p.key] = qty_used.get(p.key, 0) + qty
        y_cursor += h_s
    return placed, qty_used


def optimize_group(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                    max_shelves: int = 12, time_limit_s: float = 6.0,
                    max_sheets: int = 2000, refine_last_with_cpsat: bool = True) -> tuple:
    """
    Empacota TODO o pool de um grupo (mesma cor+espessura), chapa a chapa,
    até esgotar as peças. Como o pool é único e compartilhado, sobra de uma
    chapa pode ser preenchida com peças de qualquer Kambam do grupo.

    Usa a heurística rápida de faixas para o grosso das chapas (escala bem
    até milhares de peças) e, opcionalmente, refina a ÚLTIMA chapa de cada
    grupo com CP-SAT exato (pool pequeno nesse ponto, resolve em segundos).
    """
    remaining = {p.key: p for p in pieces}
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)
    results = []

    min_piece_area = min((p.w * p.h for p in pieces if p.w and p.h), default=1)

    for sheet_idx in range(1, max_sheets + 1):
        pool = [p for p in remaining.values() if p.qty_total > 0]
        if not pool:
            break

        total_qty_restante = sum(p.qty_total for p in pool)
        use_cpsat = refine_last_with_cpsat and total_qty_restante <= 60

        if use_cpsat:
            placed, qty_used = _refine_last_sheet_cpsat(pool, sheet_w_mm, sheet_h_mm, max_shelves, time_limit_s)
        else:
            placed, qty_used = [], {}
            pool_copy = [copy.copy(p) for p in pool]  # evita decrementar o pool real 2x
            _pack_rect(pool_copy, 0, 0, sheet_w_mm, sheet_h_mm, placed, qty_used, min_piece_area)

        if not placed:
            break  # nada mais coube (sobrou peça grande demais, etc.)

        used_area = sum((it.w / 1000) * (it.h / 1000) for it in placed)
        results.append(SheetResult(index=sheet_idx, items=placed,
                                    used_area=used_area, sheet_area=sheet_area))

        for key, qty in qty_used.items():
            remaining[key].qty_total -= qty

    sobras = [p for p in remaining.values() if p.qty_total > 0]
    return results, sobras
