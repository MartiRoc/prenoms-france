-- Construction du socle national à partir du fichier des prénoms de l'Insee.
-- Exécuté par src/02_build.py, qui fournit la variable chemin_source.
-- Niveau départemental et indice de spécificité : hors périmètre, voir J3.
--
-- Le grain de chaque table est indiqué dans son en-tête de section. C'est la
-- seule information qu'il faut avoir en tête pour relire ce fichier.

-- ---------------------------------------------------------------------------
-- 1. Vue sur la source
-- ---------------------------------------------------------------------------
-- Une vue et non une table : elle ne stocke rien, et la seule table qui mérite
-- d'être matérialisée est celle de l'étape suivante, relue plusieurs fois.
-- read_parquet lit le fichier sans étape de chargement, en ne touchant que les
-- colonnes demandées.
CREATE OR REPLACE VIEW brut AS
    SELECT * FROM read_parquet(getvariable('chemin_source'));


-- ---------------------------------------------------------------------------
-- 2. Base nettoyée. Grain : un prénom, un sexe, une année, niveau France
-- ---------------------------------------------------------------------------
-- Cette table ne fait que normaliser les types et renommer les colonnes. Toute
-- la logique dérivée vit dans les tables suivantes, ce qui laisse un point de
-- comparaison direct avec la source quand un contrôle échoue.
--
-- Le fichier mélange les trois niveaux géographiques dans une même colonne,
-- d'où le filtre. Les noms d'origine sont peu parlants : periode contient une
-- année, valeur contient un nombre de naissances.
--
-- Le rang vient de l'Insee, calculé par niveau, année et sexe. Le recalculer
-- coûterait cher pour un résultat identique, d'où le suffixe qui rappelle son
-- origine.
CREATE OR REPLACE TABLE base_nat AS
SELECT CAST(sexe    AS SMALLINT) AS sexe_code,
       prenom,
       CAST(periode AS SMALLINT) AS annee,
       valeur                    AS effectif,
       rang                      AS rang_source
FROM brut
WHERE niveau_geographique = 'FRANCE'

  -- Les deux filtres suivants sont défensifs. Ce millésime ne contient ni année
  -- sentinelle ni regroupement de prénoms rares, mais la documentation Insee
  -- mentionne les deux, et un autre millésime peut en contenir.
  --
  -- TRY_CAST rend NULL au lieu de lever une erreur, ce qui permet de filtrer
  -- plutôt que d'échouer. Dans le second filtre, le tiret bas est un joker en
  -- SQL, d'où la clause ESCAPE pour le chercher littéralement.
  AND TRY_CAST(periode AS INTEGER) IS NOT NULL
  AND prenom NOT LIKE '\_%' ESCAPE '\';


-- ---------------------------------------------------------------------------
-- 3. Dimensions de référence
-- ---------------------------------------------------------------------------
-- La source ne fournit que des codes. La correspondance vit ici plutôt que
-- d'être répétée dans chaque visuel Power BI.
CREATE OR REPLACE TABLE d_sexe AS
SELECT CAST(code AS SMALLINT) AS sexe_code, libelle AS sexe_libelle
FROM (VALUES (1, 'Masculin'), (2, 'Féminin')) AS t(code, libelle);

-- Dimension année dérivée des données plutôt que générée sur une plage fixe :
-- une année absente du fichier ne doit pas apparaître dans les filtres du
-- rapport. Le modulo évite un CAST, puisque l'opérateur de division rend un
-- flottant en DuckDB.
CREATE OR REPLACE TABLE d_annee AS
SELECT DISTINCT annee,
       annee - (annee % 10) AS decennie
FROM base_nat
ORDER BY annee;


