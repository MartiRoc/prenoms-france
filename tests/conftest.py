"""Fixtures partagées par les fichiers de tests.

pytest découvre ce fichier automatiquement. Les fixtures qu'il définit sont
disponibles dans tous les fichiers de tests du dossier, sans import, ce qui évite
de répéter l'ouverture de connexion dans chacun.
"""

import tomllib
from pathlib import Path

import duckdb
import pytest

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "data" / "out"
BRUT = RACINE / "data" / "raw"
CONFIG = RACINE / "config" / "sources.toml"

# Chaque table exportée, avec le script qui la produit. Le rattachement sert au
# message d'erreur : un test qui échoue doit dire quelle commande relancer.
PRODUCTEURS = {
    "d_prenom": "02_build.py",
    "d_annee": "02_build.py",
    "d_sexe": "02_build.py",
    "f_naissances": "02_build.py",
    "profil": "03_traits.py",
    "traits": "03_traits.py",
    "d_archetype": "04_archetypes.py",
    "prenom_archetype": "04_archetypes.py",
    "profil_archetype": "04_archetypes.py",
    "d_departement": "05_geographie.py",
    "f_specificite": "05_geographie.py",
}


@pytest.fixture(scope="session")
def config() -> dict:
    """Relit la configuration, pour comparer les données à ce qui a été déclaré."""
    with CONFIG.open("rb") as f:
        return tomllib.load(f)


@pytest.fixture(scope="session")
def con(config) -> duckdb.DuckDBPyConnection:
    """Ouvre une connexion avec une vue par table exportée, plus la source.

    La portée session construit les vues une fois pour toute la suite. Les vues
    ne chargent rien en mémoire, donc le coût se limite à la lecture des colonnes
    réellement interrogées par chaque test.
    """
    # Échec et non saut de test : une suite sautée s'affiche en vert, ce qui est
    # pire qu'un rouge. Le message regroupe les manques par script à relancer.
    manquants = {}
    for table, script in PRODUCTEURS.items():
        if not (SORTIE / f"{table}.parquet").exists():
            manquants.setdefault(script, []).append(table)
    if manquants:
        details = "; ".join(f"{s} pour {', '.join(t)}" for s, t in manquants.items())
        pytest.fail(f"Tables absentes. Lancer {details}.")

    # Toutes les tables sont exposées d'un coup, plutôt qu'à la demande de chaque
    # fichier de tests. Le prix est que la suite exige un pipeline complet : les
    # tests du socle échouent si l'étape 4 n'a pas tourné. C'est assumé, un
    # pipeline dont on ne teste qu'une moitié n'apprend pas grand-chose.
    connexion = duckdb.connect()
    for table in PRODUCTEURS:
        chemin = (SORTIE / f"{table}.parquet").as_posix()
        connexion.execute(f"CREATE VIEW {table} AS SELECT * FROM '{chemin}'")

    # La source est exposée aussi, sinon aucun test ne pourrait relier les
    # sorties à l'origine des données. C'est le seul moyen de détecter une
    # jointure qui perd des lignes.
    source = (BRUT / config["source"]["prenoms"]["fichier"]).as_posix()
    connexion.execute(f"CREATE VIEW source AS SELECT * FROM '{source}'")
    return connexion


# La portée session est possible parce qu'aucun test n'écrit : ils lisent tous des
# Parquet déjà produits. Une suite qui modifierait les données exigerait une
# fixture par test, pour que l'ordre d'exécution n'ait aucune influence.
@pytest.fixture(scope="session")
def scalaire(con):
    """Retourne une fonction qui exécute une requête d'une seule valeur.

    Une fixture qui rend une fonction plutôt qu'une valeur. Elle évite de répéter
    fetchone()[0] dans chaque test, où cette mécanique masque la question posée.
    """
    def executer(requete: str):
        return con.sql(requete).fetchone()[0]

    return executer
