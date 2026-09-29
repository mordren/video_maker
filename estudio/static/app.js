/* Estúdio de Histórias: interface. Roteamento por hash, sem framework. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rotaAtiva = hash => (location.hash || '#/') === hash;
const usd = v => 'US$ ' + Number(v || 0).toFixed(4);
const app = $('#app');
let timer = null, presets = null, statusSis = null;

async function api(url, opts = {}) {
  const o = {...opts, headers: {...(opts.headers || {})}};
  if (o.body && !(o.body instanceof FormData)) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.body); }
  const r = await fetch(url, o);
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); msg = j.detail || msg; } catch {}
    // 405 numa rota que a página conhece = o servidor ainda roda o código antigo.
    if (r.status === 405) msg = 'O servidor está rodando uma versão antiga do programa. Reinicie o servidor (Ctrl+C e python main.py) e tente de novo.';
    throw new Error(msg);
  }
  const tipo = r.headers.get('content-type') || '';
  return tipo.includes('json') ? r.json() : r.blob();
}
function toast(msg, erro = false) {
  const d = document.createElement('div'); d.textContent = msg; if (erro) d.className = 'err';
  $('#toast').append(d); setTimeout(() => d.remove(), erro ? 8000 : 4000);
}
async function acao(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}
function lightbox(src) {
  const d = document.createElement('div'); d.className = 'lightbox'; d.innerHTML = `<img src="${src}">`;
  d.onclick = () => d.remove(); document.body.append(d);
}
document.addEventListener('click', e => { const i = e.target.closest('img[data-zoom]'); if (i) lightbox(i.src); });

const STATUS = {
  na_fila: ['na fila', 'run'], escrevendo: ['escrevendo', 'run'], revisando_texto: ['humanizando', 'run'], decupando: ['dividindo em cenas', 'run'],
  aguardando_historia: ['revisar história', 'warn'], gerando_imagens: ['gerando imagens', 'run'],
  refazendo: ['refazendo cena', 'run'], aguardando_imagens: ['revisar imagens', 'warn'], narrando: ['narrando', 'run'],
  montando: ['montando', 'run'], pronto: ['pronto', 'ok'], erro: ['erro', 'err'], interrompido: ['interrompido', 'err'],
};
const EM_ANDAMENTO = ['na_fila', 'escrevendo', 'revisando_texto', 'decupando', 'gerando_imagens', 'refazendo', 'narrando', 'montando'];
const badge = s => { const [t, c] = STATUS[s] || [s, '']; return `<span class="badge ${c}">${esc(t)}</span>`; };

/* ------------------------------------------------------------------ roteador */
async function rota() {
  clearInterval(timer); timer = null;
  const h = location.hash.replace(/^#/, '') || '/';
  const [, seg, id] = h.split('/');
  $$('nav a').forEach(a => a.classList.toggle('on', a.dataset.nav === ({'': 'projetos', projeto: 'projetos', canal: 'canais'}[seg || ''] || seg)));
  app.innerHTML = '<div class="spin"></div>';
  try {
    if (!presets) presets = await api('/api/presets');
    if (!seg) await telaProjetos();
    else if (seg === 'projeto') await telaProjeto(+id);
    else if (seg === 'canais') await telaCanais();
    else if (seg === 'canal') await telaCanal(id === 'novo' ? null : +id);
    else if (seg === 'custos') await telaCustos();
    else if (seg && seg.startsWith('desempenho')) await telaDesempenho();
    else app.innerHTML = '<p>Página não encontrada.</p>';
  } catch (e) { app.innerHTML = `<div class="panel bad">${esc(e.message)}</div>`; }
}
window.addEventListener('hashchange', rota);

async function carregarSistema() {
  statusSis = await api('/api/status');
  const faltam = [];
  if (!statusSis.chaves.openrouter) faltam.push('OPENROUTER_API_KEY');
  if (!statusSis.chaves.pollinations) faltam.push('POLLINATIONS_API_KEY');
  $('#sistema').innerHTML = (statusSis.desatualizado ? `<span class="badge err" title="Algum arquivo .py mudou depois que o servidor subiu">código novo: reinicie o servidor (Ctrl+C e python main.py)</span> ` : '') +
    (statusSis.simulacao ? '<span class="badge warn">modo simulação (nada é pago)</span>'
    : faltam.length ? `<span class="badge err">falta no .env: ${faltam.join(', ')}</span>`
    : `<span class="small">${esc(statusSis.modelos.texto)} · ${esc(statusSis.modelos.juiz)} · ${esc(statusSis.modelos.imagem)}</span>`);
}
setInterval(() => carregarSistema().catch(() => {}), 30000);

/* ------------------------------------------------------------------ vídeos */
async function telaProjetos() {
  const [canais, projetos] = await Promise.all([api('/api/canais'), api('/api/projetos')]);
  if (!rotaAtiva('#/') && location.hash !== '') return;
  clearInterval(timer); timer = null;
  app.innerHTML = `
  <div class="panel">
    <h2>Novo vídeo</h2>
    <div class="row">
      <div class="fix" style="min-width:240px"><label>Canal</label>
        <select id="nCanal">${canais.map(c => `<option value="${c.id}">${esc(c.nome)} · ${esc(c.idioma)} · ${esc(c.formato)}</option>`).join('')}</select></div>
      <div><label>Assunto da história</label><input type="text" id="nAssunto" placeholder="ex.: vigia noturno ouve batidas dentro do bueiro · ou: guarda de um castelo vê a rainha morta subir a torre"></div>
    </div>
    <div class="row" style="align-items:center;margin-top:6px">
      <label class="inline fix"><input type="checkbox" id="nAuto"> automático (não para nas revisões)</label>
      <div></div>
      <button class="main fix" id="nCriar">Criar vídeo</button>
    </div>
    <div class="small" style="margin-top:6px">API: <code>POST /api/projetos {"canal": "slug-ou-id", "assunto": "...", "automatico": true}</code></div>
  </div>
  <div class="panel">
    <h2>Vídeos</h2>
    ${projetos.length ? `<table><tr><th>#</th><th>Título / assunto</th><th>Canal</th><th>Status</th><th>Custo</th><th>Atualizado</th></tr>
    ${projetos.map(p => `<tr class="link" data-id="${p.id}"><td>${p.id}</td>
      <td><b>${esc(p.titulo || '')}</b>${p.titulo ? '<br>' : ''}<span class="small">${esc(p.assunto)}</span>
      ${p.ressalva ? `<br><span class="small" style="color:var(--warn)">ressalva: ${esc(p.ressalva)}</span>` : ''}</td>
      <td>${esc(p.canal)}</td><td>${badge(p.status)}${p.automatico ? ' <span class="badge">auto</span>' : ''}</td>
      <td>${usd(p.custo)}</td><td class="small">${esc(p.atualizado_em || '')}</td></tr>`).join('')}</table>`
    : '<p class="small">Nenhum vídeo ainda.</p>'}
  </div>`;
  $('#nCriar').onclick = async () => {
    const assunto = $('#nAssunto').value.trim();
    if (!assunto) return toast('Escreva o assunto.', true);
    const r = await acao(() => api('/api/projetos', {method: 'POST', body: {canal: +$('#nCanal').value, assunto, automatico: $('#nAuto').checked}}));
    location.hash = '#/projeto/' + r.id;
  };
  $('#nAssunto').onkeydown = e => { if (e.key === 'Enter') $('#nCriar').click(); };
  $$('tr.link').forEach(tr => tr.onclick = () => location.hash = '#/projeto/' + tr.dataset.id);
  if (projetos.some(p => EM_ANDAMENTO.includes(p.status))) timer = setInterval(() => { if (!document.activeElement || document.activeElement.id !== 'nAssunto') telaProjetos(); }, 5000);
}

/* ------------------------------------------------------------------ projeto */
let abaProjeto = 'historia', ultimoEvento = 0, projetoAtual = null;

async function telaProjeto(id) {
  const p = await api('/api/projetos/' + id);
  const eventos = await api(`/api/projetos/${id}/eventos`);
  if (!rotaAtiva('#/projeto/' + id)) return;
  clearInterval(timer); timer = null;
  projetoAtual = p;
  ultimoEvento = eventos.length ? eventos[eventos.length - 1].id : 0;
  if (!p.historia && abaProjeto !== 'avaliacao') abaProjeto = 'historia';
  const etapas = [['escrevendo', 'História + Jev'], ['revisando_texto', 'Humanizer'], ['decupando', 'Cenas'], ['aguardando_historia', 'Sua revisão'],
    ['gerando_imagens', 'Imagens'], ['aguardando_imagens', 'Sua revisão'], ['narrando', 'Narração'], ['montando', 'Montagem'], ['pronto', 'Pronto']];
  const idx = etapas.findIndex(e => e[0] === p.status || (p.status === 'refazendo' && e[0] === 'aguardando_imagens'));
  const rodando = EM_ANDAMENTO.includes(p.status);
  app.innerHTML = `
  <div class="row" style="align-items:center">
    <div><h2 style="margin:0">${esc(p.historia?.titulo || p.assunto)} ${badge(p.status)}</h2>
      <div class="small">#${p.id} · ${esc(p.canal.nome)} · assunto: ${esc(p.assunto)} · custo ${usd(p.custos.total)} · <a href="/arquivos/${esc(p.pasta)}/estado.json" target="_blank">pasta ${esc(p.pasta)}</a></div></div>
    <div class="fix row" id="acoes"></div>
  </div>
  <div class="steps">${etapas.map((e, i) => `<span class="${i < idx ? 'done' : i === idx ? 'cur' : ''}">${e[1]}</span>`).join('')}</div>
  ${p.ressalva ? `<div class="panel ${p.ressalva.startsWith('REPROVADA') ? 'bad' : 'alert'}"><b>Aprovada com ressalva:</b> ${esc(p.ressalva)}. Veja as notas em Avaliações e decida se segue.</div>` : ''}
  ${p.erro ? `<div class="panel bad"><b>Erro:</b> ${esc(p.erro)}</div>` : ''}
  <div class="layout">
    <div>
      <div class="tabs">${[['historia', 'História'], ['avaliacao', `Avaliações (${p.versoes.length})`], ['imagens', 'Imagens'], ['video', 'Vídeo'], ['custos', 'Custos']]
        .map(([k, t]) => `<button data-aba="${k}" class="${abaProjeto === k ? 'on' : ''}">${t}</button>`).join('')}</div>
      <div id="aba"></div>
    </div>
    <div><h3 style="margin-top:14px">Registro</h3><div class="log" id="log"></div></div>
  </div>`;
  renderEventos(eventos);
  renderAcoes(p);
  $$('.tabs button').forEach(b => b.onclick = () => { abaProjeto = b.dataset.aba; telaProjeto(id); });
  ({historia: abaHistoria, avaliacao: abaAvaliacao, imagens: abaImagens, video: abaVideo, custos: abaCustos})[abaProjeto](p);
  if (rodando || p.status === 'refazendo') timer = setInterval(() => pollProjeto(id, p.status), 2000);
}

function renderEventos(evs, anexar = false) {
  const log = $('#log'); if (!log) return;
  const html = evs.slice().reverse().map(e => `<div class="${e.nivel}"><time>${esc((e.criado_em || '').slice(11))}</time>${esc(e.msg)}</div>`).join('');
  if (anexar) log.insertAdjacentHTML('afterbegin', html); else log.innerHTML = html;
}

async function pollProjeto(id, statusAnterior) {
  if (!rotaAtiva('#/projeto/' + id)) { clearInterval(timer); timer = null; return; }
  try {
    const evs = await api(`/api/projetos/${id}/eventos?depois=${ultimoEvento}`);
    if (evs.length) { ultimoEvento = evs[evs.length - 1].id; renderEventos(evs, true); }
    const p = await api('/api/projetos/' + id);
    if (!rotaAtiva('#/projeto/' + id)) return;
    const imgsMudaram = JSON.stringify(p.cenas.map(c => c.status + c.versao)) !== JSON.stringify(projetoAtual.cenas.map(c => c.status + c.versao));
    if (p.status !== statusAnterior || (abaProjeto === 'imagens' && imgsMudaram)) telaProjeto(id);
  } catch (e) { /* servidor reiniciando */ }
}

function renderAcoes(p) {
  const b = [];
  if (p.status === 'aguardando_historia') {
    b.push(`<button class="main" data-a="aprovar-historia">Aprovar história e gerar imagens</button>`);
    b.push(`<button data-a="refazer-historia">Escrever de novo</button>`);
  }
  if (p.status === 'aguardando_imagens') b.push(`<button class="main" data-a="aprovar-imagens">Aprovar imagens e montar vídeo</button>`);
  if (p.status === 'pronto') b.push(`<button data-a="aprovar-imagens">Montar de novo</button>`);
  if (['erro', 'interrompido'].includes(p.status)) b.push(`<button class="main" data-a="continuar">Continuar de onde parou</button>`);
  if (!EM_ANDAMENTO.includes(p.status)) {
    b.push(`<button data-a="recomecar" title="Apaga história, imagens, narração e vídeo e escreve de novo com o mesmo assunto">Recomeçar do zero</button>`);
    b.push(`<button data-a="excluir" title="Remove este vídeo e a pasta dele">Excluir</button>`);
  }
  $('#acoes').innerHTML = b.join('');
  $$('#acoes button').forEach(btn => btn.onclick = async () => {
    const a = btn.dataset.a;
    if (a === 'refazer-historia' && !confirm('Escrever uma nova versão da história? As versões anteriores continuam em Avaliações.')) return;
    if (a === 'recomecar' && !confirm('Recomeçar do zero?\n\nApaga a história, todas as versões e avaliações, as imagens, a narração e o vídeo deste projeto, e escreve tudo de novo com o mesmo assunto. Os custos já gastos continuam registrados.')) return;
    if (a === 'excluir') {
      if (!confirm(`Excluir o vídeo "${p.historia?.titulo || p.assunto}"?\n\nO projeto e a pasta dele serão apagados. Não dá para desfazer. Fichas e placas da biblioteca do canal continuam.`)) return;
      btn.disabled = true;
      try { await acao(() => api(`/api/projetos/${p.id}`, {method: 'DELETE'}), 'Vídeo excluído.'); location.hash = '#/'; }
      catch { btn.disabled = false; }
      return;
    }
    btn.disabled = true;
    try { await acao(() => api(`/api/projetos/${p.id}/${a}`, {method: 'POST'})); } finally { telaProjeto(p.id); }
  });
}

const contarPalavras = t => (t.match(/[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu) || []).length;

function abaHistoria(p) {
  const el = $('#aba'), h = p.historia;
  if (!h) { el.innerHTML = '<p class="small">A história ainda está sendo escrita. Acompanhe no registro ao lado.</p>'; return; }
  const editavel = !EM_ANDAMENTO.includes(p.status);
  const podeTexto = editavel && ['aguardando_historia', 'erro', 'interrompido'].includes(p.status);
  const narracao = (h.cenas || []).map(c => c.narracao).join(' ') || h.narracao || '';
  const efs = presets.efeitos;
  const opts = (tipo, atual, vazio) => (vazio ? `<option value="">(nenhum)</option>` : '') +
    efs.filter(e => e.tipo === tipo).map(e => `<option ${e.nome === atual ? 'selected' : ''}>${e.nome}</option>`).join('');
  const nomes = Object.fromEntries((h.personagens || []).map(x => [x.id, x.nome || x.id]));
  el.innerHTML = `
  <div class="panel leitura">
    <input type="text" id="hTitulo" class="titulo-hist" value="${esc(h.titulo)}" ${editavel ? '' : 'disabled'} placeholder="Título">
    <textarea id="hNarr" class="texto-narrador" ${podeTexto ? '' : 'disabled'}>${esc(narracao)}</textarea>
    <div class="row" style="align-items:center;margin-top:8px">
      <span class="small" id="hConta"></span>
      ${editavel ? '<button class="fix gold" id="hSalvarTexto">Salvar texto</button>' : ''}</div>
    <div class="small" style="margin-top:6px">${podeTexto ? 'Este é o texto que o narrador lê, do começo ao fim. Se você mudar a narração, as cenas são divididas de novo a partir dele.'
      : 'A narração fica fixa depois que as imagens começam; o título ainda pode mudar.'}</div>
    <label>Descrição do YouTube</label><textarea id="hDesc" ${editavel ? '' : 'disabled'}>${esc(h.descricao_youtube || '')}</textarea>
  </div>
  <details class="panel"><summary><b>Cenas, personagens e prompts de imagem</b> <span class="small">(${(h.cenas || []).length} cenas · ajuste fino antes das imagens)</span></summary>
    <div class="grid2" style="margin-top:10px">
      <div><h3>Personagens</h3>${(h.personagens || []).map(x => `<div><b>${esc(x.nome || x.id)}</b> <span class="small">(${esc(x.id)})</span><br><span class="small">${esc(x.descricao_fixa)}</span></div>`).join('') || '<span class="small">nenhum</span>'}</div>
      <div><h3>Ambientes</h3>${(h.ambientes || []).map(x => `<div><b>${esc(x.id)}</b><br><span class="small">${esc(x.descricao_fixa)}</span></div>`).join('') || '<span class="small">nenhum</span>'}</div>
    </div>
    <table class="scene-edit" style="margin-top:10px"><tr><th>#</th><th style="width:34%">Trecho narrado</th><th>Prompt de imagem</th><th style="width:130px">Tensão / efeitos</th></tr>
    ${(h.cenas || []).map((c, i) => `<tr data-i="${i}"><td><b>${c.n}</b></td>
      <td class="small" style="color:var(--txt)">${esc(c.narracao)}</td>
      <td><textarea data-k="prompt_imagem" ${editavel ? '' : 'disabled'}>${esc(c.prompt_imagem)}</textarea>
        <input type="text" data-k="destaque" placeholder="destaque (objeto em vermelho)" value="${esc(c.destaque || '')}" ${editavel ? '' : 'disabled'} style="margin-top:4px">
        <div class="small">${c.personagem ? 'personagem: ' + esc(nomes[c.personagem] || c.personagem) + ' · ' : ''}${c.ambiente ? 'ambiente: ' + esc(c.ambiente) : ''}</div></td>
      <td><select data-k="tensao" ${editavel ? '' : 'disabled'}>${[1, 2, 3, 4, 5].map(t => `<option ${t == c.tensao ? 'selected' : ''}>${t}</option>`).join('')}</select>
        <select data-k="efeito" ${editavel ? '' : 'disabled'} style="margin-top:4px">${opts('movimento', c.efeito)}</select>
        <select data-k="extra" ${editavel ? '' : 'disabled'} style="margin-top:4px">${opts('sobreposicao', c.extra, true)}</select></td></tr>`).join('')}
    </table>
    ${editavel ? '<div class="row" style="margin-top:8px"><span></span><button class="fix" id="hSalvarCenas">Salvar cenas</button></div>' : ''}
  </details>`;
  const ta = $('#hNarr');
  const ajustar = () => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 4 + 'px'; };
  const conta = () => {
    const n = contarPalavras(ta.value);
    $('#hConta').textContent = `${n} palavras · ~${(n / 2.75).toFixed(0)} s de narração${ta.value.trim() !== narracao ? ' · alterado' : ''}`;
  };
  conta(); ajustar(); ta.oninput = () => { conta(); ajustar(); };
  if (!editavel) return;
  $('#hSalvarTexto').onclick = async () => {
    const mudou = ta.value.trim() !== narracao;
    if (mudou && !confirm('Salvar o texto novo?\n\nAs cenas serão divididas de novo a partir dele (uma chamada rápida ao DeepSeek). Os prompts de imagem ajustados à mão se perdem.')) return;
    const r = await acao(() => api(`/api/projetos/${p.id}/texto`, {method: 'PUT', body: {titulo: $('#hTitulo').value, descricao_youtube: $('#hDesc').value, narracao: ta.value}}),
      mudou ? 'Texto salvo. Dividindo em cenas de novo...' : 'Título e descrição salvos.');
    telaProjeto(p.id);
  };
  $('#hSalvarCenas').onclick = async () => {
    const nova = structuredClone(h);
    nova.titulo = $('#hTitulo').value; nova.descricao_youtube = $('#hDesc').value;
    $$('tr[data-i]', el).forEach(tr => {
      const c = nova.cenas[+tr.dataset.i];
      $$('[data-k]', tr).forEach(f => c[f.dataset.k] = f.dataset.k === 'tensao' ? +f.value : f.value);
    });
    await acao(() => api(`/api/projetos/${p.id}/historia`, {method: 'PUT', body: {historia: nova}}), 'Cenas salvas.');
    telaProjeto(p.id);
  };
}

function abaAvaliacao(p) {
  const el = $('#aba');
  if (!p.versoes.length) { el.innerHTML = '<p class="small">Nenhuma versão avaliada ainda.</p>'; return; }
  el.innerHTML = p.versoes.slice().reverse().map(v => {
    const a = v.avaliacao, ch = v.checagem || {}, m = ch.metricas || {}, hum = ch.humanizer;
    return `<div class="panel ${v.passou ? '' : 'alert'}">
      <div class="row" style="align-items:center"><div><b>Versão ${v.versao}</b> · ${esc(v.origem)} ${v.passou ? '<span class="badge ok">passou</span>' : v.passou === 0 ? '<span class="badge warn">não passou</span>' : ''}</div>
      <div class="fix small">${m.palavras ?? '?'} palavras · ${m.cenas ?? '?'} cenas · ~${m.duracao_estimada_s ?? '?'}s ${v.nota_geral != null ? '· nota geral <b>' + v.nota_geral.toFixed(2) + '</b>' : ''}</div></div>
      ${(ch.problemas || []).length ? `<div class="fail small" style="margin-top:6px">Checagem em Python: ${ch.problemas.map(esc).join('; ')}</div>` : ''}
      ${a ? `<table style="margin-top:8px"><tr><th>Pergunta</th><th>Tipo</th><th>Valor</th><th>Corte</th><th></th></tr>
        ${Object.entries(a.perguntas).map(([k, q]) => `<tr><td>${esc(q.pergunta)} <span class="small">${esc(k)}${q.trava ? ' · trava' : ''}</span></td><td>${q.tipo}</td>
          <td class="${q.passou ? 'pass' : 'fail'}">${q.tipo === 'score' ? q.valor.toFixed(1) + '/10' : q.valor.toFixed(2)}</td><td>${q.corte}</td>
          <td>${q.passou ? '✓' : '✗'}</td></tr>`).join('')}</table>` : ''}
      ${hum ? `<div class="small" style="margin-top:8px"><b>Humanizer:</b> ${esc(hum.motivo)} · sinais antes ${hum.achados_antes.length}, depois ${hum.achados_depois.length}
        ${(hum.avisos || []).length ? '<br>Avisos: ' + hum.avisos.map(esc).join('; ') : ''}
        ${hum.revertidas.length ? '<br>Revertidas: ' + hum.revertidas.map(r => `cena ${r.n} (${esc(r.motivo)})`).join(', ') : ''}
        ${hum.achados_depois.length ? '<br>Restaram: ' + hum.achados_depois.map(esc).join('<br>') : ''}</div>` : ''}
      <details style="margin-top:6px"><summary class="small">Narração desta versão</summary><p>${esc(v.historia?.cenas ? v.historia.cenas.map(c => c.narracao).join(' ') : (v.historia?.narracao || ''))}</p></details>
    </div>`;
  }).join('');
}

function abaImagens(p) {
  const el = $('#aba'), h = p.historia;
  if (!h) { el.innerHTML = '<p class="small">Sem história ainda.</p>'; return; }
  const porN = Object.fromEntries(p.cenas.map(c => [c.n, c]));
  const larga = p.canal.formato !== 'short';
  const podeRefazer = ['aguardando_imagens', 'pronto', 'erro', 'interrompido'].includes(p.status);
  const refs = [...(h.personagens || []).filter(x => x.bib_chave).map(x => ['Ficha: ' + (x.nome || x.id), `biblioteca/${p.canal.slug}/personagens/${x.bib_chave}/ficha.jpg`]),
    ...(h.ambientes || []).filter(x => x.bib_chave).map(x => ['Placa: ' + x.id, `biblioteca/${p.canal.slug}/ambientes/${x.bib_chave}/placa.jpg`])];
  const t = Date.now();
  el.innerHTML = `
  ${refs.length ? `<div class="panel"><h3 style="margin-top:0">Referências</h3><div class="row" style="align-items:flex-start">${refs.map(([n, src]) =>
    `<div class="fix" style="max-width:260px"><img data-zoom src="/arquivos/${src}?t=${t}" style="max-width:100%;max-height:170px;border-radius:6px"><div class="small">${esc(n)}</div></div>`).join('')}</div></div>` : ''}
  <div class="row" style="align-items:center;margin-bottom:10px">
    <div>${p.arquivos.folha ? `<a href="${p.arquivos.folha}?t=${t}" target="_blank">Abrir folha de contato</a>` : ''}</div>
    ${podeRefazer ? '<button class="fix" id="iRefazerTodas" title="Gera fichas, placas e cenas de novo com o estilo atual do canal">Refazer todas as imagens</button>' : ''}</div>
  <div class="scenes">${h.cenas.map(c => {
    const s = porN[c.n];
    const img = s && s.status === 'ok' && s.arquivo ? `<img data-zoom src="/arquivos/${s.arquivo}?v=${s.versao}&t=${t}">`
      : s && s.status === 'gerando' ? '<div class="spin"></div>' : s && s.status === 'erro' ? `<span class="small fail" style="padding:10px">${esc(s.erro)}</span>` : '<span class="small">aguardando</span>';
    return `<div class="scene ${larga ? 'wide' : ''}"><div class="img">${img}<span class="n">${c.n}</span></div>
      <div class="body"><div>${esc(c.narracao)}</div>
      <textarea data-n="${c.n}" ${podeRefazer ? '' : 'disabled'}>${esc(c.prompt_imagem)}</textarea>
      <input type="text" data-dest="${c.n}" placeholder="destaque (objeto em vermelho)" value="${esc(c.destaque || '')}" ${podeRefazer ? '' : 'disabled'}>
      ${c.mesma_imagem_de ? `<div class="small" style="color:var(--acc)">mesma imagem da cena ${c.mesma_imagem_de}, com outro movimento (${esc(c.efeito || 'detalhe')}), sem custo</div>` : ''}
      <div class="small">tensão ${c.tensao} · ${esc(c.efeito || '')}${c.extra ? ' + ' + esc(c.extra) : ''}${s && !c.mesma_imagem_de ? ' · seed ' + s.seed + (s.versao ? ' · v' + s.versao : '') : ''}</div>
      <div class="row"><button class="small" data-refazer="${c.n}" ${podeRefazer ? '' : 'disabled'} title="${c.mesma_imagem_de ? 'Gera uma imagem própria para esta cena' : 'Gera esta imagem de novo'}">${c.mesma_imagem_de ? 'Imagem própria' : 'Refazer'}</button>
      <button class="small" data-testar="${c.n}" ${s && s.status === 'ok' ? '' : 'disabled'}>Testar efeito</button></div></div></div>`;
  }).join('')}</div>`;
  $$('[data-refazer]', el).forEach(b => b.onclick = async () => {
    const n = +b.dataset.refazer, prompt = $(`textarea[data-n="${n}"]`, el).value, destaque = $(`input[data-dest="${n}"]`, el).value;
    b.disabled = true;
    await acao(() => api(`/api/projetos/${p.id}/cenas/${n}/refazer`, {method: 'POST', body: {prompt_imagem: prompt, destaque}}), `Refazendo a cena ${n}...`);
    telaProjeto(p.id);
  });
  const rt = $('#iRefazerTodas');
  if (rt) rt.onclick = async () => {
    if (!confirm('Refazer todas as imagens?\n\nGera fichas, placas e todas as cenas de novo com o estilo atual do canal. A história continua igual. Custa o mesmo que a primeira geração.')) return;
    rt.disabled = true;
    try { await acao(() => api(`/api/projetos/${p.id}/refazer-imagens`, {method: 'POST'}), 'Refazendo as imagens...'); } finally { telaProjeto(p.id); }
  };
  $$('[data-testar]', el).forEach(b => b.onclick = async () => {
    const n = +b.dataset.testar, c = h.cenas.find(x => x.n === n);
    const ef = prompt('Efeito para testar (5 s):\n' + presets.efeitos.map(e => e.nome).join(', '), c.efeito || 'zoom_in');
    if (!ef) return;
    b.disabled = true; b.textContent = 'gerando...';
    try {
      const r = await acao(() => api('/api/efeitos/teste', {method: 'POST', body: {efeito: ef, projeto_id: p.id, n, tensao: c.tensao}}));
      const d = document.createElement('div'); d.className = 'lightbox';
      d.innerHTML = `<video src="${r.url}?t=${Date.now()}" autoplay loop controls style="max-height:90vh"></video>`;
      d.onclick = e => { if (e.target === d) d.remove(); }; document.body.append(d);
    } finally { b.disabled = false; b.textContent = 'Testar efeito'; }
  });
}

function abaVideo(p) {
  const el = $('#aba'), t = Date.now();
  const trilhas = p.canal.trilhas.filter(x => x.ativa);
  el.innerHTML = `
  ${p.arquivos.video ? `<div class="panel" style="text-align:center"><video src="${p.arquivos.video}?t=${t}" controls></video>
    <div style="margin-top:8px"><a href="${p.arquivos.video}" download>Baixar final.mp4</a>
    ${p.arquivos.legendas ? ` · <a href="${p.arquivos.legendas}" download>legendas.ass</a>` : ''}
    ${p.arquivos.narracao ? ` · <a href="${p.arquivos.narracao}" download>narração.mp3</a>` : ''}</div></div>`
    : '<p class="small">O vídeo aparece aqui depois da montagem.</p>'}
  <div class="panel"><h3 style="margin-top:0">Trilha deste vídeo</h3>
    ${trilhas.length ? `<div class="row"><select id="vTrilha"><option value="">(sorteada entre as ativas do canal)</option>
      ${trilhas.map(x => `<option value="${esc(x.arquivo)}" ${x.arquivo === p.trilha ? 'selected' : ''}>${esc(x.nome)}</option>`).join('')}</select>
      <button class="fix" id="vSalvarTrilha">Salvar</button></div>
      <div class="small" style="margin-top:6px">Depois de trocar, use "Montar de novo". Os volumes ficam no canal.</div>`
    : `<p class="small">O canal ainda não tem trilhas. O vídeo sai só com a narração. <a href="#/canal/${p.canal.id}">Adicionar trilhas no canal</a>.</p>`}
  </div>
  ${p.historia ? `<div class="panel"><h3 style="margin-top:0">Texto para o YouTube</h3><b>${esc(p.historia.titulo)}</b><p>${esc(p.historia.descricao_youtube || '')}</p></div>` : ''}
  ${p.arquivos.video ? `<div class="panel"><h3 style="margin-top:0">Enviar ao Publicador</h3>
    ${p.canal.publicador_canal ? `<div class="row"><button class="fix" id="vPub">${p.publicador ? 'Enviar de novo' : 'Enviar ao Publicador'}</button>
      <span class="small">canal no publicador: <b>${esc(p.canal.publicador_canal)}</b></span></div>` :
      `<p class="small">Defina o "canal no publicador" na <a href="#/canal/${p.canal.id}">tela do canal</a> antes de enviar.</p>`}
    ${p.publicador ? `<div class="small" style="margin-top:6px">Enviado em ${esc((p.publicador.enviado_em || '').replace('T', ' '))} como "${esc(p.publicador.arquivo)}".</div>` : ''}
  </div>` : ''}
  <div class="panel"><h3 style="margin-top:0">Publicado no YouTube</h3>
    <div class="row"><input type="text" id="vYt" placeholder="cole o link do Short depois de publicar" value="${p.youtube_id ? 'https://youtube.com/shorts/' + esc(p.youtube_id) : ''}">
      <button class="fix" id="vYtSalvar">Salvar link</button>${p.youtube_id ? '<button class="fix" id="vYtAtualizar">Atualizar métricas</button>' : ''}</div>
    ${p.metricas ? cartaoMetricas(p.metricas) : '<p class="small">As métricas aparecem aqui e na página Desempenho depois de ligar o vídeo e atualizar.</p>'}
  </div>`;
  const pub = $('#vPub');
  if (pub) pub.onclick = async () => {
    if (p.publicador && !confirm('Este vídeo já foi enviado. Enviar de novo cria outro item na fila. Continuar?')) return;
    pub.disabled = true; pub.textContent = 'Enviando...';
    try { await acao(() => api(`/api/projetos/${p.id}/publicador`, {method: 'POST'}), 'Enviado ao publicador.'); } finally { telaProjeto(p.id); }
  };
  const s = $('#vSalvarTrilha');
  if (s) s.onclick = () => acao(() => api(`/api/projetos/${p.id}/trilha`, {method: 'PUT', body: {arquivo: $('#vTrilha').value || null}}), 'Trilha salva.');
  $('#vYtSalvar').onclick = async () => {
    await acao(() => api(`/api/projetos/${p.id}/youtube`, {method: 'PUT', body: {url: $('#vYt').value.trim() || null}}), 'Link salvo.');
    telaProjeto(p.id);
  };
  const at = $('#vYtAtualizar');
  if (at) at.onclick = async () => {
    at.disabled = true; at.textContent = 'Lendo...';
    try {
      const r = await acao(() => api('/api/youtube/atualizar', {method: 'POST', body: {projeto_id: p.id}}));
      const x = r.resultados[0]; if (x && !x.ok) toast(x.erro, true);
    } finally { telaProjeto(p.id); }
  };
}

const pct = v => v == null ? '-' : (v * 100).toFixed(0) + '%';
function curvaSvg(curva, w = 160, h = 40) {
  if (!curva || !curva.length) return '';
  const maxY = Math.max(1, ...curva.map(c => c[1]));
  const pts = curva.map(([x, y]) => `${(x * w).toFixed(1)},${(h - y / maxY * h).toFixed(1)}`).join(' ');
  const y100 = (h - 1 / maxY * h).toFixed(1);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="background:var(--panel2);border-radius:4px">
    <line x1="0" x2="${w}" y1="${y100}" y2="${y100}" stroke="var(--line)" stroke-dasharray="3 3"/>
    <polyline fill="none" stroke="var(--acc)" stroke-width="1.5" points="${pts}"/></svg>`;
}
function cartaoMetricas(m) {
  const origens = Object.entries(m.origens || {}).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => `${esc(k)}: ${v}`).join(' · ');
  return `<div class="grid3" style="margin-top:10px">
    <div><div class="small">Views</div><b style="font-size:20px">${m.views ?? '-'}</b>${m.views_engajadas != null ? `<div class="small">${m.views_engajadas} engajadas</div>` : ''}</div>
    <div><div class="small">Assistido em média</div><b style="font-size:20px">${m.media_percentual != null ? m.media_percentual.toFixed(0) + '%' : '-'}</b>
      <div class="small">${m.media_visualizacao_s != null ? m.media_visualizacao_s + ' s de ' + m.duracao_s + ' s' : ''}</div></div>
    <div><div class="small">Ainda assistindo aos 3 s · no fim</div><b style="font-size:20px">${pct(m.retencao_3s)}</b> · ${pct(m.retencao_fim)}</div>
  </div>
  <div style="margin-top:8px">${curvaSvg(m.retencao_curva, 320, 60)}<div class="small">Curva de retenção (a linha tracejada é 100%; acima dela, gente revendo o Short)</div></div>
  <div class="small" style="margin-top:6px">${m.likes ?? 0} curtidas · ${m.comentarios ?? 0} comentários · ${m.compartilhamentos ?? 0} compartilhamentos${origens ? ' · origem: ' + origens : ''}
    · publicado ${esc(m.publicado_em || '')} · lido em ${esc((m.atualizado_em || '').replace('T', ' '))}</div>
  ${m.aviso_totais || m.aviso_retencao ? `<div class="small" style="color:var(--warn)">${esc(m.aviso_totais || '')} ${esc(m.aviso_retencao || '')}</div>` : ''}`;
}

/* ------------------------------------------------------------------ desempenho */
async function telaDesempenho() {
  const [st, d] = await Promise.all([api('/api/youtube/status'), api('/api/desempenho')]);
  if (!rotaAtiva('#/desempenho') && !location.hash.startsWith('#/desempenho')) return;
  const r = d.correlacao_gancho_3s;
  const conexao = !st.cliente_configurado ? `
    <div class="panel alert"><b>Para ler as métricas do YouTube, configure uma vez:</b>
      <ol class="small" style="line-height:1.7">
        <li>Em <a href="https://console.cloud.google.com/" target="_blank">console.cloud.google.com</a>, crie um projeto.</li>
        <li>Em "APIs e serviços → Biblioteca", ative <b>YouTube Data API v3</b> e <b>YouTube Analytics API</b>.</li>
        <li>Em "Tela de consentimento OAuth", escolha Externo, preencha o nome e adicione o seu e-mail como usuário de teste.</li>
        <li>Em "Credenciais → Criar credenciais → ID do cliente OAuth", tipo <b>Aplicativo da Web</b>, com o URI de redirecionamento
          <code>${esc(location.origin)}/api/youtube/retorno</code>.</li>
        <li>Baixe o JSON e salve como <code>${esc(st.arquivo_cliente)}</code>. Depois recarregue esta página e clique em Conectar YouTube.</li>
      </ol>
      <div class="small">Enquanto o app estiver "em teste" no Google, o login vale 7 dias; aí é só conectar de novo.</div></div>`
    : !st.conectado ? `<div class="panel alert">${st.erro ? `<div class="small fail">${esc(st.erro)}</div>` : ''}
        <a href="/api/youtube/conectar"><button class="main">Conectar YouTube</button></a>
        <span class="small">Abre o login do Google. O programa pede só leitura do canal e das estatísticas.</span></div>`
    : `<div class="panel"><div class="row" style="align-items:center"><div>Conectado ao canal <b>${esc(st.canal?.titulo || '')}</b></div>
        <button class="fix" id="dVincular">Ligar vídeos pelo título</button><button class="fix main" id="dAtualizar">Atualizar métricas</button>
        <button class="fix small" id="dSair">Desconectar</button></div></div>`;
  app.innerHTML = `<div class="row" style="align-items:center"><h2 style="margin:0">Desempenho</h2>
    <a class="fix" href="/api/logs/historico"><button>Baixar histórico (.log)</button></a>
    <a class="fix" href="/api/logs/resumo"><button>Baixar resumo (.jsonl)</button></a></div>
  <div style="margin-top:12px">${conexao}</div>
  <div class="panel"><b>Nota do gancho × quem ainda assiste aos 3 s:</b>
    ${r && r.r != null ? `correlação <b>${r.r}</b> em ${r.n} vídeos. ${r.r > 0.4 ? 'O Jev está acertando: nota maior, mais gente fica.' : r.r < 0.1 ? 'Quase nenhuma relação: a pergunta do gancho precisa mudar.' : 'Relação fraca: acompanhe mais vídeos.'}`
      : `precisa de pelo menos 5 vídeos com nota e métrica (hoje: ${r ? r.n : 0}).`}
    <div class="small">É a calibração do roteiro: a nota de corte do gancho vai para onde a retenção começa a subir de verdade.</div></div>
  <div class="panel"><table><tr><th>Vídeo</th><th>Views</th><th>Assistido</th><th>Aos 3 s</th><th>Retenção</th><th>Gancho (Jev)</th><th>Nota geral</th><th>Custo</th></tr>
  ${d.linhas.map(l => { const m = l.metricas || {}; return `<tr><td><a href="#/projeto/${l.id}">${esc(l.titulo || l.assunto)}</a><br>
      <span class="small">${esc(l.canal)} · ${l.youtube_id ? `<a href="https://youtube.com/shorts/${esc(l.youtube_id)}" target="_blank">${esc(l.youtube_id)}</a>` : 'sem link do YouTube'}</span></td>
      <td>${m.views ?? '-'}</td><td>${m.media_percentual != null ? m.media_percentual.toFixed(0) + '%' : '-'}</td><td>${pct(m.retencao_3s)}</td>
      <td>${curvaSvg(m.retencao_curva)}</td><td>${l.nota_gancho != null ? l.nota_gancho.toFixed(1) : '-'}</td>
      <td>${l.nota_geral != null ? l.nota_geral.toFixed(2) : '-'}</td><td>${usd(l.custo)}</td></tr>`; }).join('')}</table>
  <p class="small">"Assistido" é a porcentagem média do vídeo que cada pessoa viu. "Aos 3 s" é a fração do público que ainda estava assistindo no terceiro segundo. O YouTube Analytics atrasa 2 a 3 dias; as views chegam antes.</p></div>`;
  const b = id => $('#' + id);
  if (b('dVincular')) b('dVincular').onclick = async () => {
    const x = await acao(() => api('/api/youtube/vincular-auto', {method: 'POST'}));
    toast(x.ligados.length ? `${x.ligados.length} vídeo(s) ligado(s) pelo título.` : 'Nenhum título novo bateu. Cole o link na aba Vídeo do projeto.');
    telaDesempenho();
  };
  if (b('dAtualizar')) b('dAtualizar').onclick = async () => {
    b('dAtualizar').disabled = true; b('dAtualizar').textContent = 'Lendo o YouTube...';
    try {
      const x = await acao(() => api('/api/youtube/atualizar', {method: 'POST', body: {}}));
      const falhas = x.resultados.filter(r => !r.ok);
      toast(`${x.resultados.length - falhas.length} vídeo(s) atualizados${falhas.length ? `, ${falhas.length} com erro: ${falhas[0].erro}` : ''}.`, !!falhas.length);
    } finally { telaDesempenho(); }
  };
  if (b('dSair')) b('dSair').onclick = async () => { await acao(() => api('/api/youtube/desconectar', {method: 'POST'})); telaDesempenho(); };
}

function abaCustos(p) {
  $('#aba').innerHTML = `<div class="panel"><table><tr><th>Etapa</th><th>Serviço</th><th>Modelo</th><th>Chamadas</th><th>Valor</th><th></th></tr>
  ${p.custos.linhas.map(l => `<tr><td>${esc(l.etapa)}</td><td>${esc(l.servico)}</td><td class="small">${esc(l.modelo)}</td><td>${l.chamadas}</td>
    <td>${usd(l.valor)}</td><td class="small">${l.todos_reais ? 'real' : 'estimado'}</td></tr>`).join('')}
  <tr><td colspan="4"><b>Total</b></td><td><b>${usd(p.custos.total)}</b></td><td></td></tr></table>
  <p class="small">"real" é o valor devolvido pela API; "estimado" usa o preço do catálogo (imagens do Pollinations).</p></div>`;
}

/* ------------------------------------------------------------------ canais */
async function telaCanais() {
  const canais = await api('/api/canais');
  app.innerHTML = `<div class="row" style="align-items:center"><h2 style="margin:0">Canais</h2><button class="fix main" onclick="location.hash='#/canal/novo'">Novo canal</button></div>
  <p class="small">Cada canal guarda uma vez só como as histórias são escritas, julgadas pelo Jev, ilustradas, narradas e sonorizadas.</p>
  <div class="cards">${canais.map(c => `<div class="card" data-id="${c.id}"><b>${esc(c.nome)}</b> <span class="badge">${esc(c.idioma)}</span> <span class="badge">${esc(c.formato)}</span>
    <p class="small">${esc(c.descricao)}</p>
    <div class="small">${Object.keys(c.config.juiz.perguntas || {}).length} perguntas próprias no juiz · estilo ${esc(c.config.estilo.nome)} · voz ${esc(c.config.voz.nome)} · ${c.trilhas.length} trilha(s) · ${c.projetos} vídeo(s)</div></div>`).join('')}</div>`;
  $$('.card').forEach(d => d.onclick = () => location.hash = '#/canal/' + d.dataset.id);
}

const IDIOMAS = ['pt-BR', 'pt-PT', 'en-US', 'en-GB', 'es-ES', 'es-MX', 'fr-FR', 'de-DE', 'it-IT', 'ja-JP'];

async function telaCanal(id) {
  const c = id ? await api('/api/canais/' + id) : {nome: '', idioma: 'pt-BR', descricao: '', formato: 'short', config: structuredClone(presets.padrao), trilhas: [], biblioteca: []};
  const cfg = c.config;
  app.innerHTML = `
  <div class="row" style="align-items:center"><h2 style="margin:0">${id ? 'Canal: ' + esc(c.nome) : 'Novo canal'}</h2>
    <button class="fix" onclick="location.hash='#/canais'">Voltar</button><button class="fix main" id="cSalvar">Salvar canal</button></div>
  <div class="panel" style="margin-top:12px">
    <h3 style="margin-top:0">Identidade</h3>
    <div class="row"><div><label>Nome</label><input type="text" id="cNome" value="${esc(c.nome)}"></div>
      <div class="fix" style="min-width:140px"><label>Idioma</label><select id="cIdioma">${IDIOMAS.map(i => `<option ${i === c.idioma ? 'selected' : ''}>${i}</option>`).join('')}</select></div>
      <div class="fix" style="min-width:160px"><label>Formato</label><select id="cFormato">${Object.entries(presets.formatos).map(([k, v]) => `<option value="${k}" ${k === c.formato ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div></div>
    <label>Sobre o canal (o que é, para quem, o tom)</label><textarea id="cDesc">${esc(c.descricao)}</textarea>
    <div class="row" style="margin-top:10px;align-items:center"><button class="fix gold" id="cSugerir">Sugerir configuração com IA</button>
      <span class="small">O DeepSeek lê o nome e a descrição e preenche regras, perguntas do Jev, estilo, voz, efeitos e trilha. Nada é salvo até você clicar em Salvar.</span></div>
  </div>
  <div id="cCorpo"></div>`;
  const secoes = [['roteiro', 'Roteiro'], ['juiz', 'Juiz (Jev)'], ['visual', 'Imagens'], ['voz', 'Voz e legenda'], ['trilha', 'Trilha'], ['geral', 'Custos e publicação']];
  let secao = 'roteiro';
  const linhaRegra = (r, i) => `<div class="regra"><span class="num">${i + 1}</span><textarea data-regra rows="1">${esc(r)}</textarea><button class="small" data-delr title="Remover">×</button></div>`;
  const linhaModo = m => `<div class="modo"><input type="text" data-modo-nome value="${esc(m.nome)}" placeholder="nome do modo"><textarea data-modo-regra rows="1" placeholder="como esse modo conduz a história">${esc(m.regra)}</textarea><button class="small" data-delm title="Remover">×</button></div>`;
  const corpo = () => {
    $('#cCorpo').innerHTML = `
    <div class="tabs" id="cTabs">${secoes.map(([k, t]) => `<button data-sec="${k}" class="${k === secao ? 'on' : ''}">${t}</button>`).join('')}</div>

    <section data-secao="roteiro">
      <div class="panel">
        <div class="row" style="align-items:center"><h3 style="margin:0">Regras do roteirista</h3>
          <button class="fix" id="cVerPrompt">Ver o prompt completo</button></div>
        <p class="small">O roteirista lê estas regras em todo vídeo: nos ganchos, na história e nas reescritas. Escreva cada uma como instrução direta. Evite exemplos concretos: o modelo tende a copiá-los.</p>
        <div id="cRegras">${(cfg.regras_escrita || []).map(linhaRegra).join('')}</div>
        <button class="small" id="cAddR" style="margin-top:6px">+ regra</button>
      </div>
      <div class="panel">
        <h3 style="margin-top:0">Modos narrativos</h3>
        <p class="small">O roteirista escolhe um por vídeo junto com os ganchos, o que melhor serve ao assunto, evitando repetir o do vídeo anterior, e segue esse modo do começo ao fim. Sem modos, o canal não usa essa escolha.</p>
        <div id="cModos">${(cfg.modos_narrativos || []).map(linhaModo).join('')}</div>
        <button class="small" id="cAddM" style="margin-top:6px">+ modo</button>
      </div>
      <div class="panel">
        <h3 style="margin-top:0">Forma da história</h3>
        <label>Estrutura (batidas em ordem, separadas por vírgula)</label><input type="text" id="cEstrutura" value="${esc((cfg.estrutura || []).join(', '))}">
        <div class="row"><div><label>Palavras mín.</label><input type="number" id="cPMin" placeholder="do formato" value="${cfg.palavras_min ?? ''}"></div>
          <div><label>Palavras máx.</label><input type="number" id="cPMax" placeholder="do formato" value="${cfg.palavras_max ?? ''}"></div>
          <div><label>Máx. personagens</label><input type="number" id="cMaxP" min="1" max="3" value="${cfg.max_personagens}"></div>
          <div><label>Máx. lugares</label><input type="number" id="cMaxA" min="1" max="8" value="${cfg.max_ambientes}" title="Lugares diferentes nas imagens"></div></div>
        <label class="inline"><input type="checkbox" id="cHuman" ${cfg.humanizer ? 'checked' : ''}> passar o texto pelo humanizer-br no fim (só português)</label>
      </div>
    </section>

    <section data-secao="juiz"><div class="panel">
      <div class="small">Sempre avaliadas: ${Object.values(presets.universais).map(q => esc(q.pergunta.split('?')[0]) + '?').join(' · ')}</div>
      <label>Perguntas próprias do canal</label>
      <table id="cPerg"><tr><th>chave</th><th>tipo</th><th>pergunta</th><th>corte</th><th></th></tr>
      ${Object.entries(cfg.juiz.perguntas || {}).map(([k, q]) => linhaPergunta(k, q)).join('')}</table>
      <button class="small" id="cAddP" style="margin-top:6px">+ pergunta</button>
      <div class="small" style="margin-top:6px">A pergunta "gancho" escolhe entre os 5 ganchos de cada rodada. Depois disso o gancho fica fixo e o Jev julga só o resto da história.</div>
      <div class="small" style="margin-top:6px">score: nota 0 a 10 (corte típico 6 a 8). noul: probabilidade de sim, 0 a 1 (corte típico 0,80 a 0,90). Uma chave igual à de uma universal (ex.: gancho) muda o corte dela.</div>
    </div></section>

    <section data-secao="visual"><div class="panel">
      <label>Estilo</label><select id="cEstiloPreset"><option value="">(personalizado)</option>${Object.entries(presets.estilos).map(([k, v]) => `<option value="${k}">${esc(v.nome)}</option>`).join('')}</select>
      <label>Nome do estilo</label><input type="text" id="cEstiloNome" value="${esc(cfg.estilo.nome)}">
      <label>Prefixo: vai antes da cena (inglês; o gerador dá mais peso ao começo)</label><textarea id="cEstiloPrefixo" style="min-height:90px">${esc(cfg.estilo.prefixo || '')}</textarea>
      <label>Sufixo: vai depois da cena</label><textarea id="cEstiloSufixo" style="min-height:90px">${esc(cfg.estilo.sufixo)}</textarea>
      <div class="small">Escreva <code>[ELEMENTO EM VERMELHO]</code> no prefixo ou no sufixo e o roteirista escolhe, em cada cena, o objeto que entra no lugar.</div>
      <label class="inline"><input type="checkbox" id="cReaproveitar" ${cfg.reaproveitar_biblioteca ? 'checked' : ''}> reaproveitar personagens e ambientes da biblioteca (só do mesmo estilo)</label>
      <div class="small">Desligado: cada vídeo cria fichas e placas novas, e todas ficam guardadas na biblioteca.</div>
      <label>Resolução base das imagens</label><select id="cRes"><option value="1080" ${cfg.resolucao_base == 1080 ? 'selected' : ''}>1080</option><option value="720" ${cfg.resolucao_base == 720 ? 'selected' : ''}>720</option></select>
      <h3>Efeitos por tensão</h3>
      <table><tr><th>tensão</th><th>movimento</th><th>sobreposição</th></tr>${[1, 2, 3, 4, 5].map(t => `<tr><td>${t}</td>
        <td><select data-mov="${t}">${presets.efeitos.filter(e => e.tipo === 'movimento').map(e => `<option ${cfg.efeitos.por_tensao[t] === e.nome ? 'selected' : ''}>${e.nome}</option>`).join('')}</select></td>
        <td><select data-ext="${t}"><option value="">(nenhum)</option>${presets.efeitos.filter(e => e.tipo === 'sobreposicao').map(e => `<option ${(cfg.efeitos.extras_por_tensao || {})[t] === e.nome ? 'selected' : ''}>${e.nome}</option>`).join('')}</select></td></tr>`).join('')}</table>
      <label>Transição</label><select id="cTrans"><option value="escurecer" ${cfg.efeitos.transicao === 'escurecer' ? 'selected' : ''}>escurecer (0,25 s)</option><option value="corte_seco" ${cfg.efeitos.transicao === 'corte_seco' ? 'selected' : ''}>corte seco</option></select>
    </div>
    ${id && c.biblioteca.length ? `<div class="panel"><h3 style="margin-top:0">Biblioteca (reaproveitada entre vídeos)</h3><div class="row" style="align-items:flex-start">
      ${c.biblioteca.map(b => `<div class="fix" style="max-width:200px"><img data-zoom src="/arquivos/${esc(b.arquivo)}" style="max-width:100%;max-height:130px;border-radius:6px"><div class="small"><b>${esc(b.tipo)}: ${esc(b.chave)}</b><br>${esc(b.descricao_fixa).slice(0, 120)}</div></div>`).join('')}</div></div>` : ''}
    </section>

    <section data-secao="voz"><div class="panel">
      <div class="row"><div class="fix" style="min-width:150px"><label>Serviço</label><select id="cProv">
          <option value="qwen" ${cfg.voz.provedor === 'qwen' ? 'selected' : ''}>Qwen TTS (OpenRouter)</option>
          <option value="edge" ${cfg.voz.provedor !== 'qwen' ? 'selected' : ''}>Edge TTS (grátis)</option></select></div>
        <div data-qwen><label>Modelo</label><select id="cModeloVoz">${Object.entries(presets.modelos_qwen).map(([k, v]) => `<option value="${k}" ${cfg.voz.modelo === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div></div>
      <div class="row"><div><label>Voz</label><input type="text" id="cVoz" list="cVozLista" value="${esc(cfg.voz.nome)}"><datalist id="cVozLista"></datalist></div>
        <div class="fix" style="width:90px"><label>Velocidade</label><input type="text" id="cTaxa" value="${esc(cfg.voz.taxa)}"></div>
        <div class="fix" style="width:90px" data-edge><label>Tom</label><input type="text" id="cTom" value="${esc(cfg.voz.tom)}"></div>
        <button class="fix" id="cOuvir">Ouvir</button></div>
      <div data-instr><label>Como falar (instrução para o Qwen)</label><input type="text" id="cInstr" value="${esc(cfg.voz.instrucoes || '')}" placeholder="ex.: voz grave e calma, ritmo lento, clima de suspense">
        <label>A mais nas cenas de tensão 4 e 5</label><input type="text" id="cInstrAlta" value="${esc(cfg.voz.instrucoes_tensao_alta || '')}" placeholder="ex.: mais tenso, quase sussurrando"></div>
      <div class="small" data-qwen style="margin-top:6px">O Qwen gera a narração cena por cena, então cada imagem entra exatamente quando a fala dela começa. Velocidade no formato -6% ou +5%. A prévia custa cerca de US$ 0,0015 no OpenRouter.</div>
      <audio id="cAudio" controls style="width:100%;margin-top:8px;display:none"></audio>
      <h3>Legenda</h3>
      <div class="row"><div><label>Modelo</label><select id="cLegPreset">${Object.keys(presets.legendas).map(k => `<option ${cfg.legenda.preset === k ? 'selected' : ''}>${k}</option>`).join('')}</select></div>
        <div><label>Fonte</label><input type="text" id="cLegFonte" value="${esc(cfg.legenda.fonte || '')}"></div>
        <div class="fix" style="width:80px"><label>Tamanho</label><input type="number" id="cLegTam" value="${cfg.legenda.tamanho || 80}"></div></div>
      <div class="row"><div><label>Cor</label><input type="color" id="cLegCor" value="${cfg.legenda.cor || '#ffffff'}"></div>
        <div><label>Destaque</label><input type="color" id="cLegDest" value="${cfg.legenda.destaque || '#ffd400'}"></div>
        <div><label>Contorno</label><input type="number" id="cLegCont" value="${cfg.legenda.contorno ?? 6}"></div>
        <div><label>Posição</label><select id="cLegPos"><option value="centro" ${cfg.legenda.posicao === 'centro' ? 'selected' : ''}>centro</option><option value="baixo" ${cfg.legenda.posicao === 'baixo' ? 'selected' : ''}>baixo</option></select></div></div>
      <div class="row"><div><label>Palavras mín.</label><input type="number" id="cLegMin" value="${cfg.legenda.palavras_min ?? 2}"></div>
        <div><label>Palavras máx.</label><input type="number" id="cLegMax" value="${cfg.legenda.palavras_max ?? 4}"></div>
        <label class="inline"><input type="checkbox" id="cLegMai" ${cfg.legenda.maiusculas ? 'checked' : ''}> maiúsculas</label>
        <label class="inline"><input type="checkbox" id="cLegKar" ${cfg.legenda.karaoke ? 'checked' : ''}> karaokê</label></div>
    </div></section>

    <section data-secao="trilha"><div class="panel">
      <div class="grid2"><div>
        <label>Tipo de trilha</label><input type="text" id="cHumor" value="${esc(cfg.musica.humor || '')}" placeholder="ex.: suspense sombrio, drone grave, sem batida">
        <label>Buscas sugeridas para a Biblioteca de Áudio do YouTube</label><textarea id="cSugTri">${esc((cfg.sugestoes_trilha || []).join('\n'))}</textarea>
        <div class="row"><div><label>Música sozinha (dB)</label><input type="number" step="0.5" id="cMusSolo" value="${cfg.musica.volume_solo_db}"></div>
          <div><label>Sob a voz (dB)</label><input type="number" step="0.5" id="cMusSob" value="${cfg.musica.volume_sob_voz_db}"></div>
          <div><label>Rampa (s)</label><input type="number" step="0.05" id="cMusRampa" value="${cfg.musica.rampa_s}"></div></div>
        <div class="row"><div><label>Só música no início (s)</label><input type="number" step="0.5" id="cMusIntro" value="${cfg.musica.intro_s}"></div>
          <div><label>Só música no fim (s)</label><input type="number" step="0.5" id="cMusCauda" value="${cfg.musica.cauda_s}"></div><div></div></div>
        <div class="small" style="margin-top:6px">Voz e música são normalizadas para o mesmo volume antes da mistura; os valores acima são relativos à voz. A música baixa sozinha quando o narrador fala e volta quando ele para.</div>
      </div><div>
        <label>Arquivos do canal</label>
        ${id ? `${(c.trilhas || []).map(t => `<div class="row" style="align-items:center;margin-bottom:6px"><label class="inline fix"><input type="checkbox" data-tri="${t.id}" ${t.ativa ? 'checked' : ''}> ativa</label>
          <div>${esc(t.nome)}<audio controls preload="none" src="/arquivos/${esc(t.arquivo)}" style="width:100%;height:32px"></audio></div><button class="fix small" data-deltri="${t.id}">remover</button></div>`).join('') || '<p class="small">Nenhuma trilha ainda.</p>'}
          <input type="file" id="cTriArq" accept="audio/*" multiple style="margin-top:8px">`
        : '<p class="small">Salve o canal primeiro para enviar trilhas.</p>'}
      </div></div>
    </div></section>

    <section data-secao="geral"><div class="panel">
      <div class="row"><div><label>Orçamento por vídeo (US$)</label><input type="number" step="0.01" id="cOrc" value="${cfg.orcamento_usd}"></div>
        <div><label>Teto do laço da história (US$)</label><input type="number" step="0.005" id="cTetoLaco" value="${cfg.teto_laco_usd ?? ''}" placeholder="${statusSis ? statusSis.teto_laco : '0.01'} (padrão)"></div>
        <div><label>Canal no Publicador</label><input type="text" id="cPubCanal" value="${esc(cfg.publicador_canal || '')}" placeholder="nome do canal lá"></div></div>
      <div class="small" style="margin-top:8px">O laço (ganchos, escrita, Jev e reescritas) para quando gasta o teto e fica com a melhor versão. O orçamento por vídeo inclui o laço, as imagens e a narração.</div>
    </div></section>`;
    mostrarSecao();
    ligar();
  };
  const mostrarSecao = () => {
    $$('#cTabs button').forEach(b => b.classList.toggle('on', b.dataset.sec === secao));
    $$('[data-secao]').forEach(s => s.hidden = s.dataset.secao !== secao);
    $$('[data-secao]:not([hidden]) textarea[rows]').forEach(autoAltura);
  };
  const autoAltura = t => { t.style.height = 'auto'; t.style.height = t.scrollHeight + 2 + 'px'; };
  const renumerar = () => $$('#cRegras .num').forEach((n, i) => n.textContent = i + 1);
  const linhaPergunta = (k, q) => `<tr><td style="width:170px"><input type="text" data-p="chave" value="${esc(k)}"></td>
    <td style="width:90px"><select data-p="tipo"><option ${q.tipo === 'score' ? 'selected' : ''}>score</option><option ${q.tipo === 'noul' ? 'selected' : ''}>noul</option></select></td>
    <td><textarea data-p="pergunta" rows="1" style="min-height:36px;resize:none;overflow:hidden">${esc(q.pergunta)}</textarea><label class="inline"><input type="checkbox" data-p="trava" ${q.trava ? 'checked' : ''}> trava (falhou = reprova)</label></td>
    <td style="width:80px"><input type="number" step="0.01" data-p="corte" value="${q.corte}"></td><td><button class="small" data-delp>×</button></td></tr>`;
  const carregarVozes = async () => {
    const prov = $('#cProv').value;
    $$('[data-qwen]').forEach(e => e.style.display = prov === 'qwen' ? '' : 'none');
    $$('[data-edge]').forEach(e => e.style.display = prov === 'edge' ? '' : 'none');
    $$('[data-instr]').forEach(e => e.style.display = prov === 'qwen' ? '' : 'none');
    const vozes = await api(`/api/vozes?provedor=${prov}&modelo=${encodeURIComponent($('#cModeloVoz').value)}&idioma=${encodeURIComponent($('#cIdioma').value)}`);
    $('#cVozLista').innerHTML = vozes.map(v => `<option value="${esc(v.nome)}">${v.genero === 'Male' ? 'masc.' : 'fem.'}${v.estilo ? ' · ' + esc(v.estilo) : ''}</option>`).join('');
    if (!vozes.some(v => v.nome === $('#cVoz').value) && vozes.length) $('#cVoz').value = vozes[0].nome;
  };
  const vozAtual = () => ({...cfg.voz, provedor: $('#cProv').value, modelo: $('#cModeloVoz').value, nome: $('#cVoz').value.trim(),
    taxa: $('#cTaxa').value, tom: $('#cTom').value, instrucoes: $('#cInstr').value, instrucoes_tensao_alta: $('#cInstrAlta').value});
  const coletar = () => {
    const perguntas = {};
    $$('#cPerg tr').slice(1).forEach(tr => {
      const g = k => $(`[data-p=${k}]`, tr);
      const chave = g('chave').value.trim().toLowerCase().replace(/[^a-z0-9_]+/g, '_');
      if (chave && g('pergunta').value.trim()) perguntas[chave] = {tipo: g('tipo').value, pergunta: g('pergunta').value.trim(), corte: +g('corte').value, trava: g('trava').checked};
    });
    const pt = {}, ex = {};
    $$('[data-mov]').forEach(s => pt[s.dataset.mov] = s.value);
    $$('[data-ext]').forEach(s => { if (s.value) ex[s.dataset.ext] = s.value; });
    const num = v => v === '' ? null : +v;
    return {
      nome: $('#cNome').value.trim(), idioma: $('#cIdioma').value, formato: $('#cFormato').value, descricao: $('#cDesc').value.trim(),
      config: {
        ...cfg,  // mantém o que a tela não edita (ex.: modos_narrativos)
        regras_escrita: $$('[data-regra]').map(t => t.value.trim()).filter(Boolean),
        modos_narrativos: $$('#cModos .modo').map(d => ({nome: $('[data-modo-nome]', d).value.trim(), regra: $('[data-modo-regra]', d).value.trim()})).filter(m => m.nome && m.regra),
        estrutura: $('#cEstrutura').value.split(',').map(s => s.trim()).filter(Boolean),
        max_personagens: +$('#cMaxP').value, max_ambientes: +$('#cMaxA').value,
        palavras_min: num($('#cPMin').value), palavras_max: num($('#cPMax').value),
        humanizer: $('#cHuman').checked, orcamento_usd: +$('#cOrc').value, publicador_canal: $('#cPubCanal').value.trim(), teto_laco_usd: num($('#cTetoLaco').value),
        juiz: {perguntas},
        estilo: {nome: $('#cEstiloNome').value, prefixo: $('#cEstiloPrefixo').value.trim(), sufixo: $('#cEstiloSufixo').value.trim()},
        reaproveitar_biblioteca: $('#cReaproveitar').checked,
        resolucao_base: +$('#cRes').value,
        efeitos: {...cfg.efeitos, por_tensao: pt, extras_por_tensao: ex, transicao: $('#cTrans').value},
        voz: vozAtual(),
        legenda: {...cfg.legenda, preset: $('#cLegPreset').value, fonte: $('#cLegFonte').value, tamanho: +$('#cLegTam').value, cor: $('#cLegCor').value,
          destaque: $('#cLegDest').value, contorno: +$('#cLegCont').value, posicao: $('#cLegPos').value, palavras_min: +$('#cLegMin').value,
          palavras_max: +$('#cLegMax').value, maiusculas: $('#cLegMai').checked, karaoke: $('#cLegKar').checked,
          desligada: $('#cLegPreset').value === 'nenhuma'},
        musica: {humor: $('#cHumor').value, volume_solo_db: +$('#cMusSolo').value, volume_sob_voz_db: +$('#cMusSob').value,
          rampa_s: +$('#cMusRampa').value, intro_s: +$('#cMusIntro').value, cauda_s: +$('#cMusCauda').value},
        sugestoes_trilha: $('#cSugTri').value.split('\n').map(s => s.trim()).filter(Boolean),
      },
    };
  };
  const ligar = () => {
    $$('#cTabs button').forEach(b => b.onclick = () => { secao = b.dataset.sec; mostrarSecao(); });
    const ligarListas = () => {
      $$('textarea[rows]').forEach(t => t.oninput = () => autoAltura(t));
      $$('[data-delr]').forEach(b => b.onclick = () => { b.closest('.regra').remove(); renumerar(); });
      $$('[data-delm]').forEach(b => b.onclick = () => b.closest('.modo').remove());
    };
    ligarListas();
    $('#cAddR').onclick = () => { $('#cRegras').insertAdjacentHTML('beforeend', linhaRegra('', $$('[data-regra]').length)); ligarListas(); $$('[data-regra]').at(-1).focus(); };
    $('#cAddM').onclick = () => { $('#cModos').insertAdjacentHTML('beforeend', linhaModo({nome: '', regra: ''})); ligarListas(); $$('[data-modo-nome]').at(-1).focus(); };
    $('#cVerPrompt').onclick = async () => {
      const r = await acao(() => api('/api/canais/previa-prompt', {method: 'POST', body: coletar()}));
      const bloco = (titulo, msgs) => `<h3>${esc(titulo)}</h3>` + msgs.map(m => `<div class="small">${m.role === 'system' ? 'instruções fixas (system)' : 'pedido do vídeo (user)'}</div><pre class="prompt">${esc(m.content)}</pre>`).join('');
      const d = document.createElement('div'); d.className = 'lightbox';
      d.innerHTML = `<div class="modal"><div class="row" style="align-items:center"><h2 style="margin:0">O que cada modelo recebe</h2><button class="fix" data-fechar>Fechar</button></div>
        ${bloco('1. Ganchos · ' + r.modelo + ' (5 por rodada; o Jev escolhe)', r.gancho)}
        ${bloco('2. História · ' + r.modelo + ' (escrita; as reescritas usam as mesmas instruções fixas)', r.roteirista)}</div>`;
      d.onclick = e => { if (e.target === d || e.target.dataset.fechar !== undefined) d.remove(); };
      document.body.append(d);
    };
    carregarVozes().catch(e => toast('Não consegui listar as vozes: ' + e.message, true));
    $('#cProv').onchange = $('#cModeloVoz').onchange = () => carregarVozes();
    $('#cAddP').onclick = () => { $('#cPerg').insertAdjacentHTML('beforeend', linhaPergunta('', {tipo: 'score', pergunta: '', corte: 7})); ligarDel(); ligarListas(); };
    const ligarDel = () => $$('[data-delp]').forEach(b => b.onclick = () => b.closest('tr').remove());
    ligarDel();
    $('#cEstiloPreset').onchange = e => { const s = presets.estilos[e.target.value]; if (s) { $('#cEstiloNome').value = s.nome; $('#cEstiloPrefixo').value = s.prefixo || ''; $('#cEstiloSufixo').value = s.sufixo; } };
    $('#cLegPreset').onchange = e => {
      const l = presets.legendas[e.target.value]; if (!l || l.desligada) return;
      $('#cLegFonte').value = l.fonte; $('#cLegTam').value = l.tamanho; $('#cLegCor').value = l.cor; $('#cLegDest').value = l.destaque;
      $('#cLegCont').value = l.contorno; $('#cLegPos').value = l.posicao; $('#cLegMin').value = l.palavras_min; $('#cLegMax').value = l.palavras_max;
      $('#cLegMai').checked = l.maiusculas; $('#cLegKar').checked = l.karaoke;
    };
    $('#cOuvir').onclick = async () => {
      const b = $('#cOuvir'); b.disabled = true;
      try {
        const blob = await acao(() => api('/api/vozes/previa', {method: 'POST', body: {voz: vozAtual()}}));
        const a = $('#cAudio'); a.style.display = ''; a.src = URL.createObjectURL(blob); a.play();
      } finally { b.disabled = false; }
    };
    $$('[data-tri]').forEach(cb => cb.onchange = () => acao(() => api('/api/trilhas/' + cb.dataset.tri, {method: 'PATCH', body: {ativa: cb.checked}})));
    $$('[data-deltri]').forEach(b => b.onclick = async () => { if (!confirm('Remover esta trilha do canal?')) return; await acao(() => api('/api/trilhas/' + b.dataset.deltri, {method: 'DELETE'})); telaCanal(id); });
    const arq = $('#cTriArq');
    if (arq) arq.onchange = async () => {
      for (const f of arq.files) { const fd = new FormData(); fd.append('arquivo', f); await acao(() => api(`/api/canais/${id}/trilhas`, {method: 'POST', body: fd}), `Trilha ${f.name} enviada.`); }
      telaCanal(id);
    };
  };
  corpo();
  $('#cIdioma').onchange = () => carregarVozes();
  $('#cSugerir').onclick = async () => {
    const nome = $('#cNome').value.trim(), descricao = $('#cDesc').value.trim();
    if (!nome || !descricao) return toast('Preencha nome e descrição do canal.', true);
    if (!confirm('Substituir os campos de escrita, juiz, visual, voz, efeitos e trilha pela sugestão da IA?')) return;
    const b = $('#cSugerir'); b.disabled = true; b.textContent = 'Pensando...';
    try {
      Object.assign(c, collectKeep(), {nome, descricao});
      const s = await acao(() => api('/api/canais/sugerir', {method: 'POST', body: {nome, descricao, idioma: $('#cIdioma').value, formato: $('#cFormato').value}}), 'Sugestão carregada. Revise e salve.');
      for (const [k, v] of Object.entries(s)) cfg[k] = (v && typeof v === 'object' && !Array.isArray(v) && cfg[k] && typeof cfg[k] === 'object') ? {...cfg[k], ...v} : v;
      cfg.juiz = {perguntas: s.juiz.perguntas};
      corpo();
    } finally { b.disabled = false; b.textContent = 'Sugerir configuração com IA'; }
  };
  const collectKeep = () => { const d = coletar(); Object.assign(cfg, d.config); return {idioma: d.idioma, formato: d.formato}; };
  $('#cSalvar').onclick = async () => {
    const d = coletar();
    if (!d.nome) return toast('O canal precisa de nome.', true);
    const r = await acao(() => id ? api('/api/canais/' + id, {method: 'PUT', body: d}) : api('/api/canais', {method: 'POST', body: d}), 'Canal salvo.');
    if (!id) location.hash = '#/canal/' + r.id; else telaCanal(id);
  };
}

/* ------------------------------------------------------------------ custos */
async function telaCustos() {
  const c = await api('/api/custos');
  app.innerHTML = `<div class="panel"><h2>Custos: ${usd(c.total)}</h2><table><tr><th>Serviço</th><th>Modelo</th><th>Etapa</th><th>Chamadas</th><th>Valor</th><th></th></tr>
  ${c.por_servico.map(l => `<tr><td>${esc(l.servico)}</td><td class="small">${esc(l.modelo)}</td><td>${esc(l.etapa)}</td><td>${l.chamadas}</td><td>${usd(l.valor)}</td><td class="small">${l.todos_reais ? 'real' : 'estimado'}</td></tr>`).join('')}</table>
  <p class="small">Calibração: depois de 15 a 20 shorts, compare as notas do gancho (arquivo avaliacoes.jsonl de cada projeto) com a retenção no YouTube Studio e ajuste os cortes no canal.</p></div>`;
}

carregarSistema().catch(() => {});
rota();
