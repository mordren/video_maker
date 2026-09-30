"""Conferência do volume do corte pronto, com correção quando sai da faixa.

O YouTube abaixa para -14 LUFS o que chega mais alto, mas não sobe o que chega
baixo — áudio baixo fica baixo. E pico acima de -1 dBTP pode estourar quando
ele reencoda. A fala já sai da Fase 2 com `loudnorm` em -14, mas a mixagem com
a trilha (amix + alimiter em 0,95) e o AAC mexem nisso depois, então a medida
tem que ser no arquivo final.

Mede com o próprio `loudnorm` (print_format=json) e, se estiver fora, refaz
SÓ o áudio em duas passadas (a segunda usa a medida da primeira), com o vídeo
copiado. Medir, corrigir e medir de novo levou ~7 s num corte de 60 s no
notebook (29/09/2026); quando está dentro da faixa é só a medida (~2 s).

LRA baixo não é alerta: fala curta e comprimida tem LRA de 3 a 7 naturalmente.

Sem Qt, para poder ser testado solto e usado pelo Estúdio e pelo app.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PADRAO = {
    "audio_qa_enabled": True,
    "audio_qa_auto_fix": True,
    "audio_lufs_target": -14.0,
    "audio_lufs_min": -15.5,        # abaixo disso o YouTube não aumenta
    "audio_lufs_max": -13.0,        # acima disso ele abaixa (e o limitador já apertou à toa)
    "audio_true_peak_max": -1.0,
    "audio_true_peak_fix": -2.0,    # alvo da correção: o AAC passa ~0,7 dB disso (-1.5 saiu -0,8)
    "audio_lra_max": 12.0,
    "audio_lra_fix": 11.0,
    "audio_mix_delta_max": 1.5,     # LU que a trilha pode somar à fala
}


def config(cfg: dict | None = None) -> dict:
    """PADRAO com o que estiver na config do app (chaves audio_*)."""
    final = dict(PADRAO)
    for chave, valor in (cfg or {}).items():
        if chave in PADRAO and valor is not None:
            final[chave] = type(PADRAO[chave])(valor)
    return final


def _json_loudnorm(stderr: str) -> dict:
    """O loudnorm imprime o JSON no fim do stderr; pega o último bloco {...}."""
    fim = stderr.rfind("}")
    inicio = stderr.rfind("{", 0, fim)
    if inicio == -1 or fim == -1:
        raise RuntimeError("o ffmpeg não devolveu a medida do loudnorm")
    return json.loads(stderr[inicio:fim + 1])


class SemAudio(Exception):
    """O arquivo não tem faixa de áudio — não há o que conferir."""


def medir(arquivo: Path) -> dict:
    """LUFS integrado, true peak, LRA e limiar do áudio de `arquivo`."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(arquivo), "-vn",
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "does not contain any stream" in r.stderr:
        raise SemAudio(str(arquivo))
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou ao medir o áudio: {r.stderr[-300:]}")
    d = _json_loudnorm(r.stderr)
    return {"i": float(d["input_i"]), "tp": float(d["input_tp"]),
            "lra": float(d["input_lra"]), "thresh": float(d["input_thresh"])}


def avaliar(m: dict, cfg: dict) -> list[str]:
    """Alertas para a medida `m` (vazio = dentro da faixa)."""
    alertas = []
    if m["i"] < cfg["audio_lufs_min"]:
        alertas.append(f"volume baixo ({m['i']:.1f} LUFS; o YouTube não aumenta)")
    elif m["i"] > cfg["audio_lufs_max"]:
        alertas.append(f"volume alto ({m['i']:.1f} LUFS; o YouTube vai abaixar)")
    if m["tp"] > cfg["audio_true_peak_max"]:
        alertas.append(f"pico em {m['tp']:+.1f} dBTP (risco de estourar no encode)")
    if m["lra"] > cfg["audio_lra_max"]:
        alertas.append(f"variação de volume grande (LRA {m['lra']:.1f})")
    return alertas


def corrigir(mp4: Path, m: dict, cfg: dict) -> None:
    """Refaz o áudio de `mp4` com loudnorm na segunda passada (usando `m`, a
    medida da primeira) e o vídeo copiado. Troca o arquivo no lugar."""
    filtro = (f"loudnorm=I={cfg['audio_lufs_target']}:TP={cfg['audio_true_peak_fix']}"
              f":LRA={cfg['audio_lra_fix']}:measured_I={m['i']}:measured_TP={m['tp']}"
              f":measured_LRA={m['lra']}:measured_thresh={m['thresh']}:linear=true")
    tmp = mp4.with_name(mp4.stem + ".audioqa" + mp4.suffix)
    # aresample: o loudnorm sobe a taxa para 192 kHz por dentro; volta para 48k.
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-map", "0:v?",
                        "-map", "0:a", "-c:v", "copy", "-af", filtro + ",aresample=48000",
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg falhou ao corrigir o áudio: {r.stderr[-300:]}")
    tmp.replace(mp4)


def fala_x_mix(fala: Path, mix: dict, cfg: dict) -> str | None:
    """Com trilha: quanto a música soma à fala. A trilha fica bem abaixo
    (~-27 LUFS com ducking), então o mix quase não sobe; se subir mais que
    `audio_mix_delta_max`, ela está competindo com a voz. Só aviso."""
    delta = mix["i"] - medir(fala)["i"]
    if delta > cfg["audio_mix_delta_max"]:
        return f"trilha competindo com a voz (+{delta:.1f} LU sobre a fala)"
    return None


def resumo(m: dict) -> str:
    return f"{m['i']:.1f} LUFS · TP {m['tp']:+.1f} · LRA {m['lra']:.0f}"


def qa(mp4: Path, fala: Path | None = None, cfg: dict | None = None) -> dict:
    """Mede, corrige se precisar e mede de novo.

    `fala`: o vídeo antes da trilha (só quando teve trilha), para o aviso de
    trilha alta — ele não entra na correção. Devolve {"antes", "depois",
    "corrigido", "alertas"} ou {"erro"}; nunca levanta."""
    c = config(cfg)
    if not c["audio_qa_enabled"]:
        return {}
    try:
        antes = medir(mp4)
        alertas_mix = []
        if fala is not None and fala.exists():
            aviso = fala_x_mix(fala, antes, c)
            alertas_mix = [aviso] if aviso else []
        alertas = avaliar(antes, c)
        depois, corrigido = None, False
        if alertas and c["audio_qa_auto_fix"]:
            corrigir(mp4, antes, c)
            depois, corrigido = medir(mp4), True
            alertas = avaliar(depois, c)
        return {"antes": antes, "depois": depois, "corrigido": corrigido,
                "alertas": alertas + alertas_mix}
    except SemAudio:
        return {}
    except Exception as exc:  # noqa: BLE001 — conferência não derruba o corte
        return {"erro": str(exc)[:300]}


def linha_log(r: dict) -> str:
    """Uma linha para o log do trabalho."""
    if not r:
        return ""
    if "erro" in r:
        return f"áudio: não deu para conferir ({r['erro']})"
    if r["corrigido"]:
        a, d = r["antes"], r["depois"]
        txt = f"áudio corrigido: {a['i']:.1f} → {d['i']:.1f} LUFS, TP {a['tp']:+.1f} → {d['tp']:+.1f}"
    else:
        txt = f"áudio: {resumo(r['antes'])}"
    return txt + (" ⚠️ " + "; ".join(r["alertas"]) if r["alertas"] else " ✅")
