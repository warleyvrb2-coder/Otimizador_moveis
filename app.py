# -*- coding: utf-8 -*-
"""
Otimizador de Corte - Setor Moveleiro.

Camada web, fina de propósito: recebe os PDFs, enfileira o cálculo em segundo
plano (jobs.py), acompanha o progresso e mostra o plano pronto. Toda a lógica
de corte está em pipeline.py, que não sabe que existe web.

Fluxo:
1. Usuário sobe 1+ PDFs "Kambam"
2. Parseamos e juntamos tudo num pool único de peças
3. Agrupamos por (cor, espessura) - só pode compartilhar chapa quem tem a mesma
4. Cada grupo vai pro column generation, aproveitando sobra de uma chapa para
   peças de outro Kambam
5. Mostramos os PADRÕES de corte: o desenho, quantas chapas repetir, e o
   destino de cada peça por Kambam
"""
import os
import secrets

from flask import (Flask, request, render_template, redirect, url_for,
                   jsonify, abort, Response, send_from_directory)
from werkzeug.utils import secure_filename

import banco
import edicao
import jobs
import pipeline
import planilha
from visualize import render_sheet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Em hospedagem o disco é efêmero: escrever dentro do projeto some no próximo
# deploy. DATA_DIR permite apontar pra um volume persistente quando houver.
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
# Os desenhos ficam junto do resto dos dados, e NÃO em static/: assim um único
# volume apontado por DATA_DIR preserva cadastro, uploads e planos de uma vez.
OUTPUT_DIR = os.path.join(DATA_DIR, 'resultados')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Senha única compartilhada. Não é sistema de usuários - é uma tranca pra URL
# não ficar aberta na internet enquanto o testador usa.
APP_USUARIO = os.environ.get('APP_USUARIO', 'benetil')
APP_SENHA = os.environ.get('APP_SENHA')

# Rodando na sua máquina, sem senha, tudo bem. Publicado, NÃO: uma URL aberta
# aceita upload e expõe a produção da fábrica pra qualquer um. Em vez de
# confiar em alguém lembrar de configurar a variável, o app se recusa a
# atender quando está hospedado sem senha - falha fechado, não aberto.
EM_NUVEM = bool(os.environ.get('RAILWAY_ENVIRONMENT') or
                os.environ.get('RAILWAY_SERVICE_ID') or
                os.environ.get('FORCAR_SENHA'))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30MB
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))


@app.before_request
def exigir_senha():
    # O healthcheck da hospedagem bate aqui sem credencial nenhuma. Se este
    # endpoint pedir senha, ele recebe 401, conclui que o serviço está fora e
    # derruba o deploy inteiro - por isso fica liberado antes de qualquer
    # verificação. Não expõe nada: responde só {"ok": true}.
    if request.endpoint == 'saude':
        return None

    if not APP_SENHA:
        if EM_NUVEM:
            return Response(
                'Este app está publicado sem senha configurada e por isso está '
                'bloqueado. Defina a variável de ambiente APP_SENHA no painel da '
                'hospedagem e reinicie o serviço.', 503, {'Content-Type': 'text/plain; charset=utf-8'})
        return None

    auth = request.authorization
    if auth and auth.username == APP_USUARIO and secrets.compare_digest(auth.password or '', APP_SENHA):
        return None
    return Response('Acesso restrito.', 401,
                    {'WWW-Authenticate': 'Basic realm="Otimizador de Corte"'})


@app.context_processor
def contexto_lateral():
    """Alimenta a barra lateral em todas as telas: contadores de pendência e os
    parâmetros de máquina que aparecem no rodapé."""
    try:
        res = banco.resumo()
        par = banco.obter_parametros()
        return {'nav': {
            'pecas_pendentes': res['pecas'] - res['pecas_confirmadas'],
            'cores_pendentes': res['cores'] - res['cores_confirmadas'],
            'chapa': f"{par['chapa_larg']}×{par['chapa_alt']}",
            'kerf': str(par['kerf']).replace('.', ','),
        }}
    except Exception:                    # banco ainda não existe no primeiro acesso
        return {'nav': None}


@app.route('/')
def index():
    return render_template('index.html', pagina='novo', res=banco.resumo(),
                            maquinas=banco.listar_maquinas(so_ativas=True))


def _quando(iso: str) -> str:
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(iso).astimezone().strftime('%d/%m %H:%M')
    except (ValueError, TypeError):
        return iso or ''


