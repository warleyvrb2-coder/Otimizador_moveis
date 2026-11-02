# -*- coding: utf-8 -*-
"""
Aprendizado de combinações: quais peças o operador põe juntas no mesmo padrão.

Duas fontes, as duas já validadas por quem corta:
  1) os planos de referência do operador (referencia_operador/*.pdf), lidos uma
     única vez - é a "semente" que faz o sistema já nascer sabendo;
  2) todo plano aprovado pelo PCP, a partir do momento da aprovação.

O que sai daqui só muda a PRIORIDADE no enchimento das sobras (familias.py);
nunca coloca peça fora do pedido.
"""
import glob
import os
from itertools import combinations

import banco

PASTA_REFERENCIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referencia_operador')
# peça automática (tira avulsa, sugestão) não é escolha do operador: não ensina nada
_NAO_ENSINA = ('_tira', '_sugerido', '_avulsa')


def pares_do_padrao(codigos) -> list[tuple[str, str]]:
    """Todos os pares distintos entre os códigos de um padrão."""
    return list(combinations(sorted(set(codigos)), 2))


def aprender_resultado(plano_id: str, resultado: dict) -> int:
    """
    Registra as combinações de cada padrão do plano aprovado, uma única vez por
    plano. Devolve quantos pares foram somados. Peça sem código de catálogo
    (avulsa digitada) é ignorada.
    """
    if not banco.marcar_uma_vez(f'plano:{plano_id}'):
        return 0
    pares = []
    for g in resultado.get('grupos', []):
        for pad in g.get('padroes', []):
            cods = [it['cod'] for it in pad.get('itens', [])
                    if not str(it.get('piece_key', '')).endswith(_NAO_ENSINA)]
            pares += pares_do_padrao(cods)
    banco.registrar_combinacoes(pares)
    return len(pares)


def semear_do_operador(pasta: str = PASTA_REFERENCIA) -> int:
    """
    Lê os planos de referência uma única vez e soma as combinações deles.
    Chamada em todo cálculo; depois da primeira vez não faz nada.
    """
    if not banco.marcar_uma_vez('semente'):
        return 0
    # import aqui: regua_operador puxa pdfplumber e o motor, e só a semente precisa disso
    from regua_operador import ler_plano
    pares = []
    for caminho in sorted(glob.glob(os.path.join(pasta, 'plano-corte-*.pdf'))):
        try:
            _, pecas = ler_plano(caminho)
        except (AttributeError, IndexError, ValueError):
            continue  # PDF fora do formato: pula, não derruba o cálculo
        pares += pares_do_padrao(p.cod for p in pecas)
    banco.registrar_combinacoes(pares)
    return len(pares)
