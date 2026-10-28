# -*- coding: utf-8 -*-
"""
O cálculo do plano de corte, sem nada de web.

Está separado do Flask de propósito: o lote inteiro leva minutos, muito mais
do que uma requisição HTTP aguenta em qualquer hospedagem. Sendo função pura,
dá pra rodar numa thread de fundo (veja jobs.py), num script, ou num teste,
sem subir servidor.
"""
import os
from collections import defaultdict

import aprendizado
import banco
import edicao
from parser import parse_kambam, parse_itens, file_hash
from optimizer import PieceType
from sequencial import optimize_group_sequencial
from visualize import render_sheet

def parametros(maquina_id=None, dimensao_id=None) -> dict:
    """
    Os parâmetros da máquina que vai cortar.

    Lidos a cada rodada, não na importação: mudar na tela vale já na próxima
    otimização, sem reiniciar o servidor. Sem máquina informada, cai nos
    parâmetros globais — que é o caso de quem ainda não cadastrou nenhuma.

    O tamanho da chapa vem da dimensão escolhida no plano (Cadastros >
    Dimensão da chapa; sem escolha, a padrão), não mais da máquina.
    """
    par = banco.obter_parametros()
    if maquina_id is not None:
        m = banco.maquina(maquina_id)
        if m:
            par = {c: m[c] for c in banco.CAMPOS_MAQUINA} | {'maquina_nome': m['nome']}
    dim = banco.dimensao(dimensao_id)
    par['chapa_larg'], par['chapa_alt'] = dim['larg'], dim['alt']
    return par


def tem_veio(cor: str, respeitar: bool = True, cores: dict | None = None) -> bool:
    """
    A chapa dessa cor tem desenho direcional?

    Vem do cadastro (banco.py). Cor que ainda não foi cadastrada é tratada
    como amadeirada — o lado conservador: assumir liso por engano manda peça
    aparente sair com o veio atravessado.
    """
    if not respeitar:
        return False
    if cores is None:
        _, cores = banco.regras()
    return cores.get((cor or '').strip().upper(), True)


def ratear_por_lote(padroes: list, tipos_dict: dict, ordem_lotes: list) -> list[dict]:
    """
    Diz, para cada padrão, quantas peças de cada tipo vão para cada Kambam.

    O rateio acontece DEPOIS da otimização e não interfere nela: uma peça
    cortada serve qualquer lote que peça aquela medida, então distribuir de
    um jeito ou de outro não muda uma chapa sequer. Por isso podemos usar o
    critério que for mais útil na fábrica - aqui, fechar os lotes na ordem
    em que foram enviados (prazo de entrega), o que faz o primeiro Kambam
    ficar pronto pra montagem antes do corte inteiro terminar.

    Retorna, na mesma ordem dos padrões: {piece_key: {lote: quantidade}}.
    """
    restante = {k: dict(t.demanda_por_lote) for k, t in tipos_dict.items()}
    resultado = []
    for pat in padroes:
        deste = {}
        for key, por_chapa in pat.counts.items():
            faltam = por_chapa * pat.repeticoes
            alocado = {}
            for lote in ordem_lotes:
                if faltam <= 0:
                    break
                disponivel = restante.get(key, {}).get(lote, 0)
                if disponivel <= 0:
                    continue
                usa = min(disponivel, faltam)
                alocado[lote] = usa
                restante[key][lote] -= usa
                faltam -= usa
            if faltam > 0:  # produção acima da demanda (sobra do arredondamento)
                alocado['(excedente)'] = faltam
            deste[key] = alocado
        resultado.append(deste)
    return resultado


