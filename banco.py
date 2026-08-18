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
import json
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
            CREATE TABLE IF NOT EXISTS modelo (
                cod           TEXT PRIMARY KEY,
                descricao     TEXT NOT NULL,
                visto_em      TEXT
            );
            -- lista técnica: quantas peças de cada tipo o móvel leva
            CREATE TABLE IF NOT EXISTS modelo_peca (
                modelo_cod    TEXT NOT NULL,
                peca_cod      TEXT NOT NULL,
                por_unidade   REAL,
                confirmado    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (modelo_cod, peca_cod)
            );
            -- acabamento comercial do móvel ("CINAMAMO/O FF WHITE ARENAS").
            -- NÃO é a cor da chapa: um acabamento de duas cores usa o corpo
            -- numa chapa e as portas em outra.
            CREATE TABLE IF NOT EXISTS acabamento (
                nome          TEXT PRIMARY KEY,
                visto_em      TEXT
            );
            CREATE TABLE IF NOT EXISTS acabamento_cor (
                acabamento    TEXT NOT NULL,
                cor           TEXT NOT NULL,
                PRIMARY KEY (acabamento, cor)
            );
            -- Planos calculados. Antes viviam só na memória do servidor e
            -- sumiam a cada reinício, o que impede tanto aprovar quanto
            -- editar: não se aprova um documento que evapora.
            CREATE TABLE IF NOT EXISTS plano (
                id            TEXT PRIMARY KEY,
                criado_em     TEXT NOT NULL,
                arquivos      TEXT,
                total_chapas  INTEGER,
                resultado     TEXT NOT NULL,   -- JSON do plano inteiro
                aprovado      INTEGER NOT NULL DEFAULT 0,
                aprovado_em   TEXT,
                aprovado_por  TEXT,
                observacao    TEXT
            );
            -- Máquinas de corte. Cada uma tem seu disco, sua chapa e seu
            -- limite de empilhamento, então o mesmo lote rende diferente em
            -- cada uma. Fábrica com uma seccionadora só é caso particular,
            -- não a regra.
            CREATE TABLE IF NOT EXISTS maquina (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nome          TEXT NOT NULL,
                descricao     TEXT,
                chapa_larg    INTEGER NOT NULL,
                chapa_alt     INTEGER NOT NULL,
                kerf          REAL NOT NULL,
                pilha_max     INTEGER NOT NULL,
                estagios      INTEGER NOT NULL,
                tempo_grupo   INTEGER NOT NULL,
                ativa         INTEGER NOT NULL DEFAULT 1,
                criada_em     TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_mp_modelo ON modelo_peca(modelo_cod);
            CREATE INDEX IF NOT EXISTS ix_mp_peca   ON modelo_peca(peca_cod);
            CREATE INDEX IF NOT EXISTS ix_plano_data ON plano(criado_em DESC);
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
    criar_tabelas()
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
    criar_tabelas()
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


# palavras genéricas demais pra identificar um móvel: aparecem em quase todos
GENERICAS = {'ROUPEIRO', 'MODULO', 'MODULOS', 'KIT', 'REF', 'PORTA', 'PORTAS',
             'AEREO', 'COM', 'SEM', 'DE', 'DA', 'DO', 'NEW'}


def _tokens_modelo(descricao: str) -> set:
    """Palavras que realmente distinguem um móvel de outro."""
    t = _normalizar(descricao)
    t = re.sub(r'\bREF\b.*$', '', t)          # "REF 0129" e o que vier depois
    return {p for p in t.split() if len(p) >= 4 and p not in GENERICAS and not p.isdigit()}


def modelos_ambiguos(modelos: list[dict]) -> set:
    """
    Modelos que o nome sozinho não distingue.

    "KIT DALLAS 1 PORTA" só tem DALLAS de distintivo, e isso também aparece em
    "MODULO DALLAS CANTO RETO". Qualquer peça que cite Dallas casaria com os
    dois, e o vínculo sairia errado - pior que vínculo faltando, porque um
    vínculo errado vira lista técnica errada sem ninguém perceber.

    Regra: se as palavras de um modelo são um subconjunto das de outro, ele
    é ambíguo e fica de fora do vínculo automático.
    """
    tokens = {m['cod']: _tokens_modelo(m['desc']) for m in modelos}
    ambiguos = set()
    for cod, toks in tokens.items():
        if not toks:
            ambiguos.add(cod)
            continue
        for outro, toks2 in tokens.items():
            if outro != cod and toks and toks < toks2:
                ambiguos.add(cod)
                break
    return ambiguos


def vincular_peca_modelo(desc_peca: str, modelos: list[dict],
                          ambiguos: set | None = None) -> list[str]:
    """
    Descobre a quais móveis uma peça pertence, pelo nome.

    A descrição da peça costuma trazer o modelo no fim ("LATERAL ESQUERDA N°01
    ROUPEIRO ATLANTA/AREZZO" serve dois modelos). Casamos pelas palavras
    distintivas: exigimos que TODAS as palavras que identificam o móvel
    apareçam na peça, senão ATLANTA e AREZZO se confundiriam entre si.

    Volta lista vazia quando a descrição foi truncada, quando não nomeia
    modelo nenhum, ou quando o modelo é ambíguo - todos casos que precisam da
    sua confirmação na tela.
    """
    if ambiguos is None:
        ambiguos = modelos_ambiguos(modelos)
    alvo = _normalizar(desc_peca)
    achados = []
    for m in modelos:
        if m['cod'] in ambiguos:
            continue
        toks = _tokens_modelo(m['desc'])
        if toks and all(t in alvo for t in toks):
            achados.append(m['cod'])
    return achados


def importar_modelos(itens: list[dict], pecas: list[dict]) -> dict:
    """
    Cadastra os móveis do Kambam e deriva a lista técnica.

    A conta é direta: se o lote produz 100 roupeiros e pede 600 rodapés, o
    móvel leva 6 rodapés. Só grava quando a divisão dá inteiro exato - razão
    quebrada significa que o vínculo peça↔modelo está errado, e um número
    inventado aqui viraria lote errado lá na frente.

    Não sobrescreve lista técnica que você já confirmou na tela.
    """
    criar_tabelas()
    agora = datetime.now(timezone.utc).isoformat(timespec='seconds')

    # quantidade total por modelo e os acabamentos vistos
    unidades: dict[str, float] = {}
    modelos: list[dict] = []
    for i in itens:
        unidades[i['cod']] = unidades.get(i['cod'], 0) + i['qtd']
        if not any(m['cod'] == i['cod'] for m in modelos):
            modelos.append({'cod': i['cod'], 'desc': i['desc']})

    # quantidade total por peça (somando as cores)
    total_peca: dict[str, float] = {}
    desc_peca: dict[str, str] = {}
    for p in pecas:
        total_peca[p['cod']] = total_peca.get(p['cod'], 0) + p['qtd']
        desc_peca.setdefault(p['cod'], p['desc'])

    novos = vinculos = sem_vinculo = 0
    with conectar() as con:
        for m in modelos:
            con.execute('INSERT INTO modelo (cod, descricao, visto_em) VALUES (?,?,?) '
                        'ON CONFLICT(cod) DO UPDATE SET visto_em=excluded.visto_em',
                        (m['cod'], m['desc'], agora))
            novos += 1
        for i in itens:
            if i['acabamento']:
                con.execute('INSERT INTO acabamento (nome, visto_em) VALUES (?,?) '
                            'ON CONFLICT(nome) DO UPDATE SET visto_em=excluded.visto_em',
                            (i['acabamento'], agora))

        ambiguos = modelos_ambiguos(modelos)
        for cod, qtd in total_peca.items():
            dos_modelos = vincular_peca_modelo(desc_peca[cod], modelos, ambiguos)
            if not dos_modelos:
                sem_vinculo += 1
                continue
            base = sum(unidades.get(c, 0) for c in dos_modelos)
            if base <= 0:
                sem_vinculo += 1
                continue
            razao = qtd / base
            if abs(razao - round(razao)) > 0.001:   # não fechou: não inventa
                sem_vinculo += 1
                continue
            for c in dos_modelos:
                con.execute(
                    'INSERT INTO modelo_peca (modelo_cod, peca_cod, por_unidade, confirmado) '
                    'VALUES (?,?,?,0) ON CONFLICT(modelo_cod, peca_cod) DO UPDATE SET '
                    'por_unidade=excluded.por_unidade WHERE modelo_peca.confirmado=0',
                    (c, cod, round(razao)))
                vinculos += 1
    return {'modelos': novos, 'vinculos': vinculos, 'pecas_sem_modelo': sem_vinculo}


def listar_modelos() -> list[sqlite3.Row]:
    criar_tabelas()
    with conectar() as con:
        return con.execute("""
            SELECT m.*, COUNT(mp.peca_cod) AS n_pecas,
                   COALESCE(SUM(mp.por_unidade), 0) AS total_pecas
              FROM modelo m LEFT JOIN modelo_peca mp ON mp.modelo_cod = m.cod
             GROUP BY m.cod ORDER BY m.descricao
        """).fetchall()


def pecas_do_modelo(cod: str) -> list[sqlite3.Row]:
    criar_tabelas()
    with conectar() as con:
        return con.execute("""
            SELECT p.*, mp.por_unidade, mp.confirmado AS vinculo_confirmado,
                   (SELECT GROUP_CONCAT(DISTINCT c2.nome) FROM cor c2) AS todas_cores
              FROM modelo_peca mp JOIN peca p ON p.cod = mp.peca_cod
             WHERE mp.modelo_cod = ?
             ORDER BY mp.por_unidade DESC, p.descricao
        """, (cod,)).fetchall()


def pecas_sem_modelo(busca: str = '', limite: int = 0) -> list[sqlite3.Row]:
    """
    Peças que não ficaram ligadas a móvel nenhum.

    Elas existem em quantidade: descrição truncada no relatório, ou modelo de
    nome ambíguo demais pra vincular sem risco. Precisam de um lugar próprio,
    senão sairiam da tela junto com a lista de peças e nunca teriam o veio
    conferido - e peça não conferida é tratada como aparente, gastando chapa.
    """
    criar_tabelas()
    sql = """SELECT p.* FROM peca p
              WHERE NOT EXISTS (SELECT 1 FROM modelo_peca mp WHERE mp.peca_cod = p.cod)"""
    args = []
    if busca:
        sql += ' AND (p.cod LIKE ? OR p.descricao LIKE ?)'
        args += [f'%{busca}%', f'%{busca.upper()}%']
    sql += ' ORDER BY p.confirmado, p.descricao'
    if limite:
        sql += f' LIMIT {int(limite)}'
    with conectar() as con:
        return con.execute(sql, args).fetchall()


def contar_sem_modelo() -> int:
    criar_tabelas()
    with conectar() as con:
        return con.execute("""SELECT COUNT(*) n FROM peca p WHERE NOT EXISTS
                              (SELECT 1 FROM modelo_peca mp WHERE mp.peca_cod = p.cod)"""
                            ).fetchone()['n']


def modelo(cod: str) -> sqlite3.Row | None:
    criar_tabelas()
    with conectar() as con:
        return con.execute('SELECT * FROM modelo WHERE cod=?', (cod,)).fetchone()


def listar_acabamentos() -> list[dict]:
    """Acabamentos com as cores de chapa já mapeadas para cada um."""
    criar_tabelas()
    with conectar() as con:
        acabs = con.execute('SELECT * FROM acabamento ORDER BY nome').fetchall()
        mapa: dict[str, list[str]] = {}
        for r in con.execute('SELECT acabamento, cor FROM acabamento_cor'):
            mapa.setdefault(r['acabamento'], []).append(r['cor'])
    return [{'nome': a['nome'], 'cores': sorted(mapa.get(a['nome'], []))} for a in acabs]


def definir_acabamento_cor(acabamento: str, cor: str, ligado: bool) -> None:
    criar_tabelas()
    with conectar() as con:
        if ligado:
            con.execute('INSERT OR IGNORE INTO acabamento_cor (acabamento, cor) VALUES (?,?)',
                        (acabamento, cor))
        else:
            con.execute('DELETE FROM acabamento_cor WHERE acabamento=? AND cor=?',
                        (acabamento, cor))


def sugestao_de_busca(descricao_modelo: str) -> str:
    """
    Palavras do móvel que valem procurar nas peças.

    A descrição da peça quase sempre cita o móvel ("LATERAL ESQUERDA N°01
    ROUPEIRO ATLANTA/AREZZO"). O vínculo automático exige que TODAS as
    palavras batam e por isso recusa os casos ambíguos - mas pra busca manual
    o interessante é o contrário: uma palavra só já traz os candidatos, e quem
    escolhe é você.
    """
    toks = sorted(_tokens_modelo(descricao_modelo), key=len, reverse=True)
    return toks[0] if toks else ''


def candidatas_para_modelo(modelo_cod: str, busca: str, limite: int = 120) -> list[sqlite3.Row]:
    """Peças que combinam com a busca e ainda NÃO estão neste móvel."""
    criar_tabelas()
    if not busca.strip():
        return []
    termo = f'%{busca.strip().upper()}%'
    with conectar() as con:
        return con.execute("""
            SELECT p.*, EXISTS(SELECT 1 FROM modelo_peca mp2 WHERE mp2.peca_cod = p.cod)
                   AS em_outro
              FROM peca p
             WHERE (UPPER(p.descricao) LIKE ? OR p.cod LIKE ?)
               AND NOT EXISTS (SELECT 1 FROM modelo_peca mp
                                WHERE mp.peca_cod = p.cod AND mp.modelo_cod = ?)
             ORDER BY em_outro, p.descricao
             LIMIT ?
        """, (termo, termo, modelo_cod, limite)).fetchall()


def vincular_varias(modelo_cod: str, pecas: list[tuple[str, float]]) -> int:
    """Liga várias peças de uma vez ao mesmo móvel."""
    criar_tabelas()
    with conectar() as con:
        for cod, qtd in pecas:
            con.execute(
                'INSERT INTO modelo_peca (modelo_cod, peca_cod, por_unidade, confirmado) '
                'VALUES (?,?,?,1) ON CONFLICT(modelo_cod, peca_cod) DO UPDATE SET '
                'por_unidade=excluded.por_unidade, confirmado=1',
                (modelo_cod, cod, qtd))
    return len(pecas)


def remover_vinculo(modelo_cod: str, peca_cod: str) -> None:
    """
    Desfaz a ligação peça↔móvel.

    Necessário porque o vínculo automático erra: nome parecido entre dois
    móveis pode ligar uma peça no lugar errado, e sem poder desfazer a lista
    técnica ficaria suja pra sempre.
    """
    criar_tabelas()
    with conectar() as con:
        con.execute('DELETE FROM modelo_peca WHERE modelo_cod=? AND peca_cod=?',
                    (modelo_cod, peca_cod))


def modelos_para_escolha() -> list[sqlite3.Row]:
    """Lista enxuta pra alimentar o seletor de vínculo manual."""
    criar_tabelas()
    with conectar() as con:
        return con.execute('SELECT cod, descricao FROM modelo ORDER BY descricao').fetchall()


def definir_por_unidade(modelo_cod: str, peca_cod: str, quantidade: float) -> None:
    criar_tabelas()
    with conectar() as con:
        con.execute(
            'INSERT INTO modelo_peca (modelo_cod, peca_cod, por_unidade, confirmado) '
            'VALUES (?,?,?,1) ON CONFLICT(modelo_cod, peca_cod) DO UPDATE SET '
            'por_unidade=excluded.por_unidade, confirmado=1',
            (modelo_cod, peca_cod, quantidade))


def importar_catalogo(itens: list[dict], espessuras: set[float]) -> dict:
    """
    Traz o catálogo completo do Agrosys ("Itens com Especificações").

    Só entra o que estiver nas espessuras escolhidas: o arquivo mistura peça
    de chapa com MANTA, CAIXA de papelão e ISOPOR, que não passam pela serra
    e só poluiriam o cadastro.

    Como sempre, não mexe no que você já confirmou - e a medida do catálogo é
    a oficial, então ela atualiza a que veio do Kambam.
    """
    criar_tabelas()
    agora = datetime.now(timezone.utc).isoformat(timespec='seconds')
    novas = atualizadas = divergentes = 0
    conflitos = []
    with conectar() as con:
        for i in itens:
            if espessuras and i['esp_mm'] not in espessuras:
                continue
            cod = str(i['cod'])
            comp, larg = int(round(i['comp_mm'])), int(round(i['larg_mm']))
            atual = con.execute('SELECT comp_mm, larg_mm FROM peca WHERE cod=?', (cod,)).fetchone()
            if atual is None:
                con.execute(
                    'INSERT INTO peca (cod, descricao, aparente, confirmado, comp_mm, larg_mm, visto_em)'
                    ' VALUES (?,?,?,0,?,?,?)',
                    (cod, i['desc'], int(palpite_aparente(i['desc'])), comp, larg, agora))
                novas += 1
                continue
            # comprimento e largura trocados entre as duas fontes é sinal de
            # cadastro invertido - e a regra do veio depende de qual é qual
            if atual['comp_mm'] and (atual['comp_mm'], atual['larg_mm']) == (larg, comp) \
                    and comp != larg:
                divergentes += 1
                conflitos.append({'cod': cod, 'desc': i['desc'],
                                   'kambam': f"{atual['comp_mm']}x{atual['larg_mm']}",
                                   'catalogo': f'{comp}x{larg}'})
                continue
            con.execute('UPDATE peca SET comp_mm=?, larg_mm=?, visto_em=? WHERE cod=?',
                        (comp, larg, agora, cod))
            atualizadas += 1
    return {'novas': novas, 'atualizadas': atualizadas,
            'divergentes': divergentes, 'conflitos': conflitos[:40]}


def listar_pecas(busca: str = '', so_pendentes: bool = False, limite: int = 0) -> list[sqlite3.Row]:
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
    if limite:
        sql += f' LIMIT {int(limite)}'
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


CAMPOS_MAQUINA = ('chapa_larg', 'chapa_alt', 'kerf', 'pilha_max', 'estagios', 'tempo_grupo')


def listar_maquinas(so_ativas: bool = False) -> list[sqlite3.Row]:
    """
    As máquinas cadastradas. Cria a primeira na estreia, a partir dos
    parâmetros globais que existiam antes — assim quem já usava o sistema não
    perde a configuração nem precisa recadastrar.
    """
    criar_tabelas()
    with conectar() as con:
        tem = con.execute('SELECT COUNT(*) n FROM maquina').fetchone()['n']
    if not tem:
        p = obter_parametros()
        criar_maquina({'nome': 'Seccionadora principal',
                        'descricao': 'Criada automaticamente com os parâmetros que já estavam salvos',
                        **{c: p[c] for c in CAMPOS_MAQUINA}})
    sql = 'SELECT * FROM maquina'
    if so_ativas:
        sql += ' WHERE ativa = 1'
    sql += ' ORDER BY ativa DESC, nome'
    with conectar() as con:
        return con.execute(sql).fetchall()


def maquina(maquina_id) -> sqlite3.Row | None:
    criar_tabelas()
    with conectar() as con:
        return con.execute('SELECT * FROM maquina WHERE id=?', (maquina_id,)).fetchone()


def _validar_maquina(dados: dict) -> tuple[dict, dict]:
    limites = {'chapa_larg': (500, 6000), 'chapa_alt': (500, 3000), 'kerf': (0, 15),
               'pilha_max': (15, 400), 'estagios': (2, 3), 'tempo_grupo': (5, 600)}
    limpos, erros = {}, {}
    nome = (dados.get('nome') or '').strip()
    if not nome:
        erros['nome'] = 'dê um nome para a máquina'
    limpos['nome'] = nome[:80]
    limpos['descricao'] = (dados.get('descricao') or '').strip()[:200]
    for c in CAMPOS_MAQUINA:
        meta = PARAMETROS[c]
        try:
            v = meta['tipo'](str(dados.get(c, meta['padrao'])).replace(',', '.'))
        except (TypeError, ValueError):
            erros[c] = 'precisa ser um número'
            continue
        lo, hi = limites[c]
        if not (lo <= v <= hi):
            erros[c] = f'precisa ficar entre {lo} e {hi}'
            continue
        limpos[c] = v
    # Checkbox desmarcado simplesmente não é enviado, então ausência sozinha
    # não distingue "desmarquei" de "nem passei por um formulário". O campo
    # oculto resolve: sem ele, o padrão é ATIVA - senão a máquina criada
    # automaticamente na estreia nasceria invisível.
    if 'ativa_marcador' in dados:
        limpos['ativa'] = 1 if dados.get('ativa') in (1, '1', True, 'on') else 0
    else:
        limpos['ativa'] = 1
    return limpos, erros


def criar_maquina(dados: dict) -> tuple[int | None, dict]:
    criar_tabelas()
    limpos, erros = _validar_maquina(dados)
    if erros:
        return None, erros
    with conectar() as con:
        cur = con.execute(
            'INSERT INTO maquina (nome, descricao, chapa_larg, chapa_alt, kerf, pilha_max,'
            ' estagios, tempo_grupo, ativa, criada_em) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (limpos['nome'], limpos['descricao'], limpos['chapa_larg'], limpos['chapa_alt'],
             limpos['kerf'], limpos['pilha_max'], limpos['estagios'], limpos['tempo_grupo'],
             limpos.get('ativa', 1),
             datetime.now(timezone.utc).isoformat(timespec='seconds')))
        return cur.lastrowid, {}


