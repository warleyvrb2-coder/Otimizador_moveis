# -*- coding: utf-8 -*-
"""
Otimizador de corte guilhotinado (2 estágios) para seccionadora, via CP-SAT.

Modelo por chapa (guillotine 2-stage / "shelf packing"):
  - 1º estágio: a chapa é dividida em faixas horizontais ("shelves").
  - 2º estágio: dentro de cada faixa, as peças ficam lado a lado.
  Isso é fisicamente compatível com uma seccionadora (corte de ponta a ponta).

Como a quantidade de peças de cada tipo pode ser grande (centenas/milhares),
não modelamos peça-a-peça: cada "tipo" de peça (mesma cor+espessura+dimensão)
tem uma variável inteira de QUANTIDADE por (faixa, orientação). Isso mantém o
modelo pequeno (poucos tipos) mesmo com milhares de unidades.

Para permitir que a sobra de uma chapa sirva peças de outro Kambam (pool
combinado), resolvemos chapa a chapa, sempre a partir do MESMO pool restante
(cor+espessura), até esgotar as peças ou atingir um limite de segurança.
"""
import copy
import math
from dataclasses import dataclass, field, replace
from ortools.sat.python import cp_model

SCALE = 1  # dimensões já em mm inteiros

# Espessura do disco da seccionadora. TODO corte come esse material, então
# N peças lado a lado ocupam N*medida + (N-1)*KERF, não N*medida. Ignorar
# isso produz layouts que fecham no papel e não fecham na máquina.
KERF_MM = 4.4


