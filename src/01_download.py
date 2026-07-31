"""Télécharge les sources déclarées dans config/sources.toml et écrit un manifeste.

Le manifeste consigne la provenance de chaque fichier : millésime, URL, taille,
empreinte et date de téléchargement. C'est lui qui alimentera l'affichage de
fraîcheur du rapport.

Le script est idempotent. Un fichier déjà présent n'est pas retéléchargé, et le
manifeste n'est réécrit que si son contenu change. Pour forcer un nouveau
téléchargement, supprimer le fichier dans data/raw.
"""

import hashlib
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import requests

# Les chemins sont résolus depuis l'emplacement du script et non depuis le
# répertoire courant, qui dépend de l'endroit d'où la commande est lancée.
RACINE = Path(__file__).resolve().parent.parent
BRUT = RACINE / "data" / "raw"
SORTIE = RACINE / "data" / "out"
CONFIG = RACINE / "config" / "sources.toml"
MANIFESTE = SORTIE / "manifeste.json"

# Lecture et écriture par blocs de 1 MiB. La mémoire occupée reste constante
# quelle que soit la taille du fichier traité.
TAILLE_BLOC = 1_048_576


# ---------------------------------------------------------------------------
# 1. Empreinte du contenu. Calcul pur, sans effet de bord, donc testable seul.
# ---------------------------------------------------------------------------
def empreinte(chemin: Path) -> str:
    """Retourne l'empreinte SHA-256 du fichier, en hexadécimal.

    Sert de détecteur de changement : si la source est réactualisée à la même
    URL, l'empreinte le révèle. Un hachage n'est pas un chiffrement, il ne se
    renverse pas.
    """
    h = hashlib.sha256()
    # Le mode binaire est obligatoire : un décodage texte modifierait les
    # octets, donc l'empreinte.
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(TAILLE_BLOC), b""):
            h.update(bloc)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 2. Téléchargement. Effet de bord assumé, aucune décision prise ici.
# ---------------------------------------------------------------------------
def telecharger(url: str, cible: Path) -> None:
    """Télécharge url vers cible. Lève une exception si la requête échoue."""
    # L'écriture passe d'abord par un fichier temporaire. Sans cela, un
    # téléchargement interrompu laisserait un fichier tronqué que les
    # exécutions suivantes considéreraient comme valide.
    temporaire = cible.with_name(cible.name + ".part")

    # stream=True ne récupère que les en-têtes, le corps sera lu par blocs.
    # timeout=60 évite un blocage indéfini si le serveur cesse de répondre.
    with requests.get(url, stream=True, timeout=60) as r:
        # Vérification placée avant toute ouverture de fichier : une réponse
        # 404 contient une page HTML valide, qui serait sinon écrite sur le
        # disque sous un nom de Parquet.
        r.raise_for_status()
        with temporaire.open("wb") as f:
            for bloc in r.iter_content(chunk_size=TAILLE_BLOC):
                f.write(bloc)

    # Le renommage est atomique sur un même disque, donc le fichier final
    # n'existe jamais dans un état partiel. Il a lieu après fermeture, car
    # Windows verrouille un fichier encore ouvert.
    temporaire.replace(cible)


# ---------------------------------------------------------------------------
# 3. Relecture du manifeste précédent, pour conserver les dates déjà connues.
# ---------------------------------------------------------------------------
def sources_precedentes(chemin: Path) -> dict:
    """Indexe par nom les sources du manifeste existant, vide au premier appel."""
    if not chemin.exists():
        return {}
    donnees = json.loads(chemin.read_text(encoding="utf-8"))
    return {s["nom"]: s for s in donnees.get("sources", [])}


# ---------------------------------------------------------------------------
# 4. Orchestration. Seule fonction qui connaisse la forme du projet.
# ---------------------------------------------------------------------------
def main() -> None:
    # Les dossiers sont créés par le script et non versionnés par Git, ce qui
    # fonctionne aussi pour quelqu'un qui vient de cloner le dépôt.
    BRUT.mkdir(parents=True, exist_ok=True)
    SORTIE.mkdir(parents=True, exist_ok=True)

    # Le mode binaire est imposé par tomllib, qui décode lui-même en UTF-8.
    with CONFIG.open("rb") as f:
        config = tomllib.load(f)

    precedent = sources_precedentes(MANIFESTE)

    # Horodatage en UTC : comparable entre machines, insensible au changement
    # d'heure.
    horodatage = datetime.now(UTC).isoformat(timespec="seconds")

    sources = []

    # Une source par bloc [source.xxx] dans le TOML. Ajouter une source ne
    # demande donc aucune modification du code.
    for nom, src in config["source"].items():
        cible = BRUT / src["fichier"]

        if cible.exists():
            print(f"{nom} : déjà présent, téléchargement ignoré")
            date_telechargement = None
        else:
            print(f"{nom} : téléchargement...")
            telecharger(src["url"], cible)
            date_telechargement = horodatage

        # Taille et empreinte sont mesurées sur le fichier réel, alors que les
        # autres champs sont recopiés de la configuration.
        octets = cible.stat().st_size
        sha = empreinte(cible)

        # La date précédente n'est reprise que si le contenu est identique. Une
        # empreinte différente rend l'ancienne date fausse.
        ancien = precedent.get(nom)
        if date_telechargement is None and ancien and ancien.get("sha256") == sha:
            date_telechargement = ancien.get("date_telechargement")

        sources.append({
            "nom": nom,
            "millesime": src["millesime"],
            "url": src["url"],
            "fichier": src["fichier"],
            "octets": octets,
            "sha256": sha,
            "date_telechargement": date_telechargement,
        })
        print(f"  {cible.name}, {octets / 1e6:.1f} Mo")

    manifeste = {"sources": sources}

    # La boucle se contente d'accumuler, l'écriture a lieu une seule fois. Un
    # JSON est un document dont la syntaxe se referme, il ne s'écrit pas par
    # fragments, et un échec en cours de route ne laisse aucun fichier partiel.
    nouveau = json.dumps(manifeste, indent=2, ensure_ascii=False) + "\n"

    # Comparer avant d'écrire évite de signaler le manifeste comme modifié à
    # chaque exécution, et donc de committer du bruit.
    if not MANIFESTE.exists() or MANIFESTE.read_text(encoding="utf-8") != nouveau:
        # L'encodage et le saut de ligne sont explicites : sous Windows, les
        # valeurs par défaut produiraient un fichier illisible ailleurs et des
        # fins de ligne contraires au .gitattributes.
        MANIFESTE.write_text(nouveau, encoding="utf-8", newline="\n")
        print("\nManifeste mis à jour.")
    else:
        print("\nManifeste inchangé.")


if __name__ == "__main__":
    main()
