# data_loader.py (OPRAVENÁ A KOMPLETNÍ VERZE, znovu)

import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz 
import requests
import io
import zipfile
from entsoe import EntsoePandasClient 
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

"""
Tento modul obsahuje funkce pro načítání dat z ENTSOE API.
Používá Streamlit kešovací mechanismy (st.cache_resource, st.cache_data)
pro efektivní získávání dat a minimalizaci API volání.
Chybové stavy jsou ošetřeny vracením prázdných DataFrame a interním logováním.
"""

# --- Konfigurace a inicializace ENTSOE klienta ---
@st.cache_resource
def get_entsoe_client():
    api_token_from_secrets = st.secrets["entsoe_api"]["token"]
    return EntsoePandasClient(api_key=api_token_from_secrets)

# --- Funkce pro načítání denních cen ---
@st.cache_data(ttl=3600)
def fetch_day_ahead_prices_data(country_code: str, date: datetime.date) -> pd.DataFrame:
    client = get_entsoe_client() 
    start_ts = pd.Timestamp(f'{date} 00:00:00', tz='Europe/Brussels')
    end_ts = pd.Timestamp(f'{date + timedelta(days=1)} 00:00:00', tz='Europe/Brussels')

    try:
        df_prices_series = client.query_day_ahead_prices(
            country_code=country_code,
            start=start_ts,
            end=end_ts
        )
        
        df_prices = df_prices_series.reset_index(name='Price')
        df_prices = df_prices.rename(columns={'index': 'Time'})
        
        # Ošetření časových zón: konvertovat na UTC-naive, pokud jsou aware
        if not df_prices['Time'].dt.tz is None: 
            df_prices['Time'] = df_prices['Time'].dt.tz_convert('UTC').dt.tz_localize(None)
        
        df_prices = df_prices.dropna(subset=['Time'])
        
        return df_prices
    except Exception as e:
        logging.error(f"Nepodařilo se načíst data pro denní trh ({country_code}, {date}): {e}")
        return pd.DataFrame()


# --- FUNKCE PRO NAČÍTÁNÍ NABÍDKOVÝCH KŘIVEK (BALANCING BIDS) ---

def _parse_reserve_bid_xml_modular(xml_data_str: str, 
                                   process_type: str, 
                                   connecting_domain: str) -> list:
    data_points = []
    ns = {'rbd': 'urn:iec62325.351:tc57wg16:451-7:reservebiddocument:7:1'}
    
    try:
        root = ET.fromstring(xml_data_str)
    except ET.ParseError as e:
        logging.warning(f"Chyba parsování XML pro rezervní nabídky: {e}")
        return []

    if len(root.findall(".//rbd:Bid_TimeSeries", ns)) == 0:
        return []

    for time_series in root.findall(".//rbd:Bid_TimeSeries", ns):
        bid_id_elem = time_series.find(".//rbd:mRID", ns)
        bid_id = bid_id_elem.text if bid_id_elem is not None else "N/A"
        
        current_timeseries_direction = "Unknown"
        direction_elem = time_series.find(".//rbd:flowDirection.direction", ns)
        if direction_elem is not None and direction_elem.text:
            if direction_elem.text == "A01": 
                current_timeseries_direction = "Up"
            elif direction_elem.text == "A02":
                current_timeseries_direction = "Down"
        
        for period in time_series.findall(".//rbd:Period", ns):
            start_time_str = period.findtext('rbd:timeInterval/rbd:start', default=None, namespaces=ns) 
            
            resolution_str = period.findtext('rbd:resolution', default="PT15M", namespaces=ns) 
            
            if start_time_str is None or resolution_str is None:
                continue

            step = timedelta(minutes=15)
            if resolution_str == "PT60M" or resolution_str == "P1H": step = timedelta(hours=1)
            elif resolution_str == "PT30M": step = timedelta(minutes=30)
            elif resolution_str == "PT1M": step = timedelta(minutes=1)
            
            try:
                start_datetime = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%MZ")
            except ValueError:
                logging.warning(f"Nelze parsovat start_time: {start_time_str}")
                continue
                
            for point in period.findall(".//rbd:Point", ns):
                pos_str = point.findtext('rbd:position', default="0", namespaces=ns) 
                position = int(pos_str) if pos_str is not None else 0
                
                power_str = point.findtext('rbd:quantity.quantity', default=None, namespaces=ns) 
                if power_str is None: 
                    power_str = point.findtext('rbd:quantity', default=None, namespaces=ns) 
                
                price_str = point.findtext('rbd:energy_Price.amount', default=None, namespaces=ns) 
                if price_str is None: 
                    price_str = point.findtext('rbd:price.amount', default=None, namespaces=ns) 
                if price_str is None:
                    price_str = point.findtext('rbd:Price.amount', default=None, namespaces=ns) 

                power = float(power_str) if power_str is not None else None
                price = float(price_str) if price_str is not None else 0.0 
                
                if power is None:
                    continue

                timestamp = start_datetime + (position - 1) * step

                data_points.append({
                    "Timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "Bid ID": bid_id,
                    "Power (MW)": power,
                    "Price (EUR/MWh)": price, 
                    "Direction": current_timeseries_direction,
                    "ProcessType": process_type,
                    "ConnectingDomain": connecting_domain
                })
    return data_points

