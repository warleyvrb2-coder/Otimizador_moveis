# -*- coding: utf-8 -*-
"""
Parser dos PDFs "Kambam" (relatório de corte do Agrosys).
Extrai a tabela "PEÇAS DA PRODUÇÃO": código, descrição, comprimento,
largura, espessura, cor e quantidade (Total) de cada peça.
"""
import re
import subprocess
import tempfile
import os

# Linha de peça, ex:
# "8900-TAMPO N°10 COMODA NEW    1481,00 451,00   15,00 CINAMO FF   550,00   :   :   00:00:00"
# Alguns relatórios têm só 1 espaço entre a descrição e os números, então
# não exigimos múltiplos espaços - só ancoramos nos 3 números decimais
# seguidos da cor e do número final antes de " : : hh:mm:ss".
PIECE_RE = re.compile(
    r'^(?P<cod>\d+)-(?P<desc>.+?)\s+'
    r'(?P<comp>[\d.]+,\d{2})\s+'
    r'(?P<larg>[\d.]+,\d{2})\s+'
    r'(?P<esp>[\d.]+,\d{2})\s+'
    r'(?P<cor>[A-ZÀ-Ú0-9 /°.\-]+?)\s+'
    r'(?P<qtd>[\d.]+,\d{2})\s+:\s+:\s+[\d:]+\s*$'
)


def _to_float(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.'))


def pdf_to_layout_text(pdf_path: str) -> str:
    """Usa pdftotext -layout para preservar as colunas do relatório."""
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
        txt_path = tmp.name
    try:
        subprocess.run(
            ['pdftotext', '-layout', '-enc', 'UTF-8', pdf_path, txt_path],
            check=True, capture_output=True,
        )
        with open(txt_path, encoding='utf-8') as f:
            return f.read()
    finally:
        os.unlink(txt_path)


def parse_kambam(pdf_path: str, source_name: str | None = None) -> list[dict]:
    """
    Retorna uma lista de peças:
    {cod, desc, comp_mm, larg_mm, esp_mm, cor, qtd, origem, lote}
    """
    text = pdf_to_layout_text(pdf_path)

    # Identifica o(s) lote(s) presentes no documento, para referência.
    lote_match = re.search(r'LOTE:\s*(\S+)', text)
    lote = lote_match.group(1) if lote_match else (source_name or 'desconhecido')

    pieces = []
    for line in text.splitlines():
        m = PIECE_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        pieces.append({
            'cod': d['cod'],
            'desc': d['desc'].strip(),
            'comp_mm': _to_float(d['comp']),
            'larg_mm': _to_float(d['larg']),
            'esp_mm': _to_float(d['esp']),
            'cor': d['cor'].strip(),
            'qtd': _to_float(d['qtd']),
            'origem': source_name or os.path.basename(pdf_path),
            'lote': lote,
        })
    return pieces


if __name__ == '__main__':
    import sys
    import json
    result = parse_kambam(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nTotal de linhas de peça: {len(result)}")
    print(f"Soma das quantidades: {sum(p['qtd'] for p in result):.2f}")
