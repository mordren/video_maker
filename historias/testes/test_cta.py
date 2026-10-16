"""Testes do CTA em vídeo por canal (estudio/cta.py). Rodam com o ffmpeg do sistema e dados numa pasta temporária:

    .venv\\Scripts\\python -m unittest testes.test_cta -v
"""
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_RAIZ = tempfile.mkdtemp(prefix="estudio_cta_")
os.environ["ESTUDIO_RAIZ"] = _RAIZ
os.environ["ESTUDIO_ENV"] = str(Path(_RAIZ) / ".env")

from estudio import config, cta  # noqa: E402


def gerar(destino: Path, tam: str, fps: str, dur: float, audio: str | None, extra: list[str] | None = None):
    """Vídeo de teste (testsrc) com áudio opcional: 'stereo48', 'mono44' ou None."""
    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc=size={tam}:rate={fps}:duration={dur}"]
    if audio:
        taxa, canais = (48000, 2) if audio == "stereo48" else (44100, 1)
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={taxa}:duration={dur}", "-ac", str(canais),
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *(extra or []), str(destino)]
    subprocess.run(cmd, check=True)


class TestesCTA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pasta = Path(tempfile.mkdtemp(prefix="cta_videos_"))
        cls.principal = cls.pasta / "principal.mp4"
        gerar(cls.principal, "270x480", "30", 4.0, "stereo48")
        cls.cta_igual = cls.pasta / "cta_igual.mp4"
        gerar(cls.cta_igual, "270x480", "30", 2.0, "stereo48")
        cls.cta_largo_mudo = cls.pasta / "cta_largo_mudo.mp4"   # 16:9, 25 fps, sem áudio
        gerar(cls.cta_largo_mudo, "320x180", "25", 3.0, None)
        cls.cta_mono = cls.pasta / "cta_mono.mp4"               # 29,97 fps, mono 44,1 kHz, tamanho ímpar
        gerar(cls.cta_mono, "320x180", "30000/1001", 2.5, "mono44")

    def setUp(self):
        cta.remover("garras")
        self.trabalho = self.pasta / "copia.mp4"
        self.trabalho.write_bytes(self.principal.read_bytes())

    def tearDown(self):
        cta.remover("garras")
        for p in self.pasta.glob("copia*"):
            p.unlink()

    # ---------------------------------------------------------------- nomes e armazenamento
    def test_nome_de_canal_invalido(self):
        for ruim in ("", "../x", "a/b", "a\\b", "..", "a.b", "x" * 80, "ação"):
            with self.assertRaises(cta.ErroCTA, msg=ruim):
                cta.chave(ruim)
        self.assertEqual(cta.chave(" BR_semfim "), "br_semfim")

    def test_chave_do_canal_prefere_publicador_e_cai_no_slug(self):
        self.assertEqual(cta.chave_do_canal({"slug": "garras-no-telhado", "config": {"publicador_canal": "garras"}}), "garras")
        self.assertEqual(cta.chave_do_canal({"slug": "garras-no-telhado", "config": {}}), "garras-no-telhado")
        self.assertEqual(cta.chave_do_canal({"slug": "", "config": {"publicador_canal": "../x"}}), "")

    def test_guardar_substituir_e_remover(self):
        self.assertFalse(cta.info("garras")["existe"])
        i = cta.guardar("garras", self.cta_igual, "meu cta.mp4")
        self.assertTrue(i["existe"])
        self.assertEqual(i["nome_original"], "meu cta.mp4")
        self.assertAlmostEqual(i["duracao_s"], 2.0, delta=0.15)
        self.assertEqual((i["largura"], i["altura"]), (270, 480))
        self.assertTrue(i["audio"])
        i2 = cta.guardar("garras", self.cta_largo_mudo, "outro.mp4")   # troca
        self.assertEqual(i2["nome_original"], "outro.mp4")
        self.assertFalse(i2["audio"])
        self.assertEqual([p.name for p in cta.pasta().iterdir() if p.suffix == ".tmp"], [])
        self.assertEqual([c["canal"] for c in cta.listar()], ["garras"])
        self.assertTrue(cta.remover("garras"))
        self.assertFalse(cta.existe("garras"))
        self.assertFalse(cta.remover("garras"))

    def test_rejeita_arquivos_ruins(self):
        with self.assertRaises(cta.ErroCTA):                       # extensão
            cta.receber("garras", io.BytesIO(self.cta_igual.read_bytes()), "cta.mov")
        with self.assertRaises(cta.ErroCTA):                       # lixo com extensão certa
            cta.receber("garras", io.BytesIO(b"nao sou video" * 100), "cta.mp4")
        with self.assertRaises(cta.ErroCTA):                       # vazio
            cta.receber("garras", io.BytesIO(b""), "cta.mp4")
        longo = self.pasta / "longo.mp4"
        gerar(longo, "160x90", "10", 61.0, None, ["-preset", "ultrafast"])
        with self.assertRaises(cta.ErroCTA) as e:                  # mais de 60 s
            cta.receber("garras", io.BytesIO(longo.read_bytes()), "longo.mp4")
        self.assertIn("60", str(e.exception))
        som = self.pasta / "som.mp4"                               # só áudio
        subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=duration=2", "-c:a", "aac", str(som)], check=True)
        with self.assertRaises(cta.ErroCTA):
            cta.receber("garras", io.BytesIO(som.read_bytes()), "som.mp4")
        with self.assertRaises(cta.ErroCTA):                       # canal inválido
            cta.receber("../fora", io.BytesIO(self.cta_igual.read_bytes()), "cta.mp4")
        self.assertFalse(cta.existe("garras"))
        self.assertFalse(list(cta.pasta().glob("*.tmp")))

    def test_limite_de_tamanho(self):
        antigo, cta.MAX_BYTES = cta.MAX_BYTES, 1024
        try:
            with self.assertRaises(cta.ErroCTA):
                cta.receber("garras", io.BytesIO(self.cta_igual.read_bytes()), "cta.mp4")
        finally:
            cta.MAX_BYTES = antigo
        self.assertFalse(list(cta.pasta().glob("*.tmp")))

    # ---------------------------------------------------------------- anexar
    def conferir(self, r, dur_cta):
        s = cta.sondar(self.trabalho)
        self.assertTrue(r["anexado"])
        self.assertAlmostEqual(s["video"]["duracao"], 4.0 + dur_cta, delta=0.15)
        self.assertEqual((s["video"]["largura"], s["video"]["altura"]), (270, 480))
        self.assertEqual(s["video"]["fps"], "30/1")
        self.assertIsNotNone(s["audio"])
        self.assertEqual((s["audio"]["taxa"], s["audio"]["canais"], s["audio"]["codec"]), (48000, 2, "aac"))
        self.assertAlmostEqual(s["duracao"], s["video"]["duracao"], delta=0.2)   # áudio e vídeo do mesmo tamanho
        return s

    def test_sem_cta_nada_muda(self):
        antes = self.trabalho.read_bytes()
        r = cta.anexar(self.trabalho, "garras")
        self.assertEqual((r["anexado"], r["motivo"]), (False, "sem_cta"))
        self.assertEqual(self.trabalho.read_bytes(), antes)

    def test_anexa_cta_igual(self):
        cta.guardar("garras", self.cta_igual, "a.mp4")
        r = cta.anexar(self.trabalho, "garras", crf=21, maxrate="3M")
        self.conferir(r, 2.0)
        self.assertFalse(list(self.pasta.glob("*.cta-tmp.mp4")))

    def test_anexa_cta_com_resolucao_e_fps_diferentes_e_sem_audio(self):
        cta.guardar("garras", self.cta_largo_mudo, "b.mp4")
        r = cta.anexar(self.trabalho, "garras")
        self.conferir(r, 3.0)
        self.assertTrue(r["cta_sem_audio"])

    def test_anexa_cta_mono_29_97_fps(self):
        cta.guardar("garras", self.cta_mono, "c.mp4")
        r = cta.anexar(self.trabalho, "garras")
        self.conferir(r, 2.5)

    def test_principal_sem_audio_e_cta_com_audio(self):
        mudo = self.pasta / "copia_mudo.mp4"
        gerar(mudo, "270x480", "30", 3.0, None)
        cta.guardar("garras", self.cta_igual, "a.mp4")
        r = cta.anexar(mudo, "garras")
        s = cta.sondar(mudo)
        self.assertTrue(r["anexado"])
        self.assertAlmostEqual(s["video"]["duracao"], 5.0, delta=0.15)
        self.assertIsNotNone(s["audio"])

    def test_nao_duplica_na_retentativa(self):
        cta.guardar("garras", self.cta_igual, "a.mp4")
        r1 = cta.anexar(self.trabalho, "garras")
        d1 = cta.sondar(self.trabalho)["video"]["duracao"]
        r2 = cta.anexar(self.trabalho, "garras")
        self.assertTrue(r1["anexado"])
        self.assertEqual((r2["anexado"], r2["motivo"]), (False, "ja_anexado"))
        self.assertAlmostEqual(cta.sondar(self.trabalho)["video"]["duracao"], d1, delta=0.01)
        # vídeo montado de novo no mesmo caminho (como o final.mp4) volta a receber o CTA
        self.trabalho.write_bytes(self.principal.read_bytes())
        self.assertTrue(cta.anexar(self.trabalho, "garras")["anexado"])
        self.assertAlmostEqual(cta.sondar(self.trabalho)["video"]["duracao"], 6.0, delta=0.15)

    def test_saida_separada_preserva_o_original(self):
        cta.guardar("garras", self.cta_igual, "a.mp4")
        saida = self.pasta / "copia_saida.mp4"
        antes = self.trabalho.read_bytes()
        r = cta.anexar(self.trabalho, "garras", saida)
        self.assertTrue(r["anexado"])
        self.assertEqual(self.trabalho.read_bytes(), antes)
        self.assertAlmostEqual(cta.sondar(saida)["video"]["duracao"], 6.0, delta=0.15)

    def test_falha_deixa_o_original_intacto(self):
        cta.guardar("garras", self.cta_igual, "a.mp4")
        cta.caminho("garras").write_bytes(b"corrompido")           # CTA estragado no disco
        antes = self.trabalho.read_bytes()
        with self.assertRaises(cta.ErroCTA):
            cta.anexar(self.trabalho, "garras")
        self.assertEqual(self.trabalho.read_bytes(), antes)
        self.assertFalse(list(self.pasta.glob("*.cta-tmp.mp4")))


if __name__ == "__main__":
    unittest.main()
