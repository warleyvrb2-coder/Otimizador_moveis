# -*- coding: utf-8 -*-
"""
Gera o PDF do plano de corte, para levar impresso até a máquina.

Feito com matplotlib porque ele já está no projeto e desenha as chapas — usar
uma biblioteca de PDF só pra montar texto acrescentaria dependência sem
resolver o que é difícil aqui, que é o desenho.

O documento é montado para o CHÃO DE FÁBRICA, não para arquivo: uma página por
padrão, com o desenho grande em cima e, embaixo, quantas chapas cortar e para
onde vai cada peça. O operador nunca precisa virar a página para saber o que
está fazendo.
"""
import os
import tempfile
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from edicao import sequencia_cortes
from visualize import desenhar_chapa

A4 = (8.27, 11.69)          # retrato, em polegadas
MARGEM = 0.06
TINTA = '#1b2027'
CINZA = '#4a5560'
VERDE = '#2a6f4d'


def _texto(fig, x, y, txt, tam=9, cor=TINTA, peso='normal', ha='left'):
    fig.text(x, y, txt, fontsize=tam, color=cor, fontweight=peso, ha=ha, va='top')


def _regua(fig, y, x0=MARGEM, x1=1 - MARGEM, cor='#d8dcdf'):
    fig.add_artist(plt.Line2D([x0, x1], [y, y], color=cor, linewidth=0.8,
                               transform=fig.transFigure))


def _capa(pdf, r: dict, plano: dict) -> None:
    fig = plt.figure(figsize=A4)
    y = 0.95
    _texto(fig, MARGEM, y, 'PLANO DE CORTE', 22, TINTA, 'bold')
    y -= 0.035
    _texto(fig, MARGEM, y, r.get('maquina') or 'Máquina não identificada', 13, VERDE, 'bold')
    y -= 0.028
    emitido = datetime.now().astimezone().strftime('%d/%m/%Y %H:%M')
    _texto(fig, MARGEM, y, f'Emitido em {emitido}  ·  documento {plano["id"]}', 8.5, CINZA)

    y -= 0.02
    _regua(fig, y)
    y -= 0.03

    if plano.get('aprovado'):
        _texto(fig, MARGEM, y, f'APROVADO PELO PCP  ·  {plano["aprovado_por"]}', 11, VERDE, 'bold')
        y -= 0.022
        if plano.get('observacao'):
            _texto(fig, MARGEM, y, f'"{plano["observacao"]}"', 9, CINZA)
            y -= 0.02
    else:
        _texto(fig, MARGEM, y, 'AGUARDANDO CONFERÊNCIA DO PCP', 11, '#a97a1e', 'bold')
        y -= 0.022
    y -= 0.015

    resumo = [
        ('Chapa', f"{r['sheet_w']} × {r['sheet_h']} mm"),
        ('Espessura da serra', f"{r['kerf']} mm"),
        ('Estágios de corte', '2 + aparo' if r.get('estagios', 3) >= 3 else '2'),
        ('Veio da chapa', 'respeitado' if r.get('respeitar_veio') else 'IGNORADO'),
        ('Total de chapas', str(r.get('total_chapas', 0))),
    ]
    for rot, val in resumo:
        _texto(fig, MARGEM, y, rot, 9.5, CINZA)
        _texto(fig, 0.42, y, val, 9.5, TINTA, 'bold')
        y -= 0.021

    y -= 0.015
    _regua(fig, y)
    y -= 0.03
    _texto(fig, MARGEM, y, 'MATERIAIS', 11, TINTA, 'bold')
    y -= 0.026
    for cab, x in (('Cor / espessura', MARGEM), ('Chapas', 0.52),
                    ('Padrões', 0.64), ('Ciclos', 0.75), ('Aproveit.', 0.86)):
        _texto(fig, x, y, cab, 8, CINZA, 'bold')
    y -= 0.019
    for g in r['grupos']:
        _texto(fig, MARGEM, y, f"{g['cor']} · {g['esp']:.0f}mm", 9.5)
        _texto(fig, 0.52, y, str(g['n_chapas']), 9.5, TINTA, 'bold')
        _texto(fig, 0.64, y, str(g['n_padroes']), 9.5)
        _texto(fig, 0.75, y, str(g['ciclos_total']), 9.5)
        _texto(fig, 0.86, y, f"{g['aproveitamento_medio']:.1f}%", 9.5)
        y -= 0.021
        if y < 0.2:
            break

    if r.get('kambans_info'):
        y -= 0.02
        _regua(fig, y)
        y -= 0.03
        _texto(fig, MARGEM, y, 'KAMBANS DESTE PLANO', 11, TINTA, 'bold')
        y -= 0.024
        for k in r['kambans_info']:
            if k.get('duplicado_de'):
                continue
            _texto(fig, MARGEM, y,
                   f"{k['arquivo'][:52]}  ·  lote {k.get('lote') or '?'}  ·  "
                   f"{k.get('qtd_total', 0):.0f} peças", 9)
            y -= 0.019
            if y < 0.08:
                break

    pdf.savefig(fig)
    plt.close(fig)


