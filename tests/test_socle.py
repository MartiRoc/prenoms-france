"""Contrôles de non-régression sur les tables du socle national.

Les tests portent sur les données exportées, pas sur les fonctions Python. Sur un
pipeline, les défauts coûteux ne sont pas des exceptions mais des tables
plausibles et fausses : une clé dupliquée, des effectifs perdus dans une
jointure, une source dont la structure a changé sans prévenir. Tester la fonction
d'empreinte reviendrait à tester la bibliothèque standard.

Les fixtures viennent de conftest.py. Lancer le pipeline avant les tests :
    uv run src/01_download.py
    uv run src/02_build.py
    uv run src/03_traits.py
    uv run src/04_archetypes.py
    uv run pytest
"""

import unicodedata


# ---------------------------------------------------------------------------
# 2. Contrats de clé
# ---------------------------------------------------------------------------
# Un doublon de clé fait double compter toutes les mesures dans Power BI, sans
# jamais lever d'erreur. C'est la panne la plus coûteuse de ce pipeline, et la
# plus difficile à repérer à l'oeil : les chiffres restent plausibles.
def test_prenom_id_est_unique(scalaire):
    """Clé de substitution de la dimension, sur laquelle porte la relation."""
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id FROM d_prenom GROUP BY 1 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_le_couple_prenom_sexe_est_unique(scalaire):
    """Grain déclaré de d_prenom, à distinguer du prénom seul.

    Le prénom seul n'est pas une clé : 3 177 prénoms sont portés par les deux
    sexes. Ce test verrouille la décision de modélisation.
    """
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom, sexe_code FROM d_prenom GROUP BY 1, 2 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_le_grain_de_f_naissances_est_respecte(scalaire):
    """Une ligne par couple et par année, sans quoi les mesures sont fausses."""
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id, annee FROM f_naissances GROUP BY 1, 2 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_aucun_fait_orphelin(scalaire):
    """Un fait sans prénom correspondant apparaîtrait sous une ligne vide ou un
    total sans libellé, ce qu'on prend facilement pour un défaut d'affichage."""
    orphelins = scalaire("""
        SELECT COUNT(*) FROM f_naissances ANTI JOIN d_prenom USING (prenom_id)
    """)
    assert orphelins == 0


# ---------------------------------------------------------------------------
# 3. Conservation des données
# ---------------------------------------------------------------------------
# Les deux seuls tests qui relient la sortie à la source. Tous les autres
# vérifient une cohérence interne, qui peut être parfaite sur des données déjà
# abîmées.
def test_les_effectifs_de_la_source_sont_conserves(scalaire):
    """Le test le plus important du fichier.

    Une jointure sur le prénom seul dupliquerait les effectifs des prénoms
    mixtes, et l'écart deviendrait négatif de plusieurs dizaines de millions.
    """
    ecart = scalaire("""
        SELECT (SELECT SUM(valeur) FROM source WHERE niveau_geographique = 'FRANCE')
             - (SELECT SUM(effectif) FROM f_naissances)
    """)
    assert ecart == 0


def test_tous_les_prenoms_de_la_source_sont_presents(scalaire):
    """Aucun couple perdu par les filtres défensifs du SQL.

    Ils ne retirent rien sur ce millésime, et ce test le confirme plutôt que de
    le supposer.
    """
    ecart = scalaire("""
        SELECT (SELECT COUNT(DISTINCT (prenom, sexe)) FROM source
                WHERE niveau_geographique = 'FRANCE')
             - (SELECT COUNT(*) FROM d_prenom)
    """)
    assert ecart == 0


# ---------------------------------------------------------------------------
# 4. Sentinelles sur la source
# ---------------------------------------------------------------------------
# Ces tests ne vérifient pas le code. Ils signalent qu'un nouveau millésime a
# changé les règles du jeu, ce qui se découvre autrement en constatant des
# résultats étranges plusieurs jours plus tard.
def test2_les_effectifs_restent_des_multiples_de_cinq(scalaire):
    """Arrondi appliqué par l'Insee pour protéger l'anonymat. Sa disparition
    rendrait les seuils de diffusion du rapport inutiles."""
    hors_regle = scalaire("SELECT COUNT(*) FROM f_naissances WHERE effectif % 5 <> 0")
    assert hors_regle == 0


def test_les_annees_couvrent_la_periode_annoncee(con, config):
    """Attrape le cas où le fichier chargé ne correspond pas au millésime que le
    projet croit utiliser, après une modification partielle du TOML."""
    bornes = con.sql("SELECT MIN(annee), MAX(annee) FROM d_annee").fetchone()
    assert bornes == (1900, config["source"]["prenoms"]["millesime"])


def test_les_sexes_sont_les_deux_codes_attendus(scalaire):
    """La dimension d_sexe n'en déclare que deux, et la jointure serait muette."""
    codes = scalaire("SELECT COUNT(DISTINCT sexe_code) FROM d_prenom")
    assert codes == 2


# ---------------------------------------------------------------------------
# 5. Contrats des colonnes dérivées
# ---------------------------------------------------------------------------
# Ces colonnes existent pour l'interface du rapport. Leur défaut ne fausse aucun
# chiffre, il rend le rapport inutilisable, ce qui se voit tard et mal.
def test_la_cle_de_recherche_est_sans_accent_et_en_minuscules(con):
    """Sans cette propriété, une saisie sans accent ne trouve rien. Les prénoms
    du fichier sont accentués et en majuscules."""
    lignes = con.sql("SELECT prenom_recherche FROM d_prenom").fetchall()
    echantillon = [ligne[0] for ligne in lignes]
    # La décomposition NFD évite d'énumérer les caractères accentués possibles.
    fautifs = [
        p for p in echantillon
        if p != p.lower()
        or any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", p))
    ]
    # La comparaison à une liste vide plutôt qu'un test de vacuité : en cas
    # d'échec, pytest affiche les valeurs fautives.
    assert fautifs == [], f"Exemples : {fautifs[:5]}"


def test_le_libelle_du_segment_est_unique(scalaire):
    """Deux entrées identiques seraient indiscernables dans le segment. C'est la
    raison d'être du suffixe ajouté aux prénoms mixtes."""
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT libelle_segment FROM d_prenom GROUP BY 1 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_les_parts_somment_a_un_par_annee_et_par_sexe(scalaire):
    """Contrat de la fonction de fenêtrage, et vérification du dénominateur.

    Une somme égale à 2 par année signalerait un partitionnement oublié, une
    somme inférieure à 1 un dénominateur calculé sur un périmètre trop large.
    """
    ecart_max = scalaire("""
        SELECT MAX(ABS(total - 1)) FROM (
            SELECT annee, sexe_code, SUM(part_dans_annee_sexe) AS total
            FROM f_naissances JOIN d_prenom USING (prenom_id)
            GROUP BY annee, sexe_code)
    """)
    # Tolérance flottante : la somme de milliers de divisions ne tombe pas juste.
    # Une égalité stricte ferait échouer un test correct.
    assert ecart_max < 1e-9
