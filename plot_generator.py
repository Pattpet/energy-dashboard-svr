# plot_generator.py (UPRAVENO pro vodoznak s logem)

import streamlit as st 
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime, timedelta
import pytz 
import logging
import base64
from pathlib import Path

opa = 0.05

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_logo_as_base64(logo_path: str = "assets/logo.svg") -> str | None:
    """
    Načte SVG logo ze souboru, zakóduje ho do base64 a vrátí jako data URI.
    Vrací None, pokud soubor neexistuje.
    """
    logo_file = Path(logo_path)
    if not logo_file.is_file():
        logging.warning(f"Soubor s logem nebyl nalezen na cestě: {logo_path}")
        return None
    
    encoded_logo = base64.b64encode(logo_file.read_bytes()).decode()
    return f"data:image/svg+xml;base64,{encoded_logo}"

"""
Tento modul obsahuje funkce pro generování různých typů Plotly grafů.
Přijímá připravená data (Pandas DataFrames) a vrací Plotly Figure objekty.
"""

# --- POMOCNÉ FUNKCE PRO PŘÍPRAVU DAT KUMULATIVNÍCH KŘIVEK ---

def _prepare_afrr_bids_for_plot(df_group_raw: pd.DataFrame, direction: str, price_col: str, power_col: str) -> tuple[pd.DataFrame, float]:
    """
    Pomocná funkce pro přípravu dat kumulativní křivky aFRR bids.
    'Up' křivka je rostoucí, 'Down' křivka je klesající.
    """
    if df_group_raw.empty:
        return pd.DataFrame(), 0.0 

    df_group = df_group_raw.groupby(['Timestamp', price_col, 'Direction'], as_index=False)[power_col].sum()

    if direction == "Up":
        df_group_sorted = df_group.sort_values(by=[price_col, power_col], ascending=[True, True])
        if df_group_sorted.empty: # Defenzivní kontrola
            return pd.DataFrame(), 0.0
        price_for_zero = df_group_sorted[price_col].min()
    else: # Down (aFRR-) - klesající křivka
        df_group_sorted = df_group.sort_values(by=[price_col, power_col], ascending=[False, True])
        if df_group_sorted.empty: # Defenzivní kontrola
            return pd.DataFrame(), 0.0
        price_for_zero = df_group_sorted[price_col].max() 
    
    df_group_sorted[f"Cumulative {power_col}"] = df_group_sorted[power_col].cumsum()

    # Zajištění, že df_group_sorted není prázdné před přístupem k .iloc[0]
    if df_group_sorted.empty:
        return pd.DataFrame(), 0.0

    zero_point_data = {
        "Timestamp": df_group_sorted["Timestamp"].iloc[0], 
        "Direction": direction, 
        power_col: 0.0,
        f"Cumulative {power_col}": 0.0,
        price_col: price_for_zero
    }
    zero_point = pd.DataFrame([zero_point_data])
    
    final_df = pd.concat([zero_point, df_group_sorted], ignore_index=True)
    final_df = final_df.drop_duplicates(subset=['Timestamp', 'Direction', f"Cumulative {power_col}", price_col])
    
    final_df = final_df.sort_values(by=[price_col, f"Cumulative {power_col}"], ascending=[direction == "Up", True])
    
    valid_points_for_avg = df_group[df_group[power_col] > 0]
    weighted_average = 0.0
    if not valid_points_for_avg.empty and valid_points_for_avg[power_col].sum() > 0:
        weighted_average = (valid_points_for_avg[power_col] * valid_points_for_avg[price_col]).sum() / valid_points_for_avg[power_col].sum()
    
    return final_df, weighted_average


