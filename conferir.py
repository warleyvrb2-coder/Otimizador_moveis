# -*- coding: utf-8 -*-
"""
Confere se o parser leu o PDF inteiro.

O relatório Kambam imprime no rodapé o campo "Volume Cúbico", que é o
volume total das peças do lote. Somando comprimento x largura x espessura
x quantidade de tudo que o parser extraiu, o resultado tem que bater com
esse número. Se não bater, o parser está perdendo (ou duplicando) peça —
e é melhor descobrir isso aqui do que depois de cortar a chapa.

Uso:
    python conferir.py "caminho\\Kambam.pdf" [outro.pdf ...]
"""
import re
import sys
import os
from collections import Counter

import pdfplumber

from parser import parse_kambam, file_hash

VOLUME_RE = re.compile(r'Volume\s+C[úu]bico:\s*([\d.]+,\d+)')

# tolerância: o PDF imprime o volume com 2 casas, então a diferença de
# arredondamento sozinha já dá alguns centésimos de por cento.
TOLERANCIA_PCT = 0.5


def volume_declarado(pdf_path: str) -> float | None:
    with pdfplumber.open(pdf_path) as pdf:
        texto = '\n'.join((p.extract_text() or '') for p in pdf.pages)
    m = VOLUME_RE.search(texto)
    if not m:
        return None
    return float(m.group(1).replace('.', '').replace(',', '.'))


def conferir(pdf_path: str) -> bool:
    nome = os.path.basename(pdf_path)
    pecas = parse_kambam(pdf_path, source_name=nome)
    lido = sum(p['comp_mm'] * p['larg_mm'] * p['esp_mm'] * p['qtd'] for p in pecas) / 1e9
    declarado = volume_declarado(pdf_path)

    print(f'\n{nome}')
    print(f'  lote {pecas[0]["lote"] if pecas else "?"} · '
          f'{pecas[0].get("material") or "material não identificado" if pecas else ""}')
    print(f'  {len(pecas)} linhas de peça · {sum(p["qtd"] for p in pecas):.0f} peças')
    for cor, n in sorted(Counter(p['cor'] for p in pecas).items()):
        print(f'      {cor:20s} {n:3d} linhas')

    if declarado is None:
        print('  ?? não achei "Volume Cúbico" no PDF — sem como conferir')
        return True

    erro = 100 * (lido - declarado) / declarado if declarado else 0
    ok = abs(erro) <= TOLERANCIA_PCT
    print(f'  volume declarado no PDF : {declarado:8.2f} m3')
    print(f'  volume das peças lidas  : {lido:8.2f} m3   ({erro:+.2f}%)')
    print(f'  {"OK - leitura completa" if ok else "FALHOU - o parser está perdendo peça"}')
    return ok


def main(caminhos: list[str]) -> int:
    if not caminhos:
        print(__doc__)
        return 2

    hashes = {}
    falhas = 0
    for caminho in caminhos:
        h = file_hash(caminho)
        if h in hashes:
            print(f'\n{os.path.basename(caminho)}')
            print(f'  !! idêntico a "{hashes[h]}" — seria contado em dobro')
            continue
        hashes[h] = os.path.basename(caminho)
        if not conferir(caminho):
            falhas += 1

    print(f'\n{len(hashes)} arquivo(s) conferido(s), {falhas} com divergência')
    return 1 if falhas else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
