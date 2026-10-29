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
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

from optimizer import PieceType, SheetResult, _maior_primeiro, KERF_MM
from column_generation import Pattern
from familias import formar_familia, maior_kit_por_chapa, absorver

ESTRATEGIAS = ('area', 'width', 'height')
# rodadas de refinamento: a 1ª é o plano base, as seguintes testam girar peças
RODADAS = 3


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
            ancora = _ancora((k for k, q in restante.items() if q > 0 and
                              (fase == 'resto' or q >= capacidade[k])), piece_info)
            enchimento = {'inteiro': fase == 'resto'}
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
            for k, q in pat.counts.items():
                restante[k] = max(0, restante[k] - q * vezes)
            # as sobras dessas chapas recebem o pendente ANTES de abrir chapa nova
            grupos, restante = absorver(pat, vezes, restante, chaves, piece_info, sheet_w,
                                         sheet_h, kerf, estagios, aprendido, **enchimento)
            for g in grupos:
                g.familia = n_familia
                padroes.append(g)

    return padroes, sem_lugar


def _afinidade(pieces: list, combinacoes: dict | None):
    """
    Função afinidade(âncora) -> {peça: peso}: quantas vezes o operador juntou cada
    peça do grupo com a âncora (só o código importa, então vale para qualquer giro).
    """
    info = {p.key: p for p in pieces}

    def afinidade(ancora: str) -> dict:
        parceiros = (combinacoes or {}).get(info[ancora].cod, {})
        return {k: parceiros[p.cod] for k, p in info.items() if p.cod in parceiros}
    return afinidade


def _melhor_passada(pieces: list, sheet_w: int, sheet_h: int, kerf: float, estagios: int,
                     estrategias: tuple, combinacoes: dict | None) -> tuple:
    """
    Roda a passada com cada estratégia e devolve a de MENOS chapas (empate: menos
    padrões) como ((chapas, padrões), padrões_do_plano, sem_lugar).
    """
    piece_info = {p.key: p for p in pieces}
    demanda = {p.key: p.qty_total for p in pieces}
    afinidade = _afinidade(pieces, combinacoes)
    melhor = None
    for estrategia in estrategias:
        padroes, sem_lugar = _montar_uma_vez(piece_info, demanda, sheet_w, sheet_h, estrategia,
                                              kerf, estagios, afinidade_fn=afinidade)
        chave = (sum(p.repeticoes for p in padroes), len(padroes))
        if melhor is None or chave < melhor[0]:
            melhor = (chave, padroes, sem_lugar)
    return melhor


def _com_giro(base: list, girar) -> list:
    """As peças do grupo com largura e altura trocadas nas que giram."""
    return [replace(p, w=p.h, h=p.w) if p.key in girar else p for p in base]


def _testar_giro(tarefa: tuple) -> tuple:
    """Chave (chapas, padrões) do plano com o giro dado; função solta para rodar em outro processo."""
    base, girar, sheet_w, sheet_h, kerf, estagios, combinacoes = tarefa
    return _melhor_passada(_com_giro(base, girar), sheet_w, sheet_h, kerf, estagios,
                           ('area',), combinacoes)[0]


# Processos que testam giros ao mesmo tempo (1 = sem paralelismo). O teto de 8 é
# porque cada teste é pesado em memória; mais que isso quase não acelera.
PROCESSOS = int(os.environ.get('PROCESSOS', min(8, os.cpu_count() or 1)))
_pool = None


def _mapear(funcao, tarefas: list) -> list:
    """
    Aplica `funcao` a cada tarefa, em processos paralelos quando PROCESSOS > 1.
    Se o pool falhar (ambiente sem multiprocessamento, memória), refaz em série:
    o resultado é o mesmo, só mais lento.
    """
    global _pool
    if PROCESSOS > 1 and len(tarefas) > 1:
        try:
            if _pool is None:
                _pool = ProcessPoolExecutor(max_workers=PROCESSOS)
            return list(_pool.map(funcao, tarefas))
        except Exception:
            _pool = None
    return [funcao(t) for t in tarefas]


def _buscar_giro(base: list, candidatas: list, sheet_w: int, sheet_h: int, kerf: float,
                 estagios: int, combinacoes: dict | None) -> frozenset:
    """
    Rodadas 2 e 3: em cada rodada testa girar, uma a uma, cada peça candidata (em
    paralelo) e adota o melhor giro isolado ou a união de todos os que ajudaram,
    o que fechar em menos chapas. Para quando nenhum giro ajuda.
    """
    girar = frozenset()
    atual = _testar_giro((base, girar, sheet_w, sheet_h, kerf, estagios, combinacoes))
    for _ in range(RODADAS - 1):
        testes = [girar ^ {k} for k in candidatas]
        chaves = _mapear(_testar_giro, [(base, t, sheet_w, sheet_h, kerf, estagios, combinacoes)
                                         for t in testes])
        bons = [(c, t) for c, t in zip(chaves, testes) if c < atual]
        if not bons:
            break
        melhor_c, melhor_t = min(bons, key=lambda x: x[0])
        if len(bons) > 1:
            uniao = girar
            for _, t in bons:
                uniao = uniao ^ (t ^ girar)      # aplica cada giro que ajudou
            c_uniao = _testar_giro((base, uniao, sheet_w, sheet_h, kerf, estagios, combinacoes))
            if c_uniao < melhor_c:
                melhor_c, melhor_t = c_uniao, uniao
        girar, atual = melhor_t, melhor_c
    return girar


def optimize_group_sequencial(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                               kerf: float = KERF_MM, estagios: int = 3,
                               combinacoes: dict | None = None) -> tuple:
    """
    Mesmo contrato de optimize_group_cg: devolve (sheets, sobras, usados),
    para plugar direto no pipeline.

    Em até RODADAS rodadas: a 1ª monta o plano com todas as peças na orientação
    do cadastro; a 2ª e a 3ª testam GIRAR, uma a uma, cada peça que o cadastro
    libera (`pode_girar`) e ficam com o giro que reduz as chapas. O giro vale
    para o plano INTEIRO: a peça decidida gira em todos os padrões, nunca em
    parte deles. A busca usa só a estratégia 'area' (1/3 do custo); o plano
    final roda as 3 estratégias e fica com a de menos chapas (empate: menos
    padrões).

    combinacoes: {cod: {cod_parceiro: vezes}} do aprendizado (banco.combinacoes()).
    """
    originais = pieces
    base = [replace(p, pode_girar=False) for p in pieces]
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)

    candidatas = [p.key for p in originais if p.pode_girar and p.w != p.h]
    girar = (_buscar_giro(base, candidatas, sheet_w_mm, sheet_h_mm, kerf, estagios, combinacoes)
             if candidatas else frozenset())

    _, usados, sem_lugar = _melhor_passada(_com_giro(base, girar), sheet_w_mm, sheet_h_mm,
                                           kerf, estagios, ESTRATEGIAS, combinacoes)
    for pat in usados:
        for it in pat.items:
            if it.piece_key in girar:
                it.rotated = True

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
              for p in originais if p.qty_total - produzido.get(p.key, 0) > 0]
    return sheets, sobras, usados
