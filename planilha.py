# -*- coding: utf-8 -*-
"""
Leitura do "Itens com Especificações" exportado pelo Agrosys.

O Agrosys exporta esse relatório em dois formatos, dependendo da tela/versão
usada pelo cliente - e já vimos os dois na prática:

  - .xls "antigo": não é o binário do Excel, é SpreadsheetML (o XML que o
    Office 2003 gera). Lido por `_linhas_antigo`.
  - .xlsx de verdade (OOXML/zip com sheet1.xml + sharedStrings.xml). Lido
    por `_linhas_xlsx` na mão, sem o openpyxl: o arquivo real do Agrosys
    vem com um styles.xml que o openpyxl rejeita ("Colors must be aRGB hex
    values"), e como só precisamos dos valores das células, não das
    fórmulas de estilo, ler o XML direto evita essa dependência frágil.

`ler_itens` detecta o formato pela assinatura do arquivo (zip começa com
"PK") e não pela extensão - útil porque /cadastro/importar sempre salva o
upload com ".xml" no nome, não importa o que o usuário mandou.

Colunas úteis:
    Código  -> "8930 - LATERAL ESQUERDA N°01 ROUPEIRO ATLANTA"
    Comprim. / Altura / Largura  -> em mm

Atenção à ordem: no Agrosys, "Altura" é a ESPESSURA da chapa (15, 18, 25mm),
não a altura da peça. Confirmado pela coluna "Área M²", que bate com
Comprim. x Largura e ignora a Altura.
"""
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
NS_XLSX = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
COD_RE = re.compile(r'^(?P<cod>\d+)\s*-\s*(?P<desc>.+)$')

# limite de segurança: o catálogo real tem ~6 mil linhas
MAX_LINHAS = 200_000


def _numero(texto: str) -> float:
    """
    Converte texto numérico dos dois formatos: o .xls antigo guarda em
    pt-BR (vírgula decimal, às vezes com ponto de milhar: "1.760,000"); o
    .xlsx guarda o valor bruto da célula, sempre com ponto decimal e sem
    separador de milhar ("1760.000"). Decide pelo que encontra no texto em
    vez de assumir um formato só - os dois já apareceram na prática.
    """
    texto = (texto or '').strip()
    if not texto:
        return 0.0
    if ',' in texto and '.' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    elif ',' in texto:
        texto = texto.replace(',', '.')
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _celulas(row) -> list[str]:
    """
    Devolve as células da linha respeitando ss:Index.

    Importa porque o exportador omite células vazias e sinaliza o salto com
    ss:Index. Ignorar isso desalinha as colunas e faz a espessura virar
    largura - erro que só apareceria depois, no corte.
    """
    saida: list[str] = []
    for c in row:
        idx = c.get(f'{{{NS["ss"]}}}Index')
        if idx:
            alvo = int(idx) - 1
            while len(saida) < alvo:
                saida.append('')
        d = c.find('ss:Data', NS)
        saida.append((d.text or '').strip() if d is not None else '')
    return saida


def _linhas_antigo(caminho: str):
    """Gera as linhas (lista de células) do .xls antigo (SpreadsheetML)."""
    lidas = 0
    for _, el in ET.iterparse(caminho, events=('end',)):
        if el.tag.split('}')[-1] != 'Row':
            continue
        cels = _celulas(el)
        el.clear()
        lidas += 1
        if lidas > MAX_LINHAS:
            return
        yield cels


def _coluna_indice(ref: str) -> int:
    """'C7' -> 2 (índice de coluna 0-based, a letra antes do número da linha)."""
    letras = ''.join(c for c in ref if c.isalpha())
    idx = 0
    for c in letras:
        idx = idx * 26 + (ord(c.upper()) - ord('A') + 1)
    return idx - 1