def _prepare_capacity_for_plot(df_group_raw: pd.DataFrame, direction: str, price_col: str, power_col: str) -> tuple[pd.DataFrame, float]:
    """
    Pomocná funkce pro přípravu dat kumulativní křivky rezervované kapacity.
    Obě křivky (Up i Down) jsou rostoucí s kumulovaným výkonem.
    """
    if df_group_raw.empty:
        return pd.DataFrame(), 0.0

    df_group = df_group_raw.groupby(['Timestamp', price_col, 'Direction'], as_index=False)[power_col].sum()
    
    df_group_sorted = df_group.sort_values(by=[price_col, power_col], ascending=[True, True])

    price_for_zero = df_group_sorted[price_col].min()
    
    df_group_sorted[f"Cumulative {power_col}"] = df_group_sorted[power_col].cumsum()

    # Zajištění, že df_group_sorted není prázdné před přístupem k .iloc[0]
    if df_group_sorted.empty:
        return pd.DataFrame(), 0.0
    
    prices_above_zero = df_group_sorted[df_group_sorted[price_col] > 0]
    if not prices_above_zero.empty:
        price_for_zero = prices_above_zero[price_col].min()
        timestamp_for_zero = prices_above_zero["Timestamp"].iloc[0]
    else:
        price_for_zero = df_group_sorted[price_col].min()
        timestamp_for_zero = df_group_sorted["Timestamp"].iloc[0]

    zero_point_data = {
        "Timestamp": timestamp_for_zero, 
        "Direction": direction, 
        power_col: 0.0,
        f"Cumulative {power_col}": 0.0,
        price_col: price_for_zero
    }
    zero_point = pd.DataFrame([zero_point_data])

    
    final_df = pd.concat([zero_point, df_group_sorted], ignore_index=True)
    final_df = final_df.drop_duplicates(subset=['Timestamp', 'Direction', f"Cumulative {power_col}", price_col])
    
    final_df = final_df.sort_values(by=[price_col, f"Cumulative {power_col}"], ascending=[True, True])

    valid_points_for_avg = df_group[df_group[power_col] > 0]
    weighted_average = 0.0
    if not valid_points_for_avg.empty and valid_points_for_avg[power_col].sum() > 0:
        weighted_average = (valid_points_for_avg[power_col] * valid_points_for_avg[price_col]).sum() / valid_points_for_avg[power_col].sum()
    
    return final_df, weighted_average

# --- KONEC POMOCNÝCH FUNKCÍ ---


