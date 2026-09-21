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
import glob
import os
import secrets
import shutil
from datetime import datetime as _datetime, timezone as _timezone
from zoneinfo import ZoneInfo

from flask import (Flask, request, render_template, redirect, url_for,
                   jsonify, abort, Response, send_from_directory, send_file,
                   make_response)
from werkzeug.utils import secure_filename

import banco
import edicao
import jobs
import pipeline
import planilha
import relatorio
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

# Espessura da chapa que passa nas seccionadoras da fábrica na prática. O
# Plano Manual não tem Kambam pra tirar isso de um PDF (é dali que o fluxo
# automático pega a espessura de cada lote), então usa este número fixo pra
# calcular a pilha (quantas chapas cabem empilhadas) a partir do que a
# máquina já tem cadastrado - sem perguntar de novo pra cada chapa manual.
ESPESSURA_PADRAO_MM = 15

# Todo horário gravado no banco é UTC (datetime.now(timezone.utc), em
# banco.py e jobs.py) - correto pra guardar, mas errado pra mostrar direto:
# na sua máquina, .astimezone() sem argumento converte pro fuso do Windows
# (já configurado pra Brasília, então "funcionava"); publicado no Railway, o
# container roda em UTC, e a mesma chamada não converte NADA - o horário
# aparecia 3h adiantado. Fixar o fuso aqui, explícito, funciona igual nos
# dois lugares, não depende de qual fuso o sistema operacional do servidor
# está configurado.
FUSO_BR = ZoneInfo('America/Sao_Paulo')


def _hora_br(valor) -> str:
    """
    Formata um horário (datetime com timezone, ou string ISO) no fuso de
    Brasília, sempre - é a função ÚNICA que qualquer tela usa pra mostrar
    'quando' algo aconteceu, tanto direto do Python quanto via filtro Jinja
    ({{ valor|hora_br }}), pra nunca mais ter conversão de fuso duplicada
    (e divergente) espalhada entre rotas e templates.
    """
    if not valor:
        return ''
    try:
        momento = valor if isinstance(valor, _datetime) else _datetime.fromisoformat(valor)
    except (ValueError, TypeError):
        return str(valor)
    if momento.tzinfo is None:
        # Registro antigo, gravado antes desta correção, sem timezone
        # explícito no texto - o código sempre gravou em UTC, então assume
        # isso em vez de tratar como se já fosse hora de Brasília.
        momento = momento.replace(tzinfo=_timezone.utc)
    return momento.astimezone(FUSO_BR).strftime('%d/%m %H:%M')

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
app.jinja_env.filters['hora_br'] = _hora_br


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
        maqs = banco.listar_maquinas(so_ativas=True)
        principal = maqs[0] if maqs else None
        return {'nav': {
            'pendencias': ((res['pecas'] - res['pecas_confirmadas'])
                            + (res['cores'] - res['cores_confirmadas'])),
            'chapa': (f"{principal['chapa_larg']}×{principal['chapa_alt']}"
                       if principal else f"{par['chapa_larg']}×{par['chapa_alt']}"),
            'kerf': str(principal['kerf'] if principal else par['kerf']).replace('.', ','),
        }}
    except Exception:                    # banco ainda não existe no primeiro acesso
        return {'nav': None}


@app.route('/')
def inicio():
    """
    Painel de bater o olho.

    Existe porque a primeira tela era o formulario de upload, e quem abria o
    sistema nao tinha como saber em que pe as coisas estavam - o que faltava
    conferir, se havia plano esperando o PCP, quantas maquinas ativas.
    """
    res = banco.resumo()
    planos = banco.listar_planos(limite=6)
    return render_template('inicio.html', pagina='inicio', res=res,
                            maquinas=banco.listar_maquinas(so_ativas=True),
                            planos=planos,
                            a_conferir=sum(1 for p in planos if not p['aprovado']),
                            modelos=banco.listar_modelos(),
                            sem_modelo=banco.contar_sem_modelo(),
                            acabamentos=banco.listar_acabamentos())


@app.route('/novo')
def index():
    return render_template('index.html', pagina='novo', res=banco.resumo(),
                            maquinas=banco.listar_maquinas(so_ativas=True))


@app.route('/planos')
def planos():
    """Os que ainda estão calculando vêm da memória; o resto, do banco."""
    lista = []
    for j in sorted(jobs.todos(), key=lambda j: j.criado_em, reverse=True):
        if j.estado in ('na_fila', 'rodando', 'erro'):
            lista.append({'id': j.id, 'estado': j.estado, 'pct': j.pct,
                           'quando': _hora_br(j.criado_em),
                           'arquivos': None, 'chapas': None, 'aprovado': False})
    for p in banco.listar_planos():
        lista.append({'id': p['id'], 'estado': 'pronto', 'pct': 100,
                       'quando': _hora_br(p['criado_em']), 'arquivos': p['arquivos'],
                       'chapas': p['total_chapas'], 'aprovado': bool(p['aprovado']),
                       'aprovado_por': p['aprovado_por']})
    return render_template('planos.html', pagina='planos', planos=lista)


@app.route('/planos/<plano_id>/excluir', methods=['POST'])
def excluir_plano(plano_id):
    """
    Apaga o plano: registro no banco + imagens/PDF em disco + os Kambans
    originais que foram enviados pra gerar ele. Só remove o histórico do
    cálculo - se a chapa já foi cortada de verdade, a produção em si não
    depende deste registro pra ter acontecido.
    """
    apagou = banco.excluir_plano(plano_id)
    if not apagou:
        return jsonify({'ok': False, 'erro': 'Plano não encontrado.'}), 404

    pasta = os.path.join(OUTPUT_DIR, plano_id)
    if os.path.isdir(pasta):
        shutil.rmtree(pasta, ignore_errors=True)
    for caminho in glob.glob(os.path.join(UPLOAD_DIR, f'{plano_id}_*')):
        try:
            os.remove(caminho)
        except OSError:
            pass
    return jsonify({'ok': True})


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


@app.route('/parametros')
def parametros():
    """
    Vestígio: chapa, serra e empilhamento passaram a ser de cada MÁQUINA.

    Manter as duas telas era a maior fonte de confusão do sistema — pareciam
    editar a mesma coisa e não editavam. Fica só o redirecionamento, para
    quem tiver o endereço salvo.
    """
    return redirect(url_for('maquinas'))


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
            job_id=job_id,
            ao_terminar=lambda j: (banco.salvar_plano(j.id, j.resultado)
                                    if j.resultado and not j.resultado.get('erro') else None),
        )
        criados.append(job.id)

    if not criados:
        return redirect(url_for('index'))
    if len(criados) == 1:
        return redirect(url_for('acompanhar', job_id=criados[0]))
    return redirect(url_for('planos'))