def _cabem(espaco: float, medida: float, kerf: float) -> int:
    """Quantas peças de `medida` cabem em `espaco` considerando o kerf."""
    if medida <= 0 or espaco < medida:
        return 0
    return int((espaco + kerf) // (medida + kerf))


def _ocupa(n: int, medida: float, kerf: float) -> float:
    """Espaço consumido por n peças em fila, com os cortes entre elas."""
    return n * medida + max(0, n - 1) * kerf if n > 0 else 0.0


@dataclass
class PieceType:
    key: str            # identificador único (cod+dimensões)
    cod: str
    desc: str
    w: int               # comprimento (mm) - SEMPRE no sentido do veio
    h: int               # largura (mm)
    qty_total: int
    origem: list = field(default_factory=list)  # quais kambans/lotes contribuíram
    # Chapa amadeirada tem direção: o desenho corre no comprimento da chapa.
    # Peça desse material NÃO pode girar, senão o veio sai atravessado e o
    # móvel vira refugo. No Kambam o comprimento já vem no sentido do veio,
    # então "não girar" é a regra inteira. Cor lisa (branco) pode girar.
    pode_girar: bool = True
    # quantas unidades cada Kambam de origem pediu - {lote: qtd}
    demanda_por_lote: dict = field(default_factory=dict)

    def orientacoes(self):
        """(largura, altura, girada) permitidas para esta peça."""
        yield self.w, self.h, False
        if self.pode_girar:
            yield self.h, self.w, True


@dataclass
class PlacedItem:
    piece_key: str
    cod: str
    desc: str
    shelf: int
    x: float
    y: float
    w: float
    h: float
    rotated: bool


@dataclass
class SheetResult:
    index: int
    items: list
    used_area: float
    sheet_area: float

    @property
    def aproveitamento(self) -> float:
        return 100.0 * self.used_area / self.sheet_area if self.sheet_area else 0.0


def _best_grid_fit(pool, w, h, strategy: str = 'area', value_dict: dict | None = None,
                    kerf: float = KERF_MM):
    """
    Entre os tipos com estoque > 0, acha o que melhor preenche um
    retângulo w x h formando uma grade (nx colunas x ny linhas),
    testando as duas orientações. Retorna (piece, iw, ih, rotated, nx, ny, count)
    ou None. O critério de escolha varia pela estratégia:
      - 'area'  : maior área total coberta (padrão, guloso puro)
      - 'width' : maior aproveitamento da LARGURA do retângulo (nx*iw/w),
                  tentando deixar sobra de altura (tira embaixo) em vez de
                  sobra de largura (tira lateral) — bom quando a tira
                  lateral tende a ficar estreita demais pra reaproveitar.
      - 'height': o espelho de 'width', prioriza aproveitar a ALTURA.
    """
    best = None
    best_score = -1
    for p in pool:
        if p.qty_total <= 0:
            continue
        for iw, ih, rotated in p.orientacoes():
            if iw <= 0 or ih <= 0 or iw > w or ih > h:
                continue
            nx = _cabem(w, iw, kerf)
            ny = _cabem(h, ih, kerf)
            count = min(p.qty_total, nx * ny)
            if count <= 0:
                continue
            if strategy == 'width':
                score = _ocupa(nx, iw, kerf) / w
            elif strategy == 'height':
                score = _ocupa(ny, ih, kerf) / h
            elif strategy == 'value':
                score = count * (value_dict.get(p.key, 0.0) if value_dict else 0.0)
            else:
                score = count * iw * ih
            if score > best_score:
                best_score = score
                best = (p, iw, ih, rotated, nx, ny, count)
    return best


def _pack_rect(pool, x0: float, y0: float, w: float, h: float,
               placed: list, qty_used: dict, min_piece_area: float,
               strategy: str = 'area', depth: int = 0, value_dict: dict | None = None,
               max_depth: int | None = None, kerf: float = KERF_MM):
    """
    Empacotamento guilhotinado recursivo: preenche o retângulo (x0,y0,w,h)
    com uma grade do melhor tipo de peça que achar, depois corta o que
    sobrou em DUAS tiras retangulares (direita e embaixo do bloco usado)
    e chama a si mesma em cada uma - permitindo corte tanto no sentido
    longitudinal quanto no transversal, alternando conforme a sobra.

    max_depth limita quantos "re-cortes" empilhados um pedaço pode sofrer
    (cada nível de recursão = pegar o pedaço resultante do corte anterior
    e cortar ele de novo). None = sem limite (só a trava de segurança em
    depth>60). Isso existe pra respeitar quantos estágios de corte a
    seccionadora consegue fazer numa chapa sem reprogramar demais.
    """
    if w <= 0 or h <= 0 or depth > 60 or w * h < min_piece_area:
        return
    if max_depth is not None and depth >= max_depth:
        return  # não corta mais essa sobra - fica sem uso, mas dentro do nº de estágios permitido

    fit = _best_grid_fit(pool, w, h, strategy, value_dict, kerf)
    if fit is None:
        return
    p, iw, ih, rotated, nx, ny, count = fit

    # o bloco ocupa as peças MAIS os cortes entre elas
    used_w = _ocupa(nx, iw, kerf)
    used_h = _ocupa(ny, ih, kerf)

    placed_count = 0
    for r in range(ny):
        for c in range(nx):
            if placed_count >= count:
                break
            placed.append(PlacedItem(
                piece_key=p.key, cod=p.cod, desc=p.desc, shelf=0,
                x=x0 + c * (iw + kerf), y=y0 + r * (ih + kerf),
                w=iw, h=ih, rotated=rotated,
            ))
            placed_count += 1
        if placed_count >= count:
            break

    p.qty_total -= count
    qty_used[p.key] = qty_used.get(p.key, 0) + count

    # corte guilhotinado: uma tira à direita do bloco usado (altura toda) e
    # uma tira embaixo do bloco usado (só a largura que ele ocupou). Cada
    # uma começa DEPOIS de um corte, por isso o +kerf no deslocamento.
    _pack_rect(pool, x0 + used_w + kerf, y0, w - used_w - kerf, h,
               placed, qty_used, min_piece_area, strategy, depth + 1, value_dict, max_depth, kerf)
    _pack_rect(pool, x0, y0 + used_h + kerf, used_w, h - used_h - kerf,
               placed, qty_used, min_piece_area, strategy, depth + 1, value_dict, max_depth, kerf)


def _melhor_para_faixa(pool, restantes: dict, largura_livre: float, elegivel, kerf: float, strategy: str):
    """
    Entre os tipos com estoque em `restantes`, acha o que melhor preenche o
    que sobrou de LARGURA da faixa - uma linha só (nx colunas, sem empilhar
    peça em cima de peça dentro da faixa, que exigiria um terceiro corte).
    """
    melhor, melhor_score = None, -1
    for p in pool:
        disponivel = restantes.get(p.key, 0)
        if disponivel <= 0:
            continue
        for iw, ih, rot in p.orientacoes():
            if not elegivel(ih) or iw > largura_livre:
                continue
            nx = _cabem(largura_livre, iw, kerf)
            count = min(disponivel, nx)
            if count <= 0:
                continue
            score = _ocupa(count, iw, kerf) if strategy == 'width' else count * iw * ih
            if score > melhor_score:
                melhor_score = score
                melhor = (p, iw, ih, rot, count)
    return melhor


def _pack_shelves(pool, sheet_w: int, sheet_h: int, placed: list, qty_used: dict,
                   strategy: str = 'area', kerf: float = KERF_MM, estagios: int = 3) -> None:
    """
    Empacotamento em FAIXAS (shelf packing): a chapa vira uma pilha de tiras
    horizontais, cada uma cheia de peças lado a lado. Sempre 2 estágios - o
    corte que separa as faixas e o corte que separa as peças dentro de cada
    faixa - mais um aparo por faixa quando a peça é mais baixa que ela
    (permitido só se estagios >= 3).

    Existe pra SUBSTITUIR o empacotamento recursivo geral (_pack_rect) como
    semente do Column Generation. Aquele produz árvores de corte
    tecnicamente guilhotináveis, mas com peça pequena encravada ao lado de
    peça grande em posições que cada uma exige seu próprio corte - passa na
    contagem de estágios, mas é impraticável de acompanhar numa pilha real
    de chapas empilhadas. Faixa é o único formato que corresponde a como a
    seccionadora corta de verdade: separar a chapa em tiras primeiro, cada
    tira depois vira peças com um corte só, sem posição surpresa no meio.
    """
    y = 0.0
    faixas_geradas = []  # (h_faixa, escolhas) na ordem em que a busca gulosa achou cada uma
    while sheet_h - y > 1:
        h_livre = sheet_h - y
        # Candidata por altura de peça, mas só as MAIS PROMISSORAS: com
        # muitos tipos de peça (um lote grande passa de 100), testar TODAS
        # as alturas possíveis - cada uma exigindo um preenchimento guloso
        # completo pra avaliar - faz o cálculo de um padrão só levar minutos.
        # Prioriza pela maior área que aquela altura sozinha já garante (a
        # da peça mais valiosa que a atinge), que é o mesmo critério que
        # decide no fim - só corta as candidatas que nunca ganhariam mesmo.
        potencial: dict[float, float] = {}
        for p in pool:
            if p.qty_total <= 0:
                continue
            for iw, ih, _ in p.orientacoes():
                if ih <= h_livre:
                    potencial[ih] = max(potencial.get(ih, 0.0), p.qty_total * iw * ih)
        alturas = sorted(potencial, key=potencial.get, reverse=True)[:15]
        melhor = None  # (altura_da_faixa, [(peca, iw, ih, rot, count), ...], area_coberta, densidade)
        for h_faixa in alturas:
            elegivel = ((lambda ih, hf=h_faixa: abs(ih - hf) < 0.5) if estagios <= 2 else
                        (lambda ih, hf=h_faixa: ih <= hf + 0.5))
            restantes = {p.key: p.qty_total for p in pool}
            x, area, escolhas = 0.0, 0.0, []
            while sheet_w - x > 1:
                cand = _melhor_para_faixa(pool, restantes, sheet_w - x, elegivel, kerf, strategy)
                if cand is None:
                    break
                p, iw, ih, rot, count = cand
                escolhas.append((p, iw, ih, rot, count))
                restantes[p.key] -= count
                x += _ocupa(count, iw, kerf) + kerf
                area += count * iw * ih
            if not escolhas:
                continue
            # Densidade (área / altura da faixa), não área crua: com
            # estagios>=3 uma peça de 580mm é elegível também numa faixa
            # "candidata" de 1035mm (por causa do aparo permitido) - se só
            # ELA coubesse mesmo assim, a área coberta empataria com a da
            # faixa de 580mm certa, e a comparação por área pura ficava com
            # a mais alta das duas por ter sido avaliada primeiro,
            # desperdiçando os 455mm de diferença em vez de reaproveitá-los
            # numa faixa seguinte.
            densidade = area / h_faixa
            if melhor is None or densidade > melhor[3]:
                melhor = (h_faixa, escolhas, area, densidade)
        if melhor is None:
            break  # nada mais coube na altura que sobrou - fica sem uso

        h_faixa, escolhas, _, _ = melhor
        # A escolha gulosa acima decide QUAIS tipos entram na faixa e QUANTOS
        # de cada, mas na ordem em que cada um "ganhou" a comparação de score -
        # que pode intercalar peça grande com pequena sem nenhum motivo prático
        # (ex.: peça de 562mm de comprimento logo depois de uma de 1730mm na
        # mesma faixa). Reordenar aqui por comprimento decrescente não muda
        # quantidade nem área usada - só a posição x de cada bloco - e dá uma
        # faixa com cadência de corte previsível: da peça mais longa pra mais
        # curta, sempre a mesma sequência dentro do mesmo padrão. O código
        # como critério de desempate (peça de mesmo comprimento, código
        # diferente) agrupa as duas cópias juntas em vez de deixar a ordem
        # de descoberta da busca gulosa intercalar os dois códigos.
        escolhas = sorted(escolhas, key=lambda e: (-e[1], e[0].cod))
        faixas_geradas.append((h_faixa, escolhas))
        for p, iw, ih, rot, count in escolhas:
            p.qty_total -= count
            qty_used[p.key] = qty_used.get(p.key, 0) + count
        y += h_faixa + kerf

    # A mesma busca gulosa acima, olhando faixa por faixa sem lembrar da
    # anterior, também intercala FAIXAS inteiras: uma peça cuja demanda não
    # cabe toda numa faixa só (ex.: 8 unidades de 900mm de comprimento, só 4
    # cabem de altura) fica com o restante empurrado pra uma faixa mais
    # adiante, porque nessa hora outra peça pontuou melhor - o mesmo código
    # saindo em 2 faixas separadas por uma faixa de peça diferente no meio.
    # Reordenar a lista de faixas antes de definir o Y final resolve isso do
    # mesmo jeito que o reordenamento acima resolve dentro de uma faixa só: a
    # altura total ocupada é a soma das faixas independente da ordem, então
    # mudar a ordem não afeta o que cabe.
    #
    # Critério: comprimento decrescente da peça dominante de cada faixa
    # (escolhas já vem ordenada por comprimento, então escolhas[0] é ela) -
    # não largura. Faz a peça de MAIOR comprimento do padrão sempre cair
    # numa ponta da pilha (a primeira faixa, no topo), nunca espremida no
    # meio só porque a largura dela é menor que a de outra faixa qualquer -
    # e, por ser sort estável, duas faixas do mesmo comprimento (código
    # diferente, mesma medida) ficam na ordem do desempate por código,
    # abaixo, em vez de na ordem em que a busca gulosa as achou.
    faixas_geradas.sort(key=lambda f: (-f[1][0][1], f[1][0][0].cod))
    y = 0.0
    for h_faixa, escolhas in faixas_geradas:
        x = 0.0
        for p, iw, ih, rot, count in escolhas:
            for i in range(count):
                placed.append(PlacedItem(piece_key=p.key, cod=p.cod, desc=p.desc, shelf=0,
                                          x=x + i * (iw + kerf), y=y, w=iw, h=ih, rotated=rot))
            x += _ocupa(count, iw, kerf) + kerf
        y += h_faixa + kerf


def _pack_columns(pool, sheet_w: int, sheet_h: int, placed: list, qty_used: dict,
                   strategy: str = 'area', kerf: float = KERF_MM, estagios: int = 3) -> None:
    """
    O espelho de _pack_shelves: em vez de FAIXAS horizontais, empacota em
    COLUNAS verticais lado a lado, cada uma cheia de peças empilhadas de
    cima a baixo. Pra peças que compartilham a mesma LARGURA (em vez da
    mesma altura), coluna encaixa melhor que faixa - sem isso, peças de
    2280mm de largura ficavam cada uma na sua própria faixa com uma sobra de
    altura desperdiçada ao lado, quando na verdade cabiam empilhadas juntas
    numa coluna só, sem nenhum desperdício de altura entre elas.

    Implementado chamando _pack_shelves com os eixos trocados e transpondo
    o resultado de volta - mesma lógica, só que "deitada".

    Peça com veio travado (pode_girar=False) só tem UMA orientação válida
    no mundo físico: comprimento (w) sempre no sentido do veio da chapa.
    Como aqui os eixos internos estão trocados, a orientação "sem rotação"
    que o _pack_shelves de baixo aplicaria devolveria o comprimento no
    eixo físico ERRADO depois de transpor de volta - a peça saía virada na
    chapa mesmo sem nenhuma flag marcar "rotated" (o bug que fazia a mesma
    peça de material com veio aparecer deitada num padrão e em pé noutro,
    dentro do mesmo plano). Por isso ela entra na chamada interna já com
    w/h pré-trocados: o que pro empacotador de faixas é "sem rotação" volta,
    depois da transposição abaixo, com o veio no eixo físico certo. Peça
    que pode girar não precisa disso - qualquer uma das duas orientações é
    válida pra ela, então entra como está.
    """
    pool_eixos_trocados = [p if p.pode_girar else replace(p, w=p.h, h=p.w) for p in pool]
    peca_por_key = {p.key: p for p in pool}

    placed_t: list = []
    _pack_shelves(pool_eixos_trocados, sheet_h, sheet_w, placed_t, qty_used,
                  strategy=strategy, kerf=kerf, estagios=estagios)
    for it in placed_t:
        final_w, final_h = it.h, it.w
        # "rotated" é sempre em relação ao cadastro (w = comprimento no
        # sentido do veio) - comparar contra a peça original, nunca contra
        # a flag interna do _pack_shelves, que perde o significado aqui
        # depois da troca de eixos acima.
        original = peca_por_key.get(it.piece_key)
        girada = bool(original) and final_w != original.w
        placed.append(PlacedItem(piece_key=it.piece_key, cod=it.cod, desc=it.desc, shelf=0,
                                  x=it.y, y=it.x, w=final_w, h=final_h, rotated=girada))


def _pack_split_2_colunas(pool, sheet_w: int, sheet_h: int, kerf: float, estagios: int, strategy: str):
    """
    Tenta um corte vertical ÚNICO dividindo a chapa em 2 colunas lado a
    lado, cada uma depois empacotada em faixas de forma INDEPENDENTE.

    É a diferença entre um padrão a 88% (tudo forçado nas mesmas faixas,
    peça de 350mm de altura ao lado de peça de 404mm desperdiçando os 54mm
    de diferença) e um a 96% (peças largas numa coluna só entre si, peças
    estreitas noutra, cada grupo com sua própria sequência de alturas sem
    misturar com a do vizinho). Sem isso, a única saída pra peças de
    larguras muito diferentes era compartilhar faixa e desperdiçar a
    diferença de altura entre elas.

    Devolve (placed, qty_used, area_coberta) da melhor largura de corte
    tentada, ou None se nenhuma peça coube.
    """
    # Mesmo raciocínio do limite de alturas em _pack_shelves: com muitos
    # tipos de peça, testar TODA largura possível (2 empacotamentos
    # completos por tentativa) é caro demais. Prioriza pela peça mais
    # valiosa que atinge aquela largura.
    potencial: dict[float, float] = {}
    for p in pool:
        if p.qty_total <= 0:
            continue
        for iw, ih, _ in p.orientacoes():
            if iw < sheet_w - 50:
                potencial[iw] = max(potencial.get(iw, 0.0), p.qty_total * iw * ih)
    larguras = sorted(potencial, key=potencial.get, reverse=True)[:6]
    melhor = None
    for iw in larguras:
        pool_a = [copy.copy(p) for p in pool]
        placed_a, qty_a = [], {}
        _pack_shelves(pool_a, iw, sheet_h, placed_a, qty_a, strategy=strategy, kerf=kerf, estagios=estagios)
        if not placed_a:
            continue

        resto = sheet_w - iw - kerf
        placed_b, qty_b = [], {}
        if resto > 50:
            pool_b = [copy.copy(p) for p in pool]
            _pack_shelves(pool_b, resto, sheet_h, placed_b, qty_b, strategy=strategy, kerf=kerf, estagios=estagios)
            for it in placed_b:
                it.x += iw + kerf

        placed = placed_a + placed_b
        area = sum(it.w * it.h for it in placed)
        if melhor is None or area > melhor[2]:
            qty_used = dict(qty_a)
            for k, v in qty_b.items():
                qty_used[k] = qty_used.get(k, 0) + v
            melhor = (placed, qty_used, area)
    return melhor


STRATEGIES = ('area', 'width', 'height')


def _pack_sheet_best_of(pool, sheet_w: int, sheet_h: int, min_piece_area: float):
    """
    "Beam search" leve: roda o empacotamento guilhotinado recursivo com
    várias estratégias de escolha (multi-start), cada uma numa CÓPIA do
    pool (sem mexer no pool real), e fica com o resultado de MAIOR área
    aproveitada. Só então aplica a baixa de estoque no pool de verdade.
    """
    import copy

    best_placed, best_qty_used, best_area = None, None, -1
    for strategy in STRATEGIES:
        pool_copy = [copy.copy(p) for p in pool]  # PieceType raso: qty_total é int, seguro copiar raso
        placed, qty_used = [], {}
        _pack_rect(pool_copy, 0, 0, sheet_w, sheet_h, placed, qty_used, min_piece_area, strategy)
        area = sum(it.w * it.h for it in placed)
        if area > best_area:
            best_area = area
            best_placed, best_qty_used = placed, qty_used

    for key, qty in best_qty_used.items():
        for p in pool:
            if p.key == key:
                p.qty_total -= qty
                break
    return best_placed, best_qty_used


def _refine_last_sheet_cpsat(pool, sheet_w: int, sheet_h: int, max_shelves: int, time_limit_s: float,
                              value_dict: dict | None = None, kerf: float = KERF_MM,
                              estagios: int = 3):
    """
    Resolve o empacotamento de 1 chapa via CP-SAT (modelo de faixas),
    maximizando ÁREA por padrão (uso normal, refino da última chapa) ou,
    se value_dict for passado, maximizando o VALOR (usado como pricing
    exato do column generation, onde o valor é o dual de cada peça).

    estagios controla quantas viradas de chapa o padrão pode exigir:
      2 - toda peça da faixa tem EXATAMENTE a altura da faixa, então o
          segundo corte já entrega a peça pronta. É o mais restritivo e
          o mais simples de executar.
      3 - permite peça mais baixa que a faixa, deixando uma sobra que sai
          num corte de aparo (sempre o último, sempre na mesma direção).
          É o "2 estágios + aparo" usado na maioria das fábricas.

    O kerf entra nas duas dimensões: peças lado a lado dentro da faixa e
    faixas empilhadas gastam um corte entre cada duas.
    """
    model = cp_model.CpModel()
    usable = [p for p in pool if p.qty_total > 0 and
              any(iw <= sheet_w and ih <= sheet_h for iw, ih, _ in p.orientacoes())]
    if not usable:
        return [], {}

    # O CP-SAT só aceita coeficiente inteiro, mas o posicionamento usa o kerf
    # real (4,4mm). Arredondar para BAIXO faz a restrição reservar menos do que
    # o desenho consome, e a diferença acumula faixa a faixa até a última peça
    # ficar pendurada para fora da chapa — foi o que aconteceu: 0,4mm por corte
    # viraram 2,8mm de estouro em seis faixas. Arredondando para CIMA o modelo
    # fica conservador, e o que ele aprova sempre cabe.
    K = math.ceil(kerf)
    shelves = range(max_shelves)
    x = {}
    dim = {}  # (key, s, o) -> (largura, altura) já com a rotação aplicada
    for p in usable:
        for s in shelves:
            for iw, ih, rot in p.orientacoes():
                o = 1 if rot else 0
                if iw <= sheet_w and ih <= sheet_h:
                    x[(p.key, s, o)] = model.NewIntVar(0, int(p.qty_total), f'x_{p.key}_{s}_{o}')
                    dim[(p.key, s, o)] = (iw, ih)

    for p in usable:
        vars_p = [x[k] for k in x if k[0] == p.key]
        if vars_p:
            model.Add(sum(vars_p) <= int(p.qty_total))

    shelf_h = [model.NewIntVar(0, sheet_h, f'shelf_h_{s}') for s in shelves]
    for s in shelves:
        for chave in [k for k in x if k[1] == s]:
            h = dim[chave][1]
            b = model.NewBoolVar(f'used_{chave[0]}_{s}_{chave[2]}')
            model.Add(x[chave] >= 1).OnlyEnforceIf(b)
            model.Add(x[chave] == 0).OnlyEnforceIf(b.Not())
            if estagios <= 2:
                # a faixa tem a altura exata das peças que estão nela
                model.Add(shelf_h[s] == h).OnlyEnforceIf(b)
            else:
                model.Add(shelf_h[s] >= h).OnlyEnforceIf(b)

        # dentro da faixa: soma das larguras + um corte entre cada duas peças.
        # Modelamos como (largura + kerf) por peça e devolvemos um kerf no
        # limite, que equivale a n*larg + (n-1)*kerf <= sheet_w.
        width_terms = [x[k] * (dim[k][0] + K) for k in x if k[1] == s]
        if width_terms:
            model.Add(sum(width_terms) <= sheet_w + K)

    # faixas empilhadas: mesmo raciocínio na vertical
    usada = [model.NewBoolVar(f'faixa_{s}') for s in shelves]
    for s in shelves:
        model.Add(shelf_h[s] >= 1).OnlyEnforceIf(usada[s])
        model.Add(shelf_h[s] == 0).OnlyEnforceIf(usada[s].Not())
    model.Add(sum(shelf_h) + K * sum(usada) <= sheet_h + K)

    if value_dict is not None:
        # pricing exato: maximiza soma(dual_i * quantidade_i), valores em
        # float -> escala pra inteiro (CP-SAT não aceita coeficiente float)
        SCALE_V = 1000
        obj_terms = [x[k] * int(round(value_dict.get(k[0], 0.0) * SCALE_V)) for k in x]
    else:
        obj_terms = [x[k] * dim[k][0] * dim[k][1] for k in x]
    model.Maximize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], {}

    por_key = {p.key: p for p in usable}
    # O solver é livre pra atribuir qualquer combinação de peça a qualquer
    # índice de faixa - nada impede a demanda de um código que não cabe
    # numa faixa só de sair espalhada em índices de faixa não-adjacentes,
    # com a faixa de outro código encaixada no meio (mesmo problema do
    # laço guloso de _pack_shelves, ver correção lá). Por isso a posição Y
    # final só é decidida DEPOIS: aqui só se monta a lista de faixas na
    # ordem natural dos índices do solver, sem posicionar nada ainda.
    faixas = []  # (h_s, [(piece, w, h, rotated, qty), ...])
    qty_used = {}
    for s in shelves:
        h_s = solver.Value(shelf_h[s])
        if h_s == 0:
            continue
        itens_da_faixa = []
        for chave in [k for k in x if k[1] == s]:
            qty = solver.Value(x[chave])
            if qty <= 0:
                continue
            p = por_key[chave[0]]
            w, h = dim[chave]
            itens_da_faixa.append((p, w, h, bool(chave[2]), qty))
            qty_used[p.key] = qty_used.get(p.key, 0) + qty
        if itens_da_faixa:
            faixas.append((h_s, itens_da_faixa))

    # Mesmo critério de _pack_shelves: reordenar por comprimento decrescente
    # da peça dominante da faixa (a de maior comprimento nela) antes de
    # definir o Y - faz a peça mais longa do padrão cair numa ponta da
    # pilha, e o código dela entra como desempate (sort estável) pra duas
    # faixas do mesmo comprimento mas código diferente (ex.: duas peças de
    # medida igual, código diferente) ficarem adjacentes em vez de na ordem
    # em que o solver as atribuiu. Não muda quantidade nem viabilidade, só
    # a ordem de empilhamento.
    def _peca_dominante(faixa):
        return max(faixa[1], key=lambda item: item[1])  # item = (peca, w, h, rotated, qtd)

    faixas.sort(key=lambda f: (-_peca_dominante(f)[1], _peca_dominante(f)[0].cod))

    placed = []
    y_cursor = 0.0
    for h_s, itens_da_faixa in faixas:
        # mesma cadência de corte da versão heurística: peça mais longa
        # primeiro, código como desempate, dentro da própria faixa.
        itens_da_faixa = sorted(itens_da_faixa, key=lambda item: (-item[1], item[0].cod))
        x_cursor = 0.0
        for p, w, h, rotated, qty in itens_da_faixa:
            for _ in range(qty):
                placed.append(PlacedItem(piece_key=p.key, cod=p.cod, desc=p.desc,
                                          shelf=0, x=x_cursor, y=y_cursor, w=w, h=h,
                                          rotated=rotated))
                x_cursor += w + kerf   # o corte entre duas peças da faixa
        y_cursor += h_s + kerf         # o corte entre duas faixas
    return placed, qty_used


