import os, subprocess, time, re, json, urllib.request

COMFY = "/content/ComfyUI"
QUANT = "Q4_K_M"   # Q4_K_M (rápido) | Q5_K_S (melhor qualidade) | Q3_K_M (menos memória)

# LoRA "Hand-drawn American comic style redrawing" (Civitai, Flux.1 Kontext, versão 2147209).
# O Civitai exige login para baixar: crie uma chave em civitai.com/user/account (API Keys) e guarde
# no Colab em "Secrets" (ícone da chave, à esquerda) com o nome CIVITAI_TOKEN, ligando "Notebook access".
LORA_ARQUIVO = "american_comic_kontext.safetensors"   # o original tem nome em chinês; salvo com este
LORA_URL = "https://civitai.com/api/download/models/2147209?type=Model&format=SafeTensor"
LORA_FORCA = 1.0   # 0.6-1.0; se o traço ficar forte demais ou mudar a cena, baixe

def sh(cmd):
    return subprocess.run(cmd, shell=True)

# ComfyUI + nó GGUF
if not os.path.exists(COMFY):
    sh(f"git clone -q https://github.com/comfyanonymous/ComfyUI {COMFY}")
sh(f"git -C {COMFY} pull -q")
sh(f"pip install -q -r {COMFY}/requirements.txt")
GGUF = f"{COMFY}/custom_nodes/ComfyUI-GGUF"
if not os.path.exists(GGUF):
    sh(f"git clone -q https://github.com/city96/ComfyUI-GGUF {GGUF}")
sh(f"pip install -q -r {GGUF}/requirements.txt")

# Modelos (só a LoRA exige login, via token do Civitai)
def baixar(url, destino, cabecalho=None):
    if os.path.exists(destino):
        print("ok:", os.path.basename(destino)); return True
    print("baixando:", os.path.basename(destino))
    h = f'--header="{cabecalho}"' if cabecalho else ""
    sh(f'wget -q -c --show-progress {h} -O "{destino}.part" "{url}" && mv "{destino}.part" "{destino}"')
    ok = os.path.exists(destino)
    if not ok:
        print("FALHOU:", os.path.basename(destino))
        if os.path.exists(destino + ".part"):
            os.remove(destino + ".part")
    return ok

baixar(f"https://huggingface.co/QuantStack/FLUX.1-Kontext-dev-GGUF/resolve/main/flux1-kontext-dev-{QUANT}.gguf",
       f"{COMFY}/models/unet/flux1-kontext-dev-{QUANT}.gguf")
baixar("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors",
       f"{COMFY}/models/clip/clip_l.safetensors")
baixar("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors",
       f"{COMFY}/models/clip/t5xxl_fp8_e4m3fn.safetensors")
baixar("https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors",
       f"{COMFY}/models/vae/ae.safetensors")

# LoRA do Civitai
os.makedirs(f"{COMFY}/models/loras", exist_ok=True)
try:
    from google.colab import userdata
    token = userdata.get("CIVITAI_TOKEN")
except Exception:
    token = os.environ.get("CIVITAI_TOKEN")
usar_lora = False
if token:
    # token na URL (não no cabeçalho): o Civitai redireciona para um armazenamento que recusa o cabeçalho
    usar_lora = baixar(f"{LORA_URL}&token={token}", f"{COMFY}/models/loras/{LORA_ARQUIVO}")
else:
    print("AVISO: sem CIVITAI_TOKEN nos Secrets do Colab; o workflow sai SEM a LoRA.")

# Workflow pronto (formato API)
wf = {
 "1":  {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": f"flux1-kontext-dev-{QUANT}.gguf"}},
 "2":  {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "clip_l.safetensors", "clip_name2": "t5xxl_fp8_e4m3fn.safetensors", "type": "flux"}},
 "3":  {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
 "4":  {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
 "5":  {"class_type": "FluxKontextImageScale", "inputs": {"image": ["4", 0]}},
 "6":  {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
 "7":  {"class_type": "CLIPTextEncode", "inputs": {"text": "Convert to American comic style while keeping the main subject and composition unchanged.", "clip": ["2", 0]}},
 "8":  {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["7", 0], "latent": ["6", 0]}},
 "9":  {"class_type": "FluxGuidance", "inputs": {"conditioning": ["8", 0], "guidance": 2.5}},
 "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
 "11": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": 42, "steps": 20, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "positive": ["9", 0], "negative": ["10", 0], "latent_image": ["6", 0], "denoise": 1.0}},
 "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
 "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": "kontext"}},
}
if usar_lora:
    wf["14"] = {"class_type": "LoraLoaderModelOnly",
                "inputs": {"model": ["1", 0], "lora_name": LORA_ARQUIVO, "strength_model": LORA_FORCA}}
    wf["11"]["inputs"]["model"] = ["14", 0]
WF_PATH = "/content/kontext_gguf_api.json"
json.dump(wf, open(WF_PATH, "w"), indent=1)

# Inicia o ComfyUI
sh("pkill -f 'main.py --listen'"); time.sleep(2)
log = open("/content/comfy.log", "w")
subprocess.Popen(["python", "-u", "main.py", "--listen", "127.0.0.1", "--port", "8188"],
                 cwd=COMFY, stdout=log, stderr=subprocess.STDOUT)

for _ in range(150):
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=2); break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("ComfyUI não iniciou. Veja /content/comfy.log")

# Túnel público
if not os.path.exists("/usr/local/bin/cloudflared"):
    sh("wget -q -O /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x /usr/local/bin/cloudflared")
sh("pkill -f cloudflared"); time.sleep(1)
tlog = open("/content/tunnel.log", "w")
subprocess.Popen(["cloudflared", "tunnel", "--url", "http://127.0.0.1:8188", "--no-autoupdate"],
                 stdout=tlog, stderr=subprocess.STDOUT)

url = None
for _ in range(60):
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open("/content/tunnel.log").read())
    if m: url = m.group(0); break
    time.sleep(2)
print("\nABRA:", url or "sem URL, veja /content/tunnel.log")

# Baixa o workflow para o seu computador
from google.colab import files
files.download(WF_PATH)
