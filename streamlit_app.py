"""
Painel da carteira ArcGIS — versão Streamlit (Python nativo).

Reaproveita a mesma lógica de junção/agregação por IDCONTA usada no
relatório e na planilha originais (agora em etl.py), consultando o
Feature Service ao vivo via REST puro (sem precisar da ArcGIS Maps SDK
for JavaScript).

Prioridade ("tier") e "motivo principal" são calculados por regras
formulaicas (limiares sobre consumo, login, contatos etc.) — as mesmas
regras de fallback usadas na planilha e no painel HTML anterior. As 34
análises escritas à mão para as contas de foco imediato/da semana no
relatório (TCRE, LD Celulose, Serasa...) não estão aqui: eram uma leitura
manual de um momento específico e ficariam desatualizadas assim que os
dados mudassem.

Rodar localmente:
    pip install -r requirements.txt
    streamlit run streamlit_app.py
"""
import sys
import os
import traceback
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))
from etl import (
    ArcGISError, TIER_COLOR, TIER_LABEL, EVENT_COLOR, RISCO_ORDER, RISCO_COLOR, MODALIDADE_LABEL,
    ESTRATEGIA_CS_LABEL, AGOL_LABEL, build_portfolio, generate_token, load_portfolio_raw,
)

st.set_page_config(page_title="Painel da carteira ArcGIS", layout="wide")

DEFAULT_URL = "https://services2.arcgis.com/Az8bZXFPk4TfCJlZ/arcgis/rest/services/Acompanhamento_Contas_CS/FeatureServer"
EVENT_CAT_LABEL = {0: "Campanha", 1: "Recorrência", 2: "Apoio", 3: "Contato/tentativa"}
EVENT_STATUS_LABEL = {-1: "", 0: "sem resposta", 1: "reagendado", 2: "efetivado"}


# ============================================================================
# estado / carga
# ============================================================================
def _init_state():
    for k, v in dict(loaded=False, acc=None, series=None, events=None, ref_date=None, today=None,
                      analista="", err=None, err_trace=None, missing=None).items():
        st.session_state.setdefault(k, v)


def _load(base_url, analista, token):
    prog = st.empty()
    try:
        contas, contato, enduser, evento, consumo = load_portfolio_raw(
            base_url, analista, token, progress_cb=lambda m: prog.info(m)
        )
        if contas.empty:
            prog.empty()
            st.session_state.err = f'Nenhuma conta encontrada para "{analista}" no campo ANALISTA_CS. Confira a grafia usada no cadastro (nome completo, acentos etc.).'
            st.session_state.loaded = False
            return
        prog.info("Processando…")
        acc, series, events, ref_date, today, missing = build_portfolio(contas, contato, enduser, evento, consumo)
        prog.empty()
        st.session_state.update(loaded=True, acc=acc, series=series, events=events,
                                 ref_date=ref_date, today=today, analista=analista, err=None,
                                 err_trace=None, missing=missing)
    except ArcGISError as e:
        prog.empty()
        st.session_state.err = f"Erro do ArcGIS: {e}"
        st.session_state.err_trace = None
        st.session_state.loaded = False
    except Exception as e:  # noqa: BLE001
        prog.empty()
        hint = ""
        msg = str(e)
        if any(k in msg.lower() for k in ("token", "999", "498", "499", "403")):
            hint = " Parece exigir login — preencha a API key (ou usuário/senha) na barra lateral."
        st.session_state.err = f"Falha ao carregar: {msg}.{hint}"
        st.session_state.err_trace = traceback.format_exc()
        st.session_state.loaded = False


