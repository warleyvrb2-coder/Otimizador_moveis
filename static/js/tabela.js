/* Filtro por coluna e ordenação para as tabelas de cadastro.
   Genérico: qualquer table.cg-data-table ganha o comportamento sem JS próprio. */
(function () {
  /* Texto da célula, em minúsculas. textContent e não innerText: innerText
     obriga o navegador a recalcular o layout a CADA leitura, e como o filtro
     lê e esconde linha por linha, com 5 mil peças a tela travava. Na coluna
     de botões (À vista / Escondida) vale só o botão marcado, senão filtrar
     por "escondida" trazia todas as linhas. */
  function texto(linha, col) {
    const c = linha.children[col];
    if (!c) return '';
    const marcado = c.querySelector('.cg-escolha button.on');
    return (marcado || c).textContent.replace(/\s+/g, ' ').trim().toLowerCase();
  }

  /* Duas passadas (ler tudo, depois escrever tudo) e escrita só onde o estado
     muda, para o navegador fazer um único layout no fim. */
  function filtrar(tabela) {
    const filtros = [...tabela.querySelectorAll('.cg-table-search')]
      .map(i => ({ col: +i.dataset.col, v: i.value.trim().toLowerCase() }))
      .filter(f => f.v);
    const linhas = [...tabela.tBodies[0].querySelectorAll('tr:not(.cg-vazio)')];
    const mostrar = linhas.map(tr => filtros.every(f => texto(tr, f.col).includes(f.v)));
    let visiveis = 0;
    linhas.forEach((tr, i) => {
      if (mostrar[i]) visiveis++;
      const escondida = tr.style.display === 'none';
      if (mostrar[i] === escondida) tr.style.display = mostrar[i] ? '' : 'none';
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
    // texto de cada linha lido uma vez só, não a cada comparação do sort
    const chaves = new Map(linhas.map(l => [l, texto(l, col)]));
    linhas.sort((a, b) => {
      const x = chaves.get(a), y = chaves.get(b);
      const nx = parseFloat(x.replace(/[^\d.,-]/g, '').replace(',', '.'));
      const ny = parseFloat(y.replace(/[^\d.,-]/g, '').replace(',', '.'));
      const r = (!isNaN(nx) && !isNaN(ny) && x && y) ? nx - ny : x.localeCompare(y, 'pt-BR');
      return desc ? -r : r;
    });
    const frag = document.createDocumentFragment();
    linhas.forEach(l => frag.appendChild(l));
    corpo.appendChild(frag);
  }

  document.querySelectorAll('table.cg-data-table').forEach(tabela => {
    tabela.querySelectorAll('.cg-table-search').forEach(inp => {
      // espera a pessoa parar de digitar: refiltrar a cada letra é trabalho jogado fora
      let espera;
      inp.addEventListener('input', () => { clearTimeout(espera); espera = setTimeout(() => filtrar(tabela), 200); });
      inp.addEventListener('click', e => e.stopPropagation());
    });
    tabela.querySelectorAll('.cg-sort-btn').forEach(b => {
      b.addEventListener('click', () => ordenar(tabela, +b.dataset.col, b));
    });
  });
})();

/* Excluir com aviso de cascata: busca o impacto ANTES de perguntar, pra
   quem clica nunca ser surpreendido depois - a pergunta já vem com a
   consequência (quantos vínculos somem junto). Usado nas 4 telas de
   Cadastros (Peças, Cores, Móveis, Acabamentos). */
async function excluirRegistro(tipo, id, rotulo, linha) {
  let mensagem = `Excluir "${rotulo}"?`;
  try {
    const r = await fetch('/cadastro/impacto', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tipo, id }),
    });
    const d = await r.json();
    if (d.ok && d.mensagem) mensagem = `Excluir "${rotulo}"?\n\n${d.mensagem}`;
  } catch (e) { /* segue com a mensagem simples se o impacto falhar */ }

  if (!confirm(mensagem)) return;

  const r2 = await fetch('/cadastro/excluir', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tipo, id }),
  });
  const d2 = await r2.json().catch(() => ({}));
  // r2.ok sozinho não basta: a rota responde 200 mesmo quando o registro já
  // não existia (nada pra apagar) - o campo ok no corpo é que diz a verdade.
  if (!r2.ok || !d2.ok) {
    alert('Não consegui excluir' + (d2.erro ? ' (' + d2.erro + ')' : '') + '. Recarregue a página e tente de novo.');
    return;
  }
  linha.remove();
  const tabela = document.querySelector('table.cg-data-table');
  if (tabela) {
    const cont = document.querySelector('[data-contador="' + tabela.id + '"]');
    if (cont) {
      const n = tabela.tBodies[0].querySelectorAll('tr:not(.cg-vazio)').length;
      cont.textContent = n + (n === 1 ? ' registro' : ' registros');
    }
  }
}

function editarPeca(cod, descricao, comp, larg) {
  abrirModal('Editar peça ' + cod, `
    <div class="vc-modal-campo"><label>Descrição</label>
      <input type="text" id="ed-desc" value="${descricao.replace(/"/g, '&quot;')}"></div>
    <div class="vc-modal-campo"><label>Comprimento (mm)</label>
      <input type="number" id="ed-comp" value="${comp}"></div>
    <div class="vc-modal-campo"><label>Largura (mm)</label>
      <input type="number" id="ed-larg" value="${larg}"></div>
  `, async () => {
    const desc = document.getElementById('ed-desc').value;
    const compV = document.getElementById('ed-comp').value;
    const largV = document.getElementById('ed-larg').value;
    const r = await fetch('/cadastro/editar', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tipo: 'peca', id: cod, descricao: desc, comp_mm: compV, larg_mm: largV }),
    });
    const d = await r.json();
    if (!d.ok) { erroModal(Object.values(d.erros || {})[0] || 'Não consegui salvar.'); return; }
    location.reload();
  });
}

function editarModelo(cod, descricao) {
  abrirModal('Editar móvel ' + cod, `
    <div class="vc-modal-campo"><label>Descrição</label>
      <input type="text" id="ed-desc" value="${descricao.replace(/"/g, '&quot;')}"></div>
  `, async () => {
    const desc = document.getElementById('ed-desc').value;
    const r = await fetch('/cadastro/editar', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tipo: 'modelo', id: cod, descricao: desc }),
    });
    const d = await r.json();
    if (!d.ok) { erroModal(Object.values(d.erros || {})[0] || 'Não consegui salvar.'); return; }
    location.reload();
  });
}