@app.route('/planos')
def planos():
    """Os que ainda estão calculando vêm da memória; o resto, do banco."""
    lista = []
    for j in sorted(jobs.todos(), key=lambda j: j.criado_em, reverse=True):
        if j.estado in ('na_fila', 'rodando', 'erro'):
            lista.append({'id': j.id, 'estado': j.estado, 'pct': j.pct,
                           'quando': j.criado_em.astimezone().strftime('%d/%m %H:%M'),
                           'arquivos': None, 'chapas': None, 'aprovado': False})
    for p in banco.listar_planos():
        lista.append({'id': p['id'], 'estado': 'pronto', 'pct': 100,
                       'quando': _quando(p['criado_em']), 'arquivos': p['arquivos'],
                       'chapas': p['total_chapas'], 'aprovado': bool(p['aprovado']),
                       'aprovado_por': p['aprovado_por']})
    return render_template('planos.html', pagina='planos', planos=lista)


def diagnostico_armazenamento() -> dict:
    """
    Onde o app está realmente gravando, e se aquilo sobrevive a um deploy.

    Existe porque "anexei o volume" e "o app está usando o volume" são coisas
    diferentes: o volume só entra em uso quando DATA_DIR aponta pro caminho de
    montagem. Sem isso ele fica montado e ocioso, e o cadastro continua sendo
    perdido a cada atualização — em silêncio, que é o pior jeito de falhar.
    """
    import shutil
    # Comparar caminhos não basta: apontar DATA_DIR pra /data sem volume
    # montado ali cria só uma pasta comum dentro do container, que some no
    # próximo deploy do mesmo jeito - e o app diria "permanente" mentindo.
    # Volume de verdade é outro dispositivo de disco, então comparamos st_dev.
    mesmo_disco = None
    try:
        mesmo_disco = os.stat(DATA_DIR).st_dev == os.stat(BASE_DIR).st_dev
    except OSError:
        pass
    caminho_diferente = os.path.abspath(DATA_DIR) != os.path.abspath(BASE_DIR)
    # fora da nuvem não faz sentido falar em volume; lá dentro, só é permanente
    # quando o caminho difere E está num dispositivo separado
    permanente = caminho_diferente and (mesmo_disco is False)
    info = {
        'caminho': os.path.abspath(DATA_DIR),
        'permanente': permanente,
        'caminho_diferente': caminho_diferente,
        'mesmo_disco': mesmo_disco,
        'em_nuvem': EM_NUVEM,
        'banco_kb': (os.path.getsize(banco.DB_PATH) / 1024
                     if os.path.exists(banco.DB_PATH) else 0),
        'planos': (len(os.listdir(OUTPUT_DIR)) if os.path.isdir(OUTPUT_DIR) else 0),
        'livre_mb': None, 'total_mb': None,
    }
    try:
        uso = shutil.disk_usage(DATA_DIR)
        info['livre_mb'] = uso.free / 1024 / 1024
        info['total_mb'] = uso.total / 1024 / 1024
    except OSError:
        pass
    return info


@app.route('/parametros', methods=['GET', 'POST'])
def parametros():
    erros, salvou = {}, False
    if request.method == 'POST':
        erros = banco.salvar_parametros(request.form.to_dict())
        salvou = not erros
    return render_template('parametros.html', pagina='parametros',
                            campos=banco.PARAMETROS, valores=banco.obter_parametros(),
                            erros=erros, salvou=salvou,
                            disco=diagnostico_armazenamento())


@app.route('/saude')
def saude():
    """Usado pela hospedagem pra saber se o app subiu."""
    return {'ok': True}