def _pagina_padrao(pdf, r: dict, g: dict, p: dict, imagem_dir: str) -> None:
    fig = plt.figure(figsize=A4)

    _texto(fig, MARGEM, 0.965, f"{g['cor']} · {g['esp']:.0f}mm", 11, CINZA, 'bold')
    _texto(fig, MARGEM, 0.945, f"PADRÃO {p['n']}", 19, TINTA, 'bold')
    _texto(fig, 1 - MARGEM, 0.945, f"CORTAR {p['repeticoes']} CHAPA"
                                    f"{'S' if p['repeticoes'] > 1 else ''}",
           17, VERDE, 'bold', ha='right')
    cheios, resto = divmod(p['repeticoes'], p['pilha'])
    if resto == 0:
        texto_ciclos = f"{cheios} ciclo{'s' if cheios > 1 else ''} de {p['pilha']} empilhadas"
    else:
        texto_ciclos = (f"{cheios} ciclo{'s' if cheios > 1 else ''} de {p['pilha']} + 1 de {resto}"
                         f"  ({p['pilha']}×{cheios}+{resto}={p['repeticoes']})")
    _texto(fig, 1 - MARGEM, 0.917,
           f"{texto_ciclos}  ·  {p['aproveitamento']:.1f}% de aproveitamento", 8.5, CINZA, ha='right')

    marcas = []
    if p.get('editado'):
        marcas.append('editado à mão')
    if p.get('sugerido'):
        marcas.append('contém sugestão automática')
    if marcas:
        _texto(fig, MARGEM, 0.917, ' · '.join(marcas), 8.5, '#a97a1e', 'bold')

    # desenho da chapa ocupando a metade de cima
    prop = r['sheet_h'] / r['sheet_w']
    largura_ax = 1 - 2 * MARGEM
    altura_ax = largura_ax * prop * (A4[0] / A4[1])
    ax = fig.add_axes([MARGEM, 0.885 - altura_ax, largura_ax, altura_ax])
    desenhar_chapa(ax, p['itens'], r['sheet_w'], r['sheet_h'], veio=g.get('tem_veio'))
    ax.tick_params(labelsize=6, length=2, colors=CINZA)

    y = 0.885 - altura_ax - 0.045
    _regua(fig, y + 0.018)
    for cab, x in (('PEÇA', MARGEM), ('MEDIDA', 0.30), ('CHAPA', 0.44), ('TOTAL', 0.50)):
        _texto(fig, x, y, cab, 8, CINZA, 'bold')
    y -= 0.02

    for pc in p.get('pecas_exibicao', p['pecas']):
        if y < 0.05:
            _texto(fig, MARGEM, y, '... continua', 8.5, CINZA)
            break
        _texto(fig, MARGEM, y, str(pc['cod']), 9.5, TINTA, 'bold')
        _texto(fig, MARGEM + 0.058, y, pc['desc'][:20], 7.5, CINZA)
        _texto(fig, 0.30, y, pc['medida'], 8.5)
        _texto(fig, 0.44, y, str(pc['por_chapa']), 9.5, TINTA, 'bold')
        _texto(fig, 0.50, y, str(pc['total']), 9)
        y -= 0.0205

    # Sequência de corte: os campos que o operador digita na máquina. Vai na
    # mesma página do desenho de propósito — ele confere a figura e digita
    # sem trocar de papel.
    cortes = sequencia_cortes(p['itens'], r['sheet_w'], r['sheet_h'], r.get('kerf', 0))
    if cortes:
        x_seq = 0.58
        yy = y - 0.012
        if yy > 0.10:
            _texto(fig, x_seq, yy, 'SEQUÊNCIA NA MÁQUINA', 8, CINZA, 'bold')
            yy -= 0.019
            for c in cortes:
                if yy < 0.04:
                    break
                _texto(fig, x_seq, yy, f"{c['estagio']}", 8.5, CINZA)
                _texto(fig, x_seq + 0.03, yy, c['tipo'], 8.5)
                _texto(fig, x_seq + 0.20, yy, f"{c['medida']} mm", 9, TINTA, 'bold')
                _texto(fig, x_seq + 0.30, yy, f"x{c['quantidade']}", 8.5, CINZA)
                yy -= 0.0185

    pdf.savefig(fig)
    plt.close(fig)


def gerar(r: dict, plano: dict, destino: str | None = None) -> str:
    """Monta o PDF completo e devolve o caminho do arquivo."""
    if destino is None:
        destino = os.path.join(tempfile.gettempdir(), f'plano_{plano["id"]}.pdf')
    with PdfPages(destino) as pdf:
        _capa(pdf, r, plano)
        for g in r['grupos']:
            for p in g['padroes']:
                if p.get('itens'):
                    _pagina_padrao(pdf, r, g, p, '')
        info = pdf.infodict()
        info['Title'] = f"Plano de corte {plano['id']}"
        info['Subject'] = r.get('maquina') or ''
        info['Creator'] = 'Otimizador de Corte'
    return destino
