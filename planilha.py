# -*- coding: utf-8 -*-
"""
Leitura do "Itens com Especificações" exportado pelo Agrosys.

O arquivo tem extensão .xls mas NÃO é o formato binário do Excel: é
SpreadsheetML, o XML que o Office 2003 gera. Bibliotecas de xls/xlsx não
abrem. Como é XML puro, lemos direto e sem dependência nova.

Colunas úteis:
    Código  -> "8930 - LATERAL ESQUERDA N°01 ROUPEIRO ATLANTA"
    Comprim. / Altura / Largura  -> em mm

Atenção à ordem: no Agrosys, "Altura" é a ESPESSURA da chapa (15, 18, 25mm),
não a altura da peça. Confirmado pela coluna "Área M²", que bate com
Comprim. x Largura e ignora a Altura.
"""
import re
import xml.etree.ElementTree as ET

NS = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
COD_RE = re.compile(r'^(?P<cod>\d+)\s*-\s*(?P<desc>.+)$')

# limite de segurança: o catálogo real tem ~6 mil linhas
MAX_LINHAS = 200_000


def _numero(texto: str) -> float:
    try:
        return float((texto or '').replace(',', '.'))
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


def ler_itens(caminho: str) -> list[dict]:
    """
    Retorna [{cod, desc, comp_mm, larg_mm, esp_mm}] do catálogo.

    Só entra linha com código numérico e as três medidas preenchidas — o
    relatório traz cabeçalho, rodapé e linhas de grupo no meio, e nenhum
    deles é peça.
    """
    itens: list[dict] = []
    colunas: dict[str, int] | None = None
    lidas = 0

    for _, el in ET.iterparse(caminho, events=('end',)):
        if el.tag.split('}')[-1] != 'Row':
            continue
        cels = _celulas(el)
        el.clear()
        lidas += 1
        if lidas > MAX_LINHAS:
            break

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