# ============================================================================
# barra lateral — conexão
# ============================================================================
def sidebar():
    st.sidebar.header("Conexão")
    base_url = st.sidebar.text_input("URL do Feature Service", value=DEFAULT_URL)
    analista = st.sidebar.text_input("Analista CS (filtro obrigatório)", value=st.session_state.get("analista", ""),
                                      placeholder="ex.: Ariane")
    st.sidebar.caption("Base compartilhada entre analistas — só carrega/mostra contas cujo campo ANALISTA_CS bate com o valor acima (sem diferenciar maiúsculas/acentos exatos, mas confira a grafia se vier vazio).")

    auth_mode = st.sidebar.radio("Autenticação", ["Sem login (serviço público)", "API key", "Usuário e senha"], index=1)
    token = None
    username = password = None
    if auth_mode == "API key":
        token = st.sidebar.text_input("API key", type="password",
                                       help="Gere em arcgis.com → Configurações da organização → API keys.")
    elif auth_mode == "Usuário e senha":
        username = st.sidebar.text_input("Usuário ArcGIS")
        password = st.sidebar.text_input("Senha", type="password")
        st.sidebar.caption("Gera um token via generateToken da organização. Depende da configuração de referer/token da sua org — se falhar, prefira API key.")

    run = st.sidebar.button("Carregar carteira", type="primary", use_container_width=True)

    if run:
        if not analista.strip():
            st.session_state.err = 'Preencha o campo "Analista CS" antes de carregar.'
            st.session_state.loaded = False
        else:
            tok = token
            if auth_mode == "Usuário e senha" and username and password:
                try:
                    tok = generate_token(username, password)
                except ArcGISError as e:
                    st.session_state.err = f"Não consegui gerar o token: {e}"
                    st.session_state.loaded = False
                    tok = "__FAIL__"
            if tok != "__FAIL__":
                _load(base_url, analista.strip(), tok)

    if st.session_state.get("err"):
        st.sidebar.error(st.session_state.err)
        if st.session_state.get("err_trace"):
            with st.sidebar.expander("Detalhe técnico (pra depuração)"):
                st.code(st.session_state.err_trace)
    if st.session_state.get("loaded"):
        st.sidebar.success(f"{len(st.session_state.acc)} contas carregadas de {st.session_state.analista}")
        if st.session_state.get("missing"):
            st.sidebar.warning("Campos não encontrados no serviço (tratados como vazios):\n" + "\n".join(
                f"- {layer}: {', '.join(cols)}" for layer, cols in st.session_state.missing.items()
            ))


# ============================================================================
# filtros
# ============================================================================
def filters_ui(acc: pd.DataFrame):
    st.markdown("#### Filtros")
    c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1.1])
    with c1:
        tiers_sel = st.multiselect(
            "Prioridade", options=list(TIER_LABEL.keys()),
            format_func=lambda t: TIER_LABEL[t],
            default=[t for t in TIER_LABEL if t != 7],
        )
    with c2:
        vert_sel = st.selectbox("Vertical", ["Todas"] + sorted(acc["VERTICAL"].dropna().unique().tolist()))
    with c3:
        parc_vals = sorted(acc["PARCEIRO"].fillna("(sem parceiro)").unique().tolist())
        parc_sel = st.selectbox("Parceiro", ["Todos"] + parc_vals)
    with c4:
        mod_vals = sorted(acc["MODALIDADE_ATENDIMENTO"].dropna().unique().tolist())
        mod_sel = st.multiselect(
            "Modalidade", options=mod_vals, default=mod_vals,
            format_func=lambda m: MODALIDADE_LABEL.get(int(m), str(m)),
        )

    c5, c6, c7, c8 = st.columns([1.1, 0.8, 1.2, 1.5])
    with c5:
        est_vals = sorted(acc["ESTRATEGIA_CS"].dropna().unique().tolist())
        est_sel = st.multiselect(
            "Estratégia CS", options=est_vals, default=est_vals,
            format_func=lambda e: ESTRATEGIA_CS_LABEL.get(int(e), str(e)),
        )
    with c6:
        agol_vals = sorted(acc["AGOL"].dropna().unique().tolist())
        agol_sel = st.multiselect(
            "AGOL", options=agol_vals, default=agol_vals,
            format_func=lambda a: AGOL_LABEL.get(int(a), str(a)),
        )
    with c7:
        risco_sel = st.multiselect("Classificação de risco", options=RISCO_ORDER, default=RISCO_ORDER)
    with c8:
        q = st.text_input("Buscar conta", "")

    f = acc[acc["t"].isin(tiers_sel)]
    if vert_sel != "Todas":
        f = f[f["VERTICAL"] == vert_sel]
    if parc_sel != "Todos":
        f = f[f["PARCEIRO"].fillna("(sem parceiro)") == parc_sel]
    f = f[f["MODALIDADE_ATENDIMENTO"].isin(mod_sel)]
    f = f[f["ESTRATEGIA_CS"].isin(est_sel)]
    f = f[f["AGOL"].isin(agol_sel)]
    # contas fora da régua (ex.: sem dado de consumo) não têm peso_risco — deixa
    # passar sempre, o filtro só restringe quem TEM classificação
    f = f[f["peso_risco"].isna() | f["peso_risco"].isin(risco_sel)]
    if q.strip():
        ql = q.strip().lower()
        f = f[f["NOME_CONTA"].str.lower().str.contains(ql, na=False) | f["VERTICAL"].str.lower().str.contains(ql, na=False)]
    return f