def create_day_ahead_price_plot(
    df_prices: pd.DataFrame, 
    country: str, 
    date: datetime.date, 
    user_tz_str: str,
    df_afrr_activation_prices: pd.DataFrame = None 
) -> go.Figure:
    """
    Generuje čárový graf denních cen elektřiny a cen aktivované regulační energie.
    Převádí časy na lokální časovou zónu pro správné zobrazení na ose X.
    Dynamický rozsah Y osy.
    """
    local_tz = pytz.timezone(user_tz_str)
    tz_name_for_display = datetime.now(local_tz).tzname() 

    all_prices_df = pd.DataFrame()
    
    df_prices_localized = pd.DataFrame() 
    if not df_prices.empty:
        df_prices_localized = df_prices.copy()
        df_prices_localized['Time'] = df_prices_localized['Time'].dt.tz_localize('UTC').dt.tz_convert(local_tz)
        
        all_prices_df = pd.concat([all_prices_df, df_prices_localized.rename(columns={'Price': 'Value'})[['Time', 'Value']]], ignore_index=True)

    df_afrr_localized = pd.DataFrame() 
    if df_afrr_activation_prices is not None and not df_afrr_activation_prices.empty:
        df_afrr_localized = df_afrr_activation_prices.copy()
        df_afrr_localized['Timestamp'] = df_afrr_localized['Timestamp'].dt.tz_localize('UTC').dt.tz_convert(local_tz)

        if not df_afrr_localized.empty:
            last_timestamp = df_afrr_localized['Timestamp'].max()
            if len(df_afrr_localized) > 1:
                resolution_td = df_afrr_localized['Timestamp'].iloc[1] - df_afrr_localized['Timestamp'].iloc[0]
            else:
                resolution_td = timedelta(minutes=15) 
            
            next_timestamp = last_timestamp + resolution_td
            
            last_row_plus = df_afrr_localized[df_afrr_localized['Timestamp'] == last_timestamp]['afrr_plus_price'].iloc[0] if 'afrr_plus_price' in df_afrr_localized.columns else None
            last_row_minus = df_afrr_localized[df_afrr_localized['Timestamp'] == last_timestamp]['afrr_minus_price'].iloc[0] if 'afrr_minus_price' in df_afrr_localized.columns else None
            
            dummy_row = pd.DataFrame([{
                'Timestamp': next_timestamp,
                'afrr_plus_price': last_row_plus,
                'afrr_minus_price': last_row_minus
            }])
            df_afrr_localized = pd.concat([df_afrr_localized, dummy_row], ignore_index=True)
            df_afrr_localized = df_afrr_localized.sort_values(by='Timestamp').reset_index(drop=True)

        if 'afrr_plus_price' in df_afrr_localized.columns:
            all_prices_df = pd.concat([all_prices_df, df_afrr_localized.rename(columns={'afrr_plus_price': 'Value'})[['Timestamp', 'Value']].rename(columns={'Timestamp': 'Time'})], ignore_index=True)
        if 'afrr_minus_price' in df_afrr_localized.columns:
            all_prices_df = pd.concat([all_prices_df, df_afrr_localized.rename(columns={'afrr_minus_price': 'Value'})[['Timestamp', 'Value']].rename(columns={'Timestamp': 'Time'})], ignore_index=True)

    if all_prices_df.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data pro zobrazení.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Ceny elektřiny a aFRR pro {country} ({date.strftime('%d.%m.%Y')})")
        return fig
    
    valid_prices = all_prices_df['Value'].dropna()
    if valid_prices.empty:
        y_min, y_max = -100, 200
    else:
        min_val_all = valid_prices.min()
        max_val_all = valid_prices.max()

        y_min = min(-10, min_val_all * 1.05)
        y_max = max_val_all * 1.05
        
        if y_max <= y_min:
            y_max = y_min + 50 
    
    fig = go.Figure()

    # --- PŘIDÁNÍ LOGA JAKO VODOZNAKU ---
    logo_source = get_logo_as_base64("assets/logo.svg")
    if logo_source:
        fig.add_layout_image(
            dict(
                source=logo_source,
                xref="paper", yref="paper",
                x=0.5, y=0.5,
                sizex=0.5, sizey=0.5,
                xanchor="center", yanchor="middle",
                sizing="contain",
                opacity=opa,
                layer="below"
            )
        )
    # --- KONEC BLOKU S LOGEM ---

    if df_afrr_activation_prices is not None and not df_afrr_activation_prices.empty and \
       'afrr_plus_price' in df_afrr_localized.columns and df_afrr_localized['afrr_plus_price'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_afrr_localized['Timestamp'], 
            y=df_afrr_localized['afrr_plus_price'],
            mode='lines',
            line_shape='hv',
            name="aFRR+ Cena aktivace",
            line=dict(color='royalblue', width=1.5), 
            hovertemplate="Čas: %{x|%H:%M}<br>aFRR+: %{y:.2f} €/MWh<extra></extra>"
        ))

    if df_afrr_activation_prices is not None and not df_afrr_activation_prices.empty and \
       'afrr_minus_price' in df_afrr_localized.columns and df_afrr_localized['afrr_minus_price'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_afrr_localized['Timestamp'], 
            y=df_afrr_localized['afrr_minus_price'],
            mode='lines',
            line_shape='hv',
            name="aFRR- Cena aktivace",
            line=dict(color='darkgreen', width=1.5), 
            hovertemplate="Čas: %{x|%H:%M}<br>aFRR-: %{y:.2f} €/MWh<extra></extra>"
        ))

    if not df_prices.empty:
        fig.add_trace(go.Scatter(
            x=df_prices_localized['Time'], 
            y=df_prices_localized['Price'],
            mode='lines',
            line_shape='hv',
            name=f"Elektřina na DT", 
            line=dict(color='red', width=2.5),
            hovertemplate="Čas: %{x|%H:%M}<br>Day-Ahead: %{y:.2f} €/MWh<extra></extra>"
        ))

    fig.update_layout(
        title=f"Ceny elektřiny a aFRR za aktivaci pro {country} ({date.strftime('%d.%m.%Y')})",
        xaxis_title=f"Čas ({tz_name_for_display})", 
        yaxis_title="Cena (€/MWh)",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top", 
            y=-0.2,
            xanchor="center",
            x=0.5
        ),
        yaxis=dict(range=[y_min, y_max]), 
    )
    
    fig.update_xaxes(
        dtick="H1",
        tickformat="%H:%M",
        type='date',
        showgrid=False
    )
    return fig

