import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Page Config (Mobile-First, Clean Layout)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Committee Start Date Finder (SEC Official)",
    page_icon="🏛️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.25rem; padding-bottom: 2.5rem; max-width: 780px; }
      .result-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 1.1rem 1.25rem;
        margin-top: 0.75rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
      }
      .badge-exact {
        display: inline-block; background: #dcfce7; color: #166534;
        font-weight: 700; font-size: 0.78rem; padding: 3px 10px; border-radius: 999px;
      }
      .badge-first {
        display: inline-block; background: #fef3c7; color: #92400e;
        font-weight: 700; font-size: 0.78rem; padding: 3px 10px; border-radius: 999px;
      }
      .big-answer {
        font-size: 1.65rem; font-weight: 800; color: #0f172a; margin: 0.35rem 0 0.2rem 0;
      }
      .highlight-box {
        background: #f8fafc; border-left: 4px solid #10b981;
        padding: 0.75rem 0.9rem; border-radius: 8px; margin-top: 0.75rem;
        font-size: 0.92rem; line-height: 1.55; color: #1e293b;
      }
      mark.exact-hit {
        background: #fef08a; color: #0f172a; padding: 2px 5px;
        border-radius: 4px; font-weight: 600;
      }
      mark.first-hit {
        background: #d1fae5; color: #064e3b; padding: 2px 5px;
        border-radius: 4px; font-weight: 600;
      }
      .filing-row {
        padding: 0.65rem 0; border-bottom: 1px solid #f1f5f9; font-size: 0.88rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# SEC Official HTTP Helpers (Strict Official sec.gov Only)
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = "CommitteeTenureFinder/1.0 (research@example.com)"


def get_headers(user_agent: str) -> Dict[str, str]:
    ua = (user_agent or DEFAULT_USER_AGENT).strip()
    return {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json, text/html, */*",
    }


def clean_company_input(raw: str) -> str:
    s = re.sub(r"\b(us|uk|ln|cn|hk|jp|ca|au)\s+equity\b", "", raw, flags=re.I)
    s = re.sub(r"\bequity\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def pad_cik(cik: str) -> str:
    digits = re.sub(r"\D", "", str(cik or ""))
    return digits.zfill(10) if digits else ""


def build_sec_doc_url(cik: str, accession: str, filename: str = "") -> str:
    cik_num = str(cik).lstrip("0")
    acc_nodash = str(accession).replace("-", "")
    if not cik_num or not acc_nodash:
        return "https://www.sec.gov/edgar/searchedgar/companysearch"
    if filename:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_nodash}/{filename}"
    return f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_nodash}/{accession}-index.htm"


@st.cache_data(ttl=86400, show_spinner=False)
def load_sec_company_tickers(user_agent: str) -> List[Dict]:
    url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(url, headers=get_headers(user_agent), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return list(data.values())


def resolve_company_cik(company_query: str, user_agent: str) -> Tuple[Optional[str], str]:
    cleaned = clean_company_input(company_query)
    if re.fullmatch(r"\d{4,10}", cleaned):
        return pad_cik(cleaned), cleaned

    # 1. Exact ticker or title lookup via official SEC company_tickers.json
    try:
        companies = load_sec_company_tickers(user_agent)
        q_low = cleaned.lower()
        for c in companies:
            if str(c.get("ticker", "")).lower() == q_low:
                return pad_cik(c["cik_str"]), c.get("title", cleaned)
        for c in companies:
            if str(c.get("title", "")).lower() == q_low:
                return pad_cik(c["cik_str"]), c.get("title", cleaned)
        for c in companies:
            if q_low in str(c.get("title", "")).lower():
                return pad_cik(c["cik_str"]), c.get("title", cleaned)
    except Exception:
        pass

    # 2. Fallback to official SEC EFTS entity search
    try:
        url = "https://efts.sec.gov/LATEST/search-index"
        resp = requests.get(
            url,
            params={"keysTyped": cleaned},
            headers=get_headers(user_agent),
            timeout=12,
        )
        if resp.ok:
            hits = resp.json().get("hits", {}).get("hits", [])
            if hits:
                src = hits[0].get("_source", {})
                raw_id = hits[0].get("_id") or (src.get("ciks") or [""])[0]
                return pad_cik(raw_id), src.get("entity", cleaned)
    except Exception:
        pass

    return None, cleaned


def search_sec_filings(
    cik: str,
    person: str,
    committee: str,
    user_agent: str,
    all_forms: bool = True,
) -> List[Dict]:
    """Query SEC EFTS for all official filings mentioning person + committee."""
    url = "https://efts.sec.gov/LATEST/search-index"
    headers = get_headers(user_agent)

    # Try exact person + committee first; if empty and person has middle initial, try first + last
    name_variants = [person.strip()]
    parts = [p for p in person.strip().split() if p]
    if len(parts) >= 3:
        short_name = f"{parts[0]} {parts[-1]}"
        if short_name not in name_variants:
            name_variants.append(short_name)

    hits_raw: List[Dict] = []
    for variant in name_variants:
        params = {
            "q": f'"{variant}" "{committee.strip()}"',
            "ciks": cik,
            "dateRange": "all",
            "from": 0,
            "size": 100,
        }
        if not all_forms:
            params["forms"] = "DEF 14A,PRE 14A,DEFA14A,8-K,8-K/A,10-K,10-K/A,10-Q"

        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.ok:
            batch = resp.json().get("hits", {}).get("hits", [])
            if batch:
                hits_raw = batch
                break

    # Deduplicate by accession number and sort chronologically ascending
    seen = set()
    filings: List[Dict] = []
    for h in hits_raw:
        src = h.get("_source", {})
        file_date = src.get("file_date", "")
        if not file_date:
            continue
        adsh = src.get("adsh") or (h.get("_id", "").split(":")[0])
        doc = h.get("_id", "").split(":")[1] if ":" in h.get("_id", "") else ""
        if not adsh or adsh in seen:
            continue
        seen.add(adsh)
        form = src.get("form") or src.get("root_form") or "FILING"
        filings.append(
            {
                "date": file_date,
                "year": file_date[:4],
                "form": form,
                "adsh": adsh,
                "doc": doc,
                "url": build_sec_doc_url(cik, adsh, doc),
            }
        )

    filings.sort(key=lambda x: x["date"])
    return filings


def get_prior_proxy_filing(cik: str, before_date: str, user_agent: str) -> Optional[Dict]:
    """Fetch official data.sec.gov submissions JSON to find the last DEF 14A before first appearance."""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = requests.get(url, headers=get_headers(user_agent), timeout=15)
        if not resp.ok:
            return None
        data = resp.json()

        def extract_candidates(block: Dict) -> List[Dict]:
            out = []
            forms = block.get("form", [])
            dates = block.get("filingDate", [])
            accs = block.get("accessionNumber", [])
            docs = block.get("primaryDocument", [])
            for f, d, a, p in zip(forms, dates, accs, docs):
                if f == "DEF 14A" and d < before_date:
                    out.append(
                        {
                            "date": d,
                            "year": d[:4],
                            "form": f,
                            "url": build_sec_doc_url(cik, a, p),
                        }
                    )
            return out

        candidates = extract_candidates(data.get("filings", {}).get("recent", {}))
        # Check older historical archive files if needed
        if not candidates:
            for hist in data.get("filings", {}).get("files", [])[:2]:
                h_name = hist.get("name")
                if not h_name:
                    continue
                hr = requests.get(
                    f"https://data.sec.gov/submissions/{h_name}",
                    headers=get_headers(user_agent),
                    timeout=12,
                )
                if hr.ok:
                    candidates.extend(extract_candidates(hr.json()))

        if candidates:
            candidates.sort(key=lambda x: x["date"], reverse=True)
            return candidates[0]
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Full-Text Filing Reader & Exact-Date Sentence Highlighter
# ---------------------------------------------------------------------------
DATE_REGEX = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
    r"\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
    r"|\b(?:since|effective|in)\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b",
    re.I,
)

ACTION_REGEX = re.compile(
    r"\b(joined|appointed|elected|named|effective|since|member\s+since|designated|added|serving\s+since|served\s+since|became\s+a\s+member)\b",
    re.I,
)


def inspect_single_filing(filing: Dict, person: str, committee: str, user_agent: str) -> Dict:
    """Download official SEC filing and scan every sentence for exact date or first-appearance match."""
    result = {
        "filing": filing,
        "exact_date": None,
        "exact_sentence": None,
        "first_sentence": None,
        "error": None,
    }
    try:
        resp = requests.get(filing["url"], headers=get_headers(user_agent), timeout=18)
        if not resp.ok:
            result["error"] = f"HTTP {resp.status_code}"
            return result

        # Limit extremely huge exhibits to first 2.5MB for speed
        raw_html = resp.text[:2_500_000]
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()

        sentences = re.split(r"(?<=[.!?])\s+", text)
        tokens = [t.lower() for t in person.strip().split() if len(t) > 1]
        last_name = tokens[-1] if tokens else ""
        full_low = person.strip().lower()
        comm_low = committee.strip().lower()
        comm_core = re.sub(r"\bcommittee\b", "", comm_low).strip()

        def has_person(s_low: str) -> bool:
            return (full_low in s_low) or (bool(last_name) and re.search(rf"\b{re.escape(last_name)}\b", s_low) is not None)

        def has_committee(s_low: str) -> bool:
            return (comm_low in s_low) or (bool(comm_core) and comm_core in s_low and "committee" in s_low)

        # Pass 1: single sentence
        for s in sentences:
            s_low = s.lower()
            if has_person(s_low) and has_committee(s_low):
                cleaned_s = s.strip()[:650]
                if not result["first_sentence"]:
                    result["first_sentence"] = cleaned_s
                dates = DATE_REGEX.findall(s)
                if dates and ACTION_REGEX.search(s):
                    result["exact_date"] = dates[0]
                    result["exact_sentence"] = cleaned_s
                    return result

        # Pass 2: adjacent sentence pair
        for i in range(len(sentences) - 1):
            pair = f"{sentences[i]} {sentences[i + 1]}"
            p_low = pair.lower()
            if has_person(p_low) and has_committee(p_low):
                cleaned_p = pair.strip()[:700]
                if not result["first_sentence"]:
                    result["first_sentence"] = cleaned_p
                dates = DATE_REGEX.findall(pair)
                if dates and ACTION_REGEX.search(pair):
                    result["exact_date"] = dates[0]
                    result["exact_sentence"] = cleaned_p
                    return result

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# UI Header & Settings
# ---------------------------------------------------------------------------
st.markdown("### 🏛️ Committee Start Date Finder")
st.caption("Official SEC EDGAR filings only (`DEF 14A`, `8-K`, `10-K`, `10-Q`) · Zero third-party data")

with st.expander("⚙️ SEC User-Agent (Optional — Required by sec.gov)", expanded=False):
    user_agent = st.text_input(
        "Your Name / Email for SEC.gov",
        value=DEFAULT_USER_AGENT,
        help="SEC.gov requires an identifying User-Agent header (e.g., YourName your@email.com).",
    )

# ---------------------------------------------------------------------------
# Search Form (Mobile-Friendly)
# ---------------------------------------------------------------------------
with st.form("search_form"):
    person_in = st.text_input("Person Name", value="Jack L. Hess", placeholder="e.g., Jack L. Hess")
    company_in = st.text_input(
        "Company Name, Ticker, or CIK",
        value="Donegal Group Inc.",
        placeholder="e.g., Donegal Group Inc. or DFIN",
    )
    committee_in = st.text_input("Committee Name", value="Audit Committee", placeholder="e.g., Audit Committee")
    col_a, col_b = st.columns([2, 1])
    with col_a:
        all_forms_chk = st.checkbox("Scan ALL disclosed SEC filings (DEF 14A, 8-K, 10-K, 10-Q, etc.)", value=True)
    with col_b:
        max_inspect = st.selectbox("Filings to read", [6, 10, 15], index=0)

    submitted = st.form_submit_button("🔍 Find Committee Start Date", use_container_width=True, type="primary")

# ---------------------------------------------------------------------------
# Run Official Protocol on Submit
# ---------------------------------------------------------------------------
if submitted:
    person = person_in.strip()
    company = company_in.strip()
    committee = committee_in.strip()

    if not person or not company or not committee:
        st.warning("Please enter Person Name, Company, and Committee.")
        st.stop()

    t0 = time.time()
    with st.status("Running official SEC EDGAR protocol...", expanded=True) as status:
        st.write(f"1. Resolving **{company}** on official SEC.gov...")
        cik, official_name = resolve_company_cik(company, user_agent)
        if not cik:
            status.update(label="Company not found on SEC EDGAR", state="error")
            st.error(
                f"Could not find an SEC CIK for **{company}**. Try the exact legal company name, US stock ticker (e.g., `DFIN`, `DGICA`), or 10-digit CIK."
            )
            st.stop()

        st.write(f"✅ Resolved: **{official_name}** (`CIK {cik}`)")
        st.write(f"2. Sweeping all official SEC filings for **\"{person}\"** + **\"{committee}\"**...")
        filings = search_sec_filings(cik, person, committee, user_agent, all_forms=all_forms_chk)

        if not filings:
            status.update(label="No matching SEC filings found", state="error")
            st.warning(
                f"No official SEC filings for **{official_name} (CIK {cik})** contained both **\"{person}\"** and **\"{committee}\"**. "
                "Try removing middle initials (e.g., `Jack Hess`) or verify the committee name."
            )
            st.markdown(f"👉 [Open Official SEC Filings for {official_name}](https://www.sec.gov/edgar/browse/?CIK={cik})")
            st.stop()

        st.write(f"✅ Found **{len(filings)}** matching official filings (earliest: **{filings[0]['year']} {filings[0]['form']}** on `{filings[0]['date']}`).")
        to_scan = filings[:max_inspect]
        st.write(f"3. Reading full text of the earliest **{len(to_scan)}** official filings in parallel to check for an exact join date...")

        inspected: List[Dict] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(inspect_single_filing, f, person, committee, user_agent) for f in to_scan]
            for fut in as_completed(futures):
                inspected.append(fut.result())

        # Preserve chronological order
        inspected.sort(key=lambda r: r["filing"]["date"])

        # Also check prior DEF 14A before first appearance
        prior_proxy = get_prior_proxy_filing(cik, filings[0]["date"], user_agent)

        elapsed = time.time() - t0
        status.update(label=f"Completed in {elapsed:.1f}s · {len(filings)} official filings found", state="complete", expanded=False)

    # Determine Outcome: Exact Date vs First-Appearance Fallback
    exact_hit = next((r for r in inspected if r["exact_date"]), None)
    first_hit = next((r for r in inspected if r["first_sentence"]), None) or inspected[0]

    # Prefer earliest DEF 14A if available for the first-appearance label, otherwise earliest filing
    earliest_filing = filings[0]
    earliest_def14a = next((f for f in filings if "14A" in f["form"].upper()), None)

    if exact_hit:
        ef = exact_hit["filing"]
        st.markdown(
            f"""
            <div class="result-card">
              <span class="badge-exact">✓ Exact Date Found in Official Filing</span>
              <div class="big-answer">{html.escape(exact_hit['exact_date'])}</div>
              <div style="font-size:0.9rem;color:#475569;">
                <strong>Source Filing:</strong> {html.escape(ef['year'])} {html.escape(ef['form'])} (Filed {html.escape(ef['date'])}) · {html.escape(official_name)}
              </div>
              <div class="highlight-box">
                <div style="font-size:0.78rem;font-weight:700;color:#0f172a;margin-bottom:4px;">HIGHLIGHTED LINE FROM OFFICIAL FILING:</div>
                <mark class="exact-hit">{html.escape(exact_hit['exact_sentence'])}</mark>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(f"📄 Open Official {ef['year']} {ef['form']} Filing on SEC.gov", ef["url"], use_container_width=True)
    else:
        ff = earliest_def14a or earliest_filing
        ff_inspect = next((r for r in inspected if r["filing"]["adsh"] == ff["adsh"]), first_hit)
        prior_note = (
            f"Not listed in prior <strong>{html.escape(prior_proxy['year'])} DEF 14A</strong> (filed {html.escape(prior_proxy['date'])})."
            if prior_proxy
            else "No earlier DEF 14A proxy statement found prior to this filing."
        )
        snippet_html = (
            f'<mark class="first-hit">{html.escape(ff_inspect["first_sentence"])}</mark>'
            if ff_inspect and ff_inspect.get("first_sentence")
            else "Person and committee both appear in this filing; open the official link below to inspect the committee table directly."
        )

        st.markdown(
            f"""
            <div class="result-card">
              <span class="badge-first">First-Appearance Filing (No Exact Date Disclosed)</span>
              <div class="big-answer">{html.escape(ff['year'])} {html.escape(ff['form'])}</div>
              <div style="font-size:0.9rem;color:#475569;">
                <strong>Filed:</strong> {html.escape(ff['date'])} · <strong>Company:</strong> {html.escape(official_name)} (CIK {html.escape(cik)})<br/>
                {prior_note}
              </div>
              <div class="highlight-box">
                <div style="font-size:0.78rem;font-weight:700;color:#0f172a;margin-bottom:4px;">FIRST-APPEARANCE LINE INSPECTED ({html.escape(ff['year'])} {html.escape(ff['form'])}):</div>
                {snippet_html}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(f"📄 Open Official {ff['year']} {ff['form']} Filing on SEC.gov", ff["url"], use_container_width=True)

    # All Matching Official Filings List
    st.markdown("#### 📋 All Matching Official SEC Filings (Chronological)")
    if prior_proxy:
        st.markdown(
            f"- `{prior_proxy['date']}` · **DEF 14A** *(Prior filing — not yet listed)* · [Open Official Filing]({prior_proxy['url']})"
        )
    for idx, f in enumerate(filings[:20]):
        tag = " *(Earliest Match)*" if idx == 0 else ""
        st.markdown(f"- `{f['date']}` · **{f['form']}**{tag} · [Open Official Filing]({f['url']})")

    st.markdown(f"🔗 [View Full Official SEC Archive for {official_name}](https://www.sec.gov/edgar/browse/?CIK={cik})")