@st.cache_data(ttl=3600)
def fetch_balancing_bids_for_day_modular(
    target_date: datetime.date,
    country_code: str,
    process_type: str = "A51", 
    document_type: str = "A37", 
    business_type: str = "B74" 
) -> pd.DataFrame:
    """
    Stahuje a parsuje data "Balancing energy bids" (documentType=A37) z ENTSOE-E API
    pro jeden konkrétní den.
    """
    api_key = st.secrets["entsoe_api"]["token"]
    entsoe_api_url = "https://web-api.tp.entsoe.eu/api"
    
    country_eic_map = {
        'CZ': '10YCZ-CEPS-----N',
        'DE': '10Y1001A1001A83F',
    }
    connecting_domain = country_eic_map.get(country_code, None)

    if not connecting_domain:
        logging.error(f"Nepodporovaný kód země pro balancing bids: {country_code}.")
        return pd.DataFrame()

    start_period = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    end_period = start_period + timedelta(days=1)

    period_start_str = start_period.strftime("%Y%m%d%H%M")
    period_end_str = end_period.strftime("%Y%m%d%H%M")

    params = {
        'securityToken': api_key,
        'documentType': document_type,
        'businessType': business_type,
        'processType': process_type,
        'connecting_Domain': connecting_domain,
        'periodStart': period_start_str,
        'periodEnd': period_end_str,
    }
    
    all_extracted_data = []

    try:
        response = requests.get(entsoe_api_url, params=params, timeout=90)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '')

        if 'application/zip' in content_type or response.content.startswith(b'PK\x03\x04'):
            try:
                with io.BytesIO(response.content) as bio_zip1:
                    with zipfile.ZipFile(bio_zip1) as zip_file_level1:
                        for file_name_l1 in zip_file_level1.namelist():
                            with zip_file_level1.open(file_name_l1) as content_l1:
                                bytes_l1 = content_l1.read()
                                
                                is_nested_zip_processed = False
                                if file_name_l1.lower().endswith('.zip'):
                                    try:
                                        with io.BytesIO(bytes_l1) as bio_zip2:
                                            with zipfile.ZipFile(bio_zip2) as zip_file_level2:
                                                for file_name_l2 in zip_file_level2.namelist():
                                                    if file_name_l2.lower().endswith('.xml'):
                                                        with zip_file_level2.open(file_name_l2) as xml_file_l2:
                                                            try:
                                                                xml_data_str = xml_file_l2.read().decode("utf-8")
                                                            except UnicodeDecodeError:
                                                                xml_data_str = xml_file_l2.read().decode("ISO-8859-1", errors='replace')
                                                            
                                                            parsed_points = _parse_reserve_bid_xml_modular(xml_data_str, process_type, connecting_domain)
                                                            all_extracted_data.extend(parsed_points)
                                                is_nested_zip_processed = True
                                    except zipfile.BadZipFile:
                                        logging.warning(f"Soubor {file_name_l1} vypadal jako ZIP, ale není platný.")
                                    except Exception as e_nested:
                                        logging.error(f"Chyba při zpracování vnořeného ZIPu {file_name_l1}: {e_nested}")

                                if not is_nested_zip_processed and file_name_l1.lower().endswith('.xml'):
                                    try:
                                        xml_data_str = bytes_l1.decode("utf-8")
                                    except UnicodeDecodeError:
                                        xml_data_str = bytes_l1.decode("ISO-8859-1", errors='replace')
                                    
                                    parsed_points = _parse_reserve_bid_xml_modular(xml_data_str, process_type, connecting_domain)
                                    all_extracted_data.extend(parsed_points)
            except zipfile.BadZipFile:
                logging.error(f"Chyba: Odpověď byla označena jako ZIP, ale není to platný ZIP archiv pro {target_date}.")
            except Exception as e_zip_outer:
                 logging.error(f"Neznámá chyba při zpracování hlavního ZIPu pro {target_date}: {e_zip_outer}")
        
        elif 'application/xml' in content_type or 'text/xml' in content_type:
            try:
                xml_data_str = response.content.decode("utf-8")
            except UnicodeDecodeError:
                xml_data_str = response.content.decode("ISO-8859-1", errors='replace')
            
            if "NoMatchingData" in xml_data_str or "Error_Reason" in xml_data_str :
                 logging.info(f"API pro balancing bids vrátilo NoMatchingData/Error_Reason pro {target_date}: {xml_data_str[:250].replace(chr(10),'').replace(chr(13),'')}...")
            else:
                parsed_points = _parse_reserve_bid_xml_modular(xml_data_str, process_type, connecting_domain)
                all_extracted_data.extend(parsed_points)
        else:
            logging.warning(f"Neočekávaný Content-Type pro balancing bids: {content_type}. Obsah (prvních 200b): {response.content[:200]}")

    except requests.exceptions.HTTPError as e:
        error_text = e.response.text[:250].replace(chr(10),'').replace(chr(13),'') if e.response else "No response text"
        logging.error(f"HTTP Chyba při načítání balancing bids: {e.response.status_code if e.response else 'N/A'} pro {target_date}. Odpověď: {error_text}...")
    except requests.exceptions.RequestException as e:
        logging.error(f"Chyba spojení při načítání balancing bids: {e} pro {target_date}.")
    except Exception as e:
        logging.error(f"Neznámá chyba při stahování/základním zpracování balancing bids pro {target_date}: {e}")

    df_bids = pd.DataFrame(all_extracted_data)
    
    if not df_bids.empty and 'Timestamp' not in df_bids.columns:
        logging.error("Chyba: 'Timestamp' sloupec chybí v DataFrame z balancing bids!")
        return pd.DataFrame()

    if not df_bids.empty:
        df_bids['Timestamp'] = pd.to_datetime(df_bids['Timestamp'], errors='coerce')
        if not df_bids['Timestamp'].dt.tz is None: 
            df_bids['Timestamp'] = df_bids['Timestamp'].dt.tz_convert('UTC').dt.tz_localize(None)
        
        df_bids = df_bids.rename(columns={'Price (EUR/MWh)': 'Price (€/MWh)'})

        df_bids = df_bids.dropna(subset=['Timestamp'])
        return df_bids
    else:
        return pd.DataFrame()


