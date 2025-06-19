# app_SVR_dash.py (BEZ RÁMEČKŮ, OPTIMIZOVANÁ VERZE)

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import plotly.express as px
import pytz
import base64 

# Import modulů
import data_loader as dl 
import plot_generator as pg 

# TOTO MUSÍ BÝT ABSOLUTNĚ PRVNÍ PŘÍKAZ STREAMLITU V CELÉM SKRIPTU.
st.set_page_config(
    layout="wide",
    page_title="SVR dashboard",
    page_icon="⚡",
    initial_sidebar_state="expanded"
)

# Funkce pro načtení a zakódování lokálního obrázku
@st.cache_data
def get_img_as_base64(file):
    with open(file, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode()

img = get_img_as_base64("assets/logo.svg")

# Vytvoření sloupců pro hlavičku
col1, col2, col3 = st.columns([5, 1, 1])

# První sloupec: Nadpis a podtitulek
with col1:
    st.title("Energy Dashboard - SVR")
    st.markdown("Vítejte v dashboardu pro zobrazení SVR dat z evropských energetických trhů.")
    st.markdown("Data jsou získána skrze entsoe-e")

# Druhý sloupec: Prázdná mezera
with col2:
    st.write("")

# Třetí sloupec: Klikací logo
with col3:
    st.markdown(
        f'<a href="https://www.egubrno.cz/" target="_blank"><img src="data:image/svg+xml;base64,{img}" width="150"></a>',
        unsafe_allow_html=True
    )


# --- Sidebar pro uživatelské vstupy ---
st.sidebar.header("Nastavení dat")

# Vstup pro výběr data
today = datetime.now().date()
# Umožní vybrat datum do zítřka (day-ahead prices jsou obvykle pro zítřek)
max_allowed_date = today + timedelta(days=1) 
selected_date = st.sidebar.date_input(
    "Vyberte datum pro zobrazení dat:",
    value=today,
    max_value=max_allowed_date,
    min_value=datetime(2019, 1, 1).date() # Zabrání výběru příliš starých dat
)

# Vstup pro výběr země
country_codes = ['CZ']
selected_country = st.sidebar.selectbox(
    "Zatím k dispozici pouze CZ:",
    options=country_codes,
    index=country_codes.index('CZ')
)

# --- Výběr směru nabídkové křivky (pevně "Oba") ---
selected_bid_direction_filter = "Oba" 

# --- Logika pro slider hodiny a konverze UTC ---
# Získání časové zóny pro selected_country pro lokální konverzi
country_timezones_map = {
    'CZ': 'Europe/Prague',
    'DE': 'Europe/Berlin', 
}
user_tz_str = country_timezones_map.get(selected_country, 'UTC') 
user_tz = pytz.timezone(user_tz_str)

days_ago = (today - selected_date).days
selected_hour_local = 0 

st.sidebar.markdown("---")

# Slider je VŽDY zobrazen
selected_hour_local = st.sidebar.slider(
    "Vyberte hodinu (lokální čas) pro grafy křivek:", 
    min_value=0,
    max_value=23,
    value=datetime.now().hour,
    step=1
)

# Zobrazení informační zprávy o chování bid křivek
if days_ago < 100: 
    st.sidebar.info("Pro aktuální a blízká data (cca 100 dní) bude nabídková křivka RE aFRR vždy zobrazena pro hodinu 00:00, bez ohledu na výběr slideru. Slider ovlivňuje pouze Day-Ahead čáru a grafy kapacity.")
    bid_curve_filter_hour_utc = 0 
else: 
    st.sidebar.info("Pro historická data slider ovlivňuje jak nabídkovou křivku aFRR, tak Day-Ahead čáru a grafy kapacity.")
    naive_dt_local_bid_filter = datetime(selected_date.year, selected_date.month, selected_date.day, selected_hour_local, 0, 0)
    aware_dt_local_bid_filter = user_tz.localize(naive_dt_local_bid_filter, is_dst=None) 
    bid_curve_filter_hour_utc = aware_dt_local_bid_filter.astimezone(pytz.utc).replace(tzinfo=None).hour


st.sidebar.markdown("---") 
st.sidebar.markdown(
    f'<span style="color: rgb(255, 153, 0);">by <b><a href="https://www.linkedin.com/in/patrikpetovsky/" target="_blank" style="color: rgb(255, 153, 0); text-decoration: none;">Patrik Petovsky</a></b> <img src="https://upload.wikimedia.org/wikipedia/commons/c/ca/LinkedIn_logo_initials.png" width="20" height="20" style="vertical-align: middle;"></span>', 
    unsafe_allow_html=True
)

# --- Konverze lokální vybrané hodiny na UTC hodinu pro Day-Ahead čáru a grafy kapacity ---
naive_dt_local_day_ahead_line = datetime(selected_date.year, selected_date.month, selected_date.day, selected_hour_local, 0, 0)
aware_dt_local_day_ahead_line = user_tz.localize(naive_dt_local_day_ahead_line, is_dst=None) 
selected_hour_for_day_ahead_line_and_capacity_filter_utc = aware_dt_local_day_ahead_line.astimezone(pytz.utc).replace(tzinfo=None).hour

# Hodina pro zobrazení v titulcích grafů (vždy lokální hodina ze slideru)
selected_hour_for_display = selected_hour_local


# --- Hlavní obsah dashboardu ---
st.header(f"Přehled pro {selected_country} - {selected_date.strftime('%d.%m.%Y')}")


# --- Načtení dat s vizuální zpětnou vazbou v JEDNOM ZAVŘENÉM ST.STATUS BLOKU ---
day_ahead_data = pd.DataFrame()
afrr_activation_data = pd.DataFrame()
procured_capacity_data = pd.DataFrame()
all_aggregated_bids_data = {} # Pro uložení A67 i A68
balancing_bids_afrr = pd.DataFrame()

all_data_loaded_successfully = True

# Jediný status box pro všechny načítání
with st.status("Načítání dat z ENTSOE-E API...", expanded=False) as status:
    st.write("Načítám denní ceny...")
    day_ahead_data = dl.fetch_day_ahead_prices_data(selected_country, selected_date)
    if day_ahead_data.empty:
        status.write("⚠️ Denní ceny nejsou dostupné pro vybrané datum.")
        all_data_loaded_successfully = False
    else:
        status.write("✅ Denní ceny načteny.")
    
    st.write("Načítám ceny aktivace aFRR...")
    afrr_activation_data = dl.fetch_afrr_activation_prices_data(selected_date, selected_country)
    if afrr_activation_data.empty:
        status.write("⚠️ Ceny aktivace aFRR nejsou dostupné pro vybrané datum.")
        all_data_loaded_successfully = False
    else:
        status.write("✅ Ceny aktivace aFRR načteny.")

    st.write("Načítám rezervovanou kapacitu...")
    procured_capacity_data = dl.fetch_procured_capacity_data(
        target_date=selected_date,
        country_code=selected_country
    )
    if procured_capacity_data.empty:
        status.write("⚠️ Data rezervované kapacity nejsou dostupná pro vybrané datum.")
        all_data_loaded_successfully = False
    else:
        status.write("✅ Data rezervované kapacity načtena.")

    st.write("Načítám agregované nabídky (Central i Local Selection)...")
    all_aggregated_bids_data = dl.fetch_all_aggregated_bids_data(
        target_date=selected_date,
        country_code=selected_country
    )
    if all_aggregated_bids_data.get("A67", pd.DataFrame()).empty and all_aggregated_bids_data.get("A68", pd.DataFrame()).empty:
        status.write("⚠️ Agregované nabídky nejsou dostupné pro vybrané datum.")
        all_data_loaded_successfully = False
    else:
        status.write("✅ Agregované nabídky načteny.")

    st.write("Načítám balancing bids pro aFRR...")
    balancing_bids_afrr = dl.fetch_balancing_bids_for_day_modular(
        target_date=selected_date,
        country_code=selected_country,
        process_type="A51"
    )
    if balancing_bids_afrr.empty:
        status.write("⚠️ Balancing bids pro aFRR nejsou dostupné pro vybrané datum.")
        all_data_loaded_successfully = False
    else:
        status.write("✅ Balancing bids načteny.")
    
    # Aktualizace finálního stavu status boxu
    if all_data_loaded_successfully:
        status.update(label="Načítání dat dokončeno! ✅", state="complete", expanded=False) 
    else:
        status.update(label="Načítání dat dokončeno s problémy. ⚠️", state="error", expanded=True) # Rozbalí se, pokud jsou chyby


# --- Rozložení grafů do sloupců a řad (2x2 grid) ---
col1_row1, col2_row1 = st.columns(2)

with col1_row1:
    st.subheader("Ceny elektřiny na denním trhu a cen aktivace aFRR") # Zpět na subheader
    st.write("")  
    st.write("")  
    st.write("")  
    st.write("")  
    st.write("")  

    fig_day_ahead = pg.create_day_ahead_price_plot(
        df_prices=day_ahead_data, 
        country=selected_country, 
        date=selected_date, 
        user_tz_str=user_tz_str,
        df_afrr_activation_prices=afrr_activation_data 
    )
    st.plotly_chart(fig_day_ahead, use_container_width=True)

with col2_row1:
    st.subheader(f"Agregované aktivace a nabídky aFRR") # Zpět na subheader
    
    # Přepínač nyní uvnitř sloupce (filtrování z již načtených dat)
    selected_agg_bids_process_type_label = st.radio(
        "Vyberte typ agregovaných nabídek:",
        options=["Central Selection (A67)", "Local Selection (A68)"],
        index=0, # Defaultně Central Selection
        horizontal=True,
        key="agg_bids_radio", # Přidán key pro unikátnost
        #help="Central Selection (A67) je pro celoevropské agregované nabídky, Local Selection (A68) pro lokální"
    )
    
    # Získání dat pro vybraný typ
    selected_agg_bids_process_type_code = {
        "Central Selection (A67)": "A67",
        "Local Selection (A68)": "A68"
    }[selected_agg_bids_process_type_label]
    
    # Zde se vybere již načtený DataFrame
    aggregated_bids_data_for_plot = all_aggregated_bids_data.get(selected_agg_bids_process_type_code, pd.DataFrame())

    if aggregated_bids_data_for_plot.empty:
        st.info(f"Žádná data pro {selected_agg_bids_process_type_label} nejsou dostupná.")
    
    fig_agg_bids = pg.create_aggregated_bids_plot(
        df_agg_bids=aggregated_bids_data_for_plot, # Použijeme filtrovaná data
        country=selected_country,
        date=selected_date,
        user_tz_str=user_tz_str,
        selected_process_type_label=selected_agg_bids_process_type_label # Pro titulek grafu
    )
    st.plotly_chart(fig_agg_bids, use_container_width=True)


col1_row2, col2_row2 = st.columns(2)

with col1_row2:
    st.subheader(f"Nabídková křivka regulační energie (RE) aFRR pro {selected_hour_for_display:02d}:00-{selected_hour_for_display+1:02d}:00") # Zpět na subheader
    
    # --- Vložení prázdných řádků pro zarovnání výšky grafu ---  
    st.write("")  
    st.write("")  
    st.write("")  
    # --- KONEC VKLÁDÁNÍ ---

    fig_bids_curve, cumulative_bids_data_for_display = pg.create_cumulative_bid_curve_plot(
        df_raw_bids=balancing_bids_afrr, 
        selected_date=selected_date, 
        bid_curve_filter_hour_utc=bid_curve_filter_hour_utc, 
        day_ahead_line_hour_utc=selected_hour_for_day_ahead_line_and_capacity_filter_utc, 
        country=selected_country,
        bid_type="aFRR",
        display_local_hour=selected_hour_for_display, 
        df_day_ahead_prices=day_ahead_data, 
        selected_bid_direction=selected_bid_direction_filter 
    )
    st.plotly_chart(fig_bids_curve, use_container_width=True) 

    # if st.checkbox("Zobrazit data nabídkových křivek aFRR pro vybranou hodinu", key="raw_data_cumulative_bids"):
    #     if not cumulative_bids_data_for_display.empty:
    #         st.dataframe(cumulative_bids_data_for_display)
    #     else:
    #         st.info("Žádná kumulovaná data pro zobrazení.")


with col2_row2:
    st.subheader(f"Nabídková křivka rezervovaného výkonu (RV) pro {selected_hour_for_display:02d}:00-{selected_hour_for_display+1:02d}:00") # Zpět na subheader
    
    show_weighted_avg_capacity = st.checkbox("Zobrazit vážený průměr ceny RV", key="show_weighted_avg_capacity")

    fig_proc_capacity_curve, cumulative_proc_capacity_data_for_display = pg.create_cumulative_procured_capacity_curve_plot(
        df_raw_capacity=procured_capacity_data,
        selected_date=selected_date,
        selected_hour_utc=selected_hour_for_day_ahead_line_and_capacity_filter_utc, 
        country=selected_country,
        display_local_hour=selected_hour_for_display,
        show_weighted_average=show_weighted_avg_capacity 
    )
    st.plotly_chart(fig_proc_capacity_curve, use_container_width=True) 

    # if st.checkbox("Zobrazit data nabídkových křivek kapacity pro vybranou hodinu", key="raw_data_cumulative_capacity_bids"):
    #     if not cumulative_proc_capacity_data_for_display.empty:
    #         st.dataframe(cumulative_proc_capacity_data_for_display)
    #     else:
    #         st.info("Žádná kumulovaná data kapacity pro zobrazení.")