# ============================================================================
# KPIs
# ============================================================================
def kpis(F: pd.DataFrame, acc_total: int):
    sc = F[F["t"] != 7]
    f12 = int(F["t"].isin([1, 2]).sum())
    ren = int((sc["days_to_end"] <= 60).sum())
    sem = int((sc["eff_90"] == 0).sum())
    act = sc.loc[sc["hasUse"], "act"].sum()
    us = sc.loc[sc["hasUse"], "users"].sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Contas na seleção", len(F), f"{acc_total} na base")
    c2.metric("Foco imediato/semana", f12, "prioridades 1 e 2")
    c3.metric("Vencidos/vencendo em 60d", ren)
    c4.metric("Sem contato efetivo em 90d", f"{100*sem/len(sc):.0f}%" if len(sc) else "—", f"{sem} de {len(sc)}")
    c5.metric("Ativados / cadastrados", f"{100*act/us:.0f}%" if us else "—", f"{int(act)} de {int(us)}")


# ============================================================================
# gráficos
# ============================================================================
def chart_tiers(acc_scope: pd.DataFrame, key_prefix: str = "tiers"):
    counts = acc_scope["t"].value_counts().reindex(TIER_LABEL.keys(), fill_value=0)
    fig = go.Figure(go.Bar(
        x=counts.values, y=[TIER_LABEL[t] for t in counts.index], orientation="h",
        marker_color=[TIER_COLOR[t] for t in counts.index],
        text=counts.values, textposition="outside", hovertemplate="%{y}: %{x} contas<extra></extra>",
    ))
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis=dict(autorange="reversed"),
                       xaxis_title=None, showlegend=False, plot_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")


def chart_scatter(F: pd.DataFrame, key_prefix: str = "scatter"):
    pts = F[F["hasUse"] & (F["tot_cred"] > 0) & F["perc"].notna() & F["elapsed"].notna()]
    if pts.empty:
        st.info("Sem contas com pacote e consumo AGOL para a seleção.")
        return
    fig = go.Figure()
    fig.add_shape(type="line", x0=0, y0=0, x1=110, y1=110, line=dict(color="#cfd5de", dash="dash"))
    for t in sorted(pts["t"].unique()):
        g = pts[pts["t"] == t]
        fig.add_trace(go.Scatter(
            x=g["elapsed"].clip(upper=110), y=g["perc"].clip(upper=110), mode="markers",
            name=TIER_LABEL[t], marker=dict(color=TIER_COLOR[t], size=9, line=dict(color="white", width=1)),
            text=g["NOME_CONTA"],
            hovertemplate="<b>%{text}</b><br>Consumido: %{y:.0f}%<br>Prazo decorrido: %{x:.0f}%<extra></extra>",
        ))
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10),
                       xaxis_title="% do prazo do contrato decorrido", yaxis_title="% créditos consumidos",
                       xaxis_range=[0, 112], yaxis_range=[0, 112], plot_bgcolor="white",
                       legend=dict(orientation="h", y=-0.2))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")
    st.caption(f"{len(pts)} contas com pacote e consumo AGOL; {len(F)-len(pts)} ficam de fora (só Enterprise ou sem pacote). Eixos limitados a 110%.")


def chart_renew(F: pd.DataFrame, today: pd.Timestamp, key_prefix: str = "renew"):
    rows = F[(F["t"] != 7) & F["days_to_end"].notna() & (F["days_to_end"] >= -90) & (F["days_to_end"] <= 90)]
    rows = rows.sort_values("days_to_end")
    if rows.empty:
        st.info("Nenhum contrato nessa janela para a seleção.")
        return
    fig = go.Figure()
    for t in sorted(rows["t"].unique()):
        g = rows[rows["t"] == t]
        fig.add_trace(go.Scatter(
            x=g["days_to_end"], y=g["NOME_CONTA"], mode="markers", name=TIER_LABEL[t],
            marker=dict(color=TIER_COLOR[t], size=11, line=dict(color="white", width=1)),
            hovertemplate="<b>%{y}</b><br>%{x} dias<extra></extra>",
        ))
    fig.add_vline(x=0, line_dash="dash", line_color="#78828f")
    fig.update_layout(height=max(240, 26 * len(rows)), margin=dict(l=10, r=10, t=10, b=10),
                       xaxis_title="dias até o fim do contrato (negativo = já venceu)",
                       yaxis=dict(autorange="reversed"), plot_bgcolor="white", legend=dict(orientation="h", y=-0.15))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")