# --- NOVÁ FUNKCE: create_aggregated_bids_plot pro pozici 1,2 ---
def create_aggregated_bids_plot(
    df_agg_bids: pd.DataFrame, 
    country: str, 
    date: datetime.date, 
    user_tz_str: str,
    selected_process_type_label: str
) -> go.Figure:
    """
    Generuje graf agregovaných nabídek (objemů) pro aFRR+ a aFRR-.
    Zobrazuje offered, activated a unavailable objemy.
    """
    local_tz = pytz.timezone(user_tz_str)
    tz_name_for_display = datetime.now(local_tz).tzname()

    start_of_day_local_aware = local_tz.localize(datetime(date.year, date.month, date.day, 0, 0, 0), is_dst=None)
    end_of_day_local_aware = local_tz.localize(datetime(date.year, date.month, date.day, 23, 59, 59), is_dst=None)

    if df_agg_bids.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data agregovaných nabídek.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(
            title=f"Agregované nabídky {selected_process_type_label} pro {country} ({date.strftime('%d.%m.%Y')})",
            xaxis=dict(range=[start_of_day_local_aware, end_of_day_local_aware])
        )
        return fig

    df_agg_localized = df_agg_bids.copy()
    df_agg_localized['Timestamp'] = df_agg_localized['Timestamp'].dt.tz_localize('UTC').dt.tz_convert(local_tz)

    fig = go.Figure()
    
    # --- PŘIDÁNÍ LOGA JAKO VODOZNAKU ---
    logo_source = get_logo_as_base64("assets/logo.svg")
    if logo_source:
        fig.add_layout_image(
            dict(
                source=logo_source,
                xref="paper", yref="paper",
                x=0.5, y=0.5,
                sizex=0.5, sizey=0.5,
                xanchor="center", yanchor="middle",
                sizing="contain",
                opacity=opa,
                layer="below"
            )
        )
    # --- KONEC BLOKU S LOGEM ---
    
    # aFRR+ Activated (fill + line)
    if 'afrr_plus_activated' in df_agg_localized.columns and df_agg_localized['afrr_plus_activated'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_plus_activated'],
            mode="lines", name="aFRR+ Activated",
            line=dict(color="royalblue", width=2, shape="hv"),
            fill="tozeroy", fillcolor="rgba(0, 0, 255, 0.1)",
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR+ Activated: %{{y:.2f}} MW<extra></extra>"
        ))
    
    # aFRR- Activated (fill + line)
    if 'afrr_minus_activated' in df_agg_localized.columns and df_agg_localized['afrr_minus_activated'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_minus_activated'],
            mode="lines", name="aFRR- Activated",
            line=dict(color="darkgreen", width=2, shape="hv"),
            fill="tozeroy", fillcolor="rgba(0, 128, 0, 0.1)",
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR- Activated: %{{y:.2f}} MW<extra></extra>"
        ))

    # aFRR+ Offered (dashed line) - nyní vypnuté ve výchozím stavu
    if 'afrr_plus_offered' in df_agg_localized.columns and df_agg_localized['afrr_plus_offered'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_plus_offered'],
            mode="lines", name="aFRR+ Offered",
            line=dict(color="royalblue", width=1.5, dash="dot", shape="hv"),
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR+ Offered: %{{y:.2f}} MW<extra></extra>",
            visible='legendonly'
        ))
    
    # aFRR- Offered (dashed line) - nyní vypnuté ve výchozím stavu
    if 'afrr_minus_offered' in df_agg_localized.columns and df_agg_localized['afrr_minus_offered'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_minus_offered'],
            mode="lines", name="aFRR- Offered",
            line=dict(color="darkgreen", width=1.5, dash="dot", shape="hv"),
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR- Offered: %{{y:.2f}} MW<extra></extra>",
            visible='legendonly'
        ))

    # aFRR+ Unavailable (solid line)
    if 'afrr_plus_unavailable' in df_agg_localized.columns and df_agg_localized['afrr_plus_unavailable'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_plus_unavailable'],
            mode="lines", name="aFRR+ Unavailable",
            line=dict(color="orange", width=1.5, shape="hv"),
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR+ Unavailable: %{{y:.2f}} MW<extra></extra>"
        ))
    
    # aFRR- Unavailable (solid line)
    if 'afrr_minus_unavailable' in df_agg_localized.columns and df_agg_localized['afrr_minus_unavailable'].notna().any():
        fig.add_trace(go.Scatter(
            x=df_agg_localized['Timestamp'], y=df_agg_localized['afrr_minus_unavailable'],
            mode="lines", name="aFRR- Unavailable",
            line=dict(color="purple", width=1.5, shape="hv"),
            hovertemplate=f"Čas: %{{x|%H:%M}}<br>aFRR- Unavailable: %{{y:.2f}} MW<extra></extra>"
        ))

    fig.update_layout(
        title=f"Agregované nabídky {selected_process_type_label} pro {country} ({date.strftime('%d.%m.%Y')})",
        xaxis_title=f"Čas ({tz_name_for_display})",
        yaxis_title="Výkon (MW)",
        plot_bgcolor="white",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.2,
            xanchor="center",
            x=0.5
        ),
        font=dict(size=16)
    )
    fig.update_xaxes(
        dtick="H1",
        tickformat="%H:%M",
        type='date',
        showgrid=True, gridcolor="#eeeeee",
        range=[start_of_day_local_aware, end_of_day_local_aware]
    )
    fig.update_yaxes(
        showgrid=True, gridcolor="#eeeeee"
    )
    return fig


