-- Niveau départemental et indice de spécificité géographique.
-- Exécuté par src/05_geographie.py, qui fournit les chemins des deux sources.
--
-- Le grain de chaque table est indiqué dans son en-tête de section.

-- ---------------------------------------------------------------------------
-- 1. Sources
-- ---------------------------------------------------------------------------
-- Le fichier des prénoms est relu ici, et non repris de base_nat, parce que
-- l'étape 2 n'a gardé que le niveau national.
CREATE OR REPLACE VIEW brut AS
    SELECT * FROM read_parquet(getvariable('chemin_prenoms'));

CREATE OR REPLACE VIEW cog AS
    SELECT * FROM read_csv(getvariable('chemin_departements'));


-- ---------------------------------------------------------------------------
-- 2. f_naissances_dep. Grain : un couple, un département et une année
-- ---------------------------------------------------------------------------
-- La table qui permet de croiser une période et un territoire. f_naissances
-- porte l'année sans la géographie, f_specificite porte la géographie sans
-- l'année, et aucun visuel ne peut donc filtrer les deux à la fois.
--
-- Le filtre sur le niveau est vital et non défensif : 18 codes de région sont
-- aussi des codes de département, dont 11, 24, 32, 44, 75, 84 et 93. Une
-- jointure sans ce filtre mélangerait des régions et des départements sans
-- lever la moindre erreur.
--
-- Les deux autres filtres reprennent ceux du niveau national et restent
-- défensifs. Ce millésime ne contient ni année sentinelle ni regroupement de
-- prénoms rares, mais la documentation Insee mentionne les deux.
--
-- Le rang de la source n'est pas repris. Il est calculé par niveau
-- géographique, donc un rang départemental ne se compare pas au rang national
-- de f_naissances, et leur cohabitation dans le modèle induirait en erreur.
CREATE OR REPLACE TABLE f_naissances_dep AS
WITH base_dep AS (
    SELECT CAST(sexe    AS SMALLINT) AS sexe_code,
           prenom,
           CAST(periode AS SMALLINT) AS annee,
           geographie                AS dep_code,
           valeur::BIGINT            AS effectif
    FROM brut
    WHERE niveau_geographique = 'DEP'
      AND TRY_CAST(periode AS INTEGER) IS NOT NULL
      AND prenom NOT LIKE '\_%' ESCAPE '\'
)
SELECT p.prenom_id,
       b.annee,
       b.dep_code,
       b.effectif
FROM base_dep b
JOIN d_prenom p USING (prenom, sexe_code);


-- ---------------------------------------------------------------------------
-- 3. Naissances départementales cumulées. Grain : un couple et un département
-- ---------------------------------------------------------------------------
-- Toute la période cumulée, agrégée depuis la table annuelle plutôt que relue
-- depuis la source. Les deux grains ne peuvent alors plus diverger, et le
-- filtre de niveau géographique n'est écrit qu'une fois.
--
-- Le cumul reste le grain de l'indice. Le découper par année réduirait
-- fortement le nombre de cellules au-dessus du seuil de diffusion, et l'indice
-- deviendrait illisible sur les départements peu peuplés.
--
-- Le prénom et le sexe reviennent par la dimension, l'indice de la section
-- suivante partitionnant sur le couple que la table annuelle ne porte plus.
CREATE OR REPLACE TABLE naissances_dep AS
SELECT p.sexe_code,
       p.prenom,
       f.dep_code,
       SUM(f.effectif)::BIGINT AS observe
FROM f_naissances_dep f
JOIN d_prenom p USING (prenom_id)
GROUP BY p.sexe_code, p.prenom, f.dep_code;


-- ---------------------------------------------------------------------------
-- 4. f_specificite. Grain : un couple et un département
-- ---------------------------------------------------------------------------
-- L'indice est un quotient de localisation : l'effectif observé rapporté à
-- l'effectif attendu si le prénom se répartissait comme l'ensemble des
-- naissances. Un indice de 2 signifie deux fois plus de porteurs que la taille
-- du département ne le laisserait attendre.
--
-- Le point de méthode est le choix de l'univers. Observé et attendu sont
-- calculés dans le seul monde des données départementales, jamais à partir des
-- totaux nationaux : la somme départementale ne représente que 91,5 % du
-- national, les prénoms trop rares n'étant pas publiés à cette maille. Mélanger
-- les deux univers biaiserait tous les indices vers le bas sans que rien ne le
-- signale.
--
-- Les deux composantes sont stockées, pas l'indice. Un ratio ne s'additionne
-- pas : une moyenne d'indices sur plusieurs départements ne vaut pas l'indice de
-- leur regroupement. Power BI recalculera le rapport des sommes, ce qui est
-- juste à tout niveau d'agrégation.
CREATE OR REPLACE TABLE f_specificite AS
WITH totaux AS (
    SELECT n.*,
           SUM(observe) OVER (PARTITION BY prenom, sexe_code) AS total_prenom,
           SUM(observe) OVER (PARTITION BY dep_code)          AS total_departement,
           SUM(observe) OVER ()                               AS total_general
    FROM naissances_dep n
)
SELECT p.prenom_id,
       t.dep_code,
       t.observe,
       t.total_prenom * t.total_departement / t.total_general AS attendu
FROM totaux t
JOIN d_prenom p USING (prenom, sexe_code);


-- ---------------------------------------------------------------------------
-- 5. d_departement. Grain : un département
-- ---------------------------------------------------------------------------
-- Le Code officiel géographique fournit les libellés, absents du fichier des
-- prénoms. Les 101 codes des deux fichiers coïncident exactement, ce qu'un test
-- vérifie plutôt que de le supposer.
--
-- Les codes des DOM tiennent sur trois caractères, ceux de métropole sur deux,
-- Corse comprise avec 2A et 2B. Le drapeau permet de cadrer une carte sur la
-- métropole sans écrire la liste des codes dans un visuel.
--
-- Le total départemental sert de dénominateur à la fréquence d'un prénom pour
-- 10 000 naissances. Il vit dans la dimension plutôt que dans une mesure DAX,
-- où aucun filtre de prénom ne peut alors l'atteindre. Il se calcule sur les
-- naissances départementales et non sur le total national, faute de quoi le
-- numérateur et le dénominateur ne couvriraient pas le même univers.
--
-- La dimension arrive en fin de fichier parce que cette colonne dépend d'une
-- table construite plus haut. La jointure externe et la valeur de repli sont
-- défensives : un département sans aucune naissance publiée resterait présent
-- sur la carte, avec une fréquence vide plutôt qu'une zone absente.
CREATE OR REPLACE TABLE d_departement AS
SELECT c.DEP                AS dep_code,
       c.LIBELLE            AS dep_libelle,
       c.REG                AS region_code,
       LENGTH(c.DEP) = 3    AS est_dom,
       COALESCE(t.total, 0) AS naissances_departement
FROM cog c
LEFT JOIN (
    SELECT dep_code,
           SUM(observe)::BIGINT AS total
    FROM naissances_dep
    GROUP BY dep_code
) t ON t.dep_code = c.DEP;
