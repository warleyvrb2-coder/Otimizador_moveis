# -*- coding: utf-8 -*-
"""
Edição manual do plano de corte.

Por que existe: o otimizador tem tempo limitado e às vezes deixa uma sobra
grande sem uso que o operador enxerga na hora. Chamar uma peça pra dentro
daquela sobra é um ganho real — mas só se o resultado ainda sair na máquina.

Por isso toda edição passa por três verificações, nesta ordem:

  1. CABE?  medida da peça mais a serra dentro do espaço livre
  2. NÃO SOBREPÕE?  nenhuma peça pode invadir outra
  3. AINDA É GUILHOTINA?  todo corte tem que atravessar a chapa de ponta a
     ponta. Um layout com peça "encaixada" no meio pode ser perfeito no papel
     e impossível numa seccionadora, que não faz corte parcial.

A terceira é a que ninguém lembra e a que estraga o lote.
"""
from dataclasses import dataclass

TOL = 0.5


@dataclass
class Retalho:
    x: float
    y: float
    w: float
    h: float

    @property
    def area_m2(self) -> float:
        return (self.w / 1000) * (self.h / 1000)


def _bordas(itens: list[dict], w: float, h: float) -> tuple[list, list]:
    xs = {0.0, float(w)}
    ys = {0.0, float(h)}
    for it in itens:
        xs.update((float(it['x']), float(it['x']) + float(it['w'])))
        ys.update((float(it['y']), float(it['y']) + float(it['h'])))
    return sorted(xs), sorted(ys)


def _ocupada(itens: list[dict], x0, y0, x1, y1) -> bool:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for it in itens:
        ix, iy = float(it['x']), float(it['y'])
        if ix - TOL < cx < ix + float(it['w']) + TOL and \
           iy - TOL < cy < iy + float(it['h']) + TOL:
            return True
    return False


def retalhos_livres(itens: list[dict], w: float, h: float,
                    minimo: float = 80.0) -> list[Retalho]:
    """
    Os espaços vazios da chapa, como retângulos.

    Trabalha numa grade formada pelas bordas das peças já posicionadas: dentro
    de cada célula dessa grade ou está tudo ocupado ou está tudo livre, então
    juntar células livres vizinhas dá os retângulos de sobra.

    Devolve só os MAXIMAIS (nenhum contido em outro) e acima de `minimo` nos
    dois lados — retalho menor que isso não serve pra peça nenhuma e só
    poluiria a tela.
    """
    xs, ys = _bordas(itens, w, h)
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx <= 0 or ny <= 0:
        return []

    livre = [[not _ocupada(itens, xs[i], ys[j], xs[i + 1], ys[j + 1])
              for j in range(ny)] for i in range(nx)]

    achados: list[tuple] = []
    for i in range(nx):
        for j in range(ny):
            if not livre[i][j]:
                continue
            # cresce à direita o máximo possível, e a cada largura cresce
            # para baixo o máximo que aquela largura permite
            max_j = ny
            for i2 in range(i, nx):
                if not livre[i2][j]:
                    break
                j2 = j
                while j2 < max_j and livre[i2][j2]:
                    j2 += 1
                max_j = j2
                if max_j == j:
                    break
                achados.append((xs[i], ys[j], xs[i2 + 1], ys[max_j]))

    # descarta os contidos em outro e os pequenos demais
    caixas = []
    for a in achados:
        if a[2] - a[0] < minimo or a[3] - a[1] < minimo:
            continue
        if any(b is not a and b[0] <= a[0] + TOL and b[1] <= a[1] + TOL
               and b[2] >= a[2] - TOL and b[3] >= a[3] - TOL and b != a
               for b in achados):
            continue
        caixas.append(a)

    unicos = sorted(set(caixas), key=lambda c: -((c[2] - c[0]) * (c[3] - c[1])))
    return [Retalho(x, y, x2 - x, y2 - y) for x, y, x2, y2 in unicos]


def sobrepoe(itens: list[dict]) -> tuple | None:
    for i in range(len(itens)):
        for j in range(i + 1, len(itens)):
            a, b = itens[i], itens[j]
            if (float(a['x']) < float(b['x']) + float(b['w']) - TOL and
                float(b['x']) < float(a['x']) + float(a['w']) - TOL and
                float(a['y']) < float(b['y']) + float(b['h']) - TOL and
                float(b['y']) < float(a['y']) + float(a['h']) - TOL):
                return a, b
    return None


