# Changelog

Histórico do que foi ajustado no projeto, sessão por sessão. Cada entrada
explica o problema real por trás da mudança — não só o "o quê", mas o
"porquê" — porque isso é o que evita reabrir o mesmo bug dois meses depois.

## 2026-09-17 (6) — Peça avulsa entra exatamente no espaço escolhido, não no "melhor lugar"

### O problema: cliquei num espaço, mandei incluir, e a peça foi pra outro canto

A peça avulsa por dimensão (entrada anterior) sempre usava busca de melhor
encaixe em QUALQUER canto livre da chapa (`_melhor_encaixe_valido`), mesmo
quando a pessoa já tinha clicado num espaço específico antes de digitar a
dimensão. Reportado com print: espaço de 724×1306mm selecionado, dimensão
240×240 digitada, e a peça apareceu num espaço completamente diferente
(porque aquele outro cabia com menos desperdício).

**A correção**: o formulário de peça avulsa agora manda também o índice do
espaço selecionado (`sobraSel`) quando existe um. No servidor
(`aplicar_edicao`, `app.py`), a ação `incluir_avulsa` passou a ter dois
caminhos - com espaço escolhido, usa a nova `_encaixar_repetido_no_retalho`
(encaixa ali, e SÓ ali, repetindo pra cada unidade pedida dentro do que
ainda sobra daquele mesmo espaço - nunca pulando pra outro canto da
chapa); sem espaço escolhido (formulário sem nenhuma sobra marcada), segue
com o comportamento de sempre (melhor encaixe em qualquer canto). A nova
`_contido` garante que, depois de cada peça encaixada, a próxima só
considera sobra que ainda está dentro dos limites do espaço ORIGINAL
escolhido - não qualquer sobra do mesmo tamanho que exista em outro lugar
da chapa por coincidência.

Ajustado nas duas telas (`editar.html` e `manual.html`) - no plano manual
o formulário de peça avulsa saiu do painel "lista geral" (onde nunca
haveria espaço selecionado) para um cartão fixo, visível tanto com a
lista geral quanto com um espaço específico escolhido.

Testado isolado: duas peças avulsas diferentes, cada uma mandada pra um
espaço distinto, cada uma caiu exatamente na origem do espaço escolhido
(não no outro). Quantidade maior que 1 num espaço específico empilha
todas dentro dele, lado a lado, até não caber mais - sem espaço nenhum
selecionado, continua caindo no melhor encaixe de sempre.

### Investigado e não é bug: lista "cabem aqui" já é o catálogo inteiro

Also reportado: a lista de peças que cabem num espaço deveria mostrar
"tudo que tem na base, independente de qual Kambam" - já é assim.
`banco.listar_pecas()` (usada tanto em `/candidatas` quanto na lista do
plano manual) não tem NENHUM filtro por cor/Kambam - a tabela `peca` do
cadastro nem guarda essa informação por peça, é só cod/descrição/medida.
Contado ao vivo no plano real do usuário: dos 132 itens cadastrados no
total, exatamente 9 cabem geometricamente (respeitando guilhotina e veio)
no espaço de 724×1306mm do print - o "9" já é a conta certa sobre o
catálogo inteiro, não um recorte por Kambam.

## 2026-09-17 (5) — Excluir plano (ícones), reabrir quantidades do plano manual, peça avulsa por dimensão

### Botão de excluir plano não funcionava (bug de aspas com `|tojson`)

O botão 🗑️ da tela "Planos gerados" não fazia nada: o `onclick` estava em
aspas DUPLAS e `{{ p.id|tojson }}` também gera aspas duplas (é JSON) - as
aspas coincidiam e cortavam o atributo HTML no meio, então o clique nunca
chegava a chamar `excluirPlano(...)`. Corrigido usando aspas simples no
atributo (mesmo padrão já usado em `cad_pecas.html`/`tabela.js` pra esse
exato problema). A rota `/planos/<id>/excluir` (`app.py`) e
`banco.excluir_plano` já estavam certas - o bug era só no HTML.

### Ícones das ações da lista de planos

Trocado o emoji (👁️/🗑️/📄) por SVG monocromático pequeno, na ordem Abrir →
Excluir → PDF, mesmo padrão visual da coluna de ações usada nos cadastros
de outros sistemas do usuário. O ícone de PDF ganhou uma folha com selo
vermelho "PDF" em vez de uma folha em branco genérica.