def ler_kambans(arquivos: list[tuple[str, str]]) -> tuple[list, list]:
    """
    arquivos: [(caminho_em_disco, nome_original)].

    O mesmo relatório baixado duas vezes ("Kambam.pdf" e "Kambam (1).pdf")
    dobraria silenciosamente a produção do lote, então comparamos o hash do
    conteúdo e ignoramos a repetição.
    """
    todas_pecas, info, vistos = [], [], {}
    for caminho, nome in arquivos:
        h = file_hash(caminho)
        if h in vistos:
            info.append({'arquivo': nome, 'n_linhas': 0, 'qtd_total': 0,
                         'duplicado_de': vistos[h]})
            continue
        vistos[h] = nome
        ignoradas: list = []
        pecas = parse_kambam(caminho, source_name=nome, ignoradas=ignoradas)
        todas_pecas.extend(pecas)
        # a outra metade do relatório: os MÓVEIS que este lote monta. Com ela
        # dá pra derivar quantas peças de cada tipo cada móvel leva.
        banco.importar_modelos(parse_itens(caminho), pecas)
        info.append({'arquivo': nome, 'n_linhas': len(pecas),
                     'qtd_total': sum(p['qtd'] for p in pecas),
                     'lote': pecas[0]['lote'] if pecas else None,
                     'material': pecas[0].get('material') if pecas else None,
                     # linhas de peça que o leitor não conseguiu interpretar
                     'ignoradas': ignoradas})
    return todas_pecas, info


def agrupar(todas_pecas: list, respeitar_veio: bool) -> dict:
    """
    Agrupa por (cor, espessura) - só compartilha chapa quem tem os dois iguais.

    Guardamos a demanda POR LOTE, não só o total: sem isso não dá pra dizer ao
    operador quantas peças de cada chapa vão pra cada Kambam, e as peças saem
    da máquina numa pilha só, sem destino.

    A rotação de cada peça sai do cruzamento do cadastro: a cor precisa ter
    desenho direcional E a peça precisa ficar aparente pra que girar seja
    proibido. Prateleira em chapa amadeirada gira; porta em chapa lisa gira.
    """
    regras_pecas, regras_cores = banco.regras()
    # Medida que você conferiu vale mais que a do Kambam. É o caso da 10313,
    # que vinha 442x450 no relatório e 450x442 no catálogo do Agrosys: como a
    # regra do veio depende de qual número é o comprimento, cortar pela fonte
    # errada inutiliza a peça.
    conferidas = banco.medidas_confirmadas()
    grupos = defaultdict(dict)
    for p in todas_pecas:
        cor, esp = p['cor'], p['esp_mm']
        if p['cod'] in conferidas:
            p = dict(p)
            p['comp_mm'], p['larg_mm'] = conferidas[p['cod']]
        chave = f"{p['cod']}_{int(p['comp_mm'])}x{int(p['larg_mm'])}"
        qtd = int(round(p['qtd']))
        gira = True if not respeitar_veio else \
            banco.pode_girar(p['cod'], cor, regras_pecas, regras_cores)
        d = grupos[(cor, esp)]
        if chave in d:
            d[chave].qty_total += qtd
            if p['origem'] not in d[chave].origem:
                d[chave].origem.append(p['origem'])
        else:
            d[chave] = PieceType(
                key=chave, cod=p['cod'], desc=p['desc'],
                w=int(round(p['comp_mm'])), h=int(round(p['larg_mm'])),
                qty_total=qtd, origem=[p['origem']],
                pode_girar=gira,
            )
        d[chave].demanda_por_lote[p['origem']] = \
            d[chave].demanda_por_lote.get(p['origem'], 0) + qtd
    return grupos


# Sugestão do aprendizado só entra em chapa que já tem pedido de verdade: numa
# chapa de resto quase vazia ela virava a maior parte da chapa (peça a mais só
# para encher sobra)
MIN_PEDIDO_SUGESTAO = 50.0


