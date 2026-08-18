# -*- coding: utf-8 -*-
"""Desenho do layout de corte de uma chapa (PNG na tela, e páginas do PDF)."""
import os
import tempfile

# Em hospedagem o HOME costuma ser somente leitura, e aí o matplotlib
# reclama (ou falha) ao montar o cache de fontes. Apontar pra um diretório
# temporário resolve. Precisa vir ANTES do import do matplotlib.
os.environ.setdefault('MPLCONFIGDIR', os.path.join(tempfile.gettempdir(), 'mpl'))
os.makedirs(os.environ['MPLCONFIGDIR'], exist_ok=True)

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import hashlib


def _color_for(cod: str):
    h = int(hashlib.md5(str(cod).encode()).hexdigest(), 16)
    hue = (h % 360) / 360.0
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(hue, 0.45, 0.92)
    return (r, g, b)


def _campos(item):
    """Aceita tanto o objeto PlacedItem quanto o dicionário do plano salvo."""
    if isinstance(item, dict):
        return (item['cod'], float(item['x']), float(item['y']),
                float(item['w']), float(item['h']), bool(item.get('rotated')))
    return (item.cod, float(item.x), float(item.y),
            float(item.w), float(item.h), bool(item.rotated))


def desenhar_chapa(ax, itens, sheet_w_mm: int, sheet_h_mm: int, veio: bool = False):
    """
    Desenha a chapa e as peças no eixo recebido.

    Separado de render_sheet pra o PDF poder usar o mesmo desenho: a página
    impressa e a imagem da tela precisam ser a mesma coisa, senão o operador
    confere um e corta pelo outro.
    """
    ax.add_patch(patches.Rectangle((0, 0), sheet_w_mm, sheet_h_mm,
                                    facecolor='#f4f1ea', edgecolor='#333', linewidth=2))

    # O veio corre no comprimento da chapa. Desenhar isso não é enfeite: se
    # uma peça foi cadastrada com comprimento e largura trocados no Agrosys,
    # o otimizador não tem como perceber (pra ele são dois números), mas o
    # operador percebe na hora que vê o traço atravessado.
    if veio:
        for y in range(0, int(sheet_h_mm), 60):
            ax.plot([0, sheet_w_mm], [y, y], color='#c9b99a', linewidth=0.4,
                    zorder=0, alpha=0.7)

    for item in itens:
        cod, x, y, w, h, girada = _campos(item)
        rect = patches.Rectangle((x, y), w, h, facecolor=_color_for(cod),
                                  edgecolor='#222', linewidth=0.8)
        ax.add_patch(rect)

        # A fonte acompanha o tamanho da peça em vez de ser fixa: o desenho é
        # lido na máquina, muitas vezes impresso, e código que não se lê não
        # serve pra nada. Peça grande ganha rótulo grande.
        tam = max(7.0, min(min(w, h) / 14.0, 22.0))
        dims = f'{w:.0f} x {h:.0f}'
        rot = '  ⟲' if girada else ''
        if w > 150 and h > 110:
            ax.text(x + w / 2, y + h / 2 - h * 0.10, cod, ha='center', va='center',
                    fontsize=tam, fontweight='bold', color='#111')
            ax.text(x + w / 2, y + h / 2 + h * 0.16, dims + rot, ha='center', va='center',
                    fontsize=max(6.5, tam * 0.62), color='#333')
        elif w > 70 and h > 34:
            # peça pequena: só o código, sem dimensão, pra não poluir
            ax.text(x + w / 2, y + h / 2, cod, ha='center', va='center',
                    fontsize=max(6.5, min(tam, 11.0)), fontweight='bold', color='#111')

    ax.set_xlim(0, sheet_w_mm)
    ax.set_ylim(0, sheet_h_mm)
    ax.invert_yaxis()
    ax.set_aspect('equal')
    return ax


def render_sheet(sheet, sheet_w_mm: int, sheet_h_mm: int, out_path: str, titulo: str = '',
                 veio: bool = False):
    fig, ax = plt.subplots(figsize=(13, 13 * sheet_h_mm / sheet_w_mm))
    desenhar_chapa(ax, sheet.items, sheet_w_mm, sheet_h_mm, veio=veio)

    sub = f'  |  Aproveitamento: {sheet.aproveitamento:.1f}%'
    if getattr(sheet, 'repeticoes', 0):
        sub += f'  |  repetir {sheet.repeticoes}x'
    ax.set_title(titulo + sub, fontsize=11)
    ax.set_xlabel('mm  ——— sentido do veio ———' if veio else 'mm')
    ax.set_ylabel('mm')
    plt.tight_layout()
    plt.savefig(out_path, dpi=130)
    plt.close(fig)
