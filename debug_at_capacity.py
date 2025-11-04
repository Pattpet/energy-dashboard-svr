# debug_at_capacity.py
import requests
from datetime import datetime, timedelta
import logging
import os
import zipfile
import io
import xml.etree.ElementTree as ET

# Předpokládáme, že eic_codes.py je ve stejném adresáři
import eic_codes 

# --- Nastavení logování pro debugovací skript ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ZÍSKÁNÍ API KLÍČE ---
# Protože tento skript nebude spouštěn přes Streamlit, nemůžeme přímo číst st.secrets.
# Pro účely ladění sem prosím VLOŽTE SVŮJ ENTSOE API KLÍČ.
# NEBO se ujistěte, že máte environmentální proměnnou ENTSOE_API_TOKEN nastavenou.
# Jakmile budeme hotovi s debugováním, tuto část odstraníme.

# Možnost 1: Vložte svůj API klíč přímo (POZOR na bezpečnost, jen pro lokální debug)
API_TOKEN = "ef3d70e0-2c39-48f7-9c60-e19a8f382f06" 

# Možnost 2: Přečtěte z environmentální proměnné (bezpečnější pro lokální debug)
# API_TOKEN = os.environ.get("ENTSOE_API_TOKEN") 

if not API_TOKEN:
    logging.error("ENTSPE API Klíč není nastaven. Prosím, vložte ho do skriptu 'debug_at_capacity.py' nebo nastavte environmentální proměnnou ENTSOE_API_TOKEN.")
    exit()

# --- Parametry pro API volání ---
COUNTRY_CODE = "AT"
TARGET_DATE = datetime(2025, 8, 21).date() # Datum, pro které máme problém
DOCUMENT_TYPE = "A15" # Procured balancing reserves
PROCESS_TYPE = "A51" # FCR, aFRR, mFRR total
MARKET_AGREEMENT_TYPE = "A01" # Day-Ahead

ENTSOE_API_URL = "https://web-api.tp.entsoe.eu/api"

def fetch_raw_procured_capacity_data(
    target_date: datetime.date,
    country_code: str,
    process_type: str,
    market_agreement_type: str,
    document_type: str,
    api_key: str
) -> str:
    """
    Stahuje syrová data z ENTSOE-E API pro rezervovanou kapacitu a vrací je jako řetězec.
    Pokud je odpověď ZIP, extrahuje XML.
    """
    area_domain = eic_codes.get_eic(country_code)

    if not area_domain:
        logging.error(f"Nepodporovaný kód země pro rezervovanou kapacitu: {country_code} (EIC kód nenalezen).")
        return ""

    start_period = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_period = start_period + timedelta(days=1)

    period_start_str = start_period.strftime("%Y%m%d%H%M")
    period_end_str = end_period.strftime("%Y%m%d%H%M")

    params = {
        'securityToken': api_key,
        'documentType': document_type,
        'processType': process_type,
        'area_Domain': area_domain,
        'periodStart': period_start_str,
        'periodEnd': period_end_str,
        'Type_MarketAgreement.Type': market_agreement_type
    }
    
    logging.info(f"Volám ENTSOE-E API pro {country_code} ({target_date})...")
    logging.info(f"Parametry: {params}")

    try:
        response = requests.get(url=ENTSOE_API_URL, params=params, timeout=90)
        response.raise_for_status() # Vyvolá chybu pro HTTP chyby (4xx nebo 5xx)

        content_type = response.headers.get('Content-Type', '')
        logging.info(f"Content-Type odpovědi: {content_type}")

        if 'application/zip' in content_type or response.content.startswith(b'PK\x03\x04'):
            logging.info("Odpověď je ZIP archiv. Rozbaluji...")
            xml_content = ""
            with io.BytesIO(response.content) as bio_zip1:
                with zipfile.ZipFile(bio_zip1) as zip_file_level1:
                    for file_name_l1 in zip_file_level1.namelist():
                        logging.info(f"Nalezen soubor v ZIPu: {file_name_l1}")
                        with zip_file_level1.open(file_name_l1) as content_l1:
                            bytes_l1 = content_l1.read()
                            
                            if file_name_l1.lower().endswith('.zip'):
                                logging.info(f"Nalezen vnořený ZIP: {file_name_l1}. Rozbaluji...")
                                with io.BytesIO(bytes_l1) as bio_zip2:
                                    with zipfile.ZipFile(bio_zip2) as zip_file_level2:
                                        for file_name_l2 in zip_file_level2.namelist():
                                            logging.info(f"Nalezen soubor ve vnořeném ZIPu: {file_name_l2}")
                                            if file_name_l2.lower().endswith('.xml'):
                                                with zip_file_level2.open(file_name_l2) as xml_file_l2:
                                                    xml_content += xml_file_l2.read().decode("utf-8", errors='replace') + "\n"
                            elif file_name_l1.lower().endswith('.xml'):
                                xml_content += bytes_l1.decode("utf-8", errors='replace') + "\n"
            return xml_content
        
        elif 'application/xml' in content_type or 'text/xml' in content_type:
            logging.info("Odpověď je XML soubor.")
            return response.content.decode("utf-8", errors="replace")
        else:
            logging.warning(f"Neočekávaný Content-Type: {content_type}. Obsah (prvních 500b): {response.content[:500]}")
            return ""

    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP Chyba: {e.response.status_code} pro {target_date}. Odpověď: {e.response.text[:500]}...")
        return ""
    except requests.exceptions.RequestException as e:
        logging.error(f"Chyba spojení: {e} pro {target_date}.")
        return ""
    except Exception as e:
        logging.error(f"Neznámá chyba: {e} pro {target_date}.")
        return ""

if __name__ == "__main__":
    raw_xml_data = fetch_raw_procured_capacity_data(
        target_date=TARGET_DATE,
        country_code=COUNTRY_CODE,
        process_type=PROCESS_TYPE,
        market_agreement_type=MARKET_AGREEMENT_TYPE,
        document_type=DOCUMENT_TYPE,
        api_key=API_TOKEN
    )

    if raw_xml_data:
        print("\n--- Získaná syrová XML data (prvních 5000 znaků) ---")
        print(raw_xml_data[:5000]) # Vytiskneme jen prvních 5000 znaků, aby to nebylo moc dlouhé
        print("\n--- Konec syrových XML dat ---")

        # Zkusíme si XML parsnout a vypsat jména elementů a atributů,
        # abychom získali lepší představu o struktuře
        try:
            root = ET.fromstring(raw_xml_data)
            print("\n--- Prvních 10 elementů s jejich tagy a atributy ---")
            for i, elem in enumerate(root.iter()):
                if i >= 10:
                    break
                print(f"Tag: {elem.tag}, Atributy: {elem.attrib}, Text: {elem.text.strip() if elem.text else ''}")
            print("\n--- Konec ukázky elementů ---")

        except ET.ParseError as e:
            print(f"\nCHYBA: Nepodařilo se parsovat XML: {e}")
            print("Pravděpodobně se nepodařilo stáhnout validní XML data.")
    else:
        print(f"\nNepodařilo se získat žádná data pro {COUNTRY_CODE} ({TARGET_DATE}).")