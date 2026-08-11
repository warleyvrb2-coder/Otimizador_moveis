# -*- coding: utf-8 -*-
"""
Cadastro de peças e cores (SQLite).

Duas perguntas independentes decidem se uma peça pode girar na chapa:

  1. A COR tem desenho direcional? (amadeirada x lisa)
  2. A PEÇA fica aparente no móvel montado?

Só quando as DUAS respostas são "sim" a rotação é proibida. Uma prateleira
interna pode girar mesmo em chapa amadeirada, porque ninguém vê o desenho
dela; e uma porta pode girar em chapa branca lisa, porque não há desenho pra
sair atravessado. Modelar isso separado é o que permite recuperar material
sem arriscar peça aparente — no CAST FF 2 L a diferença medida foi de 62
chapas num lote só.

SQLite de propósito: um arquivo, zero servidor, mesmo comportamento na sua
máquina e publicado. O caminho vem de DATA_DIR — publicado, precisa apontar
pra um volume, senão o cadastro some no próximo deploy.
"""
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'cadastro.db'))

# Palavras que denunciam peça APARENTE. Conferidas primeiro, porque em
# descrição composta ("BASE/PRAT/TAMPO") o lado seguro é tratar como aparente:
# errar aqui custa material, errar pro outro lado custa a peça inteira.
# "VISTA" é o caso típico - na marcenaria é literalmente a peça que fica à vista.
APARENTES = ('VISTA', 'PORTA', 'FRENTE', 'MOLDURA', 'RODAPE',
             'TAMPO', 'LATERAL', 'BASE')

# Peças INTERNAS: ficam escondidas no móvel montado, então o desenho da chapa
# não aparece e elas podem ser giradas à vontade pra economizar material.
INTERNAS = ('PRATELEIRA', 'PRAT', 'DIVISAO', 'DIVISOES',
            'SUPORTE', 'FUNDO', 'TRASEIR', 'NICHO')


def _normalizar(texto: str) -> str:
    """Maiúsculas sem acento: DIVISÃO e DIVISAO viram a mesma coisa."""
    sem_acento = ''.join(c for c in unicodedata.normalize('NFD', texto or '')
                          if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^A-Z0-9 ]+', ' ', sem_acento.upper())


def palpite_aparente(descricao: str) -> bool:
    """
    Chuta se a peça fica à vista, pelo nome. É só um ponto de partida pra
    você não começar com uma lista vazia de 125 itens - o que vale é o que
    for confirmado na tela.
    """
    t = _normalizar(descricao)
    if any(p in t for p in APARENTES):
        return True
    if any(p in t for p in INTERNAS):
        return False
    return True


def palpite_amadeirada(cor: str) -> bool:
    """Cor lisa costuma ter BRANCO/WHITE no nome e nada de madeira."""
    t = _normalizar(cor)
    if 'BRANC' in t or 'WHITE' in t:
        return False
    return True