def _shared_strings_xlsx(z: zipfile.ZipFile) -> list[str]:
    strings: list[str] = []
    try:
        dados = z.open('xl/sharedStrings.xml')
    except KeyError:
        return strings
    with dados:
        for _, el in ET.iterparse(dados, events=('end',)):
            if el.tag.split('}')[-1] != 'si':
                continue
            texto = ''.join(t.text or '' for t in el.iter(f"{{{NS_XLSX['m']}}}t"))
            strings.append(texto)
            el.clear()
    return strings


def _linhas_xlsx(caminho: str):
    """
    Gera as linhas (lista de células) do .xlsx verdadeiro (OOXML).

    Colunas vêm por referência ("A1", "Q7"...) e podem pular célula vazia
    sem avisar - igual ao ss:Index do formato antigo, só que aqui é a
    própria letra da coluna que marca a posição.
    """
    lidas = 0
    with zipfile.ZipFile(caminho) as z:
        strings = _shared_strings_xlsx(z)
        with z.open('xl/worksheets/sheet1.xml') as f:
            for _, row_el in ET.iterparse(f, events=('end',)):
                if row_el.tag.split('}')[-1] != 'row':
                    continue
                celulas: dict[int, str] = {}
                maxcol = -1
                for c in row_el.findall(f"{{{NS_XLSX['m']}}}c"):
                    idx = _coluna_indice(c.get('r', ''))
                    v = c.find(f"{{{NS_XLSX['m']}}}v")
                    texto = v.text if v is not None else ''
                    if c.get('t') == 's':
                        try:
                            texto = strings[int(texto)]
                        except (ValueError, IndexError):
                            texto = ''
                    celulas[idx] = texto or ''
                    maxcol = max(maxcol, idx)
                row_el.clear()
                lidas += 1
                if lidas > MAX_LINHAS:
                    return
                yield [celulas.get(i, '') for i in range(maxcol + 1)]


def ler_itens(caminho: str) -> list[dict]:
    """
    Retorna [{cod, desc, comp_mm, larg_mm, esp_mm}] do catálogo.

    Só entra linha com código numérico e as três medidas preenchidas — o
    relatório traz cabeçalho, rodapé e linhas de grupo no meio, e nenhum
    deles é peça.
    """
    with open(caminho, 'rb') as f:
        assinatura = f.read(2)
    linhas = _linhas_xlsx(caminho) if assinatura == b'PK' else _linhas_antigo(caminho)

    itens: list[dict] = []
    colunas: dict[str, int] | None = None

    for cels in linhas:
        if colunas is None:
            if cels and cels[0].strip() == 'Código':
                achatado = [c.replace('\n', ' ').strip() for c in cels]
                colunas = {}
                for i, nome in enumerate(achatado):
                    if nome.startswith('Comprim'):
                        colunas['comp'] = i
                    elif nome.startswith('Altura'):
                        colunas['esp'] = i
                    elif nome.startswith('Largura'):
                        colunas['larg'] = i
            continue

        if not cels or not cels[0] or not cels[0][0].isdigit():
            continue
        m = COD_RE.match(cels[0])
        if not m:
            continue

        def pega(chave, padrao):
            i = colunas.get(chave, padrao)
            return _numero(cels[i]) if i < len(cels) else 0.0

        comp, esp, larg = pega('comp', 4), pega('esp', 5), pega('larg', 6)
        if comp <= 0 or larg <= 0:
            continue
        itens.append({
            'cod': m.group('cod'),
            'desc': m.group('desc').strip(),
            'comp_mm': comp,
            'larg_mm': larg,
            'esp_mm': esp,
        })
    return itens


if __name__ == '__main__':
    import sys
    itens = ler_itens(sys.argv[1])
    print(f'{len(itens)} itens')
    for i in itens[:8]:
        print(f"  {i['cod']:8s} {i['comp_mm']:8.0f} x {i['larg_mm']:7.0f} x {i['esp_mm']:5.0f}  {i['desc'][:44]}")