@app.route('/manual/novo', methods=['GET', 'POST'])
def manual_novo():
    """
    Ponto de partida do plano manual: escolhe a máquina (pra saber o
    tamanho da chapa, a serra e a pilha) e o sistema cria uma chapa em
    branco pra montar o padrão à mão - pra um pedido avulso que não veio de
    Kambam nenhum, ou pra testar uma combinação de peças antes de rodar de
    verdade.

    Não pergunta espessura: o cadastro da máquina já basta. ESPESSURA_PADRAO_MM
    é a chapa que passa nessa seccionadora na prática (a mesma conta que o
    dono do sistema fez de cabeça: 120mm de pilha ÷ 15mm = 8 chapas) - se um
    dia a fábrica passar a rodar espessura diferente nesse plano, é aqui que
    se ajusta, não pedindo pra digitar de novo em cada chapa manual.
    """
    maquinas = banco.listar_maquinas(so_ativas=True)
    if request.method == 'GET':
        return render_template('manual_novo.html', pagina='novo_manual', maquinas=maquinas,
                                espessura_padrao=ESPESSURA_PADRAO_MM)

    maq = banco.maquina(request.form.get('maquina_id', type=int))
    if not maq:
        return render_template('manual_novo.html', pagina='novo_manual', maquinas=maquinas,
                                espessura_padrao=ESPESSURA_PADRAO_MM,
                                erro='Escolha uma máquina.')

    # Mesma conta do fluxo automático (pipeline.py): quantas chapas dessa
    # espessura cabem dentro do limite de empilhamento já cadastrado na
    # máquina - só que aqui a espessura é a padrão da fábrica, não perguntada.
    pilha = max(1, int(maq['pilha_max'] // ESPESSURA_PADRAO_MM))

    # Sem cor pra consultar aqui (o plano manual não vem de um Kambam com
    # material definido) - "respeitar o veio" vira a própria decisão: ligado
    # trata a chapa como se tivesse veio (o lado conservador, igual cor sem
    # cadastro no plano automático - pipeline.tem_veio cai em True); desligado
    # libera girar qualquer peça, só pra comparar quanto o veio custaria.
    respeitar_veio = request.form.get('respeitar_veio') is not None
    veio = pipeline.tem_veio('', respeitar_veio, {})

    plano_id = os.urandom(5).hex()
    grupo = {
        'cor': 'Plano manual', 'esp': ESPESSURA_PADRAO_MM, 'tem_veio': veio,
        'n_tipos_peca': 0, 'qtd_total_pecas': 0, 'n_chapas': 0, 'n_padroes': 1,
        'ciclos_total': 0, 'aproveitamento_medio': 0,
        'padroes': [{
            'n': 1, 'itens': [], 'arquivo': f'/plano/{plano_id}/manual_padrao1.png',
            'repeticoes': 1, 'aproveitamento': 0, 'ciclos': 1, 'pilha': pilha, 'pecas': [],
        }],
        'sobras': [],
    }
    resultado = {
        'erro': None, 'manual': True, 'kambans_info': [],
        'sheet_w': maq['chapa_larg'], 'sheet_h': maq['chapa_alt'], 'kerf': maq['kerf'],
        'estagios': maq['estagios'], 'respeitar_veio': respeitar_veio,
        'maquina': maq['nome'], 'maquina_id': maq['id'], 'pilha_max': maq['pilha_max'],
        'total_chapas': 0, 'sugestoes': [], 'importado': {}, 'cadastro': banco.resumo(),
        'grupos': [grupo],
    }
    _redesenhar(plano_id, resultado, grupo, grupo['padroes'][0])
    banco.salvar_plano(plano_id, resultado)
    return redirect(url_for('manual_editor', plano_id=plano_id))


@app.route('/manual/<plano_id>/pecas')
def manual_pecas(plano_id):
    """
    Busca de peças pro arrastar-e-soltar: devolve o cadastro puro (código,
    descrição, medida), sem filtrar por sobra nenhuma - o encaixe em cada
    espaço é conferido no navegador enquanto arrasta, e de novo no servidor
    quando solta, então esta lista não precisa saber onde a peça vai cair.
    """
    busca = request.args.get('busca', '').strip()
    itens = [{'cod': p['cod'], 'desc': p['descricao'], 'comp': p['comp_mm'], 'larg': p['larg_mm'],
              'aparente': bool(p['aparente'])}
             for p in banco.listar_pecas(busca, limite=200)]
    return jsonify(itens)


@app.route('/manual/<plano_id>')
def manual_editor(plano_id):
    """A chapa em branco pra arrastar peça, com o mesmo motor de encaixe/validação
    de guilhotina da edição normal - só a tela é outra."""
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    if not r.get('manual'):
        abort(404)
    grupo = r['grupos'][0]
    padrao = grupo['padroes'][0]
    return _sem_cache(make_response(render_template(
        'manual.html', pagina='novo_manual', plano=salvo, r=r, grupo=grupo, padrao=padrao,
        erro=request.args.get('erro'))))


@app.route('/manual/<plano_id>/quantidades', methods=['GET', 'POST'])
def manual_quantidades(plano_id):
    """
    Última etapa: você diz quanto precisa de cada peça que colocou na
    chapa, e o sistema calcula quantas vezes repetir o padrão. É a mesma
    conta de sempre - quantas chapas até a peça mais exigente do padrão
    fechar a quantidade pedida - só que partindo de um padrão desenhado à
    mão em vez de vindo do Kambam.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    if not r.get('manual'):
        abort(404)
    grupo = r['grupos'][0]
    padrao = grupo['padroes'][0]

    from collections import Counter
    contagem = Counter(it['cod'] for it in padrao['itens'])
    descricoes = {it['cod']: it['desc'] for it in padrao['itens']}
    # Se a página já foi calculada antes (reabrindo pra editar), padrao['pecas']
    # guarda a quantidade desejada da última vez em 'lotes' - sem isso, reabrir
    # pra ajustar um número já apagava todos os outros, forçando digitar tudo
    # de novo em vez de só corrigir o que mudou.
    anteriores = {item['cod']: item.get('lotes', {}).get('Plano manual', 0)
                  for item in padrao.get('pecas') or []}
    pecas_no_padrao = [{'cod': cod, 'desc': descricoes[cod], 'por_chapa': qtd,
                         'desejado_anterior': anteriores.get(cod, 0)}
                        for cod, qtd in sorted(contagem.items())]

    if not pecas_no_padrao:
        return redirect(url_for('manual_editor', plano_id=plano_id,
                                 erro='Posicione ao menos uma peça antes de calcular.'))

    if request.method == 'GET':
        return render_template('manual_quantidades.html', pagina='novo_manual',
                                plano=salvo, r=r, padrao=padrao, pecas=pecas_no_padrao)

    desejado = {}
    for p in pecas_no_padrao:
        try:
            q = int(request.form.get(f'qtd_{p["cod"]}', '0') or '0')
        except ValueError:
            q = 0
        if q > 0:
            desejado[p['cod']] = q
    if not desejado:
        return render_template('manual_quantidades.html', pagina='novo_manual',
                                plano=salvo, r=r, padrao=padrao, pecas=pecas_no_padrao,
                                erro='Informe a quantidade de pelo menos uma peça.')

    # Quantas vezes repetir a chapa até a peça mais exigente do padrao
    # fechar: se o padrao tem 3 peças X e voce quer 100, precisa de 34
    # chapas (arredondado pra cima - sobra é normal, chapa se corta inteira).
    padrao['repeticoes'] = max(-(-desejado[cod] // contagem[cod]) for cod in desejado)
    padrao['ciclos'] = -(-padrao['repeticoes'] // padrao.get('pilha', 1))
    padrao['pecas'] = _pecas_do_padrao(padrao)
    for item in padrao['pecas']:
        item['lotes'] = {'Plano manual': desejado.get(item['cod'], 0)}
    padrao['pecas_exibicao'] = padrao['pecas']
    grupo['n_tipos_peca'] = len(padrao['pecas'])
    grupo['qtd_total_pecas'] = sum(item['total'] for item in padrao['pecas'])
    grupo['ciclos_total'] = padrao['ciclos']

    _redesenhar(plano_id, r, grupo, padrao)
    _recalcular(r)
    _conferir_demanda(r)
    banco.atualizar_resultado(plano_id, r)
    return redirect(url_for('resultado', job_id=plano_id))


@app.route('/maquinas')
def maquinas():
    return render_template('maquinas.html', pagina='maquinas',
                            maquinas=banco.listar_maquinas(),
                            disco=diagnostico_armazenamento())


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
    # endereços antigos: as telas viraram abas dentro de /cadastros
    if aba in ('pecas', 'cores'):
        return redirect(url_for('cadastros', aba='pecas' if aba == 'pecas' else 'chapas'))
    return _cadastro_antigo(aba)


def _cadastro_antigo(aba):
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


@app.route('/cadastros/')
@app.route('/cadastros/<aba>')
def cadastros(aba='moveis'):
    """
    Uma area so para os quatro cadastros, trocando por ABAS.

    Antes eram quatro itens soltos no menu e a peca podia ser editada em tres
    telas diferentes - o "vai e vem" de que reclamaram. Como sao assuntos
    irmaos, abas deixam claro que fazem parte do mesmo lugar e qual esta
    aberta.
    """
    res = banco.resumo()
    contas = {'moveis': len(banco.listar_modelos()), 'pecas': res['pecas'],
               'chapas': res['cores'], 'acabamentos': len(banco.listar_acabamentos())}
    comum = {'pagina': 'cadastros', 'aba': aba, 'contas': contas, 'res': res}

    # Sem no-store, sair da tela e voltar (trocar de aba, botao Voltar) pode
    # reaproveitar esta pagina do cache do navegador - e como aqui e ONDE se
    # edita e exclui cadastro, isso mostra uma contagem e uma lista que ja
    # nao existem mais no banco, dando a impressao de que a exclusao "nao
    # pegou" quando na verdade so a TELA que esta desatualizada.
    if aba == 'moveis':
        html = render_template('cad_moveis.html', modelos=banco.listar_modelos(),
                                sem_modelo=banco.contar_sem_modelo(), **comum)
    elif aba == 'pecas':
        busca = request.args.get('busca', '').strip()
        pendentes = request.args.get('pendentes') == '1'
        html = render_template('cad_pecas.html', busca=busca, pendentes=pendentes,
                                itens=banco.listar_pecas(busca, so_pendentes=pendentes,
                                                          limite=300),
                                efemero=_disco_efemero(), **comum)
    elif aba == 'chapas':
        html = render_template('cad_chapas.html', itens=banco.listar_cores(), **comum)
    elif aba == 'acabamentos':
        html = render_template('cad_acabamentos.html',
                                acabamentos=banco.listar_acabamentos(),
                                cores=[c['nome'] for c in banco.listar_cores()], **comum)
    else:
        abort(404)
    return _sem_cache(make_response(html))
    abort(404)


def _disco_efemero() -> bool:
    d = diagnostico_armazenamento()
    return bool(d['em_nuvem'] and not d['permanente'])


@app.route('/modelos')
def modelos():
    return redirect(url_for('cadastros', aba='moveis'))


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
    return redirect(url_for('cadastros', aba='acabamentos'))


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


def _impacto_exclusao(tipo: str, ident: str):
    """
    O que se perde ao excluir este registro, em número e em frase pronta pra
    mostrar num confirm(). Existe pra quem clica em excluir NUNCA ser
    surpreendido depois - a pergunta já vem com a consequência.
    """
    if tipo == 'peca':
        imp = banco.impacto_exclusao_peca(ident)
        n = imp['vinculos_modelo']
        msg = (f'Esta peça está vinculada a {n} móvel(is). O vínculo será removido '
               f'(o(s) móvel(is) em si continua(m) cadastrado(s)).' if n else
               'Esta peça não está vinculada a nenhum móvel.')
        return imp, msg
    if tipo == 'cor':
        imp = banco.impacto_exclusao_cor(ident)
        n = imp['vinculos_acabamento']
        msg = (f'Esta cor está vinculada a {n} acabamento(s). O vínculo será removido.' if n
               else 'Esta cor não está vinculada a nenhum acabamento.')
        return imp, msg
    if tipo == 'modelo':
        imp = banco.impacto_exclusao_modelo(ident)
        n = imp['vinculos_peca']
        msg = (f'Este móvel tem {n} peça(s) na lista técnica. O vínculo será removido '
               f'(as peças em si não são apagadas).' if n else
               'Este móvel não tem peças vinculadas.')
        return imp, msg
    if tipo == 'acabamento':
        imp = banco.impacto_exclusao_acabamento(ident)
        n = imp['vinculos_cor']
        msg = (f'Este acabamento usa {n} cor(es) de chapa. O vínculo será removido.' if n
               else 'Este acabamento não tem cor vinculada.')
        return imp, msg
    return None, None


@app.route('/cadastro/impacto', methods=['POST'])
def cadastro_impacto():
    """O que vai junto se este registro for excluído - pra montar o aviso ANTES de apagar."""
    d = request.get_json(silent=True) or {}
    tipo, ident = d.get('tipo'), d.get('id')
    if not tipo or not ident:
        return jsonify({'ok': False}), 400
    imp, msg = _impacto_exclusao(tipo, str(ident))
    if imp is None:
        return jsonify({'ok': False}), 400
    return jsonify({'ok': True, 'impacto': imp, 'mensagem': msg})


@app.route('/cadastro/excluir', methods=['POST'])
def cadastro_excluir():
    d = request.get_json(silent=True) or {}
    tipo, ident = d.get('tipo'), d.get('id')
    if not tipo or not ident:
        return jsonify({'ok': False}), 400
    ident = str(ident)
    if tipo == 'peca':
        apagou = banco.excluir_peca(ident)
    elif tipo == 'cor':
        apagou = banco.excluir_cor(ident)
    elif tipo == 'modelo':
        apagou = banco.excluir_modelo(ident)
    elif tipo == 'acabamento':
        apagou = banco.excluir_acabamento(ident)
    else:
        return jsonify({'ok': False}), 400
    # apagou=False significa que o id não existia mais - quem chamou (o
    # navegador) tira a linha da tela mesmo assim, mas melhor a resposta
    # dizer a verdade do que fingir sucesso quando não apagou nada.
    if not apagou:
        return jsonify({'ok': False, 'erro': 'já não existia'}), 404
    return jsonify({'ok': True})


@app.route('/cadastro/editar', methods=['POST'])
def cadastro_editar():
    d = request.get_json(silent=True) or {}
    tipo, ident = d.get('tipo'), d.get('id')
    if not tipo or not ident:
        return jsonify({'ok': False}), 400
    ident = str(ident)
    if tipo == 'peca':
        erros = banco.editar_peca(ident, d.get('descricao'), d.get('comp_mm'), d.get('larg_mm'))
    elif tipo == 'modelo':
        erros = banco.editar_modelo(ident, d.get('descricao'))
    else:
        return jsonify({'ok': False}), 400
    if erros:
        return jsonify({'ok': False, 'erros': erros}), 400
    return jsonify({'ok': True})


def _sem_cache(resposta):
    """
    Impede o navegador de mostrar uma versão velha desta tela.

    Sem isso, trocar de aba e voltar (ou usar o botão Voltar) pode restaurar
    a página exatamente como estava - inclusive o aviso de "lote não fecha" -
    de antes da última edição, porque o navegador nunca foi instruído a não
    guardar essa resposta. O aviso está certo no servidor; o problema é o
    navegador não perguntar de novo.
    """
    resposta.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resposta


@app.route('/resultado/<job_id>')
def resultado(job_id):
    # o plano gravado é a fonte da verdade; a memória só cobre o que ainda
    # está calculando nesta execução do servidor
    salvo = banco.obter_plano(job_id)
    if salvo:
        # a conferencia e calculada na hora de mostrar: assim vale tambem pro
        # plano recem-calculado, onde o arredondamento do inteiro ja produz
        # um pouco a mais do que o Kambam pediu
        _conferir_demanda(salvo['resultado'])
        maq = banco.maquina(salvo['resultado'].get('maquina_id'))
        return _sem_cache(make_response(render_template(
            'resultado.html', plano=salvo,
            reeq=request.args.get('reeq'),
            motivo_reeq=request.args.get('motivo'),
            extra_ok=request.args.get('extra_ok', type=int),
            extra_erro=request.args.get('extra_erro'),
            pct_extra=float(maq['pct_extra']) if maq and maq['pct_extra'] else 0.0,
            **salvo['resultado'])))
    job = jobs.obter(job_id) or abort(404)
    if job.estado == 'erro':
        return render_template('resultado.html', erro=job.erro, kambans_info=None), 500
    if job.estado != 'pronto':
        return redirect(url_for('acompanhar', job_id=job_id))
    maq = banco.maquina(job.resultado.get('maquina_id'))
    return _sem_cache(make_response(render_template(
        'resultado.html', pct_extra=float(maq['pct_extra']) if maq and maq['pct_extra'] else 0.0,
        **job.resultado)))


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
        # Pedido e produção lado a lado, sempre recalculados. Antes a etiqueta
        # mostrava só a demanda do momento do cálculo e ficava obsoleta depois
        # de qualquer edição — número velho na tela é pior que número nenhum.
        g['total_pedido'] = sum(_demanda_do_grupo(g).values())
        g['total_produz'] = sum(_producao_do_grupo(g).values())
    resultado['total_chapas'] = sum(g['n_chapas'] for g in resultado['grupos'])


def _demanda_do_grupo(g: dict) -> dict:
    """
    O que este grupo precisa produzir, por codigo de peca.

    Comeca no que os Kambans pediram, congelado no momento do calculo, e
    aplica os ajustes que voce aceitou depois. E por isso que a demanda muda
    nos DOIS sentidos: aceitar um acrescimo sobe o alvo, aceitar uma retirada
    desce - em vez de o plano ficar eternamente "errado" contra um numero que
    nao vale mais.
    """
    pedido = {}
    for pad in g.get('padroes', []):
        for pc in pad.get('pecas', []):
            pedido[pc['cod']] = pedido.get(pc['cod'], 0) + sum((pc.get('lotes') or {}).values())
    for cod, delta in (g.get('ajustes') or {}).items():
        pedido[cod] = max(0, pedido.get(cod, 0) + delta)
    return pedido


def _producao_do_grupo(g: dict) -> dict:
    """Quantas pecas de cada codigo o plano produz hoje, com a geometria atual."""
    produzido = {}
    for pad in g.get('padroes', []):
        for it in pad.get('itens', []):
            produzido[it['cod']] = produzido.get(it['cod'], 0) + pad['repeticoes']
    return produzido


def _pecas_do_padrao(pad: dict) -> list[dict]:
    """
    Refaz a tabela PEÇA/MEDIDA/POR CHAPA/TOTAL a partir da geometria atual
    (pad['itens']), para exibição depois de uma edição manual.

    NÃO mexe em pad['pecas']: aquele campo fica congelado como veio do
    cálculo original porque é dali que _demanda_do_grupo tira o "pedido"
    (via pc['lotes']) - sobrescrever apagaria o rateio por Kambam e zeraria
    a demanda de qualquer padrão editado. Por isso esta é uma tabela NOVA,
    só para mostrar o que está na chapa agora; o destino por lote não dá
    para recompor aqui (peça acrescentada à mão não tem lote de origem).
    """
    contagem: dict[str, dict] = {}
    for it in pad.get('itens', []):
        c = contagem.setdefault(it['cod'], {'desc': it['desc'], 'w': it['w'], 'h': it['h'], 'qtd': 0})
        c['qtd'] += 1
    saida = []
    for cod, info in sorted(contagem.items(), key=lambda kv: -kv[1]['qtd']):
        saida.append({'cod': cod, 'desc': info['desc'],
                       'medida': f"{int(round(info['w']))}x{int(round(info['h']))}",
                       'por_chapa': info['qtd'], 'total': info['qtd'] * pad['repeticoes']})
    return saida


def _conferir_demanda(resultado):
    """
    Compara o que o plano produz com o que precisa produzir.

    O sistema NAO equaliza sozinho: nao tira peca para fechar a conta nem
    reescreve o pedido. Ele mostra a diferenca e deixa a decisao com voce,
    porque as duas saidas sao legitimas e so quem conhece o lote sabe qual
    vale - aceitar a mudanca, ou reequilibrar o plano para voltar ao alvo.

    Sobra pequena e normal mesmo sem edicao: chapa se corta inteira, entao o
    ultimo padrao quase sempre produz um pouco a mais.
    """
    for g in resultado['grupos']:
        pedido = _demanda_do_grupo(g)
        produzido = _producao_do_grupo(g)
        # FALTA e EXCEDENTE não são a mesma gravidade e não podem aparecer
        # misturados: falta significa que o lote não fecha e alguém vai
        # descobrir na montagem; excedente é inerente a cortar chapa inteira e
        # acontece em quase todo plano, mesmo sem ninguém editar nada. Juntar
        # os dois faz o normal parecer erro e o erro passar despercebido.
        g['conferencia'] = []
        g['faltas'] = []
        g['excedentes'] = []
        for cod in sorted(set(pedido) | set(produzido)):
            p, q = pedido.get(cod, 0), produzido.get(cod, 0)
            if p == q:
                continue
            linha = {'cod': cod, 'pedido': p, 'produz': q, 'dif': q - p}
            g['conferencia'].append(linha)
            (g['faltas'] if q < p else g['excedentes']).append(linha)
        g['total_pedido'] = sum(pedido.values())
        g['total_produz'] = sum(produzido.values())
        # nome antigo, ainda usado pela tela de edicao
        g['diferencas'] = [{'cod': c['cod'], 'dif': c['dif']} for c in g['conferencia']]


def _linhas_planejado_produzido(resultado: dict) -> list[dict]:
    """
    Junta a conferencia peca-a-peca de TODOS os grupos do plano numa lista
    so, com descricao/medida do cadastro anexadas - e, ao contrario de
    g['conferencia'] (que so guarda DIFERENCA, pra nao poluir a tela de
    edicao com linha que bate certinho), aqui entram TAMBEM as pecas que
    fecham em cima do pedido: os relatorios do plano inteiro (Planejado x
    Produzido, Pecas Extras) precisam do total certo, nao so das excecoes.

    Vale pra plano automatico e manual igual - os dois passam pela mesma
    _demanda_do_grupo/_producao_do_grupo, entao nao tem nada especifico de
    um ou outro aqui.
    """
    catalogo = {p['cod']: p for p in banco.listar_pecas(limite=5000)}
    linhas = []
    for g in resultado.get('grupos', []):
        pedido = _demanda_do_grupo(g)
        produzido = _producao_do_grupo(g)
        for cod in sorted(set(pedido) | set(produzido)):
            p, q = pedido.get(cod, 0), produzido.get(cod, 0)
            info = catalogo.get(cod)
            linhas.append({
                'cod': cod,
                'desc': info['descricao'] if info else '(fora do cadastro)',
                'cor': g.get('cor'), 'esp': g.get('esp'),
                'medida': f"{info['comp_mm']}×{info['larg_mm']}" if info else '—',
                'pedido': p, 'produzido': q, 'dif': q - p,
            })
    return linhas


def _rebalancear_grupo(g: dict) -> dict:
    """
    Recalcula QUANTAS chapas de cada padrao, para bater com a demanda atual.

    Os padroes ficam como estao - inclusive o que voce editou a mao. O que
    muda e a repeticao de cada um. E por isso que editar passa a valer de
    verdade: acrescentar uma peca num padrao pode deixar outro padrao rodar
    menos vezes, e aI o lote inteiro sai com menos chapa.
    """
    from types import SimpleNamespace
    from collections import Counter
    from column_generation import _solve_master_ip

    padroes = [p for p in g.get('padroes', []) if p.get('itens')]
    if not padroes:
        return {'ok': False, 'motivo': 'Este grupo nao tem geometria gravada para recalcular.'}

    demanda = _demanda_do_grupo(g)
    demanda = {c: q for c, q in demanda.items() if q > 0}
    if not demanda:
        return {'ok': False, 'motivo': 'A demanda deste grupo ficou zerada.'}

    modelo = [SimpleNamespace(counts=Counter(it['cod'] for it in p['itens'])) for p in padroes]
    # so cabe exigir peca que algum padrao saiba produzir
    produziveis = {c for m in modelo for c in m.counts}
    faltantes = [c for c in demanda if c not in produziveis]
    alvo = {c: q for c, q in demanda.items() if c in produziveis}
    if not alvo:
        return {'ok': False, 'motivo': 'Nenhum padrao atual produz as pecas pedidas.'}

    usos = _solve_master_ip(modelo, alvo, list(alvo), time_limit_s=20.0)
    if usos is None:
        return {'ok': False, 'motivo': 'Nao consegui fechar uma combinacao no tempo disponivel.'}

    antes = sum(p['repeticoes'] for p in padroes)
    for p, n in zip(padroes, usos):
        p['repeticoes'] = int(n)
    g['padroes'] = [p for p in padroes if p['repeticoes'] > 0]
    depois = sum(p['repeticoes'] for p in g['padroes'])
    return {'ok': True, 'antes': antes, 'depois': depois, 'faltantes': faltantes}


def _pode_girar_aqui(peca, grupo):
    """Peca so e obrigada a manter a orientacao se a cor tem veio E ela aparece."""
    return not (grupo.get('tem_veio') and peca['aparente'])


def _melhor_encaixe_valido(base_item, comp, larg, kerf, pode_girar, itens_ocupados,
                            sheet_w, sheet_h, max_estagios, forcar_giro=None):
    """
    Acha, entre os espacos livres, um encaixe pra peca que TAMBEM preserva a
    guilhotina - nao so o que sobra menos.

    edicao.melhor_encaixe_em_retalhos escolhe so a sobra com menos
    desperdicio, mas essa pode quebrar o corte em estagios demais enquanto
    outra sobra, geometricamente pior, continua cortavel. Por isso aqui a
    busca tenta cada sobra livre em ordem de menos pra mais desperdicio e
    para na primeira que passa nas duas checagens (cabe E continua
    guilhotinavel) - sem isso, "não achei onde encaixar" às vezes queria
    dizer só "o primeiro lugar que tentei não serviu".

    forcar_giro repassa pra edicao.encaixar: None deixa cada sobra escolher
    a orientacao que couber, True/False so aceita a peca naquela orientacao
    especifica - e o que o arraste com "virar ao soltar" marcado precisa,
    pra buscar em toda a chapa sem trocar a orientacao que a pessoa pediu.
    """
    livres = edicao.retalhos_livres(itens_ocupados, sheet_w, sheet_h, kerf=kerf)
    candidatas = []
    for retalho in livres:
        enc = edicao.encaixar(retalho, comp, larg, kerf, pode_girar, forcar_giro=forcar_giro)
        if enc:
            sobra = retalho.w * retalho.h - enc['w'] * enc['h']
            candidatas.append((sobra, enc))
    candidatas.sort(key=lambda c: c[0])
    for _, enc in candidatas:
        candidato = {**base_item, **enc}
        if not edicao.validar_padrao(itens_ocupados + [candidato], sheet_w, sheet_h, max_estagios):
            return candidato
    return None


def _contido(retalho, original) -> bool:
    """Se `retalho` está inteiramente dentro dos limites de `original` -
    usado pra saber se uma sobra recalculada depois de um encaixe ainda é
    "o mesmo espaço" que a pessoa escolheu, e não outro pedaço qualquer da
    chapa que sobrou de coincidência com o mesmo tamanho."""
    return (retalho.x >= original.x - 0.5 and retalho.y >= original.y - 0.5 and
            retalho.x + retalho.w <= original.x + original.w + 0.5 and
            retalho.y + retalho.h <= original.y + original.h + 0.5)


def _encaixar_repetido_no_retalho(base_item, comp, larg, kerf, pode_girar, itens_ocupados,
                                   sheet_w, sheet_h, max_estagios, retalho_alvo, quantidade):
    """
    Encaixa até `quantidade` cópias da mesma peça, todas dentro do MESMO
    espaço que a pessoa escolheu na tela - nunca em outro lugar da chapa.

    edicao.encaixar sempre ancora a peça no canto (x,y) do retalho recebido;
    depois de encaixar uma, o que sobra desse mesmo espaço vira uma ou mais
    sobras novas na lista recalculada por edicao.retalhos_livres. A cada
    passo, considera TODAS as sobras que ainda estão CONTIDAS dentro do
    espaço ORIGINAL escolhido (`_contido` contra `retalho_alvo`, sempre o
    mesmo - nunca contra a última sobra tentada), da maior pra menor, e
    para na primeira que aceita a peça. Sem isso (versão anterior, contra
    a última sobra): depois de encher uma linha inteira lado a lado, a
    última fatia que sobra no fim dela costuma ficar estreita demais pra
    peça - e o laço desistia ali, mesmo com o resto do espaço original
    (a próxima linha inteira, por exemplo) ainda livre e maior que a fatia
    estreita que acabou de falhar.
    """
    itens = list(itens_ocupados)
    incluidos = []
    while len(incluidos) < quantidade:
        livres = edicao.retalhos_livres(itens, sheet_w, sheet_h, kerf=kerf)
        candidatos = sorted((s for s in livres if _contido(s, retalho_alvo)),
                            key=lambda s: -(s.w * s.h))
        colocado = False
        for alvo in candidatos:
            enc = edicao.encaixar(alvo, comp, larg, kerf, pode_girar)
            if not enc:
                continue
            candidato = {**base_item, **enc}
            if edicao.validar_padrao(itens + [candidato], sheet_w, sheet_h, max_estagios):
                continue  # esse encaixe especifico quebraria a guilhotina - tenta o proximo
            itens.append(candidato)
            incluidos.append(candidato)
            colocado = True
            break
        if not colocado:
            break
    return incluidos


def _preencher_padroes_extra(padroes, catalogo, orcamento_fisico, pode_girar_por_cod,
                              kerf, sheet_w, sheet_h, max_estagios):
    """
    Enche os espacos livres dos padroes com pecas extras dos codigos que
    ainda tem orcamento - overproducao deliberada pra subir o aproveitamento
    depois que o pedido ja fecha, nunca no lugar dele.

    O orcamento (fisico, ja multiplicado pelas repeticoes) e COMPARTILHADO
    entre todos os padroes do grupo e MUTADO conforme cada peca entra: quem
    processa primeiro consome o credito primeiro, entao a ordem de `padroes`
    importa (mesma escolha do projeto de referencia que originou essa ideia -
    tentar redistribuir por "quem esta mais fraco" foi medido e piorou o
    pior caso, ver o comentario de `preencher_padroes_fracos` no material
    estudado).

    A cada passo, tenta TODOS os codigos com credito em TODOS os espacos
    livres do padrao e fica com o encaixe que sobra menos espaco (best-fit
    global) - repete ate nao caber mais nada ou o credito acabar. Devolve o
    total fisico de pecas extras adicionadas nesta chamada.
    """
    total_fisico = 0
    for pad in padroes:
        itens_originais = pad.get('itens') or []
        if not itens_originais or not any(v > 0 for v in orcamento_fisico.values()):
            continue
        reps = max(1, pad.get('repeticoes', 1))
        itens = list(itens_originais)
        progresso = True
        while progresso:
            progresso = False
            livres = edicao.retalhos_livres(itens, sheet_w, sheet_h, kerf=kerf)
            if not livres:
                break
            melhor = None
            for cod, restante_fisico in orcamento_fisico.items():
                if restante_fisico < reps:
                    continue
                peca = catalogo.get(cod)
                if peca is None:
                    continue
                pode_girar = pode_girar_por_cod.get(cod, True)
                for retalho in livres:
                    enc = edicao.encaixar(retalho, peca['comp_mm'], peca['larg_mm'], kerf, pode_girar)
                    if enc is None:
                        continue
                    sobra = retalho.w * retalho.h - enc['w'] * enc['h']
                    if melhor is None or sobra < melhor[0]:
                        melhor = (sobra, enc, cod, peca)
            if melhor is None:
                break
            _, enc, cod, peca = melhor
            candidato = {'cod': cod, 'desc': peca['descricao'], 'piece_key': f'{cod}_extra',
                         'shelf': 0, 'extra': True, **enc}
            tentativa = itens + [candidato]
            if edicao.validar_padrao(tentativa, sheet_w, sheet_h, max_estagios):
                # esse encaixe especifico quebraria o corte guilhotina - desiste
                # dessa peca NESTE padrao (evita repetir a mesma tentativa invalida
                # pra sempre), mas o credito dela continua de pe pros outros padroes.
                orcamento_fisico[cod] = min(orcamento_fisico[cod], reps - 1)
                continue
            itens = tentativa
            orcamento_fisico[cod] -= reps
            total_fisico += reps
            progresso = True
        if len(itens) != len(itens_originais):
            pad['itens'] = itens
            pad['editado'] = True
    return total_fisico


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
    # O arquivo é regravado com o MESMO nome, então a URL da imagem não muda -
    # e o navegador serve a versão em cache em vez de buscar a nova. Sem isso,
    # a chapa editada só aparecia atualizada depois de um F5 forçado (o PDF
    # nunca sofria disso porque desenha direto dos dados, sem passar pelo PNG).
    padrao['imagem_versao'] = padrao.get('imagem_versao', 0) + 1


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>')
def editar_padrao(plano_id, gi, pi):
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None or 'itens' not in padrao:
        abort(404)
    livres = edicao.retalhos_livres(padrao['itens'], r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
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
    return _sem_cache(make_response(render_template(
        'editar.html', pagina='planos', plano=salvo, r=r,
        grupo=grupo, padrao=padrao, gi=gi, pi=pi, livres=livres,
        escolhido=escolhido, busca=busca, candidatas=candidatas[:80],
        erro=request.args.get('erro'), ok=request.args.get('ok'), aviso=request.args.get('aviso'),
        estagios_atuais=edicao.estagios(padrao['itens'],
                                         r['sheet_w'], r['sheet_h']),
        cortes=edicao.sequencia_cortes(padrao['itens'], r['sheet_w'],
                                        r['sheet_h'], r['kerf']))))


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>/dados')
def dados_padrao(plano_id, gi, pi):
    """Geometria do padrao pro navegador desenhar e simular na hora."""
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None or 'itens' not in padrao:
        abort(404)
    livres = edicao.retalhos_livres(padrao['itens'], r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
    return jsonify({
        'chapa': {'w': r['sheet_w'], 'h': r['sheet_h'], 'kerf': r['kerf'],
                   'estagios': r['estagios'], 'veio': bool(grupo.get('tem_veio')),
                   'cor': grupo.get('cor')},
        'itens': padrao['itens'],
        'livres': [{'x': s.x, 'y': s.y, 'w': s.w, 'h': s.h, 'area': round(s.area_m2, 2)}
                    for s in livres],
        'repeticoes': padrao['repeticoes'],
        'aproveitamento': padrao['aproveitamento'],
        'estagios_atuais': edicao.estagios(padrao['itens'], r['sheet_w'], r['sheet_h']),
    })


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>/simular', methods=['POST'])
def simular_encaixe(plano_id, gi, pi):
    """
    Responde se a peca entra naquela sobra e, quando nao entra, por que.

    Nao grava nada: existe pra tela poder mostrar o encaixe (ou o motivo da
    recusa) enquanto o operador escolhe, em vez de so depois de confirmar.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None:
        abort(404)
    d = request.get_json(silent=True) or {}
    cod = str(d.get('cod') or '').strip()
    i_ret = d.get('retalho')

    peca = next((p for p in banco.listar_pecas(cod, limite=80) if p['cod'] == cod), None)
    if peca is None:
        return jsonify({'ok': False, 'curto': 'peca desconhecida',
                         'motivo': 'O codigo ' + cod + ' nao existe no cadastro de pecas.'})
    livres = edicao.retalhos_livres(padrao['itens'], r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
    if not isinstance(i_ret, int) or not (0 <= i_ret < len(livres)):
        return jsonify({'ok': False, 'curto': 'sem sobra',
                         'motivo': 'Escolha primeiro em qual espaco da chapa a peca entra.'})

    return jsonify(edicao.diagnosticar(
        livres[i_ret], peca['comp_mm'], peca['larg_mm'], r['kerf'],
        _pode_girar_aqui(peca, grupo), padrao['itens'],
        r['sheet_w'], r['sheet_h'], r['estagios']))


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>/candidatas')
def candidatas_sobra(plano_id, gi, pi):
    """
    Todas as pecas avaliadas para uma sobra: as que cabem E as que nao cabem,
    estas com o motivo. Ver a peca recusada e o porque vale mais que uma lista
    curta so com as aprovadas.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None:
        abort(404)
    i_ret = request.args.get('retalho', type=int)
    busca = request.args.get('busca', '').strip()
    livres = edicao.retalhos_livres(padrao['itens'], r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
    if i_ret is None or not (0 <= i_ret < len(livres)):
        return jsonify({'cabem': [], 'nao_cabem': []})

    ret = livres[i_ret]
    cabem, nao = [], []
    for p in banco.listar_pecas(busca, limite=400):
        d = edicao.diagnosticar(ret, p['comp_mm'], p['larg_mm'], r['kerf'],
                                 _pode_girar_aqui(p, grupo), padrao['itens'],
                                 r['sheet_w'], r['sheet_h'], r['estagios'])
        linha = {'cod': p['cod'], 'desc': p['descricao'],
                  'comp': p['comp_mm'], 'larg': p['larg_mm'],
                  'curto': d['curto'], 'motivo': d['motivo']}
        if d['ok']:
            linha.update({'x': d['x'], 'y': d['y'], 'w': d['w'], 'h': d['h'],
                           'rotated': d['rotated']})
            cabem.append(linha)
        elif d['curto'] != 'nao cabe' or len(nao) < 40:
            nao.append(linha)
    cabem.sort(key=lambda c: -(c['w'] * c['h']))
    return jsonify({'cabem': cabem[:60], 'nao_cabem': nao[:40]})


@app.route('/resultado/<plano_id>/padrao/<int:gi>/<int:pi>/aplicar', methods=['POST'])
def aplicar_edicao(plano_id, gi, pi):
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    grupo, padrao = _localizar_padrao(r, gi, pi)
    if padrao is None or 'itens' not in padrao:
        abort(404)

    def volta(**extra):
        # A tela nova de arrastar dentro da chapa fala com esta rota via
        # fetch, nao via form classico - ela manda 'formato=json' e espera
        # {ok, erro/aviso} de volta, sem redirect nenhum, pra poder mostrar
        # o resultado sem recarregar a pagina inteira.
        if request.form.get('formato') == 'json':
            return jsonify({'ok': 'erro' not in extra, **extra})
        # O plano manual (fluxo antigo, formulario classico) reusa esta
        # mesma rota, mas quem chama e a tela de arrastar-e-soltar
        # (/manual/<id>), nao a de editar padrao comum - por isso o destino
        # do redirect vem do formulario quando presente, em vez de sempre
        # voltar pra editar_padrao.
        destino = request.form.get('voltar_para')
        if destino:
            from urllib.parse import urlencode
            sep = '&' if '?' in destino else '?'
            return redirect(destino + (sep + urlencode(extra) if extra else ''))
        return redirect(url_for('editar_padrao', plano_id=plano_id, gi=gi, pi=pi, **extra))

    itens = [dict(i) for i in padrao['itens']]
    acao = request.form.get('acao')
    peca = None
    removida = None
    livres = []
    i_ret = None
    peca_cod_registro = None
    aviso = None

    if acao == 'remover':
        idx = request.form.get('indice', type=int)
        if idx is None or not (0 <= idx < len(itens)):
            return volta(erro='Peca nao encontrada neste padrao.')
        removida = itens[idx].get('cod')
        peca_cod_registro = removida
        itens.pop(idx)
    elif acao == 'adicionar':
        i_ret = request.form.get('retalho', type=int)
        cod = (request.form.get('cod') or '').strip()
        girar_raw = request.form.get('girar')
        forcar_giro = None if girar_raw is None else (girar_raw == '1')
        livres = edicao.retalhos_livres(itens, r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
        peca = next((p for p in banco.listar_pecas(cod, limite=80) if p['cod'] == cod), None)
        if peca is None:
            return volta(erro='Codigo ' + cod + ' nao existe no cadastro de pecas.')
        if i_ret is None or not (0 <= i_ret < len(livres)):
            return volta(erro='Escolha em qual sobra a peca vai entrar.')
        enc = edicao.encaixar(livres[i_ret], peca['comp_mm'], peca['larg_mm'], r['kerf'],
                               _pode_girar_aqui(peca, grupo), forcar_giro=forcar_giro)
        if enc:
            itens.append({'cod': peca['cod'], 'desc': peca['descricao'],
                           'piece_key': peca['cod'] + '_manual', 'shelf': 0, **enc})
        elif forcar_giro is not None:
            # A caixa especifica onde a pessoa soltou nao aceita a peca NESSA
            # orientacao - mas ela pediu pra virar, nao pediu pra cair
            # exatamente ali. Antes de recusar, procura em toda a chapa por
            # outro lugar onde a peca entre virada (a mesma logica que o
            # botao de girar usa numa peca ja colocada).
            candidato = _melhor_encaixe_valido(
                {'cod': peca['cod'], 'desc': peca['descricao'], 'piece_key': peca['cod'] + '_manual', 'shelf': 0},
                peca['comp_mm'], peca['larg_mm'], r['kerf'], _pode_girar_aqui(peca, grupo), itens,
                r['sheet_w'], r['sheet_h'], r['estagios'], forcar_giro=forcar_giro)
            if candidato is None:
                orientacao = 'virada' if forcar_giro else 'sem virar'
                return volta(retalho=i_ret,
                             erro=f"A peca {peca['cod']} nao cabe {orientacao} em lugar nenhum desta chapa.")
            itens.append(candidato)
            aviso = f"A peca {peca['cod']} nao coube na sobra escolhida nessa orientacao; entrou no melhor espaco livre da chapa."
        else:
            return volta(retalho=i_ret, erro='A peca nao cabe nesta sobra considerando a folga da serra.')
        peca_cod_registro = peca['cod']
    elif acao == 'mover':
        # Reposiciona uma peca ja colocada na chapa (arraste dentro do
        # canvas) - a diferenca pra 'adicionar' e que a peca ja existe no
        # padrao, so a posicao muda; largura/altura/giro ficam como estao.
        idx = request.form.get('indice', type=int)
        x = request.form.get('x', type=float)
        y = request.form.get('y', type=float)
        if idx is None or not (0 <= idx < len(itens)) or x is None or y is None:
            return volta(erro='Peca ou posicao invalida.')
        itens[idx] = {**itens[idx], 'x': x, 'y': y}
        peca_cod_registro = itens[idx].get('cod')
    elif acao == 'girar':
        # Vira 90 graus uma peca ja colocada - nao precisa mais decidir a
        # orientacao so no momento de soltar, da pra corrigir depois. Se nao
        # couber virada NO MESMO lugar, procura sozinho outro espaco livre
        # da chapa onde ela caiba virada, em vez de so recusar - a pessoa
        # pediu pra virar, nao pediu pra manter a posicao a qualquer custo.
        idx = request.form.get('indice', type=int)
        if idx is None or not (0 <= idx < len(itens)):
            return volta(erro='Peca nao encontrada neste padrao.')
        alvo = itens[idx]
        forcar_veio = request.form.get('forcar_veio') == '1'
        peca_cad = next((p for p in banco.listar_pecas(alvo['cod'], limite=80)
                          if p['cod'] == alvo['cod']), None)
        if peca_cad is not None and not _pode_girar_aqui(peca_cad, grupo) and not forcar_veio:
            # Bloqueio de veio, nao de geometria - diferente de "nao cabe",
            # aqui quem decide se vale o risco e a pessoa em frente a chapa
            # de verdade, nao o sistema. Devolve um aviso pra tela poder
            # perguntar "gira mesmo assim?" em vez de so recusar sem saida -
            # orcamento/regra automatica existe pra limitar o automatico,
            # nao pra travar uma decisao humana explicita (mesmo principio
            # do "adicionar" no editor de padrao comum).
            return volta(erro=f"A peca {alvo['cod']} e aparente e esta chapa respeita o veio - "
                              f"girar deixa o desenho da madeira atravessado.",
                         veio_bloqueou='1')
        novo_w, novo_h = alvo['h'], alvo['w']
        rodada = not alvo.get('rotated', False)
        outros = itens[:idx] + itens[idx + 1:]

        no_lugar = {**alvo, 'w': novo_w, 'h': novo_h, 'rotated': rodada}
        if not edicao.validar_padrao(outros + [no_lugar], r['sheet_w'], r['sheet_h'], r['estagios']):
            itens[idx] = no_lugar
        else:
            candidato = _melhor_encaixe_valido(
                {'cod': alvo['cod'], 'desc': alvo.get('desc'), 'piece_key': alvo.get('piece_key'),
                 'shelf': alvo.get('shelf', 0)},
                novo_w, novo_h, r['kerf'], False, outros, r['sheet_w'], r['sheet_h'], r['estagios'])
            if candidato is None:
                return volta(erro=f"A peca {alvo['cod']} nao cabe virada em lugar nenhum desta chapa.")
            candidato['rotated'] = rodada
            itens = outros[:idx] + [candidato] + outros[idx:]
        peca_cod_registro = alvo.get('cod')
    elif acao == 'incluir_melhor':
        # Inclui N unidades de uma peca automaticamente, cada uma no melhor
        # espaco livre que sobrar depois da anterior - o operador so diz a
        # peca e a quantidade, sem precisar clicar sobra por sobra.
        cod = (request.form.get('cod') or '').strip()
        quantidade = max(1, request.form.get('quantidade', type=int) or 1)
        peca = next((p for p in banco.listar_pecas(cod, limite=80) if p['cod'] == cod), None)
        if peca is None:
            return volta(erro='Codigo ' + cod + ' nao existe no cadastro de pecas.')
        pode_girar = _pode_girar_aqui(peca, grupo)
        incluidas, motivo_parada = 0, None
        for _ in range(quantidade):
            novo_item = _melhor_encaixe_valido(
                {'cod': peca['cod'], 'desc': peca['descricao'], 'piece_key': peca['cod'] + '_manual', 'shelf': 0},
                peca['comp_mm'], peca['larg_mm'], r['kerf'], pode_girar, itens, r['sheet_w'], r['sheet_h'], r['estagios'])
            if novo_item is None:
                motivo_parada = 'nao ha mais espaco livre onde esta peca caiba (respeitando o corte em guilhotina)'
                break
            itens = itens + [novo_item]
            incluidas += 1
        if incluidas == 0:
            return volta(erro=f'Nao consegui incluir nenhuma peca {cod}: {motivo_parada}')
        if incluidas < quantidade:
            aviso = f'Incluidas {incluidas} de {quantidade} pedidas ({motivo_parada}).'
        peca_cod_registro = peca['cod']
    elif acao == 'incluir_avulsa':
        # Peca SEM codigo no cadastro - o operador digita a dimensao
        # (comprimento x largura) na hora, pra cobrir sobra com algo que nao
        # tem numero cadastrado (corte avulso, teste, retalho de outro
        # pedido). O codigo exibido vira a propria dimensao ("400x390"), pra
        # a mesma dimensao usada duas vezes no padrao agrupar como o mesmo
        # "tipo" no relatorio, igual peca de catalogo agrupa pelo cod dela.
        # Sem "aparente" no cadastro pra consultar, cai no mesmo lado
        # conservador do resto do sistema (banco.pode_girar): se a chapa tem
        # veio, a peca avulsa nao gira sozinha. Para girar mesmo assim, o
        # operador usa o botao de girar na peca ja colocada, que pergunta
        # antes de furar a regra, igual peca de catalogo.
        try:
            comp = float((request.form.get('comp') or '').replace(',', '.'))
            larg = float((request.form.get('larg') or '').replace(',', '.'))
        except ValueError:
            return volta(erro='Informe comprimento e largura em milímetros (só números).')
        if comp <= 0 or larg <= 0:
            return volta(erro='Comprimento e largura precisam ser maiores que zero.')
        quantidade = max(1, request.form.get('quantidade', type=int) or 1)
        cod_avulso = f'{comp:.0f}x{larg:.0f}'
        pode_girar = not grupo.get('tem_veio')
        base_item = {'cod': cod_avulso, 'desc': f'Peça avulsa {comp:.0f}×{larg:.0f}mm',
                     'piece_key': cod_avulso + '_avulsa', 'shelf': 0}
        # Com espaço escolhido na tela (retalho preenchido): a peça entra
        # EXATAMENTE ali, e só ali - nunca no "melhor lugar" de outro canto
        # da chapa, mesmo que caiba melhor em outro lugar. Sem espaço
        # escolhido (formulário sempre visível, sem nenhuma sobra marcada):
        # cai no comportamento de sempre, melhor encaixe em qualquer canto.
        i_ret = request.form.get('retalho', type=int)
        if i_ret is not None:
            livres = edicao.retalhos_livres(itens, r['sheet_w'], r['sheet_h'], kerf=r['kerf'])
            if not (0 <= i_ret < len(livres)):
                return volta(erro='Esse espaço não existe mais nesta chapa - a seleção deve ter ficado velha.')
            incluidos = _encaixar_repetido_no_retalho(
                base_item, comp, larg, r['kerf'], pode_girar, itens,
                r['sheet_w'], r['sheet_h'], r['estagios'], livres[i_ret], quantidade)
            itens = itens + incluidos
            incluidas = len(incluidos)
            motivo_parada = 'esse espaço específico não tem mais lugar pra essa dimensão'
        else:
            incluidas, motivo_parada = 0, None
            for _ in range(quantidade):
                novo_item = _melhor_encaixe_valido(
                    base_item, comp, larg, r['kerf'], pode_girar, itens, r['sheet_w'], r['sheet_h'], r['estagios'])
                if novo_item is None:
                    motivo_parada = 'nao ha mais espaco livre onde essa dimensao caiba (respeitando o corte em guilhotina)'
                    break
                itens = itens + [novo_item]
                incluidas += 1
        if incluidas == 0:
            return volta(erro=f'Nao consegui incluir nenhuma peca avulsa {cod_avulso}: {motivo_parada}')
        if incluidas < quantidade:
            aviso = f'Incluídas {incluidas} de {quantidade} pedidas ({motivo_parada}).'
        peca_cod_registro = cod_avulso
    else:
        return volta(erro='Acao desconhecida.')

    problemas = edicao.validar_padrao(itens, r['sheet_w'], r['sheet_h'], r['estagios'])
    if problemas:
        return volta(erro=' '.join(problemas))

    aprov_antes = padrao.get('aproveitamento')
    padrao['itens'] = itens
    padrao['editado'] = True
    padrao['pecas_exibicao'] = _pecas_do_padrao(padrao)
    _recalcular(r)
    _conferir_demanda(r)
    # _redesenhar PRECISA rodar antes de salvar: é ela que grava o novo
    # imagem_versao no padrao, e o que não estiver em `r` neste ponto não
    # vai pro banco - salvar antes perderia esse número e o navegador
    # continuaria servindo a imagem antiga em cache.
    _redesenhar(plano_id, r, grupo, padrao)
    banco.atualizar_resultado(plano_id, r)

    # O registro e o que permite o sistema repetir sozinho o que voce faz
    # sempre - e, mais util ainda, mostrar onde o otimizador esta deixando
    # espaco na mesa de forma sistematica.
    banco.registrar_edicao({
        'plano_id': plano_id, 'cor': grupo.get('cor'), 'esp': grupo.get('esp'),
        'acao': acao, 'peca_cod': peca_cod_registro,
        'sobra_w': int(livres[i_ret].w) if acao == 'adicionar' else None,
        'sobra_h': int(livres[i_ret].h) if acao == 'adicionar' else None,
        'repeticoes': padrao.get('repeticoes'),
        'aprov_antes': aprov_antes, 'aprov_depois': padrao.get('aproveitamento'),
    })
    return volta(ok='1', **({'aviso': aviso} if aviso else {}))


@app.route('/cadastro/resolver-medida', methods=['POST'])
def resolver_medida():
    """Adota a medida do catálogo para uma peça em conflito."""
    cod = (request.form.get('cod') or '').strip()
    try:
        comp = int(float(request.form.get('comp')))
        larg = int(float(request.form.get('larg')))
    except (TypeError, ValueError):
        abort(400)
    if not cod:
        abort(400)
    banco.resolver_conflito(cod, comp, larg)
    return redirect(request.form.get('voltar') or url_for('cadastro', aba='pecas'))


@app.route('/aprendizado')
def aprendizado():
    """O que o sistema aprendeu com as edicoes manuais."""
    return render_template('aprendizado.html', pagina='aprendizado',
                            regras=banco.regras_aprendidas(minimo=1),
                            historico=banco.historico_edicoes(60),
                            auto=banco.obter_parametros().get('auto_sugerir', 1))


@app.route('/resultado/<plano_id>/pdf')
def plano_pdf(plano_id):
    """O plano em PDF, uma página por padrão, para levar até a máquina."""
    salvo = banco.obter_plano(plano_id) or abort(404)
    caminho = relatorio.gerar(salvo['resultado'], salvo)
    nome = f"plano-corte-{plano_id}.pdf"
    return send_file(caminho, mimetype='application/pdf',
                      as_attachment=True, download_name=nome)


@app.route('/resultado/<plano_id>/relatorio/planejado-produzido')
def relatorio_planejado_produzido(plano_id):
    """Todo o plano, peça a peça: quanto foi pedido e quanto está sendo produzido hoje."""
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    _conferir_demanda(r)
    linhas = _linhas_planejado_produzido(r)
    return _sem_cache(make_response(render_template(
        'relatorio_planejado_produzido.html', pagina='planos', plano=salvo, r=r,
        linhas=linhas,
        n_certinho=sum(1 for l in linhas if l['dif'] == 0),
        n_falta=sum(1 for l in linhas if l['dif'] < 0),
        n_excedente=sum(1 for l in linhas if l['dif'] > 0))))


@app.route('/resultado/<plano_id>/relatorio/pecas-extras')
def relatorio_pecas_extras(plano_id):
    """
    Só as peças que o plano corta a mais do que o Kambam/quantidade pediu -
    seja por sobra inerente de cortar chapa inteira, aprendizado automático,
    ou o botão "Preencher com peças extras".
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    _conferir_demanda(r)
    linhas = sorted((l for l in _linhas_planejado_produzido(r) if l['dif'] > 0),
                     key=lambda l: -l['dif'])
    total_chapas = sum(g.get('n_chapas', 0) for g in r.get('grupos', []))
    # media ponderada por chapa: um grupo com 2000 chapas pesa mais no
    # aproveitamento do plano inteiro do que um com 5.
    soma_ponderada = sum(g.get('aproveitamento_medio', 0) * g.get('n_chapas', 0)
                          for g in r.get('grupos', []))
    aproveitamento_final = (soma_ponderada / total_chapas) if total_chapas else 0
    return _sem_cache(make_response(render_template(
        'relatorio_pecas_extras.html', pagina='planos', plano=salvo, r=r,
        linhas=linhas, total_extras=sum(l['dif'] for l in linhas),
        total_chapas=total_chapas, aproveitamento_final=aproveitamento_final)))


@app.route('/resultado/<plano_id>/grupo/<int:gi>/aceitar', methods=['POST'])
def aceitar_diferenca(plano_id, gi):
    """
    Assume a alteracao como o novo pedido, para mais ou para menos.

    Sem isto o plano editado ficaria permanentemente marcado como divergente
    de um numero que voce ja decidiu mudar.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    try:
        g = r['grupos'][gi]
    except (IndexError, KeyError):
        abort(404)

    cod = (request.form.get('cod') or '').strip()
    pedido, produzido = _demanda_do_grupo(g), _producao_do_grupo(g)
    ajustes = g.get('ajustes') or {}

    if request.form.get('todas') == '1':
        for c in set(pedido) | set(produzido):
            d = produzido.get(c, 0) - pedido.get(c, 0)
            if d:
                ajustes[c] = ajustes.get(c, 0) + d
    elif cod:
        d = produzido.get(cod, 0) - pedido.get(cod, 0)
        if d:
            ajustes[cod] = ajustes.get(cod, 0) + d
    else:
        abort(400)

    g['ajustes'] = ajustes
    _conferir_demanda(r)
    banco.atualizar_resultado(plano_id, r)
    return redirect(url_for('resultado', job_id=plano_id) + '#g' + str(gi))


@app.route('/resultado/<plano_id>/grupo/<int:gi>/reequilibrar', methods=['POST'])
def reequilibrar(plano_id, gi):
    """Recalcula quantas chapas de cada padrao para bater com a demanda atual."""
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    try:
        g = r['grupos'][gi]
    except (IndexError, KeyError):
        abort(404)

    res = _rebalancear_grupo(g)
    if res['ok']:
        _recalcular(r)
        _conferir_demanda(r)
        for pad in g['padroes']:
            _redesenhar(plano_id, r, g, pad)
        banco.atualizar_resultado(plano_id, r)
    return redirect(url_for('resultado', job_id=plano_id,
                             reeq=('%d:%d' % (res['antes'], res['depois'])) if res['ok']
                                   else 'erro', motivo=res.get('motivo')) + '#g' + str(gi))


@app.route('/resultado/<plano_id>/grupo/<int:gi>/preencher-extra', methods=['POST'])
def _preencher_extra_no_grupo(plano_id: str, r: dict, g: dict, pct: float, catalogo: dict) -> int:
    """
    Enche os espacos livres dos padroes de UM grupo com pecas extras, ate
    `pct`% da demanda de cada peca. Devolve quantas unidades fisicas
    entraram (0 se nao tinha pedido ou nao sobrou espaco/orcamento).

    So mexe no grupo (nao salva no banco) - quem chama decide quando gravar,
    pra dar pra encher varios grupos e salvar uma unica vez no final.
    """
    pedido = {c: q for c, q in _demanda_do_grupo(g).items() if q > 0}
    if not pedido:
        return 0
    # ceil(qtd * pct / 100), sempre >=1 pra quem tem pedido>0 e pct>0.
    orcamento_fisico = {cod: -(-int(qtd * pct) // 100) for cod, qtd in pedido.items()}
    pode_girar_por_cod = {cod: _pode_girar_aqui(catalogo[cod], g) for cod in orcamento_fisico if cod in catalogo}
    total = _preencher_padroes_extra(g['padroes'], catalogo, orcamento_fisico, pode_girar_por_cod,
                                      r['kerf'], r['sheet_w'], r['sheet_h'], r['estagios'])
    if total:
        for pad in g['padroes']:
            pad['pecas_exibicao'] = _pecas_do_padrao(pad)
            _redesenhar(plano_id, r, g, pad)
    return total


def preencher_extra(plano_id, gi):
    """
    Enche os espacos livres dos padroes deste grupo com pecas extras, ate o
    percentual de "Pecas extras (%)" cadastrado na maquina do plano.

    Sempre overproducao deliberada por cima do pedido - nunca troca uma peca
    do pedido por outra, so aproveita sobra de chapa que ja ia ser cortada
    de qualquer jeito. O percentual e por MAQUINA (nao por plano) porque e
    uma decisao de quanto estoque extra a fabrica aceita gerar, nao do
    calculo de um lote especifico.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']
    try:
        g = r['grupos'][gi]
    except (IndexError, KeyError):
        abort(404)

    maq = banco.maquina(r.get('maquina_id'))
    pct = float(maq['pct_extra']) if maq and maq['pct_extra'] else 0.0
    if pct <= 0:
        return redirect(url_for('resultado', job_id=plano_id,
                                 extra_erro='Esta maquina nao tem "Pecas extras (%)" configurado '
                                             '(ou esta em 0). Ajuste em Maquinas.') + '#g' + str(gi))

    catalogo = {p['cod']: p for p in banco.listar_pecas(limite=5000)}
    total = _preencher_extra_no_grupo(plano_id, r, g, pct, catalogo)
    if total:
        _recalcular(r)
        _conferir_demanda(r)
        banco.atualizar_resultado(plano_id, r)
        return redirect(url_for('resultado', job_id=plano_id, extra_ok=total) + '#g' + str(gi))
    return redirect(url_for('resultado', job_id=plano_id,
                             extra_erro='Nao sobrou espaco livre suficiente pra encaixar peca extra '
                                        'nenhuma neste grupo.') + '#g' + str(gi))


@app.route('/resultado/<plano_id>/preencher-extra-tudo', methods=['POST'])
def preencher_extra_tudo(plano_id):
    """
    A mesma coisa que o botao por grupo, so que pra TODOS os grupos do plano
    de uma vez - existe porque o botao por grupo, escondido no cabecalho de
    cada material, e facil de nao encontrar num plano com varios grupos. Um
    unico botao no topo do plano, sempre visivel, resolve isso.
    """
    salvo = banco.obter_plano(plano_id) or abort(404)
    r = salvo['resultado']

    maq = banco.maquina(r.get('maquina_id'))
    pct = float(maq['pct_extra']) if maq and maq['pct_extra'] else 0.0
    if pct <= 0:
        return redirect(url_for('resultado', job_id=plano_id,
                                 extra_erro='Esta maquina nao tem "Pecas extras (%)" configurado '
                                             '(ou esta em 0). Ajuste em Maquinas.'))

    catalogo = {p['cod']: p for p in banco.listar_pecas(limite=5000)}
    total = 0
    for g in r.get('grupos', []):
        total += _preencher_extra_no_grupo(plano_id, r, g, pct, catalogo)

    if total:
        _recalcular(r)
        _conferir_demanda(r)
        banco.atualizar_resultado(plano_id, r)
        return redirect(url_for('resultado', job_id=plano_id, extra_ok=total))
    return redirect(url_for('resultado', job_id=plano_id,
                             extra_erro='Nao sobrou espaco livre suficiente pra encaixar peca extra '
                                        'nenhuma neste plano.'))


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
