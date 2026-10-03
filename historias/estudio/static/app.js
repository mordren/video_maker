/* Estúdio de Histórias: interface. Roteamento por hash, sem framework. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rotaAtiva = hash => (location.hash || '#/') === hash;
// pessoas da cena: [{id, onde, faz}] (projetos antigos guardavam só o id)
const quem = c => c.pessoas || (c.personagens || (c.personagem ? [c.personagem] : [])).map(id => ({id, onde: '', faz: ''}));
const usd = v => 'US$ ' + Number(v || 0).toFixed(4);
const app = $('#app');
let timer = null, presets = null, statusSis = null;
// Página única do Estúdio (porta 8090): este app divide a página com o Cortador e ganha as partes dele em
// Canais, Custos e Configurações. Sozinho (python main.py), nada muda.
const UNIFICADO = !!window.ESTUDIO_UNIFICADO;
const REINICIAR = UNIFICADO ? 'reinicie o serviço de Histórias (videomaker-historias)' : 'reinicie o servidor (Ctrl+C e python main.py)';

async function api(url, opts = {}) {
  const o = {...opts, headers: {...(opts.headers || {})}};
  if (o.body && !(o.body instanceof FormData)) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.body); }
  const r = await fetch(url, o);
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); msg = j.detail || j.erro || msg; } catch {}
    // 405 numa rota que a página conhece = o servidor ainda roda o código antigo.
    if (r.status === 405) msg = `O servidor está rodando uma versão antiga do programa: ${REINICIAR} e tente de novo.`;
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
  if (UNIFICADO && !location.hash) { location.replace('#/cortador'); return; }
  const h = location.hash.replace(/^#/, '') || '/';
  const [, seg, id] = h.split('/');
  const NAV = {'': 'projetos', projeto: 'projetos', canal: 'canais', 'canal-pub': 'canais', ...(UNIFICADO ? {desempenho: 'projetos'} : {})};
  $$('nav a').forEach(a => a.classList.toggle('on', a.dataset.nav === (NAV[(seg || '').split('?')[0]] || seg)));
  if (UNIFICADO) {
    window.scrollTo(0, 0);   // a outra aba podia estar rolada até o fim
    const cortador = seg === 'cortador';
    $('#cortador').hidden = !cortador;
    app.hidden = cortador;
    if (cortador) { window.cortador.ativar(); return; }
    window.cortador.desativar();
  }
  app.innerHTML = '<div class="spin"></div>';
  try {
    // A tela de um canal de publicação é do Cortador: abre mesmo com o serviço de Histórias fora do ar.
    if (seg === 'canal-pub') return await telaCanalPub(decodeURIComponent(id || ''));
    if (!presets && !(UNIFICADO && ['canais', 'custos', 'configuracoes', 'teste-comfy'].includes(seg))) presets = await api('/api/presets');
    if (!seg) await telaProjetos();
    else if (seg === 'projeto') await telaProjeto(+id);
    else if (seg === 'canais') await telaCanais();
    else if (seg === 'canal') await telaCanal(id === 'novo' ? null : +id);
    else if (seg === 'custos') await telaCustos();
    else if (seg === 'gerador') await telaGerador();
    else if (seg === 'teste-comfy') await telaTesteComfy();
    else if (seg === 'configuracoes') await telaConfiguracoes();
    else if (seg && seg.startsWith('desempenho')) await telaDesempenho();
    else app.innerHTML = '<p>Página não encontrada.</p>';
  } catch (e) { app.innerHTML = `<div class="panel bad">${esc(e.message)}</div>`; }
}
window.addEventListener('hashchange', rota);

async function carregarSistema() {
  try { statusSis = await api('/api/status'); }
  catch (e) {
    if (UNIFICADO) $('#sistema').innerHTML = `<span class="badge err" title="${esc(e.message)}">Histórias fora do ar — o Cortador segue funcionando</span>`;
    throw e;
  }
  // Servidor sem janela (Linux): "Escolher…" e "Abrir pasta" do Music não têm onde abrir.
  document.body.classList.toggle('sem-janela', statusSis.windows === false);
  const faltam = [];
  if (!statusSis.chaves.openrouter) faltam.push('OPENROUTER_API_KEY');
  if (!statusSis.chaves.pollinations) faltam.push('POLLINATIONS_API_KEY');
  const rotulo = UNIFICADO ? 'Histórias: ' : '';
  $('#sistema').innerHTML = (statusSis.desatualizado ? `<span class="badge err" title="Algum arquivo .py mudou depois que o servidor subiu">código novo: ${REINICIAR}</span> ` : '') +
    (statusSis.simulacao ? '<span class="badge warn">modo simulação (nada é pago)</span>'
    : faltam.length ? `<span class="badge err">${rotulo}falta no .env: ${faltam.join(', ')}</span>`
    : `<span class="small">${rotulo}${esc(statusSis.modelos.texto)} · ${esc(statusSis.modelos.juiz)} · ${esc(statusSis.modelos.imagem)}</span>`);
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
    <h2>Vídeos${UNIFICADO ? ' <a class="small" href="#/desempenho" style="margin-left:10px;font-weight:400">desempenho no YouTube →</a>' : ''}</h2>
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
  ${p.ressalva ? `<div class="panel ${p.ressalva.startsWith('REPROVADA') ? 'bad' : 'alert'}"><b>${p.ressalva.startsWith('REPROVADA') ? 'Reprovada no juiz' : 'Aprovada com ressalva'}:</b> ${esc(p.ressalva)}. Veja as notas em Avaliações e decida se segue.</div>` : ''}
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
        <div class="small">${quem(c).map(p => `<b>${esc(nomes[p.id] || p.id)}</b>${p.onde ? ' ' + esc(p.onde) : ''}${p.faz ? ': ' + esc(p.faz) : ''}<br>`).join('')}${c.ambiente ? 'ambiente: ' + esc(c.ambiente) : ''}</div></td>
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

/* Acompanha o Refazer de uma cena: consulta /cenas/{n}/status, mostra estado e segundos, e confere se o arquivo mudou. */
const vigias = {};
const ROTULO = {aguardando: 'Na fila, aguardando começar', gerando: 'Gerando imagem', pronto: 'Pronto', erro: 'Erro'};
function textoVigia(n) {
  const v = vigias[n]; if (!v) return '';
  const seg = Math.round((Date.now() - v.t0) / 1000);
  return `${ROTULO[v.estado] || 'Enviando pedido'}... ${seg}s` + (v.fila > 0 && v.estado === 'aguardando' ? ` (fila: ${v.fila})` : '');
}
function vigiarCena(pid, n) {
  vigias[n] = {t0: Date.now(), estado: 'aguardando', fila: 0};
  const tick = async () => {
    const v = vigias[n]; if (!v) return;
    try {
      const s = await api(`/api/projetos/${pid}/cenas/${n}/status`);
      if (v.estado !== s.estado) console.log(`[refazer] cena ${n}: estado ${v.estado} -> ${s.estado} (${Math.round((Date.now() - v.t0) / 1000)}s)`);
      v.estado = s.estado; v.fila = s.fila;
      const el = document.querySelector(`[data-ov="${n}"] .ovt`); if (el) el.textContent = textoVigia(n);
      if (s.estado === 'pronto' || s.estado === 'erro') {
        console.log(`[refazer] cena ${n}: recebido em ${Math.round((Date.now() - v.t0) / 1000)}s (v${s.versao}, seed ${s.seed}, sha1 ${s.sha1}, ${s.tamanho} bytes)`);
        console.log(`[refazer] cena ${n}: atualizando interface`);
        delete vigias[n];
        if (s.estado === 'erro') toast(`Cena ${n} falhou: ${s.erro}`, true);
        else if (s.imagem_mudou === false) toast(`Cena ${n}: o arquivo NÃO mudou (mesmo hash ${s.sha1}). Veja o Registro.`, true);
        else toast(`Cena ${n} atualizada (v${s.versao}, seed ${s.seed}, ${Math.round(s.segundos)}s).`);
        if (rotaAtiva('#/projeto/' + pid)) telaProjeto(pid);
        return;
      }
    } catch (e) { /* servidor ocupado ou reiniciando: tenta de novo */ }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 500);
  setInterval_ovt(n);
}
// atualiza o contador de segundos a cada segundo, mesmo entre consultas
function setInterval_ovt(n) {
  const i = setInterval(() => {
    if (!vigias[n]) return clearInterval(i);
    const el = document.querySelector(`[data-ov="${n}"] .ovt`); if (el) el.textContent = textoVigia(n);
  }, 1000);
}