# --- FUNKCE PRO NAČÍTÁNÍ AKTIVOVANÝCH CEN RE (aFRR+, aFRR-) ---

def _parse_activated_balancing_price_xml_modular(xml_data_str: str) -> pd.DataFrame:
    """
    Parsuje XML obsah pro aktivované ceny regulační energie.
    Vrací DataFrame s UTC-naive datetime a cenami.
    """
    ns = {'ns': 'urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1'}
    data = []
    try:
        root = ET.fromstring(xml_data_str)
    except ET.ParseError as e:
        logging.warning(f"Chyba parsování XML pro aktivované ceny RE: {e}")
        return pd.DataFrame()

    for ts in root.findall('.//ns:TimeSeries', ns):
        flow_direction = ts.findtext('ns:flowDirection.direction', default=None, namespaces=ns)
        period = ts.find('ns:Period', ns)
        if period is not None:
            start_time_str = period.findtext('ns:timeInterval/ns:start', default=None, namespaces=ns)
            resolution_str = period.findtext('ns:resolution', default="PT15M", namespaces=ns)
            if start_time_str is None or resolution_str is None:
                continue
            
            start_time_utc_aware = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%MZ").replace(tzinfo=pytz.UTC)

            if resolution_str == "PT15M":
                step = timedelta(minutes=15)
            elif resolution_str in ("PT60M", "P1H"):
                step = timedelta(hours=1)
            elif resolution_str == "PT30M":
                step = timedelta(minutes=30)
            else:
                step = timedelta(minutes=15) # Default

            for point in period.findall('ns:Point', ns):
                pos_str = point.findtext('ns:position', default="0", namespaces=ns)
                pos = int(pos_str) if pos_str is not None else 0
                price_str = point.findtext('ns:activation_Price.amount', default="nan", namespaces=ns)
                price = float(price_str) if price_str is not None else float('nan')
                
                dt_utc_aware = start_time_utc_aware + (pos - 1) * step
                
                data.append({
                    "Timestamp": dt_utc_aware.replace(tzinfo=None),  
                    "flowDirection": flow_direction,
                    "activation_price": price
                })
    
    df = pd.DataFrame(data)
    if df.empty or 'Timestamp' not in df.columns:
        logging.error("Chyba: 'Timestamp' sloupec chybí v DataFrame z aktivovaných cen RE!")
        return pd.DataFrame()

    df_out = df.pivot_table(
        index="Timestamp",
        columns="flowDirection",
        values="activation_price"
    ).rename(
        columns={"A01": "afrr_plus_price", "A02": "afrr_minus_price"}
    ).reset_index()

    for col in ["afrr_plus_price", "afrr_minus_price"]:
        if col not in df_out.columns:
            df_out[col] = float('nan')

    return df_out[["Timestamp", "afrr_plus_price", "afrr_minus_price"]].sort_values("Timestamp")


