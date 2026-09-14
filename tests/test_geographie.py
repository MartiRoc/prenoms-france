"""Contrôles de non-régression sur la table géographique et l'indice de spécificité.

Les tables testées ici sont produites par 05_geographie.py. Elles reposent sur une
jointure entre deux sources publiées séparément, et sur un indice dont le calcul
dépend du choix de l'univers de référence. Les deux se trompent silencieusement.

Les fixtures viennent de conftest.py.
"""

# ---------------------------------------------------------------------------
# 1. Référentiel géographique
# ---------------------------------------------------------------------------
def test_les_departements_sont_uniques(scalaire):
    """Un code dupliqué multiplierait les effectifs de tout un département."""
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT dep_code FROM d_departement GROUP BY 1 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_chaque_departement_a_un_libelle(scalaire):
    """Un libellé vide se traduirait par une zone sans nom sur la carte."""
    sans_libelle = scalaire("""
        SELECT COUNT(*) FROM d_departement
        WHERE dep_libelle IS NULL OR dep_libelle = ''
    """)
    assert sans_libelle == 0


def test_le_drapeau_dom_suit_la_longueur_du_code(scalaire):
    """Les codes des DOM tiennent sur trois caractères, ceux de métropole sur deux.

    Le drapeau sert à cadrer une carte sur la métropole. Une incohérence
    afficherait une carte de France centrée sur l'Atlantique.
    """
    incoherents = scalaire("""
        SELECT COUNT(*) FROM d_departement WHERE est_dom <> (LENGTH(dep_code) = 3)
    """)
    assert incoherents == 0


def test_la_corse_est_decoupee_en_deux(scalaire):
    """Le Parquet des prénoms distingue 2A et 2B, contrairement au CSV départemental.

    Ce test verrouille une propriété de la source qui n'est pas évidente : la
    documentation du fichier départemental en CSV regroupe la Corse sous le code
    20, et un basculement d'un format à l'autre casserait la jointure.
    """
    codes = scalaire("""
        SELECT COUNT(*) FROM d_departement WHERE dep_code IN ('2A', '2B')
    """)
    assert codes == 2


def test_le_total_departemental_est_renseigne(scalaire):
    """La colonne sert de dénominateur à la fréquence pour 10 000 naissances.

    Une valeur nulle passerait inaperçue dans Power BI, où la division rend un
    résultat vide plutôt qu'une erreur. La carte se viderait alors sans que rien
    ne signale d'où vient le problème.
    """
    manquants = scalaire("""
        SELECT COUNT(*) FROM d_departement
        WHERE naissances_departement IS NULL OR naissances_departement <= 0
    """)
    assert manquants == 0


def test_chaque_departement_porte_une_region(scalaire):
    """La jointure est écrite en dur, donc rien ne la vérifie côté source.

    Un code de région apparu au Code officiel géographique sans être ajouté à
    la liste laisserait un libellé vide, et le segment de la page de synthèse
    afficherait une entrée sans nom.
    """
    orphelins = scalaire("""
        SELECT COUNT(*) FROM d_departement WHERE region_libelle IS NULL
    """)
    assert orphelins == 0


# ---------------------------------------------------------------------------
# 2. Contrats de clé et intégrité référentielle
# ---------------------------------------------------------------------------
def test_le_grain_de_f_specificite_est_respecte(scalaire):
    """Une ligne par couple et par département, sans quoi la carte double compte."""
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id, dep_code FROM f_specificite
            GROUP BY 1, 2 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_aucun_fait_sans_prenom(scalaire):
    orphelins = scalaire("""
        SELECT COUNT(*) FROM f_specificite ANTI JOIN d_prenom USING (prenom_id)
    """)
    assert orphelins == 0


def test_aucun_fait_sans_departement(scalaire):
    """La jointure entre les deux sources doit être exacte.

    Un code présent d'un côté seulement signalerait un décalage de millésime
    entre le fichier des prénoms et le Code officiel géographique.
    """
    orphelins = scalaire("""
        SELECT COUNT(*) FROM f_specificite ANTI JOIN d_departement USING (dep_code)
    """)
    assert orphelins == 0


def test_tous_les_departements_portent_des_naissances(scalaire):
    """L'inverse du test précédent, qui attraperait un filtre trop large."""
    sans_naissance = scalaire("""
        SELECT COUNT(*) FROM d_departement ANTI JOIN f_specificite USING (dep_code)
    """)
    assert sans_naissance == 0


# ---------------------------------------------------------------------------
# 3. Conservation depuis la source
# ---------------------------------------------------------------------------
def test_les_effectifs_departementaux_sont_conserves(scalaire):
    """Relie la table de faits à la source, comme au niveau national.

    C'est ce test qui attraperait une jointure portant sur le prénom seul, ou un
    filtre de niveau géographique oublié.
    """
    ecart = scalaire(r"""
        SELECT (SELECT SUM(valeur) FROM source
                WHERE niveau_geographique = 'DEP'
                  AND prenom NOT LIKE '\_%' ESCAPE '\')
             - (SELECT SUM(observe) FROM f_specificite)
    """)
    assert ecart == 0


