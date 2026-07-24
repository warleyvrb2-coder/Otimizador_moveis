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
# também precisa do poppler-utils instalado no sistema (pdftotext):
#   Ubuntu/Debian: sudo apt install poppler-utils
#   Mac: brew install poppler

python3 app.py
```

Abra http://localhost:5000 no navegador, suba o(s) PDF(s) do Kambam e
clique em "Otimizar corte".

## Como funciona por dentro

- `parser.py` — extrai a tabela "PEÇAS DA PRODUÇÃO" do PDF via
  `pdftotext -layout` + regex (código, comprimento, largura, espessura,
  cor, quantidade).
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
