# -*- coding: utf-8 -*-
"""
Otimização "maiores primeiro, por família": monta o plano de cima para baixo,
da peça mais comprida para a mais curta.

Regra de negócio (pedida pela produção):
  1) a peça de MAIOR comprimento ainda pendente é a âncora; a família dela
     (familias.py: peças de quantidade proporcional) define o padrão, repetido
     até a demanda da família acabar - todas as peças dela terminam juntas;
  2) nos espaços que sobram na chapa entram as peças menores, também da mais
     comprida para a mais curta, sem passar do que cada repetição comporta;
  3) esgotada a família, passa-se para a próxima âncora, até o fim.

Difere do Column Generation (column_generation.py), que olha o lote inteiro e
só minimiza o nº de chapas: ele junta peças de tamanhos variados onde der o
melhor encaixe e acaba espalhando um mesmo código por vários padrões. Aqui o
critério principal é a ORDEM; o aproveitamento vem de preencher bem cada
chapa dentro dessa ordem.
"""
from dataclasses import replace

from optimizer import PieceType, SheetResult, _maior_primeiro, KERF_MM
from column_generation import Pattern
from familias import formar_familia, maior_kit_por_chapa, preencher_sobras

ESTRATEGIAS = ('area', 'width', 'height')
# Enchimento das chapas cheias só com peça PEQUENA (a que nunca fecharia uma
# chapa sozinha): o tipo grande não é consumido aos pedaços e enche chapa dele.
# Custo medido nos lotes 1456+1458: ~15% mais chapas; com False o enchimento é
# livre (900 chapas), mas 6 de 32 tipos se espalham em mais de 2 padrões.
SO_PEQUENAS = True


def _ancora(chaves, piece_info: dict) -> str | None:
    """A peça de maior comprimento entre `chaves` (desempate: área, depois código)."""
    chaves = list(chaves)
    if not chaves:
        return None
    return min(chaves, key=lambda k: _maior_primeiro(piece_info[k].w, piece_info[k].h,
                                                      piece_info[k].cod))


def _montar_uma_vez(piece_info: dict, demanda: dict, sheet_w: int, sheet_h: int,
                     estrategia: str, kerf: float, estagios: int, max_padroes: int = 400,
                     afinidade_fn=None):
    """
    Uma passada completa com uma estratégia de empacotamento, em DUAS fases:

    1) 'cheias': cada tipo (ou família de até MAX_TIPOS tipos) que fecha ao menos
       uma chapa cheia faz só chapas cheias, da peça mais comprida para a mais
       curta, com as menores completando as sobras (no máximo MAX_TIPOS tipos);
    2) 'resto': o que sobrou (resto de cada família, peças pequenas) é reunido
       no fim, no máximo MAX_TIPOS tipos por chapa e cada tipo INTEIRO numa
       chapa só (não entra pedaço).

    Devolve (padroes_na_ordem_de_criacao, sem_lugar) - sem_lugar: tipos que não
    cabem nem sozinhos numa chapa.

    afinidade_fn(ancora) -> {peça: peso}: o que o aprendizado diz que anda com
    a âncora (ordem de teste na família e prioridade no enchimento).
    """
    restante = dict(demanda)
    padroes: list[Pattern] = []
    sem_lugar: dict = {}
    n_familia = 0

    # quantas peças de cada tipo cabem numa chapa só dele
    capacidade = {}
    for k in restante:
        capacidade[k], _ = maior_kit_por_chapa(piece_info, {k: 1}, 10 ** 6, sheet_w, sheet_h,
                                                estrategia, kerf, estagios)
        if capacidade[k] == 0:
            sem_lugar[k] = restante[k]
            restante[k] = 0

    for fase in ('cheias', 'resto'):
        while len(padroes) < max_padroes:
            if fase == 'cheias':
                ancora = _ancora((k for k, q in restante.items() if q > 0 and q >= capacidade[k]),
                                 piece_info)
                enchimento = {'elegiveis': ({k for k, q in restante.items() if 0 < q < capacidade[k]}
                                            if SO_PEQUENAS else None),
                              'inteiro': False}
            else:
                ancora = _ancora((k for k, q in restante.items() if q > 0), piece_info)
                enchimento = {'elegiveis': None, 'inteiro': True}
            if ancora is None:
                break

            aprendido = afinidade_fn(ancora) if afinidade_fn else None
            familia = formar_familia(ancora, restante, piece_info, sheet_w, sheet_h, estrategia,
                                      kerf, estagios, aprendido, enchimento)
            c, pat = 0, None
            if familia is not None:
                chaves, kits, kit = familia
                c, pat = maior_kit_por_chapa(piece_info, kit, kits, sheet_w, sheet_h,
                                              estrategia, kerf, estagios)
            if c == 0:
                # sem isso a mesma âncora voltaria para sempre
                sem_lugar[ancora] = restante[ancora]
                restante[ancora] = 0
                continue

            n_familia += 1
            vezes = kits // c      # na fase 'cheias' o que sobra de kits fica para o resto
            pat = preencher_sobras(pat, restante, set(chaves), vezes, piece_info,
                                    sheet_w, sheet_h, kerf, estagios, aprendido, **enchimento)
            pat.repeticoes = vezes
            pat.familia = n_familia
            for k, q in pat.counts.items():
                restante[k] = max(0, restante[k] - q * vezes)
            padroes.append(pat)

    return padroes, sem_lugar


def optimize_group_sequencial(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                               kerf: float = KERF_MM, estagios: int = 3,
                               combinacoes: dict | None = None) -> tuple:
    """
    Mesmo contrato de optimize_group_cg: devolve (sheets, sobras, usados),
    para plugar direto no pipeline.

    Roda a passada "maiores primeiro" com cada estratégia de empacotamento e
    fica com a que fecha em MENOS chapas (empate: menos padrões) - a ordem é a
    mesma nas três, só muda como cada chapa é preenchida.

    combinacoes: {cod: {cod_parceiro: vezes}} do aprendizado (banco.combinacoes()).

    Toda peça entra travada na orientação do cadastro (pode_girar=False), como
    no Column Generation: o mesmo código só pode aparecer numa posição em todo
    o plano.
    """
    pieces = [replace(p, pode_girar=False) for p in pieces]
    piece_info = {p.key: p for p in pieces}
    demanda = {p.key: p.qty_total for p in pieces}
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)

    def afinidade(ancora: str) -> dict:
        """Peso de cada peça do grupo = quantas vezes o operador a juntou com a âncora."""
        parceiros = (combinacoes or {}).get(piece_info[ancora].cod, {})
        return {k: parceiros[p.cod] for k, p in piece_info.items() if p.cod in parceiros}

    melhor = None
    for estrategia in ESTRATEGIAS:
        padroes, sem_lugar = _montar_uma_vez(piece_info, demanda, sheet_w_mm, sheet_h_mm,
                                              estrategia, kerf, estagios, afinidade_fn=afinidade)
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
