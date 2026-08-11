# -*- coding: utf-8 -*-
"""
Parser dos PDFs "Kambam" (relatório de corte do Agrosys).
Extrai a tabela "PEÇAS DA PRODUÇÃO": código, descrição, comprimento,
largura, espessura, cor e quantidade (Total) de cada peça.

Trabalha com as COORDENADAS das palavras (pdfplumber), não com o texto
remontado. Isso importa porque o relatório quebra células no meio: a cor
"CINAMO FF 2" sai como "CINAMO FF" numa linha e "2" na linha de baixo, e
a descrição da peça continua na linha seguinte. Lendo por coordenada, o
"2" órfão é reconhecido pela posição horizontal como continuação da
coluna Cor - coisa impossível de fazer no texto linearizado, onde ele
vira um número solto indistinguível de qualquer outro.
"""
import hashlib
import re
import os
from collections import defaultdict

import pdfplumber

# "8930-LATERAL ESQUERDA N°01 ROUPEIRO" - código + hífen abre uma linha de peça.
# As linhas de "ITENS DA PRODUÇÃO" (móvel acabado) usam "8750  ROUPEIRO ..."
# com espaço em vez de hífen, então esse padrão já separa as duas tabelas.
COD_RE = re.compile(r'^(?P<cod>\d{3,6})-(?P<resto>.*)$')

# número decimal no formato brasileiro: 2342,00 / 1.740,00
NUM_RE = re.compile(r'^[\d.]+,\d{2}$')

MATERIAL_RE = re.compile(r'MATERIAL:\s*(?P<cod>\d+)\s*-\s*(?P<desc>.+?)\s*$')
LOTE_RE = re.compile(r'LOTE:\s*(?P<lote>\S+)')

# tolerância vertical pra considerar que duas palavras estão na mesma linha
Y_TOL = 3.0
# tolerância horizontal pra casar a continuação de uma célula com a coluna dela
X_TOL = 6.0
# distância vertical máxima entre uma linha e a continuação dela. As linhas
# do relatório ficam a ~10pt uma da outra; o rodapé ("Volume Cúbico ... Tempo
# total do setor") cai bem longe, e sem esse limite ele gruda na cor da
# última peça da página.
GAP_MAX = 25.0


