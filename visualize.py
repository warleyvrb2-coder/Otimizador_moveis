# -*- coding: utf-8 -*-
"""Gera uma imagem PNG do layout de corte de uma chapa."""
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
    h = int(hashlib.md5(cod.encode()).hexdigest(), 16)
    hue = (h % 360) / 360.0
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(hue, 0.45, 0.92)
    return (r, g, b)


def render_sheet(sheet, sheet_w_mm: int, sheet_h_mm: int, out_path: str, titulo: str = '',
                 veio: bool = False):
    fig, ax = plt.subplots(figsize=(13, 13 * sheet_h_mm / sheet_w_mm))

    ax.add_patch(patches.Rectangle((0, 0), sheet_w_mm, sheet_h_mm,
                                    facecolor='#f4f1ea', edgecolor='#333', linewidth=2))

    # O veio corre no comprimento da chapa. Desenhar isso não é enfeite: se
    # uma peça foi cadastrada com comprimento e largura trocados no Agrosys,
    # o otimizador não tem como perceber (pra ele são dois números), mas o
    # operador percebe na hora que vê o traço atravessado.
    if veio:
        for y in range(0, sheet_h_mm, 60):
            ax.plot([0, sheet_w_mm], [y, y], color='#c9b99a', linewidth=0.4,
                    zorder=0, alpha=0.7)

    seen_cods = {}
    for it in sheet.items:
        color = _color_for(it.cod)
        seen_cods.setdefault(it.cod, color)
        rect = patches.Rectangle((it.x, it.y), it.w, it.h,
                                  facecolor=color, edgecolor='#222', linewidth=0.8)
        ax.add_patch(rect)
        # A fonte acompanha o tamanho da peça em vez de ser fixa: o desenho é
        # lido na máquina, muitas vezes impresso, e código que não se lê não
        # serve pra nada. Peça grande ganha rótulo grande.
        escala = min(it.w, it.h) / 14.0
        tam = max(7.0, min(escala, 22.0))
        dims = f'{it.w:.0f} x {it.h:.0f}'
        rot = '  ⟲' if it.rotated else ''
        if it.w > 150 and it.h > 85:
            ax.text(it.x + it.w / 2, it.y + it.h / 2 - it.h * 0.10, it.cod,
                    ha='center', va='center', fontsize=tam, fontweight='bold', color='#111')
            ax.text(it.x + it.w / 2, it.y + it.h / 2 + it.h * 0.16, dims + rot,
                    ha='center', va='center', fontsize=max(6.5, tam * 0.62), color='#333')
        elif it.w > 70 and it.h > 34:
            # peça pequena: só o código, sem dimensão, pra não poluir
            ax.text(it.x + it.w / 2, it.y + it.h / 2, it.cod,
                    ha='center', va='center', fontsize=max(6.5, min(tam, 11.0)),
                    fontweight='bold', color='#111')

    ax.set_xlim(0, sheet_w_mm)
    ax.set_ylim(0, sheet_h_mm)
    ax.invert_yaxis()
    ax.set_aspect('equal')
    sub = f'  |  Aproveitamento: {sheet.aproveitamento:.1f}%'
    if getattr(sheet, 'repeticoes', 0):
        sub += f'  |  repetir {sheet.repeticoes}x'
    ax.set_title(titulo + sub, fontsize=11)
    ax.set_xlabel('mm  ——— sentido do veio ———' if veio else 'mm')
    ax.set_ylabel('mm')
    plt.tight_layout()
    plt.savefig(out_path, dpi=130)
    plt.close(fig)