function abaImagens(p) {
  const el = $('#aba'), h = p.historia;
  if (!h) { el.innerHTML = '<p class="small">Sem história ainda.</p>'; return; }
  const porN = Object.fromEntries(p.cenas.map(c => [c.n, c]));
  const larga = p.canal.formato !== 'short';
  const podeRefazer = ['aguardando_imagens', 'pronto', 'erro', 'interrompido'].includes(p.status);
  // Opções do combobox de referência da cena: só o que já tem imagem na biblioteca deste projeto
  const opcoesRef = [...(h.personagens || []).filter(x => x.bib_chave).map(x => [`personagem:${x.id}`, 'Ficha: ' + (x.nome || x.id)]),
    ...(h.ambientes || []).filter(x => x.bib_chave).map(x => [`ambiente:${x.id}`, 'Placa: ' + x.id])];
  const linhaRef = (n, sel, ativo) => `<div class="row" data-refl style="align-items:center;gap:6px"><select data-ref="${n}" ${ativo ? '' : 'disabled'}>
    ${opcoesRef.map(([v, r]) => `<option value="${esc(v)}" ${v === sel ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select>
    <button class="small" data-refx="${n}" title="Remover referência" ${ativo ? '' : 'disabled'}>X</button></div>`;
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
    const ov = vigias[c.n] ? `<div class="ov" data-ov="${c.n}"><div class="spin"></div><div class="ovt">${esc(textoVigia(c.n))}</div></div>` : '';
    const img = ov + (s && s.status === 'ok' && s.arquivo ? `<img data-zoom src="/arquivos/${s.arquivo}?v=${s.versao}&t=${t}">`
      : s && s.status === 'gerando' && !ov ? '<div class="spin"></div>' : s && s.status === 'erro' ? `<span class="small fail" style="padding:10px">${esc(s.erro)}</span>` : '<span class="small">aguardando</span>');
    return `<div class="scene ${larga ? 'wide' : ''}"><div class="img">${img}<span class="n">${c.n}</span></div>
      <div class="body"><div>${esc(c.narracao)}</div>
      <textarea data-n="${c.n}" ${podeRefazer ? '' : 'disabled'}>${esc(c.prompt_imagem)}</textarea>
      <input type="text" data-dest="${c.n}" placeholder="destaque (objeto em vermelho)" value="${esc(c.destaque || '')}" ${podeRefazer ? '' : 'disabled'}>
      ${opcoesRef.length ? `<div class="small">Referências desta cena (a 1ª vale mais; vazio = automático)</div>
      <div data-refs="${c.n}">${(c.referencias || []).filter(v => opcoesRef.some(o => o[0] === v)).map(v => linhaRef(c.n, v, podeRefazer)).join('')}</div>
      <button class="small" data-refadd="${c.n}" ${podeRefazer ? '' : 'disabled'}>Adicionar referência</button>` : ''}
      ${c.mesma_imagem_de ? `<div class="small" style="color:var(--acc)">mesma imagem da cena ${c.mesma_imagem_de}, com outro movimento (${esc(c.efeito || 'detalhe')}), sem custo</div>` : ''}
      <div class="small">tensão ${c.tensao} · ${esc(c.efeito || '')}${c.extra ? ' + ' + esc(c.extra) : ''}${s && !c.mesma_imagem_de ? ' · seed ' + s.seed + (s.versao ? ' · v' + s.versao : '') : ''}</div>
      <div class="row"><button class="small" data-refazer="${c.n}" ${podeRefazer ? '' : 'disabled'} title="${c.mesma_imagem_de ? 'Gera uma imagem própria para esta cena' : 'Gera esta imagem de novo'}">${c.mesma_imagem_de ? 'Imagem própria' : 'Refazer'}</button>
      <button class="small" data-testar="${c.n}" ${s && s.status === 'ok' ? '' : 'disabled'}>Testar efeito</button></div></div></div>`;
  }).join('')}</div>`;
  const ligarRemover = box => $$('[data-refx]', box).forEach(x => x.onclick = () => x.closest('[data-refl]').remove());
  $$('[data-refs]', el).forEach(ligarRemover);
  $$('[data-refadd]', el).forEach(b => b.onclick = () => {
    const n = b.dataset.refadd, box = $(`[data-refs="${n}"]`, el);
    if ($$('[data-refl]', box).length >= 2) return alert('No máximo 2 referências por cena.');
    const usadas = $$('select', box).map(x => x.value);
    const livre = opcoesRef.find(o => !usadas.includes(o[0])) || opcoesRef[0];
    box.insertAdjacentHTML('beforeend', linhaRef(n, livre[0], true));
    ligarRemover(box);
  });
  $$('[data-refazer]', el).forEach(b => b.onclick = async () => {
    const n = +b.dataset.refazer, prompt = $(`textarea[data-n="${n}"]`, el).value, destaque = $(`input[data-dest="${n}"]`, el).value;
    const box = $(`[data-refs="${n}"]`, el);
    const referencias = box ? [...new Set($$('select', box).map(x => x.value))].slice(0, 2) : undefined;
    b.disabled = true;
    console.log(`[refazer] cena ${n}: iniciando geração (pedido enviado)`);
    await acao(() => api(`/api/projetos/${p.id}/cenas/${n}/refazer`, {method: 'POST', body: {prompt_imagem: prompt, destaque, referencias}}), `Refazendo a cena ${n}...`);
    vigiarCena(p.id, n);
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
  const [canais, pub] = await Promise.all([
    UNIFICADO ? api('/api/canais').catch(e => ({erro: e.message})) : api('/api/canais'),
    UNIFICADO ? api('/api/estudio/canais').catch(e => ({erro: e.message})) : null]);
  const historias = Array.isArray(canais) ? canais : [];
  app.innerHTML = (UNIFICADO ? blocoCanaisPublicacao(pub, historias) : '') +
  `<div class="row" style="align-items:center"><h2 style="margin:0">${UNIFICADO ? 'Canais de histórias' : 'Canais'}</h2><button class="fix main" onclick="location.hash='#/canal/novo'">Novo canal</button></div>
  <p class="small">Cada canal guarda uma vez só como as histórias são escritas, julgadas pelo Jev, ilustradas, narradas e sonorizadas.${UNIFICADO ? ' O "canal no Publicador" (seção Custos e publicação) diz para qual canal de publicação o vídeo vai.' : ''}</p>
  ${canais.erro ? `<div class="panel bad">${esc(canais.erro)}</div>` : `<div class="cards">${historias.map(c => `<div class="card" data-id="${c.id}"><b>${esc(c.nome)}</b> <span class="badge">${esc(c.idioma)}</span> <span class="badge">${esc(c.formato)}</span>
    <p class="small">${esc(c.descricao)}</p>
    <div class="small">${Object.keys(c.config.juiz.perguntas || {}).length} perguntas próprias no juiz · estilo ${esc(c.config.estilo.nome)} · voz ${esc(c.config.voz.nome)} · ${c.trilhas.length} trilha(s) · ${c.projetos} vídeo(s)${c.config.publicador_canal ? ` · posta em ${esc(c.config.publicador_canal)}` : ''}</div></div>`).join('')}</div>`}`;
  $$('.card[data-id]').forEach(d => d.onclick = () => location.hash = '#/canal/' + d.dataset.id);
  $$('.card[data-pub]').forEach(d => d.onclick = () => location.hash = '#/canal-pub/' + encodeURIComponent(d.dataset.pub));
  const novo = $('#pubNovo');
  if (novo) novo.onclick = () => {
    const nome = (prompt('Nome do canal como está no Publicador (ex.: garras):') || '').trim();
    if (nome) location.hash = '#/canal-pub/' + encodeURIComponent(nome);
  };
}

/* ------------------------------------------------------------------ canais de publicação (página única) */
const normal = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
const linkPublicador = url => String(url || '').replace(/\/\/(127\.0\.0\.1|localhost)(?=[:/]|$)/, '//' + location.hostname);

function blocoCanaisPublicacao(pub, historias) {
  if (!pub || pub.erro) return `<div class="panel bad">Canais de publicação: ${esc(pub ? pub.erro : 'sem resposta')}</div>`;
  const cards = pub.canais.map(c => {
    const p = c.perfil;
    const hist = historias.filter(h => (h.config.publicador_canal || '') === c.nome);
    const confira = p && c.marca && p.nome_exibicao && normal(c.marca) !== normal(p.nome_exibicao);
    const situacao = !c.no_publicador ? '<span class="badge err">fora do Publicador</span>'
      : c.conectado ? '<span class="badge ok">conectado</span>' : '<span class="badge warn">sem login</span>';
    const pubInfo = c.publicacao ? ` · posta a cada ${c.publicacao.intervalo_min}–${c.publicacao.intervalo_max} min${c.publicacao.tiktok_conta ? ` · TikTok ${esc(c.publicacao.tiktok_conta)}` : ''}` : '';
    return `<div class="card" data-pub="${esc(c.nome)}">
      <b>${esc((p && p.nome_exibicao) || c.nome)}</b> <span class="badge">${esc(c.nome)}</span> ${situacao}
      <p class="small">${esc(p ? p.descricao : 'Sem perfil ainda: clique para criar.')}</p>
      ${p ? `<div class="small">${esc(p.nicho)} · ${p.duracao_min_s}–${p.duracao_max_s} s · legenda ${esc(p.legenda_padrao)} · ${p.formatos.map(f => esc(pub.formatos[f] || f)).join(', ')}</div>` : ''}
      <div class="small" style="margin-top:4px">visual: ${esc(c.visual || '(nenhum)')}${c.marca ? ` (assina «${esc(c.marca)}»)` : ''}${confira ? ' <span style="color:var(--warn)" title="O nome no visual é diferente do nome do canal">⚠ confira</span>' : ''}${pubInfo}${hist.length ? ` · histórias: ${hist.map(h => esc(h.nome)).join(', ')}` : ''}</div>
    </div>`;
  }).join('');
  return `<div class="row" style="align-items:center"><h2 style="margin:0">Canais de publicação</h2>
      <button class="fix" id="pubNovo">Perfil para outro canal</button></div>
    <p class="small">Os canais de verdade (YouTube/TikTok), como estão no Publicador${pub.publicador_ok ? '' : ' (<span style="color:var(--err)">o Publicador não respondeu</span>)'}. O perfil diz o que o canal é e como os cortes dele saem; o Cortador usa a legenda padrão, o visual e a duração do perfil.</p>
    <div class="cards" style="margin-bottom:26px">${cards}</div>`;
}

async function telaCanalPub(nome) {
  const d = await api('/api/estudio/canais');
  if (!rotaAtiva('#/canal-pub/' + encodeURIComponent(nome))) return;
  const c = d.canais.find(x => x.nome === nome) || {nome, perfil: null, visual: '', marca: '', publicacao: null, no_publicador: false};
  const p = {...d.padrao, ...(c.perfil || {})};
  let historias = [];
  try { historias = (await api('/api/canais')).filter(h => (h.config.publicador_canal || '') === nome); } catch { /* Histórias fora do ar */ }
  const pubUrl = linkPublicador(d.publicador_url);
  const q = c.publicacao;
  app.innerHTML = `
  <div class="row" style="align-items:center"><h2 style="margin:0">Canal de publicação: ${esc(p.nome_exibicao || nome)} <span class="badge">${esc(nome)}</span></h2>
    <button class="fix" onclick="location.hash='#/canais'">Voltar</button>
    ${c.perfil ? '<button class="fix" id="pApagar">Apagar perfil</button>' : ''}
    <button class="fix main" id="pSalvar">Salvar perfil</button></div>
  ${c.perfil ? '' : '<div class="panel alert small" style="margin-top:12px">Este canal ainda não tem perfil. Preencha e salve.</div>'}
  <div class="panel" style="margin-top:12px">
    <h3 style="margin-top:0">Identidade</h3>
    <div class="row"><div><label>Nome que aparece</label><input type="text" id="pNome" value="${esc(p.nome_exibicao)}" placeholder="${esc(nome)}"></div>
      <div><label>Nicho</label><input type="text" id="pNicho" value="${esc(p.nicho)}"></div></div>
    <label>Sobre o canal</label><textarea id="pDesc">${esc(p.descricao)}</textarea>
    <div class="row"><div><label>Público</label><input type="text" id="pPublico" value="${esc(p.publico)}"></div>
      <div><label>Tom</label><input type="text" id="pTom" value="${esc(p.tom)}"></div></div>
    <label>De onde vêm os cortes</label><input type="text" id="pFontes" value="${esc(p.fontes)}">
  </div>
  <div class="panel">
    <h3 style="margin-top:0">Como os cortes saem</h3>
    <div class="row"><div><label>Duração mínima (s)</label><input type="number" id="pDMin" min="5" max="3600" value="${p.duracao_min_s}"></div>
      <div><label>Duração máxima (s)</label><input type="number" id="pDMax" min="5" max="3600" value="${p.duracao_max_s}"></div>
      <div><label>Legenda padrão</label><select id="pLeg"><option value="new" ${p.legenda_padrao === 'new' ? 'selected' : ''}>new (glow + karaokê)</option><option value="old" ${p.legenda_padrao === 'old' ? 'selected' : ''}>old (amarela)</option></select></div>
      <div><label>Visual (logo e cores)</label><select id="pVisual"><option value="">(não definido)</option>${d.visuais.map(v => `<option value="${esc(v.nome)}" ${v.nome === c.visual ? 'selected' : ''}>${esc(v.nome)}${v.marca ? ' · assina ' + esc(v.marca) : ''}</option>`).join('')}</select></div></div>
    <label>Formatos que combinam com o canal</label>
    <div class="row">${Object.entries(d.formatos).map(([k, v]) => `<label class="inline fix"><input type="checkbox" data-fmt="${k}" ${p.formatos.includes(k) ? 'checked' : ''}> ${esc(v)}</label>`).join('')}</div>
    <label class="inline"><input type="checkbox" id="pLongo" ${p.video_longo ? 'checked' : ''}> aceita vídeo longo (15–20 min)</label>
    <label>Aberturas que seguram</label><textarea id="pAbertura">${esc(p.abertura)}</textarea>
    <label>Como escrever o título</label><textarea id="pTitulo">${esc(p.titulo_estilo)}</textarea>
    <label>Observações</label><textarea id="pObs">${esc(p.observacoes)}</textarea>
    <div class="small" style="margin-top:8px">O Cortador pré-seleciona a legenda e o visual deste canal nas propostas e avisa quando o corte passa da duração máxima. O visual é o mesmo que a produção sempre usou (lembrado por canal).</div>
  </div>
  <div class="panel"><h3 style="margin-top:0">Publicação</h3>
    ${q ? `<p class="small">No Publicador: privacidade <b>${esc(q.privacidade)}</b> · intervalo de ${q.intervalo_min} a ${q.intervalo_max} min${q.ordem_aleatoria ? ' · ordem aleatória' : ''} · TikTok: ${esc(q.tiktok_conta || 'nenhum')}${q.descricao ? ` · descrição: «${esc(q.descricao)}»` : ''}</p>`
      : `<p class="small">${c.no_publicador ? 'Sem configuração de postagem lida.' : 'Este canal não está no Publicador.'}</p>`}
    <p class="small">Intervalo, privacidade, descrição e TikTok se mudam no <a href="${esc(pubUrl)}" target="_blank" rel="noopener">Publicador ↗</a>.</p>
    ${historias.length ? `<p class="small">Canais de história que postam aqui: ${historias.map(h => `<a href="#/canal/${h.id}">${esc(h.nome)}</a>`).join(', ')}</p>` : ''}
  </div>`;
  $('#pSalvar').onclick = async () => {
    const corpo = {nome_exibicao: $('#pNome').value, descricao: $('#pDesc').value, nicho: $('#pNicho').value,
      publico: $('#pPublico').value, tom: $('#pTom').value, fontes: $('#pFontes').value,
      duracao_min_s: +$('#pDMin').value, duracao_max_s: +$('#pDMax').value, legenda_padrao: $('#pLeg').value,
      formatos: $$('[data-fmt]').filter(x => x.checked).map(x => x.dataset.fmt), video_longo: $('#pLongo').checked,
      abertura: $('#pAbertura').value, titulo_estilo: $('#pTitulo').value, observacoes: $('#pObs').value,
      visual: $('#pVisual').value};
    await acao(() => api('/api/estudio/canais/' + encodeURIComponent(nome), {method: 'PUT', body: corpo}), 'Perfil do canal salvo.');
    telaCanalPub(nome);
  };
  const apagar = $('#pApagar');
  if (apagar) apagar.onclick = async () => {
    if (!confirm(`Apagar o perfil de ${nome}? O canal continua no Publicador e o visual continua lembrado.`)) return;
    await acao(() => api('/api/estudio/canais/' + encodeURIComponent(nome), {method: 'DELETE'}), 'Perfil apagado.');
    location.hash = '#/canais';
  };
}

const IDIOMAS = ['pt-BR', 'pt-PT', 'en-US', 'en-GB', 'es-ES', 'es-MX', 'fr-FR', 'de-DE', 'it-IT', 'ja-JP'];

async function telaCanal(id) {
  let recorteEstilo = 0;  // bordas a tirar de cada cena (estilos que desenham moldura); vem do estilo escolhido
  const c = id ? await api('/api/canais/' + id) : {nome: '', idioma: 'pt-BR', descricao: '', formato: 'short', config: structuredClone(presets.padrao), trilhas: [], biblioteca: []};
  const cfg = c.config;
  recorteEstilo = cfg.estilo.recorte || 0;
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
        <div class="row"><div><label>Duração mínima (s)</label><input type="number" id="cDMin" min="5" step="1" placeholder="pelas palavras" value="${cfg.duracao_min_s ?? ''}" title="Segundos de narração; preenchido, vale mais que as palavras"></div>
          <div><label>Duração máxima (s)</label><input type="number" id="cDMax" min="5" step="1" placeholder="pelas palavras" value="${cfg.duracao_max_s ?? ''}" title="Segundos de narração; preenchido, vale mais que as palavras"></div>
          <div><label>Imagens mín.</label><input type="number" id="cIMin" min="1" step="1" placeholder="do formato" value="${cfg.imagens_min ?? ''}" title="Imagens (cenas) por vídeo"></div>
          <div><label>Imagens máx.</label><input type="number" id="cIMax" min="1" step="1" placeholder="do formato" value="${cfg.imagens_max ?? ''}" title="Imagens (cenas) por vídeo"></div></div>
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
      <label>CTA no fim do vídeo (4 ou 5 palavras, dito pelo narrador depois da última frase; vazio = sem CTA)</label>
      <input type="text" id="cCta" value="${esc(cfg.cta || '')}" placeholder="ex.: Siga para mais histórias.">
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
  // score é nota de 0 a 10 e noul é probabilidade de 0 a 1: um corte 7 num noul nunca passaria (e com trava reprova tudo).
  const cortePorTipo = (tipo, c) => tipo === 'noul' && c > 1 ? Math.min(c, 10) / 10 : tipo === 'score' && c > 0 && c <= 1 ? Math.round(c * 100) / 10 : c;
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
      const chave = g('chave').value.trim().toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9_]+/g, '_');
      if (chave && g('pergunta').value.trim()) perguntas[chave] = {tipo: g('tipo').value, pergunta: g('pergunta').value.trim(), corte: cortePorTipo(g('tipo').value, +g('corte').value), trava: g('trava').checked};
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
        duracao_min_s: num($('#cDMin').value), duracao_max_s: num($('#cDMax').value),
        imagens_min: num($('#cIMin').value), imagens_max: num($('#cIMax').value),
        humanizer: $('#cHuman').checked, orcamento_usd: +$('#cOrc').value, publicador_canal: $('#cPubCanal').value.trim(), cta: $('#cCta').value.trim(), teto_laco_usd: num($('#cTetoLaco').value),
        juiz: {perguntas},
        estilo: {nome: $('#cEstiloNome').value, prefixo: $('#cEstiloPrefixo').value.trim(), sufixo: $('#cEstiloSufixo').value.trim(), recorte: recorteEstilo},
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
    const ligarDel = () => {
      $$('[data-delp]').forEach(b => b.onclick = () => b.closest('tr').remove());
      $$('#cPerg [data-p=tipo]').forEach(s => s.onchange = () => { const c = $('[data-p=corte]', s.closest('tr')); c.value = cortePorTipo(s.value, +c.value); });
    };
    ligarDel();
    $('#cEstiloPreset').onchange = e => { const s = presets.estilos[e.target.value]; if (s) { $('#cEstiloNome').value = s.nome; $('#cEstiloPrefixo').value = s.prefixo || ''; $('#cEstiloSufixo').value = s.sufixo; recorteEstilo = s.recorte || 0; } };
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
  const [c, k] = await Promise.all([
    UNIFICADO ? api('/api/custos').catch(e => ({erro: e.message})) : api('/api/custos'),
    UNIFICADO ? api('/api/estudio/custos').catch(e => ({erro: e.message})) : null]);
  if (!rotaAtiva('#/custos')) return;
  const historias = c.erro ? `<div class="panel bad">Histórias: ${esc(c.erro)}</div>`
    : `<div class="panel"><h2>${UNIFICADO ? 'Histórias e Music' : 'Custos'}: ${usd(c.total)}</h2><table><tr><th>Serviço</th><th>Modelo</th><th>Etapa</th><th>Chamadas</th><th>Valor</th><th></th></tr>
  ${c.por_servico.map(l => `<tr><td>${esc(l.servico)}</td><td class="small">${esc(l.modelo)}</td><td>${esc(l.etapa)}</td><td>${l.chamadas}</td><td>${usd(l.valor)}</td><td class="small">${l.todos_reais ? 'real' : 'estimado'}</td></tr>`).join('')}</table>
  <p class="small">${UNIFICADO ? 'O custo de cada vídeo de história está na aba Custos do próprio vídeo. O Music não gasta API (só o computador). ' : ''}Calibração: depois de 15 a 20 shorts, compare as notas do gancho (arquivo avaliacoes.jsonl de cada projeto) com a retenção no YouTube Studio e ajuste os cortes no canal.</p></div>`;
  app.innerHTML = (UNIFICADO ? blocoCustosCortador(k, c) : '') + historias;
}

function blocoCustosCortador(k, h) {
  if (!k || k.erro) return `<div class="panel bad">Cortador: ${esc(k ? k.erro : 'sem resposta')}</div>`;
  const p = k.periodos;
  const quadro = (rotulo, valor, sub = '') => `<div class="panel fix" style="min-width:150px;margin:0"><div class="small">${esc(rotulo)}</div><div style="font-size:20px;font-weight:600">${usd(valor)}</div>${sub ? `<div class="small">${sub}</div>` : ''}</div>`;
  const totalHist = h && !h.erro ? h.total : null;
  const contas = (k.contas || []).map(a => {
    if (a.erro) return `<tr><td>${esc(a.servico)} …${esc(a.final)}</td><td class="small">${esc(a.usado_por.join(', '))}</td><td colspan="4" class="small" style="color:var(--err)">não respondeu (${esc(a.erro)})</td></tr>`;
    if (a.servico === 'deepseek') return `<tr><td>DeepSeek …${esc(a.final)}</td><td class="small">${esc(a.usado_por.join(', '))}</td><td colspan="4">saldo: ${(a.saldos || []).map(s => `<b>${esc(s.total_balance)} ${esc(s.currency)}</b>`).join(' · ') || '?'}</td></tr>`;
    return `<tr><td>OpenRouter …${esc(a.final)}</td><td class="small">${esc(a.usado_por.join(', '))}</td><td>${usd(a.usage_daily)}</td><td>${usd(a.usage_weekly)}</td><td>${usd(a.usage_monthly)}</td>
      <td>${usd(a.usage)}${a.limit_remaining != null ? `<div class="small">restam ${usd(a.limit_remaining)}</div>` : ''}</td></tr>`;
  }).join('');
  const maiorDia = Math.max(0.000001, ...k.por_dia.map(d => d.valor));
  const dias = k.por_dia.map(d => `<div class="row" style="align-items:center;gap:8px;margin:2px 0"><span class="small fix" style="width:78px">${esc(d.dia.slice(5).split('-').reverse().join('/'))}</span>
    <div style="flex:1;background:var(--panel2);border-radius:3px;height:10px"><div style="width:${(100 * d.valor / maiorDia).toFixed(1)}%;height:100%;background:var(--acc);border-radius:3px"></div></div><span class="small fix" style="width:96px;text-align:right">${usd(d.valor)}</span></div>`).join('');
  return `<h2>Custos</h2>
  <div class="row" style="align-items:stretch;margin-bottom:16px">
    ${quadro('Cortador hoje', p.hoje)}${quadro('Cortador 7 dias', p['7 dias'])}${quadro('Cortador 30 dias', p['30 dias'])}
    ${quadro('Cortador total', p.total, k.desde ? 'desde ' + esc(k.desde.slice(0, 10).split('-').reverse().join('/')) : 'ainda sem registro')}
    ${totalHist != null ? quadro('Histórias total', totalHist) : ''}
  </div>
  <div class="panel"><h3 style="margin-top:0">Gasto real das contas</h3>
    <table><tr><th>Chave</th><th>Usada por</th><th>Hoje (UTC)</th><th>Esta semana</th><th>Este mês</th><th>Total</th></tr>${contas || '<tr><td colspan="6" class="small">Nenhuma chave configurada.</td></tr>'}</table>
    <p class="small">Direto da conta (OpenRouter /key e saldo do DeepSeek): inclui o que foi gasto com a mesma chave fora do Estúdio. Atualiza a cada 5 min.</p></div>
  <div class="grid2">
    <div class="panel"><h3 style="margin-top:0">Cortador por modelo</h3>
      ${k.por_modelo.length ? `<table><tr><th>Serviço</th><th>Modelo</th><th>Chamadas</th><th>Valor</th><th></th></tr>${k.por_modelo.map(l => `<tr><td>${esc(l.servico)} <span class="small">${esc(l.tipo)}</span></td><td class="small">${esc(l.modelo)}</td><td>${l.chamadas}</td><td>${usd(l.valor)}</td><td class="small">${l.todos_reais ? 'real' : l.algum_real ? 'parte real' : 'estimado'}</td></tr>`).join('')}</table>` : '<p class="small">Nenhuma chamada registrada ainda: o registro começa com esta versão.</p>'}
      ${k.por_etapa.length ? `<h3>Por etapa</h3><table>${k.por_etapa.map(e => `<tr><td>${esc(e.etapa)}</td><td>${usd(e.valor)}</td></tr>`).join('')}</table>` : ''}
      <p class="small">"real" é o valor devolvido pelo OpenRouter; "estimado" usa os preços da aba Configurações (o DeepSeek não devolve o custo).</p></div>
    <div class="panel"><h3 style="margin-top:0">Cortador por dia (30 dias)</h3>${dias || '<p class="small">Sem gasto registrado.</p>'}
      ${k.por_trabalho.length ? `<h3>Por trabalho</h3><table><tr><th>Vídeo</th><th>Canal</th><th>Chamadas</th><th>Valor</th></tr>${k.por_trabalho.map(t => `<tr><td>${esc(t.titulo)}${t.existe ? '' : ' <span class="small">(apagado)</span>'}<div class="small">${esc(t.id)}</div></td><td class="small">${esc(t.canal)}</td><td>${t.chamadas}</td><td>${usd(t.valor)}</td></tr>`).join('')}</table>` : ''}</div>
  </div>`;
}

/* ------------------------------------------------------------------ gerador de vídeo (playlists lo-fi 16:9) */
let gFontes = null, gSalvarT = null;
const G_POSICOES = [['inf_esq', 'inferior esquerdo'], ['inf_centro', 'inferior, no centro'], ['inf_dir', 'inferior direito'],
  ['sup_esq', 'superior esquerdo'], ['sup_dir', 'superior direito'], ['centro', 'centro']];
const G_AUDIO = /\.(mp3|wav)$/i;
const hms = s => {
  s = Math.max(0, Math.floor((s || 0) + 0.01));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), p = n => String(n).padStart(2, '0');
  return (h ? p(h) + ':' : '') + p(m) + ':' + p(s % 60);
};
const falta = s => s < 60 ? `~${Math.max(5, Math.round(s / 5) * 5)} s` : s < 3600 ? `~${Math.round(s / 60)} min` : `~${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min`;
const tamanhoArq = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
// mesma conta de gerador.linha_do_tempo: o crossfade não passa de metade de uma faixa do meio
function gLinhaTempo(durs, xf) {
  const n = durs.length, soma = durs.reduce((a, b) => a + b, 0);
  if (n < 2) return {xf: 0, soma, total: soma};
  let lim = Math.min(durs[0], durs[n - 1]) - 0.1;
  for (const d of durs.slice(1, -1)) lim = Math.min(lim, d / 2 - 0.05);
  const x = Math.max(0, Math.min(xf, lim));
  return {xf: x, soma, total: soma - x * (n - 1)};
}