def estagios(itens: list[dict], w: float, h: float) -> int:
    """
    Quantos estágios de corte o layout exige, ou 99 se não for guilhotinável.

    Um estágio é um conjunto de cortes PARALELOS: serrar oito tiras na mesma
    direção é um estágio só; virar a chapa 90° e cortar é o segundo. É essa a
    contagem que a seccionadora enxerga.
    """
    caixas = [(float(i['x']), float(i['y']), float(i['w']), float(i['h'])) for i in itens]
    return _estagios(caixas, 0.0, 0.0, float(w), float(h), None, {})


def _estagios(rects, x0, y0, w, h, dir_pai, memo) -> int:
    chave = (round(x0), round(y0), round(w), round(h), dir_pai)
    if chave in memo:
        return memo[chave]
    dentro = [r for r in rects
              if r[0] >= x0 - TOL and r[1] >= y0 - TOL
              and r[0] + r[2] <= x0 + w + TOL and r[1] + r[3] <= y0 + h + TOL]
    if not dentro:
        memo[chave] = 0
        return 0
    if len(dentro) == 1:
        r = dentro[0]
        if (abs(r[0] - x0) < TOL and abs(r[1] - y0) < TOL
                and abs(r[2] - w) < TOL and abs(r[3] - h) < TOL):
            memo[chave] = 0
            return 0

    melhor = 99
    for d in ('V', 'H'):
        i, t = (0, 2) if d == 'V' else (1, 3)
        lim0, lim1 = (x0, x0 + w) if d == 'V' else (y0, y0 + h)
        cortes = sorted({r[i] for r in dentro} | {r[i] + r[t] for r in dentro})
        for c in cortes:
            if not (lim0 + TOL < c < lim1 - TOL):
                continue
            if any(r[i] < c - TOL and r[i] + r[t] > c + TOL for r in dentro):
                continue                      # o corte atravessaria uma peça
            a = [r for r in dentro if r[i] + r[t] <= c + TOL]
            b = [r for r in dentro if r[i] >= c - TOL]
            if len(a) + len(b) != len(dentro):
                continue
            # Um dos lados pode sair vazio: é o corte de APARO, que separa a
            # peça da sobra. Exigir peça nos dois lados recusaria o layout
            # mais banal que existe — faixas paralelas com folga na borda.
            if not a and not b:
                continue
            if d == 'V':
                ca = _estagios(a, x0, y0, c - x0, h, d, memo)
                cb = _estagios(b, c, y0, x0 + w - c, h, d, memo)
            else:
                ca = _estagios(a, x0, y0, w, c - y0, d, memo)
                cb = _estagios(b, x0, c, w, y0 + h - c, d, memo)
            melhor = min(melhor, max(ca, cb) + (0 if d == dir_pai else 1))
    memo[chave] = melhor
    return melhor


def encaixar(retalho: Retalho, comp: float, larg: float, kerf: float,
             pode_girar: bool) -> dict | None:
    """
    Onde a peça fica dentro da sobra, ou None se não couber.

    Exige a folga do kerf além da medida: o corte que separa a peça nova das
    vizinhas também come material. É um pouco conservador quando a sobra
    encosta na borda da chapa, e essa é a direção certa de errar.
    """
    for w, h, girada in ((comp, larg, False), (larg, comp, True)):
        if girada and not pode_girar:
            continue
        if w + kerf <= retalho.w + TOL and h + kerf <= retalho.h + TOL:
            return {'x': retalho.x, 'y': retalho.y, 'w': w, 'h': h, 'rotated': girada}
    return None


def validar_padrao(itens: list[dict], w: float, h: float, max_estagios: int) -> list[str]:
    """Devolve a lista de problemas. Vazia significa que pode ir pra máquina."""
    problemas = []
    for it in itens:
        if (float(it['x']) < -TOL or float(it['y']) < -TOL
                or float(it['x']) + float(it['w']) > w + TOL
                or float(it['y']) + float(it['h']) > h + TOL):
            problemas.append(f"A peça {it.get('cod')} passa da borda da chapa.")
            break
    par = sobrepoe(itens)
    if par:
        problemas.append(f"As peças {par[0].get('cod')} e {par[1].get('cod')} se sobrepõem.")
    est = estagios(itens, w, h)
    if est >= 99:
        problemas.append('Este layout não é cortável em guilhotina: não existe corte que '
                          'atravesse a chapa de ponta a ponta sem passar por cima de uma peça.')
    elif est > max_estagios:
        problemas.append(f'Este layout exigiria {est} estágios de corte, e a máquina está '
                          f'configurada para {max_estagios}.')
    return problemas
