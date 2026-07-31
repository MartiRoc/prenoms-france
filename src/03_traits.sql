-- Profils de trajectoire et traits de forme, préalables à la classification.
-- Exécuté par src/03_traits.py, qui fournit les variables seuil et millesime.
-- La classification elle-même est dans une étape distincte.
--
-- Le grain de chaque table est indiqué dans son en-tête de section.

-- ---------------------------------------------------------------------------
-- 1. Prénoms éligibles. Grain : un couple (prénom, sexe)
-- ---------------------------------------------------------------------------
-- Sous le seuil, un prénom n'a pas de trajectoire mais quelques points isolés :
-- moins de 1 000 naissances cumulées correspond à une dizaine d'années
-- renseignées en moyenne, parfois une seule. Les prénoms écartés restent dans
-- le rapport, ils n'y reçoivent simplement pas d'archétype.
CREATE OR REPLACE TABLE eligibles AS
SELECT prenom_id, prenom_affichage, libelle_segment, sexe_code, naissances_totales
FROM d_prenom
WHERE naissances_totales >= getvariable('seuil');


-- ---------------------------------------------------------------------------
-- 2. Grille complète. Grain : un couple et une année, y compris les vides
-- ---------------------------------------------------------------------------
-- Le fichier source ne contient une ligne que pour les années où le prénom a
-- été donné. Comparer des formes exige au contraire une série continue : sans
-- les zéros, un lissage sauterait les creux et une largeur de plage compterait
-- des années absentes.
CREATE OR REPLACE TABLE grille AS
SELECT e.prenom_id,
       a.annee,
       COALESCE(f.effectif, 0) AS effectif
FROM eligibles e
CROSS JOIN d_annee a
LEFT JOIN f_naissances f ON f.prenom_id = e.prenom_id AND f.annee = a.annee;


-- ---------------------------------------------------------------------------
-- 3. Profil normalisé. Grain : un couple et une année
-- ---------------------------------------------------------------------------
-- La normalisation est ce qui rend les formes comparables. Chaque trajectoire
-- est ramenée à la répartition de ses propres naissances, donc Marie et Kevin
-- deviennent comparables malgré un facteur quinze sur les effectifs.
--
-- Le lissage sur cinq ans efface les variations d'une année à l'autre, qui
-- viennent en partie de l'arrondi à 5 appliqué par l'Insee.
--
-- Le cumul servira à borner la trajectoire sur ses 90 % centraux, pour que
-- quelques naissances isolées un siècle plus tôt n'étirent pas la forme.
CREATE OR REPLACE TABLE profil AS
SELECT g.prenom_id,
       g.annee,
       g.effectif,
       g.effectif / e.naissances_totales AS part,
       AVG(g.effectif / e.naissances_totales) OVER (
           PARTITION BY g.prenom_id ORDER BY g.annee
           ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING) AS lisse,
       SUM(g.effectif) OVER (
           PARTITION BY g.prenom_id ORDER BY g.annee) / e.naissances_totales AS cumul
FROM grille g
JOIN eligibles e USING (prenom_id);


-- ---------------------------------------------------------------------------
-- 4. Sommets. Grain : un couple
-- ---------------------------------------------------------------------------
-- Séparé de la table suivante parce qu'un seuil relatif au pic doit connaître
-- le pic, et qu'une fonction de fenêtrage ne peut pas être imbriquée dans une
-- agrégation.
CREATE OR REPLACE TABLE sommets AS
SELECT prenom_id,
       MAX(lisse) AS hauteur_pic,
       arg_max(annee, lisse) AS annee_pic
FROM profil
GROUP BY prenom_id;