@app.route('/otimizar', methods=['POST'])
def otimizar():
    """
    Cada máquina tem sua própria caixa de upload e vira um plano separado.

    Separado de propósito: máquinas com chapa ou disco diferentes produzem
    planos diferentes, e juntar tudo num documento só impediria o PCP de
    aprovar o de uma máquina sem aprovar o da outra. Também não faria sentido
    compartilhar sobra entre chapas de tamanhos distintos.
    """
    max_estagios_raw = request.form.get('max_estagios', 'ilimitado')
    max_depth = None if max_estagios_raw == 'ilimitado' else int(max_estagios_raw)
    respeitar_veio = request.form.get('respeitar_veio') is not None

    criados = []
    for maq in banco.listar_maquinas(so_ativas=True):
        arquivos = [f for f in request.files.getlist(f'kambans_{maq["id"]}')
                    if f and f.filename]
        if not arquivos:
            continue
        job_id = os.urandom(5).hex()
        salvos = []
        for f in arquivos:
            nome = secure_filename(f.filename) or 'kambam.pdf'
            caminho = os.path.join(UPLOAD_DIR, f'{job_id}_{nome}')
            f.save(caminho)
            salvos.append((caminho, f.filename))
        job = jobs.criar(
            pipeline.rodar, salvos, os.path.join(OUTPUT_DIR, job_id), f'/plano/{job_id}',
            respeitar_veio=respeitar_veio, max_depth=max_depth, maquina_id=maq['id'],
            ao_terminar=lambda j: (banco.salvar_plano(j.id, j.resultado)
                                    if j.resultado and not j.resultado.get('erro') else None),
        )
        criados.append(job.id)

    if not criados:
        return redirect(url_for('index'))
    if len(criados) == 1:
        return redirect(url_for('acompanhar', job_id=criados[0]))
    return redirect(url_for('planos'))


@app.route('/maquinas')
def maquinas():
    return render_template('maquinas.html', pagina='maquinas',
                            maquinas=banco.listar_maquinas())


@app.route('/maquinas/nova', methods=['GET', 'POST'])
@app.route('/maquinas/<int:maquina_id>', methods=['GET', 'POST'])
def maquina_form(maquina_id=None):
    m = banco.maquina(maquina_id) if maquina_id else None
    if maquina_id and not m:
        abort(404)
    erros = {}
    if request.method == 'POST':
        dados = request.form.to_dict()
        if request.form.get('excluir') == '1' and maquina_id:
            banco.excluir_maquina(maquina_id)
            return redirect(url_for('maquinas'))
        if maquina_id:
            erros = banco.atualizar_maquina(maquina_id, dados)
            if not erros:
                return redirect(url_for('maquinas'))
            m = dict(m) | dados
        else:
            novo_id, erros = banco.criar_maquina(dados)
            if not erros:
                return redirect(url_for('maquinas'))
            m = dados
    # o que voltou do formulário pode estar incompleto (campo em branco, número
    # inválido); os padrões preenchem o resto pra tela conseguir renderizar
    padroes = {c: banco.PARAMETROS[c]['padrao'] for c in banco.CAMPOS_MAQUINA}
    valores = padroes | dict(m or {})
    return render_template('maquina.html', pagina='maquinas', m=valores,
                            maquina_id=maquina_id, campos=banco.PARAMETROS, erros=erros)


@app.route('/plano/<job_id>/<nome>')
def imagem_do_plano(job_id, nome):
    """Serve os desenhos das chapas, que ficam no DATA_DIR e não em static/."""
    return send_from_directory(os.path.join(OUTPUT_DIR, secure_filename(job_id)), nome)


@app.route('/calculando/<job_id>')
def acompanhar(job_id):
    job = jobs.obter(job_id) or abort(404)
    if job.estado == 'pronto':
        return redirect(url_for('resultado', job_id=job_id))
    return render_template('calculando.html', job=job)


@app.route('/progresso/<job_id>')
def progresso(job_id):
    job = jobs.obter(job_id) or abort(404)
    return jsonify(job.como_json())


@app.route('/cadastro/')
@app.route('/cadastro/<aba>')
def cadastro(aba='pecas'):
    if aba not in ('pecas', 'cores'):
        abort(404)
    banco.criar_tabelas()
    busca = request.args.get('busca', '').strip()
    pendentes = request.args.get('pendentes') == '1'
    itens = (banco.listar_pecas(busca, so_pendentes=pendentes, limite=300) if aba == 'pecas'
             else banco.listar_cores())
    res = banco.resumo()
    # Publicado sem volume, o banco vive no disco efêmero e some no próximo
    # deploy. Quem conferir 125 peças precisa saber disso ANTES, não depois.
    efemero = EM_NUVEM and os.path.abspath(banco.DB_PATH).startswith(os.path.abspath(BASE_DIR))
    return render_template('cadastro.html', pagina=aba, aba=aba, itens=itens, busca=busca,
                            pendentes=pendentes, efemero=efemero, res=res,
                            total=res['pecas'] if aba == 'pecas' else res['cores'],
                            confirmados=(res['pecas_confirmadas'] if aba == 'pecas'
                                          else res['cores_confirmadas']))


