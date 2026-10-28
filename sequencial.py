# -*- coding: utf-8 -*-
"""
Otimização "maiores primeiro": monta o plano de cima para baixo, da peça mais
comprida para a mais curta.

Regra de negócio (pedida pela produção):
  1) a peça de MAIOR comprimento ainda pendente define o padrão: as chapas
     saem cheias dela, e a demanda inteira dela fica junta, em sequência;
  2) nos espaços que sobram na chapa entram as peças menores, também da mais
     comprida para a mais curta;
  3) esgotada a peça, passa-se para a próxima maior, e assim até o fim.

Difere do Column Generation (column_generation.py), que olha o lote inteiro e
só minimiza o nº de chapas: ele junta peças de tamanhos variados onde der o
melhor encaixe e acaba espalhando um mesmo código por vários padrões. Aqui o
critério principal é a ORDEM; o aproveitamento vem de preencher bem cada
chapa dentro dessa ordem.
"""
from dataclasses import replace

import edicao
from optimizer import PieceType, PlacedItem, SheetResult, _maior_primeiro, _cabem, KERF_MM
from column_generation import Pattern, _generate_pattern

ESTRATEGIAS = ('area', 'width', 'height')


def _ancora(restante: dict, piece_info: dict) -> str | None:
    """A peça pendente de maior comprimento (desempate: área, depois código)."""
    pendentes = [k for k, q in restante.items() if q > 0]
    if not pendentes:
        return None
    return min(pendentes, key=lambda k: _maior_primeiro(piece_info[k].w, piece_info[k].h,
                                                         piece_info[k].cod))


def _cortar_excesso(pat: Pattern, chave: str, limite: int) -> Pattern:
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


def _preencher_sobras(pat: Pattern, restante: dict, ancora: str, piece_info: dict,
                       sheet_w: int, sheet_h: int, kerf: float, estagios: int) -> Pattern:
    """
    Completa as sobras da chapa da âncora com as peças menores, da mais
    comprida para a mais curta.

    Cada peça entra em BLOCO (colunas x linhas) dentro da maior sobra onde mais
    cabem, e o bloco só fica se o padrão continuar cortável em guilhotina com o
    nº de estágios da máquina (mesma checagem do editor manual). Sem isso a
    sobra à direita das peças grandes ficava vazia e o aproveitamento caía.
    """
    itens = [{'cod': i.cod, 'desc': i.desc, 'piece_key': i.piece_key, 'shelf': 0,
              'x': i.x, 'y': i.y, 'w': i.w, 'h': i.h, 'rotated': i.rotated} for i in pat.items]
    counts = dict(pat.counts)
    ordem = sorted((k for k, q in restante.items() if q > 0 and k != ancora),
                   key=lambda k: _maior_primeiro(piece_info[k].w, piece_info[k].h, piece_info[k].cod))
    for k in ordem:
        peca = piece_info[k]
        falta = restante[k]
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

    placed = [PlacedItem(piece_key=i['piece_key'], cod=i['cod'], desc=i['desc'], shelf=0,
                          x=i['x'], y=i['y'], w=i['w'], h=i['h'], rotated=i['rotated'])
              for i in itens]
    return Pattern(counts=counts, items=placed,
                    used_area=sum(i.w * i.h for i in placed) / 1_000_000)


