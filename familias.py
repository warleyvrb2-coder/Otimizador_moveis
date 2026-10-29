# -*- coding: utf-8 -*-
"""
Famílias de peças: o que o operador chama de "kit".

Peças que saem do mesmo móvel têm quantidades PROPORCIONAIS no Kambam (ex.:
1.160, 928 e 232 = 5 : 4 : 1). O sistema descobre isso sozinho, só pelas
quantidades - nenhum cadastro novo. Uma família vira um padrão repetido N
chapas, com "proporção x c" peças de cada tipo por chapa, de modo que todas as
peças dela terminam juntas (e nenhuma passa do pedido).

A forma da família é decidida pelo preenchimento: uma peça só entra no kit se
isso AUMENTA o aproveitamento da chapa cheia da família (já com o enchimento).
"""
from math import gcd
from functools import reduce

import edicao
from column_generation import Pattern, _generate_pattern
from optimizer import PlacedItem, _maior_primeiro, _cabem

# peças de um mesmo tipo por kit; acima disso a proporção é coincidência de
# números grandes, não um móvel (ex.: 330 e 400 dão 33 : 40)
RAZAO_MAX = 12
GANHO_MIN = 0.005   # a peça só entra na família se somar pelo menos 0,5 p.p.
# parceiro que o operador já juntou antes entra mesmo perdendo até 1 p.p.: o
# histórico dele vale mais que a conta de uma chapa só
TOLERANCIA_APRENDIDA = 0.01
# tipos de peça diferentes numa mesma chapa (família + enchimento). Os planos do
# operador têm até 3; 4 foi o equilíbrio escolhido (3 custou ~7 chapas a mais
# no lote 1456 e 6 deixava mosaico de 6 tipos): acima disso vira mosaico
MAX_TIPOS = 4


def cortar_excesso(pat: Pattern, chave: str, limite: int) -> Pattern:
    """
    Tira do padrão as peças de `chave` acima de `limite` (as últimas da lista,
    que o empacotador coloca por último).

    O empacotador fecha faixa inteira e passa do teto pedido: com 2 peças
    pendentes ele devolvia 4. Tirar peça nunca estraga a guilhotina, e o
    espaço liberado volta a ser sobra para as menores.
    """
    excesso = pat.counts.get(chave, 0) - limite
    if excesso <= 0:
        return pat
    itens = list(pat.items)
    for pos in range(len(itens) - 1, -1, -1):
        if excesso and itens[pos].piece_key == chave:
            itens.pop(pos)
            excesso -= 1
    counts = dict(pat.counts)
    counts[chave] = limite
    return Pattern(counts=counts, items=itens,
                    used_area=sum(i.w * i.h for i in itens) / 1_000_000)


def _padrao_exato(piece_info: dict, demanda: dict, sheet_w: int, sheet_h: int,
                   estrategia: str, kerf: float, estagios: int) -> Pattern | None:
    """
    Um padrão com EXATAMENTE `demanda` (peça -> quantidade) numa chapa, ou None
    se o empacotador não consegue encaixar tudo. Excesso do empacotador é cortado.
    """
    pat = _generate_pattern(piece_info, demanda, sheet_w, sheet_h, strategy=estrategia,
                             kerf=kerf, estagios=estagios)
    if any(pat.counts.get(k, 0) < q for k, q in demanda.items()):
        return None
    for k, q in demanda.items():
        pat = cortar_excesso(pat, k, q)
    return pat