def _to_float(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.'))


def file_hash(path: str) -> str:
    """SHA-1 do arquivo, pra detectar o mesmo PDF enviado duas vezes."""
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for bloco in iter(lambda: f.read(65536), b''):
            h.update(bloco)
    return h.hexdigest()


def _linhas_da_pagina(page) -> list[list[dict]]:
    """Agrupa as palavras da página em linhas por posição vertical."""
    palavras = page.extract_words()
    linhas = defaultdict(list)
    for w in palavras:
        chave = round(w['top'] / Y_TOL)
        linhas[chave].append(w)
    return [sorted(linhas[k], key=lambda w: w['x0']) for k in sorted(linhas)]


def _parse_linha_peca(linha: list[dict]) -> dict | None:
    """
    Lê a linha PRINCIPAL de uma peça (a que começa com "código-").
    Retorna também as fronteiras horizontais das colunas Cor e Total, que
    são usadas depois pra encaixar as continuações das linhas seguintes.
    """
    m = COD_RE.match(linha[0]['text'])
    if not m:
        return None

    # as três medidas são os três primeiros decimais da linha, em ordem
    nums = [(i, w) for i, w in enumerate(linha) if NUM_RE.match(w['text'])]
    if len(nums) < 4:  # comp, larg, esp e total
        return None
    (i_comp, w_comp), (i_larg, w_larg), (i_esp, w_esp) = nums[0], nums[1], nums[2]

    # descrição: o que vem antes do comprimento
    desc = m.group('resto') + ' ' + ' '.join(w['text'] for w in linha[1:i_comp])

    # depois da espessura vem a cor (palavras não-numéricas) e então o Total
    # (primeiro decimal seguinte). A cor pode conter dígitos soltos - é o
    # caso de "CAST FF 2" - por isso o corte é pelo FORMATO decimal, não
    # por "ser número".
    cor_palavras, w_total = [], None
    for w in linha[i_esp + 1:]:
        if NUM_RE.match(w['text']):
            w_total = w
            break
        cor_palavras.append(w)
    if w_total is None or not cor_palavras:
        return None

    return {
        'cod': m.group('cod'),
        'desc': desc.strip(),
        'comp_mm': _to_float(w_comp['text']),
        'larg_mm': _to_float(w_larg['text']),
        'esp_mm': _to_float(w_esp['text']),
        'cor': ' '.join(w['text'] for w in cor_palavras),
        'qtd': _to_float(w_total['text']),
        '_cor_x0': cor_palavras[0]['x0'],
        '_total_x0': w_total['x0'],
        '_desc_x0': linha[0]['x0'],
    }


def _aplicar_continuacao(peca: dict, linha: list[dict]) -> None:
    """
    Encaixa uma linha de continuação na peça anterior, usando a coluna em
    que cada palavra caiu. É isso que recupera o "2" de "CINAMO FF 2" e o
    "W/ARENA" de "OFF W/ARENA".
    """
    cor_ini = peca['_cor_x0'] - X_TOL
    cor_fim = peca['_total_x0'] - X_TOL
    desc_fim = peca['_desc_x0'] + 40  # continuação da descrição fica à esquerda

    for w in linha:
        if cor_ini <= w['x0'] < cor_fim:
            peca['cor'] += ' ' + w['text']
        elif w['x0'] < desc_fim:
            peca['desc'] += ' ' + w['text']


def parse_kambam(pdf_path: str, source_name: str | None = None) -> list[dict]:
    """
    Retorna uma lista de peças:
    {cod, desc, comp_mm, larg_mm, esp_mm, cor, qtd, material, origem, lote}
    """
    pecas: list[dict] = []
    lote = None
    material = None
    atual: dict | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            atual = None  # peça não continua de uma página pra outra
            ultimo_top = 0.0
            for linha in _linhas_da_pagina(page):
                texto = ' '.join(w['text'] for w in linha)
                topo = min(w['top'] for w in linha)

                if lote is None:
                    m = LOTE_RE.search(texto)
                    if m:
                        lote = m.group('lote')
                m = MATERIAL_RE.search(texto)
                if m:
                    material = f"{m.group('cod')} - {m.group('desc')}"
                    atual = None
                    continue

                nova = _parse_linha_peca(linha)
                if nova is not None:
                    nova['material'] = material
                    pecas.append(nova)
                    atual = nova
                    ultimo_top = topo
                elif atual is not None:
                    # Linha sem código: continuação da peça anterior, desde
                    # que esteja logo abaixo dela. Cabeçalho repetido de
                    # página e rodapé de totais ficam longe e são cortados
                    # pelo GAP_MAX - o resto pega pelo texto.
                    if topo - ultimo_top > GAP_MAX or re.search(
                            r'Página:|PEÇAS DA PRODUÇÃO|Cod\./Desc\.|Usuário:|Volume Cúbico', texto):
                        atual = None
                    else:
                        _aplicar_continuacao(atual, linha)
                        ultimo_top = topo

    for p in pecas:
        p['cor'] = re.sub(r'\s+', ' ', p['cor']).strip()
        p['desc'] = re.sub(r'\s+', ' ', p['desc']).strip()
        p['lote'] = lote or (source_name or 'desconhecido')
        p['origem'] = source_name or os.path.basename(pdf_path)
        for k in ('_cor_x0', '_total_x0', '_desc_x0'):
            p.pop(k, None)

    return pecas


if __name__ == '__main__':
    import sys
    import json
    result = parse_kambam(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nTotal de linhas de peça: {len(result)}")
    print(f"Soma das quantidades: {sum(p['qtd'] for p in result):.2f}")
