"""Classe les trajectoires en archétypes et écrit les tables destinées au rapport.

Lit traits.parquet et profil.parquet, écrit d_archetype.parquet,
prenom_archetype.parquet et profil_archetype.parquet.

La méthode de classification tient dans la fonction classer(). Elle est
volontairement simple et isolée, pour être remplacée sans toucher au reste du
script : tout ce qui suit ne dépend que d'une affectation d'un prénom à un
groupe.
"""

import tomllib
from pathlib import Path

import duckdb
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.preprocessing import StandardScaler

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "data" / "out"
CONFIG = RACINE / "config" / "sources.toml"

# Deux mesures et non cinq. Les cinq traits calculés à l'étape 3 ne portent que
# deux dimensions réelles : deux composantes principales expliquent 84 % de la
# variance, et les corrélations disent lesquelles. L'année du pic va avec la part
# après 2000 à 0,79 et avec l'asymétrie à 0,71, la largeur de plage va avec la
# concentration à moins 0,78. Les donner toutes pondérerait la récence trois
# fois. Ces deux-ci se nomment, ce qu'une composante principale ne fait pas.
MESURES = ["annee_pic", "largeur_forte"]

# Graine fixée : k-means part de centres tirés au hasard, donc deux exécutions
# sans graine donneraient des groupes numérotés différemment.
GRAINE = 0

# Trois tables pour trois usages : les libellés des groupes, le rattachement de
# chaque prénom à son groupe, et la forme moyenne de chaque groupe pour les
# petits multiples du rapport.
A_EXPORTER = ["d_archetype", "prenom_archetype", "profil_archetype"]


# ---------------------------------------------------------------------------
# 1. Préparation
# ---------------------------------------------------------------------------
def configuration() -> dict:
    """Retourne la configuration, libellés et nombre de groupes compris."""
    with CONFIG.open("rb") as f:
        return tomllib.load(f)


def charger(con: duckdb.DuckDBPyConnection) -> None:
    """Expose les tables des étapes précédentes."""
    # Des vues et non des copies : DuckDB lit les Parquet à la demande, en ne
    # touchant que les colonnes réellement interrogées.
    for table in ["d_prenom", "traits", "profil"]:
        chemin = SORTIE / f"{table}.parquet"
        if not chemin.exists():
            raise FileNotFoundError(f"{chemin} absent. Lancer 03_traits.py.")
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM '{chemin.as_posix()}'")