def chart_adoption(F: pd.DataFrame, series: dict, key_prefix: str = "adoption"):
    sc = F[(F["t"] != 7)]
    months: dict[int, dict] = {}
    for idc in sc["IDCONTA"]:
        s = series.get(idc)
        if not s:
            continue
        last_per_month = {}
        for row in s:
            d0, perc, act, users, login_days, tot = row
            ym = (d0.year - 2025) * 12 + (d0.month - 1)
            last_per_month[ym] = (act, users)
        for ym, (act, users) in last_per_month.items():
            o = months.setdefault(ym, {"act": 0, "us": 0})
            o["act"] += act or 0
            o["us"] += users or 0
    if not months:
        st.info("Sem série de consumo para a seleção.")
        return
    ks = sorted(months.keys())
    labels = [f"{2025 + k//12}-{(k%12)+1:02d}" for k in ks]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=labels, y=[months[k]["act"] for k in ks], mode="lines+markers",
                              name="Ativados", line=dict(color="#2a78d6", width=2)))
    fig.add_trace(go.Scatter(x=labels, y=[months[k]["us"] for k in ks], mode="lines+markers",
                              name="Cadastrados", line=dict(color="#eb6834", width=2)))
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="white",
                       legend=dict(orientation="h", y=-0.25))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")


def chart_events(F: pd.DataFrame, monthly_map: dict, key_prefix: str = "events"):
    ids = F.loc[F["t"] != 7, "IDCONTA"]
    combined = None
    for idc in ids:
        m = monthly_map.get(idc)
        if m is None or m.empty:
            continue
        combined = m if combined is None else combined.add(m, fill_value=0)
    if combined is None or combined.empty:
        st.info("Sem eventos para a seleção.")
        return
    combined = combined.sort_index().tail(9)
    labels = [f"{2025 + int(k)//12}-{(int(k)%12)+1:02d}" for k in combined.index]
    fig = go.Figure()
    cat_order = [0, 1, 2, 3]  # campanha, recorrência, apoio, outro
    names = {0: "Campanha", 1: "Recorrência", 2: "Apoio", 3: "Contato/tentativa"}
    colors = {0: EVENT_COLOR["Campanha"], 1: EVENT_COLOR["Recorrência"], 2: EVENT_COLOR["Apoio"], 3: EVENT_COLOR["Contato/tentativa"]}
    for c in cat_order:
        if c not in combined.columns:
            continue
        fig.add_trace(go.Bar(x=labels, y=combined[c].values, name=names[c], marker_color=colors[c]))
    fig.update_layout(barmode="stack", height=280, margin=dict(l=10, r=10, t=10, b=10), plot_bgcolor="white",
                       legend=dict(orientation="h", y=-0.25))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")
    st.caption("Campanhas em massa não são interação individual com o cliente.")


def quality_panel(F: pd.DataFrame):
    sc = F[F["t"] != 7]
    n = max(len(sc), 1)
    rows = [
        ("Engajamento e maturidade = 4 (parece \"não avaliado\")", ((sc["ENGAJAMENTO"] == 4) & (sc["MATURIDADE"] == 4)).sum() / n),
        ("Sem dado de uso (Enterprise sem AGOL, ou fora da base de consumo)", (~sc["hasUse"]).sum() / n),
        ("Com 0 ou 1 contato cadastrado", (sc["n_contatos"] <= 1).sum() / n),
    ]
    for label, pct in rows:
        st.caption(f"{label} — **{pct*100:.0f}%**")
        st.progress(min(1.0, pct))


