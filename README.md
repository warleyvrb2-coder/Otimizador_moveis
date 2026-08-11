# Otimizador de Corte - Setor Moveleiro (MVP)

## O que faz
Recebe 1+ relatórios "Kambam" (PDF) do Agrosys, extrai as peças de cada
lote, agrupa por cor+espessura (só peças do mesmo grupo podem dividir
chapa), e calcula o layout de corte guilhotinado (compatível com
seccionadora, ex: Gibem) maximizando o aproveitamento de cada chapa
2750x1850mm.

Peças de Kambans diferentes mas do mesmo grupo (cor+espessura) entram no
MESMO pool de otimização — então a sobra de uma chapa pode ser
aproveitada por peça de outro lote.

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
| `CG_TIME_BUDGET_S` | segundos de otimização por grupo | 45 |
| `KERF_MM` | espessura do disco da seccionadora | 4.4 |
| `PILHA_MAX_MM` | altura máxima da pilha no corte múltiplo | 105 |
| `ESTAGIOS` | 2 = estrito, 3 = 2 estágios + aparo | 3 |
| `DATA_DIR` | onde gravar uploads e o cadastro (**aponte pro volume**) | pasta do projeto |
| `DB_PATH` | caminho do SQLite, se quiser fora do `DATA_DIR` | `DATA_DIR/cadastro.db` |

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
- `optimizer.py` — peças de base do corte guilhotinado: empacotamento
  recursivo (heurístico, usado como ponto de partida) e um solver
  CP-SAT de 1 chapa (modelo de faixas), reaproveitado tanto pra refino
  quanto como "pricing" exato do column generation.
- `column_generation.py` — **motor principal**, resolve o lote inteiro
  via Column Generation (o método clássico de cutting stock):
  1. Gera um conjunto inicial de padrões de corte com a heurística
     (pra já começar com uma solução viável).
  2. Resolve o LP relaxado (GLOP) — minimiza nº de chapas.
  3. Usa os preços-sombra (dual) do LP pra gerar, via **CP-SAT**, o
     próximo padrão que mais vale a pena adicionar (pricing exato).
  4. Repete até não achar mais padrão que melhore.
  5. Arredonda pra número inteiro de chapas por padrão (CP-SAT).

  No Kambam de teste: **673 chapas / 88,4%** (heurística sozinha) →
  **634 chapas / 93,8%** (column generation), a ~6,5% do mínimo
  teórico de área. Ajustável via `CG_TIME_BUDGET_S` no `app.py`
  (mais tempo = mais perto do ótimo; 45s é um bom equilíbrio pra
  demo, pode subir se rodar em background).
- `visualize.py` — desenha cada chapa como PNG (matplotlib).
- `app.py` — Flask: upload, orquestração, página de resultado.

## Ajustar

- Tamanho da chapa: `SHEET_W_MM` / `SHEET_H_MM` no topo do `app.py`
  (hoje 2750x1850mm, ~5,09 m²).
- Quantas chapas de amostra mostrar na tela: `MAX_IMAGENS` em `app.py`
  (relatórios grandes geram muitas chapas — hoje mostra 15 de amostra:
  as 5 primeiras, as 5 melhores e as 5 piores em aproveitamento).

## Próximos passos sugeridos
- Ler direto do banco do Agrosys em vez de PDF (fase 2, como combinado)
- Sequência de corte pro operador (ordem das faixas)
- Exportar layout em PDF/lista de corte pra imprimir na seccionadora
