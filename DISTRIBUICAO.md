# Distribuição do Corta+Legenda

## Executável Standalone

O app foi compilado em um executável Windows standalone usando PyInstaller.

### Localização
- **Executável**: `dist/Corta+Legenda.exe` (~196 MB)
- **Dependências**: `dist/CortaLegenda/_internal/`

### Requisitos de Sistema
- **Windows 7+** (testado em Windows 11)
- **FFmpeg** instalado e no PATH (baixar em https://ffmpeg.org)
- **python-vlc** incluído no executável
- **yt-dlp** incluído no executável
- **Whisper** (opcional): se quiser legendas automáticas, instale com:
  ```bash
  pip install openai-whisper
  ```

### Como Usar

#### Opção 1: Executável Direto
Basta clicar duas vezes em `Corta+Legenda.exe`

#### Opção 2: Script de Instalação
Execute o `INSTALAR.bat` para copiar o app para a pasta de programas e criar um atalho no Desktop.

#### Opção 3: Distribuição Portável
Copie a pasta `dist/CortaLegenda` inteira para qualquer lugar e coloque `Corta+Legenda.exe` ao lado.

### Primeiras Execuções
Na primeira execução, o app pode demorar alguns segundos a mais para inicializar (extração de dependências). Execuções posteriores são instantâneas.

### Dados
O app salva:
- **Vídeos processados**: `~/Videos/CortaLegenda/`
- **Trabalho temporário**: `~/.CortaLegenda/work/`

Você pode mudar a pasta de saída direto na aba **Edição**.

### FFmpeg
Se o FFmpeg não estiver no PATH:
1. Baixe em https://ffmpeg.org/download.html
2. Descompacte para `C:\ffmpeg` (ou outro local)
3. Na primeira execução do app, ele pedirá o caminho

### Whisper (Legendas Automáticas)
Para usar legendas automáticas com Whisper (sem dependência de vídeo com legenda pronta):
1. Instale Whisper: `pip install openai-whisper`
2. Ao gerar legendas, o app baixará o modelo (~1.4 GB na primeira vez)

Se não tiver Whisper, o app tenta reaproveitar legendas já presentes no vídeo (ex.: do YouTube via yt-dlp).

## Reconstruir o Executável

Se quiser recompilar a partir do código-fonte:

```bash
# 1. Clone o repo
git clone <repo>
cd "Video Maker"

# 2. Instale as dependências de desenvolvimento
pip install -r requirements.txt

# 3. Recrie o executável
.venv/Scripts/pyinstaller.exe CortaLegenda.spec --noconfirm
```

O novo executável estará em `dist/Corta+Legenda.exe`.

## Tamanho do Build
- **Executável único**: ~196 MB (inclui Python runtime e bibliotecas)
- **Descompactado**: ~450 MB (com `_internal`)

Se quiser reduzir:
- Use `--onefile` no spec (mais lento na inicialização)
- Remova dependências desnecessárias do spec
- Comprima com UPX (já está habilitado no spec)

## Troubleshooting

### "FFmpeg não encontrado"
→ Instale FFmpeg em https://ffmpeg.org e adicione ao PATH, ou escolha o caminho na primeira execução.

### App lento/congela ao processar
→ Reduzir o "Preset" na aba Edição (mais rápido = menor qualidade).

### Erro ao baixar vídeo do YouTube
→ O app tenta reconectar até 4 vezes. Se persistir, pode ser bloqueio geográfico ou limite de taxa.

### Legendas em branco/vazias
→ O vídeo não tem fala detectable pelo Whisper. Tente escolher outro trecho ou forneça uma legenda manualmente.

## Próximas Versões
- [ ] Installer NSIS (instalador clássico)
- [ ] Atualização automática integrada
- [ ] Suporte a MacOS/Linux