### Plano manual: reabrir a tela de quantidades pra editar e recalcular

Depois de calcular um plano manual, não existia jeito de voltar pra tela
"Quanto você precisa de cada peça?" (`/manual/<id>/quantidades`) - só dava
pra ver o resultado final ou começar um plano manual novo do zero.

`/resultado/<id>` (`resultado.html`) ganhou o botão "← Editar quantidades"
quando o plano é manual (`{% if manual %}` - o campo já vinha no `resultado`
salvo, só faltava usar na tela). A rota `manual_quantidades` já era
reentrante (recalcula a lista de peças a partir do padrão atual a cada
GET) - só faltava também pré-preencher os campos com a quantidade pedida
da última vez (`padrao['pecas'][].lotes['Plano manual']`), pra "editar"
significar ver o número anterior e ajustar, não digitar tudo de novo.

### Peça avulsa por dimensão (sem código no cadastro)

Pedido novo: incluir uma peça que não existe no cadastro, direto pela
dimensão (comprimento × largura em mm) em vez do código.

**Onde**: `editar.html` (editor de padrão do plano automático) e
`manual.html` (plano manual) - os dois ganharam um campo "Peça avulsa por
dimensão" (comprimento, largura, quantidade, botão Incluir), ao lado da
busca por código.

**Backend, uma ação nova em `aplicar_edicao`
(`app.py`)**: `incluir_avulsa` - mesmo raciocínio de `incluir_melhor`
(quantidade N, cada uma no melhor espaço livre do padrão, sem escolher
onde), só que sem consultar `banco.listar_pecas` nenhuma. O "código" que
aparece no relatório é a própria dimensão (`"400x390"`), pra duas peças
avulsas da mesma medida agruparem como o mesmo tipo, igual peça de
catálogo agrupa pelo código dela. Sem `aparente` cadastrado pra consultar,
a peça avulsa entra travada na orientação (não gira) sempre que a chapa
tem veio - mesmo lado conservador que `banco.pode_girar` já usa quando
falta cadastro. Pra girar mesmo assim, é só usar o botão de girar (⟳) na
peça já colocada, que pergunta antes de furar a regra, igual peça de
catálogo.

Testado isolado (sem depender do servidor rodando): 5 peças avulsas de
400×390mm incluídas automaticamente em espaços livres distintos, sem
nenhuma saindo rotacionada, com o código gerado corretamente a partir da
dimensão. Parsing aceita tanto `400.5` quanto `400,5` (vírgula decimal
pt-BR) no comprimento/largura.

## 2026-09-17 (4) — Editor de padrão comum ganha arrastar/girar do plano manual + rótulo de peça fina

### O pedido: mesma interação do plano manual dentro do editor de padrão gerado pelo otimizador

O editor de um padrão já gerado (`/resultado/<id>/padrao/<gi>/<pi>`, tela
"Editar" de cada padrão do plano) só permitia clicar num espaço vazio pra
adicionar peça e clicar numa peça pra tirar - sem arrastar pra reposicionar
nem girar 90° uma peça já colocada, ao contrário do plano manual
(`/manual/<id>`), que já tinha as duas coisas.

**Descoberta ao investigar**: o back-end (`aplicar_edicao` em `app.py`) já
suportava as ações `mover` e `girar` por completo, de forma genérica -
comentário no próprio código já dizia que o plano manual reusa essa MESMA
rota via fetch (`formato=json`). Ou seja, o editor de padrão comum e o
plano manual sempre desenharam do MESMO dado (`/dados`) e sempre gravaram
pela MESMA rota (`/aplicar`); só a tela (`editar.html`) nunca ganhou os
gestos de arrastar/girar que `manual.html` já tinha. Não foi preciso mudar
nenhuma linha de Python.

**A correção, só em `templates/editar.html`**:
- Portados de `manual.html`: `mmDoEvento`, `clampPos`, `sobrepoeCliente`,
  `iniciarArrasteInterno`/`moverArrasteInterno`/`soltarArrasteInterno`
  (arrastar peça já colocada) e `girar` (com a mesma pergunta de
  confirmação quando a peça é aparente numa chapa com veio - bloqueio vem
  do servidor, `veio_bloqueou=1`, a tela só pergunta "girar mesmo assim?").
