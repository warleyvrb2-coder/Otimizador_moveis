# -*- coding: utf-8 -*-
"""Conferência rápida do editor: python teste_editor.py (sem framework, só asserts)."""
import edicao

KERF = 4.4


def teste_arraste_encaixa_alinhado() -> None:
    """Peça solta encostada na vizinha (sem folga da serra) vai para o canto da sobra."""
    a = {'cod': 'A', 'x': 0.0, 'y': 0.0, 'w': 400.0, 'h': 600.0}
    x, y = edicao.alinhar_ao_retalho([a], 300, 200, 400.0, 300.0, 1000, 600, KERF)
    assert abs(x - (400 + KERF)) < 0.6, x      # 1 kerf depois da vizinha, não colado nela
    assert abs(y - 0) < 0.6 or abs(y - 400) < 0.6, y


def teste_arraste_longe_fica_onde_caiu() -> None:
    """Fora de qualquer sobra em que a peça caiba, a posição não muda."""
    a = {'cod': 'A', 'x': 0.0, 'y': 0.0, 'w': 1000.0, 'h': 590.0}
    assert edicao.alinhar_ao_retalho([a], 300, 200, 100.0, 100.0, 1000, 600, KERF) == (100.0, 100.0)


if __name__ == '__main__':
    teste_arraste_encaixa_alinhado()
    teste_arraste_longe_fica_onde_caiu()
    print('ok')
