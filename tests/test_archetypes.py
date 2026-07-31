"""Contrôles de non-régression sur les profils, les traits et les archétypes.

Les tables testées ici sont produites par 03_traits.py et 04_archetypes.py. Elles
portent des mesures dérivées, dont les défauts ne se voient pas à l'oeil : un
profil incomplet, un trait hors bornes ou un groupe vide produisent un rapport
qui s'affiche normalement en racontant quelque chose de faux.

Les fixtures viennent de conftest.py.
"""

# ---------------------------------------------------------------------------
# 1. Profils. Une série incomplète fausse toute mesure de forme.
# ---------------------------------------------------------------------------
def test_la_grille_est_complete(scalaire):
    """Chaque prénom éligible a une ligne par année, y compris les années à zéro.

    Sans les zéros, un lissage sauterait les creux et une largeur de plage
    compterait des années absentes. C'est le contrôle qui protège tous les traits.
    """
    manquants = scalaire("""
        SELECT (SELECT COUNT(DISTINCT prenom_id) FROM profil)
             * (SELECT COUNT(*) FROM d_annee)
             - (SELECT COUNT(*) FROM profil)
    """)
    assert manquants == 0


def test_chaque_profil_somme_a_un(scalaire):
    """Contrat de la normalisation, qui rend les formes comparables entre prénoms."""
    ecart_max = scalaire("""
        SELECT MAX(ABS(total - 1)) FROM (
            SELECT prenom_id, SUM(part) AS total FROM profil GROUP BY prenom_id)
    """)
    assert ecart_max < 1e-9


def test_le_profil_lisse_perd_peu_de_masse(scalaire):
    """La moyenne mobile sur cinq ans perd de la masse aux deux bords de la période.

    La somme du profil lissé ne vaut donc pas exactement 1, elle s'en écarte de
    quelques pourcents. Le test borne cette perte plutôt que de l'ignorer : un
    écart plus large signalerait une fenêtre de lissage mal posée.
    """
    bornes = scalaire("""
        SELECT MAX(ABS(total - 1)) FROM (
            SELECT prenom_id, SUM(lisse) AS total FROM profil GROUP BY prenom_id)
    """)
    assert bornes < 0.05


# ---------------------------------------------------------------------------
# 2. Traits. Chacun a un domaine annoncé, qui doit être respecté.
# ---------------------------------------------------------------------------
def test_un_trait_par_prenom_eligible(scalaire):
    """Autant de lignes dans traits que de prénoms distincts dans profil.

    Un prénom présent dans les profils mais absent des traits disparaîtrait
    silencieusement de la classification.
    """
    ecart = scalaire("""
        SELECT (SELECT COUNT(DISTINCT prenom_id) FROM profil)
             - (SELECT COUNT(*) FROM traits)
    """)
    assert ecart == 0


def test_le_seuil_d_eligibilite_est_respecte(scalaire, config):
    """Aucun prénom sous le seuil ne reçoit de trait de forme.

    Sous le seuil, un prénom a une dizaine d'années renseignées : ses traits
    seraient calculés sur du bruit.
    """
    seuil = config["archetypes"]["seuil_naissances"]
    sous_le_seuil = scalaire(f"""
        SELECT COUNT(*) FROM traits JOIN d_prenom USING (prenom_id)
        WHERE naissances_totales < {seuil}
    """)
    assert sous_le_seuil == 0


def test_les_proportions_restent_entre_zero_et_un(scalaire):
    """Concentration, part après 2000 et asymétrie sont des proportions.

    L'asymétrie est bornée explicitement dans le SQL, parce que le pic d'un
    prénom encore en montée tombe hors des 90 % centraux de sa trajectoire.
    """
    hors_bornes = scalaire("""
        SELECT COUNT(*) FROM traits
        WHERE concentration   NOT BETWEEN 0 AND 1
           OR part_apres_2000 NOT BETWEEN 0 AND 1
           OR asymetrie       NOT BETWEEN 0 AND 1
    """)
    assert hors_bornes == 0


def test_les_comptages_sont_strictement_positifs(scalaire):
    """Un prénom a au moins une plage forte, celle qui contient son pic."""
    incoherents = scalaire("""
        SELECT COUNT(*) FROM traits WHERE largeur_forte < 1 OR nb_periodes < 1
    """)
    assert incoherents == 0


def test_l_annee_du_pic_reste_dans_la_periode(scalaire, config):
    """Une année hors période signalerait un décalage entre la grille et la source."""
    millesime = config["source"]["prenoms"]["millesime"]
    hors_periode = scalaire(f"""
        SELECT COUNT(*) FROM traits WHERE annee_pic NOT BETWEEN 1900 AND {millesime}
    """)
    assert hors_periode == 0