@app.route('/modelos')
def modelos():
    return render_template('modelos.html', pagina='modelos',
                            modelos=banco.listar_modelos(),
                            sem_modelo=banco.contar_sem_modelo())


@app.route('/modelos/sem-modelo')
def pecas_orfas():
    """
    As peças que não ficaram ligadas a móvel nenhum.

    Precisam de um lugar próprio: são a maioria hoje, e sem esta tela sairiam
    do alcance quando o cadastro de peças virou parte dos modelos. Peça que
    ninguém confere é tratada como aparente, o que gasta chapa a mais.
    """
    busca = request.args.get('busca', '').strip()
    pecas = banco.pecas_sem_modelo(busca, limite=300)
    return render_template('modelo.html', pagina='modelos', m=None, orfas=True,
                            titulo_pagina='Peças sem modelo', busca=busca, pecas=pecas,
                            todos_modelos=banco.modelos_para_escolha(),
                            pendentes=sum(1 for p in pecas if not p['confirmado']))


@app.route('/modelos/<cod>')
def modelo_detalhe(cod):
    m = banco.modelo(cod) or abort(404)
    pecas = banco.pecas_do_modelo(cod)
    # o campo de busca já vem preenchido com a palavra que identifica o móvel:
    # a descrição da peça quase sempre cita o modelo, então isso costuma trazer
    # os candidatos de primeira
    busca = request.args.get('busca')
    if busca is None:
        busca = banco.sugestao_de_busca(m['descricao'])
    return render_template('modelo.html', pagina='modelos', m=m, orfas=False,
                            titulo_pagina=m['descricao'], pecas=pecas, busca=busca,
                            candidatas=banco.candidatas_para_modelo(cod, busca),
                            todos_modelos=banco.modelos_para_escolha(),
                            pendentes=sum(1 for p in pecas if not p['confirmado']))


@app.route('/acabamentos')
def acabamentos():
    return render_template('acabamentos.html', pagina='acabamentos',
                            acabamentos=banco.listar_acabamentos(),
                            cores=[c['nome'] for c in banco.listar_cores()])


@app.route('/acabamentos/marcar', methods=['POST'])
def acabamento_marcar():
    d = request.get_json(silent=True) or {}
    if not d.get('acabamento') or not d.get('cor'):
        return jsonify({'ok': False}), 400
    banco.definir_acabamento_cor(d['acabamento'], d['cor'], bool(d.get('ligado')))
    return jsonify({'ok': True})


@app.route('/modelos/por-unidade', methods=['POST'])
def modelo_por_unidade():
    d = request.get_json(silent=True) or {}
    try:
        qtd = float(str(d.get('quantidade', '')).replace(',', '.'))
    except ValueError:
        return jsonify({'ok': False}), 400
    if not d.get('modelo') or not d.get('peca') or qtd < 0:
        return jsonify({'ok': False}), 400
    banco.definir_por_unidade(d['modelo'], d['peca'], qtd)
    return jsonify({'ok': True})


CATALOGO_DIR = os.path.join(DATA_DIR, 'catalogo')
os.makedirs(CATALOGO_DIR, exist_ok=True)


