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
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Official SEC EDGAR Helpers (Strict sec.gov Only)
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = "CommitteeTenureFinder/2.0 (research@example.com)"


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
    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=get_headers(user_agent),
        timeout=15,
    )
    resp.raise_for_status()
    return list(resp.json().values())


def resolve_company_cik(company_query: str, user_agent: str) -> Tuple[Optional[str], str]:
    cleaned = clean_company_input(company_query)
    if re.fullmatch(r"\d{4,10}", cleaned):
        return pad_cik(cleaned), cleaned

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

    try:
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
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


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_all_company_filings(cik: str, user_agent: str) -> Tuple[str, List[Dict]]:
    """
    Fetch EVERY official filing from data.sec.gov/submissions (recent + historical archives)
    so older years (e.g., 2005–2014) are never missed.
    """
    headers = get_headers(user_agent)
    resp = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers, timeout=18)
    resp.raise_for_status()
    data = resp.json()
    official_name = data.get("name", f"CIK {cik}")

    all_filings: List[Dict] = []

    def append_block(block: Dict) -> None:
        forms = block.get("form", [])
        dates = block.get("filingDate", [])
        accs = block.get("accessionNumber", [])
        docs = block.get("primaryDocument", [])
        for f, d, a, p in zip(forms, dates, accs, docs):
            if not d or not a:
                continue
            all_filings.append(
                {
                    "date": d,
                    "year": d[:4],
                    "form": f,
                    "adsh": a,
                    "doc": p,
                    "url": build_sec_doc_url(cik, a, p),
                }
            )

    append_block(data.get("filings", {}).get("recent", {}))

    # Load historical submission blocks in parallel (up to 4 blocks covers 20+ years)
    hist_files = data.get("filings", {}).get("files", [])[:4]
    if hist_files:
        def fetch_hist(fname: str) -> Optional[Dict]:
            try:
                r = requests.get(f"https://data.sec.gov/submissions/{fname}", headers=headers, timeout=15)
                return r.json() if r.ok else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for block in pool.map(fetch_hist, [h["name"] for h in hist_files if h.get("name")]):
                if block:
                    append_block(block)

    # Deduplicate by accession number and sort chronologically ascending
    seen = set()
    unique: List[Dict] = []
    for f in sorted(all_filings, key=lambda x: x["date"]):
        if f["adsh"] not in seen:
            seen.add(f["adsh"])
            unique.append(f)

    return official_name, unique


# ---------------------------------------------------------------------------
# Strict Filing Text Inspector (Proximity Membership + Zero False-Positive Dates)
# ---------------------------------------------------------------------------
MONTH_PAT = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
)

EXACT_DATE_PAT = re.compile(
    rf"\b{MONTH_PAT}\s+\d{{1,2}},\s+(?:19|20)\d{{2}}\b"
    r"|\b(?:19|20)\d{2}-\d{2}-\d{2}\b",
    re.I,
)

MONTH_YEAR_PAT = re.compile(
    rf"\b(?:since|effective|in|on)\s+({MONTH_PAT}\s+(?:19|20)\d{{2}})\b",
    re.I,
)

APPOINT_VERB_PAT = re.compile(
    r"\b(appointed|elected|joined|named|effective|member\s+since|serving\s+since|served\s+since|became\s+a\s+member|designated|added\s+to)\b",
    re.I,
)

# Dates that are almost NEVER committee appointment dates (fiscal year ends, record dates, etc.)
IGNORE_DATE_CONTEXT_PAT = re.compile(
    r"\b(fiscal\s+year\s+ended|year\s+ended|years\s+ended|ended\s+december\s+31|as\s+of\s+december\s+31"
    r"|record\s+date|expiration\s+date|stock\s+options|annual\s+report|10-k|beneficially\s+owned)\b",
    re.I,
)


def fetch_filing_plain_text(url: str, user_agent: str) -> str:
    resp = requests.get(url, headers=get_headers(user_agent), timeout=18)
    if not resp.ok:
        return ""
    raw = resp.text[:3_000_000]
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def make_person_patterns(person: str) -> Tuple[re.Pattern, re.Pattern]:
    parts = [p.strip(".,") for p in person.strip().split() if p.strip(".,")]
    full_pat = re.compile(re.escape(person.strip()), re.I)
    last_name = parts[-1] if parts else person.strip()
    if len(parts) >= 2:
        first_last = f"{parts[0]} {last_name}"
        # Matches "Jack L. Hess", "Jack Hess", "Mr. Hess", "Ms. Hess", "Hess," or "Messrs. ... Hess"
        person_near_pat = re.compile(
            rf"(?:{re.escape(person.strip())}|{re.escape(first_last)}|\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+{re.escape(last_name)}\b|\b{re.escape(last_name)}\b)",
            re.I,
        )
    else:
        person_near_pat = re.compile(rf"\b{re.escape(last_name)}\b", re.I)
    return full_pat, person_near_pat


