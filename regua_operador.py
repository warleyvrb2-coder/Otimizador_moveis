# -*- coding: utf-8 -*-
"""
Régua oficial do otimizador: compara os motores com os planos que o operador
montou à mão e validou (PDFs em referencia_operador/).

Para cada PDF lê as peças do catálogo (código, medida, total) e roda os motores
com essa demanda. O operador usa 1 padrão por plano; o número de chapas dele é
a referência. Uso:  python regua_operador.py [pasta_dos_pdfs]
"""
import glob
import os
import re
import sys

import pdfplumber

from optimizer import PieceType
from sequencial import optimize_group_sequencial
from column_generation import optimize_group_cg

CHAPA_W, CHAPA_H, KERF, ESTAGIOS = 2750, 1860, 4.4, 3


def ler_plano(caminho: str) -> tuple[int, list[PieceType]]:
    """Total de chapas do operador e as peças de catálogo (as avulsas ficam fora)."""
    with pdfplumber.open(caminho) as pdf:
        texto = '\n'.join(p.extract_text() or '' for p in pdf.pages)
    chapas = int(re.search(r'Total de chapas (\d+)', texto).group(1))
    tabela = texto.split('PEÇA MEDIDA CHAPA TOTAL')[1].split('SEQUÊNCIA')[0]
    pecas = []
    for linha in tabela.splitlines():
        m = re.match(r'^(\d{4,6})(?!\d*x)\D.*?(\d+)x(\d+)\s+(\d+)\s+(\d+)\s*$', linha)
        if m:
            cod = m[1]
            pecas.append(PieceType(key=cod, cod=cod, desc=cod, w=int(m[2]), h=int(m[3]),
                                    qty_total=int(m[5]), pode_girar=False))
    return chapas, pecas


def main(pasta: str) -> None:
    """Imprime, por plano, as chapas do operador e as de cada motor."""
    for caminho in sorted(glob.glob(os.path.join(pasta, 'plano-corte-*.pdf'))):
        chapas, pecas = ler_plano(caminho)
        if not pecas:
            continue
        copia = lambda: [PieceType(**p.__dict__) for p in pecas]
        seq, _, pad_seq = optimize_group_sequencial(copia(), CHAPA_W, CHAPA_H, KERF, ESTAGIOS)
        cg, _, pad_cg = optimize_group_cg(copia(), CHAPA_W, CHAPA_H, time_budget_s=8,
                                           kerf=KERF, estagios=ESTAGIOS)
        print(f'{os.path.basename(caminho)[12:22]} operador={chapas:4d} '
              f'sequencial={len(seq):4d} ({len(pad_seq)} pad) CG={len(cg):4d} ({len(pad_cg)} pad)')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'referencia_operador')