@st.cache_data(ttl=3600)
def fetch_afrr_activation_prices_data(
    target_date: datetime.date, 
    country_code: str,
    business_type: str = "A96", 
    process_type: str = "A16",  
    document_type: str = "A84"  
) -> pd.DataFrame:
    """
    Načítá a parsuje ceny aktivované regulační energie (aFRR+, aFRR-) pro danou zemi a datum.
    """
    api_key = st.secrets["entsoe_api"]["token"]
    entsoe_api_url = "https://web-api.tp.entsoe.eu/api"

    country_eic_map = {
        'CZ': '10YCZ-CEPS-----N',
        'DE': '10Y1001A1001A83F',
    }
    control_area_domain = country_eic_map.get(country_code, None)

    # DŮLEŽITÉ: Tyto řádky byly možná vynechány nebo přesunuty při předchozí úpravě,
    # což způsobilo NameError. Zajišťuji jejich přítomnost zde.
    country_timezones_map_for_loader = {
        'CZ': 'Europe/Prague',
        'DE': 'Europe/Berlin',
    }
    local_tz_str = country_timezones_map_for_loader.get(country_code, 'UTC') 

    if not control_area_domain:
        logging.error(f"Nepodporovaný kód země pro aFRR aktivované ceny: {country_code}.")
        return pd.DataFrame()

    dates_to_fetch = [target_date - timedelta(days=1), target_date]
    all_fetched_data = []

    for day_to_fetch in dates_to_fetch:
        start_period = datetime(day_to_fetch.year, day_to_fetch.month, day_to_fetch.day, 0, 0, 0)
        end_period = start_period + timedelta(days=1)

        period_start_str = start_period.strftime("%Y%m%d%H%M")
        period_end_str = end_period.strftime("%Y%m%d%H%M")

        params = {
            "securityToken": api_key,
            "documentType": document_type,
            "processType": process_type,
            "controlArea_Domain": control_area_domain,
            "businessType": business_type,
            "periodStart": period_start_str,
            "periodEnd": period_end_str
        }
        
        try:
            response = requests.get(url=entsoe_api_url, params=params, timeout=60)
            response.raise_for_status() 

            xml_str = response.content.decode("utf-8", errors="replace")
            
            if "NoMatchingData" in xml_str or "Error_Reason" in xml_str:
                logging.info(f"API pro aktivované ceny aFRR vrátilo (pro {day_to_fetch}): {xml_str[:250].replace(chr(10), '').replace(chr(13), '')}...")
                continue 
            
            df_day_prices = _parse_activated_balancing_price_xml_modular(xml_str)
            if not df_day_prices.empty:
                all_fetched_data.append(df_day_prices)

        except requests.exceptions.HTTPError as e:
            error_text = e.response.text[:250].replace(chr(10),'').replace(chr(13),'') if e.response else "No response text"
            logging.error(f"HTTP Chyba při načítání aktivovaných cen aFRR (pro {day_to_fetch}): {e.response.status_code if e.response else 'N/A'}. Odpověď: {error_text}...")
        except requests.exceptions.RequestException as e:
            logging.error(f"Chyba spojení při načítání aktivovaných cen aFRR (pro {day_to_fetch}): {e}.")
        except Exception as e:
            logging.error(f"Neznámá chyba při stahování/zpracování aktivovaných cen aFRR (pro {day_to_fetch}): {e}.")
    
    if not all_fetched_data:
        return pd.DataFrame()

    df_afrr_prices_raw = pd.concat(all_fetched_data, ignore_index=True)
    
    temp_local_tz = pytz.timezone(local_tz_str) # <-- Zde je local_tz_str již definována
    
    if df_afrr_prices_raw['Timestamp'].dt.tz is not None:
        df_afrr_prices_raw['Timestamp'] = df_afrr_prices_raw['Timestamp'].dt.tz_convert(None)

    df_afrr_prices_raw['Timestamp_aware_local'] = df_afrr_prices_raw['Timestamp'].dt.tz_localize('UTC', ambiguous='NaT', nonexistent='NaT').dt.tz_convert(temp_local_tz)

    df_afrr_prices_filtered = df_afrr_prices_raw[df_afrr_prices_raw['Timestamp_aware_local'].dt.date == target_date].copy()
    
    if not df_afrr_prices_filtered.empty:
        df_afrr_prices_filtered['Timestamp'] = df_afrr_prices_filtered['Timestamp_aware_local'].dt.tz_convert('UTC').dt.tz_localize(None)
        df_afrr_prices_filtered = df_afrr_prices_filtered.drop(columns=['Timestamp_aware_local'])

        df_afrr_prices_filtered = df_afrr_prices_filtered.dropna(subset=['Timestamp'])
        return df_afrr_prices_filtered
    else:
        return pd.DataFrame()