def atualizar_maquina(maquina_id, dados: dict) -> dict:
    criar_tabelas()
    limpos, erros = _validar_maquina(dados)
    if erros:
        return erros
    with conectar() as con:
        con.execute(
            'UPDATE maquina SET nome=?, descricao=?, chapa_larg=?, chapa_alt=?, kerf=?,'
            ' pilha_max=?, estagios=?, tempo_grupo=?, ativa=? WHERE id=?',
            (limpos['nome'], limpos['descricao'], limpos['chapa_larg'], limpos['chapa_alt'],
             limpos['kerf'], limpos['pilha_max'], limpos['estagios'], limpos['tempo_grupo'],
             limpos['ativa'], maquina_id))
    return {}


def excluir_maquina(maquina_id) -> None:
    """
    Remove a máquina. Planos antigos não são afetados: cada um guarda os
    parâmetros com que foi calculado, então continuam abrindo corretamente
    mesmo depois da máquina sair do cadastro.
    """
    criar_tabelas()
    with conectar() as con:
        con.execute('DELETE FROM maquina WHERE id=?', (maquina_id,))


def salvar_plano(plano_id: str, resultado: dict) -> None:
    """
    Guarda o plano inteiro como JSON.

    Serializado em bloco de propósito: o plano é um documento fechado, que
    vale exatamente como foi calculado — se ele fosse remontado depois a
    partir do cadastro atual, mudar o veio de uma peça reescreveria um plano
    já aprovado, e o desenho no chão de fábrica deixaria de bater.
    """
    criar_tabelas()
    arquivos = ', '.join(k['arquivo'] for k in resultado.get('kambans_info') or [])
    with conectar() as con:
        con.execute(
            'INSERT INTO plano (id, criado_em, arquivos, total_chapas, resultado) '
            'VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET resultado=excluded.resultado',
            (plano_id, datetime.now(timezone.utc).isoformat(timespec='seconds'),
             arquivos, resultado.get('total_chapas'), json.dumps(resultado, ensure_ascii=False)))