def vertical_table(F: pd.DataFrame, key_prefix: str = "vertical"):
    sc = F[F["t"] != 7]
    if sc.empty:
        st.info("Sem contas para a seleção.")
        return
    g = sc.groupby("VERTICAL").agg(
        contas=("IDCONTA", "size"),
        login30=("login_days", lambda s: (s <= 30).sum()),
        com_dado=("login_days", lambda s: s.notna().sum()),
        perc_mediana=("perc", "median"),
        efetivos_media=("eff_90", "mean"),
        sem_contato=("eff_90", lambda s: (s == 0).sum()),
    ).reset_index().sort_values("contas", ascending=False)
    g["Login ≤30d"] = g.apply(lambda r: f"{int(r.login30)}/{int(r.com_dado)}" if r.com_dado else "—", axis=1)
    g["% créditos (mediana)"] = g["perc_mediana"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—")
    g["Efetivos 90d (média)"] = g["efetivos_media"].round(1)
    out = g[["VERTICAL", "contas", "Login ≤30d", "% créditos (mediana)", "Efetivos 90d (média)", "sem_contato"]]
    out.columns = ["Vertical", "Contas", "Login ≤30d", "% créditos consumidos (mediana)", "Contatos efetivos 90d (média)", "Sem contato 90d"]
    st.dataframe(out, use_container_width=True, hide_index=True, key=f"table_{key_prefix}")


# ============================================================================
# tabela principal + detalhe
# ============================================================================
def accounts_table(F: pd.DataFrame, key_prefix: str = "accounts"):
    show = F.copy()
    show["Prioridade"] = show["t"].map(TIER_LABEL)
    show["Créditos consumidos"] = show["perc"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—")
    show["Prazo decorrido"] = show["elapsed"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—")
    show["Fim do contrato (dias)"] = show["days_to_end"]
    show["Último login (dias)"] = show["login_days"]
    cols = ["NOME_CONTA", "Prioridade", "mot", "Fim do contrato (dias)", "Créditos consumidos", "Prazo decorrido",
            "Último login (dias)", "eff_90"]
    labels = ["Conta", "Prioridade", "Motivo principal", "Fim do contrato (d)", "Créditos consumidos",
              "Prazo decorrido", "Último login (d)", "Efetivos 90d"]
    tbl = show[cols].rename(columns=dict(zip(cols, labels))).sort_values("Prioridade")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=420, key=f"table_{key_prefix}")


def account_detail(acc: pd.DataFrame, series: dict, events: dict, key_prefix: str = "detail"):
    st.markdown("#### Detalhe da conta")
    names = acc.sort_values("NOME_CONTA")["NOME_CONTA"].tolist()
    if not names:
        return
    sel = st.selectbox("Escolha uma conta", names, key=f"select_{key_prefix}")
    a = acc[acc["NOME_CONTA"] == sel].iloc[0]

    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown(f"**{a['NOME_CONTA']}** · {a.get('VERTICAL','')} · {a.get('SUBSETOR','') or ''}")
        st.caption(f"Parceiro: {a.get('PARCEIRO') or '—'} · Exec. recorrência: {a.get('EXECUTIVO_RECORRENCIA') or '—'} · Exec. negócios: {a.get('EXECUTIVO_NEGOCIOS') or '—'}")
        st.markdown(f":{'red' if a['t'] in (1,2,6) else 'orange' if a['t']==3 else 'blue' if a['t']==4 else 'green'}[**{TIER_LABEL[a['t']]}**]")
        st.info(f"**Motivo principal:** {a['mot']}")
        st.success(f"**Próxima ação sugerida:** {a['ac']}")
    with c2:
        st.metric("Créditos do pacote", f"{a['tot_cred']:.0f}" if pd.notna(a.get('tot_cred')) else "—")
        st.metric("Consumido", f"{a['perc']:.0f}%" if pd.notna(a.get('perc')) else "—")
        st.metric("Prazo decorrido", f"{a['elapsed']:.0f}%" if pd.notna(a.get('elapsed')) else "—")
        st.metric("Último login (dias)", f"{a['login_days']:.0f}" if pd.notna(a.get('login_days')) else "—")

    s = series.get(a["IDCONTA"])
    if s:
        df = pd.DataFrame(s, columns=["data", "perc", "ativados", "cadastrados", "login_dias", "total"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["data"], y=df["perc"].clip(upper=120), mode="lines+markers",
                                  name="% consumido", line=dict(color="#2a78d6", width=2), fill="tozeroy",
                                  fillcolor="rgba(42,120,214,.12)"))
        fig.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10), title="% do pacote consumido",
                           plot_bgcolor="white", yaxis_range=[0, max(105, df['perc'].max()*1.05)])
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}_perc")

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df["data"], y=df["ativados"], mode="lines+markers", name="Ativados",
                                   line=dict(color="#2a78d6", width=2)))
        fig2.add_trace(go.Scatter(x=df["data"], y=df["cadastrados"], mode="lines+markers", name="Cadastrados",
                                   line=dict(color="#eb6834", width=2)))
        fig2.update_layout(height=200, margin=dict(l=10, r=10, t=30, b=10), title="Usuários ativados e cadastrados",
                            plot_bgcolor="white", legend=dict(orientation="h", y=-0.3))
        st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}_users")
    else:
        st.caption("Sem série de consumo AGOL para esta conta.")

    evl = events["evl"].get(a["IDCONTA"])
    st.markdown("**Últimos eventos (sem campanhas)**")
    if evl is not None and len(evl):
        rows = evl.sort_values("DATA", ascending=False)
        for _, r in rows.iterrows():
            cat = EVENT_CAT_LABEL.get(r["cat"], "")
            status = EVENT_STATUS_LABEL.get(r["STATUS"], "")
            st.caption(f"{r['DATA'].strftime('%d/%m/%y')} · {cat}{' · ' + status if status else ''} — {r['RESUMO'] or '—'}")
    else:
        st.caption(f"Nenhum evento registrado além de campanhas ({int(a.get('ev_campaign') or 0)} campanhas recebidas).")