# --- FUNKCE PRO NAČÍTÁNÍ REZERVOVANÉ KAPACITY (A15) ---

def _parse_procured_capacity_xml_modular(xml_data_str: str, process_type: str, area_domain: str, market_agreement_type: str) -> list:
    data_points = []
    ns = {'bmd': 'urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1'}
    try:
        root = ET.fromstring(xml_data_str)
    except ET.ParseError as e:
        logging.warning(f"Chyba parsování XML pro rezervovanou kapacitu: {e}")
        return []
    for time_series in root.findall(".//bmd:TimeSeries", ns):
        timeseries_id_elem = time_series.find(".//bmd:mRID", ns)
        timeseries_id = timeseries_id_elem.text if timeseries_id_elem is not None else "N/A"
        direction_elem = time_series.find(".//bmd:flowDirection.direction", ns)
        direction = "Up" if direction_elem is not None and direction_elem.text == "A01" else "Down" if direction_elem is not None and direction_elem.text == "A02" else "Unknown"
        for period in time_series.findall(".//bmd:Period", ns):
            start_time_str = period.findtext('bmd:timeInterval/bmd:start', default=None, namespaces=ns)
            resolution_str = period.findtext('bmd:resolution', default="PT15M", namespaces=ns)
            if start_time_str is None or resolution_str is None:
                continue
            step = timedelta(minutes=15)
            if resolution_str in ["PT60M", "P1H"]:
                step = timedelta(hours=1)
            elif resolution_str == "PT30M":
                step = timedelta(minutes=30)
            elif resolution_str == "PT1M":
                step = timedelta(minutes=1)
            try:
                start_datetime = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%MZ")
            except ValueError:
                logging.warning(f"Nelze parsovat start_time pro kapacitu: {start_time_str}")
                continue
            for point in period.findall(".//bmd:Point", ns):
                pos_str = point.findtext('bmd:position', default="0", namespaces=ns) 
                position = int(pos_str) if pos_str is not None else 0
                capacity_str = point.findtext('bmd:quantity', default=None, namespaces=ns) 
                price_str = point.findtext('bmd:procurement_Price.amount', default=None, namespaces=ns) 
                
                capacity = float(capacity_str) if capacity_str is not None else None
                price = float(price_str) if price_str is not None else 0.0 
                if capacity is None:
                    continue
                timestamp = start_datetime + (position - 1) * step
                data_points.append({
                    "Timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "TimeSeries ID": timeseries_id,
                    "Capacity (MW)": capacity,
                    "Capacity Price (EUR/MW)": price,
                    "Direction": direction,
                    "ProcessType": process_type,
                    "AreaDomain": area_domain,
                    "MarketAgreementType": market_agreement_type
                })
    return data_points