def conectar() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def criar_tabelas() -> None:
    with conectar() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS peca (
                cod           TEXT PRIMARY KEY,
                descricao     TEXT NOT NULL,
                aparente      INTEGER NOT NULL,
                confirmado    INTEGER NOT NULL DEFAULT 0,
                comp_mm       INTEGER,
                larg_mm       INTEGER,
                visto_em      TEXT
            );
            CREATE TABLE IF NOT EXISTS cor (
                nome          TEXT PRIMARY KEY,
                amadeirada    INTEGER NOT NULL,
                confirmado    INTEGER NOT NULL DEFAULT 0,
                visto_em      TEXT
            );
            CREATE TABLE IF NOT EXISTS parametro (
                chave         TEXT PRIMARY KEY,
                valor         TEXT NOT NULL
            );
        """)


# Parâmetros da máquina. Ficam no banco pra poder ser ajustados na tela por
# quem opera - antes só existiam como variável de ambiente, o que obriga a
# mexer no painel da hospedagem e reiniciar o serviço só pra trocar o kerf.
# O valor de ambiente ainda serve de padrão inicial.
PARAMETROS = {
    'chapa_larg':  {'padrao': 2750, 'tipo': int,
                    'rotulo': 'Comprimento da chapa (mm)',
                    'ajuda': 'O lado maior. É nele que corre o veio da madeira.'},
    'chapa_alt':   {'padrao': 1850, 'tipo': int,
                    'rotulo': 'Largura da chapa (mm)',
                    'ajuda': 'O lado menor da chapa inteira, como vem do fornecedor.'},
    'kerf':        {'padrao': 4.4, 'tipo': float,
                    'rotulo': 'Espessura da serra (mm)',
                    'ajuda': 'Quanto de material cada corte consome. Para conferir: corte '
                             'um retalho ao meio, meça as duas metades, some e compare com '
                             'o original — a diferença é este número.'},
    'pilha_max':   {'padrao': 105, 'tipo': int,
                    'rotulo': 'Altura máxima da pilha (mm)',
                    'ajuda': 'Quanto a seccionadora corta empilhado. Guardamos em milímetros '
                             'e não em número de chapas porque o limite muda com a espessura: '
                             '105mm dá 7 chapas de 15mm, mas só 5 de 18mm.'},
    'estagios':    {'padrao': 3, 'tipo': int,
                    'rotulo': 'Estágios de corte',
                    'ajuda': '2 = toda peça tem a altura exata da faixa, mais simples de '
                             'executar. 3 = permite um corte de aparo para tirar a sobra da '
                             'faixa; rende mais e é o padrão na maioria das fábricas.'},
    'tempo_grupo': {'padrao': 45, 'tipo': int,
                    'rotulo': 'Tempo de cálculo por grupo (s)',
                    'ajuda': 'Quanto o otimizador pensa em cada cor. Mais tempo chega mais '
                             'perto do ótimo, com retorno decrescente.'},
}


def obter_parametros() -> dict:
    """Valores atuais: o que está no banco, senão o ambiente, senão o padrão."""
    criar_tabelas()
    with conectar() as con:
        salvos = {r['chave']: r['valor'] for r in con.execute('SELECT chave, valor FROM parametro')}
    saida = {}
    for chave, meta in PARAMETROS.items():
        bruto = salvos.get(chave, os.environ.get(chave.upper(), meta['padrao']))
        try:
            saida[chave] = meta['tipo'](bruto)
        except (TypeError, ValueError):
            saida[chave] = meta['padrao']
    return saida


def salvar_parametros(valores: dict) -> dict:
    """Grava só o que for número válido e estiver dentro de um limite sensato."""
    limites = {'chapa_larg': (500, 6000), 'chapa_alt': (500, 3000), 'kerf': (0, 15),
               'pilha_max': (15, 400), 'estagios': (2, 3), 'tempo_grupo': (5, 600)}
    erros = {}
    with conectar() as con:
        for chave, meta in PARAMETROS.items():
            if chave not in valores:
                continue
            try:
                v = meta['tipo'](str(valores[chave]).replace(',', '.'))
            except (TypeError, ValueError):
                erros[chave] = 'precisa ser um número'
                continue
            lo, hi = limites[chave]
            if not (lo <= v <= hi):
                erros[chave] = f'precisa ficar entre {lo} e {hi}'
                continue
            con.execute('INSERT INTO parametro (chave, valor) VALUES (?,?) '
                        'ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor', (chave, str(v)))
    return erros


def importar(pecas: list[dict]) -> dict:
    """
    Traz do Kamban tudo que ainda não está cadastrado, com o palpite inicial.

    NÃO mexe no que já existe: se você marcou uma peça como interna, um
    Kamban novo não vai desmarcar. Só atualiza a medida e a data, que servem
    pra você reconhecer a peça na tela.
    """
    agora = datetime.now(timezone.utc).isoformat(timespec='seconds')
    novas_pecas = novas_cores = 0
    with conectar() as con:
        for p in pecas:
            cod = str(p['cod'])
            existe = con.execute('SELECT 1 FROM peca WHERE cod=?', (cod,)).fetchone()
            if existe:
                con.execute('UPDATE peca SET comp_mm=?, larg_mm=?, visto_em=? WHERE cod=?',
                            (int(p['comp_mm']), int(p['larg_mm']), agora, cod))
            else:
                con.execute(
                    'INSERT INTO peca (cod, descricao, aparente, confirmado, comp_mm, larg_mm, visto_em)'
                    ' VALUES (?,?,?,0,?,?,?)',
                    (cod, p['desc'], int(palpite_aparente(p['desc'])),
                     int(p['comp_mm']), int(p['larg_mm']), agora))
                novas_pecas += 1

            cor = p['cor'].strip()
            if not cor:
                continue
            if con.execute('SELECT 1 FROM cor WHERE nome=?', (cor,)).fetchone():
                con.execute('UPDATE cor SET visto_em=? WHERE nome=?', (agora, cor))
            else:
                con.execute('INSERT INTO cor (nome, amadeirada, confirmado, visto_em) VALUES (?,?,0,?)',
                            (cor, int(palpite_amadeirada(cor)), agora))
                novas_cores += 1
    return {'pecas_novas': novas_pecas, 'cores_novas': novas_cores}


def listar_pecas(busca: str = '', so_pendentes: bool = False) -> list[sqlite3.Row]:
    criar_tabelas()
    sql = 'SELECT * FROM peca'
    cond, args = [], []
    if busca:
        cond.append('(cod LIKE ? OR descricao LIKE ?)')
        args += [f'%{busca}%', f'%{busca.upper()}%']
    if so_pendentes:
        cond.append('confirmado = 0')
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    sql += ' ORDER BY confirmado, descricao, cod'
    with conectar() as con:
        return con.execute(sql, args).fetchall()


def listar_cores() -> list[sqlite3.Row]:
    criar_tabelas()
    with conectar() as con:
        return con.execute('SELECT * FROM cor ORDER BY nome').fetchall()


def definir_peca(cod: str, aparente: bool) -> None:
    with conectar() as con:
        con.execute('UPDATE peca SET aparente=?, confirmado=1 WHERE cod=?', (int(aparente), cod))


def definir_cor(nome: str, amadeirada: bool) -> None:
    with conectar() as con:
        con.execute('UPDATE cor SET amadeirada=?, confirmado=1 WHERE nome=?', (int(amadeirada), nome))


def resumo() -> dict:
    criar_tabelas()
    with conectar() as con:
        p = con.execute('SELECT COUNT(*) t, SUM(confirmado) c FROM peca').fetchone()
        k = con.execute('SELECT COUNT(*) t, SUM(confirmado) c FROM cor').fetchone()
    return {'pecas': p['t'] or 0, 'pecas_confirmadas': p['c'] or 0,
            'cores': k['t'] or 0, 'cores_confirmadas': k['c'] or 0}


def regras() -> tuple[dict, dict]:
    """
    Devolve (aparente_por_cod, amadeirada_por_cor) pro otimizador consultar.
    """
    criar_tabelas()
    with conectar() as con:
        pecas = {r['cod']: bool(r['aparente']) for r in con.execute('SELECT cod, aparente FROM peca')}
        cores = {r['nome'].strip().upper(): bool(r['amadeirada'])
                 for r in con.execute('SELECT nome, amadeirada FROM cor')}
    return pecas, cores


def pode_girar(cod: str, cor: str, pecas: dict, cores: dict) -> bool:
    """
    A peça só é obrigada a manter a orientação quando a cor tem desenho
    direcional E ela fica aparente no móvel. Falta de cadastro cai no lado
    conservador (assume que tem veio e que é aparente).
    """
    cor_tem_veio = cores.get((cor or '').strip().upper(), True)
    peca_aparece = pecas.get(str(cod), True)
    return not (cor_tem_veio and peca_aparece)
