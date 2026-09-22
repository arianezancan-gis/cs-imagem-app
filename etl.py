"""
etl.py — conexão com o Feature Service (REST puro, sem SDK) e a mesma
junção/agregação por IDCONTA usada no relatório e no painel HTML anterior,
agora em pandas "nativo" para rodar dentro de um app Streamlit.

Não depende de streamlit: pode ser testado isoladamente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import requests

LAYER_IDS = {"contas": 0, "contato": 1, "enduser": 2, "evento": 3, "consumo": 6}

TIER_LABEL = {
    1: "Foco imediato", 2: "Foco da semana", 3: "Monitoramento", 4: "Oportunidade",
    5: "Estável", 6: "Saúde desconhecida", 7: "Fora do escopo (confirmar)",
}

# mesma paleta usada nos painéis HTML anteriores (já validada para a família de produtos)
TIER_COLOR = {1: "#d03b3b", 2: "#ec835a", 3: "#e0a100", 4: "#2a78d6", 5: "#2f9a3b", 6: "#8b93a1", 7: "#c3c9d2"}
EVENT_COLOR = {"Campanha": "#b8bfca", "Recorrência": "#2a78d6", "Apoio": "#1baf7a", "Contato/tentativa": "#eb6834"}

# Campos esperados em cada camada. Serviços diferentes (ex.: a base compartilhada
# entre analistas) podem ter nomes de campo ligeiramente diferentes — em vez de
# quebrar com KeyError, o app preenche o que faltar como vazio e avisa na tela
# quais campos não foram encontrados, pra você conferir o nome exato no serviço.
CONTAS_COLS = ["IDCONTA", "NOME_CONTA", "VERTICAL", "SUBSETOR", "PARCEIRO", "EXECUTIVO_RECORRENCIA",
               "EXECUTIVO_NEGOCIOS", "ENGAJAMENTO", "MATURIDADE", "STATUS", "ESTRATEGIA_CS",
               "ESTRATEGIA_ATENDIMENTO", "MODALIDADE_ATENDIMENTO", "ENTERPRISE", "AGOL", "ANALISTA_CS"]
CONTATO_COLS = ["IDCONTA"]
ENDUSER_COLS = ["IDCONTA", "ENDUSER", "DEPARTAMENTO"]
EVENTO_COLS = ["IDCONTA", "RESUMO", "FORMATO", "TIPO", "STATUS", "DATA"]
CONSUMO_COLS = ["IDCONTA", "ENDUSER", "DATA", "CREDITOS", "TOTAL_CREDITOS", "TOTAL_USER", "TOTAL_ATIVADO",
                "LAST_LOGIN", "DATA_INICIO", "DATA_FIM"]


def _missing_fields(df: pd.DataFrame, expected: list[str]) -> list[str]:
    return [c for c in expected if c not in df.columns]


def _ensure_columns(df: pd.DataFrame, expected: list[str]) -> pd.DataFrame:
    df = df.copy()
    for c in expected:
        if c not in df.columns:
            df[c] = np.nan
    return df


def missing_fields_report(contas, contato, enduser, evento, consumo) -> dict[str, list[str]]:
    """Campos esperados que não vieram do serviço, por camada (antes do padding)."""
    report = {
        "CONTAS_0": _missing_fields(contas, CONTAS_COLS),
        "CONTATO_1": _missing_fields(contato, CONTATO_COLS),
        "ENDUSER_2": _missing_fields(enduser, ENDUSER_COLS),
        "EVENTO_3": _missing_fields(evento, EVENTO_COLS),
        "CONSUMO_AGOL_6": _missing_fields(consumo, CONSUMO_COLS),
    }
    return {k: v for k, v in report.items() if v}


# ============================================================================
# 1) Conexão — REST puro (requests), sem a ArcGIS Maps SDK for JavaScript
# ============================================================================
class ArcGISError(RuntimeError):
    pass


def generate_token(username: str, password: str, portal: str = "https://www.arcgis.com", expiration_min: int = 60) -> str:
    """Gera um token a partir de usuário/senha (alternativa à API key).
    Depende da organização aceitar geração de token por referer — se falhar,
    a API key costuma ser o caminho mais confiável para consultas de leitura."""
    r = requests.post(
        f"{portal.rstrip('/')}/sharing/rest/generateToken",
        data={
            "username": username, "password": password, "f": "json",
            "referer": "https://streamlit.app", "expiration": expiration_min,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "token" not in data:
        msg = (data.get("error") or {}).get("message", "Falha ao gerar token.")
        raise ArcGISError(msg)
    return data["token"]


def _query(base_url: str, layer_id: int, where: str, token: str | None, out_fields: str = "*") -> pd.DataFrame:
    url = f"{base_url.rstrip('/')}/{layer_id}/query"
    rows: list[dict] = []
    offset, page = 0, 1000
    while True:
        params = {
            "where": where, "outFields": out_fields, "f": "json",
            "returnGeometry": "false", "resultRecordCount": page, "resultOffset": offset,
        }
        if token:
            params["token"] = token
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            details = data["error"].get("details")
            if details:
                msg += " — " + "; ".join(details)
            raise ArcGISError(msg)
        feats = data.get("features", [])
        rows.extend(f["attributes"] for f in feats)
        # alguns serviços limitam o retorno ao próprio maxRecordCount (ex.: 100 ou 200),
        # mesmo pedindo mais — nesse caso a página vem menor que o pedido só por isso,
        # não porque acabaram os registros. exceededTransferLimit avisa quando é o caso;
        # avançar pelo nº real recebido (em vez do tamanho pedido) evita pular registros.
        if not feats:
            break
        exceeded = bool(data.get("exceededTransferLimit"))
        if not exceeded and len(feats) < page:
            break
        offset += len(feats)
        if offset > 60000:  # trava de segurança
            break
    return pd.DataFrame(rows)


def query_layer(base_url: str, layer_id: int, token: str | None = None, where: str = "1=1", out_fields: str = "*") -> pd.DataFrame:
    return _query(base_url, layer_id, where, token, out_fields)


def query_by_ids(base_url: str, layer_id: int, id_field: str, ids, token: str | None = None, chunk_size: int = 200, out_fields: str = "*") -> pd.DataFrame:
    ids = [i for i in ids if i is not None]
    if not ids:
        return pd.DataFrame()
    is_num = isinstance(ids[0], (int, float, np.integer, np.floating)) and not isinstance(ids[0], bool)
    frames = []
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        if is_num:
            vals = ",".join(str(int(v)) for v in chunk)
        else:
            vals = ",".join("'" + str(v).replace("'", "''") + "'" for v in chunk)
        where = f"{id_field} IN ({vals})"
        frames.append(_query(base_url, layer_id, where, token, out_fields))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_portfolio_raw(base_url: str, analista: str, token: str | None = None, progress_cb=None):
    """Carrega as 5 camadas já filtradas por ANALISTA_CS (via CONTAS_0) e
    por IDCONTA (nas demais). progress_cb(str) é chamado a cada etapa, se dado."""
    def note(msg):
        if progress_cb:
            progress_cb(msg)

    note("Consultando CONTAS_0…")
    analista_esc = analista.replace("'", "''")
    contas = query_layer(base_url, LAYER_IDS["contas"], token, where=f"UPPER(ANALISTA_CS) = UPPER('{analista_esc}')")
    if contas.empty:
        return contas, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    if "IDCONTA" not in contas.columns:
        cols_found = ", ".join(contas.columns) if len(contas.columns) else "(nenhum campo)"
        raise ArcGISError(
            f"CONTAS_0 devolveu {len(contas)} registro(s) para \"{analista}\", mas nenhum tem o campo "
            f"'IDCONTA' — não dá pra continuar sem ele (é a chave usada pra juntar as outras 4 camadas). "
            f"Campos encontrados nesse retorno: {cols_found}. Confira o nome exato do campo de ID no serviço."
        )

    ids = [i for i in contas["IDCONTA"].tolist() if i is not None]
    if not ids:
        return contas.iloc[0:0], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    note(f"{len(contas)} contas encontradas — carregando CONTATO_1…")
    contato = query_by_ids(base_url, LAYER_IDS["contato"], "IDCONTA", ids, token)
    note("Carregando ENDUSER_2…")
    enduser = query_by_ids(base_url, LAYER_IDS["enduser"], "IDCONTA", ids, token)
    note("Carregando EVENTO_3…")
    evento = query_by_ids(base_url, LAYER_IDS["evento"], "IDCONTA", ids, token)
    note("Carregando CONSUMO_AGOL_6…")
    consumo = query_by_ids(base_url, LAYER_IDS["consumo"], "IDCONTA", ids, token)
    return contas, contato, enduser, evento, consumo


def esri_dt(series) -> pd.Series:
    """Datas do REST do ArcGIS vêm como epoch ms (UTC) quando f=json; mas
    alguns serviços devolvem string ISO — trata os dois casos."""
    s = pd.Series(series)
    if s.empty:
        return pd.to_datetime(s)
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_datetime(s, unit="ms", utc=True).dt.tz_localize(None)
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_localize(None)


# ============================================================================
# 2) ETL — a mesma junção/agregação por IDCONTA do relatório original
#    (feat.py), portada para rodar sobre dados carregados ao vivo.
# ============================================================================
CAMP_KW = ["convite", "email sobre", "email com grava", "material sobre novidades", "envio do convite",
           "aviso labor", "descontinua", "evento esri", "geobim", "envio de material sobre ia",
           "como levar gis", "laborat"]
INTERNAL_RE = r"^(?:recebid|repasse|removido|atribui|contexto|alinhamento|informa)"
APOIO_KW = ["apoio", "duvida", "dúvida", "chamado", "erro"]


def _classify_events(evento: pd.DataFrame) -> pd.DataFrame:
    ev = evento.copy()
    if ev.empty:
        return ev
    ev["DATA"] = esri_dt(ev.get("DATA"))
    ev = ev.dropna(subset=["DATA"]).copy()
    ev["res"] = ev.get("RESUMO", "").fillna("").astype(str).str.lower().str.strip()
    ev["campaign"] = (ev.get("FORMATO") == 7) | ev["res"].apply(lambda s: any(k in s for k in CAMP_KW))
    ev["internal"] = (
        (ev.get("FORMATO") == 5) | ev.get("TIPO", pd.Series(dtype=object)).isin([2, 3, 4])
        | ev["res"].str.contains(INTERNAL_RE, regex=True) | ev["res"].str.contains("contexto da conta")
    ) & ~ev["campaign"]
    ev["touch"] = (~ev["campaign"]) & (~ev["internal"])
    ev["rec"] = ev["res"].str.startswith("recorr")
    ev["attempt"] = ev["res"].str.contains("tentativa")

    def _cat(row):
        if row["campaign"]:
            return 0
        if row["internal"]:
            return 4
        if row["rec"]:
            return 1
        if row.get("FORMATO") == 3 or any(k in row["res"] for k in APOIO_KW):
            return 2
        return 3

    ev["cat"] = ev.apply(_cat, axis=1)
    ev["ym"] = (ev["DATA"].dt.year - 2025) * 12 + (ev["DATA"].dt.month - 1)
    return ev


def _event_stats(ev: pd.DataFrame, today: pd.Timestamp) -> dict:
    """id -> {touch_eff, eff_90, touch_noresp, rec_n, ev_campaign, days_since_eff, monthly(df), evl(list)}"""
    out: dict[int, dict] = {}
    if ev.empty:
        return out
    for idc, g in ev.groupby("IDCONTA"):
        g = g.sort_values("DATA")
        eff = g[(g["touch"]) & (g["STATUS"] == 2) & (~g["attempt"])]
        noresp = g[(g["touch"]) & (g["STATUS"] == 0)]
        last_eff = eff["DATA"].max() if len(eff) else pd.NaT
        gm = g[g["cat"] != 4]
        monthly = gm.groupby(["ym", "cat"]).size().unstack(fill_value=0)
        for c in range(4):
            if c not in monthly.columns:
                monthly[c] = 0
        monthly = monthly[[0, 1, 2, 3]].sort_index()
        evl = gm.tail(14)[["DATA", "cat", "STATUS", "RESUMO"]].copy()
        out[idc] = dict(
            touch_eff=len(eff),
            eff_90=int((eff["DATA"] >= today - pd.Timedelta(days=90)).sum()),
            touch_noresp=len(noresp),
            rec_n=int(g["rec"].sum()),
            ev_campaign=int(g["campaign"].sum()),
            days_since_eff=None if pd.isna(last_eff) else int((today - last_eff).days),
            monthly=monthly,
            evl=evl,
        )
    return out


F_COLS = ["IDCONTA", "co_stale_days", "tot_cred", "perc", "n_org", "ini", "fim", "elapsed",
          "days_to_end", "users", "act", "login", "login_days", "first_snap", "n_snap",
          "vel60", "vel_prev", "act_d90", "act_d180"]


def _consumo_stats(consumo: pd.DataFrame, today: pd.Timestamp):
    """Réplica de feat.py: dedup por (conta,org,dia); agrega orgs por dia;
    calcula consumo entre snapshots do mesmo contrato; retorna (F, series_map, ref_date).
    F sempre tem todas as colunas de F_COLS (mesmo vazia), pra merge() e o resto do
    pipeline nunca quebrarem por coluna ausente quando não há dado de consumo."""
    co = consumo.copy()
    empty_F = _ensure_columns(pd.DataFrame(columns=["IDCONTA"]), F_COLS)
    if co.empty:
        return empty_F, {}, None

    for col in ("DATA", "LAST_LOGIN", "DATA_INICIO", "DATA_FIM"):
        if col in co.columns:
            co[col] = esri_dt(co[col])
    co = co.dropna(subset=["DATA"]).copy()
    if co.empty:
        return empty_F, {}, None

    co["D"] = co["DATA"].dt.normalize()
    co = co.sort_values(["IDCONTA", "ENDUSER", "D"]).drop_duplicates(["IDCONTA", "ENDUSER", "D"], keep="last")

    def agg(g):
        tc = g["TOTAL_CREDITOS"].sum()
        cr = g["CREDITOS"].sum()
        return pd.Series({
            "tot": tc, "cred": cr, "perc": (100 * (1 - cr / tc) if tc > 0 else 0),
            "users": g["TOTAL_USER"].sum(), "act": g["TOTAL_ATIVADO"].sum(),
            "login": g["LAST_LOGIN"].max(), "ini": g["DATA_INICIO"].max(), "fim": g["DATA_FIM"].min(),
            "n_org": g["ENDUSER"].nunique(),
        })

    ag = co.groupby(["IDCONTA", "D"]).apply(agg).reset_index()

    co["dcred"] = co.groupby(["IDCONTA", "ENDUSER"])["CREDITOS"].diff()
    co["dini"] = co.groupby(["IDCONTA", "ENDUSER"])["DATA_INICIO"].diff().dt.days.fillna(0)
    co["cons"] = np.where((co["dcred"] < 0) & (co["dini"] == 0), -co["dcred"], 0.0)
    cons_day = co.groupby(["IDCONTA", "D"])["cons"].sum().rename("cons").reset_index()
    ag = ag.merge(cons_day, on=["IDCONTA", "D"], how="left")
    ag["cons"] = ag["cons"].fillna(0.0)

    ref_date = ag["D"].max()

    rows, series_map = [], {}
    for idc, g in ag.groupby("IDCONTA"):
        g = g.sort_values("D")
        last = g.iloc[-1]
        d = {"IDCONTA": idc}
        d["co_stale_days"] = (ref_date - last["D"]).days
        d["tot_cred"] = last["tot"]; d["perc"] = last["perc"]; d["n_org"] = last["n_org"]
        d["ini"] = last["ini"]; d["fim"] = last["fim"]
        span = (last["fim"] - last["ini"]).days if pd.notna(last["fim"]) and pd.notna(last["ini"]) else None
        d["elapsed"] = (last["D"] - last["ini"]).days / span * 100 if span and span > 0 else np.nan
        d["days_to_end"] = (last["fim"] - today).days if pd.notna(last["fim"]) else np.nan
        d["users"] = last["users"]; d["act"] = last["act"]
        d["login"] = last["login"]
        d["login_days"] = (last["D"] - last["login"]).days if pd.notna(last["login"]) else np.nan
        d["first_snap"] = g["D"].min(); d["n_snap"] = len(g)

        for lab, (lo, hi) in {"vel60": (0, 60), "vel_prev": (60, 150)}.items():
            w = g[(g["D"] > last["D"] - pd.Timedelta(days=hi)) & (g["D"] <= last["D"] - pd.Timedelta(days=lo))]
            span_d = (w["D"].max() - w["D"].min()).days if len(w) > 1 else 0
            d[lab] = (w["cons"].iloc[1:].sum() / max(w["tot"].mean(), 1) * 100 / span_d * 30) if span_d >= 20 and w["tot"].mean() > 0 else np.nan

        for k in (90, 180):
            b_ = g[g["D"] <= last["D"] - pd.Timedelta(days=k)]
            if len(b_):
                d[f"act_d{k}"] = last["act"] - b_.iloc[-1]["act"]
            else:
                d[f"act_d{k}"] = np.nan

        rows.append(d)
        series_map[idc] = g[["D", "perc", "act", "users", "login", "tot"]].assign(
            login_days=lambda x: (x["D"] - x["login"]).dt.days
        )[["D", "perc", "act", "users", "login_days", "tot"]].values.tolist()

    return pd.DataFrame(rows), series_map, ref_date


def tier_of(r: pd.Series) -> int:
    if r.get("ESTRATEGIA_ATENDIMENTO") == 2:
        return 7
    if pd.isna(r.get("tot_cred")):
        return 3 if (r.get("eff_90") or 0) > 0 else 6
    if pd.notna(r.get("co_stale_days")) and r["co_stale_days"] > 14:
        return 6
    perc, dte, el, lg = r.get("perc"), r.get("days_to_end"), r.get("elapsed"), r.get("login_days")
    if (((pd.notna(perc) and perc >= 90 and pd.notna(dte) and dte > 45)
         or (pd.notna(perc) and perc >= 80 and pd.notna(el) and el < 50))
            and pd.notna(lg) and lg <= 30):
        return 4
    bad = (pd.notna(lg) and lg > 30) \
        or (pd.notna(perc) and perc <= 2 and pd.notna(el) and el >= 50 and (r.get("tot_cred") or 0) >= 1000) \
        or (pd.notna(dte) and dte <= 60)
    if bad:
        return 3
    if (r.get("eff_90") or 0) == 0 and r.get("ESTRATEGIA_CS") == 1 and (r.get("n_snap") or 0) < 8:
        return 6
    return 5


def motivo_of(r: pd.Series):
    t = r["t"]
    if t == 7:
        return ("Perfil compatível com conta fora do escopo de atendimento (estratégia de atendimento = 2)",
                "Confirmar com gestor se a conta sai da carteira")
    if pd.isna(r.get("tot_cred")):
        if t == 6:
            return ("Sem dados de consumo AGOL (só Enterprise) e sem contato efetivo em 90 dias: não há evidência para afirmar saúde",
                    "Tentar contato com o ponto de contato; obter dados de uso do Enterprise")
        return (f"Adoção invisível (sem AGOL); relacionamento com {int(r.get('eff_90') or 0)} contato(s) efetivo(s) em 90 dias",
                "Levantar uso do Enterprise com o cliente na próxima recorrência")
    if t == 6:
        stale = int(r["co_stale_days"]) if pd.notna(r.get("co_stale_days")) else "?"
        return (f"Contrato vencido/sem snapshot há {stale} dias; sem contato efetivo em 90d",
                "Confirmar com executivo se houve renovação ou churn")
    if t == 4:
        perc = r.get("perc"); el = r.get("elapsed")
        return (f"Créditos {round(perc) if pd.notna(perc) else '?'}% consumidos com {round(el) if pd.notna(el) else '?'}% do prazo — consumo acima do ritmo do contrato",
                "Avaliar ampliação de pacote antes de esgotar")
    if t == 3:
        parts = []
        lg, perc, el, dte = r.get("login_days"), r.get("perc"), r.get("elapsed"), r.get("days_to_end")
        if pd.notna(lg) and lg > 30:
            parts.append(f"último login há {int(lg)} dias")
        if pd.notna(perc) and perc <= 2 and pd.notna(el) and el >= 50:
            parts.append(f"créditos praticamente sem uso ({perc:.1f}%) com {round(el)}% do prazo")
        if pd.notna(dte) and dte <= 60:
            parts.append(f"renovação em {int(dte)} dias")
        s = "; ".join(parts) if parts else "Sinais de baixa atividade"
        s = s[0].upper() + s[1:]
        return (s, "Validar uso real com o cliente (créditos baixos podem refletir uso de Enterprise/apps)")
    if (r.get("eff_90") or 0) == 0:
        return ("Uso recente e consumo estável, contato do CS escasso (perfil autônomo)",
                "Manter baixa intensidade; contato preventivo trimestral")
    return ("Uso recente, consumo estável e contato efetivo recente", "Manter cadência atual")


def build_portfolio(contas: pd.DataFrame, contato: pd.DataFrame, enduser: pd.DataFrame,
                     evento: pd.DataFrame, consumo: pd.DataFrame):
    """Retorna (acc_df, series_map, events_map, ref_date, today, missing_report) prontos pro painel.
    missing_report lista, por camada, os campos esperados que não vieram do serviço
    (o app não quebra por isso — só preenche como vazio — mas vale conferir os nomes)."""
    today = pd.Timestamp.now().normalize()
    missing = missing_fields_report(contas, contato, enduser, evento, consumo)
    contas = _ensure_columns(contas, CONTAS_COLS)
    contato = _ensure_columns(contato, CONTATO_COLS)
    enduser = _ensure_columns(enduser, ENDUSER_COLS)
    evento = _ensure_columns(evento, EVENTO_COLS)
    consumo = _ensure_columns(consumo, CONSUMO_COLS)

    if contas.empty:
        return contas, {}, {}, None, today, missing

    n_contatos = contato.groupby("IDCONTA").size().rename("n_contatos") if len(contato) else pd.Series(dtype="int64", name="n_contatos")

    ev = _classify_events(evento)
    ev_stats = _event_stats(ev, today)

    F, series_map, ref_date = _consumo_stats(consumo, today)

    a = contas.merge(F, on="IDCONTA", how="left").merge(n_contatos, on="IDCONTA", how="left")
    a["n_contatos"] = a["n_contatos"].fillna(0).astype(int)
    a["hasUse"] = a["tot_cred"].notna()

    for col, default in [("touch_eff", 0), ("eff_90", 0), ("touch_noresp", 0), ("rec_n", 0),
                          ("ev_campaign", 0), ("days_since_eff", None)]:
        a[col] = a["IDCONTA"].map(lambda i: ev_stats.get(i, {}).get(col, default))

    a["t"] = a.apply(tier_of, axis=1)
    mot_ac = a.apply(motivo_of, axis=1)
    a["mot"] = mot_ac.apply(lambda x: x[0])
    a["ac"] = mot_ac.apply(lambda x: x[1])
    a["tier_label"] = a["t"].map(TIER_LABEL)
    a["tier_color"] = a["t"].map(TIER_COLOR)

    events_map = {idc: v["evl"] for idc, v in ev_stats.items()}
    monthly_map = {idc: v["monthly"] for idc, v in ev_stats.items()}

    return a, series_map, {"evl": events_map, "monthly": monthly_map}, ref_date, today, missing