@st.cache_data(ttl=3600)
def fetch_procured_capacity_data(
    target_date: datetime.date,
    country_code: str,
    process_type: str = "A51", 
    market_agreement_type: str = "A01", 
    document_type: str = "A15" 
) -> pd.DataFrame:
    """
    Stahuje a parsuje data "Procured balancing reserves" (A15) z ENTSOE-E API.
    Vrací DataFrame s časovou řadou cen a objemů za rezervovanou kapacitu (pro Day-Ahead).
    """
    api_key = st.secrets["entsoe_api"]["token"]
    entsoe_api_url = "https://web-api.tp.entsoe.eu/api"
    
    country_eic_map = {
        'CZ': '10YCZ-CEPS-----N',
        'DE': '10Y1001A1001A83F',
    }
    area_domain = country_eic_map.get(country_code, None)

    if not area_domain:
        logging.error(f"Nepodporovaný kód země pro rezervovanou kapacitu: {country_code}.")
        return pd.DataFrame()

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
    
    all_extracted_data = []

    try:
        response = requests.get(url=entsoe_api_url, params=params, timeout=90)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', '')

        if 'application/zip' in content_type or response.content.startswith(b'PK\x03\x04'):
            try:
                with io.BytesIO(response.content) as bio_zip1:
                    with zipfile.ZipFile(bio_zip1) as zip_file_level1:
                        for file_name_l1 in zip_file_level1.namelist():
                            with zip_file_level1.open(file_name_l1) as content_l1:
                                bytes_l1 = content_l1.read()
                                
                                is_nested_zip_processed = False
                                if file_name_l1.lower().endswith('.zip'):
                                    try:
                                        with io.BytesIO(bytes_l1) as bio_zip2:
                                            with zipfile.ZipFile(bio_zip2) as zip_file_level2:
                                                for file_name_l2 in zip_file_level2.namelist():
                                                    if file_name_l2.lower().endswith('.xml'):
                                                        with zip_file_level2.open(file_name_l2) as xml_file_l2:
                                                            try:
                                                                xml_data_str = xml_file_l2.read().decode("utf-8")
                                                            except UnicodeDecodeError:
                                                                xml_data_str = xml_file_l2.read().decode("ISO-8859-1", errors='replace')
                                                            
                                                            parsed_points = _parse_procured_capacity_xml_modular(xml_data_str, process_type, area_domain, market_agreement_type)
                                                            all_extracted_data.extend(parsed_points)
                                                is_nested_zip_processed = True
                                    except zipfile.BadZipFile:
                                        logging.warning(f"Soubor {file_name_l1} vypadal jako ZIP, ale není platný pro kapacitu.")
                                    except Exception as e_nested:
                                        logging.error(f"Chyba při zpracování vnořeného ZIPu pro kapacitu {file_name_l1}: {e_nested}")

                                if not is_nested_zip_processed and file_name_l1.lower().endswith('.xml'):
                                    try:
                                        xml_data_str = bytes_l1.decode("utf-8")
                                    except UnicodeDecodeError:
                                        xml_data_str = bytes_l1.decode("ISO-8859-1", errors='replace')
                                    
                                    parsed_points = _parse_procured_capacity_xml_modular(xml_data_str, process_type, area_domain, market_agreement_type)
                                    all_extracted_data.extend(parsed_points)
            except zipfile.BadZipFile:
                logging.error(f"Chyba: Odpověď byla označena jako ZIP, ale není to platný ZIP archiv pro rezervovanou kapacitu.")
            except Exception as e_zip_outer:
                 logging.error(f"Neznámá chyba při zpracování hlavního ZIPu pro rezervovanou kapacitu: {e_zip_outer}")
        
        elif 'application/xml' in content_type or 'text/xml' in content_type:
            try:
                xml_data_str = response.content.decode("utf-8")
            except UnicodeDecodeError:
                xml_data_str = response.content.decode("ISO-8859-1", errors='replace')
            
            if "NoMatchingData" in xml_data_str or "Error_Reason" in xml_data_str :
                 logging.info(f"API pro rezervovanou kapacitu vrátilo NoMatchingData/Error_Reason (pro {target_date}): {xml_data_str[:250].replace(chr(10),'').replace(chr(13),'')}...")
            else:
                parsed_points = _parse_procured_capacity_xml_modular(xml_data_str, process_type, area_domain, market_agreement_type)
                all_extracted_data.extend(parsed_points)
        else:
            logging.warning(f"Neočekávaný Content-Type pro rezervovanou kapacitu: {content_type}. Obsah (prvních 200b): {response.content[:200]}")

    except requests.exceptions.HTTPError as e:
        error_text = e.response.text[:250].replace(chr(10),'').replace(chr(13),'') if e.response else "No response text"
        logging.error(f"HTTP Chyba při načítání rezervované kapacity: {e.response.status_code if e.response else 'N/A'} pro {target_date}. Odpověď: {error_text}...")
    except requests.exceptions.RequestException as e:
        logging.error(f"Chyba spojení při načítání rezervované kapacity: {e} pro {target_date}.")
    except Exception as e:
        logging.error(f"Neznámá chyba při stahování/základním zpracování pro rezervovanou kapacitu pro {target_date}: {e}")

    df_capacity = pd.DataFrame(all_extracted_data)
    
    if not df_capacity.empty and 'Timestamp' not in df_capacity.columns:
        logging.error("Chyba: 'Timestamp' sloupec chybí v DataFrame z rezervované kapacity!")
        return pd.DataFrame()

    if not df_capacity.empty:
        df_capacity['Timestamp'] = pd.to_datetime(df_capacity['Timestamp'], errors='coerce')
        if not df_capacity['Timestamp'].dt.tz is None: 
            df_capacity['Timestamp'] = df_capacity['Timestamp'].dt.tz_convert('UTC').dt.tz_localize(None)
        
        df_capacity = df_capacity.dropna(subset=['Timestamp'])
        return df_capacity
    else:
        return pd.DataFrame()