def create_cumulative_bid_curve_plot(
    df_raw_bids: pd.DataFrame, 
    selected_date: datetime.date, 
    bid_curve_filter_hour_utc: int, 
    day_ahead_line_hour_utc: int, 
    country: str,
    bid_type: str = "aFRR",
    display_local_hour: int = None, 
    df_day_ahead_prices: pd.DataFrame = None, 
    selected_bid_direction: str = "Oba" 
) -> tuple[go.Figure, pd.DataFrame]: 
    """
    Generuje kumulovanou nabídkovou křivku pro konkrétní hodinu a den (aFRR bids).
    """
    hour_for_title_fallback = display_local_hour if display_local_hour is not None else bid_curve_filter_hour_utc

    if df_raw_bids.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data pro nabídkové křivky.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka {bid_type} pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title_fallback:02d}:00")
        return fig, pd.DataFrame() 

    hour_for_title = display_local_hour if display_local_hour is not None else bid_curve_filter_hour_utc 

    start_time_bid_utc_naive = datetime(selected_date.year, selected_date.month, selected_date.day, bid_curve_filter_hour_utc, 0, 0)
    
    hourly_bids = df_raw_bids[
        df_raw_bids['Timestamp'] == start_time_bid_utc_naive
    ].copy() 

    if hourly_bids.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data pro nabídkové křivky pro vybranou hodinu.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka {bid_type} pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00")
        return fig, pd.DataFrame() 
    
    df_up_with_zero, weighted_avg_up = _prepare_afrr_bids_for_plot(hourly_bids[hourly_bids["Direction"] == "Up"].copy(), "Up", "Price (€/MWh)", "Power (MW)")
    df_down_with_zero, weighted_avg_down = _prepare_afrr_bids_for_plot(hourly_bids[hourly_bids["Direction"] == "Down"].copy(), "Down", "Price (€/MWh)", "Power (MW)")
    
    plots_to_combine = []
    if selected_bid_direction == "Oba" or selected_bid_direction == "Up":
        df_up_plot = df_up_with_zero[['Cumulative Power (MW)', 'Price (€/MWh)']].copy()
        df_up_plot['Curve Type'] = f'{bid_type} Up (aFRR+)' 
        plots_to_combine.append(df_up_plot)

    if selected_bid_direction == "Oba" or selected_bid_direction == "Down":
        df_down_plot = df_down_with_zero[['Cumulative Power (MW)', 'Price (€/MWh)']].copy()
        df_down_plot['Curve Type'] = f'{bid_type} Down (aFRR-)'
        plots_to_combine.append(df_down_plot)

    if not plots_to_combine: 
        fig = go.Figure()
        fig.add_annotation(text=f"Nejsou data pro směr '{selected_bid_direction}'.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka {bid_type} pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00")
        return fig, pd.DataFrame() 

    combined_plot_df = pd.concat(plots_to_combine, ignore_index=True)
    
    fig = px.line(
        combined_plot_df,
        x="Cumulative Power (MW)",
        y="Price (€/MWh)",
        color="Curve Type",
        title=f"Nabídková křivka RE {bid_type} pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00",
        labels={ "Cumulative Power (MW)": "Kumulovaný výkon (MW)", "Price (€/MWh)": "Cena (€/MWh)", "Curve Type": ""},
        hover_data={"Price (€/MWh)": ":.2f", "Cumulative Power (MW)": ":.2f"},
        line_shape='hv',
        color_discrete_map={ f'{bid_type} Up (aFRR+)': 'royalblue', f'{bid_type} Down (aFRR-)': 'darkgreen' }
    )
    
    # --- PŘIDÁNÍ LOGA JAKO VODOZNAKU ---
    logo_source = get_logo_as_base64("assets/logo.svg")
    if logo_source:
        fig.add_layout_image(
            dict(
                source=logo_source,
                xref="paper", yref="paper",
                x=0.5, y=0.5,
                sizex=0.5, sizey=0.5,
                xanchor="center", yanchor="middle",
                sizing="contain",
                opacity=opa,
                layer="below"
            )
        )
    # --- KONEC BLOKU S LOGEM ---

    if not combined_plot_df.empty:
        if weighted_avg_up > 0 or weighted_avg_up < 0: 
            fig.add_hline( y=weighted_avg_up, line_dash="dash", line_color="royalblue", annotation_text=f"aFRR+ průměr: {weighted_avg_up:.2f} €/MWh", annotation_position="top right", annotation_font_color="royalblue", row="all", col="all")
        if weighted_avg_down > 0 or weighted_avg_down < 0: 
            fig.add_hline(y=weighted_avg_down, line_dash="dash", line_color="darkgreen", annotation_text=f"aFRR- průměr: {weighted_avg_down:.2f} €/MWh", annotation_position="bottom right", annotation_font_color="darkgreen", row="all", col="all")

    if df_day_ahead_prices is not None and not df_day_ahead_prices.empty:
        target_timestamp_utc_naive = datetime(selected_date.year, selected_date.month, selected_date.day, day_ahead_line_hour_utc, 0, 0)
        day_ahead_price_row = df_day_ahead_prices[df_day_ahead_prices['Time'] == target_timestamp_utc_naive]
        if not day_ahead_price_row.empty:
            day_ahead_price = day_ahead_price_row['Price'].iloc[0]
            fig.add_hline(y=day_ahead_price, line_dash="dot", line_color="red", annotation_text=f"Cena Day-Ahead: {day_ahead_price:.2f} €/MWh", annotation_position="bottom right", annotation_font_color="red")

    fig.update_layout(yaxis=dict(range=[-600, 1000]))
    fig.update_layout(
        hovermode="x unified",
        legend=dict( orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5)
    )
    return fig, combined_plot_df

