"""Construit les profils de trajectoire et les traits de forme des prénoms éligibles.

Lit les tables produites par 02_build.py, écrit profil.parquet et traits.parquet,
et affiche la distribution de chaque trait.

La classification n'est pas ici, volontairement. Regarder les traits avant de
classer évite de chercher des groupes dans des mesures qui ne séparent rien, et
c'est la distribution affichée en fin d'exécution qui servira à décider du nombre
d'archétypes.
"""

import tomllib
from pathlib import Path

import duckdb

# Les chemins sont résolus depuis l'emplacement du script et non depuis le
# répertoire courant, qui dépend de l'endroit d'où la commande est lancée.
RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "data" / "out"
CONFIG = RACINE / "config" / "sources.toml"
SQL = Path(__file__).resolve().parent / "03_traits.sql"

# Tables produites par l'étape précédente. Cette étape lit les Parquet exportés
# plutôt que de reconstruire le socle, ce qui rend la dépendance explicite et
# permet de relancer 03 sans relancer 02.
ENTREES = ["d_prenom", "d_annee", "f_naissances"]

# profil sert à l'étape suivante, qui a besoin des courbes pour calculer les
# formes moyennes de chaque archétype. traits porte les mesures à classer.
A_EXPORTER = ["profil", "traits"]

# Prénoms de contrôle, choisis pour couvrir des trajectoires connues et
# nettement différentes. Les afficher à chaque exécution permet de repérer qu'un
# trait s'est mis à raconter n'importe quoi, ce qu'une distribution agrégée ne
# montrerait pas.
TEMOINS = ["Marie (F)", "Jean (M)", "Camille (M)", "Camille (F)",
           "Kevin (M)", "Louise (F)", "Emma (F)", "Léo (M)"]


# ---------------------------------------------------------------------------
# 1. Préparation de la session
# ---------------------------------------------------------------------------
def configuration() -> dict:
    """Retourne la configuration du projet, seuil d'éligibilité compris."""
    with CONFIG.open("rb") as f:
        return tomllib.load(f)


def preparer(con: duckdb.DuckDBPyConnection, config: dict) -> None:
    """Expose les tables de l'étape précédente et fixe les paramètres du SQL."""
    for table in ENTREES:
        chemin = SORTIE / f"{table}.parquet"

        # Échec explicite avec le nom du script à lancer, plutôt qu'une erreur
        # DuckDB sur un fichier introuvable.
        if not chemin.exists():
            raise FileNotFoundError(f"{chemin} absent. Lancer 02_build.py.")

        # Une vue et non une copie : DuckDB lit le Parquet à la demande, en ne
        # touchant que les colonnes réellement interrogées.
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM '{chemin.as_posix()}'")

    # Le seuil et le millésime viennent de la configuration et passent par des
    # paramètres liés. Aucune de ces deux valeurs n'apparaît dans le SQL, qui
    # reste ainsi valable pour un autre millésime ou un autre seuil.
    con.execute("SET VARIABLE seuil = ?", [config["archetypes"]["seuil_naissances"]])
    millesime = config["source"]["prenoms"]["millesime"]
    con.execute("SET VARIABLE millesime = ?", [millesime])