@app.route('/cadastro/importar', methods=['GET', 'POST'])
def importar_catalogo():
    """
    Traz o catálogo de itens do Agrosys em duas etapas.

    Duas porque o arquivo mistura peça de chapa com MANTA, CAIXA de papelão e
    ISOPOR - coisas que não passam pela serra. Em vez de eu adivinhar o que
    interessa, mostro as espessuras encontradas com exemplos e você escolhe.
    """
    if request.method == 'GET':
        return render_template('importar.html', pagina='modelos', etapa='enviar')

    # etapa 2: confirmar as espessuras de um arquivo já enviado
    token = request.form.get('token')
    if token:
        caminho = os.path.join(CATALOGO_DIR, secure_filename(token))
        if not os.path.exists(caminho):
            return redirect(url_for('importar_catalogo'))
        escolhidas = {float(e) for e in request.form.getlist('espessura')}
        resultado = banco.importar_catalogo(planilha.ler_itens(caminho), escolhidas)
        os.remove(caminho)
        return render_template('importar.html', pagina='modelos', etapa='pronto',
                                resultado=resultado)

    # etapa 1: recebe o arquivo e mostra o que tem dentro
    f = request.files.get('planilha')
    if not f or not f.filename:
        return redirect(url_for('importar_catalogo'))
    token = os.urandom(6).hex() + '.xml'
    caminho = os.path.join(CATALOGO_DIR, token)
    f.save(caminho)
    try:
        itens = planilha.ler_itens(caminho)
    except Exception as e:                      # noqa: BLE001 - arquivo de terceiro
        os.remove(caminho)
        return render_template('importar.html', pagina='modelos', etapa='enviar',
                                erro=f'Não consegui ler a planilha ({type(e).__name__}). '
                                      'O arquivo precisa ser o "Itens com Especificações" '
                                      'exportado pelo Agrosys.')
    if not itens:
        os.remove(caminho)
        return render_template('importar.html', pagina='modelos', etapa='enviar',
                                erro='A planilha foi lida mas não tem nenhuma linha de item '
                                      'com medidas. Confira se é o relatório certo.')

    grupos = {}
    for i in itens:
        g = grupos.setdefault(i['esp_mm'], {'qtd': 0, 'exemplos': []})
        g['qtd'] += 1
        if len(g['exemplos']) < 3:
            g['exemplos'].append(i['desc'][:46])
    espessuras = [{'valor': e, **v} for e, v in sorted(grupos.items(), key=lambda kv: -kv[1]['qtd'])]
    return render_template('importar.html', pagina='modelos', etapa='escolher',
                            arquivo=f.filename, token=token, total=len(itens),
                            espessuras=espessuras)


@app.route('/modelos/<cod>/adicionar', methods=['POST'])
def modelo_adicionar(cod):
    """Vincula várias peças ao móvel de uma vez, vindo da busca."""
    if not banco.modelo(cod):
        abort(404)
    escolhidas = []
    for peca in request.form.getlist('peca'):
        try:
            qtd = float((request.form.get(f'qtd_{peca}') or '1').replace(',', '.'))
        except ValueError:
            qtd = 1.0
        if qtd > 0:
            escolhidas.append((peca, qtd))
    if escolhidas:
        banco.vincular_varias(cod, escolhidas)
    return redirect(url_for('modelo_detalhe', cod=cod,
                             busca=request.form.get('busca', '')))


@app.route('/modelos/vincular', methods=['POST'])
def modelo_vincular():
    """Liga uma peça a um móvel, ou desfaz a ligação."""
    d = request.get_json(silent=True) or {}
    modelo_cod, peca_cod = d.get('modelo'), d.get('peca')
    if not modelo_cod or not peca_cod:
        return jsonify({'ok': False}), 400
    if d.get('remover'):
        banco.remover_vinculo(modelo_cod, peca_cod)
        return jsonify({'ok': True})
    try:
        qtd = float(str(d.get('quantidade', 1)).replace(',', '.'))
    except ValueError:
        return jsonify({'ok': False, 'erro': 'quantidade inválida'}), 400
    if qtd <= 0:
        return jsonify({'ok': False, 'erro': 'quantidade precisa ser maior que zero'}), 400
    banco.definir_por_unidade(modelo_cod, peca_cod, qtd)
    return jsonify({'ok': True})


@app.route('/cadastro/marcar', methods=['POST'])
def cadastro_marcar():
    dados = request.get_json(silent=True) or {}
    tipo, ident, valor = dados.get('tipo'), dados.get('id'), bool(dados.get('valor'))
    if tipo == 'peca' and ident:
        banco.definir_peca(str(ident), valor)
    elif tipo == 'cor' and ident:
        banco.definir_cor(str(ident), valor)
    else:
        return jsonify({'ok': False}), 400
    return jsonify({'ok': True})


@app.route('/resultado/<job_id>')
def resultado(job_id):
    # o plano gravado é a fonte da verdade; a memória só cobre o que ainda
    # está calculando nesta execução do servidor
    salvo = banco.obter_plano(job_id)
    if salvo:
        return render_template('resultado.html', plano=salvo, **salvo['resultado'])
    job = jobs.obter(job_id) or abort(404)
    if job.estado == 'erro':
        return render_template('resultado.html', erro=job.erro, kambans_info=None), 500
    if job.estado != 'pronto':
        return redirect(url_for('acompanhar', job_id=job_id))
    return render_template('resultado.html', **job.resultado)


