"""Modelo do LR-ASD (Liao et al., IJCV 2025 — https://github.com/Junhua-Liao/LR-ASD,
commit 1b6dcd2, licença MIT em LICENSE): quem está falando, pela boca + voz.

Só as partes de inferência (Model, Encoder, Classifier, lossAV), sem mudança
além dos imports relativos. O uso fica em fase1/falante_ativo.py; o peso
(finetuning_TalkSet.model, 3,4 MB) fica em fase1/models/.
"""
