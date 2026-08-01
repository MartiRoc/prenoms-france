"""Construit la table géographique et l'indice de spécificité départementale.

Relit le fichier des prénoms, que l'étape 2 avait filtré sur le niveau national,
et le joint au Code officiel géographique pour obtenir les libellés.

Écrit d_departement.parquet, f_naissances_dep.parquet et f_specificite.parquet.

La table annuelle porte le grain le plus fin de l'étape, un couple par
département et par année. Les deux autres en dérivent, ce qui rend leur
cohérence vraie par construction plutôt que vérifiée après coup.

La table de spécificité stocke l'observé et l'attendu, pas leur rapport. Un ratio
ne s'additionne pas, donc l'indice se calcule dans Power BI comme le rapport des
sommes, ce qui reste juste à tout niveau d'agrégation.
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
SQL = Path(__file__).resolve().parent / "05_geographie.sql"

# La table intermédiaire naissances_dep n'est pas exportée : elle ne sert qu'aux
# contrôles, et f_specificite en contient déjà les effectifs.
A_EXPORTER = ["d_departement", "f_naissances_dep", "f_specificite"]


# ---------------------------------------------------------------------------
# 1. Préparation
# ---------------------------------------------------------------------------
def preparer(con: duckdb.DuckDBPyConnection) -> None:
    """Expose d_prenom et transmet au SQL le chemin des deux sources brutes.

    Cette étape est la première à lire deux sources. Elle vérifie que la section
    correspondante existe dans la configuration plutôt que de laisser DuckDB
    échouer sur un chemin vide.
    """
    with CONFIG.open("rb") as f:
        config = tomllib.load(f)

    # d_prenom porte la clé de substitution, qui doit rester la même que dans le
    # reste du modèle. La recalculer ici produirait des identifiants divergents.
    dimension = SORTIE / "d_prenom.parquet"
    if not dimension.exists():
        raise FileNotFoundError(f"{dimension} absent. Lancer 02_build.py.")
    con.execute(f"CREATE VIEW d_prenom AS SELECT * FROM '{dimension.as_posix()}'")

    # Les deux sources sont déclarées dans le TOML et passées au SQL par des
    # variables. Le nom des fichiers n'apparaît donc ni ici ni dans le SQL.
    for cle, variable in [("prenoms", "chemin_prenoms"),
                          ("departements", "chemin_departements")]:
        if cle not in config["source"]:
            raise KeyError(f"Section [source.{cle}] absente de sources.toml.")

        chemin = BRUT / config["source"][cle]["fichier"]
        if not chemin.exists():
            raise FileNotFoundError(f"{chemin} absent. Lancer 01_download.py.")

        verifier(SORTIE / "manifeste.json", chemin, cle)
        con.execute(f"SET VARIABLE {variable} = ?", [chemin.as_posix()])


# ---------------------------------------------------------------------------
# 2. Contrôles propres à cette étape
# ---------------------------------------------------------------------------
def controler(con: duckdb.DuckDBPyConnection) -> None:
    """Vérifie la jointure géographique et la cohérence de l'indice.

    Aucun de ces contrôles ne porte sur une valeur d'indice : ils portent sur les
    conditions qui rendent l'indice calculable, la correspondance des codes, la
    conservation des effectifs et la cohérence des marges.
    """
    # Un code présent d'un seul côté signalerait un décalage de millésime entre
    # le fichier des prénoms et le Code officiel géographique.
    sans_libelle = con.sql("""
        SELECT COUNT(*) FROM (SELECT DISTINCT dep_code FROM naissances_dep)
        ANTI JOIN d_departement USING (dep_code)
    """).fetchone()[0]

    # Un couple présent au niveau départemental et absent du niveau national
    # serait une incohérence de la source, pas de notre traitement.
    perdues = con.sql("""
        SELECT (SELECT SUM(observe) FROM naissances_dep)
             - (SELECT SUM(observe) FROM f_specificite)
    """).fetchone()[0]

    # Les trois marges du calcul doivent redonner le même total. C'est ce qui
    # valide les clauses PARTITION BY de l'indice : une partition mal posée
    # produirait des attendus plausibles et faux.
    marges = con.sql("""
        SELECT (SELECT SUM(observe) FROM naissances_dep) AS total,
               (SELECT SUM(t) FROM (SELECT SUM(observe) AS t FROM naissances_dep
                                    GROUP BY dep_code)) AS par_departement,
               (SELECT SUM(t) FROM (SELECT SUM(observe) AS t FROM naissances_dep
                                    GROUP BY prenom, sexe_code)) AS par_prenom
    """).fetchone()

    # L'attendu total reste inférieur à l'observé total, et l'écart mesure la
    # part creuse de la table. L'identité entre les deux ne vaut que sur la
    # grille complète des 1,9 million de couples possibles, alors que seules les
    # 258 000 combinaisons réellement observées sont stockées. La borne à 10 %
    # signalerait une table devenue anormalement creuse, par exemple si un
    # millésime cessait de publier les petits effectifs.
    creux = con.sql("""
        SELECT (SUM(observe) - SUM(attendu)) / SUM(observe) FROM f_specificite
    """).fetchone()[0]

    negatifs = con.sql("""
        SELECT COUNT(*) FROM f_specificite WHERE attendu <= 0 OR observe <= 0
    """).fetchone()[0]

    # Les deux grains décrivent les mêmes naissances. Un écart signalerait que
    # l'agrégation sur l'année a perdu des lignes en chemin.
    ecart_grain = con.sql("""
        SELECT (SELECT SUM(effectif) FROM f_naissances_dep)
             - (SELECT SUM(observe) FROM naissances_dep)
    """).fetchone()[0]

    # Le total stocké dans la dimension doit redonner celui des faits. Une
    # jointure manquée le laisserait à zéro, et toutes les fréquences par
    # département seraient vides sans qu'aucune erreur n'apparaisse.
    ecart_total = con.sql("""
        SELECT (SELECT SUM(naissances_departement) FROM d_departement)
             - (SELECT SUM(observe) FROM naissances_dep)
    """).fetchone()[0]

    # Le seul contrôle qui relie la sortie à la source, et le seul capable
    # d'attraper un filtre de niveau géographique oublié. Les autres portent sur
    # une cohérence interne, qui reste parfaite sur des données déjà abîmées :
    # dix-huit codes de région étant aussi des codes de département, une table
    # mélangeant les deux niveaux garde des jointures exactes et des marges
    # cohérentes.
    ecart_source = con.sql(r"""
        SELECT (SELECT SUM(valeur) FROM brut
                WHERE niveau_geographique = 'DEP'
                  AND prenom NOT LIKE '\_%' ESCAPE '\')
             - (SELECT SUM(observe) FROM f_specificite)
    """).fetchone()[0]

    assert sans_libelle == 0, f"{sans_libelle} départements sans libellé"
    assert perdues == 0, f"{perdues} naissances perdues à la jointure"
    assert marges[0] == marges[1] == marges[2], f"Marges incohérentes : {marges}"
    assert negatifs == 0, f"{negatifs} cellules à effectif nul ou négatif"
    assert ecart_grain == 0, f"Écart entre les deux grains : {ecart_grain}"
    assert ecart_total == 0, f"Total départemental incohérent : {ecart_total}"
    assert ecart_source == 0, f"Écart avec la source : {ecart_source}"
    assert 0 <= creux < 0.10, f"Table anormalement creuse : {creux:.1%}"
    print(f"Contrôles : OK (part creuse {creux:.1%})")


def decrire(con: duckdb.DuckDBPyConnection) -> None:
    """Affiche de quoi juger la table produite."""
    # Le taux de cellules au-dessus de 30 naissances annonce ce que le seuil de
    # diffusion laissera visible sur la carte.
    couverture = con.sql("""
        SELECT COUNT(DISTINCT prenom_id) AS prenoms,
               COUNT(*) AS cellules,
               ROUND(100.0 * COUNT(*) FILTER (WHERE observe >= 30)
                     / COUNT(*), 1) AS pct_30
        FROM f_specificite
    """).fetchone()
    print(f"{couverture[0]:,} prénoms présents en département, "
          f"{couverture[1]:,} cellules, "
          f"{couverture[2]} % au-dessus de 30 naissances")

    # Les indices les plus élevés sur des effectifs conséquents. C'est le visuel
    # que portera la page géographique, et le meilleur moyen de voir tout de
    # suite si le calcul dit quelque chose de sensé.
    # Le seuil de 500 écarte les indices spectaculaires portés par une poignée de
    # naissances, qui occuperaient tout le classement sans rien signifier.
    print("\nSpécificités les plus marquées, à partir de 500 naissances")
    con.sql("""
        SELECT p.libelle_segment AS prenom, d.dep_libelle AS departement,
               f.observe, ROUND(f.attendu) AS attendu,
               ROUND(f.observe / f.attendu, 1) AS indice
        FROM f_specificite f
        JOIN d_prenom p USING (prenom_id)
        JOIN d_departement d USING (dep_code)
        WHERE f.observe >= 500
        ORDER BY f.observe / f.attendu DESC
        LIMIT 12
    """).show()


# ---------------------------------------------------------------------------
# 3. Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    # Le dossier peut manquer chez quelqu'un qui vient de cloner le dépôt.
    SORTIE.mkdir(parents=True, exist_ok=True)

    # Base en mémoire, comme aux étapes précédentes : rien à nettoyer, aucun état
    # conservé entre deux exécutions.
    con = duckdb.connect()
    preparer(con)

    # Le SQL construit les quatre tables en cascade et n'écrit rien sur le
    # disque. L'export est décidé plus bas, après les contrôles.
    con.execute(SQL.read_text(encoding="utf-8"))

    controler(con)
    decrire(con)

    print()
    for table in A_EXPORTER:
        cible = SORTIE / f"{table}.parquet"
        # Interpolation sans risque : les deux valeurs viennent du programme.
        con.execute(f"COPY {table} TO '{cible.as_posix()}' (FORMAT PARQUET)")
        lignes = con.sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<16} {lignes:>9,} lignes  ->  {cible.name}")

    con.close()


if __name__ == "__main__":
    main()
