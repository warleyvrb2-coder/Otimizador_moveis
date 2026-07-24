# -*- coding: utf-8 -*-
"""
Otimizador de Corte - Setor Moveleiro (MVP)

Fluxo:
1. Usuário sobe 1+ PDFs "Kambam"
2. Parseamos e juntamos tudo num pool único de peças
3. Agrupamos por (cor, espessura) - só pode compartilhar chapa quem tem a mesma
4. Para cada grupo, rodamos o otimizador CP-SAT (chapa a chapa, guillotine
   2-estágios), aproveitando sobra de uma chapa para peças de outro Kambam
5. Mostramos o desenho de cada chapa + % de aproveitamento
"""
import os
import uuid
import shutil
from collections import defaultdict

from flask import Flask, request, render_template, redirect, url_for, send_from_directory

from parser import parse_kambam
from optimizer import PieceType
from column_generation import optimize_group_cg
from visualize import render_sheet

# orçamento de tempo do column generation por grupo (cor+espessura).
# Quanto mais tempo, mais perto do ótimo teórico ele chega (veja README).
CG_TIME_BUDGET_S = 45.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
OUTPUT_DIR = os.path.join(BASE_DIR, 'static', 'resultados')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Chapa padrão MDF: 2750 x 1850 mm (~5,1 m²)
SHEET_W_MM = 2750
SHEET_H_MM = 1850

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30MB


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/otimizar', methods=['POST'])
def otimizar():
    files = request.files.getlist('kambans')
    files = [f for f in files if f and f.filename]
    if not files:
        return redirect(url_for('index'))

    run_id = uuid.uuid4().hex[:10]
    run_dir = os.path.join(OUTPUT_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    max_estagios_raw = request.form.get('max_estagios', 'ilimitado')
    max_depth = None if max_estagios_raw == 'ilimitado' else int(max_estagios_raw)

    # 1) Parse de todos os PDFs -> pool único de peças (linhas cruas)
    todas_pecas = []
    kambans_info = []
    for f in files:
        path = os.path.join(UPLOAD_DIR, f'{run_id}_{f.filename}')
        f.save(path)
        pecas = parse_kambam(path, source_name=f.filename)
        todas_pecas.extend(pecas)
        kambans_info.append({'arquivo': f.filename, 'n_linhas': len(pecas),
                              'qtd_total': sum(p['qtd'] for p in pecas)})

    if not todas_pecas:
        return render_template('resultado.html', erro=(
            'Não consegui extrair nenhuma peça dos PDFs enviados. '
            'Verifique se o formato bate com o padrão do relatório Kambam.'
        ), kambans_info=kambans_info)

    # 2) Agrupa por (cor, espessura) e consolida por (cor,esp,cod,comp,larg)
    grupos = defaultdict(dict)  # (cor,esp) -> {chave: PieceType}
    for p in todas_pecas:
        cor, esp = p['cor'], p['esp_mm']
        chave = f"{p['cod']}_{int(p['comp_mm'])}x{int(p['larg_mm'])}"
        d = grupos[(cor, esp)]
        if chave in d:
            d[chave].qty_total += int(round(p['qtd']))
            d[chave].origem.append(p['origem'])
        else:
            d[chave] = PieceType(
                key=chave, cod=p['cod'], desc=p['desc'],
                w=int(round(p['comp_mm'])), h=int(round(p['larg_mm'])),
                qty_total=int(round(p['qtd'])), origem=[p['origem']],
            )

    # 3) Otimiza cada grupo separadamente
    grupos_resultado = []
    for (cor, esp), tipos_dict in grupos.items():
        tipos = list(tipos_dict.values())
        qtd_total_grupo = sum(t.qty_total for t in tipos)
        sheets, sobras = optimize_group_cg(
            [PieceType(**{**t.__dict__}) for t in tipos],
            SHEET_W_MM, SHEET_H_MM, time_budget_s=CG_TIME_BUDGET_S, max_depth=max_depth,
        )

        MAX_IMAGENS = 15
        if len(sheets) > MAX_IMAGENS:
            # amostra: as piores (menor aproveitamento) + as melhores + primeiras
            por_aproveitamento = sorted(sheets, key=lambda s: s.aproveitamento)
            amostra_keys = set(s.index for s in por_aproveitamento[:5])
            amostra_keys |= set(s.index for s in por_aproveitamento[-5:])
            amostra_keys |= set(s.index for s in sheets[:5])
            sheets_mostrar = [s for s in sheets if s.index in amostra_keys]
            sheets_mostrar.sort(key=lambda s: s.index)
        else:
            sheets_mostrar = sheets

        imagens = []
        for sheet in sheets_mostrar:
            img_name = f'{cor}_{int(esp)}mm_chapa{sheet.index}.png'.replace(' ', '_').replace('/', '-')
            img_path = os.path.join(run_dir, img_name)
            render_sheet(sheet, SHEET_W_MM, SHEET_H_MM, img_path,
                         titulo=f'{cor} · {esp:.0f}mm · Chapa {sheet.index}')
            imagens.append({
                'arquivo': f'resultados/{run_id}/{img_name}',
                'chapa': sheet.index,
                'aproveitamento': sheet.aproveitamento,
            })

        area_total_pool = sum(t.w * t.h * t.qty_total for t in tipos) / 1_000_000
        area_usada = sum(s.used_area for s in sheets)
        n_chapas = len(sheets)
        aproveitamento_medio = (100 * area_usada / (n_chapas * (SHEET_W_MM / 1000) * (SHEET_H_MM / 1000))
                                 if n_chapas else 0)

        grupos_resultado.append({
            'cor': cor,
            'esp': esp,
            'n_tipos_peca': len(tipos),
            'qtd_total_pecas': qtd_total_grupo,
            'n_chapas': n_chapas,
            'aproveitamento_medio': aproveitamento_medio,
            'imagens': imagens,
            'amostra': len(sheets) > MAX_IMAGENS,
            'sobras': [{'cod': s.cod, 'desc': s.desc, 'qtd': s.qty_total} for s in sobras],
        })

    grupos_resultado.sort(key=lambda g: -g['qtd_total_pecas'])

    return render_template('resultado.html', erro=None, kambans_info=kambans_info,
                            grupos=grupos_resultado, sheet_w=SHEET_W_MM, sheet_h=SHEET_H_MM)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
