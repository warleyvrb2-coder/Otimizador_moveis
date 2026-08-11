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
import jobs
import pipeline

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
    return render_template('index.html', pagina='novo',
                            res=banco.resumo(), par=banco.obter_parametros())


@app.route('/planos')
def planos():
    lista = []
    for j in sorted(jobs.todos(), key=lambda j: j.criado_em, reverse=True):
        r = j.resultado or {}
        lista.append({
            'id': j.id, 'estado': j.estado, 'pct': j.pct,
            'quando': j.criado_em.astimezone().strftime('%d/%m %H:%M'),
            'arquivos': ', '.join(k['arquivo'] for k in r.get('kambans_info') or []) or None,
            'chapas': r.get('total_chapas'),
        })
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
    permanente = os.path.abspath(DATA_DIR) != os.path.abspath(BASE_DIR)
    info = {
        'caminho': os.path.abspath(DATA_DIR),
        'permanente': permanente,
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
    arquivos_form = [f for f in request.files.getlist('kambans') if f and f.filename]
    if not arquivos_form:
        return redirect(url_for('index'))

    max_estagios_raw = request.form.get('max_estagios', 'ilimitado')
    max_depth = None if max_estagios_raw == 'ilimitado' else int(max_estagios_raw)
    # checkbox: só chega no request quando marcado
    respeitar_veio = request.form.get('respeitar_veio') is not None

    job_id = os.urandom(5).hex()
    salvos = []
    for f in arquivos_form:
        nome = secure_filename(f.filename) or 'kambam.pdf'
        caminho = os.path.join(UPLOAD_DIR, f'{job_id}_{nome}')
        f.save(caminho)
        salvos.append((caminho, f.filename))

    run_dir = os.path.join(OUTPUT_DIR, job_id)
    job = jobs.criar(
        pipeline.rodar, salvos, run_dir, f'/plano/{job_id}',
        respeitar_veio=respeitar_veio, max_depth=max_depth,
    )
    return redirect(url_for('acompanhar', job_id=job.id))


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
    itens = (banco.listar_pecas(busca, so_pendentes=pendentes) if aba == 'pecas'
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
                            modelos=banco.listar_modelos())


@app.route('/modelos/<cod>')
def modelo_detalhe(cod):
    m = banco.modelo(cod) or abort(404)
    return render_template('modelo.html', pagina='modelos', m=m,
                            pecas=banco.pecas_do_modelo(cod))


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
    job = jobs.obter(job_id) or abort(404)
    if job.estado == 'erro':
        return render_template('resultado.html', erro=job.erro, kambans_info=None), 500
    if job.estado != 'pronto':
        return redirect(url_for('acompanhar', job_id=job_id))
    return render_template('resultado.html', **job.resultado)


if __name__ == '__main__':
    # Só para desenvolvimento na sua máquina. Publicado, quem sobe o app é o
    # gunicorn (veja o Procfile) - nunca este bloco, e nunca com debug ligado:
    # o depurador do Flask permite executar código no servidor pela página.
    app.run(host='127.0.0.1', port=5000, debug=bool(os.environ.get('FLASK_DEBUG')))
