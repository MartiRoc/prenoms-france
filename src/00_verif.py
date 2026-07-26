from pathlib import Path
import duckdb

import sys, os
print("interpréteur :", sys.executable)
print("CWD          :", os.getcwd())

RACINE = Path(__file__).resolve().parent.parent   # src/00_verif.py → racine du projet
FICHIER = RACINE / "data" / "raw" / "prenoms-2025.parquet"

con = duckdb.connect()

print(con.sql(f"SELECT count(*) AS lignes FROM '{FICHIER.as_posix()}'"))

print(con.sql(f"""
    SELECT prenom, valeur, rang
    FROM '{FICHIER}'
    WHERE niveau_geographique = 'FRANCE'
      AND periode = '2025'
      AND sexe = '1'
    ORDER BY rang
    LIMIT 10
"""))