def _localizar_padrao(resultado, gi, pi):
    try:
        grupo = resultado['grupos'][gi]
        return grupo, grupo['padroes'][pi]
    except (IndexError, KeyError, TypeError):
        return None, None


def _recalcular(resultado):
    """Refaz os numeros do plano depois de mexer nas pecas de um padrao."""
    area_chapa = (resultado['sheet_w'] / 1000) * (resultado['sheet_h'] / 1000)
    for g in resultado['grupos']:
        chapas = 0
        area_usada = 0.0
        for pad in g['padroes']:
            usada = sum((i['w'] / 1000) * (i['h'] / 1000) for i in pad.get('itens', []))
            pad['aproveitamento'] = 100 * usada / area_chapa if area_chapa else 0
            chapas += pad['repeticoes']
            area_usada += usada * pad['repeticoes']
        g['n_chapas'] = chapas
        g['aproveitamento_medio'] = (100 * area_usada / (chapas * area_chapa)
                                      if chapas and area_chapa else 0)
        g['n_padroes'] = len(g['padroes'])
    resultado['total_chapas'] = sum(g['n_chapas'] for g in resultado['grupos'])


def _conferir_demanda(resultado):
    """
    Compara o que o plano produz com o que os Kambans pediram.

    Mexer num padrao repetido 42 vezes mexe em 42 pecas de uma vez: tirar uma
    peca deixa o lote incompleto, acrescentar gera excedente. Sem esta conta a
    edicao pareceria inofensiva e o operador so descobriria na montagem.
    """
    for g in resultado['grupos']:
        pedido, produzido = {}, {}
        for pad in g['padroes']:
            for pc in pad.get('pecas', []):
                pedido[pc['cod']] = pedido.get(pc['cod'], 0) + sum(pc.get('lotes', {}).values())
            for it in pad.get('itens', []):
                produzido[it['cod']] = produzido.get(it['cod'], 0) + pad['repeticoes']
        g['diferencas'] = [{'cod': c, 'dif': produzido.get(c, 0) - pedido.get(c, 0)}
                            for c in sorted(set(pedido) | set(produzido))
                            if produzido.get(c, 0) != pedido.get(c, 0)]


def _pode_girar_aqui(peca, grupo):
    """Peca so e obrigada a manter a orientacao se a cor tem veio E ela aparece."""
    return not (grupo.get('tem_veio') and peca['aparente'])


