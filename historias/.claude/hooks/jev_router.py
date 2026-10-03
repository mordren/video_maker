"""Hook UserPromptSubmit: o Jev (via OpenRouter Decisions API) classifica o pedido e sugere o modelo.

O hook não troca o modelo da sessão (o Claude Code não permite). Ele injeta no contexto uma recomendação:
a sessão fica no Haiku e, quando o Jev aponta um modelo maior, o trabalho pesado vai para um subagente com esse modelo.
O Jev recebe o pedido mais o contexto do trabalho (últimas mensagens da conversa, git status, diff, commits).
Cada decisão vai para jev_router.log (uma linha JSON) para calibrar depois.
Qualquer falha (sem chave, sem rede, timeout, resposta estranha) deixa o prompt passar sem alteração.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

URL = "https://openrouter.ai/api/alpha/decisions"
MODELO_JEV = os.getenv("MODELO_JUIZ", "typesafe/jev-1.13")
TIMEOUT = 12
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev_router.log")

# Errar para baixo (difícil no Haiku) dá retrabalho; errar para cima só custa dinheiro.
# Por isso "haiku" só vale com confiança alta; sem ela, cai para o sonnet.
CONFIANCA_MINIMA = {"haiku": 0.8, "sonnet": 0.5, "opus": 0.5}
FALLBACK = "sonnet"

NIVEIS = {
    "haiku": "pergunta curta, leitura ou busca simples, edição pequena e localizada em um lugar só, comando de rotina, explicação rápida",
    "sonnet": "implementar ou alterar uma funcionalidade, depurar um erro, refatorar alguns arquivos, escrever texto com regras",
    "opus": "mudança grande em vários módulos, decisão de arquitetura, bug difícil de causa desconhecida, raciocínio longo e delicado",
}


def _git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=4,
                           encoding="utf-8", errors="replace")
        return r.stdout.strip()[:2500]
    except Exception:
        return ""


def _texto(conteudo):
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return " ".join(b.get("text", "") for b in conteudo if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _conversa(caminho, quantas=6, limite=700):
    """Últimas mensagens de texto do usuário e do assistente na transcrição da sessão."""
    if not caminho or not os.path.exists(caminho):
        return []
    saida = []
    try:
        with open(caminho, encoding="utf-8", errors="replace") as f:
            linhas = f.readlines()[-400:]
        for linha in linhas:
            try:
                e = json.loads(linha)
            except ValueError:
                continue
            papel = e.get("type")
            if papel not in ("user", "assistant") or e.get("isSidechain"):
                continue
            t = _texto((e.get("message") or {}).get("content")).strip()
            if t and not t.startswith("<"):
                saida.append({"papel": "usuario" if papel == "user" else "assistente", "texto": t[:limite]})
    except Exception:
        return []
    return saida[-quantas:]


def _contexto(entrada):
    cwd = entrada.get("cwd") or os.getcwd()
    return {
        "projeto": os.path.basename(cwd),
        "ultimas_mensagens_da_conversa": _conversa(entrada.get("transcript_path")),
        "git_status": _git(cwd, "status", "--short"),
        "git_diff_resumo": _git(cwd, "diff", "--stat", "HEAD"),
        "ultimos_commits": _git(cwd, "log", "--oneline", "-5"),
    }


def _registrar(dados):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(dados, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    try:
        entrada = json.load(sys.stdin)
        prompt = (entrada.get("prompt") or "").strip()
        chave = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not prompt or prompt.startswith("/") or not chave:
            return

        corpo = {
            "model": MODELO_JEV,
            "state": {"pedido_do_usuario": prompt[:6000], "contexto_do_trabalho": _contexto(entrada)},
            "questions": {
                "nivel": {
                    "type": "choice",
                    "instructions": (
                        "Qual o menor modelo de IA que resolve bem o pedido do usuário a um assistente de programação? "
                        "Considere o contexto: um pedido curto pode depender de muitas alterações em andamento "
                        "ou de uma conversa anterior complexa."
                    ),
                    "criteria": NIVEIS,
                }
            },
        }
        req = urllib.request.Request(
            URL, data=json.dumps(corpo).encode("utf-8"),
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
        )
        inicio = time.time()
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.load(r)

        r = (resp.get("answers") or {}).get("nivel") or {}
        escolha, conf = r.get("choice"), float(r.get("confidence") or 0)
        if escolha not in NIVEIS:
            return
        final = escolha if conf >= CONFIANCA_MINIMA[escolha] else FALLBACK

        _registrar({"quando": time.strftime("%Y-%m-%d %H:%M:%S"), "sessao": entrada.get("session_id"),
                    "prompt": prompt[:300], "jev": escolha, "confianca": round(conf, 3),
                    "probabilidades": r.get("probabilities"), "recomendado": final,
                    "segundos": round(time.time() - inicio, 1)})

        if final == "haiku":
            texto = (f"[Jev] Pedido leve (confiança {conf:.0%}): resolva direto nesta sessão, "
                     "sem delegar a um modelo maior.")
        else:
            nota = "" if final == escolha else f" (o Jev apontou {escolha} com só {conf:.0%}; subi por segurança)"
            texto = (f"[Jev] Pedido de nível {final}{nota}: delegue a execução a um subagente "
                     f"com model=\"{final}\" (ferramenta Agent) e apenas resuma o resultado ao usuário.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": texto}}))
    except Exception:
        return


if __name__ == "__main__":
    main()