def create_cumulative_procured_capacity_curve_plot(
    df_raw_capacity: pd.DataFrame, 
    selected_date: datetime.date, 
    selected_hour_utc: int, 
    country: str,
    display_local_hour: int = None,
    show_weighted_average: bool = False 
) -> tuple[go.Figure, pd.DataFrame]:
    """
    Generuje kumulovanou nabídkovou křivku pro rezervovanou kapacitu pro konkrétní hodinu.
    """
    hour_for_title_fallback = display_local_hour if display_local_hour is not None else selected_hour_utc

    if df_raw_capacity.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data pro rezervovanou kapacitu.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka kapacity pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title_fallback:02d}:00")
        return fig, pd.DataFrame() 

    hour_for_title = display_local_hour if display_local_hour is not None else selected_hour_utc

    start_time_utc_naive = datetime(selected_date.year, selected_date.month, selected_date.day, selected_hour_utc, 0, 0)
    hourly_capacity = df_raw_capacity[df_raw_capacity['Timestamp'] == start_time_utc_naive].copy() 

    if hourly_capacity.empty:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou dostupná data pro rezervovanou kapacitu pro vybranou hodinu.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka kapacity pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00")
        return fig, pd.DataFrame() 
    
    df_up_with_zero, weighted_avg_up = _prepare_capacity_for_plot(hourly_capacity[hourly_capacity["Direction"] == "Up"].copy(), "Up", "Capacity Price (EUR/MW)", "Capacity (MW)")
    df_down_with_zero, weighted_avg_down = _prepare_capacity_for_plot(hourly_capacity[hourly_capacity["Direction"] == "Down"].copy(), "Down", "Capacity Price (EUR/MW)", "Capacity (MW)")
    
    plots_to_combine = []
    
    df_up_plot = df_up_with_zero[['Cumulative Capacity (MW)', 'Capacity Price (EUR/MW)']].copy()
    df_up_plot['Curve Type'] = 'RV Up (aFRR+)'
    plots_to_combine.append(df_up_plot)

    df_down_plot = df_down_with_zero[['Cumulative Capacity (MW)', 'Capacity Price (EUR/MW)']].copy()
    df_down_plot['Curve Type'] = 'RV Down (aFRR-)'
    plots_to_combine.append(df_down_plot)

    if not plots_to_combine:
        fig = go.Figure()
        fig.add_annotation(text="Nejsou data kapacity pro zobrazení.",
                           xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                           font=dict(size=16, color="gray"))
        fig.update_layout(title=f"Kumulovaná nabídková křivka kapacity pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00")
        return fig, pd.DataFrame()

    combined_plot_df = pd.concat(plots_to_combine, ignore_index=True)
    
    fig = px.line(
        combined_plot_df,
        x="Cumulative Capacity (MW)",
        y="Capacity Price (EUR/MW)",
        color="Curve Type",
        title=f"Denní nabídková křivka RV pro {country} - {selected_date.strftime('%d.%m.%Y')} {hour_for_title:02d}:00",
        labels={ "Cumulative Capacity (MW)": "Kumulovaný výkon (MW)", "Capacity Price (EUR/MW)": "Cena (€/MW/h)", "Curve Type": ""},
        hover_data={"Capacity Price (EUR/MW)": ":.2f", "Cumulative Capacity (MW)": ":.2f"},
        line_shape='hv',
        color_discrete_map={ 'RV Up (aFRR+)': 'royalblue', 'RV Down (aFRR-)': 'darkgreen'}
    )
    
    # --- PŘIDÁNÍ LOGA JAKO VODOZNAKU ---
    logo_source = get_logo_as_base64("assets/logo.svg")
    if logo_source:
        fig.add_layout_image(
            dict(
                source=logo_source,
                xref="paper", yref="paper",
                x=0.5, y=0.5,
                sizex=0.5, sizey=0.5,
                xanchor="center", yanchor="middle",
                sizing="contain",
                opacity=opa,
                layer="below"
            )
        )
    # --- KONEC BLOKU S LOGEM ---
    
    if show_weighted_average and not combined_plot_df.empty: 
        if weighted_avg_up > weighted_avg_down: up_yanchor, down_yanchor = "bottom", "top"
        else: up_yanchor, down_yanchor = "top", "bottom"

        if weighted_avg_up != 0: 
            fig.add_hline(y=weighted_avg_up, line_dash="dash", line_color="firebrick", row="all", col="all")
            fig.add_annotation(xref="paper", x=1, y=weighted_avg_up, xanchor="right", yanchor=up_yanchor, text=f"RV+ průměr: {weighted_avg_up:.2f} €/MW", font=dict(color="firebrick"), bgcolor="rgba(255,255,255,0.8)", showarrow=False)
        if weighted_avg_down != 0: 
            fig.add_hline(y=weighted_avg_down, line_dash="dash", line_color="darkviolet", row="all", col="all")
            fig.add_annotation(xref="paper", x=1, y=weighted_avg_down, xanchor="right", yanchor=down_yanchor, text=f"RV- průměr: {weighted_avg_down:.2f} €/MW", font=dict(color="darkviolet"), bgcolor="rgba(255,255,255,0.8)", showarrow=False)

    y_min_default, y_max_default = -2, 50 
    valid_prices = combined_plot_df['Capacity Price (EUR/MW)'].dropna()
    if not valid_prices.empty:
        min_price, max_price = valid_prices.min(), valid_prices.max()
        y_min, y_max = min(-2, min_price - 2), max_price + 2 
        if y_max <= y_min: y_max = y_min + 10 
    else: 
        y_min, y_max = y_min_default, y_max_default
    
    fig.update_layout(yaxis=dict(range=[y_min, y_max]))
    
    fig.update_layout(
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5)
    )
    return fig, combined_plot_df