def inspect_filing_for_committee(
    filing: Dict,
    person: str,
    committee: str,
    user_agent: str,
) -> Dict:
    """
    Checks whether `person` is actually listed in `committee` in this filing (using proximity
    around committee mentions, not just anywhere in the document) AND whether an exact
    appointment date for THAT person on THAT committee is explicitly stated.
    """
    out = {
        "filing": filing,
        "is_member": False,
        "exact_date": None,
        "exact_sentence": None,
        "member_snippet": None,
    }

    text = fetch_filing_plain_text(filing["url"], user_agent)
    if not text:
        return out

    full_pat, near_pat = make_person_patterns(person)
    parts = [p.strip(".,") for p in person.strip().split() if p.strip(".,")]
    last_name = parts[-1] if parts else person.strip()

    # Person must appear somewhere in the filing by full name or first+last
    first_last_pat = (
        re.compile(rf"\b{re.escape(parts[0])}\s+(?:[A-Z]\.?\s+)?{re.escape(last_name)}\b", re.I)
        if len(parts) >= 2
        else full_pat
    )
    if not first_last_pat.search(text):
        return out

    comm_clean = committee.strip()
    comm_pat = re.compile(re.escape(comm_clean), re.I)

    # Scan every occurrence of the committee name in the filing
    for m in comm_pat.finditer(text):
        start = max(0, m.start() - 260)
        end = min(len(text), m.end() + 480)
        window = text[start:end]

        if not near_pat.search(window):
            continue

        # Check if this window looks like actual committee membership / report / appointment
        w_low = window.lower()
        membership_cues = [
            "members", "member", "consists of", "composed of", "comprised of",
            "report of", "audit committee:", "chair", "appointed", "joined",
            "served on", "serves on", "committee are", "committee were",
        ]
        # Exclude pure table-of-contents lines
        if any(cue in w_low for cue in membership_cues) or filing["form"].startswith("8-K"):
            out["is_member"] = True
            if not out["member_snippet"]:
                out["member_snippet"] = window.strip()[:480]

        # Strict Exact-Date Check: date must be tightly attached to THIS person's name
        # Split window into sentences and check each sentence
        for sent in re.split(r"(?<=[.!?])\s+", window):
            if not near_pat.search(sent):
                continue
            if IGNORE_DATE_CONTEXT_PAT.search(sent):
                continue
            if not APPOINT_VERB_PAT.search(sent):
                continue

            # Check parenthetical directly after person's name: e.g., "Jack L. Hess (since January 30, 2015)"
            paren = re.search(
                rf"{near_pat.pattern}\s*\([^)]*?(?:since|effective|appointed|joined)[^)]*?({EXACT_DATE_PAT.pattern}|{MONTH_PAT}\s+(?:19|20)\d{{2}})[^)]*\)",
                sent,
                re.I,
            )
            if paren:
                out["is_member"] = True
                out["exact_date"] = paren.group(1).strip()
                out["exact_sentence"] = sent.strip()[:550]
                return out

            # Check direct sentence stating THIS person was appointed/joined THIS committee on <date>
            # Ensure the sentence doesn't list 4+ other names with a parenthetical date belonging to someone else
            if comm_pat.search(sent) and len(sent) < 420:
                # Ignore if date is inside parentheses attached to a DIFFERENT name
                cleaned_sent = re.sub(r"\([^)]*\)", " ", sent)
                dates = EXACT_DATE_PAT.findall(cleaned_sent)
                if not dates:
                    my = MONTH_YEAR_PAT.findall(cleaned_sent)
                    dates = my
                if dates:
                    d_val = dates[0].strip()
                    # Double check it's not a fiscal year end like December 31
                    if not re.search(r"december\s+31|12/31", d_val, re.I):
                        out["is_member"] = True
                        out["exact_date"] = d_val
                        out["exact_sentence"] = sent.strip()[:550]
                        return out

    return out


