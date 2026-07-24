# -*- coding: utf-8 -*-
"""Gera uma imagem PNG do layout de corte de uma chapa."""
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


def render_sheet(sheet, sheet_w_mm: int, sheet_h_mm: int, out_path: str, titulo: str = ''):
    fig, ax = plt.subplots(figsize=(11, 11 * sheet_h_mm / sheet_w_mm))

    ax.add_patch(patches.Rectangle((0, 0), sheet_w_mm, sheet_h_mm,
                                    facecolor='#f4f1ea', edgecolor='#333', linewidth=2))

    seen_cods = {}
    for it in sheet.items:
        color = _color_for(it.cod)
        seen_cods.setdefault(it.cod, color)
        rect = patches.Rectangle((it.x, it.y), it.w, it.h,
                                  facecolor=color, edgecolor='#222', linewidth=0.8)
        ax.add_patch(rect)
        dims = f'{it.w:.0f}x{it.h:.0f}'
        label = f'{it.cod}\n{dims}' + (' (R)' if it.rotated else '')
        if it.w > 160 and it.h > 90:
            ax.text(it.x + it.w / 2, it.y + it.h / 2, label,
                    ha='center', va='center', fontsize=6.5, color='#111')
        elif it.w > 80 and it.h > 40:
            # peça pequena: só o código, sem dimensão, pra não poluir
            ax.text(it.x + it.w / 2, it.y + it.h / 2, it.cod,
                    ha='center', va='center', fontsize=5.5, color='#111')

    ax.set_xlim(0, sheet_w_mm)
    ax.set_ylim(0, sheet_h_mm)
    ax.invert_yaxis()
    ax.set_aspect('equal')
    ax.set_title(f'{titulo}  |  Aproveitamento: {sheet.aproveitamento:.1f}%', fontsize=11)
    ax.set_xlabel('mm')
    ax.set_ylabel('mm')
    plt.tight_layout()
    plt.savefig(out_path, dpi=130)
    plt.close(fig)
