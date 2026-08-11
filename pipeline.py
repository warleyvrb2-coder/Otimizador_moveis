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

import banco
from parser import parse_kambam, parse_itens, file_hash
from optimizer import PieceType
from column_generation import optimize_group_cg
from visualize import render_sheet

def parametros() -> dict:
    """Lidos a cada rodada, não na importação: assim mudar na tela vale já na
    próxima otimização, sem reiniciar o servidor."""
    return banco.obter_parametros()


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
        pecas = parse_kambam(caminho, source_name=nome)
        todas_pecas.extend(pecas)
        # a outra metade do relatório: os MÓVEIS que este lote monta. Com ela
        # dá pra derivar quantas peças de cada tipo cada móvel leva.
        banco.importar_modelos(parse_itens(caminho), pecas)
        info.append({'arquivo': nome, 'n_linhas': len(pecas),
                     'qtd_total': sum(p['qtd'] for p in pecas),
                     'lote': pecas[0]['lote'] if pecas else None,
                     'material': pecas[0].get('material') if pecas else None})
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
    grupos = defaultdict(dict)
    for p in todas_pecas:
        cor, esp = p['cor'], p['esp_mm']
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


def rodar(arquivos: list[tuple[str, str]], run_dir: str, url_prefixo: str,
          respeitar_veio: bool = True, max_depth: int | None = None,
          progresso=None) -> dict:
    """
    Calcula o plano de corte completo.

    progresso: callable(feito, total, texto) chamado a cada grupo concluído,
    pra alimentar a tela de acompanhamento enquanto o cálculo roda.
    """
    def aviso(feito, total, texto):
        if progresso:
            progresso(feito, total, texto)

    par = parametros()
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

    grupos = agrupar(todas_pecas, respeitar_veio)
    ordem_lotes = [k['arquivo'] for k in kambans_info if not k.get('duplicado_de')]
    os.makedirs(run_dir, exist_ok=True)

    total = len(grupos)
    grupos_resultado = []
    for n, ((cor, esp), tipos_dict) in enumerate(grupos.items(), start=1):
        aviso(n - 1, total, f'Otimizando {cor} {esp:.0f}mm ({n} de {total})...')
        tipos = list(tipos_dict.values())
        sheets, sobras, padroes = optimize_group_cg(
            [PieceType(**{**t.__dict__}) for t in tipos],
            largura, altura, time_budget_s=par['tempo_grupo'], max_depth=max_depth,
            kerf=par['kerf'], estagios=par['estagios'],
        )

        rateio = ratear_por_lote(padroes, tipos_dict, ordem_lotes)
        por_chapa = max(1, int(par['pilha_max'] // esp)) if esp else 1

        blocos = []
        for i, pat in enumerate(padroes, start=1):
            img_name = f'{cor}_{int(esp)}mm_padrao{i}.png'.replace(' ', '_').replace('/', '-')
            render_sheet(pat, largura, altura, os.path.join(run_dir, img_name),
                         titulo=f'{cor} · {esp:.0f}mm · Padrão {i}',
                         veio=tem_veio(cor, respeitar_veio))
            blocos.append({
                'n': i,
                'arquivo': f'{url_prefixo}/{img_name}',
                'repeticoes': pat.repeticoes,
                'aproveitamento': pat.aproveitamento,
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
            'padroes': blocos,
            'sobras': [{'cod': s.cod, 'desc': s.desc, 'medida': f'{s.w}x{s.h}',
                        'qtd': s.qty_total} for s in sobras],
        })

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
        'total_chapas': sum(g['n_chapas'] for g in grupos_resultado),
        'importado': importado,
        'cadastro': banco.resumo(),
    }