def _refine_last_sheet_cpsat_colunas(pool, sheet_w: int, sheet_h: int, max_shelves: int,
                                      time_limit_s: float, value_dict: dict | None = None,
                                      kerf: float = KERF_MM, estagios: int = 3):
    """
    Versão em COLUNAS do modelo exato acima - o mesmo raciocínio de
    _pack_columns, mas pro CP-SAT.

    O modelo exato só sabe montar em faixas horizontais. Isso é ótimo
    quando as peças compartilham altura, mas quando muitas cópias do MESMO
    código compartilham LARGURA (e não altura - ex.: uma peça de 280mm e
    outra de 198mm de altura, sempre paredas), a versão em faixa intercala
    as duas por linha e sobra uma tira de ~(280-198)mm ao lado de CADA
    linha, em vez de uma vez só no fim de uma coluna com as 2 pilhas
    separadas. Chamado com os eixos trocados e o resultado transposto de
    volta, igual _pack_columns - inclusive a mesma pré-troca de w/h antes
    de entrar no modelo: aqui toda peça já chega com pode_girar=False
    (optimize_group_cg trava a orientação antes de gerar qualquer padrão),
    então sem essa pré-troca o comprimento cairia no eixo físico errado
    depois de transpor, do mesmo jeito que dava em _pack_columns.

    Roda um CP-SAT inteiro a mais por chamada (o de faixa e o de coluna,
    fica-se com o melhor) - o preço de ter as duas opções na hora do
    pricing exato do Column Generation.
    """
    pool_eixos_trocados = [p if p.pode_girar else replace(p, w=p.h, h=p.w) for p in pool]
    peca_por_key = {p.key: p for p in pool}

    placed_t, qty_used = _refine_last_sheet_cpsat(pool_eixos_trocados, sheet_h, sheet_w, max_shelves,
                                                   time_limit_s, value_dict=value_dict, kerf=kerf,
                                                   estagios=estagios)
    placed = []
    for it in placed_t:
        final_w, final_h = it.h, it.w
        original = peca_por_key.get(it.piece_key)
        girada = bool(original) and final_w != original.w
        placed.append(PlacedItem(piece_key=it.piece_key, cod=it.cod, desc=it.desc, shelf=it.shelf,
                                  x=it.y, y=it.x, w=final_w, h=final_h, rotated=girada))
    return placed, qty_used