# ---------------------------------------------------------------------------
# 2. Description des traits, qui est la sortie utile de cette étape
# ---------------------------------------------------------------------------
def decrire(con: duckdb.DuckDBPyConnection) -> None:
    """Affiche la couverture, la distribution des traits et les témoins."""
    # La couverture répond à la question que pose le seuil : combien de prénoms
    # sont écartés, et quelle part des naissances ils représentent.
    couverture = con.sql("""
        SELECT COUNT(*) AS eligibles,
               ROUND(100.0 * SUM(naissances_totales)
                     / (SELECT SUM(naissances_totales) FROM d_prenom), 1) AS pct
        FROM eligibles
    """).fetchone()
    print(f"Prénoms retenus : {couverture[0]:,}"
          f" ({couverture[1]} % des naissances du fichier)")

    # Les quartiles disent si un trait sépare réellement. Un trait dont les
    # quartiles sont collés n'apportera rien à la classification, et vaut mieux
    # être retiré que standardisé au même poids que les autres.
    #
    # UNPIVOT retourne les colonnes en lignes, ce qui permet de décrire les cinq
    # traits d'un seul geste. La variante en cinq requêtes assemblées par
    # UNION ALL demanderait vingt lignes pour la même idée, et une modification
    # à cinq endroits le jour où une statistique s'ajoute.
    print("\nDistribution des traits")
    con.sql("""
        SELECT trait,
               ROUND(MIN(valeur), 2) AS mini,
               ROUND(QUANTILE_CONT(valeur, 0.25), 2) AS q1,
               ROUND(MEDIAN(valeur), 2) AS mediane,
               ROUND(QUANTILE_CONT(valeur, 0.75), 2) AS q3,
               ROUND(MAX(valeur), 2) AS maxi
        FROM (UNPIVOT traits
              ON annee_pic, largeur_forte, concentration, part_apres_2000, asymetrie
              INTO NAME trait VALUE valeur)
        GROUP BY trait ORDER BY trait
    """).show()

    # Le nombre de plages fortes est un comptage, pas une grandeur continue : ses
    # quartiles ne diraient rien, sa répartition si.
    print("Répartition des plages fortes et des trajectoires en cours")
    con.sql("""
        SELECT nb_periodes, COUNT(*) AS prenoms,
               COUNT(*) FILTER (WHERE en_cours) AS dont_en_cours
        FROM traits GROUP BY nb_periodes ORDER BY nb_periodes
    """).show()

    # Ce qu'il faut y voir : un pic ancien avec une plage large pour les
    # classiques, une plage courte et une concentration élevée pour les feux de
    # paille, une asymétrie proche de 1 pour les prénoms encore en montée.
    print("Témoins")
    # La liste passe par une table temporaire plutôt que par une clause IN
    # construite en Python, ce qui évite d'assembler du SQL à partir de valeurs.
    con.execute("CREATE OR REPLACE TABLE temoins AS"
                " SELECT * FROM (SELECT UNNEST(?)) AS t(libelle)", [TEMOINS])
    con.sql("""
        SELECT e.libelle_segment AS prenom, t.annee_pic, t.largeur_forte,
               t.nb_periodes, ROUND(t.concentration, 2) AS concentration,
               ROUND(t.asymetrie, 2) AS asymetrie, t.en_cours
        FROM traits t
        JOIN eligibles e USING (prenom_id)
        JOIN temoins ON temoins.libelle = e.libelle_segment
        ORDER BY t.annee_pic
    """).show()


# ---------------------------------------------------------------------------
# 3. Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    config = configuration()

    # Base en mémoire, comme aux étapes précédentes : rien à nettoyer, aucun état
    # conservé entre deux exécutions.
    con = duckdb.connect()

    preparer(con, config)

    # Le SQL construit cinq tables en cascade, des prénoms éligibles jusqu'aux
    # traits. Il n'écrit rien sur le disque, l'export est décidé plus bas.
    con.execute(SQL.read_text(encoding="utf-8"))

    # Le seul contrôle qui compte à cette étape. Une série incomplète fausserait
    # toutes les mesures de forme sans rien casser : un lissage sauterait les
    # creux, une largeur de plage compterait des années absentes. Le produit du
    # nombre de prénoms par le nombre d'années donne le compte attendu.
    attendu = con.sql("SELECT COUNT(*) * (SELECT COUNT(*) FROM d_annee)"
                      " FROM eligibles").fetchone()[0]
    obtenu = con.sql("SELECT COUNT(*) FROM profil").fetchone()[0]
    assert obtenu == attendu, (
        f"Grille incomplète : {obtenu} lignes au lieu de {attendu}")

    decrire(con)

    # Les tables intermédiaires, grille et sommets, ne sont pas exportées : elles
    # se recalculent en une seconde et n'intéressent ni l'étape suivante ni
    # Power BI.
    print()
    for table in A_EXPORTER:
        cible = SORTIE / f"{table}.parquet"
        # Interpolation sans risque : les deux valeurs viennent du programme.
        con.execute(f"COPY {table} TO '{cible.as_posix()}' (FORMAT PARQUET)")
        lignes = con.sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<14} {lignes:>9,} lignes  ->  {cible.name}")

    con.close()


if __name__ == "__main__":
    main()
