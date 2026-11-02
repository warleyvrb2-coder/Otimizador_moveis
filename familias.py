# -*- coding: utf-8 -*-
"""
Montagem das chapas de UMA peça só, mais o enchimento das sobras.

Antes existiam kits por proporção de quantidade (ex.: 1.160/928/232 = 5:4:1).
Foram retirados: misturavam a peça com outras desde a primeira chapa e deixavam
a mesma peça espalhada (ex.: 5 do 9202 por chapa em 10 chapas, quando cabem 12
numa chapa só dela). Agora a peça enche chapas só dela, repartida por igual, e
o que sobra de espaço é completado por outros tipos que caibam INTEIROS.
"""
from column_generation import Pattern, _generate_pattern
from optimizer import PlacedItem, _maior_primeiro, _cabem
import edicao

# tipos de peça diferentes numa mesma chapa (a peça + o enchimento). Os planos do
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


def maior_quantidade_por_chapa(piece_info: dict, chave: str, quantidade: int, sheet_w: int,
                               sheet_h: int, estrategia: str, kerf: float,
                               estagios: int) -> tuple[int, Pattern | None]:
    """
    Quantas peças de `chave` cabem numa chapa só dela (no máximo `quantidade`) e o
    padrão com elas. (0, None) se nem uma cabe. O empacotador fecha faixa inteira
    e passa do teto; `cortar_excesso` tira o que passou do pedido.
    """
    pat = _generate_pattern(piece_info, {chave: quantidade}, sheet_w, sheet_h,
                             strategy=estrategia, kerf=kerf, estagios=estagios)
    n = min(pat.counts.get(chave, 0), quantidade)
    return (n, cortar_excesso(pat, chave, n)) if n > 0 else (0, None)


def distribuir_chapas(piece_info: dict, chave: str, quantidade: int, por_chapa: int,
                      sheet_w: int, sheet_h: int, estrategia: str, kerf: float,
                      estagios: int) -> list:
    """
    Reparte `quantidade` por igual em S = ceil(quantidade / por_chapa) chapas:
    nenhuma fica com 1 ou 2 peças sobrando no fim (o resto de uma peça não vira
    chapa à parte, misturada com outras). Gasta as mesmas S chapas que "por_chapa
    em cada uma + uma chapa de resto".

    Devolve [(padrão, nº de chapas)], no máximo 2 padrões (com `base + 1` peças e
    com `base`). Se o empacotador não fechar exatamente algum deles, usa mais
    chapas (S + 1, ...) até fechar.
    """
    chapas = -(-quantidade // por_chapa)
    while chapas <= quantidade:
        base, extra = divmod(quantidade, chapas)
        grupos = []
        for k, n in ((base + 1, extra), (base, chapas - extra)):
            if n:
                n_k, pat = maior_quantidade_por_chapa(piece_info, chave, k, sheet_w, sheet_h,
                                                       estrategia, kerf, estagios)
                if n_k != k:
                    break
                grupos.append((pat, n))
        else:
            return grupos
        chapas += 1
    return []


def preencher_sobras(pat: Pattern, restante: dict, excluir: set, vezes: int,
                       piece_info: dict, sheet_w: int, sheet_h: int, kerf: float,
                       estagios: int, prioridade: dict | None = None) -> Pattern:
    """
    Completa as sobras da chapa com as peças menores, da mais
    comprida para a mais curta, sem passar de MAX_TIPOS tipos na chapa.

    `vezes` é quantas chapas repetem este padrão: cada peça de enchimento entra
    no máximo `pendente // vezes` por chapa, para a repetição nunca passar do
    pedido. `excluir` são as peças que não podem entrar. `prioridade` ({peça: peso})
    põe na frente as que o operador já costuma juntar com a peça da chapa.

    Cada peça entra em BLOCO (colunas x linhas) dentro da maior sobra onde mais
    cabem, e o bloco só fica se o padrão continuar cortável em guilhotina com o
    nº de estágios da máquina (mesma checagem do editor manual). Sem isso a
    sobra à direita das peças grandes ficava vazia e o aproveitamento caía.
    """
    itens = [{'cod': i.cod, 'desc': i.desc, 'piece_key': i.piece_key, 'shelf': 0,
              'x': i.x, 'y': i.y, 'w': i.w, 'h': i.h, 'rotated': i.rotated} for i in pat.items]
    counts = dict(pat.counts)
    tipos_livres = MAX_TIPOS - len(counts)
    ordem = sorted((k for k, q in restante.items() if q > 0 and k not in excluir),
                   key=lambda k: ((-(prioridade or {}).get(k, 0),)
                                  + _maior_primeiro(piece_info[k].w, piece_info[k].h, piece_info[k].cod)))
    for k in ordem:
        if tipos_livres <= 0:
            break
        peca = piece_info[k]
        falta = restante[k] // vezes
        antes = counts.get(k, 0)
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
        if counts.get(k, 0) > antes:
            tipos_livres -= 1

    placed = [PlacedItem(piece_key=i['piece_key'], cod=i['cod'], desc=i['desc'], shelf=0,
                          x=i['x'], y=i['y'], w=i['w'], h=i['h'], rotated=i['rotated'])
              for i in itens]
    return Pattern(counts=counts, items=placed,
                    used_area=sum(i.w * i.h for i in placed) / 1_000_000)


def absorver(pat: Pattern, vezes: int, restante: dict, excluir: set, piece_info: dict,
             sheet_w: int, sheet_h: int, kerf: float, estagios: int,
             prioridade: dict | None = None, max_rondas: int = 8) -> tuple[list, dict]:
    """
    Espalha o que está pendente (`restante`) nas sobras das `vezes` chapas do
    padrão `pat`, ANTES de abrir qualquer chapa nova.

    Cada rodada enche a sobra com o que couber (até MAX_TIPOS tipos no total) e
    repete esse enchimento nas chapas que as peças pendentes alcançam; a chapa
    do padrão se divide em grupos (mesma chapa da peça, enchimentos
    diferentes) até as peças acabarem ou não caber mais nada. As que sobram
    ficam sem enchimento.

    `excluir` são os tipos que não podem entrar (a própria peça e os que
    não couberam inteiros). Devolve (padroes, pendente_depois): padrões com
    `repeticoes` somando `vezes`, e o pendente já descontado do que foi
    absorvido (sem descontar a própria família, que quem chama já consome).
    """
    pendente = dict(restante)
    base = dict(pat.counts)
    padroes, chapas = [], vezes
    for _ in range(max_rondas):
        if chapas <= 0:
            break
        cheia = preencher_sobras(pat, pendente, excluir, 1, piece_info, sheet_w, sheet_h,
                                  kerf, estagios, prioridade)
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