def test_la_troncature_est_coherente_avec_le_millesime(scalaire, config):
    """en_cours doit valoir vrai exactement pour les pics des dix dernières années.

    Ce drapeau décide de l'avertissement affiché sur le rapport. Une incohérence
    ferait passer une trajectoire tronquée pour une trajectoire terminée.
    """
    millesime = config["source"]["prenoms"]["millesime"]
    incoherents = scalaire(f"""
        SELECT COUNT(*) FROM traits
        WHERE en_cours <> (annee_pic >= {millesime} - 9)
    """)
    assert incoherents == 0


# ---------------------------------------------------------------------------
# 3. Archétypes. La liaison alimente tout le rapport.
# ---------------------------------------------------------------------------
def test_chaque_prenom_a_exactement_un_archetype(scalaire):
    """Couverture et unicité en un seul test.

    Les prénoms sous le seuil reçoivent le groupe 0. Sans cette couverture
    complète, le rapport afficherait des lignes vides sur la majorité des
    prénoms, ce qu'un lecteur prend pour un défaut d'affichage.
    """
    ecart = scalaire("""
        SELECT (SELECT COUNT(*) FROM d_prenom) - (SELECT COUNT(*) FROM prenom_archetype)
    """)
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id FROM prenom_archetype GROUP BY 1 HAVING COUNT(*) > 1)
    """)
    assert (ecart, doublons) == (0, 0)


def test_aucune_liaison_orpheline(scalaire):
    """Intégrité référentielle de la liaison vers les libellés.

    Le cas se produirait si le nombre de groupes changeait dans la configuration
    sans que la classification soit relancée.
    """
    orphelines = scalaire("""
        SELECT COUNT(*) FROM prenom_archetype ANTI JOIN d_archetype USING (archetype_id)
    """)
    assert orphelines == 0


def test_les_libelles_correspondent_a_la_configuration(scalaire, config):
    """Un décalage produirait des groupes mal nommés sans lever d'erreur."""
    attendus = config["archetypes"]["libelles"]
    classes = scalaire("SELECT COUNT(*) FROM d_archetype WHERE classe")
    assert classes == len(attendus)


def test_les_prenoms_classes_sont_exactement_les_eligibles(scalaire):
    """Le groupe 0 rassemble les prénoms sans trait, et eux seuls."""
    # Comparer deux booléens par <> exprime un « exactement l'un ou l'autre » en
    # une ligne, là où deux conditions symétriques demanderaient un OR.
    incoherents = scalaire("""
        SELECT COUNT(*) FROM prenom_archetype pa
        LEFT JOIN traits t USING (prenom_id)
        WHERE (pa.archetype_id = 0) <> (t.prenom_id IS NULL)
    """)
    assert incoherents == 0


def test_la_distance_au_centre_accompagne_les_prenoms_classes(scalaire):
    """Renseignée pour les classés, absente pour les autres, jamais négative."""
    incoherents = scalaire("""
        SELECT COUNT(*) FROM prenom_archetype
        WHERE (archetype_id = 0) <> (distance_centre IS NULL)
           OR distance_centre < 0
    """)
    assert incoherents == 0


def test_aucun_groupe_n_est_vide(scalaire):
    """Un groupe vide signalerait une classification dégénérée.

    Le cas arrive quand le nombre de groupes demandé dépasse la structure des
    données, et il passerait inaperçu : le rapport afficherait simplement une
    catégorie sans prénom.
    """
    # NOT EXISTS plutôt qu'une jointure suivie d'un comptage : la question porte
    # sur l'absence, et le moteur peut s'arrêter à la première correspondance.
    vides = scalaire("""
        SELECT COUNT(*) FROM d_archetype d
        WHERE d.classe
          AND NOT EXISTS (SELECT 1 FROM prenom_archetype pa
                          WHERE pa.archetype_id = d.archetype_id)
    """)
    assert vides == 0


# ---------------------------------------------------------------------------
# 4. Formes moyennes, qui alimentent les petits multiples du rapport.
# ---------------------------------------------------------------------------
def test_chaque_groupe_a_une_forme_complete(scalaire):
    """Un trou dans une courbe moyenne se verrait comme une chute à zéro."""
    manquants = scalaire("""
        SELECT (SELECT COUNT(*) FROM d_archetype WHERE classe)
             * (SELECT COUNT(*) FROM d_annee)
             - (SELECT COUNT(*) FROM profil_archetype)
    """)
    assert manquants == 0


def test_les_formes_moyennes_restent_normalisees(scalaire):
    """La moyenne de profils normalisés reste une répartition sommant à un.

    La tolérance reprend celle du profil lissé, dont la perte aux bords se
    reporte ici.
    """
    ecart_max = scalaire("""
        SELECT MAX(ABS(total - 1)) FROM (
            SELECT archetype_id, SUM(part_moyenne) AS total
            FROM profil_archetype GROUP BY archetype_id)
    """)
    assert ecart_max < 0.05