# ---------------------------------------------------------------------------
# 2. La classification, seule partie destinée à être remplacée
# ---------------------------------------------------------------------------
def classer(traits, nombre: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Affecte chaque prénom à un groupe et retourne les centres en unités d'origine.

    La standardisation est indispensable : l'année du pic couvre 125 unités et la
    largeur de plage une centaine, sans quoi la distance euclidienne du k-means
    serait dictée par la seule année.

    Les groupes sont renumérotés par ordre d'époque croissante, du plus ancien au
    plus récent. C'est ce qui rend l'association avec les libellés stable d'une
    exécution à l'autre, alors que k-means numérote ses groupes arbitrairement.
    """
    echelle = StandardScaler().fit(traits[MESURES])
    X = echelle.transform(traits[MESURES])

    # n_init=20 relance vingt fois depuis des centres différents et garde le
    # meilleur résultat. Le k-means converge vers un optimum local, donc un seul
    # départ suffirait à obtenir un découpage médiocre sans le savoir.
    modele = KMeans(n_clusters=nombre, n_init=20, random_state=GRAINE).fit(X)
    centres = echelle.inverse_transform(modele.cluster_centers_)

    # np.argsort donne l'ordre des anciens numéros, il faut l'inverser pour
    # obtenir le nouveau numéro de chaque ancien.
    ordre = np.argsort(centres[:, 0])
    renumerotation = np.empty_like(ordre)
    renumerotation[ordre] = np.arange(len(ordre))

    groupes = renumerotation[modele.labels_] + 1
    distances = np.linalg.norm(X - modele.cluster_centers_[modele.labels_], axis=1)
    silhouette = silhouette_score(X, modele.labels_)

    return groupes, distances, silhouette


def controler_stabilite(traits, nombre: int, tirages: int = 20) -> float:
    """Mesure l'accord des affectations entre le jeu complet et des sous-échantillons.

    Une classification qui se réorganise à chaque tirage ne décrit rien de
    stable. L'indice de Rand ajusté vaut 1 pour un accord parfait et 0 pour un
    accord dû au hasard. Ce contrôle ne change aucune sortie, il informe : le
    retirer ne casserait rien.
    """
    echelle = StandardScaler().fit(traits[MESURES])
    X = echelle.transform(traits[MESURES])
    reference = KMeans(n_clusters=nombre, n_init=20, random_state=GRAINE).fit(X).labels_

    generateur = np.random.default_rng(GRAINE)
    accords = []
    for _ in range(tirages):
        # Tirage sans remise de 80 % des prénoms, puis comparaison des
        # affectations sur ce sous-ensemble uniquement.
        indices = generateur.choice(len(X), size=int(0.8 * len(X)), replace=False)
        essai = KMeans(n_clusters=nombre, n_init=20,
                       random_state=GRAINE).fit(X[indices])
        accords.append(adjusted_rand_score(reference[indices], essai.labels_))

    return float(np.mean(accords))


# ---------------------------------------------------------------------------
# 3. Construction des tables du rapport
# ---------------------------------------------------------------------------
def construire(con: duckdb.DuckDBPyConnection, libelles: list[str]) -> None:
    """Assemble les trois tables exportées à partir de l'affectation."""
    # Le groupe 0 accueille les prénoms sous le seuil d'éligibilité. Sans lui, le
    # rapport afficherait des lignes vides pour 50 000 prénoms, ce qu'un lecteur
    # prend pour un défaut d'affichage.
    con.execute("""
        CREATE OR REPLACE TABLE d_archetype AS
        SELECT * FROM (VALUES (0, 'Non classé', false))
                    AS t(archetype_id, libelle, classe)
    """)

    # Insertion en boucle plutôt qu'un VALUES écrit en dur, parce que le nombre
    # de groupes vient de la configuration. Les libellés passent par des
    # paramètres liés, comme toute valeur.
    for numero, libelle in enumerate(libelles, start=1):
        con.execute("INSERT INTO d_archetype VALUES (?, ?, true)", [numero, libelle])

    # Table de liaison plutôt qu'une colonne ajoutée à d_prenom. Écraser une
    # table produite par une étape antérieure rendrait l'ordre d'exécution
    # fragile, alors que cette étape sera relancée souvent.
    con.execute("""
        CREATE OR REPLACE TABLE prenom_archetype AS
        SELECT p.prenom_id,
               COALESCE(a.groupe, 0) AS archetype_id,
               a.distance_centre
        FROM d_prenom p
        LEFT JOIN affectation a USING (prenom_id)
    """)

    # Forme moyenne de chaque groupe, pour les petits multiples du rapport. La
    # moyenne porte sur les profils normalisés, donc chaque prénom pèse pareil
    # quel que soit son volume.
    con.execute("""
        CREATE OR REPLACE TABLE profil_archetype AS
        SELECT a.groupe AS archetype_id,
               pr.annee,
               AVG(pr.lisse) AS part_moyenne
        FROM profil pr
        JOIN affectation a USING (prenom_id)
        GROUP BY a.groupe, pr.annee
    """)


def decrire(con: duckdb.DuckDBPyConnection, silhouette: float,
            stabilite: float) -> None:
    """Affiche de quoi juger la classification et nommer les groupes."""
    # Comment lire ces deux nombres. La silhouette mesure la séparation des
    # groupes, de 0 pour des groupes indistincts à 1 pour des groupes isolés :
    # sur des données réelles sans vide entre les groupes, 0,45 est correct et
    # signale une segmentation d'un continuum plutôt que des types naturels. Le
    # Rand ajusté mesure la reproductibilité : au-dessus de 0,9, les mêmes
    # prénoms se retrouvent ensemble quel que soit le sous-échantillon.
    print(f"Silhouette {silhouette:.3f}   stabilité (Rand ajusté) {stabilite:.3f}")
    print("\nComposition des groupes")
    con.sql("""
        SELECT d.archetype_id AS grp, d.libelle,
               COUNT(*) AS prenoms,
               MIN(t.annee_pic) AS pic_min,
               ROUND(MEDIAN(t.annee_pic)) AS pic_median,
               MAX(t.annee_pic) AS pic_max,
               ROUND(MEDIAN(t.largeur_forte)) AS largeur_med,
               ROUND(MEDIAN(t.concentration), 2) AS concentr_med,
               COUNT(*) FILTER (WHERE t.en_cours) AS en_cours
        FROM prenom_archetype pa
        JOIN d_archetype d USING (archetype_id)
        JOIN traits t USING (prenom_id)
        GROUP BY d.archetype_id, d.libelle ORDER BY d.archetype_id
    """).show()

    # Les prénoms les plus proches du centre, et non les plus portés. Un prénom
    # très porté peut être excentré dans son groupe, ce qui donnerait une idée
    # fausse de ce que le groupe contient.
    print("Prénoms les plus proches du centre de chaque groupe")
    con.sql("""
        SELECT archetype_id AS grp,
               string_agg(libelle_segment, ', ' ORDER BY rang) AS representatifs
        FROM (
            SELECT pa.archetype_id, p.libelle_segment,
                   row_number() OVER (PARTITION BY pa.archetype_id
                                      ORDER BY pa.distance_centre) AS rang
            FROM prenom_archetype pa
            JOIN d_prenom p USING (prenom_id)
            WHERE pa.archetype_id > 0)
        WHERE rang <= 6 GROUP BY archetype_id ORDER BY archetype_id
    """).show()


# ---------------------------------------------------------------------------
# 4. Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    config = configuration()["archetypes"]
    libelles = config["libelles"]
    nombre = config["nombre"]

    # Un décalage entre le nombre de libellés et le nombre de groupes
    # produirait des groupes mal nommés sans lever d'erreur.
    if len(libelles) != nombre:
        raise ValueError(f"{len(libelles)} libellés pour {nombre} groupes.")

    con = duckdb.connect()
    charger(con)

    # Le passage par un tableau de données est le seul endroit où les données
    # quittent DuckDB, parce que scikit-learn ne sait pas lire autre chose.
    # L'ordre explicite rend l'exécution reproductible, et l'alignement entre le
    # tableau et les affectations calculées plus bas repose sur cet ordre.
    traits = con.sql("SELECT * FROM traits ORDER BY prenom_id").df()

    groupes, distances, silhouette = classer(traits, nombre)
    stabilite = controler_stabilite(traits, nombre)

    # Une copie plutôt qu'une vue sur le tableau d'origine, pour ne pas ajouter
    # de colonnes à une structure que pandas partage parfois en mémoire.
    affectation = traits[["prenom_id"]].copy()
    affectation["groupe"] = groupes
    affectation["distance_centre"] = distances
    # register expose le tableau à DuckDB sans le copier, ce qui permet aux
    # requêtes suivantes de joindre l'affectation aux tables du socle.
    con.register("affectation", affectation)

    construire(con, libelles)


    # Chaque prénom du rapport doit avoir exactement un archétype, y compris les
    # non classés. Un écart signalerait une jointure fautive sur l'affectation.
    total = con.sql("SELECT COUNT(*) FROM d_prenom").fetchone()[0]
    lies = con.sql("SELECT COUNT(*) FROM prenom_archetype").fetchone()[0]
    assert lies == total, f"{lies} liaisons pour {total} prénoms"

    decrire(con, silhouette, stabilite)

    print()
    for table in A_EXPORTER:
        cible = SORTIE / f"{table}.parquet"
        con.execute(f"COPY {table} TO '{cible.as_posix()}' (FORMAT PARQUET)")
        lignes = con.sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<18} {lignes:>9,} lignes  ->  {cible.name}")

    con.close()


if __name__ == "__main__":
    main()