# --- POMOCNÉ FUNKCE PRO AGREGÁTOVANÉ NABÍDKY (A24) ---
# Tuto funkci bylo třeba přesunout na vyšší úroveň, aby byla dostupná pro _fetch_single_aggregated_bids_data
def _parse_aggregated_bids_xml_modular(xml_data_str: str) -> pd.DataFrame:
    """
    Parsuje XML obsah pro agregované nabídky (A24).
    Vrací DataFrame s UTC-naive datetime a objemy (offered, activated, unavailable).
    """
    ns = {'ns': 'urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1'}
    data = []
    try:
        root = ET.fromstring(xml_data_str)
    except ET.ParseError as e:
        logging.warning(f"Chyba parsování XML pro agregované nabídky: {e}")
        return pd.DataFrame()

    for ts in root.findall('.//ns:TimeSeries', ns):
        flow_direction = ts.findtext('ns:flowDirection.direction', default=None, namespaces=ns)
        period = ts.find('ns:Period', ns)
        if period is not None:
            start_time_str = period.findtext('ns:timeInterval/ns:start', default=None, namespaces=ns)
            resolution_str = period.findtext('ns:resolution', default="PT15M", namespaces=ns)
            if start_time_str is None or resolution_str is None:
                continue
            
            start_time_utc_aware = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%MZ").replace(tzinfo=pytz.UTC)

            if resolution_str == "PT15M":
                step = timedelta(minutes=15)
            elif resolution_str in ("PT60M", "P1H"):
                step = timedelta(hours=1)
            elif resolution_str == "PT30M":
                step = timedelta(minutes=30)
            elif resolution_str == "PT1M":
                step = timedelta(minutes=1)
            else:
                step = timedelta(minutes=15)

            for point in period.findall('ns:Point', ns):
                pos_str = point.findtext('ns:position', default="0", namespaces=ns)
                pos = int(pos_str) if pos_str is not None else 0
                offered_str = point.findtext('ns:quantity', default=None, namespaces=ns)
                activated_str = point.findtext('ns:secondaryQuantity', default=None, namespaces=ns)
                unavailable_str = point.findtext('ns:unavailable_Quantity.quantity', default=None, namespaces=ns)
                
                offered = float(offered_str) if offered_str is not None else float('nan')
                activated = float(activated_str) if activated_str is not None else float('nan')
                unavailable = float(unavailable_str) if unavailable_str is not None else float('nan')
                
                dt_utc_aware = start_time_utc_aware + (pos - 1) * step
                
                data.append({
                    "Timestamp": dt_utc_aware.replace(tzinfo=None),
                    "flowDirection": flow_direction,
                    "offered": offered,
                    "activated": activated,
                    "unavailable": unavailable
                })
    return pd.DataFrame(data)