def maior_kit_por_chapa(piece_info: dict, kit: dict, kits_total: int, sheet_w: int,
                         sheet_h: int, estrategia: str, kerf: float,
                         estagios: int) -> tuple[int, Pattern | None]:
    """
    Quantos kits cabem numa chapa (no máximo `kits_total`) e o padrão deles.

    kit: peça -> peças por kit. Tenta do teto por área para baixo e para no
    primeiro que o empacotador consegue fechar. (0, None) se nem 1 kit cabe.
    """
    if len(kit) == 1:
        # peça isolada: o que couber de uma chapa cheia, cortado no pedido
        (chave, _), = kit.items()
        pat = _generate_pattern(piece_info, {chave: kits_total}, sheet_w, sheet_h,
                                 strategy=estrategia, kerf=kerf, estagios=estagios)
        n = min(pat.counts.get(chave, 0), kits_total)
        return (n, cortar_excesso(pat, chave, n)) if n > 0 else (0, None)

    area_kit = sum(piece_info[k].w * piece_info[k].h * r for k, r in kit.items())
    teto = min(kits_total, int(sheet_w * sheet_h // area_kit))
    for c in range(teto, 0, -1):
        pat = _padrao_exato(piece_info, {k: c * r for k, r in kit.items()},
                             sheet_w, sheet_h, estrategia, kerf, estagios)
        if pat is not None:
            return c, pat
    return 0, None


def preencher_sobras(pat: Pattern, restante: dict, excluir: set, vezes: int,
                       piece_info: dict, sheet_w: int, sheet_h: int, kerf: float,
                       estagios: int, prioridade: dict | None = None,
                       elegiveis: set | None = None, inteiro: bool = False) -> Pattern:
    """
    Completa as sobras da chapa da família com as peças menores, da mais
    comprida para a mais curta, sem passar de MAX_TIPOS tipos na chapa.

    `elegiveis` limita quais peças podem entrar (None = qualquer pendente).
    `inteiro` exige que o tipo caiba TODO (o que cabe nesta chapa por
    repetição); se não couber inteiro ele nem entra - assim o resto de um tipo
    não se espalha em pedaços por vários padrões.

    `vezes` é quantas chapas repetem este padrão: cada peça de enchimento entra
    no máximo `pendente // vezes` por chapa, para a repetição nunca passar do
    pedido. `excluir` são as peças da própria família. `prioridade` ({peça: peso})
    põe na frente as que o operador já costuma juntar com esta família.

    Cada peça entra em BLOCO (colunas x linhas) dentro da maior sobra onde mais
    cabem, e o bloco só fica se o padrão continuar cortável em guilhotina com o
    nº de estágios da máquina (mesma checagem do editor manual). Sem isso a
    sobra à direita das peças grandes ficava vazia e o aproveitamento caía.
    """
    itens = [{'cod': i.cod, 'desc': i.desc, 'piece_key': i.piece_key, 'shelf': 0,
              'x': i.x, 'y': i.y, 'w': i.w, 'h': i.h, 'rotated': i.rotated} for i in pat.items]
    counts = dict(pat.counts)
    tipos_livres = MAX_TIPOS - len(counts)
    ordem = sorted((k for k, q in restante.items() if q > 0 and k not in excluir
                    and (elegiveis is None or k in elegiveis)),
                   key=lambda k: ((-(prioridade or {}).get(k, 0),)
                                  + _maior_primeiro(piece_info[k].w, piece_info[k].h, piece_info[k].cod)))
    for k in ordem:
        if tipos_livres <= 0:
            break
        peca = piece_info[k]
        falta = restante[k] // vezes
        antes = (list(itens), dict(counts))
        while falta > 0:
            livres = edicao.retalhos_livres(itens, sheet_w, sheet_h, minimo=30.0, kerf=kerf)
            candidatos = []
            for r in livres:
                cols, linhas = _cabem(r.w, peca.w, kerf), _cabem(r.h, peca.h, kerf)
                if cols and linhas:
                    candidatos.append((min(cols * linhas, falta), cols, linhas, r))
            # mais peças primeiro; empate, a menor sobra (encaixe justo)
            candidatos.sort(key=lambda c: (-c[0], c[3].w * c[3].h))
            colocou = False
            for n, cols, linhas, r in candidatos:
                bloco = []
                for idx in range(n):
                    bloco.append({'cod': peca.cod, 'desc': peca.desc, 'piece_key': peca.key,
                                  'shelf': 0, 'rotated': False, 'w': peca.w, 'h': peca.h,
                                  'x': r.x + (idx % cols) * (peca.w + kerf),
                                  'y': r.y + (idx // cols) * (peca.h + kerf)})
                if not edicao.validar_padrao(itens + bloco, sheet_w, sheet_h, estagios):
                    itens += bloco
                    counts[k] = counts.get(k, 0) + n
                    falta -= n
                    colocou = True
                    break
            if not colocou:
                break
        if inteiro and falta > 0:
            itens, counts = antes     # não coube inteiro: desiste deste tipo
        elif counts.get(k, 0) > antes[1].get(k, 0):
            tipos_livres -= 1

    placed = [PlacedItem(piece_key=i['piece_key'], cod=i['cod'], desc=i['desc'], shelf=0,
                          x=i['x'], y=i['y'], w=i['w'], h=i['h'], rotated=i['rotated'])
              for i in itens]
    return Pattern(counts=counts, items=placed,
                    used_area=sum(i.w * i.h for i in placed) / 1_000_000)


def absorver(pat: Pattern, vezes: int, restante: dict, chaves: list, piece_info: dict,
             sheet_w: int, sheet_h: int, kerf: float, estagios: int,
             prioridade: dict | None = None, inteiro: bool = False,
             max_rondas: int = 8) -> tuple[list, dict]:
    """
    Espalha o que está pendente (`restante`) nas sobras das `vezes` chapas do
    padrão `pat`, ANTES de abrir qualquer chapa nova.

    Cada rodada enche a sobra com o que couber (até MAX_TIPOS tipos no total) e
    repete esse enchimento nas chapas que as peças pendentes alcançam; a chapa
    do padrão se divide em grupos (mesma chapa da família, enchimentos
    diferentes) até as peças acabarem ou não caber mais nada. As que sobram
    ficam sem enchimento.

    Devolve (padroes, pendente_depois): padrões com `repeticoes` somando
    `vezes`, e o pendente já descontado do que foi absorvido (sem descontar a
    própria família, que quem chama já consome).
    """
    pendente = dict(restante)
    base = dict(pat.counts)
    padroes, chapas = [], vezes
    for _ in range(max_rondas):
        if chapas <= 0:
            break
        cheia = preencher_sobras(pat, pendente, set(chaves), 1, piece_info, sheet_w, sheet_h,
                                  kerf, estagios, prioridade, inteiro=inteiro)
        extra = {k: q - base.get(k, 0) for k, q in cheia.counts.items() if q > base.get(k, 0)}
        if not extra:
            break
        grupo = min(chapas, min(pendente[k] // n for k, n in extra.items()))
        cheia.repeticoes = grupo
        padroes.append(cheia)
        for k, n in extra.items():
            pendente[k] -= n * grupo
        chapas -= grupo
    if chapas > 0:
        simples = Pattern(counts=dict(base), items=list(pat.items), used_area=pat.used_area)
        simples.repeticoes = chapas
        padroes.append(simples)
    return padroes, pendente


def _avaliar(piece_info: dict, restante: dict, chaves: list, sheet_w: int, sheet_h: int,
              estrategia: str, kerf: float, estagios: int, prioridade: dict | None = None,
              enchimento: dict | None = None):
    """
    Monta o kit das `chaves` pelo mdc das quantidades pendentes e mede a chapa
    cheia dele JÁ COM o enchimento das sobras. None se a proporção é grande
    demais ou nem 1 kit cabe; senão (aproveitamento, kits_total, kit, c, padrao).

    Medir com o enchimento é o que evita juntar peças só porque somam área:
    uma peça que sozinha já fecha a chapa com as menores não precisa de kit.
    """
    kits_total = reduce(gcd, (restante[k] for k in chaves))
    kit = {k: restante[k] // kits_total for k in chaves}
    if max(kit.values()) > RAZAO_MAX:
        return None
    c, pat = maior_kit_por_chapa(piece_info, kit, kits_total, sheet_w, sheet_h,
                                  estrategia, kerf, estagios)
    if c == 0:
        return None
    vezes = kits_total // c
    cheia = preencher_sobras(pat, restante, set(chaves), 1, piece_info, sheet_w, sheet_h,
                              kerf, estagios, prioridade, **(enchimento or {}))
    extra = {k: q - pat.counts.get(k, 0) for k, q in cheia.counts.items()
             if q > pat.counts.get(k, 0)}
    # fração das chapas da família que o pendente alcança com esse enchimento
    alcance = min(1.0, min(restante[k] // n for k, n in extra.items()) / vezes) if extra else 0.0
    area = pat.used_area + (cheia.used_area - pat.used_area) * alcance
    return area / (sheet_w * sheet_h / 1_000_000), kits_total, kit, c, pat


def formar_familia(ancora: str, restante: dict, piece_info: dict, sheet_w: int,
                    sheet_h: int, estrategia: str, kerf: float, estagios: int,
                    afinidade: dict | None = None, enchimento: dict | None = None):
    """
    Família da âncora (no máximo MAX_TIPOS tipos): parte só dela e vai juntando as outras peças pendentes
    (as com afinidade aprendida primeiro, depois da mais comprida para a mais
    curta) enquanto cada uma aumentar o aproveitamento das chapas da família.

    afinidade: {chave_da_peça: peso} - quanto o histórico diz que ela anda com
    a âncora; muda a ORDEM de tentativa e dá a tolerância de TOLERANCIA_APRENDIDA,
    mas a peça ainda tem que caber certinho (kit fecha numa chapa).
    Devolve (chaves, kits_total, kit) ou None se a âncora não cabe numa chapa.
    """
    afinidade = afinidade or {}
    atual = _avaliar(piece_info, restante, [ancora], sheet_w, sheet_h, estrategia, kerf,
                      estagios, afinidade, enchimento)
    if atual is None:
        return None
    chaves = [ancora]
    candidatas = sorted((k for k, q in restante.items() if q > 0 and k != ancora),
                        key=lambda k: (-afinidade.get(k, 0),
                                       _maior_primeiro(piece_info[k].w, piece_info[k].h,
                                                        piece_info[k].cod)))
    for k in candidatas:
        if len(chaves) >= MAX_TIPOS:
            break
        teste = _avaliar(piece_info, restante, chaves + [k], sheet_w, sheet_h,
                          estrategia, kerf, estagios, afinidade, enchimento)
        minimo = atual[0] - TOLERANCIA_APRENDIDA if afinidade.get(k, 0) > 0 else atual[0] + GANHO_MIN
        if teste is not None and teste[0] > minimo:
            chaves, atual = chaves + [k], teste
    return chaves, atual[1], atual[2]
