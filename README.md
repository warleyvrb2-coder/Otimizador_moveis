# Otimizador de Corte - Setor Moveleiro

Sistema em produção pra Benetil Móveis. Veja o [`CHANGELOG.md`](CHANGELOG.md)
pro histórico de correções — este README descreve o estado atual.

## O que faz
Recebe 1+ relatórios "Kambam" (PDF) do Agrosys, extrai as peças de cada
lote, agrupa por cor+espessura (só peças do mesmo grupo podem dividir
chapa), e calcula o layout de corte guilhotinado (compatível com
seccionadora, ex: Gibem) maximizando o aproveitamento de cada chapa —
tamanho, espessura da serra e limite de empilhamento configurados por
**máquina** (tela Configurações → Máquinas, cada uma com seu próprio plano).

Peças de Kambans diferentes mas do mesmo grupo (cor+espessura) entram no
MESMO pool de otimização — então a sobra de uma chapa pode ser
aproveitada por peça de outro lote.

Além do cálculo, o sistema cobre o fluxo inteiro até a máquina: edição
manual de um padrão específico (com validação de guilhotina em tempo
real), aprovação do plano pelo PCP, aprendizado do que o operador
acrescenta à mão, PDF pronto pra imprimir, e cadastro de peças/cores/
móveis/acabamentos com conferência manual.

## Como rodar

```bash
pip install -r requirements.txt
python app.py
```

Não depende de nada fora do `requirements.txt` — a leitura do PDF é feita
pelo pdfplumber, em Python puro.

## Publicar (Railway)

O repositório já traz `Procfile`, `railway.json`, `.python-version` e as
versões travadas no `requirements.txt`. No Railway basta apontar pro repo.

Variáveis de ambiente:

| variável | para quê | padrão |
|---|---|---|
| `APP_SENHA` | tranca a URL (Basic Auth). **Sem ela o app fica aberto** | vazio |
| `APP_USUARIO` | usuário do Basic Auth | `benetil` |
| `SECRET_KEY` | chave de sessão do Flask | aleatória a cada boot |
| `DATA_DIR` | onde gravar uploads, cadastro e desenhos (**aponte pro volume**) | pasta do projeto |
| `DB_PATH` | caminho do SQLite, se quiser fora do `DATA_DIR` | `DATA_DIR/cadastro.db` |
| `FORCAR_SENHA` | trata como "em nuvem" mesmo rodando fora do Railway (testar o bloqueio de senha localmente) | vazio |
| `FLASK_DEBUG` | liga o reloader/debugger do Flask - **nunca em produção** | vazio |

Chapa, serra, altura de pilha, estágios e tempo de cálculo por grupo **não
são mais variável de ambiente** — viraram parâmetro de cada máquina
cadastrada (tela Configurações → Máquinas, tabela `maquina` em
`banco.py`). Servem só de valor inicial pra primeira máquina, criada
sozinha na estreia: `CHAPA_LARG`, `CHAPA_ALT`, `KERF`, `PILHA_MAX`,
`ESTAGIOS`, `TEMPO_GRUPO`.

Dois pontos que NÃO são detalhe:

- **Um worker só.** O `Procfile` fixa `--workers 1` porque o estado dos
  cálculos em andamento vive na memória do processo (`jobs.py`). Com dois
  workers, o navegador pergunta o progresso pra um processo que não tem o
  job e recebe 404.
- **Disco efêmero.** Sem volume, os PNGs gerados E o cadastro somem no próximo
  deploy. Anexe um volume no Railway e aponte `DATA_DIR` pra ele antes de
  alguém conferir as peças uma a uma — o app avisa na tela quando detecta essa
  situação, mas é melhor resolver antes.

## Cadastro (banco.py)

Duas perguntas independentes decidem se uma peça pode ser girada na chapa:

1. **A cor tem desenho direcional?** (amadeirada x lisa)
2. **A peça fica aparente no móvel montado?**

Girar só é proibido quando as duas respostas são "sim". Prateleira em chapa
amadeirada gira (ninguém vê); porta em chapa branca lisa gira (não há desenho
pra sair torto). Modelar separado é o que permite recuperar material sem
arriscar peça aparente — no `CAST FF 2 L` a diferença medida entre girar e não
girar foi de **62 chapas num lote só**.

O cadastro se preenche sozinho: todo Kamban processado traz as peças e cores
novas com um palpite pelo nome (`VISTA`, `PORTA`, `LATERAL`, `TAMPO` → aparente;
`PRATELEIRA`, `DIVISÃO`, `SUPORTE` → escondida). Descrição composta cai no lado
conservador. Peça já conferida na tela **nunca** é sobrescrita por importação.

Sem cadastro, tudo é tratado como amadeirado e aparente — que é o pior
aproveitamento e o menor risco.