async function telaGerador() {
  const [e, fontes] = await Promise.all([api('/api/gerador'), gFontes || api('/api/gerador/fontes')]);
  gFontes = fontes;
  if (!rotaAtiva('#/gerador')) return;
  clearInterval(timer); timer = null;
  const cfg = e.config, tx = cfg.texto, cod = e.codificador;
  let faixas = e.faixas;
  const opt = (pares, atual) => pares.map(([v, r]) => `<option value="${esc(v)}" ${String(v) === String(atual) ? 'selected' : ''}>${esc(r)}</option>`).join('');
  const chk = v => v ? 'checked' : '';
  // linha "Pasta": caminho editável + janela de escolha; a saída é criada se não existir
  const campoPasta = k => `<div class="row pasta"><label class="fix">Pasta</label>
    <input type="text" data-pasta="${k}" value="${esc(e.pastas[k])}" spellcheck="false" title="Digite o caminho ou clique em Escolher. Vazio volta para a pasta padrão ao lado do programa.">
    <button class="fix small" data-escolher="${k}">Escolher…</button></div>
    ${k !== 'saida' && !e.pastas_existem[k] ? '<div class="small aviso">Esta pasta não existe. Escolha outra.</div>' : ''}`;
  app.innerHTML = `<div id="gTela">
  <div class="row" style="align-items:center">
    <div><h2 style="margin:0">Music</h2>
      <div class="small">Junta a playlist e o fundo num vídeo ${esc(e.resolucao.replace('x', '×'))} · ${e.fps} fps pronto para o YouTube ·
        codificador: <span class="badge ${cod.gpu ? 'ok' : ''}" title="${esc(cod.motivo || '')}">${esc(cod.rotulo)}</span>${cod.gpu ? '' : ' <span class="small">(sem GPU NVIDIA neste computador)</span>'}</div></div>
    <div class="fix row" id="gAcoes"><button id="gPrevia" title="Renderiza 10 s do vídeo final com as configurações atuais">Prévia de 10 s</button>
      <button class="main" id="gRender">Renderizar vídeo</button></div>
  </div>
  <div id="gProg" style="margin-top:12px"></div>
  <div class="grid2" id="gCorpo" style="margin-top:12px">
    <div>
      <div class="panel" id="gDrop">
        <div class="row" style="align-items:center"><h3 style="margin:0">Músicas</h3>
          <button class="fix small" id="gCarregar" title="Acrescenta os arquivos da pasta de músicas que ainda não estão na lista">Carregar da pasta</button>
          <button class="fix small" data-abrir="musicas">Abrir pasta</button>
          <button class="fix small" id="gLimpar">Limpar lista</button></div>
        ${campoPasta('musicas')}
        <div class="small" style="margin:6px 0 10px">Arraste arquivos .mp3 ou .wav do Explorer para cá (eles são copiados para a pasta de músicas). Arraste as linhas para mudar a ordem.</div>
        <div id="gFaixas" class="faixas"></div>
        <div id="gTotais" style="margin-top:10px"></div>
      </div>
    </div>
    <div>
      <div class="panel"><h3 style="margin-top:0">Fundo</h3>
        <div class="row"><label class="inline fix"><input type="radio" name="gModo" value="slideshow" ${chk(cfg.modo === 'slideshow')}> slideshow de imagens</label>
          <label class="inline fix"><input type="radio" name="gModo" value="clipe" ${chk(cfg.modo === 'clipe')}> clipe em loop</label></div>
        <div data-modo="slideshow">
          <div class="row" style="align-items:center"><span class="small"><b>${e.imagens.length}</b> imagem(ns) na pasta de imagens</span>
            <button class="fix small" data-abrir="imagens">Abrir pasta</button><button class="fix small" id="gAtualizar">Atualizar</button></div>
          ${campoPasta('imagens')}
          <div class="thumbs">${e.imagens.slice(0, 14).map(n => `<img loading="lazy" src="/api/gerador/miniatura/${encodeURIComponent(n)}" title="${esc(n)}">`).join('')}
            ${e.imagens.length > 14 ? `<span class="small">+${e.imagens.length - 14}</span>` : ''}</div>
          <div class="row"><div><label>Ordem</label><select id="gOrdem">${opt([['nome', 'pelo nome do arquivo'], ['aleatoria', 'aleatória']], cfg.ordem)}</select></div>
            <div><label>Segundos por imagem</label><input type="number" id="gSeg" min="3" step="1" value="${cfg.segundos_imagem}"></div>
            <div><label>Crossfade (s)</label><input type="number" id="gTrans" min="0.3" step="0.1" value="${cfg.transicao_s}"></div></div>
          <div class="row" style="align-items:center"><label class="inline fix"><input type="checkbox" id="gKB" ${chk(cfg.ken_burns)}> zoom lento (Ken Burns)</label>
            <div></div><button class="fix small" id="gSortear" title="A prévia e o vídeo usam a mesma ordem sorteada">Sortear outra ordem</button></div>
        </div>
        <div data-modo="clipe">
          <div class="row" style="align-items:center"><span class="small"><b>${e.clipes.length}</b> vídeo(s) nas pastas de vídeos e de imagens</span>
            <button class="fix small" data-abrir="videos">Abrir pasta de vídeos</button><button class="fix small" id="gAtualizarClipes">Atualizar</button></div>
          ${campoPasta('videos')}
          <label>Clipe</label>
          <div class="row"><select id="gClipe">${e.clipes.length ? opt(e.clipes.map(c => [c.nome, `${c.nome} (${hms(c.duracao)})`]), cfg.clipe) : '<option value="">(nenhum vídeo nas pastas de vídeos e de imagens)</option>'}</select>
            <label class="fix small" style="margin:0"><input type="file" id="gClipeArq" accept="video/*" style="display:none"><span class="badge run" style="cursor:pointer;padding:6px 10px">Enviar clipe</span></label></div>
          <div class="small" style="margin-top:4px">O clipe se repete até o fim do áudio; o som dele é ignorado.</div>
        </div>
        <label>Ajuste a 16:9</label><select id="gAjuste">${opt([['cortar', 'cortar o que sobra (sem distorcer)'], ['preencher', 'imagem inteira sobre fundo desfocado']], cfg.ajuste)}</select>
      </div>
      <div class="panel"><h3 style="margin-top:0">Áudio</h3>
        <div class="row" style="align-items:flex-end"><div class="fix" style="width:210px"><label>Crossfade entre faixas (s)</label><input type="number" id="gXf" min="0" max="15" step="0.5" value="${cfg.crossfade_s}"></div>
          <label class="inline fix"><input type="checkbox" id="gNorm" ${chk(cfg.normalizar)}> normalizar o volume (${e.alvo_lufs} LUFS, a referência do YouTube)</label></div>
      </div>
      <div class="panel"><h3 style="margin-top:0">Nome da música na tela</h3>
        <label class="inline"><input type="checkbox" id="gTxt" ${chk(tx.ligado)}> mostrar o nome da faixa atual (aparece e some com fade a cada troca)</label>
        <div class="row"><div><label>Posição</label><select id="gPos">${opt(G_POSICOES, tx.posicao)}</select></div>
          <div><label>Fonte</label><select id="gFonte">${opt(fontes.map(f => [f.arquivo, f.nome]), tx.fonte)}</select></div></div>
        <div class="row"><div><label>Tamanho</label><input type="number" id="gTam" min="12" max="120" value="${tx.tamanho}"></div>
          <div><label>Cor</label><input type="color" id="gCor" value="${esc(tx.cor)}"></div></div>
        <h3>Efeitos</h3>
        <div class="row"><label class="inline fix"><input type="checkbox" id="gVin" ${chk(cfg.vinheta)}> vinheta</label>
          <label class="inline fix"><input type="checkbox" id="gGrao" ${chk(cfg.grao)}> grão de filme</label>
          <label class="inline fix"><input type="checkbox" id="gChuva" ${chk(cfg.chuva)}> chuva</label></div>
      </div>
      <div class="panel"><h3 style="margin-top:0">Saída</h3>
        ${campoPasta('saida')}
        <div class="row"><div><label>Nome do arquivo</label><input type="text" id="gNome" value="${esc(cfg.nome_saida)}"></div>
          <div class="fix" style="width:150px"><label>Início da prévia (s)</label><input type="number" id="gIniPrev" min="0" step="1" placeholder="automático" value="${cfg.previa_inicio ?? ''}"></div></div>
        <div class="small" style="margin-top:6px">Sai na pasta acima, como <code><span id="gNomeEx"></span></code> (MP4 H.264 + AAC), com o .txt de capítulos ao lado. A pasta é criada se não existir.
          Sem início definido, a prévia mostra a troca da 1ª para a 2ª faixa.</div>
      </div>
    </div>
  </div>
  <div class="panel" id="gSaidas"></div></div>`;

  /* lista de músicas */
  const renderTotais = () => {
    const lt = gLinhaTempo(faixas.map(f => f.duracao), +$('#gXf').value || 0);
    $('#gTotais').innerHTML = faixas.length ? `<b>${faixas.length}</b> faixa(s) · soma ${hms(lt.soma)} · <b>vídeo final ${hms(lt.total)}</b>` +
      (faixas.length > 1 && lt.xf > 0 ? ` <span class="small">(${faixas.length - 1} crossfade(s) de ${lt.xf.toFixed(1)} s)</span>` : '') : '';
  };
  const renderFaixas = () => {
    $('#gFaixas').innerHTML = faixas.length ? faixas.map((f, i) => `<div class="faixa" draggable="true" data-i="${i}">
        <span class="alca" title="Arraste para mudar a ordem">⠿</span><span class="num">${i + 1}</span>
        <div class="nome"><b>${esc(f.titulo)}</b><span class="small">${esc(f.nome)}</span></div>
        <span class="dur">${hms(f.duracao)}</span><button class="small" data-del="${i}" title="Tirar da lista (o arquivo continua na pasta)">×</button></div>`).join('')
      : '<div class="small vazio">Lista vazia. Arraste arquivos .mp3 ou .wav do Explorer para cá ou clique em Carregar da pasta.</div>';
    renderTotais();
  };

  /* configurações: salvas sozinhas a cada mudança e restauradas ao abrir a aba */
  const coletar = () => ({
    ...cfg, modo: $('input[name=gModo]:checked').value, ordem: $('#gOrdem').value,
    segundos_imagem: +$('#gSeg').value, transicao_s: +$('#gTrans').value, ken_burns: $('#gKB').checked, ajuste: $('#gAjuste').value,
    clipe: $('#gClipe').value, crossfade_s: +$('#gXf').value, normalizar: $('#gNorm').checked,
    texto: {ligado: $('#gTxt').checked, posicao: $('#gPos').value, fonte: $('#gFonte').value, tamanho: +$('#gTam').value, cor: $('#gCor').value},
    vinheta: $('#gVin').checked, grao: $('#gGrao').checked, chuva: $('#gChuva').checked,
    nome_saida: $('#gNome').value.trim(), previa_inicio: $('#gIniPrev').value === '' ? null : +$('#gIniPrev').value,
    pastas: Object.fromEntries($$('[data-pasta]').map(i => [i.dataset.pasta, i.value.trim()])),
  });
  const corpo = () => ({config: coletar(), faixas: faixas.map(f => f.nome)});
  const salvarAgora = () => { clearTimeout(gSalvarT); return api('/api/gerador/config', {method: 'PUT', body: corpo()}); };
  const salvar = () => { clearTimeout(gSalvarT); gSalvarT = setTimeout(() => salvarAgora().catch(err => toast('Não consegui salvar as configurações: ' + err.message, true)), 500); };
  const mostrarModo = () => {
    const m = $('input[name=gModo]:checked').value;
    $$('[data-modo]').forEach(d => d.hidden = d.dataset.modo !== m);
    $('#gSortear').style.visibility = $('#gOrdem').value === 'aleatoria' ? '' : 'hidden';
    const n = ($('#gNome').value.trim() || 'lofi_mix').replace(/[<>:"/\\|?*]+/g, '');
    $('#gNomeEx').textContent = `${n}_AAAA-MM-DD_HH-MM.mp4`;
  };
  // os campos de pasta só valem quando o texto fecha (change): salvar a cada letra gravaria caminhos pela metade
  const aoMudar = ev => { if (!ev.target.matches('input,select') || ev.target.type === 'file' || ev.target.dataset.pasta) return; mostrarModo(); renderTotais(); salvar(); };
  $('#gCorpo').addEventListener('input', aoMudar);
  $('#gCorpo').addEventListener('change', aoMudar);
  // pasta nova: salva e recarrega a tela (músicas, miniaturas, clipes e saídas vêm da pasta nova); a lista de músicas antiga não vale mais
  const trocouPasta = async qual => {
    if (qual === 'musicas') faixas = [];
    await salvarAgora().catch(err => toast('Não consegui salvar as configurações: ' + err.message, true));
    telaGerador();
  };
  $('#gCorpo').addEventListener('change', ev => { if (ev.target.dataset.pasta) trocouPasta(ev.target.dataset.pasta); });
  $('#gSortear').onclick = () => { cfg.semente = Math.floor(Math.random() * 1e6) + 1; salvar(); toast('Nova ordem sorteada (vale para a prévia e para o vídeo).'); };

  /* arrastar: arquivos do Explorer entram na lista; linhas da lista mudam de lugar */
  const drop = $('#gDrop'), lista = $('#gFaixas');
  let arrastando = null;
  const limparMarcas = () => $$('.faixa', lista).forEach(r => r.classList.remove('antes', 'depois'));
  const posicao = ev => {
    const r = ev.target.closest('.faixa'); if (!r) return {r: null, i: faixas.length};
    const b = r.getBoundingClientRect(), antes = ev.clientY < b.top + b.height / 2;
    return {r, antes, i: +r.dataset.i + (antes ? 0 : 1)};
  };
  lista.addEventListener('dragstart', ev => {
    const r = ev.target.closest('.faixa'); if (!r) return;
    arrastando = +r.dataset.i; ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/x-faixa', r.dataset.i);
    r.classList.add('arrastando');
  });
  lista.addEventListener('dragend', () => { arrastando = null; $$('.faixa', lista).forEach(r => r.classList.remove('arrastando', 'antes', 'depois')); });
  drop.addEventListener('dragover', ev => {
    if (arrastando === null && [...ev.dataTransfer.types].includes('Files')) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy'; drop.classList.add('drop-on'); return; }
    if (arrastando === null) return;
    ev.preventDefault(); limparMarcas();
    const p = posicao(ev); if (p.r) p.r.classList.add(p.antes ? 'antes' : 'depois');
  });
  drop.addEventListener('dragleave', ev => { if (!drop.contains(ev.relatedTarget)) { drop.classList.remove('drop-on'); limparMarcas(); } });
  drop.addEventListener('drop', ev => {
    ev.preventDefault(); drop.classList.remove('drop-on'); limparMarcas();
    if (arrastando === null) { if (ev.dataTransfer.files.length) enviarMusicas(ev.dataTransfer.files); return; }
    let destino = posicao(ev).i;
    const [item] = faixas.splice(arrastando, 1);
    if (destino > arrastando) destino--;
    faixas.splice(destino, 0, item);
    arrastando = null; renderFaixas(); salvar();
  });
  const enviarMusicas = async arquivos => {
    const ok = [...arquivos].filter(f => G_AUDIO.test(f.name));
    if (ok.length < arquivos.length) toast(`${arquivos.length - ok.length} arquivo(s) ignorado(s): só .mp3 e .wav entram na lista.`, true);
    if (!ok.length) return;
    const fd = new FormData(); ok.forEach(f => fd.append('arquivos', f));
    drop.classList.add('enviando');
    try {
      const r = await acao(() => api('/api/gerador/musicas', {method: 'POST', body: fd}));
      const novas = r.filter(f => !faixas.some(x => x.nome === f.nome));
      faixas.push(...novas); renderFaixas(); salvar();
      toast(`${novas.length} música(s) adicionada(s)${r.length > novas.length ? `; ${r.length - novas.length} já estava(m) na lista` : ''}.`);
    } finally { drop.classList.remove('enviando'); }
  };
  lista.addEventListener('click', ev => {
    const b = ev.target.closest('[data-del]'); if (!b) return;
    faixas.splice(+b.dataset.del, 1); renderFaixas(); salvar();
  });
  $('#gCarregar').onclick = async () => {
    const r = await acao(() => api('/api/gerador'));
    const novas = r.na_pasta.filter(f => !faixas.some(x => x.nome === f.nome));
    faixas.push(...novas); renderFaixas(); salvar();
    toast(novas.length ? `${novas.length} música(s) da pasta adicionada(s).` : `Nenhuma música nova na pasta (${r.pastas.musicas}).`);
  };
  $('#gLimpar').onclick = () => {
    if (!faixas.length || !confirm('Tirar todas as músicas da lista? Os arquivos continuam na pasta de músicas.')) return;
    faixas = []; renderFaixas(); salvar();
  };
  $('#gAtualizar').onclick = $('#gAtualizarClipes').onclick = async () => { await salvarAgora().catch(() => {}); telaGerador(); };
  $('#gClipeArq').onchange = async ev => {
    const f = ev.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('arquivo', f);
    const r = await acao(() => api('/api/gerador/clipes', {method: 'POST', body: fd}), `Clipe ${f.name} enviado.`);
    cfg.clipe = r.nome; await salvarAgora().catch(() => {}); telaGerador();
  };
  $('#gTela').onclick = async ev => {
    const a = ev.target.closest('[data-abrir]');
    if (a) return acao(() => api('/api/gerador/abrir/' + a.dataset.abrir, {method: 'POST'}));
    const s = ev.target.closest('[data-escolher]');
    if (s) {  // a janela do Windows abre no servidor (este computador) e a resposta só volta quando o usuário escolhe ou cancela
      const qual = s.dataset.escolher; s.disabled = true;
      try {
        const r = await acao(() => api('/api/gerador/escolher-pasta/' + qual, {method: 'POST'})).catch(() => null);
        const campo = $(`[data-pasta="${qual}"]`);
        if (r && r.pasta && campo) { campo.value = r.pasta; await trocouPasta(qual); }
      } finally { s.disabled = false; }
      return;
    }
    const v = ev.target.closest('[data-ver]');
    if (v) {
      const d = document.createElement('div'); d.className = 'lightbox';
      d.innerHTML = `<video src="${esc(v.dataset.ver)}" autoplay controls style="max-height:90vh;max-width:94vw"></video>`;
      d.onclick = x => { if (x.target === d) d.remove(); }; document.body.append(d);
    }
  };

  /* renderização: progresso, cancelar e resultado */
  const renderSaidas = s => {
    $('#gSaidas').innerHTML = `<div class="row" style="align-items:center"><h3 style="margin:0">Vídeos na pasta de saída</h3><button class="fix small" data-abrir="saida">Abrir pasta</button></div>` +
      (s.length ? `<table style="margin-top:8px"><tr><th>Arquivo</th><th>Tamanho</th><th>Data</th><th></th></tr>${s.map(x => `<tr><td>${esc(x.nome)}</td>
        <td>${tamanhoArq(x.tamanho)}</td><td class="small">${esc(x.data.replace('T', ' '))}</td>
        <td><button class="small" data-ver="${esc(x.url)}">Assistir</button> <a href="${esc(x.url)}" download>baixar</a>${x.capitulos ? ` · <a href="${esc(x.capitulos)}" target="_blank">capítulos</a>` : ''}</td></tr>`).join('')}</table>`
      : '<p class="small">Nenhum vídeo ainda.</p>');
  };
  const renderProgresso = (t, acabou = false) => {  // acabou: terminou agora (a prévia toca sozinha)
    const el = $('#gProg'); if (!el) return;
    const rodando = !!t && t.estado === 'rodando';
    $('#gPrevia').disabled = $('#gRender').disabled = rodando;
    if (!t) { el.innerHTML = ''; return; }
    const nome = t.tipo === 'previa' ? 'Prévia de 10 s' : 'Vídeo completo';
    const avisos = (t.avisos || []).map(a => `<div class="small" style="color:var(--warn);margin-top:6px">${esc(a)}</div>`).join('');
    const fechar = '<button class="fix small" data-fechar>Fechar</button>';
    if (rodando) {
      const pct = Math.floor(t.progresso * 100);
      el.innerHTML = `<div class="panel"><div class="row" style="align-items:center">
        <div><b>${nome}</b> · ${esc(t.etapa)} ${t.total_etapas ? `<span class="small">(etapa ${t.n_etapa} de ${t.total_etapas})</span>` : ''}</div>
        <span class="fix small"><b style="color:var(--txt)">${pct}%</b> · ${t.restante != null ? 'faltam ' + falta(t.restante) : 'calculando o tempo restante...'} · ${hms(t.decorrido)} decorridos</span>
        <button class="fix" id="gCancelar">Cancelar</button></div>
        <div class="barra"><div style="width:${pct}%"></div></div>${avisos}</div>`;
      $('#gCancelar').onclick = async () => {
        if (t.tipo === 'video' && !confirm('Cancelar a renderização? O que já foi feito se perde.')) return;
        $('#gCancelar').disabled = true;
        await acao(() => api('/api/gerador/cancelar', {method: 'POST'}), 'Cancelando...').catch(() => {});
      };
      return;
    }
    const r = t.resultado;
    if (t.estado === 'erro') el.innerHTML = `<div class="panel bad"><div class="row" style="align-items:center"><div><b>${nome}: não deu certo.</b> ${esc(t.erro)}</div>${fechar}</div>${avisos}</div>`;
    else if (t.estado === 'cancelado') el.innerHTML = `<div class="panel alert"><div class="row" style="align-items:center"><div><b>${nome} cancelado.</b> Os arquivos temporários foram apagados.</div>${fechar}</div></div>`;
    else if (t.tipo === 'previa') el.innerHTML = `<div class="panel"><div class="row" style="align-items:center"><div><b>Prévia pronta</b>
        <span class="small">de ${hms(r.inicio)} a ${hms(r.inicio + r.duracao)} do vídeo final (${hms(r.total)}) · feita em ${hms(t.decorrido)}</span></div>${fechar}</div>
        <video src="${esc(r.video)}" controls ${acabou ? 'autoplay' : ''} style="width:100%;max-height:60vh;margin-top:8px"></video>${avisos}</div>`;
    else el.innerHTML = `<div class="panel"><div class="row" style="align-items:center"><div><b>Vídeo pronto</b>
        <span class="small">${hms(r.duracao)} · ${tamanhoArq(r.tamanho)} · ${esc(r.codificador)} · feito em ${hms(t.decorrido)}</span></div>${fechar}</div>
        <div class="grid2" style="margin-top:8px"><div><video src="${esc(r.video)}" controls style="width:100%"></video>
          <div class="small" style="margin-top:4px"><a href="${esc(r.video)}" download>Baixar MP4</a> · <a href="${esc(r.capitulos)}" download>capítulos (.txt)</a><br>${esc(r.arquivo)}</div></div>
        <div><label style="margin-top:0">Capítulos para a descrição do YouTube</label><textarea id="gCap" readonly style="min-height:200px;font-family:Consolas,monospace">${esc(r.texto_capitulos)}</textarea>
          <button class="small" id="gCopiar" style="margin-top:6px">Copiar capítulos</button></div></div>${avisos}</div>`;
    const f = $('[data-fechar]', el); if (f) f.onclick = () => { el.innerHTML = ''; };
    const c = $('#gCopiar'); if (c) c.onclick = () => navigator.clipboard.writeText(r.texto_capitulos).then(() => toast('Capítulos copiados.'), () => { $('#gCap').select(); toast('Selecionei o texto: use Ctrl+C.'); });
  };
  const acompanhar = () => {
    clearInterval(timer);
    timer = setInterval(async () => {
      if (!rotaAtiva('#/gerador')) { clearInterval(timer); timer = null; return; }
      try {
        const t = await api('/api/gerador/progresso');
        renderProgresso(t, true);
        if (t && t.estado === 'rodando') return;
        clearInterval(timer); timer = null;
        if (t && t.estado === 'pronto' && t.tipo === 'video') { renderSaidas((await api('/api/gerador')).saidas); toast('Vídeo pronto.'); }
      } catch { /* servidor ocupado: tenta de novo no próximo segundo */ }
    }, 1000);
  };
  const iniciar = async tipo => {
    $('#gPrevia').disabled = $('#gRender').disabled = true;
    try {
      const t = await acao(() => api('/api/gerador/' + (tipo === 'previa' ? 'previa' : 'renderizar'), {method: 'POST', body: corpo()}));
      clearTimeout(gSalvarT);
      renderProgresso(t); acompanhar();
    } catch { $('#gPrevia').disabled = $('#gRender').disabled = false; }
  };
  $('#gPrevia').onclick = () => iniciar('previa');
  $('#gRender').onclick = () => iniciar('video');

  renderFaixas(); mostrarModo(); renderSaidas(e.saidas); renderProgresso(e.trabalho);
  if (e.trabalho && e.trabalho.estado === 'rodando') acompanhar();
}

/* ------------------------------------------------------------------ configurações globais */
let abaConfig = UNIFICADO ? 'c_acesso' : 'acesso';
const BASE_HIST = '/api/configuracoes', BASE_CORT = '/api/estudio/configuracoes';

// O que o usuário cola é muitas vezes a linha inteira da célula do Colab: fica só o primeiro link, sem barra nem pontuação no fim.
// Devolve '' para campo vazio e null quando há texto mas nenhum link.
function extrairLinkComfy(texto) {
  texto = (texto || '').trim();
  if (!texto) return '';
  const m = texto.match(/https?:\/\/\S+/i);
  return m ? m[0].replace(/[\s.,;:!?)\]}"'>]+$/, '').replace(/\/+$/, '') : null;
}

async function telaConfiguracoes() {
  // Página única: os grupos do Cortador (Flask) vêm antes dos das Histórias (este serviço); cada campo sabe
  // para onde salvar. As chaves do Cortador chegam com o prefixo "c:".
  const [hist, cort] = await Promise.all([
    UNIFICADO ? api(BASE_HIST).catch(e => ({erro: e.message})) : api(BASE_HIST),
    UNIFICADO ? api(BASE_CORT).catch(e => ({erro: e.message})) : null]);
  if (!rotaAtiva('#/configuracoes')) return;
  const marcar = (cfg, base, lado) => (cfg && !cfg.erro ? cfg.grupos : []).map(g => ({...g, base, lado}));
  const grupos = [...marcar(cort, BASE_CORT, 'Cortador'), ...marcar(hist, BASE_HIST, UNIFICADO ? 'Histórias e Music' : '')];
  if (!grupos.length) { app.innerHTML = `<div class="panel bad">${esc((hist && hist.erro) || (cort && cort.erro) || 'Sem configurações.')}</div>`; return; }
  const formatos = (hist && !hist.erro && hist.formatos) || {};
  if (!grupos.some(g => g.id === abaConfig)) abaConfig = grupos[0].id;
  const campos = Object.fromEntries(grupos.flatMap(g => g.campos.map(c => [c.chave, {...c, base: g.base}])));
  const nomeCampo = c => (c.formato ? formatos[c.formato] + ' · ' : '') + c.rotulo;
  const textoPadrao = c => {
    if (c.padrao_rotulo) return c.padrao_rotulo;
    if (c.tipo === 'booleano') return c.padrao ? 'ligado' : 'desligado';
    if (c.tipo === 'escolha') return (c.opcoes.find(o => o.valor === c.padrao) || {rotulo: c.padrao}).rotulo;
    return c.padrao === '' || c.padrao == null ? '(vazio)' : String(c.padrao);
  };
  const ORIGEM = {'.env': ['.env', 'ok'], 'configuracoes.json': ['json', 'ok'], 'ambiente do sistema': ['ambiente do sistema', 'warn'], 'padrão': ['padrão', ''],
                  'config_cortador.json': ['config_cortador.json', 'ok'], 'fase1/.env': ['fase1/.env', 'ok'], 'publicador.env': ['publicador.env', 'ok'],
                  'config.json': ['config.json', 'ok'], 'config_crop.yaml': ['config_crop.yaml', '']};
  const selos = c => {
    const [t, cls] = ORIGEM[c.origem] || [c.origem, ''];
    return `<span class="badge ${cls}" title="Onde este valor está salvo">${esc(t)}</span>` +
      (c.ao_vivo ? '' : ` <span class="badge warn" title="Só vale depois de reiniciar">reiniciar</span>`) +
      (c.pendente_reinicio ? ' <span class="badge err" title="O valor salvo ainda não está em uso: reinicie">aguardando reinício</span>' : '');
  };
  // O campo mostra o valor em uso: o salvo ou, sem nada salvo, o padrão (a API manda valor null no padrão).
  const emUso = c => c.valor ?? c.padrao ?? '';
  const igualPadrao = (c, v) => c.padrao != null && v !== '' &&
    ((c.tipo === 'inteiro' || c.tipo === 'numero') ? Number(v) === Number(c.padrao) : String(v) === String(c.padrao));
  const entrada = (c, curto = false) => {
    const k = esc(c.chave), pad = esc(textoPadrao(c));
    if (c.tipo === 'leitura') {
      return `<div class="cfg-linha"><input type="text" value="${esc(typeof c.valor === 'object' ? JSON.stringify(c.valor) : c.valor)}" disabled></div>`;
    }
    if (c.tipo === 'segredo') {
      const v = c.valor || {};
      return `<div class="cfg-linha"><input type="password" data-cfg="${k}" autocomplete="new-password" spellcheck="false"
        placeholder="${v.definido ? `definida (…${esc(v.final)}) — deixe em branco para manter` : 'não definida'}">
        <button class="small" type="button" data-mostrar="${k}">Mostrar</button>
        ${v.definido ? `<button class="small" type="button" data-apagar="${k}" title="Remove a chave do arquivo">Apagar</button>` : ''}</div>`;
    }
    const restaurar = `<button class="small" type="button" data-restaurar="${k}" title="Restaurar padrão: ${pad}">${curto ? 'padrão' : 'Restaurar padrão'}</button>`;
    if (c.tipo === 'booleano') {
      return `<div class="cfg-linha"><label class="inline" style="flex:1"><input type="checkbox" data-cfg="${k}" ${c.valor ? 'checked' : ''}> ligado</label>${restaurar}</div>`;
    }
    let campo;
    if (c.tipo === 'escolha') {
      const atual = emUso(c), temPadrao = c.opcoes.some(o => o.valor === c.padrao);
      campo = `<select data-cfg="${k}">${temPadrao ? '' : `<option value="">padrão (${pad})</option>`}${c.opcoes.map(o =>
        `<option value="${esc(o.valor)}" ${atual === o.valor ? 'selected' : ''}>${esc(o.rotulo)}${o.valor === c.padrao ? ' (padrão)' : ''}</option>`).join('')}</select>`;
    } else if (c.tipo === 'inteiro' || c.tipo === 'numero') {
      campo = `<input type="number" data-cfg="${k}" placeholder="${pad}" value="${esc(emUso(c))}"
        ${c.minimo != null ? `min="${c.minimo}"` : ''} ${c.maximo != null ? `max="${c.maximo}"` : ''} step="${c.tipo === 'inteiro' ? 1 : (c.passo || 'any')}">`;
    } else {
      const lista = c.sugestoes && c.sugestoes.length ? `list="dl_${k}"` : '';
      campo = `<input type="text" data-cfg="${k}" placeholder="${pad}" value="${esc(emUso(c))}" spellcheck="false" ${lista}>` +
        (lista ? `<datalist id="dl_${k}">${c.sugestoes.map(s => `<option value="${esc(s)}">`).join('')}</datalist>` : '');
    }
    return `<div class="cfg-linha">${campo}${c.unidade ? `<span class="small fix">${esc(c.unidade)}</span>` : ''}${restaurar}</div>`;
  };
  const bloco = c => `<div class="cfg-campo" data-bloco="${esc(c.chave)}">
    <label>${esc(c.rotulo)} ${selos(c)}</label>${entrada(c)}
    <div class="small">${esc(c.ajuda)}${c.aviso ? ` <span style="color:var(--warn)">${esc(c.aviso)}</span>` : ''}</div></div>`;
  const celula = c => `<div class="cfg-campo" data-bloco="${esc(c.chave)}" title="${esc(nomeCampo(c))} · ${esc(c.origem)}">${entrada(c, true)}
    <div class="small">${c.origem !== 'padrão' ? esc((ORIGEM[c.origem] || [c.origem])[0]) : ''}${c.pendente_reinicio ? ' · aguardando reinício' : ''}</div></div>`;
  const tabelaFormatos = g => {
    const fmts = Object.keys(formatos);
    const linhas = g.campos.filter(c => c.formato === fmts[0]);
    return `<table class="cfg-fmt"><tr><th>Parâmetro</th>${fmts.map(f => `<th>${esc(formatos[f])}</th>`).join('')}</tr>
      ${linhas.map(l => `<tr><td><b>${esc(l.rotulo)}</b>${l.unidade ? ` <span class="small">(${esc(l.unidade)})</span>` : ''}<div class="small">${esc(l.ajuda)}</div></td>
        ${fmts.map(f => `<td>${celula(campos[`FORMATOS.${f}.${l.campo}`])}</td>`).join('')}</tr>`).join('')}</table>`;
  };
  const idTeste = (g, t) => `${g.id}:${t.servico}`;
  // Em Imagens, o link do ComfyUI ganha uma caixa própria no topo (salva e testa de uma vez); a linha comum de COMFY_URL some.
  const temCaixaComfy = g => g.campos.some(c => c.chave === 'COMFY_URL');
  const caixaComfy = g => `<div class="panel" style="background:transparent">
      <label for="cfComfyUrl">Link do ComfyUI (o que a célula do Colab imprime)</label>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input id="cfComfyUrl" style="flex:1;min-width:280px" placeholder="https://xxxx.trycloudflare.com" spellcheck="false" value="${esc(emUso(campos.COMFY_URL))}">
        <button type="button" id="cfComfySalvar" class="main">Salvar e testar</button>
      </div>
      <p class="small" style="margin-bottom:4px">Pode colar a linha inteira do Colab. Link respondendo: as imagens saem do ComfyUI; senão, pela API (Pollinations).</p>
      <div id="cfComfyRes" class="small"></div></div>`;
  const corpoGrupo = g => `<section data-grupo="${g.id}" ${g.id === abaConfig ? '' : 'hidden'}>
    <div class="panel"><p class="small" style="margin-top:0">${g.lado ? `<b>${esc(g.lado)}</b> · ` : ''}${esc(g.descricao)}</p>
    ${temCaixaComfy(g) ? caixaComfy(g) : ''}
    ${g.testes.length ? `<div class="row" style="align-items:center;margin-bottom:10px">${g.testes.map(t =>
      `<div class="fix"><button type="button" data-testar="${esc(idTeste(g, t))}">${esc(t.rotulo)}</button><span class="small cfg-res" data-res="${esc(idTeste(g, t))}">usa o que está salvo</span></div>`).join('')}</div>` : ''}
    ${g.id === 'formatos' ? tabelaFormatos(g) : `<div class="cfg-grade">${g.campos.filter(c => !(temCaixaComfy(g) && c.chave === 'COMFY_URL')).map(bloco).join('')}</div>`}</div></section>`;
  const abas = lado => grupos.filter(g => g.lado === lado).map(g => `<button data-g="${g.id}" class="${g.id === abaConfig ? 'on' : ''}">${esc(g.titulo)}</button>`).join('');
  const lados = [...new Set(grupos.map(g => g.lado))];
  const arquivos = [cort && !cort.erro ? `Cortador: chaves em <code>${esc(cort.env_arquivo)}</code>, o resto em <code>${esc(cort.json_arquivo)}</code> (só o que difere dos YAML do repositório)` : '',
    hist && !hist.erro ? `${UNIFICADO ? 'Histórias: ' : ''}chaves em <code>${esc(hist.env_arquivo)}</code>, o resto em <code>${esc(hist.json_arquivo)}</code>` : ''].filter(Boolean);

  app.innerHTML = `
  <div class="row" style="align-items:center"><h2 style="margin:0">Configurações</h2>
    <span class="small fix" id="kConta"></span><button class="fix main" id="kSalvar">Salvar configurações</button></div>
  <p class="small">${arquivos.join('. ')}. Nenhuma chave volta inteira pela API: para trocar uma, digite a nova; em branco, a atual é mantida. O que é por canal fica em Canais; as pastas e efeitos de cada render, em Music.</p>
  ${[...(hist && hist.erro ? [`Histórias: ${hist.erro}`] : []), ...(cort && cort.erro ? [`Cortador: ${cort.erro}`] : []),
     ...((cort && cort.avisos) || []), ...((hist && hist.avisos) || [])].map(a => `<div class="panel alert small">${esc(a)}</div>`).join('')}
  ${lados.map(l => `<div class="tabs" data-lado="${esc(l)}" style="flex-wrap:wrap;align-items:center">${l ? `<span class="small fix" style="padding:0 8px 0 0;font-weight:600">${esc(l)}</span>` : ''}${abas(l)}</div>`).join('')}
  ${grupos.map(corpoGrupo).join('')}`;

  const els = Object.fromEntries($$('[data-cfg]').map(e => [e.dataset.cfg, e]));
  const original = {};
  for (const [k, e] of Object.entries(els)) original[k] = e.type === 'checkbox' ? e.checked : e.value;
  const blocos = Object.fromEntries($$('[data-bloco]').map(e => [e.dataset.bloco, e]));
  const lido = k => els[k].type === 'checkbox' ? els[k].checked : els[k].value.trim();
  const mudou = k => campos[k].tipo === 'segredo' ? lido(k) !== '' : lido(k) !== (typeof original[k] === 'string' ? original[k].trim() : original[k]);
  const enviado = k => { const v = lido(k), c = campos[k]; return c.tipo !== 'booleano' && c.tipo !== 'segredo' && igualPadrao(c, v) ? '' : v; };
  const mudancas = () => Object.fromEntries(Object.keys(els).filter(mudou).map(k => [k, enviado(k)]));
  const atualizar = () => {
    let n = 0;
    for (const k of Object.keys(els)) {
      const m = mudou(k); n += m;
      blocos[k].classList.toggle('mudou', m);
    }
    $('#kConta').textContent = n ? `${n} alteração(ões) não salva(s)` : '';
  };
  Object.values(els).forEach(e => { e.oninput = atualizar; e.onchange = atualizar; });
  atualizar();

  $$('.tabs[data-lado] button').forEach(b => b.onclick = () => {
    abaConfig = b.dataset.g;
    $$('.tabs[data-lado] button').forEach(x => x.classList.toggle('on', x === b));
    $$('[data-grupo]').forEach(s => s.hidden = s.dataset.grupo !== abaConfig);
  });
  $$('[data-restaurar]').forEach(b => b.onclick = () => {
    const k = b.dataset.restaurar, e = els[k];
    if (e.type === 'checkbox') e.checked = !!campos[k].padrao; else e.value = campos[k].padrao ?? '';
    atualizar();
  });
  $$('[data-mostrar]').forEach(b => b.onclick = () => {
    const e = els[b.dataset.mostrar], ver = e.type === 'password';
    e.type = ver ? 'text' : 'password'; b.textContent = ver ? 'Ocultar' : 'Mostrar';
  });
  $$('[data-apagar]').forEach(b => b.onclick = async () => {
    const c = campos[b.dataset.apagar];
    if (!confirm(`Apagar "${c.rotulo}"?\n\nA linha sai do arquivo de chaves (há uma cópia .bak desde a primeira gravação). Sem ela, o programa volta a avisar que falta a chave.`)) return;
    try { await acao(() => api(c.base + '/restaurar', {method: 'POST', body: {chaves: [c.chave]}}), `${c.rotulo} apagada.`); }
    catch { return; }
    carregarSistema().catch(() => {});
    telaConfiguracoes();
  });
  $$('[data-testar]').forEach(b => b.onclick = async () => {
    const [gid, servico] = b.dataset.testar.split(':');
    const g = grupos.find(x => x.id === gid);
    const res = $(`[data-res="${b.dataset.testar}"]`);
    b.disabled = true; res.className = 'small cfg-res'; res.textContent = 'testando...';
    try {
      const r = await api(`${g.base}/testar/${servico}`, {method: 'POST'});
      res.className = 'small cfg-res ' + (r.ok ? 'ok' : 'err'); res.textContent = r.mensagem;
    } catch (e) { res.className = 'small cfg-res err'; res.textContent = e.message; }
    finally { b.disabled = false; }
  });
  const gComfy = grupos.find(temCaixaComfy);
  if (gComfy) {
    const res = $('#cfComfyRes'), entradaUrl = $('#cfComfyUrl');
    const mostrar = (ok, rotulo, texto) => { res.innerHTML = `<span class="badge ${ok ? 'ok' : 'err'}">${esc(rotulo)}</span> ${esc(texto)}`; };
    const testarComfy = async () => {
      const r = await api(`${gComfy.base}/testar/comfy`, {method: 'POST'});
      mostrar(r.ok, r.ok ? 'conectado' : 'sem conexão', r.mensagem);
    };
    if (entradaUrl.value.trim()) { res.textContent = 'Testando...'; testarComfy().catch(err => { res.textContent = err.message; }); }
    $('#cfComfySalvar').onclick = async () => {
      const url = extrairLinkComfy(entradaUrl.value);
      if (url === null) return mostrar(false, 'sem link', 'Não achei um link (https://...) no que foi colado.');
      const b = $('#cfComfySalvar'); b.disabled = true;
      try {
        await acao(() => api(gComfy.base, {method: 'PUT', body: {COMFY_URL: url}}), url ? 'Link salvo.' : 'Link removido.');
        entradaUrl.value = url;
        if (!url) return mostrar(false, 'sem link', 'As imagens vão pela API (Pollinations).');
        res.textContent = 'Testando...';
        await testarComfy();
      } catch (err) { res.textContent = err.message; } finally { b.disabled = false; }
    };
  }
  $('#kSalvar').onclick = async () => {
    const mud = mudancas();
    if (!Object.keys(mud).length) return toast('Nada mudou.');
    const b = $('#kSalvar'); b.disabled = true;
    try {
      // Um lote por serviço; cada um é tudo ou nada.
      const porBase = {};
      for (const [k, v] of Object.entries(mud)) (porBase[campos[k].base] ||= {})[k] = v;
      const r = {salvos: [], restaurados: [], ao_vivo: [], reiniciar: []};
      for (const [base, lote] of Object.entries(porBase)) {
        const x = await acao(() => api(base, {method: 'PUT', body: lote}));
        for (const chave of Object.keys(r)) r[chave].push(...(x[chave] || []));
      }
      const nomes = ks => ks.map(k => nomeCampo(campos[k])).join(', ');
      const feitos = [...r.salvos, ...r.restaurados];
      const partes = [];
      const vivos = r.ao_vivo.filter(k => feitos.includes(k)), reinicia = r.reiniciar.filter(k => feitos.includes(k));
      if (vivos.length) partes.push(`Já vale: ${nomes(vivos)}.`);
      if (reinicia.length) partes.push(`Precisa reiniciar: ${nomes(reinicia)}.`);
      toast(`Salvo. ${partes.join(' ')}`);
      carregarSistema().catch(() => {});
      await telaConfiguracoes();
    } catch { /* o aviso já apareceu */ } finally { b.disabled = false; }
  };
}


/* ------------------------------------------------------------------ teste do ComfyUI */
async function telaTesteComfy() {
  const e = await api('/api/teste-comfy');
  if (!rotaAtiva('#/teste-comfy')) return;
  const opcoesProj = e.projetos.map(p => `<option value="${p.id}">#${p.id} · ${esc(p.titulo)}</option>`).join('');
  app.innerHTML = `
    <h2>Teste do ComfyUI</h2>
    <div class="panel">
      <label>Link do Cloudflare (o que a célula do Colab imprime; muda a cada sessão)</label>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <input id="tcUrl" style="flex:1;min-width:280px" placeholder="https://xxxx.trycloudflare.com" value="${esc(e.url)}">
        <button id="tcSalvar" class="main">Salvar e testar</button>
      </div>
      <p class="small">Workflow: <b>${esc(e.workflow)}</b> · saída ${e.megapixels} MP · referências ${e.megapixels_ref} MP. Salvar grava COMFY_URL; o motor de imagem dos vídeos continua como está nas Configurações.</p>
      <div id="tcConexao" class="small"></div>
    </div>
    <div class="panel">
      ${e.projetos.length ? `
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
        <div style="flex:1;min-width:240px"><label>História</label><select id="tcProj">${opcoesProj}</select></div>
        <div style="flex:1;min-width:240px"><label>Cena</label><select id="tcCena"></select></div>
        <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="tcRefazer"> Refazer fichas e placas</label>
        <button id="tcGerar" class="gold">Gerar esta cena</button>
        <button id="tcTodas" class="main">Gerar a história inteira</button>
        <button id="tcParar" hidden>Parar</button>
      </div>
      <p class="small">Gera a ficha de cada pessoa da cena, a placa do lugar (se houver) e a cena usando essas imagens como referência. As fichas ficam guardadas: testar outra cena com as mesmas pessoas só gera a cena.</p>
      <p class="small"><b>História inteira:</b> do jeito do vídeo, com o workflow de uma imagem chamado uma vez por imagem: primeiro as bases (fichas e placas), depois cada cena com as bases como referência. Não mexe no projeto; tudo fica na pasta teste_comfy dele. Parar termina a imagem que está no Comfy e para.</p>`
      : '<p>Nenhuma história escrita ainda. Crie uma em Histórias e volte aqui.</p>'}
    </div>
    <div id="tcSaida"></div>`;

  const conexao = async () => {
    const r = await api('/api/configuracoes/testar/comfy', {method: 'POST'});
    $('#tcConexao').innerHTML = `<span class="badge ${r.ok ? 'ok' : 'err'}">${r.ok ? 'conectado' : 'sem conexão'}</span> ${esc(r.mensagem)}`;
  };
  if (e.url) conexao().catch(() => {});
  $('#tcSalvar').onclick = async () => {
    const url = extrairLinkComfy($('#tcUrl').value);
    if (url === null) { $('#tcConexao').textContent = 'Não achei um link (https://...) no que foi colado.'; return; }
    $('#tcUrl').value = url;
    await acao(() => api('/api/configuracoes', {method: 'PUT', body: {COMFY_URL: url}}), url ? 'Link salvo.' : 'Link removido.');
    $('#tcConexao').textContent = 'Testando...';
    await conexao().catch(err => { $('#tcConexao').textContent = err.message; });
  };
  if (!e.projetos.length) return;

  const cenas = () => {
    const p = e.projetos.find(x => x.id === +$('#tcProj').value);
    $('#tcCena').innerHTML = p.cenas.map(c => `<option value="${c.n}">${c.n} · ${esc(c.resumo)}</option>`).join('');
  };
  $('#tcProj').onchange = cenas; cenas();

  const desenhar = j => {
    const passos = j.passos || [], log = j.log || [];
    const agora = Date.now() / 1000;
    const caixa = document.querySelector('#tcLog');
    // Quem estava lendo o log mais acima não é puxado para o fim a cada atualização.
    const noFim = !caixa || caixa.scrollTop + caixa.clientHeight >= caixa.scrollHeight - 30;
    const abertos = [...document.querySelectorAll('#tcSaida details[open]')].map(d => d.dataset.id);
    $('#tcSaida').innerHTML = (j.estado === 'erro' ? `<div class="panel bad">${esc(j.erro)}</div>` : '') +
      (j.estado === 'gerando' ? '<p class="small">Gerando no Comfy... cada imagem leva perto de um minuto na T4 (mais na primeira, que carrega o modelo).</p>' : '') +
      `<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">` + passos.map((p, i) => `
        <div class="panel" style="width:300px;margin:0">
          <b>${esc(p.rotulo)}</b>
          ${p.arquivo ? `<img data-zoom src="${p.arquivo}" style="width:100%;border-radius:6px;margin-top:8px;cursor:zoom-in">`
            : p.status === 'erro' ? `<p class="bad">${esc(p.erro)}</p>` : '<div class="spin"></div>'}
          <p class="small">${p.status === 'gerando' ? `<b>${esc(p.progresso || '')}</b> · ${Math.round(agora - (p.inicio || agora))} s`
            : p.segundos != null ? p.segundos + ' s' : ''}</p>
          <details data-id="prompt${i}"${abertos.includes('prompt' + i) ? ' open' : ''}><summary class="small">prompt</summary><p class="small">${esc(p.prompt)}</p></details>
        </div>`).join('') + '</div>' +
      (log.length ? `<div class="panel" style="margin-top:16px">
        <div style="display:flex;justify-content:space-between;align-items:center"><b>Log</b>
          <span class="small">${log.length} linha(s) · também no servidor: journalctl --user -u videomaker-historias -f</span></div>
        <div id="tcLog" class="log" style="max-height:340px;margin-top:8px">${log.map(l =>
          `<div class="${/ERRO|erro/.test(l.texto) ? 'erro' : ''}"><time>${esc(l.hora)}</time>${l.passo ? `<b>${esc(l.passo)}</b> · ` : ''}${esc(l.texto)}</div>`).join('')}</div>
      </div>` : '');
    const nova = document.querySelector('#tcLog');
    if (nova && noFim) nova.scrollTop = nova.scrollHeight;
  };
  const acompanhar = () => {
    clearInterval(timer);
    timer = setInterval(async () => {
      if (!rotaAtiva('#/teste-comfy')) { clearInterval(timer); timer = null; return; }
      const j = await api('/api/teste-comfy/progresso').catch(() => null);
      if (!j) return;
      desenhar(j);
      if (j.estado !== 'gerando') { clearInterval(timer); timer = null; ocupado(false); }
    }, 2000);
  };
  const ocupado = sim => {
    $('#tcGerar').disabled = $('#tcTodas').disabled = sim;
    $('#tcParar').hidden = !sim; $('#tcParar').disabled = false;
  };
  const gerar = async todas => {
    ocupado(true);
    try {
      const j = await acao(() => api('/api/teste-comfy/gerar', {method: 'POST', body: {
        projeto_id: +$('#tcProj').value, cena: +$('#tcCena').value, refazer_fichas: $('#tcRefazer').checked, todas}}));
      desenhar(j); acompanhar();
    } catch { ocupado(false); }
  };
  $('#tcGerar').onclick = () => gerar(false);
  $('#tcTodas').onclick = () => gerar(true);
  $('#tcParar').onclick = async () => {
    $('#tcParar').disabled = true;
    await acao(() => api('/api/teste-comfy/parar', {method: 'POST'}), 'Para depois da imagem atual.').catch(() => {});
  };
  if (e.job && e.job.estado) { desenhar(e.job); if (e.job.estado === 'gerando') { ocupado(true); acompanhar(); } }
}

/* ------------------------------------------------------------------ início */
// Por último: abrir a página já numa aba (ex.: #/configuracoes) chama a tela na hora, e as constantes
// de cada tela (BASE_HIST, abaConfig...) precisam estar declaradas antes.
carregarSistema().catch(() => {});
rota();