def _redesenhar(plano_id, r, grupo, padrao):
    """Refaz o PNG do padrao editado, senao o desenho mentiria."""
    from types import SimpleNamespace
    itens = [SimpleNamespace(**i) for i in padrao['itens']]
    chapa = SimpleNamespace(items=itens, repeticoes=padrao['repeticoes'],
                             aproveitamento=padrao['aproveitamento'])
    nome = padrao['arquivo'].rsplit('/', 1)[-1]
    caminho = os.path.join(OUTPUT_DIR, secure_filename(plano_id), nome)
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    render_sheet(chapa, r['sheet_w'], r['sheet_h'], caminho,
                 titulo=(grupo['cor'] + ' - Padrao ' + str(padrao['n']) + ' (editado)'),
                 veio=grupo.get('tem_veio'))


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>')
def editar_padrao(plano_id, gi, pi):
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None or 'itens' not in padrao:
        abort(404)
    livres = edicao.retalhos_livres(padrao['itens'], r['sheet_w'], r['sheet_h'])
    escolhido = request.args.get('retalho', type=int)
    busca = request.args.get('busca', '').strip()
    candidatas = []
    if escolhido is not None and 0 <= escolhido < len(livres):
        ret = livres[escolhido]
        for p in banco.listar_pecas(busca, limite=500):
            enc = edicao.encaixar(ret, p['comp_mm'], p['larg_mm'], r['kerf'],
                                   _pode_girar_aqui(p, grupo))
            if enc:
                candidatas.append({'cod': p['cod'], 'desc': p['descricao'],
                                    'comp': p['comp_mm'], 'larg': p['larg_mm'],
                                    'girada': enc['rotated']})
    return render_template('editar.html', pagina='planos', plano=salvo, r=r,
                            grupo=grupo, padrao=padrao, gi=gi, pi=pi, livres=livres,
                            escolhido=escolhido, busca=busca, candidatas=candidatas[:80],
                            erro=request.args.get('erro'), ok=request.args.get('ok'),
                            estagios_atuais=edicao.estagios(padrao['itens'],
                                                             r['sheet_w'], r['sheet_h']))


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>/aplicar', methods=['POST'])
def aplicar_edicao(plano_id, gi, pi):
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None or 'itens' not in padrao:
        abort(404)

    def volta(**extra):
        return redirect(url_for('editar_padrao', plano_id=plano_id, gi=gi, pi=pi, **extra))

    itens = [dict(i) for i in padrao['itens']]
    acao = request.form.get('acao')
    peca = None
    removida = None
    livres = []
    i_ret = None

    if acao == 'remover':
        idx = request.form.get('indice', type=int)
        if idx is None or not (0 <= idx < len(itens)):
            return volta(erro='Peca nao encontrada neste padrao.')
        removida = itens[idx].get('cod')
        itens.pop(idx)
    elif acao == 'adicionar':
        i_ret = request.form.get('retalho', type=int)
        cod = (request.form.get('cod') or '').strip()
        livres = edicao.retalhos_livres(itens, r['sheet_w'], r['sheet_h'])
        peca = next((p for p in banco.listar_pecas(cod, limite=80) if p['cod'] == cod), None)
        if peca is None:
            return volta(erro='Codigo ' + cod + ' nao existe no cadastro de pecas.')
        if i_ret is None or not (0 <= i_ret < len(livres)):
            return volta(erro='Escolha em qual sobra a peca vai entrar.')
        enc = edicao.encaixar(livres[i_ret], peca['comp_mm'], peca['larg_mm'], r['kerf'],
                               _pode_girar_aqui(peca, grupo))
        if not enc:
            return volta(retalho=i_ret,
                          erro='A peca nao cabe nesta sobra considerando a folga da serra.')
        itens.append({'cod': peca['cod'], 'desc': peca['descricao'],
                       'piece_key': peca['cod'] + '_manual', 'shelf': 0, **enc})
    else:
        return volta(erro='Acao desconhecida.')

    problemas = edicao.validar_padrao(itens, r['sheet_w'], r['sheet_h'], r['estagios'])
    if problemas:
        return volta(erro=' '.join(problemas))

    aprov_antes = padrao.get('aproveitamento')
    padrao['itens'] = itens
    padrao['editado'] = True
    _recalcular(r)
    _conferir_demanda(r)
    banco.atualizar_resultado(plano_id, r)
    _redesenhar(plano_id, r, grupo, padrao)

    # O registro e o que permite o sistema repetir sozinho o que voce faz
    # sempre - e, mais util ainda, mostrar onde o otimizador esta deixando
    # espaco na mesa de forma sistematica.
    banco.registrar_edicao({
        'plano_id': plano_id, 'cor': grupo.get('cor'), 'esp': grupo.get('esp'),
        'acao': acao, 'peca_cod': (peca['cod'] if acao == 'adicionar' else removida),
        'sobra_w': int(livres[i_ret].w) if acao == 'adicionar' else None,
        'sobra_h': int(livres[i_ret].h) if acao == 'adicionar' else None,
        'repeticoes': padrao.get('repeticoes'),
        'aprov_antes': aprov_antes, 'aprov_depois': padrao.get('aproveitamento'),
    })
    return volta(ok='1')


@app.route('/aprendizado')
def aprendizado():
    """O que o sistema aprendeu com as edicoes manuais."""
    return render_template('aprendizado.html', pagina='aprendizado',
                            regras=banco.regras_aprendidas(minimo=1),
                            historico=banco.historico_edicoes(60),
                            auto=banco.obter_parametros().get('auto_sugerir', 1))


@app.route('/resultado/<job_id>/aprovar', methods=['POST'])
def aprovar(job_id):
    if not banco.obter_plano(job_id):
        abort(404)
    desfazer = request.form.get('desfazer') == '1'
    banco.aprovar_plano(job_id, por=(request.form.get('por') or APP_USUARIO).strip()[:60],
                         observacao=(request.form.get('observacao') or '').strip()[:400],
                         aprovar=not desfazer)
    return redirect(url_for('resultado', job_id=job_id))


if __name__ == '__main__':
    # Só para desenvolvimento na sua máquina. Publicado, quem sobe o app é o
    # gunicorn (veja o Procfile) - nunca este bloco, e nunca com debug ligado:
    # o depurador do Flask permite executar código no servidor pela página.
    app.run(host='127.0.0.1', port=5000, debug=bool(os.environ.get('FLASK_DEBUG')))