def test_le_niveau_geographique_a_bien_ete_filtre(scalaire):
    """Vérifie que la table ne contient pas de régions déguisées en départements.

    Dix-huit codes de région sont aussi des codes de département, dont 11, 24,
    32, 44, 75, 84 et 93. Sans filtre sur le niveau, les effectifs de ces
    départements seraient gonflés par ceux de la région homonyme. Le total
    départemental resterait alors très supérieur au total national.
    """
    total_dep = scalaire("SELECT SUM(observe) FROM f_specificite")
    total_national = scalaire("""
        SELECT SUM(valeur) FROM source WHERE niveau_geographique = 'FRANCE'
    """)
    assert total_dep < total_national


# ---------------------------------------------------------------------------
# 4. Cohérence de l'indice
# ---------------------------------------------------------------------------
def test1_les_effectifs_restent_des_multiples_de_cinq(scalaire):
    """Arrondi de l'Insee, présent aussi au niveau départemental."""
    hors_regle = scalaire("SELECT COUNT(*) FROM f_specificite WHERE observe % 5 <> 0")
    assert hors_regle == 0


def test_les_deux_composantes_sont_strictement_positives(scalaire):
    """Un attendu nul rendrait l'indice infini, et Power BI afficherait une erreur."""
    incoherents = scalaire("""
        SELECT COUNT(*) FROM f_specificite WHERE observe <= 0 OR attendu <= 0
    """)
    assert incoherents == 0


def test_l_indice_median_reste_proche_de_un(scalaire):
    """Le test qui vaut pour tous les autres sur ce calcul.

    Par construction, la plupart des prénoms se répartissent à peu près comme la
    population, donc l'indice médian doit être voisin de 1. Un attendu calculé à
    partir des totaux nationaux, alors que l'observé vient du niveau
    départemental, décalerait systématiquement cette médiane sans rien casser
    d'autre. La restriction aux grosses cellules écarte le bruit des petits
    effectifs.
    """
    mediane = scalaire("""
        SELECT MEDIAN(observe / attendu) FROM f_specificite WHERE observe >= 500
    """)
    assert 0.9 < mediane < 1.2


def test_la_part_creuse_reste_faible(scalaire):
    """Documente une propriété structurelle plutôt qu'un défaut.

    La somme des attendus reste inférieure à celle des observés, parce que la
    table ne stocke que les combinaisons réellement rencontrées, une sur sept.
    L'écart mesure la masse attendue sur les cellules absentes. Un écart plus
    large signalerait un millésime cessant de publier les petits effectifs.
    """
    creux = scalaire("""
        SELECT (SUM(observe) - SUM(attendu)) / SUM(observe) FROM f_specificite
    """)
    assert 0 <= creux < 0.10


# ---------------------------------------------------------------------------
# 5. Table annuelle départementale
# ---------------------------------------------------------------------------
def test_le_grain_de_f_naissances_dep_est_respecte(scalaire):
    """Une ligne par couple, département et année.

    Un doublon viendrait d'une jointure sur le prénom seul, qui multiplierait
    les lignes des 3 177 prénoms portés par les deux sexes.
    """
    doublons = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id, dep_code, annee FROM f_naissances_dep
            GROUP BY 1, 2, 3 HAVING COUNT(*) > 1)
    """)
    assert doublons == 0


def test_les_deux_grains_departementaux_concordent(scalaire):
    """L'agrégation sur l'année doit redonner exactement f_specificite.

    Les deux tables décrivent les mêmes naissances à deux grains différents.
    Cette redondance est assumée, l'indice ayant besoin du cumul et la page de
    synthèse ayant besoin de l'année. Elle n'est tenable que si un test la
    verrouille, couple par couple et non sur le seul total général.
    """
    ecarts = scalaire("""
        SELECT COUNT(*) FROM (
            SELECT prenom_id, dep_code, SUM(effectif) AS cumul
            FROM f_naissances_dep
            GROUP BY prenom_id, dep_code
        ) a
        JOIN f_specificite s USING (prenom_id, dep_code)
        WHERE a.cumul <> s.observe
    """)
    assert ecarts == 0


def test_les_annees_couvrent_la_meme_plage_que_le_national(scalaire):
    """Les deux tables de faits se filtrent par la même dimension année.

    Une plage plus étroite au niveau départemental laisserait des années sans
    aucune donnée sur la page de synthèse, alors que la courbe nationale, elle,
    continuerait de les afficher.
    """
    hors_plage = scalaire("""
        SELECT COUNT(*) FROM (SELECT DISTINCT annee FROM f_naissances_dep)
        ANTI JOIN d_annee USING (annee)
    """)
    assert hors_plage == 0