- Toda ação (adicionar, incluir, remover, mover, girar) passou a usar o
  mesmo `enviar()` via `fetch` que o plano manual já usava, no lugar do
  formulário oculto que recarregava a página inteira a cada clique -
  padronizado pra não ter dois jeitos diferentes de falar com a mesma rota
  na mesma tela.
- Cada peça ganhou os botões ⟳ (girar) e × (remover) desenhados sobre ela,
  e o próprio corpo da peça responde a arraste - igual ao plano manual.

Testado direto no navegador contra o servidor real: arrastar uma peça já
colocada persiste no servidor (a posição volta certa depois de recarregar
os dados) e girar uma peça aparente numa chapa com veio trava com a mesma
mensagem e a mesma pergunta de confirmação do plano manual.

### Peça fina sem nenhum rótulo no editor (mesmo sendo mostrado no relatório)

A peça só ganhava o código escrito em cima dela quando `w>150 e h>90` -
critério que faltava pra peça fina como um rodapé de 70mm de altura (ex.:
8860, 1375×70), que ficava sem NENHUM texto no editor, mesmo aparecendo
com o código escrito no relatório/PDF (`visualize.py`, que tem um segundo
critério mais permissivo, `w>70 e h>34`, só pro código sem a dimensão).

**A correção**: `editar.html` e `manual.html` ganharam o mesmo segundo
critério do relatório - peça abaixo do primeiro limiar mas acima do
segundo mostra o código, só que menor. Confirmado no navegador: a peça
8860 (1375×70) agora aparece com o código "8860" desenhado dentro dela.

### O problema: peça de código diferente encaixada NO MEIO de duas faixas do mesmo código

Mesmo depois da cadência dentro da faixa (entrada anterior), o mesmo código
ainda podia sair em duas faixas/colunas SEPARADAS por uma faixa de peça
diferente no meio - ex.: padrão com 8 unidades de 10324 (900x450), onde só
4 cabem "de pé" numa faixa por vez: saía faixa 1 = 10324 (4un), faixa 2 =
10312 (peça diferente), faixa 3 = 10324 de novo (as 4 restantes). Do ponto
de vista de quem corta, é o mesmo problema de peça grande/pequena
intercalada, só que entre faixas inteiras em vez de dentro de uma.

**Causa raiz**: a busca gulosa de `_pack_shelves` decide a MELHOR faixa a
cada passo do laço externo (`while sheet_h - y > 1`) olhando só o que sobrou
de demanda NAQUELE momento, sem nenhuma memória de qual faixa vinha antes.
Quando a demanda de um código não cabe inteira numa faixa (limite de altura
da chapa), o restante fica pra depois - e nesse "depois" outra peça pode
pontuar melhor por uma faixa, empurrando a segunda parte do mesmo código pra
uma posição não-adjacente à primeira.

**A correção**: `_pack_shelves` agora separa a GERAÇÃO das faixas (o laço
guloso, que continua decidindo quais peças e quantas por faixa exatamente
como antes) do POSICIONAMENTO final em Y - depois de geradas todas as
faixas do padrão, elas são reordenadas por largura decrescente (mesmo
critério já usado dentro de uma faixa) antes de calcular o Y de cada uma.
A ordem de sort é estável, então duas faixas do mesmo código (mesma
largura dominante) ficam automaticamente adjacentes, preservando a ordem
em que a busca gulosa as encontrou entre códigos diferentes. Não muda
quantidade, aproveitamento nem viabilidade - a altura total ocupada pela
soma das faixas é a mesma em qualquer ordem.

Como `_pack_columns` chama `_pack_shelves` internamente com os eixos
trocados, a correção vale igual pra padrões em coluna (é exatamente o caso
do 10324/10312 acima, que saía em colunas, não faixas).

Testado isolado com a mesma composição de peças do padrão reportado (8x
10324 + 4x 10312 + 1x 10998 + 6x 10302): as duas colunas de 10324 (4+4)
saem adjacentes, 10312+10998 depois, 10302 por último - nenhum código
picotado. Regressões de veio travado e cadência dentro da faixa continuam
passando.

