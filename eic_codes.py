# eic_codes.py

# slovník: kód země -> EIC kód
eic_by_country = {
    "CZ": "10YCZ-CEPS-----N",
    "DE_50": "10YDE-VE-------2", # Ponecháváme, i když zatím nebudeme aktivně používat
    "DE_TR": "10YDE-ENBW-----N",
    "DE_tennet": "10YDE-EON------1",
    "DE_amprion": "10YDE-RWENET---I",
    "AT": "10YAT-APG------L", # EIC kód pro Rakousko
    "PL": "10YPL-AREA-----S",
    "SK": "10YSK-SEPS-----K",
    "BE": "10YBE----------2",
    "FR": "10YFR-RTE------C",
}

def list_keys():
    """Vrátí seznam dostupných kódů zemí."""
    return list(eic_by_country.keys())

def get_eic(country: str) -> str:
    """Vrátí EIC kód pro zadanou zemi (case-insensitive)."""
    # Upraveno pro bezpečnější přístup, pokud klíč neexistuje
    country_upper = country.strip().upper()
    if country_upper in eic_by_country:
        return eic_by_country[country_upper]
    else:
        # Vrátíme prázdný řetězec, což by mělo být ošetřeno v data_loaderu.
        # Streamlit logování bude také zaznamenávat upozornění, pokud se EIC nenajde.
        logging.warning(f"EIC kód pro zemi '{country}' nebyl nalezen.")
        return ""