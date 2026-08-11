# Corta+Legenda

Aplicativo Windows que corta um vídeo, converte para formato vertical, aplica logo/texto e cria legendas localmente. Nenhum vídeo é enviado para um serviço externo.

## Recursos do MVP

- Recorte por tempo de início e fim;
- Legendas geradas somente para o trecho recortado, em blocos de até quatro palavras;
- Legendas em Montserrat amarela com contorno preto destacado;
- Saída original ou vertical 9:16 (preenchida, com fundo desfocado ou com imagem fixa no topo);
- Logo com posição e tamanho configuráveis;
- Texto sobre o vídeo;
- Exportação MP4 H.264.

O app inicia com `assets\\default-logo.png`, no canto inferior esquerdo com 250 px, e com o texto padrão "Glauber Fugiu do Mamãe Falei!". A seleção de vídeo abre diretamente em `Vídeos\\CortaLegenda\\Downloads`.

## Instalação para desenvolvimento

1. Instale o [Python 3.11 ou 3.12](https://www.python.org/downloads/windows/) e marque a opção **Add Python to PATH**.
2. Instale o [FFmpeg](https://ffmpeg.org/download.html) e deixe `ffmpeg` disponível no `PATH`.
3. Abra o PowerShell nesta pasta e execute:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Na primeira geração de legendas, o Whisper baixa o modelo `small`. Depois disso, a transcrição é offline. O modelo ocupa cerca de 500 MB e costuma equilibrar bem qualidade e velocidade em português.

## Criar o executável

Com o ambiente virtual ativado:

```powershell
pyinstaller --noconsole --onefile --name CortaLegenda app.py
```

O executável será gerado em `dist\CortaLegenda.exe`. FFmpeg deve continuar instalado no Windows; numa próxima etapa ele pode ser incluído junto ao instalador.

## Observações

- A prévia mostra o arquivo original; o recorte, formato, logo e textos são aplicados na exportação.
- Para rever as legendas, abra o arquivo `.srt` temporário indicado na interface antes de exportar. A edição visual de cada fala será a próxima evolução recomendada.

## Baixar por URL

O aplicativo procura primeiro por `tools\\yt-dlp.exe`, uma cópia local atualizada que acompanha este projeto. Cole uma URL no campo **Ou baixe um vídeo** e o arquivo será salvo em `Vídeos\\CortaLegenda\\Downloads`; depois, selecione-o normalmente para cortar e editar.

Para vídeos segmentados (DASH/HLS), o campo **Conexões simultâneas** usa 8 por padrão, permitindo baixar vários fragmentos em paralelo. Se o site limitar a velocidade, pode haver ganho pequeno ou nenhum; reduza para 1 se houver erros.

## Cortes em lote (aba CSV)

A aba **📊 CSV Lotes** corta vários trechos de uma vez, cada um exportado em **9:16** pronto para shorts. As legendas são geradas por **Whisper** por corte (bem mais sincronizadas do que texto colado), com opção de desligar.

### Formato do CSV

Use um cabeçalho na primeira linha; as colunas são identificadas pelo nome e podem vir em qualquer ordem:

```csv
inicio,fim,titulo,formato,imagem,comentario
00:10,00:25,Abertura,estender,,Boa abertura
01:30,01:55,Reação,imagem,capas/reacao.png,Reação do público
05:00,05:20,Bônus,transparente,,
```

- **inicio / fim**: `MM:SS`, `HH:MM:SS` ou segundos.
- **titulo**: nome do arquivo de saída (opcional).
- **comentario** (opcional): notas sobre o corte, exibidas na tabela de preview.
- **formato** (opcional): como enquadrar o corte em 9:16. Se ficar vazio, usa o **formato padrão** escolhido na aba.
  - `estender` — preenche o 9:16 cortando as laterais;
  - `transparente` — mantém o vídeo 16:9 centralizado, com fundo desfocado em cima e embaixo;
  - `imagem` — dois blocos 16:9 empilhados: a **imagem fixa em cima** e o corte embaixo;
  - `original` — mantém 16:9 (para vídeos longos).
- **imagem** (opcional): caminho do PNG/JPG usado só quando `formato=imagem`. Caminhos relativos são resolvidos a partir da pasta do CSV. Assim, cada corte pode ter a sua própria imagem, e os cortes que não usam esse modo simplesmente deixam a coluna em branco.

Sem cabeçalho, o app aceita o formato antigo por posição: `inicio, fim, titulo, legenda`.

## Cortes de Live (aba 🔴 Live)

Cole a URL de uma live do YouTube e clique em **⏺ Gravar live**:

- A gravação usa `yt-dlp --live-from-start`, ou seja, baixa **desde o começo da live** mesmo que você entre atrasado. Os arquivos ficam em `Vídeos\CortaLegenda\Lives\live_<data>` e crescem continuamente enquanto a live roda.
- No player, **🔄 Atualizar** recarrega o arquivo (pega o trecho mais novo mantendo a posição), **⏩ Ao vivo** pula para o ponto mais recente, **⏪** volta 10s e os botões **1× / 1.5× / 2×** mudam a velocidade — útil para alcançar o ao-vivo assistindo rápido.
- Em **Novo corte** você marca início/fim (com **📍 agora** na posição do player), define o título (queimado na tela), o formato (estender / transparente / imagem fixa / original) e clica **✂️ Exportar corte** — tudo **sem esperar a live acabar**.
- Na exportação, o app extrai o áudio do período marcado, gera as legendas com **Whisper** (sincronizadas, estilo Montserrat amarela) e queima no vídeo junto com logo e título. Dá para desligar as legendas no checkbox.

Observação: evite marcar o fim do corte nos últimos segundos do ao-vivo — deixe uns 10s de folga para o trecho já estar gravado em disco.

Use somente links de vídeos que você tem autorização para baixar e reutilizar, respeitando os termos da plataforma e os direitos autorais.