Mesmo defeito, mesma correção, aplicado também no modelo exato
(`_refine_last_sheet_cpsat`): o solver do CP-SAT é livre pra atribuir
qualquer peça a qualquer índice de faixa, então a demanda de um código que
não cabe numa faixa só podia sair espalhada em índices não-adjacentes do
mesmo jeito. A função agora só monta a lista de faixas com os índices
naturais do solver, sem posicionar Y nenhum, reordena por largura
decrescente da peça dominante (sort estável, mesmo critério de
`_pack_shelves`) e só então define o Y final - inclusive já reaproveitando
a cadência largura-decrescente dentro de cada faixa.

## 2026-09-17 (2) — Trava a orientação da peça em todo o plano + cadência de corte por faixa

### O problema: mesma peça, posições diferentes entre chapas do mesmo plano (de novo)

Depois da correção do `_pack_columns` (entrada anterior), a mesma peça ainda
aparecia em orientações diferentes entre padrões do mesmo plano - ex.: 11883
(1833x815, PODE girar, cor sem veio) saía "de pé" em 3 chapas e "de lado" só
numa 4ª chapa isolada, porque naquela sobra específica a versão girada
cobria alguns milímetros extras. O ganho de aproveitamento não compensa: o
operador não tem como saber, olhando a pilha de chapas, qual delas foi
cortada de qual jeito.

**Causa raiz**: `pode_girar=True` (peça sem veio) deixa o empacotador
escolher a orientação caso a caso, padrão a padrão, sem nenhuma memória do
que já foi decidido nos padrões anteriores do MESMO plano. Cada padrão é
gerado de forma isolada (heurística de semente ou CP-SAT do pricing do
Column Generation) - nenhum dos dois carrega estado entre padrões.

**A correção**:
- `column_generation.optimize_group_cg` agora trava toda peça do grupo em
  `pode_girar=False` (mantendo a orientação do cadastro) antes de gerar
  qualquer padrão - heurística e CP-SAT exato passam a só ver UMA
  orientação possível pra cada peça, do primeiro ao último padrão do plano.
  Isso vale só pra dentro de um plano/grupo (cor+espessura); não afeta o
  cadastro nem a edição manual, onde girar continua sendo escolha de quem
  está montando o padrão.
- `pipeline.aplicar_aprendizado` (sugestão automática que preenche sobra com
  peça repetida à mão) tinha o mesmo problema por um caminho totalmente
  separado: decidia a orientação sobra por sobra, sem lembrar o que já
  tinha decidido pra aquele código nos padrões anteriores. Agora usa
  `forcar_giro` (parâmetro que `edicao.encaixar` já tinha, criado pro plano
  manual) pra travar na primeira orientação que coube e exigir a mesma nas
  sobras seguintes.

Verificado rodando o pipeline completo (`pipeline.rodar`) com o PDF real do
usuário: 0 códigos com mais de uma orientação em todo o plano, contra 1
código inconsistente (10998, via aprendizado) antes da segunda parte da
correção.

### Cadência de corte dentro da faixa: peça grande antes da pequena

`_pack_shelves` decidia QUAIS peças entram numa faixa e QUANTAS de cada,
mas a ORDEM esquerda-pra-direita saía na ordem em que cada tipo "ganhou" a
comparação de score - podendo intercalar peça pequena com peça grande sem
nenhum motivo prático (ex.: 562mm logo depois de 1730mm na mesma faixa),
o que é inviável de acompanhar na serra.

**A correção**: depois de decidida a faixa, os blocos de peça são
reordenados por largura decrescente antes de posicionar o x de cada um -
não muda quantidade nem área usada (só a ordem), e passa a cortar sempre da
peça mais larga pra mais estreita, na mesma sequência em todo padrão que
usa essa combinação.

### Modelo exato (CP-SAT) ganha a mesma alternativa de coluna da heurística

O usuário reportou ainda um padrão específico (peças 8196+10664 pareadas
faixa a faixa) onde a diferença de altura entre as duas (280 vs 198) deixa
uma faixa de sobra de ~85mm repetida em CADA linha, em vez de concentrada
uma única vez - o que aconteceria se as cópias de cada código fossem
empilhadas juntas (modo coluna) em vez de pareadas faixa a faixa.