As 4 telas de cadastro (Móveis, Peças, Cores, Acabamentos) têm **editar**
e **excluir** por linha. Excluir sempre avisa antes o que vai junto (ex:
"esta peça está vinculada a 2 móveis, o vínculo será removido") — a
exclusão nunca é silenciosa. Editar é limitado de propósito: dá pra
corrigir descrição/medida de uma peça ou a descrição de um móvel, mas
**não** dá pra renomear o nome de uma cor ou de um acabamento nem o
código de uma peça/móvel - esses são a chave usada pra casar com o
próximo Kamban importado, e deixar renomear quebraria esse casamento
silenciosamente na importação seguinte.

Abra http://localhost:5000 no navegador, suba o(s) PDF(s) do Kambam e
clique em "Otimizar corte".

## Como funciona por dentro

- `parser.py` — extrai a tabela "PEÇAS DA PRODUÇÃO" do PDF pelas
  COORDENADAS das palavras (pdfplumber): agrupa por posição vertical pra
  formar as linhas e usa a posição horizontal pra saber a que coluna cada
  palavra pertence.

  Isso não é preciosismo. O relatório quebra células no meio da linha: a
  cor `CINAMO FF 2` sai como "CINAMO FF" numa linha e "2" na linha de
  baixo, e a descrição da peça continua abaixo também. Lendo o texto
  linearizado (`pdftotext` + regex, como era antes), esse "2" vira um
  número solto sem dono — e a linha inteira da peça deixa de casar com o
  regex quando a quebra acontece no lugar errado. **A versão anterior
  perdia 37% das peças em silêncio e truncava toda cor com sufixo**,
  fazendo `CINAMO FF 1` e `CINAMO FF 2` — materiais diferentes — caírem
  no mesmo pool de corte.

  Conferência: a soma de `comp × larg × esp × qtd` bate com o campo
  `Volume Cúbico` que o próprio relatório imprime no rodapé, nos 6 PDFs
  de teste, com folga de 0,05% (o arredondamento do PDF).
- `optimizer.py` — peças de base do corte guilhotinado, todas produzindo
  **sempre faixa ou coluna** (nunca uma árvore de corte emaranhada — ver
  [`CHANGELOG.md`](CHANGELOG.md) pro bug que isso resolveu):
  - `_pack_shelves` — empacotamento guloso em faixas horizontais.
  - `_pack_columns` — o espelho de `_pack_shelves`, em colunas verticais
    (vale quando as peças compartilham largura, não altura).
  - `_pack_split_2_colunas` — corte vertical único dividindo a chapa em 2
    colunas, cada uma em faixas de forma independente (o que evita
    misturar peça de altura muito diferente numa faixa só e desperdiçar
    a diferença).
  - `_refine_last_sheet_cpsat` — solver CP-SAT de 1 chapa (modelo de
    faixas), usado tanto pro refino exato quanto como "pricing" do
    column generation.
- `column_generation.py` — **motor principal**, resolve o lote inteiro
  via Column Generation (o método clássico de cutting stock):
  1. Gera um conjunto inicial de padrões testando faixas, colunas e a
     divisão em 2 colunas pra cada um, ficando com o de maior
     aproveitamento (pra já começar com uma solução viável e prática).
  2. Resolve o LP relaxado (GLOP) — minimiza nº de chapas.
  3. Usa os preços-sombra (dual) do LP pra gerar, via **CP-SAT**, o
     próximo padrão que mais vale a pena adicionar (pricing exato).
  4. Repete até não achar mais padrão que melhore.
  5. Arredonda pra número inteiro de chapas por padrão (CP-SAT).

  Tempo de cálculo por grupo configurável por máquina (padrão 45s - mais
  tempo chega mais perto do ótimo, com retorno decrescente).
- `edicao.py` — edição manual de um padrão: onde uma peça cabe numa
  sobra, se o layout resultante ainda é guilhotinável, sequência de
  cortes pro operador digitar na máquina.
- `visualize.py` / `relatorio.py` — desenha cada chapa (matplotlib) e
  monta o PDF do plano, uma página por padrão.
- `app.py` — Flask: upload, orquestração, cadastros, edição, aprovação.

## Ajustar

- Chapa, serra, empilhamento, estágios e tempo de cálculo: tela
  Configurações → Máquinas (por máquina, não mais constante no código).
- Quantas alturas/larguras candidatas o empacotador guloso tenta por
  faixa/coluna antes de decidir: os `[:15]` / `[:6]` em `_pack_shelves` e
  `_pack_split_2_colunas` (`optimizer.py`) — baixar rende cálculo mais
  rápido e leve piora de aproveitamento; subir é o oposto.

## Próximos passos sugeridos
- Ler direto do banco do Agrosys em vez de PDF (fase 2, como combinado)
- Recursão além de 2 colunas em `_pack_split_2_colunas` (hoje só tenta 1
  corte vertical; um lote com 3+ grupos de largura bem diferentes entre
  si ainda pode deixar aproveitamento na mesa)
