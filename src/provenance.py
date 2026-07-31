"""Vérification de provenance des fichiers sources.

Le manifeste écrit par 01_download.py enregistre l'empreinte de chaque source.
Ce module permet aux étapes suivantes de contrôler qu'elles lisent bien ce
fichier-là, et non une version modifiée, tronquée ou remplacée à la main.

Sans ce contrôle, le manifeste ne sert qu'à documenter. Avec lui, il devient une
garantie : une source altérée arrête le pipeline au lieu de produire des tables
plausibles et fausses.

Ce module porte un nom sans préfixe numérique, contrairement aux scripts, pour
être importable. C'est la règle à suivre pour toute fonction partagée.
"""

import hashlib
import json
from pathlib import Path

TAILLE_BLOC = 1_048_576


def empreinte(chemin: Path) -> str:
    """Retourne l'empreinte SHA-256 du fichier, en hexadécimal.

    Lecture par blocs, pour que la mémoire occupée reste constante quelle que
    soit la taille du fichier.
    """
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(TAILLE_BLOC), b""):
            h.update(bloc)
    return h.hexdigest()


def verifier(manifeste: Path, source: Path, nom: str) -> None:
    """Compare l'empreinte du fichier à celle enregistrée dans le manifeste.

    Lève une exception si elles diffèrent. Ne fait rien si le manifeste est
    absent ou ne connaît pas cette source, pour qu'un pipeline lancé sans
    téléchargement préalable reste utilisable.
    """
    if not manifeste.exists():
        return

    sources = json.loads(manifeste.read_text(encoding="utf-8")).get("sources", [])
    enregistree = next((s for s in sources if s["nom"] == nom), None)
    if enregistree is None:
        return

    attendue = enregistree["sha256"]
    obtenue = empreinte(source)
    if obtenue != attendue:
        raise ValueError(
            f"{source.name} ne correspond pas au manifeste.\n"
            f"  attendu : {attendue}\n"
            f"  obtenu  : {obtenue}\n"
            f"Relancer 01_download.py, ou supprimer le fichier pour le "
            f"retélécharger."
        )