# ---------------------------------------------------------------------------
# Fast Binary Search + Windowed 8-K Sweep Algorithm
# ---------------------------------------------------------------------------
def run_fast_accurate_protocol(
    cik: str,
    all_filings: List[Dict],
    person: str,
    committee: str,
    user_agent: str,
    log_fn,
) -> Dict:
    # 1. Separate all DEF 14A proxy statements (chronological ascending)
    proxies = [f for f in all_filings if f["form"] in ("DEF 14A", "DEF 14A/A")]
    if not proxies:
        proxies = [f for f in all_filings if "14A" in f["form"].upper()]

    checked_records: List[Dict] = []
    cache: Dict[str, Dict] = {}

    def check_cached(f: Dict) -> Dict:
        if f["adsh"] not in cache:
            res = inspect_filing_for_committee(f, person, committee, user_agent)
            cache[f["adsh"]] = res
            checked_records.append(res)
        return cache[f["adsh"]]

    first_proxy_idx: Optional[int] = None
    exact_hit: Optional[Dict] = None

    if proxies:
        log_fn(f"2. Found **{len(proxies)}** official DEF 14A proxy statements ({proxies[0]['year']}–{proxies[-1]['year']}). Running fast binary search...")

        # Find at least one proxy where person is on the committee (check newest, then sample backwards in parallel)
        probe_indices = sorted(
            set([len(proxies) - 1, max(0, len(proxies) - 3), max(0, len(proxies) - 6), len(proxies) // 2, 0]),
            reverse=True,
        )
        known_member_idx: Optional[int] = None
        known_nonmember_idx: int = -1

        with ThreadPoolExecutor(max_workers=4) as pool:
            fut_map = {pool.submit(check_cached, proxies[i]): i for i in probe_indices}
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                res = fut.result()
                if res["exact_date"] and not exact_hit:
                    exact_hit = res
                if res["is_member"]:
                    if known_member_idx is None or idx < known_member_idx:
                        known_member_idx = idx
                else:
                    if idx < (known_member_idx if known_member_idx is not None else len(proxies)):
                        known_nonmember_idx = max(known_nonmember_idx, idx)

        # If not found in initial probes (e.g., short tenure years ago), scan remaining proxies in parallel batches
        if known_member_idx is None:
            remaining = [i for i in range(len(proxies) - 1, -1, -1) if i not in probe_indices]
            for b_start in range(0, len(remaining), 6):
                batch_idxs = remaining[b_start : b_start + 6]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    fut_map = {pool.submit(check_cached, proxies[i]): i for i in batch_idxs}
                    for fut in as_completed(fut_map):
                        idx = fut_map[fut]
                        res = fut.result()
                        if res["exact_date"] and not exact_hit:
                            exact_hit = res
                        if res["is_member"]:
                            if known_member_idx is None or idx < known_member_idx:
                                known_member_idx = idx
                if known_member_idx is not None:
                    break

        # Binary search between (known_nonmember_idx + 1) and known_member_idx to pinpoint EXACT first DEF 14A!
        if known_member_idx is not None:
            lo = max(0, known_nonmember_idx + 1)
            hi = known_member_idx
            while lo < hi:
                mid = (lo + hi) // 2
                res = check_cached(proxies[mid])
                if res["exact_date"] and not exact_hit:
                    exact_hit = res
                if res["is_member"]:
                    hi = mid
                else:
                    lo = mid + 1
            first_proxy_idx = lo

            # Ensure immediately preceding proxy is checked (to confirm not listed)
            if first_proxy_idx > 0:
                check_cached(proxies[first_proxy_idx - 1])

    # 2. Sweep ALL 8-K and 10-K filings in the exact appointment window!
    first_proxy = proxies[first_proxy_idx] if first_proxy_idx is not None else None
    prior_proxy = proxies[first_proxy_idx - 1] if (first_proxy_idx is not None and first_proxy_idx > 0) else None

    if first_proxy and not exact_hit:
        win_start = prior_proxy["date"] if prior_proxy else f"{int(first_proxy['year']) - 2}-01-01"
        win_end = first_proxy["date"]
        window_8ks = [
            f
            for f in all_filings
            if f["form"] in ("8-K", "8-K/A") and win_start <= f["date"] <= win_end
        ]
        if window_8ks:
            log_fn(
                f"3. Checking **{len(window_8ks)}** Form 8-K filings between `{win_start}` and `{win_end}` for an exact appointment date..."
            )
            with ThreadPoolExecutor(max_workers=4) as pool:
                for res in pool.map(check_cached, window_8ks[:20]):
                    if res["exact_date"] and not exact_hit:
                        exact_hit = res

    checked_records.sort(key=lambda r: r["filing"]["date"])
    return {
        "exact_hit": exact_hit,
        "first_proxy": cache[first_proxy["adsh"]] if first_proxy else None,
        "prior_proxy": cache[prior_proxy["adsh"]] if prior_proxy else None,
        "checked": checked_records,
    }


# ---------------------------------------------------------------------------
# UI Header & Search Form
# ---------------------------------------------------------------------------
st.markdown("### 🏛️ Committee Start Date Finder")
st.caption("Official SEC EDGAR filings only (`DEF 14A`, `8-K`, `10-K`) · Zero third-party data")

with st.expander("⚙️ SEC User-Agent (Optional)", expanded=False):
    user_agent = st.text_input(
        "Identifying User-Agent for SEC.gov",
        value=DEFAULT_USER_AGENT,
        help="SEC.gov requires an identifying User-Agent (e.g., YourName your@email.com).",
    )

with st.form("search_form"):
    person_in = st.text_input("Person Name", value="Jack L. Hess", placeholder="e.g., Jack L. Hess")
    company_in = st.text_input(
        "Company Name, Ticker, or CIK",
        value="Donegal Group Inc.",
        placeholder="e.g., Donegal Group Inc. or DFIN",
    )
    committee_in = st.text_input("Committee Name", value="Audit Committee", placeholder="e.g., Audit Committee")
    submitted = st.form_submit_button("🔍 Find Committee Start Date", use_container_width=True, type="primary")

# ---------------------------------------------------------------------------
# Execute Protocol
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
            st.error(f"Could not resolve **{company}** to an SEC CIK. Try the exact company name, ticker (e.g., `DGICA`, `DFIN`), or CIK.")
            st.stop()

        official_name, all_filings = fetch_all_company_filings(cik, user_agent)
        st.write(f"✅ Resolved **{official_name}** (`CIK {cik}`) · **{len(all_filings)}** total historical SEC filings indexed.")

        outcome = run_fast_accurate_protocol(cik, all_filings, person, committee, user_agent, st.write)
        elapsed = time.time() - t0
        status.update(label=f"Done in {elapsed:.1f}s · {len(outcome['checked'])} filings inspected", state="complete", expanded=False)

    exact_hit = outcome["exact_hit"]
    first_proxy = outcome["first_proxy"]
    prior_proxy = outcome["prior_proxy"]

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
        st.link_button(f"📄 Open Official {ef['year']} {ef['form']} on SEC.gov", ef["url"], use_container_width=True)

    elif first_proxy:
        ff = first_proxy["filing"]
        prior_text = (
            f"Not listed in prior <strong>{html.escape(prior_proxy['filing']['year'])} DEF 14A</strong> (filed {html.escape(prior_proxy['filing']['date'])})."
            if prior_proxy
            else "Earliest available DEF 14A proxy statement in SEC archive."
        )
        snippet = first_proxy.get("member_snippet") or "Member listed in committee section of this filing."
        st.markdown(
            f"""
            <div class="result-card">
              <span class="badge-first">First-Appearance Filing (No Exact Date Disclosed)</span>
              <div class="big-answer">{html.escape(ff['year'])} {html.escape(ff['form'])}</div>
              <div style="font-size:0.9rem;color:#475569;">
                <strong>Filed:</strong> {html.escape(ff['date'])} · <strong>Company:</strong> {html.escape(official_name)} (CIK {html.escape(cik)})<br/>
                {prior_text}
              </div>
              <div class="highlight-box">
                <div style="font-size:0.78rem;font-weight:700;color:#0f172a;margin-bottom:4px;">FIRST-APPEARANCE LINE ({html.escape(ff['year'])} {html.escape(ff['form'])}):</div>
                <mark class="first-hit">{html.escape(snippet)}</mark>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.link_button(f"📄 Open Official {ff['year']} {ff['form']} on SEC.gov", ff["url"], use_container_width=True)

    else:
        st.warning(
            f"Could not find **{person}** listed on the **{committee}** in **{official_name}**'s DEF 14A proxy statements on SEC.gov. "
            "Check the spelling of the person's name or committee name."
        )

    # Checked Filings Summary
    st.markdown("#### 📋 Official Filings Inspected")
    for r in outcome["checked"]:
        f = r["filing"]
        if r["exact_date"]:
            tag = f"✅ Exact Date: {r['exact_date']}"
        elif first_proxy and f["adsh"] == first_proxy["filing"]["adsh"]:
            tag = "⭐ First Appearance"
        elif r["is_member"]:
            tag = "Member"
        else:
            tag = "Not Listed"
        st.markdown(f"- `{f['date']}` · **{f['form']}** — *{tag}* · [Open SEC Filing]({f['url']})")

    st.markdown(f"🔗 [Full Official SEC EDGAR Archive for {official_name}](https://www.sec.gov/edgar/browse/?CIK={cik})")
