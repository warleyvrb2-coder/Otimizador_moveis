# Changelog

Histórico do que foi ajustado no projeto, sessão por sessão. Cada entrada
explica o problema real por trás da mudança — não só o "o quê", mas o
"porquê" — porque isso é o que evita reabrir o mesmo bug dois meses depois.

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