# Pomocná funkce pro vyplnění NaN offered hodnot
def _fill_offered_nearest_modular(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["afrr_plus_offered", "afrr_minus_offered"]:
        if col in df.columns:
            df[col] = df[col].interpolate(method="nearest", limit_direction="both")
    return df

# --- PŮVODNÍ FUNKCE PRO NAČÍTÁNÍ AGREGÁTOVANÝCH NABÍDEK (A24) ---
# Tuto funkci budeme volat z nové wrapper funkce fetch_all_aggregated_bids_data
# aby byla stale cachovana a s logikou parsovani a cisteni.
@st.cache_data(ttl=3600)
def _fetch_single_aggregated_bids_data(
    target_date: datetime.date, 
    country_code: str,
    process_type: str, 
    document_type: str = "A24" 
) -> pd.DataFrame:
    """
    Načítá a parsuje agregované nabídky (A24) pro danou zemi a datum pro JEDEN process_type.
    """
    api_key = st.secrets["entsoe_api"]["token"]
    entsoe_api_url = "https://web-api.tp.entsoe.eu/api"
    
    country_eic_map = {
        'CZ': '10YCZ-CEPS-----N',
        'DE': '10Y1001A1001A83F',
    }
    area_domain = country_eic_map.get(country_code, None)

    if not area_domain:
        logging.error(f"Nepodporovaný kód země pro agregované nabídky: {country_code}.")
        return pd.DataFrame()

    dates_to_fetch = [target_date - timedelta(days=1), target_date]
    all_fetched_data = []

    for day_to_fetch in dates_to_fetch:
        start_period = datetime(day_to_fetch.year, day_to_fetch.month, day_to_fetch.day, 0, 0, 0)
        end_period = start_period + timedelta(days=1)

        period_start_str = start_period.strftime("%Y%m%d%H%M")
        period_end_str = end_period.strftime("%Y%m%d%H%M")

        params = {
            "securityToken": api_key,
            "documentType": document_type,
            "processType": process_type,
            "area_Domain": area_domain,
            "periodStart": period_start_str,
            "periodEnd": period_end_str
        }
        
        try:
            response = requests.get(url=entsoe_api_url, params=params, timeout=60)
            response.raise_for_status()

            xml_str = response.content.decode("utf-8", errors="replace")
            
            if "NoMatchingData" in xml_str or "Error_Reason" in xml_str:
                logging.info(f"API pro agregované nabídky vrátilo (pro {day_to_fetch}, {process_type}): {xml_str[:250].replace(chr(10), '').replace(chr(13), '')}...")
                continue 
            
            df_day_bids = _parse_aggregated_bids_xml_modular(xml_str) # <-- Zde se volá funkce
            if not df_day_bids.empty:
                all_fetched_data.append(df_day_bids)

        except requests.exceptions.HTTPError as e:
            error_text = e.response.text[:250].replace(chr(10),'').replace(chr(13),'') if e.response else "No response text"
            logging.error(f"HTTP Chyba při načítání agregovaných nabídek (pro {day_to_fetch}, {process_type}): {e.response.status_code if e.response else 'N/A'}. Odpověď: {error_text}...")
        except requests.exceptions.RequestException as e:
            logging.error(f"Chyba spojení při načítání agregovaných nabídek (pro {day_to_fetch}, {process_type}): {e}.")
        except Exception as e:
            logging.error(f"Neznámá chyba při stahování/zpracování agregovaných nabídek (pro {day_to_fetch}, {process_type}): {e}.")
    
    if not all_fetched_data:
        return pd.DataFrame()

    df_agg_bids_raw = pd.concat(all_fetched_data, ignore_index=True)
    
    country_timezones_map_for_loader = {
        'CZ': 'Europe/Prague',
        'DE': 'Europe/Berlin',
    }
    local_tz_str = country_timezones_map_for_loader.get(country_code, 'UTC') 
    temp_local_tz = pytz.timezone(local_tz_str)
    
    if df_agg_bids_raw['Timestamp'].dt.tz is not None:
        df_agg_bids_raw['Timestamp'] = df_agg_bids_raw['Timestamp'].dt.tz_convert(None)

    df_agg_bids_raw['Timestamp_aware_local'] = df_agg_bids_raw['Timestamp'].dt.tz_localize('UTC', ambiguous='NaT', nonexistent='NaT').dt.tz_convert(temp_local_tz)

    df_agg_bids_filtered = df_agg_bids_raw[df_agg_bids_raw['Timestamp_aware_local'].dt.date == target_date].copy()
    
    if not df_agg_bids_filtered.empty:
        df_agg_bids_filtered['Timestamp'] = df_agg_bids_filtered['Timestamp_aware_local'].dt.tz_convert('UTC').dt.tz_localize(None)
        df_agg_bids_filtered = df_agg_bids_filtered.drop(columns=['Timestamp_aware_local'])

        piv = df_agg_bids_filtered.pivot_table(
            index="Timestamp",
            columns="flowDirection",
            values=["offered", "activated", "unavailable"]
        )
        piv.columns = [
            f"afrr_plus_{col}" if fd == "A01" else f"afrr_minus_{col}"
            for col, fd in piv.columns
        ]
        piv = piv.reset_index()
        
        all_expected_cols = [
            "Timestamp",
            "afrr_plus_offered", "afrr_plus_activated", "afrr_plus_unavailable",
            "afrr_minus_offered", "afrr_minus_activated", "afrr_minus_unavailable"
        ]
        for col in all_expected_cols:
            if col not in piv.columns:
                piv[col] = float('nan')

        piv = piv[all_expected_cols].sort_values("Timestamp")
        
        piv = _fill_offered_nearest_modular(piv)

        for col in ["afrr_minus_offered", "afrr_minus_activated", "afrr_minus_unavailable"]:
            if col in piv.columns:
                piv[col] = -piv[col]
        
        return piv
    else:
        return pd.DataFrame()

# --- NOVÁ FUNKCE: Wrapper pro načítání obou typů agregovaných nabídek ---
@st.cache_data(ttl=3600)
def fetch_all_aggregated_bids_data(
    target_date: datetime.date,
    country_code: str
) -> dict[str, pd.DataFrame]:
    """
    Načítá a kešuje oba typy agregovaných nabídek (A67 a A68).
    Vrací slovník {process_type: DataFrame}.
    """
    data = {}
    
    # Načtení Central Selection (A67)
    df_central = _fetch_single_aggregated_bids_data(target_date, country_code, "A67")
    data["A67"] = df_central

    # Načtení Local Selection (A68)
    df_local = _fetch_single_aggregated_bids_data(target_date, country_code, "A68")
    data["A68"] = df_local

    return data