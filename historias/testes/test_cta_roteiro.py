"""Testes do CTA falado do canal como instrução do roteirista (historia.instrucao_final / sistema_escrita).
O texto do CTA (config "cta") vai no prompt enviado ao DeepSeek e o modelo escreve o convite no fim da narração;
o pipeline não acrescenta mais nada à fala. Nenhuma chamada real: o modelo e o banco são trocados por mocks.

    .venv\\Scripts\\python -m unittest testes.test_cta_roteiro -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_RAIZ = tempfile.mkdtemp(prefix="estudio_cta_roteiro_")
os.environ["ESTUDIO_RAIZ"] = _RAIZ
os.environ["ESTUDIO_ENV"] = str(Path(_RAIZ) / ".env")

from estudio import canais, clientes, historia, montagem, narracao, pipeline  # noqa: E402


def canal_com(cta):
    """Canal como o banco devolve: configuração padrão mesclada com o que o canal define."""
    extra = {} if cta is ... else {"cta": cta}
    return {"nome": "Garras no Telhado", "idioma": "pt-BR", "formato": "short",
            "descricao": "Histórias de terror curtas.", "config": canais.mesclar(canais.CONFIG_PADRAO, extra)}


def campo_narracao(prompt: str) -> str:
    """O trecho do prompt que descreve o campo narracao (até o campo titulo)."""
    ini = prompt.index("- narracao:")
    return prompt[ini:prompt.index("- titulo:", ini)]


class InstrucaoDoCTA(unittest.TestCase):
    def test_cta_padrao_entra_no_campo_narracao(self):
        trecho = campo_narracao(historia.sistema_escrita(canal_com(...)))
        self.assertIn('"Siga para mais histórias."', trecho)
        self.assertIn("Depois da última frase da história", trecho)
        self.assertIn("convite curto e natural", trecho)
        self.assertIn("sem inventar outra chamada", trecho)
        self.assertIn("não repita", trecho)

    def test_cta_do_canal_substitui_o_padrao(self):
        prompt = historia.sistema_escrita(canal_com("Se inscreva e ative o sininho."))
        self.assertIn('"Se inscreva e ative o sininho."', prompt)
        self.assertNotIn("Siga para mais histórias.", prompt)

    def test_espacos_e_quebras_de_linha_do_cta_sao_normalizados(self):
        instr = historia.instrucao_final(canal_com("  Siga\n  para   mais\thistórias.  "))
        self.assertIn('"Siga para mais histórias."', instr)

    def test_canal_sem_cta_nao_recebe_instrucao_de_convite(self):
        for vazio in ("", "   ", None):
            with self.subTest(cta=vazio):
                prompt = historia.sistema_escrita(canal_com(vazio))
                self.assertNotIn("CTA", prompt)
                self.assertNotIn("Depois da última frase da história", prompt)
                self.assertIn("Termine na última frase da história, sem convite", campo_narracao(prompt))

    def test_programa_nao_acrescenta_mais_o_convite(self):
        self.assertNotIn("o programa acrescenta depois", historia.sistema_escrita(canal_com(...)))

    def test_prompt_igual_em_todas_as_chamadas_do_canal(self):
        # O prefixo precisa ser idêntico para o cache do provedor valer (ganchos, escrita e reescrita).
        c = canal_com(...)
        self.assertEqual(historia.sistema_escrita(c), historia.sistema_escrita(c))
        self.assertEqual(historia.mensagens_gancho("assunto", c)[0]["content"], historia.sistema_escrita(c))

    def test_instrucao_vai_ao_modelo_na_escrita_e_na_reescrita(self):
        c = canal_com("Siga para mais histórias.")
        projeto = {"id": 1, "assunto": "casa velha"}
        resposta = {"titulo": "t", "descricao_youtube": "d", "narracao": "Gancho. Corpo da história."}
        h = historia.normalizar_narrativa(resposta)
        with mock.patch.object(historia, "_checar_teto"), mock.patch.object(historia, "_media_custo", return_value=0.0), \
                mock.patch.object(clientes, "chat", return_value=(resposta, 0.0)) as chat:
            historia.escrever(projeto, c, "Gancho.")
            historia.reescrever(projeto, c, h, ["a narração tem palavras a menos"], None)
        self.assertEqual(chat.call_count, 2)
        for chamada in chat.call_args_list:
            sistema = chamada.args[1][0]
            self.assertEqual(sistema["role"], "system")
            self.assertIn('a partir do CTA do canal ("Siga para mais histórias.")', sistema["content"])


class VideoNaoAcrescentaCTA(unittest.TestCase):
    """etapa_video usa a narração das cenas como está (o convite, se houver, já foi escrito pelo roteirista)."""

    def rodar(self, cta, cta_em_video):
        cenas = [{"n": 1, "narracao": "Primeira cena."}, {"n": 2, "narracao": "A última frase da história."}]
        h = {"cenas": cenas}
        projeto = {"id": 7, "pasta": "projetos/x", "historia": h}
        canal = canal_com(cta)
        canal["config"]["voz"] = {"provedor": "qwen", "nome": "loongjohn", "modelo": "m"}
        video = Path(_RAIZ) / "final.mp4"
        video.write_bytes(b"x" * 1024)
        vistas, eventos = [], []

        def narrar(pasta, lista, voz, pid):
            vistas.extend(c["narracao"] for c in lista)
            return {"duracao": 4.0, "cenas": [{"n": 1, "inicio": 0, "fim": 2}, {"n": 2, "inicio": 2, "fim": 4}]}

        with mock.patch.object(pipeline, "_contexto", return_value=(projeto, canal)), \
                mock.patch.object(pipeline, "obter", return_value=projeto), \
                mock.patch.object(narracao, "narrar", side_effect=narrar), \
                mock.patch.object(montagem, "montar", return_value=video), \
                mock.patch.object(montagem, "anexar_cta", return_value={"anexado": cta_em_video}), \
                mock.patch.object(pipeline.custos, "total", return_value=0.0), \
                mock.patch.object(pipeline.db, "atualizar"), mock.patch.object(pipeline.db, "executar"), \
                mock.patch.object(pipeline.db, "evento", side_effect=lambda pid, msg, nivel="info": eventos.append((nivel, msg))):
            pipeline.etapa_video(7)
        return vistas, cenas, eventos

    def test_fala_e_cenas_ficam_como_aprovadas(self):
        vistas, cenas, eventos = self.rodar("Siga para mais histórias.", cta_em_video=False)
        self.assertEqual(vistas, ["Primeira cena.", "A última frase da história."])
        self.assertEqual([c["narracao"] for c in cenas], vistas)
        self.assertFalse([e for e in eventos if "CTA" in e[1]])

    def test_aviso_quando_o_canal_tem_cta_falado_e_cta_em_video(self):
        _, _, eventos = self.rodar("Siga para mais histórias.", cta_em_video=True)
        avisos = [m for n, m in eventos if n == "aviso"]
        self.assertEqual(len(avisos), 1)
        self.assertIn("CTA falado", avisos[0])
        self.assertIn("CTA em vídeo", avisos[0])

    def test_sem_aviso_quando_o_canal_nao_tem_cta_falado(self):
        _, _, eventos = self.rodar("", cta_em_video=True)
        self.assertFalse([e for e in eventos if e[0] == "aviso"])


if __name__ == "__main__":
    unittest.main()