# ============================================================================
# classificação de risco — pontuação ponderada (mesma régua do Arcade)
# ============================================================================
def risco_kpis(F: pd.DataFrame):
    r = F[F["peso_risco"].notna()]
    cols = st.columns(5)
    cols[0].metric("Total de contas", len(F))
    for col, label in zip(cols[1:], RISCO_ORDER):
        col.metric(label, int((r["peso_risco"] == label).sum()))


def chart_risco_dist(F: pd.DataFrame, key_prefix: str = "risco_dist"):
    r = F[F["peso_risco"].notna()]
    if r.empty:
        st.info("Nenhuma conta classificada na seleção.")
        return
    counts = r["peso_risco"].value_counts().reindex(RISCO_ORDER, fill_value=0)
    fig = go.Figure(go.Bar(
        x=counts.values, y=counts.index, orientation="h",
        marker_color=[RISCO_COLOR[k] for k in counts.index],
        text=counts.values, textposition="outside", hovertemplate="%{y}: %{x} contas<extra></extra>",
    ))
    fig.update_layout(height=220, margin=dict(l=10, r=10, t=10, b=10), yaxis=dict(autorange="reversed"),
                       xaxis_title=None, showlegend=False, plot_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"chart_{key_prefix}")


def risco_table(F: pd.DataFrame, key_prefix: str = "risco_table"):
    r = F[F["peso_risco"].notna()].copy()
    if r.empty:
        st.info("Nenhuma conta classificada na seleção.")
        return
    cols = ["NOME_CONTA", "nm_modalidade", "peso_risco", "peso_total"]
    labels = ["Conta", "Modalidade", "Classificação", "Pontuação"]
    tbl = r[cols].rename(columns=dict(zip(cols, labels))).sort_values("Pontuação")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=420, key=f"table_{key_prefix}")


def risco_metodologia():
    """Resumo fixo de como a pontuação é calculada — não muda conforme a conta
    selecionada, é a régua em si (pra explicar de uma vez, não caso a caso)."""
    st.markdown("A pontuação de cada conta soma 6 componentes — os limiares de cada um mudam "
                 "conforme a modalidade de atendimento (Tech/Low/Mid/High Touch, New Logo) — e desconta "
                 "uma penalidade por status da conta:")
    componentes = [
        "Contato com o cliente — interações individuais e em massa nos últimos 100 dias",
        "% de créditos consumidos — ritmo de consumo do pacote frente ao tempo de contrato",
        "% de usuários ativados — proporção dos usuários cadastrados que já ativaram",
        "Maturidade — nível de maturidade registrado no cadastro da conta",
        "Login recente — dias desde o último acesso",
        "Quantidade de apps em uso",
    ]
    for c in componentes:
        st.caption(f"+ {c}")
    st.caption("− Penalidade por status da conta (varia por status e modalidade)")

    st.markdown("**A soma final define a classificação:**")
    faixas = [("Crítico", "até 20 pontos"), ("Alto", "21 a 40 pontos"),
              ("Médio", "41 a 60 pontos"), ("Baixo", "acima de 60 pontos")]
    for label, faixa in faixas:
        cor = "red" if label in ("Crítico", "Alto") else "orange" if label == "Médio" else "green"
        st.markdown(f":{cor}[**{label}**] — {faixa}")