def aplicar_aprendizado(grupos_resultado: list, par: dict) -> list[dict]:
    """
    Repete sozinho o que o operador faz sempre.

    Uma peca chamada a mao mais de uma vez na mesma cor deixa de ser acaso e
    vira intencao. Quando um padrao novo tem sobra onde aquela peca cabe, o
    sistema ja coloca — marcada como sugestao, pra dar pra tirar.

    IMPORTANTE, e por isso as sugestoes vem separadas no relatorio: acrescentar
    peca aqui NAO economiza chapa. O plano do otimizador ja atende toda a
    demanda, entao o que entra na sobra e producao a mais, que so vale se voce
    quiser mesmo aquelas pecas. Esconder isso seria vender ganho que nao existe.
    """
    import edicao
    # catálogo inteiro: com teto fixo, peça além dele nunca seria sugerida
    pecas_cad = {p['cod']: p for p in banco.listar_pecas(limite=0)}
    aplicadas = []
    for g in grupos_resultado:
        # regras da cor + as aprendidas no plano manual (valem para toda cor)
        do_grupo = banco.regras_para_cor(g['cor'], minimo=2)
        if not do_grupo:
            continue
        # Mesma regra de consistência do otimizador principal (ver
        # column_generation.optimize_group_cg): a peça sugerida pelo
        # aprendizado entra numa orientação na primeira sobra onde couber e
        # trava nela pro resto do plano inteiro - senão a mesma sugestão
        # podia entrar "de pé" numa chapa e "de lado" noutra, só porque a
        # sobra de cada uma tinha um formato diferente.
        orientacao_travada: dict[str, bool] = {}
        for pad in g['padroes']:
            if pad['aproveitamento_pedido'] < MIN_PEDIDO_SUGESTAO:
                continue
            for regra in do_grupo:
                p = pecas_cad.get(regra['peca_cod'])
                if not p:
                    continue
                livres = edicao.retalhos_livres(pad['itens'], par['chapa_larg'], par['chapa_alt'],
                                                 kerf=par['kerf'])
                for ret in livres:
                    enc = edicao.encaixar(ret, p['comp_mm'], p['larg_mm'], par['kerf'],
                                           pode_girar=not (g['tem_veio'] and p['aparente']),
                                           forcar_giro=orientacao_travada.get(p['cod']))
                    if not enc:
                        continue
                    teste = pad['itens'] + [{'cod': p['cod'], 'desc': p['descricao'],
                                              'piece_key': p['cod'] + '_sugerido',
                                              'shelf': 0, **enc}]
                    if edicao.validar_padrao(teste, par['chapa_larg'], par['chapa_alt'],
                                              par['estagios']):
                        continue
                    pad['itens'] = teste
                    pad['sugerido'] = True
                    orientacao_travada.setdefault(p['cod'], enc['rotated'])
                    aplicadas.append({'cor': g['cor'], 'cod': p['cod'],
                                       'desc': p['descricao'], 'padrao': pad['n'],
                                       'vezes': regra['vezes'],
                                       'quantidade': pad['repeticoes']})
                    break
    return aplicadas


def colocar_tiras(grupos_resultado: list, par: dict) -> None:
    """
    Põe as tiras avulsas nas sobras estreitas de cada padrão e refaz os
    aproveitamentos.

    Roda DEPOIS do aprendizado de propósito: a peça sugerida tem prioridade
    sobre a tira no mesmo espaço. O aproveitamento do plano passa a ter dois
    números: `aproveitamento` (chapa inteira, tiras incluídas - o que o
    operador costuma citar) e `aproveitamento_pedido` (só as peças do pedido).
    """
    area_chapa = (par['chapa_larg'] / 1000) * (par['chapa_alt'] / 1000)
    for g in grupos_resultado:
        usada_total = usada_pedido = 0.0
        for pad in g['padroes']:
            novas = edicao.tiras_avulsas(pad['itens'], par['chapa_larg'], par['chapa_alt'],
                                          par['kerf'], par['estagios'])
            pad['itens'] += novas
            por_medida: dict = {}
            for t in novas:
                por_medida[t['cod']] = por_medida.get(t['cod'], 0) + 1
            for cod, qtd in por_medida.items():
                pad['pecas'].append({'cod': cod, 'desc': 'Peça avulsa (tira)', 'medida': cod,
                                      'por_chapa': qtd, 'total': qtd * pad['repeticoes'],
                                      'lotes': {}, 'tira': True})
            usada = sum(i['w'] * i['h'] for i in pad['itens']) / 1_000_000
            pedido = sum(i['w'] * i['h'] for i in pad['itens']
                          if not i['piece_key'].endswith(('_tira', '_sugerido'))) / 1_000_000
            pad['aproveitamento'] = 100 * usada / area_chapa
            pad['aproveitamento_pedido'] = 100 * pedido / area_chapa
            usada_total += usada * pad['repeticoes']
            usada_pedido += pedido * pad['repeticoes']
        chapas = g['n_chapas']
        g['aproveitamento_medio'] = 100 * usada_total / (chapas * area_chapa) if chapas else 0
        g['aproveitamento_pedido'] = 100 * usada_pedido / (chapas * area_chapa) if chapas else 0