def atualizar_resultado(plano_id: str, resultado: dict) -> None:
    """
    Regrava o plano depois de uma edicao manual.

    Retira a aprovacao de proposito: o PCP aprovou um desenho especifico, e
    mexer nele depois torna aquela assinatura invalida. Melhor pedir nova
    conferencia do que deixar circular um papel aprovado que nao corresponde
    mais ao que esta na tela.
    """
    criar_tabelas()
    with conectar() as con:
        con.execute('UPDATE plano SET resultado=?, total_chapas=?, aprovado=0, '
                    'aprovado_em=NULL, aprovado_por=NULL WHERE id=?',
                    (json.dumps(resultado, ensure_ascii=False),
                     resultado.get('total_chapas'), plano_id))


def obter_plano(plano_id: str) -> dict | None:
    criar_tabelas()
    with conectar() as con:
        r = con.execute('SELECT * FROM plano WHERE id=?', (plano_id,)).fetchone()
    if not r:
        return None
    return {'id': r['id'], 'criado_em': r['criado_em'], 'aprovado': bool(r['aprovado']),
            'aprovado_em': r['aprovado_em'], 'aprovado_por': r['aprovado_por'],
            'observacao': r['observacao'], 'resultado': json.loads(r['resultado'])}


def listar_planos(limite: int = 60) -> list[dict]:
    criar_tabelas()
    with conectar() as con:
        linhas = con.execute(
            'SELECT id, criado_em, arquivos, total_chapas, aprovado, aprovado_em, aprovado_por '
            'FROM plano ORDER BY criado_em DESC LIMIT ?', (limite,)).fetchall()
    return [dict(r) for r in linhas]


def aprovar_plano(plano_id: str, por: str, observacao: str = '', aprovar: bool = True) -> None:
    """
    Marca o plano como conferido pelo PCP.

    O que aprovação resolve na prática: o plano aprovado é o que vale, e não
    precisa ser recalculado. Recalcular daria outro resultado — o otimizador
    tem tempo limitado e o cadastro muda — e aí o papel na máquina não bateria
    mais com a tela.
    """
    criar_tabelas()
    agora = datetime.now(timezone.utc).isoformat(timespec='seconds') if aprovar else None
    with conectar() as con:
        con.execute('UPDATE plano SET aprovado=?, aprovado_em=?, aprovado_por=?, observacao=? '
                    'WHERE id=?',
                    (int(aprovar), agora, por if aprovar else None, observacao or None, plano_id))


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
