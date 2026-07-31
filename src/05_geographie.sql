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
-- 2. d_departement. Grain : un département
-- ---------------------------------------------------------------------------
-- Le Code officiel géographique fournit les libellés, absents du fichier des
-- prénoms. Les 101 codes des deux fichiers coïncident exactement, ce qu'un test
-- vérifie plutôt que de le supposer.
--
-- Les codes des DOM tiennent sur trois caractères, ceux de métropole sur deux,
-- Corse comprise avec 2A et 2B. Le drapeau permet de cadrer une carte sur la
-- métropole sans écrire la liste des codes dans un visuel.
CREATE OR REPLACE TABLE d_departement AS
SELECT DEP                  AS dep_code,
       LIBELLE              AS dep_libelle,
       REG                  AS region_code,
       LENGTH(DEP) = 3      AS est_dom
FROM cog;


-- ---------------------------------------------------------------------------
-- 3. Naissances départementales. Grain : un couple et un département
-- ---------------------------------------------------------------------------
-- Toute la période cumulée. La version par décennie ferait 809 776 lignes au
-- lieu de 258 164, ce qui reste abordable, mais elle réduirait fortement le
-- nombre de cellules au-dessus du seuil de diffusion.
--
-- Le filtre sur le niveau est vital et non défensif : 18 codes de région sont
-- aussi des codes de département, dont 11, 24, 32, 44, 75, 84 et 93. Une
-- jointure sans ce filtre mélangerait des régions et des départements sans
-- lever la moindre erreur.
CREATE OR REPLACE TABLE naissances_dep AS
SELECT CAST(sexe AS SMALLINT) AS sexe_code,
       prenom,
       geographie             AS dep_code,
       SUM(valeur)::BIGINT    AS observe
FROM brut
WHERE niveau_geographique = 'DEP'
  AND prenom NOT LIKE '\_%' ESCAPE '\'
GROUP BY sexe_code, prenom, dep_code;


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