def optimize_group(pieces: list[PieceType], sheet_w_mm: int, sheet_h_mm: int,
                    max_shelves: int = 12, time_limit_s: float = 6.0,
                    max_sheets: int = 2000, refine_last_with_cpsat: bool = True) -> tuple:
    """
    Empacota TODO o pool de um grupo (mesma cor+espessura), chapa a chapa,
    até esgotar as peças. Como o pool é único e compartilhado, sobra de uma
    chapa pode ser preenchida com peças de qualquer Kambam do grupo.

    Usa a heurística rápida de faixas para o grosso das chapas (escala bem
    até milhares de peças) e, opcionalmente, refina a ÚLTIMA chapa de cada
    grupo com CP-SAT exato (pool pequeno nesse ponto, resolve em segundos).
    """
    remaining = {p.key: p for p in pieces}
    sheet_area = (sheet_w_mm / 1000) * (sheet_h_mm / 1000)
    results = []

    min_piece_area = min((p.w * p.h for p in pieces if p.w and p.h), default=1)

    for sheet_idx in range(1, max_sheets + 1):
        pool = [p for p in remaining.values() if p.qty_total > 0]
        if not pool:
            break

        total_qty_restante = sum(p.qty_total for p in pool)
        use_cpsat = refine_last_with_cpsat and total_qty_restante <= 60

        if use_cpsat:
            placed, qty_used = _refine_last_sheet_cpsat(pool, sheet_w_mm, sheet_h_mm, max_shelves, time_limit_s)
        else:
            placed, qty_used = [], {}
            pool_copy = [copy.copy(p) for p in pool]  # evita decrementar o pool real 2x
            _pack_rect(pool_copy, 0, 0, sheet_w_mm, sheet_h_mm, placed, qty_used, min_piece_area)

        if not placed:
            break  # nada mais coube (sobrou peça grande demais, etc.)

        used_area = sum((it.w / 1000) * (it.h / 1000) for it in placed)
        results.append(SheetResult(index=sheet_idx, items=placed,
                                    used_area=used_area, sheet_area=sheet_area))

        for key, qty in qty_used.items():
            remaining[key].qty_total -= qty

    sobras = [p for p in remaining.values() if p.qty_total > 0]
    return results, sobras