**Causa raiz**: a semente heurística já testa faixa, coluna e split-2-
colunas e fica com a de maior área (`_generate_pattern`, `exact=False`) -
mas o pricing exato do Column Generation (`_refine_last_sheet_cpsat`, CP-
SAT) só sabia montar em faixas. Quando um padrão de poucas chapas vinha
desse caminho, não existia alternativa de coluna pra comparar.

**A correção** (usuário optou por aceitar o custo de desempenho):
`_refine_last_sheet_cpsat_colunas`, nova função em `optimizer.py` - mesma
ideia de `_pack_columns`, mas resolvendo o CP-SAT com os eixos trocados e
transpondo o resultado de volta (incluindo a mesma pré-troca de w/h nas
peças, já que toda peça chega travada em `pode_girar=False` depois da
correção anterior). `_generate_pattern` (`column_generation.py`), no
caminho exato, agora resolve as DUAS variantes (faixa e coluna) e fica com
a que pontuar melhor no mesmo critério que o modelo já otimiza - valor dual
no pricing, área fora dele. Custa um CP-SAT inteiro a mais por chamada do
pricing exato; planos grandes podem demorar mais para calcular.

Testado isoladamente com o mix real de peças (5x 8196 + 5x 10664 + 1x 8213
+ 1x 9485): o modelo exato em faixa, sozinho, já resolve melhor que a
heurística gulosa testada antes (encaixa as 12 peças em vez de 10, por
buscar exaustivamente em vez de greedy) - a variante em coluna entra como
rede de segurança pros casos em que a faixa não é a melhor opção, sem
piorar nenhum caso onde ela já era.

## 2026-09-17 — Corrige peça com veio travado saindo rotacionada em `_pack_columns`

### O problema: mesma peça, duas orientações diferentes dentro do mesmo plano

Peça de material com veio (`pode_girar=False`) aparecia deitada num padrão
do plano e em pé noutro, dentro do MESMO plano de corte — ex.: a 8197
(FRENTE GAVETA N°16, 562x177) saía como `177x562` no Padrão 1 e `562x177`
no Padrão 3. Corte executado assim atravessa o veio e o móvel vira refugo.

**Causa raiz**: `_pack_columns` (empacotamento em colunas) é implementado
chamando `_pack_shelves` com os eixos da chapa trocados e transpondo o
resultado de volta ("mesma lógica, só deitada" — ver entrada de
2026-09-05/06). Essa troca de eixos é transparente pra peça que PODE girar
(as duas orientações continuam válidas), mas pra peça com veio travado
`orientacoes()` só oferece a orientação "sem rotação" — e essa única
orientação, depois de transposta de volta pro eixo físico da chapa, saía
com o comprimento (`w`, que por definição tem que ficar sempre no sentido
do veio) no eixo ERRADO. Nenhuma flag `rotated` acusava isso porque a
rotação acontecia na troca de eixo, não na escolha de orientação da peça.

**A correção, em `_pack_columns` (`optimizer.py`)**: peça com veio travado
entra na chamada interna de `_pack_shelves` já com `w`/`h` pré-trocados,
pra que a orientação "sem rotação" do empacotador de faixas volte, depois
da transposição, com o comprimento no eixo físico certo. Peça que pode
girar não precisa disso e entra como está. A flag `rotated` do item final
também passou a ser calculada comparando contra o cadastro original da
peça (`w` final ≠ `w` do cadastro ⇒ rotated), em vez de inverter uma flag
interna que perde sentido depois da troca de eixos — essa é a definição
correta independente de qual caminho de empacotamento gerou o item.

Coberto com teste isolado rodando `_pack_columns` sozinho contra uma peça
de veio travado (0 violações após a correção, 100% de violação antes) e
com o pipeline completo de `column_generation.optimize_group_cg` usando as
três peças do exemplo (8195/8196/8197) — nenhum padrão gerado saiu com
peça de veio em orientação diferente do cadastro.

## 2026-09-05 / 2026-09-06 — Motor de corte em faixas/colunas + CRUD dos cadastros

### O problema: padrão de corte impossível de executar na prática

O Column Generation gerava padrões que passavam na validação de guilhotina
(`edicao.estagios() <= estagios configurado`), mas eram inexecutáveis na
seccionadora: peça pequena encravada ao lado de peça grande, cada uma
exigindo um corte em posição própria, sem nenhuma faixa ou coluna
consistente ao longo da chapa. O operador (Adilson) não conseguia seguir
esse desenho na máquina.