-- ---------------------------------------------------------------------------
-- 5. Traits de forme. Grain : un couple
-- ---------------------------------------------------------------------------
-- Six traits interprétables plutôt qu'une distance entre courbes brutes. Ils
-- s'expliquent à l'oral, ils résistent au bruit, et ils sont eux-mêmes du
-- contenu : la largeur de plage forte répond à la question de la durée d'une
-- mode.
CREATE OR REPLACE TABLE traits AS
WITH etats AS (
    -- Hystérésis à deux seuils : on entre en plage forte au-dessus de 0,50 du
    -- pic, on n'en sort qu'en repassant sous 0,35. Entre les deux, l'état
    -- précédent est conservé.
    --
    -- Un seuil unique produisait des plages fictives : une courbe qui longe le
    -- seuil le franchit plusieurs fois pour des variations minuscules, ce qui
    -- donnait six périodes à Camille masculin et trois à Marie.
    SELECT p.prenom_id, p.annee, p.part, p.effectif, p.cumul,
           s.hauteur_pic, s.annee_pic,
           CASE WHEN p.lisse >= 0.50 * s.hauteur_pic THEN 1
                WHEN p.lisse <  0.35 * s.hauteur_pic THEN 0 END AS etat_brut
    FROM profil p
    JOIN sommets s USING (prenom_id)
),
propage AS (
    -- Report de la dernière valeur connue sur les années laissées indécises par
    -- l'hystérésis. Le rang par effectif servira à mesurer la concentration.
    SELECT *,
           LAST_VALUE(etat_brut IGNORE NULLS) OVER (
               PARTITION BY prenom_id ORDER BY annee
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS etat,
           row_number() OVER (PARTITION BY prenom_id ORDER BY effectif DESC) AS rang_annee
    FROM etats
),
transitions AS (
    -- Une nouvelle plage commence là où l'état passe de 0 à 1.
    SELECT *,
           COALESCE(LAG(etat) OVER (PARTITION BY prenom_id ORDER BY annee), 0) AS etat_avant
    FROM propage
),
mesures AS (
    SELECT prenom_id,
           ANY_VALUE(annee_pic) AS annee_pic,

           -- Durée de la mode, en années passées au-dessus de la mi-hauteur.
           COUNT(*) FILTER (WHERE etat = 1) AS largeur_forte,

           -- Nombre de plages fortes. Deux ou plus signalent un retour de mode.
           COUNT(*) FILTER (WHERE etat = 1 AND etat_avant = 0) AS nb_periodes,

           -- Part des naissances concentrée sur les dix meilleures années.
           -- Sépare le feu de paille du prénom porté sur trois générations.
           --
           -- Le bornage à 1 retire le bruit de représentation : additionner des
           -- centaines de flottants dont la somme vaut exactement 1 dépasse 1
           -- d'un epsilon machine, ce qui suffit à violer le contrat de la
           -- colonne. Six prénoms sont concernés, ceux dont toutes les
           -- naissances sont postérieures à 2000.
           LEAST(1, SUM(part) FILTER (WHERE rang_annee <= 10)) AS concentration,

           -- Isole les prénoms récents et les importations.
           LEAST(1, SUM(part) FILTER (WHERE annee >= 2000)) AS part_apres_2000,

           -- Bornes des 90 % centraux de la trajectoire.
           MIN(annee) FILTER (WHERE cumul >= 0.05) AS annee_debut,
           MAX(annee) FILTER (WHERE cumul <= 0.95) AS annee_fin
    FROM transitions
    GROUP BY prenom_id
)
SELECT prenom_id,
       annee_pic,
       largeur_forte,
       nb_periodes,
       concentration,
       part_apres_2000,
       annee_debut,
       annee_fin,

       -- Position du pic dans la trajectoire, de 0 pour une chute immédiate à 1
       -- pour une montée lente. Le bornage entre 0 et 1 traite les prénoms dont
       -- le pic tombe hors des 90 % centraux, ce qui arrive aux deux extrémités
       -- de la période.
       GREATEST(0, LEAST(1,
           (annee_pic - annee_debut) * 1.0 / NULLIF(annee_fin - annee_debut, 0)
       )) AS asymetrie,

       -- Trajectoire tronquée à droite : le pic tombe dans les dix dernières
       -- années, donc la suite de la courbe n'existe pas encore. Ces prénoms
       -- reçoivent une catégorie à part plutôt que d'être écartés, puisque ce
       -- sont ceux que les visiteurs chercheront en premier.
       annee_pic >= getvariable('millesime') - 9 AS en_cours
FROM mesures;