def risco_view(F: pd.DataFrame):
    st.caption("Réplica exata da regra que já roda no ArcGIS (Arcade) — mesmos pesos e limiares. "
               "Quanto maior a pontuação, mais saudável a conta.")

    excluidas = int(F["peso_risco"].isna().sum())
    if excluidas:
        st.caption(f"{excluidas} conta(s) fora da régua (dado insuficiente pra calcular a pontuação).")

    risco_kpis(F)
    st.markdown("---")

    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("##### Contas por classificação")
        chart_risco_dist(F)
    with c2:
        st.markdown("##### Como a pontuação é calculada")
        risco_metodologia()

    st.markdown("##### Todas as contas, ordenadas da menor para a maior pontuação")
    risco_table(F)


# ============================================================================
# visão executiva — uma tela, foco em risco (pra apresentação)
# ============================================================================
RISK_TIERS = [1, 2, 3, 6]  # foco imediato, foco da semana, monitoramento, saúde desconhecida


def risk_kpis(F: pd.DataFrame, acc_total: int):
    sc = F[F["t"] != 7]
    risk = sc[sc["t"].isin(RISK_TIERS)]
    ren60 = sc[sc["days_to_end"].notna() & (sc["days_to_end"] <= 60)]
    sem_contato = sc[sc["eff_90"] == 0]
    credit_risk = risk.loc[risk["hasUse"], "tot_cred"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Contas em risco", len(risk), f"de {len(sc)} na seleção")
    c2.metric("Renovação em até 60 dias", len(ren60))
    c3.metric("Sem contato efetivo em 90 dias", len(sem_contato))
    c4.metric("Créditos em risco (pacote)", f"{credit_risk:,.0f}".replace(",", "."))


def spotlight_accounts(F: pd.DataFrame, n: int = 3):
    """As contas mais urgentes, contadas como história: motivo + o que já está sendo feito."""
    risk = F[F["t"].isin(RISK_TIERS)].copy()
    if risk.empty:
        st.info("Nenhuma conta em risco na seleção atual.")
        return
    risk["_ord"] = risk["days_to_end"].fillna(9999)
    risk = risk.sort_values(["t", "_ord"]).head(n)

    cols = st.columns(len(risk))
    for col, (_, a) in zip(cols, risk.iterrows()):
        with col:
            cor = "red" if a["t"] in (1, 2, 6) else "orange"
            st.markdown(f":{cor}[**{a['NOME_CONTA']}**]  ·  {TIER_LABEL[a['t']]}")
            st.caption(a["mot"])
            st.success(f"Próximo passo: {a['ac']}")
            if pd.notna(a.get("days_to_end")):
                dte = int(a["days_to_end"])
                st.caption(f"Contrato vence em {dte} dias" if dte >= 0 else f"Contrato vencido há {-dte} dias")


def risk_accounts_table(F: pd.DataFrame, key_prefix: str = "risk"):
    """Todas as contas em risco, priorizadas — a lista de trabalho por trás dos KPIs."""
    risk = F[F["t"].isin(RISK_TIERS)].copy()
    if risk.empty:
        st.info("Nenhuma conta em risco na seleção atual.")
        return
    risk["Prioridade"] = risk["t"].map(TIER_LABEL)
    risk["Vence em (dias)"] = risk["days_to_end"]
    cols = ["NOME_CONTA", "Prioridade", "mot", "ac", "Vence em (dias)"]
    labels = ["Conta", "Prioridade", "Motivo", "Próximo passo", "Vence em (dias)"]
    tbl = risk[cols].rename(columns=dict(zip(cols, labels))).sort_values("Prioridade")
    st.dataframe(tbl, use_container_width=True, hide_index=True, height=min(420, 80 + 35 * len(tbl)),
                 key=f"table_{key_prefix}")


def executive_view(F: pd.DataFrame, acc: pd.DataFrame, series: dict, events: dict, today: pd.Timestamp):
    risk_kpis(F, len(acc))
    st.markdown("---")

    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("##### Contas por prioridade")
        chart_tiers(F, key_prefix="exec_tiers")
    with c2:
        st.markdown("##### Contratos vencendo (±90 dias)")
        chart_renew(F, today, key_prefix="exec_renew")

    st.markdown("---")
    st.markdown("##### Contas em risco agora — e o que estamos fazendo a respeito")
    spotlight_accounts(F)

    st.markdown("##### Todas as contas em risco, priorizadas")
    risk_accounts_table(F, key_prefix="exec_risk")

    with st.expander("Ver painel completo do analista (consumo, adoção, atividade do time)"):
        st.caption("Detalhe operacional do dia a dia — não recomendado pra apresentação.")
        analyst_view(F, acc, series, events, today, with_kpis=False, key_prefix="exec_nested")


# ============================================================================
# visão analista — o painel de trabalho completo, granular
# ============================================================================
def analyst_view(F: pd.DataFrame, acc: pd.DataFrame, series: dict, events: dict, today: pd.Timestamp,
                  with_kpis: bool = True, key_prefix: str = "analyst"):
    if with_kpis:
        kpis(F, len(acc))
        st.markdown("---")

    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("##### Onde estão as contas")
        chart_tiers(F, key_prefix=f"{key_prefix}_tiers")
    with c2:
        st.markdown("##### Consumo de créditos × prazo do contrato")
        chart_scatter(F, key_prefix=f"{key_prefix}_scatter")

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("##### Contratos vencendo (±90 dias)")
        chart_renew(F, today, key_prefix=f"{key_prefix}_renew")
    with c4:
        st.markdown("##### Adoção ao longo do tempo")
        chart_adoption(F, series, key_prefix=f"{key_prefix}_adoption")

    c5, c6 = st.columns(2)
    with c5:
        st.markdown("##### O que o CS está fazendo")
        chart_events(F, events["monthly"], key_prefix=f"{key_prefix}_events")
    with c6:
        st.markdown("##### Qualidade do cadastro")
        quality_panel(F)

    st.markdown("##### Por vertical")
    vertical_table(F, key_prefix=f"{key_prefix}_vertical")

    st.markdown("##### Contas")
    accounts_table(F, key_prefix=f"{key_prefix}_accounts")

    st.markdown("---")
    account_detail(F if len(F) else acc, series, events, key_prefix=f"{key_prefix}_detail")


# ============================================================================
# main
# ============================================================================
def main():
    _init_state()
    st.title("Painel da carteira ArcGIS")
    st.caption("Dados ao vivo do Feature Service, filtrados pelo analista CS. Prioridade e motivo são calculados por regras automáticas — não são as análises manuais do relatório.")

    view_mode = st.sidebar.radio(
        "Visão",
        ["Classificação de risco (fórmula)", "Executiva (apresentação)", "Analista (completa)"],
        index=0,
        help="Classificação de risco: a régua ponderada (mesma do Arcade), auditável conta a conta. "
             "Executiva: uma tela, focada em risco — pra apresentar. Analista: o painel completo do dia a dia.",
    )
    st.sidebar.markdown("---")
    sidebar()

    if not st.session_state.loaded:
        st.info("Preencha a conexão na barra lateral e clique em **Carregar carteira**.")
        return

    acc = st.session_state.acc
    series = st.session_state.series
    events = st.session_state.events
    ref_date = st.session_state.ref_date
    today = st.session_state.today

    sub = f"{len(acc)} contas cadastradas"
    if ref_date is not None:
        sub += f" · consumo AGOL até {ref_date.strftime('%d/%m/%Y')}"
    sub += f" · posição em {today.strftime('%d/%m/%Y')}"
    st.caption(sub)

    if st.session_state.get("missing"):
        missing = st.session_state.missing
        detail = " · ".join(f"{layer}: {', '.join(cols)}" for layer, cols in missing.items())
        st.warning(f"Alguns campos esperados não foram encontrados no serviço e foram tratados como vazios (pode afetar prioridade/motivo). {detail}")

    F = filters_ui(acc)
    st.markdown("---")

    if view_mode.startswith("Classificação"):
        risco_view(F)
    elif view_mode.startswith("Executiva"):
        executive_view(F, acc, series, events, today)
    else:
        analyst_view(F, acc, series, events, today)


if __name__ == "__main__":
    main()
