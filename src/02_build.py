"""Exécute src/02_build.sql sur la source déclarée et exporte les tables en Parquet.

Le SQL porte les transformations, parce que c'est le langage fait pour manipuler
des ensembles. Python se limite à ce que le SQL fait mal : résoudre des chemins,
lire une configuration, contrôler le résultat, écrire des fichiers.

Les tables produites sont celles que consomme le modèle Power BI. Le niveau
départemental et l'indice de spécificité ne sont pas traités ici.
"""

import tomllib
from pathlib import Path

import duckdb

from provenance import verifier

# Les chemins sont résolus depuis l'emplacement du script et non depuis le
# répertoire courant, qui dépend de l'endroit d'où la commande est lancée.
RACINE = Path(__file__).resolve().parent.parent
BRUT = RACINE / "data" / "raw"
SORTIE = RACINE / "data" / "out"
CONFIG = RACINE / "config" / "sources.toml"

# Le fichier SQL est voisin de ce script, d'où un seul niveau remonté.
SQL = Path(__file__).resolve().parent / "02_build.sql"

# Liste explicite plutôt que le catalogue complet de DuckDB : le SQL crée aussi
# base_nat, table de travail qui n'a pas sa place dans le modèle Power BI.
A_EXPORTER = ["d_prenom", "d_annee", "d_sexe", "f_naissances"]


# ---------------------------------------------------------------------------
# 1. Résolution de la source. Le nom du fichier reste hors du code.
# ---------------------------------------------------------------------------
def chemin_source() -> Path:
    """Retourne le fichier source déclaré dans la configuration.

    Passer au millésime suivant se limitera à modifier config/sources.toml,
    puisque ni ce script ni le SQL ne mentionnent de nom de fichier.
    """
    # Le mode binaire est imposé par tomllib, qui décode lui-même en UTF-8.
    with CONFIG.open("rb") as f:
        config = tomllib.load(f)
    return BRUT / config["source"]["prenoms"]["fichier"]


# ---------------------------------------------------------------------------
# 2. Contrôles. Ils portent sur les pannes silencieuses, pas sur les erreurs.
# ---------------------------------------------------------------------------
def controler(con: duckdb.DuckDBPyConnection) -> None:
    """Vérifie la cohérence des tables construites.

    Les trois contrôles couvrent les trois façons dont une jointure abîme des
    données sans lever d'erreur : perdre des lignes, en dupliquer, en produire
    d'orphelines. Ils deviendront des tests pytest à l'étape suivante.
    """
    # Une jointure portant sur le prénom seul au lieu du couple (prénom, sexe)
    # dupliquerait les effectifs des prénoms mixtes, et l'écart serait négatif.
    ecart = con.sql("""
        SELECT (SELECT SUM(effectif) FROM base_nat)
             - (SELECT SUM(effectif) FROM f_naissances) AS ecart
    """).fetchone()[0]

    # Le grain déclaré de f_naissances est un couple et une année, donc ces deux
    # colonnes doivent identifier une ligne unique. Un doublon ferait double
    # compter toutes les mesures dans Power BI.
    doublons = con.sql("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id, annee FROM f_naissances
            GROUP BY 1, 2 HAVING COUNT(*) > 1)
    """).fetchone()[0]

    # Un fait sans prénom correspondant apparaîtrait dans le rapport sous une
    # ligne vide ou un total sans libellé.
    orphelins = con.sql("""
        SELECT COUNT(*) FROM f_naissances f
        ANTI JOIN d_prenom p USING (prenom_id)
    """).fetchone()[0]

    # assert convient ici parce que ces conditions relèvent de la cohérence
    # interne : leur violation signale un défaut du SQL, pas une situation que
    # l'utilisateur pourrait corriger.
    assert ecart == 0, f"Effectifs perdus à la jointure : {ecart}"
    assert doublons == 0, f"Clés dupliquées dans f_naissances : {doublons}"
    assert orphelins == 0, f"Faits sans prénom correspondant : {orphelins}"
    print("Contrôles : OK")


# ---------------------------------------------------------------------------
# 3. Orchestration. Seule fonction qui connaisse la forme du projet.
# ---------------------------------------------------------------------------
def main() -> None:
    source = chemin_source()

    # Échec explicite plutôt qu'une erreur DuckDB peu lisible, avec un message
    # qui dit quoi faire.
    if not source.exists():
        raise FileNotFoundError(f"Source absente : {source}. Lancer 01_download.py.")

    # La source doit être celle qu'a téléchargée 01_download.py, sinon les
    # tables produites ne correspondraient pas au manifeste affiché en fin de
    # rapport.
    verifier(SORTIE / "manifeste.json", source, "prenoms")

    SORTIE.mkdir(parents=True, exist_ok=True)

    # Base en mémoire, donc aucun fichier .duckdb à gérer et aucun état conservé
    # entre deux exécutions. Tout se reconstruit depuis le Parquet source.
    con = duckdb.connect()

    # Paramètre lié plutôt que concaténation dans la chaîne SQL. Un chemin
    # contenant une apostrophe casserait la requête, et sur une valeur venue de
    # l'extérieur le même défaut s'appelle une injection SQL.
    con.execute("SET VARIABLE chemin_source = ?", [source.as_posix()])

    # Le fichier reste exécutable tel quel dans un client DuckDB.
    con.execute(SQL.read_text(encoding="utf-8"))

    # Contrôler avant d'exporter. Un Parquet écrit puis découvert faux est un
    # Parquet que Power BI a déjà chargé.
    controler(con)

    for table in A_EXPORTER:
        cible = SORTIE / f"{table}.parquet"

        # Interpolation inévitable : COPY TO n'accepte qu'un littéral, et un nom
        # de table ne peut pas être un paramètre lié. Sans risque ici, les deux
        # valeurs venant du programme et non de l'extérieur.
        con.execute(f"COPY {table} TO '{cible.as_posix()}' (FORMAT PARQUET)")

        lignes = con.sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        # Alignement en colonnes : un changement de volumétrie se remarque.
        print(f"  {table:<14} {lignes:>9,} lignes  ->  {cible.name}")

    # Sans effet sur une base en mémoire, mais une base fichier resterait
    # verrouillée.
    con.close()


if __name__ == "__main__":
    main()
