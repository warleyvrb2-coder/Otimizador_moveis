/* Filtro por coluna e ordenação para as tabelas de cadastro.
   Genérico: qualquer table.cg-data-table ganha o comportamento sem JS próprio. */
(function () {
  function texto(linha, col) {
    const c = linha.children[col];
    return (c ? c.innerText : '').trim().toLowerCase();
  }

  function filtrar(tabela) {
    const filtros = [...tabela.querySelectorAll('.cg-table-search')]
      .map(i => ({ col: +i.dataset.col, v: i.value.trim().toLowerCase() }))
      .filter(f => f.v);
    let visiveis = 0;
    tabela.tBodies[0].querySelectorAll('tr').forEach(tr => {
      if (tr.classList.contains('cg-vazio')) return;
      const ok = filtros.every(f => texto(tr, f.col).includes(f.v));
      tr.style.display = ok ? '' : 'none';
      if (ok) visiveis++;
    });
    const cont = document.querySelector('[data-contador="' + tabela.id + '"]');
    if (cont) cont.textContent = visiveis + (visiveis === 1 ? ' registro' : ' registros');
  }

  function ordenar(tabela, col, botao) {
    const corpo = tabela.tBodies[0];
    const linhas = [...corpo.querySelectorAll('tr')]
      .filter(t => !t.classList.contains('cg-vazio'));
    const desc = botao.classList.contains('asc');
    tabela.querySelectorAll('.cg-sort-btn').forEach(b => b.classList.remove('on', 'asc'));
    botao.classList.add('on');
    if (!desc) botao.classList.add('asc');
    linhas.sort((a, b) => {
      const x = texto(a, col), y = texto(b, col);
      const nx = parseFloat(x.replace(/[^\d.,-]/g, '').replace(',', '.'));
      const ny = parseFloat(y.replace(/[^\d.,-]/g, '').replace(',', '.'));
      const r = (!isNaN(nx) && !isNaN(ny) && x && y) ? nx - ny : x.localeCompare(y, 'pt-BR');
      return desc ? -r : r;
    });
    linhas.forEach(l => corpo.appendChild(l));
  }

  document.querySelectorAll('table.cg-data-table').forEach(tabela => {
    tabela.querySelectorAll('.cg-table-search').forEach(inp => {
      inp.addEventListener('input', () => filtrar(tabela));
      inp.addEventListener('click', e => e.stopPropagation());
    });
    tabela.querySelectorAll('.cg-sort-btn').forEach(b => {
      b.addEventListener('click', () => ordenar(tabela, +b.dataset.col, b));
    });
  });
})();