**Causa raiz**: a semente inicial do Column Generation vinha de um
empacotamento guilhotinado recursivo genérico (`_pack_rect`, o antigo
`optimizer.py`), que a cada passo escolhia o melhor "bloco" de peças pro
retângulo atual e recortava o resto em duas tiras (direita e embaixo),
recursivamente. Isso produz árvores de corte tecnicamente válidas, mas sem
nenhuma garantia de que a mesma posição de corte se repita ao longo da
chapa — cada sub-região podia ter uma largura de coluna diferente.

**A correção, em 3 peças novas em `optimizer.py`**:

- `_pack_shelves` — empacotamento guloso em **faixas horizontais**: a
  chapa vira uma pilha de tiras, cada uma cheia de peças lado a lado.
  Sempre 2 estágios (+ aparo se sobrar altura). Substituiu `_pack_rect` na
  semente do CG.
- `_pack_columns` — o espelho de `_pack_shelves`, em **colunas verticais**
  em vez de faixas. Implementado transpondo os eixos e chamando
  `_pack_shelves` "deitado". Vale quando as peças compartilham LARGURA em
  vez de altura.
- `_pack_split_2_colunas` — tenta um corte vertical único dividindo a
  chapa em 2 colunas lado a lado, cada uma empacotada em faixas de forma
  **independente**. É a diferença entre 88% de aproveitamento (peça de
  350mm de altura forçada a dividir faixa com peça de 404mm, perdendo os
  54mm de diferença) e 96% (cada coluna com sua própria sequência de
  alturas, sem misturar com a vizinha).

`column_generation.py` agora testa as 3 e fica com a de maior área
coberta, pra cada padrão gerado na fase de semeadura do CG.

**Dois bugs achados no caminho, ambos em `_pack_shelves`:**

1. *Comparação por área crua, não por densidade.* Com `estagios >= 3`
   (aparo permitido), uma peça de 580mm de altura também é elegível como
   candidata de uma faixa "experimental" de 1035mm (porque 580 ≤ 1035).
   Se só ela coubesse mesmo assim, a área coberta empatava com a da faixa
   de 580mm de verdade — e como as alturas eram testadas da maior pra
   menor, a comparação por área pura ficava com a primeira (a errada),
   desperdiçando os 455mm de diferença por faixa. Corrigido comparando por
   **área ÷ altura da faixa** (densidade), não área crua.
2. *Custo computacional.* Testar TODA altura/largura possível como
   candidata, em um grupo com 100+ tipos de peça, fazia UM padrão levar
   ~1s pra gerar — e a semeadura gera centenas. Um lote de 3 grupos foi de
   163s (versão só-faixas, sem a correção de densidade) pra **1826s (30
   min)** depois de acrescentar colunas e split. Corrigido limitando cada
   busca às ~15 alturas / 6 larguras mais promissoras (por
   `qty_total × área`, o mesmo critério que decide no fim) em vez de
   testar todas. Resultado: 359s pro mesmo lote, com a MESMA qualidade.

**Resultado medido**, mesmo lote de 3 Kambans, antes/depois:

| | Antes (bug) | Só faixas (sem correção de densidade) | Faixas + Colunas + Split (final) |
|---|---:|---:|---:|
| Total de chapas | 3.034 | 3.706 (+22%) | **2.674 (−11,9%)** |
| CINAMO FF 2 · 15mm | 1.867 chapas · 89,8% | 2.494 chapas · 70,5% | **1.746 chapas · 95,0%** |
| Tempo (3 grupos) | ~3 min | ~3 min | ~6 min |

Menos chapas que a versão com bug, sem nenhum padrão emaranhado, ao custo
de dobrar o tempo de cálculo (testar 3 formas de encaixe em vez de 1).

### Cadastros ganharam editar e excluir

Nenhuma das 4 telas de Cadastros (Móveis, Peças, Cores, Acabamentos) tinha
como excluir um registro — só dava pra mexer direto no banco. Adicionado:

- **Excluir**, nas 4 telas, com aviso de cascata ANTES de apagar (rotas
  `/cadastro/impacto` e `/cadastro/excluir` em `app.py`, funções
  `excluir_peca/cor/modelo/acabamento` em `banco.py`). O aviso mostra
  quantos vínculos somem junto (ex: "esta peça está vinculada a 2
  móveis") antes de confirmar — decidido depois de ficar claro que um
  usuário sem esse aviso não tem como saber o efeito colateral de excluir.
  Excluir uma peça remove o vínculo com móveis, não o móvel; excluir um
  móvel remove a lista técnica, não as peças.
- **Editar**, em Peças (descrição, comprimento, largura) e Móveis
  (descrição). Cores e Acabamentos não ganharam edição de nome — o nome é
  a chave usada pra casar com o próximo Kamban importado, e deixar
  renomear quebraria esse casamento silenciosamente. A edição que já
  existia neles (toggle "tem desenho de madeira" / cores vinculadas)
  continua sendo o único jeito de editar esses dois.
- Modal genérico reutilizável em `templates/base.html`
  (`abrirModal`/`fecharModal`/`erroModal`) + funções em
  `static/js/tabela.js` (`excluirRegistro`, `editarPeca`, `editarModelo`).

`excluir_*` em `banco.py` agora devolve `True`/`False` conforme o `DELETE`
realmente apagou alguma linha (via `cursor.rowcount`) — sem isso, excluir
um id que já não existe mais "funciona" silenciosamente sem apagar nada, e
quem chamou não tem como distinguir isso de um sucesso de verdade.

### Bugs menores corrigidos na mesma leva

- **Tela de Cadastros ficava com cache do navegador.** Excluir um
  registro funcionava (o banco realmente mudava), mas trocar de aba ou
  usar o botão Voltar mostrava a lista de antes da exclusão — faltava
  `Cache-Control: no-store` na rota `/cadastros/<aba>` (já existia em
  `/resultado/<id>` desde a sessão anterior, mas não tinha sido replicado
  aqui). Corrigido.
- **`.gitignore` desatualizado**: ignorava `static/resultados/*`, um
  caminho de uma versão anterior do projeto. O `resultados/` que o
  `app.py` realmente usa hoje (na raiz do projeto) não estava coberto, e
  um `git add -A` chegou a subir ~570 arquivos de teste (imagens
  geradas, banco de dados) antes de ser percebido. Adicionado
  `resultados/*` / `catalogo/*` / `.claude/` ao ignore.

## 2026-08-31 — Correções de edição de plano de corte

Commit [`77e6e8f`](../../commit/77e6e8f).

- **Editar um padrão (add/remover peça) não refletia na tela.** Quatro
  causas empilhadas, cada uma escondendo a próxima:
  1. `pad['pecas']` (a tabela de peças do padrão) era montada uma vez no
     cálculo original e nunca refeita depois de uma edição manual.
     Corrigido com `pad['pecas_exibicao']`, recalculada a partir da
     geometria atual — sem sobrescrever `pad['pecas']`, que
     `_demanda_do_grupo` usa como fonte do pedido original do Kamban.
  2. A imagem do padrão é regravada com o MESMO nome de arquivo — o
     navegador servia a versão em cache. Corrigido com um contador
     `imagem_versao` usado como cache-busting (`?v=N`) na URL do PNG.
  3. `banco.atualizar_resultado()` era chamado ANTES de `_redesenhar()` —
     o `imagem_versao` novo nunca chegava a ser salvo. Ordem invertida.
  4. **Causa raiz**: `jobs.criar()` gerava seu próprio id (`uuid4`),
     diferente do `job_id` que `app.py` usa pra nomear a pasta de imagens
     do plano. Toda edição gravava a imagem numa pasta órfã que a tela
     nunca consultava. Corrigido: `jobs.criar()` aceita `job_id` explícito.
     Planos já salvos antes da correção foram reparados com um script que
     localiza e corrige o descompasso.
- **Rótulo "23 ciclos de 7 chapas empilhadas" parecia erro de conta**
  (23×7=161≠160). Não era: o último ciclo é parcial (22×7+6=160). O texto
  agora mostra a conta quebrada ("22 de 7 + 1 de 6").
- **Aviso "lote não fecha" sumia ao trocar de aba** — mesma causa do bug
  de cache dos Cadastros acima, só que na tela `/resultado/<id>`, corrigida
  primeiro ali.