def desenhar_padroes(grupos_resultado: list, par: dict, run_dir: str) -> None:
    """
    Gera o PNG de cada padrão a partir da geometria FINAL (com sugestões e
    tiras). Antes o desenho saía antes do aprendizado e a tela mostrava uma
    chapa diferente da que ia no PDF.
    """
    from types import SimpleNamespace
    for g in grupos_resultado:
        for pad in g['padroes']:
            chapa = SimpleNamespace(items=[SimpleNamespace(**i) for i in pad['itens']],
                                     repeticoes=pad['repeticoes'],
                                     aproveitamento=pad['aproveitamento'])
            render_sheet(chapa, par['chapa_larg'], par['chapa_alt'],
                         os.path.join(run_dir, pad['arquivo'].rsplit('/', 1)[-1]),
                         titulo=f"{g['cor']} · {g['esp']:.0f}mm · Padrão {pad['n']}",
                         veio=g['tem_veio'])


def rodar(arquivos: list[tuple[str, str]], run_dir: str, url_prefixo: str,
          respeitar_veio: bool = True, max_depth: int | None = None,
          maquina_id=None, progresso=None, dimensao_id=None) -> dict:
    """
    Calcula o plano de corte completo.

    progresso: callable(feito, total, texto) chamado a cada grupo concluído,
    pra alimentar a tela de acompanhamento enquanto o cálculo roda.
    """
    def aviso(feito, total, texto):
        if progresso:
            progresso(feito, total, texto)

    par = parametros(maquina_id, dimensao_id)
    largura, altura = par['chapa_larg'], par['chapa_alt']

    aviso(0, 1, 'Lendo os PDFs...')
    todas_pecas, kambans_info = ler_kambans(arquivos)
    if not todas_pecas:
        return {'erro': ('Não consegui extrair nenhuma peça dos PDFs enviados. '
                          'Verifique se o formato bate com o padrão do relatório Kambam.'),
                'kambans_info': kambans_info}

    # Todo Kamban processado alimenta o cadastro: peça nova entra com o palpite
    # automático, peça já conferida por você não é mexida.
    banco.criar_tabelas()
    importado = banco.importar(todas_pecas)
    aprendizado.semear_do_operador()
    combinacoes = banco.combinacoes()

    grupos = agrupar(todas_pecas, respeitar_veio)
    ordem_lotes = [k['arquivo'] for k in kambans_info if not k.get('duplicado_de')]
    os.makedirs(run_dir, exist_ok=True)

    total = len(grupos)
    grupos_resultado = []
    for n, ((cor, esp), tipos_dict) in enumerate(grupos.items(), start=1):
        aviso(n - 1, total, f'Otimizando {cor} {esp:.0f}mm ({n} de {total})...')
        tipos = list(tipos_dict.values())
        # "Maiores primeiro": a peça mais comprida define o padrão, a demanda
        # dela fica junta, e as menores completam os espaços (sequencial.py).
        sheets, sobras, padroes = optimize_group_sequencial(
            [PieceType(**{**t.__dict__}) for t in tipos],
            largura, altura, kerf=par['kerf'], estagios=par['estagios'],
            combinacoes=combinacoes,
        )

        rateio = ratear_por_lote(padroes, tipos_dict, ordem_lotes)
        por_chapa = max(1, int(par['pilha_max'] // esp)) if esp else 1

        blocos = []
        for i, pat in enumerate(padroes, start=1):
            img_name = f'{cor}_{int(esp)}mm_padrao{i}.png'.replace(' ', '_').replace('/', '-')
            blocos.append({
                'n': i,
                'familia': getattr(pat, 'familia', None),
                # A geometria vai junto: sem as coordenadas o plano salvo não
                # tem o que editar depois, e o desenho seria a única fonte —
                # imagem não dá pra validar nem recalcular.
                'itens': [{'cod': it.cod, 'desc': it.desc, 'piece_key': it.piece_key,
                            'x': round(it.x, 1), 'y': round(it.y, 1),
                            'w': round(it.w, 1), 'h': round(it.h, 1),
                            'rotated': bool(it.rotated)} for it in pat.items],
                'arquivo': f'{url_prefixo}/{img_name}',
                'repeticoes': pat.repeticoes,
                'aproveitamento': pat.aproveitamento,
                'aproveitamento_pedido': pat.aproveitamento,
                'ciclos': -(-pat.repeticoes // por_chapa),  # arredonda pra cima
                'pilha': por_chapa,
                'pecas': [
                    {'cod': tipos_dict[k].cod, 'desc': tipos_dict[k].desc,
                     'medida': f'{tipos_dict[k].w}x{tipos_dict[k].h}',
                     'por_chapa': q, 'total': q * pat.repeticoes,
                     'lotes': rateio[i - 1].get(k, {})}
                    for k, q in sorted(pat.counts.items(), key=lambda kv: -kv[1])
                ],
            })

        n_chapas = len(sheets)
        area_chapa = (largura / 1000) * (altura / 1000)
        grupos_resultado.append({
            'cor': cor,
            'esp': esp,
            'tem_veio': tem_veio(cor, respeitar_veio),
            'n_tipos_peca': len(tipos),
            'qtd_total_pecas': sum(t.qty_total for t in tipos),
            'n_chapas': n_chapas,
            'n_padroes': len(padroes),
            'ciclos_total': sum(b['ciclos'] for b in blocos),
            'aproveitamento_medio': (100 * sum(s.used_area for s in sheets)
                                      / (n_chapas * area_chapa) if n_chapas else 0),
            'aproveitamento_pedido': (100 * sum(s.used_area for s in sheets)
                                       / (n_chapas * area_chapa) if n_chapas else 0),
            'padroes': blocos,
            'sobras': [{'cod': s.cod, 'desc': s.desc, 'medida': f'{s.w}x{s.h}',
                        'qtd': s.qty_total} for s in sobras],
        })

    aviso(total, total, 'Aplicando o que o sistema aprendeu...')
    sugestoes = aplicar_aprendizado(grupos_resultado, par)

    aviso(total, total, 'Aproveitando as tiras das sobras...')
    colocar_tiras(grupos_resultado, par)
    desenhar_padroes(grupos_resultado, par, run_dir)

    aviso(total, total, 'Montando o plano...')
    grupos_resultado.sort(key=lambda g: -g['qtd_total_pecas'])
    return {
        'erro': None,
        'kambans_info': kambans_info,
        'grupos': grupos_resultado,
        'sheet_w': largura,
        'sheet_h': altura,
        'kerf': par['kerf'],
        'estagios': par['estagios'],
        'respeitar_veio': respeitar_veio,
        'maquina': par.get('maquina_nome'),
        'maquina_id': maquina_id,
        'pilha_max': par['pilha_max'],
        'total_chapas': sum(g['n_chapas'] for g in grupos_resultado),
        'sugestoes': sugestoes,
        'importado': importado,
        'cadastro': banco.resumo(),
    }
