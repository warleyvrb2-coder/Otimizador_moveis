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
                   jsonify, abort, Response)
from werkzeug.utils import secure_filename

import jobs
import pipeline

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Em hospedagem o disco é efêmero: escrever dentro do projeto some no próximo
# deploy. DATA_DIR permite apontar pra um volume persistente quando houver.
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
OUTPUT_DIR = os.path.join(BASE_DIR, 'static', 'resultados')
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
    if not APP_SENHA:
        if EM_NUVEM and request.endpoint != 'saude':
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


@app.route('/')
def index():
    return render_template('index.html')


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
        pipeline.rodar, salvos, run_dir, f'resultados/{job_id}',
        respeitar_veio=respeitar_veio, max_depth=max_depth,
    )
    return redirect(url_for('acompanhar', job_id=job.id))


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