def _montar_uma_vez(piece_info: dict, demanda: dict, sheet_w: int, sheet_h: int,
                     estrategia: str, kerf: float, estagios: int, max_padroes: int = 400):
    """
    Uma passada completa "maiores primeiro" com uma estratégia de empacotamento.
    Devolve (padroes_na_ordem_de_criacao, sem_lugar) - sem_lugar: tipos que não
    cabem nem sozinhos numa chapa.
    """
    restante = dict(demanda)
    padroes: list[Pattern] = []
    sem_lugar: dict = {}

    while len(padroes) < max_padroes:
        ancora = _ancora(restante, piece_info)
        if ancora is None:
            break

        # quantas da âncora cabem numa chapa sozinha: é o que ela "pede" por chapa
        sozinha = _generate_pattern(piece_info, {ancora: restante[ancora]}, sheet_w, sheet_h,
                                     strategy=estrategia, kerf=kerf, estagios=estagios)
        por_chapa = sozinha.counts.get(ancora, 0)
        if por_chapa <= 0:
            sem_lugar[ancora] = restante.pop(ancora)
            restante[ancora] = 0
            continue

        # chapa da âncora: ela sozinha, no teto de uma chapa cheia (ou o que
        # restar dela), e as menores completando o que sobrar
        if restante[ancora] < por_chapa:
            sozinha = _cortar_excesso(
                _generate_pattern(piece_info, {ancora: restante[ancora]}, sheet_w, sheet_h,
                                  strategy=estrategia, kerf=kerf, estagios=estagios),
                ancora, restante[ancora])
        pat = _preencher_sobras(sozinha, restante, ancora, piece_info,
                                 sheet_w, sheet_h, kerf, estagios)

        # repete o MESMO padrão enquanto houver demanda para todas as peças dele
        vezes = min(restante[k] // q for k, q in pat.counts.items() if q > 0)
        vezes = max(1, vezes)
        pat.repeticoes = vezes
        for k, q in pat.counts.items():
            restante[k] = max(0, restante[k] - q * vezes)
        padroes.append(pat)

    return padroes, sem_lugar


def optimize_group_sequencial(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                               kerf: float = KERF_MM, estagios: int = 3) -> tuple:
    """
    Mesmo contrato de optimize_group_cg: devolve (sheets, sobras, usados),
    para plugar direto no pipeline.

    Roda a passada "maiores primeiro" com cada estratégia de empacotamento e
    fica com a que fecha em MENOS chapas (empate: menos padrões) - a ordem é a
    mesma nas três, só muda como cada chapa é preenchida.

    Toda peça entra travada na orientação do cadastro (pode_girar=False), como
    no Column Generation: o mesmo código só pode aparecer numa posição em todo
    o plano.
    """
    pieces = [replace(p, pode_girar=False) for p in pieces]
    piece_info = {p.key: p for p in pieces}
    demanda = {p.key: p.qty_total for p in pieces}
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)

    melhor = None
    for estrategia in ESTRATEGIAS:
        padroes, sem_lugar = _montar_uma_vez(piece_info, demanda, sheet_w_mm, sheet_h_mm,
                                              estrategia, kerf, estagios)
        chapas = sum(p.repeticoes for p in padroes)
        chave = (chapas, len(padroes))
        if melhor is None or chave < melhor[0]:
            melhor = (chave, padroes, sem_lugar)
    _, usados, sem_lugar = melhor

    # trava de sanidade: peça fora da borda não existe no mundo físico
    usados = [p for p in usados
              if not any(i.x + i.w > sheet_w_mm + 0.5 or i.y + i.h > sheet_h_mm + 0.5
                         for i in p.items)]

    sheets, idx = [], 1
    for pat in usados:
        pat.used_area = sum((it.w / 1000) * (it.h / 1000) for it in pat.items)
        pat._area_chapa = sheet_area
        for _ in range(pat.repeticoes):
            sheets.append(SheetResult(index=idx, items=pat.items,
                                       used_area=pat.used_area, sheet_area=sheet_area))
            idx += 1

    produzido: dict = {}
    for pat in usados:
        for k, q in pat.counts.items():
            produzido[k] = produzido.get(k, 0) + q * pat.repeticoes
    sobras = [PieceType(key=p.key, cod=p.cod, desc=p.desc, w=p.w, h=p.h,
                         qty_total=p.qty_total - produzido.get(p.key, 0))
              for p in pieces if p.qty_total - produzido.get(p.key, 0) > 0]
    return sheets, sobras, usados