-- ---------------------------------------------------------------------------
-- 4. d_prenom. Grain : un couple (prénom, sexe)
-- ---------------------------------------------------------------------------
-- Le choix de grain est la décision de modélisation la plus importante du
-- fichier. Le prénom seul ne constitue pas une clé : 3 177 prénoms sont portés
-- par les deux sexes, avec des trajectoires nettement différentes. CAMILLE
-- culmine en 1910 chez les garçons et en 1998 chez les filles. Une jointure sur
-- le prénom seul mélangerait deux populations, et le résultat serait faux sans
-- qu'aucune erreur n'apparaisse.
CREATE OR REPLACE TABLE d_prenom AS
WITH agg AS (
    -- Attributs calculés une fois ici plutôt que mesurés à la volée dans Power
    -- BI, où ils coûteraient cher sur 52 340 prénoms et 126 années.
    SELECT sexe_code,
           prenom,
           SUM(effectif)::BIGINT    AS naissances_totales,
           MIN(annee)               AS premiere_annee,
           MAX(annee)               AS derniere_annee,
           arg_max(annee, effectif) AS annee_pic,   -- ex aequo, choix arbitraire
           MAX(effectif)            AS effectif_pic,
           MIN(rang_source)         AS meilleur_rang
    FROM base_nat
    GROUP BY sexe_code, prenom
),
libelles AS (
    -- Trois colonnes dérivées du prénom, parce que trois usages différents s'y
    -- opposent : l'affichage veut une casse lisible, la recherche veut une
    -- forme sans accent, et le segment veut un libellé unique.
    SELECT *,
           -- DuckDB ne fournit pas initcap. On découpe sur le tiret, on
           -- capitalise chaque segment et on recolle : JEAN-PIERRE devient
           -- Jean-Pierre.
           array_to_string(
               list_transform(string_split(prenom, '-'),
                              x -> upper(x[1]) || lower(x[2:])),
               '-') AS prenom_affichage,

           -- Sans cette colonne, une personne qui tape "leo" ne trouverait
           -- rien : les prénoms du fichier sont accentués, 7 150 d'entre eux
           -- portent au moins un accent.
           lower(strip_accents(prenom)) AS prenom_recherche,

           -- Nombre de sexes associés au prénom, 1 ou 2. Une fonction de
           -- fenêtrage plutôt qu'un regroupement, pour ne pas perdre le détail.
           COUNT(*) OVER (PARTITION BY prenom) AS nb_sexes
    FROM agg
)
SELECT
       -- Clé de substitution plutôt que la clé naturelle composite : Power BI
       -- joint plus vite sur un entier, et ses relations ne portent que sur une
       -- colonne. L'ORDER BY rend l'attribution déterministe, condition pour
       -- que des tests de non-régression aient un sens.
       row_number() OVER (ORDER BY prenom, sexe_code) AS prenom_id,

       prenom,
       prenom_affichage,
       prenom_recherche,

       -- Le libellé du segment doit être unique, sinon Leo y apparaîtrait deux
       -- fois sans que rien ne les distingue. Le suffixe ne concerne que les
       -- prénoms portés par les deux sexes, pour ne pas alourdir les autres.
       CASE WHEN nb_sexes > 1
            THEN prenom_affichage || ' (' || CASE sexe_code WHEN 1 THEN 'M' ELSE 'F' END || ')'
            ELSE prenom_affichage
       END AS libelle_segment,

       nb_sexes > 1 AS prenom_mixte,
       sexe_code,
       naissances_totales,
       premiere_annee,
       derniere_annee,
       annee_pic,
       effectif_pic,
       meilleur_rang
FROM libelles;


-- ---------------------------------------------------------------------------
-- 5. f_naissances. Grain : un couple (prénom, sexe) et une année
-- ---------------------------------------------------------------------------
-- Table de faits : des mesures additives et une clé vers la dimension. Les
-- attributs descriptifs restent dans d_prenom, ce qui est le principe du schéma
-- en étoile.
CREATE OR REPLACE TABLE f_naissances AS
SELECT p.prenom_id,
       b.annee,
       b.effectif,
       b.rang_source,

       -- Part du prénom parmi les naissances du même sexe cette année-là. Le
       -- nom de la colonne le dit explicitement, parce qu'un simple part_annee
       -- laisserait croire à une part sur l'ensemble des naissances.
       --
       -- Le dénominateur est la somme des effectifs retenus dans le fichier, et
       -- non le nombre réel de naissances en France : les prénoms rares sont
       -- absents et les valeurs sont arrondies au multiple de 5 le plus proche.
       -- À écrire dans la page de méthode du rapport.
       b.effectif / SUM(b.effectif) OVER (PARTITION BY b.annee, b.sexe_code)
           AS part_dans_annee_sexe

FROM base_nat b

-- La jointure porte sur les deux colonnes. Sur le prénom seul, elle produirait
-- un produit cartésien partiel pour les prénoms mixtes, donc des effectifs
-- doublés. C'est le contrôle d'écart de somme, dans 02_build.py, qui attrape
-- cette erreur.
JOIN d_prenom p USING (prenom, sexe_code);
