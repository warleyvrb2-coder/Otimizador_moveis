# -*- coding: utf-8 -*-
"""
Régua oficial do otimizador: compara os motores com os planos que o operador
montou à mão e validou (PDFs em referencia_operador/).

Para cada PDF lê as peças do catálogo (código, medida, total) e roda o motor
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


def conferir(pecas: list[PieceType], padroes: list, sobras: list) -> list[str]:
    """Problemas do plano contra o pedido: peça a mais, peça a menos, peça não produzida."""
    produzido = {}
    for pat in padroes:
        for k, q in pat.counts.items():
            produzido[k] = produzido.get(k, 0) + q * pat.repeticoes
    erros = [f'{p.cod}: pedido {p.qty_total}, produzido {produzido.get(p.key, 0)}'
             for p in pecas if produzido.get(p.key, 0) != p.qty_total]
    return erros + [f'{s.cod}: sobrou {s.qty_total} sem lugar' for s in sobras]


def mistura(pasta: str) -> bool:
    """
    Junta TODOS os planos do operador num pedido só e deixa o motor descobrir
    as famílias sozinho. Passa se fecha o pedido exato e gasta no máximo 2% de
    chapas a mais que a soma do operador (o código repetido entre planos faz o
    motor reagrupar de outro jeito).
    """
    chapas_operador, juntas = 0, {}
    for caminho in sorted(glob.glob(os.path.join(pasta, 'plano-corte-*.pdf'))):
        chapas, pecas = ler_plano(caminho)
        if not pecas:
            continue  # plano só de peça avulsa: não tem o que comparar
        chapas_operador += chapas
        for p in pecas:
            chave = f'{p.cod}_{p.w}x{p.h}'
            if chave in juntas:
                juntas[chave].qty_total += p.qty_total
            else:
                juntas[chave] = PieceType(key=chave, cod=p.cod, desc=p.cod, w=p.w, h=p.h,
                                           qty_total=p.qty_total, pode_girar=False)
    pecas = list(juntas.values())
    chapas_motor, sobras, padroes = optimize_group_sequencial(
        [PieceType(**p.__dict__) for p in pecas], CHAPA_W, CHAPA_H, KERF, ESTAGIOS)
    erros = conferir(pecas, padroes, sobras)
    ok = chapas_motor and len(chapas_motor) <= chapas_operador * 1.02 and not erros
    print(f'MISTURA operador={chapas_operador} motor={len(chapas_motor)} ({len(padroes)} pad) '
          f'{"OK" if ok else "REPROVADO"} {"; ".join(erros)}')
    return bool(ok)


def main(pasta: str) -> int:
    """
    Imprime, por plano, as chapas do operador e as do motor, e devolve quantos
    planos reprovaram (mais chapas que o operador, ou pedido não fechado).
    """
    reprovados = 0
    for caminho in sorted(glob.glob(os.path.join(pasta, 'plano-corte-*.pdf'))):
        chapas, pecas = ler_plano(caminho)
        if not pecas:
            continue
        copia = [PieceType(**p.__dict__) for p in pecas]
        seq, sobras, padroes = optimize_group_sequencial(copia, CHAPA_W, CHAPA_H, KERF, ESTAGIOS)
        erros = conferir(pecas, padroes, sobras)
        ok = len(seq) <= chapas and not erros
        reprovados += not ok
        print(f'{os.path.basename(caminho)[12:22]} operador={chapas:4d} '
              f'motor={len(seq):4d} ({len(padroes)} pad) {"OK" if ok else "REPROVADO"} '
              f'{"; ".join(erros)}')
    reprovados += not mistura(pasta)
    print(f'reprovados: {reprovados}')
    return reprovados


if __name__ == '__main__':
    sys.exit(1 if main(sys.argv[1] if len(sys.argv) > 1 else 'referencia_operador') else 0)
