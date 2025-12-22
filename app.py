import streamlit as st
import os
import logging
import urllib.parse
import pickle
import tiktoken
import re
import time
from io import BytesIO

# --- NEW: WORD DOC GENERATION ---
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH

# --- MODERN PARTNER PACKAGES ---
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings, ChatOpenAI

# --- STANDARD LANGCHAIN IMPORTS (FIXED) ---
from langchain.retrievers import ParentDocumentRetriever, EnsembleRetriever
from langchain.storage import LocalFileStore
from langchain.storage._lc_store import create_kv_docstore
from langchain_community.retrievers import BM25Retriever

# --- CORE BUILDING BLOCKS ---
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_text_splitters import RecursiveCharacterTextSplitter

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ST_DB_PATH = os.path.join(BASE_DIR, "legal_db_vectors")
DOC_STORE_PATH = os.path.join(BASE_DIR, "legal_docstore_fs")
BM25_PATH = os.path.join(BASE_DIR, "bm25_retriever.pkl")
COLLECTION_NAME = "legal_cases_eyecite"

PREFERRED_MODEL = "gpt-4o" 
TEMPERATURE = 0.4

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AdLitemPro")

# --- UI SETUP ---
st.set_page_config(page_title="AdLitem Pro", layout="wide", page_icon="⚖️")

# --- CUSTOM CSS (BLUE THEME) ---
st.markdown("""
<style>
    .stApp { max-width: 1100px; margin: 0 auto; }
    
    /* BRANDING & HEADERS */
    .main-header { font-family: 'Helvetica Neue', sans-serif; font-size: 2.8rem; color: #FFFFFF; font-weight: 800; text-align: center; margin-bottom: 0.2rem; }
    .subtitle { font-size: 0.95rem; color: #94A3B8; text-align: center; margin-bottom: 2rem; font-weight: 400; letter-spacing: 0.05em; }
    
    /* INPUT BOX BLUE HIGHLIGHT (FORCE OVERRIDE) */
    div[data-baseweb="input"]:focus-within {
        border-color: #38BDF8 !important;
        box-shadow: 0 0 0 1px #38BDF8 !important;
    }
    .stButton button {
        border-color: #38BDF8 !important;
        color: #38BDF8 !important;
    }
    .stButton button:hover {
        border-color: #0EA5E9 !important;
        color: #0EA5E9 !important;
    }

    /* MEMO STYLES */
    .memo-container { background: #FFFFFF; border-radius: 8px; border: 1px solid #E2E8F0; overflow: hidden; margin-bottom: 1.5rem; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
    .brief-answer { background-color: #F8FAFC; color: #0F172A; padding: 24px; border-bottom: 2px solid #38BDF8; font-family: 'Georgia', serif; font-size: 1.05rem; line-height: 1.6; }
    .discussion-box { background-color: #FFFFFF; color: #1E293B; padding: 32px; font-family: 'Georgia', serif; font-size: 1.1rem; line-height: 1.8; }
    .memo-header { color: #0369A1; font-weight: 800; font-size: 1.4rem; margin-top: 1.5rem; margin-bottom: 0.8rem; font-family: 'Helvetica Neue', sans-serif; text-transform: uppercase; letter-spacing: 0.03em; }
    .inline-citation { color: #0284c7; font-weight: bold; font-size: 0.9em; }

    /* AUTHORITY LIST */
    .auth-item { border-left: 4px solid #38BDF8; background: #1E293B; padding: 15px; margin-bottom: 12px; border-radius: 0 4px 4px 0; }
    .auth-label { font-size: 0.7rem; font-weight: 800; color: #38BDF8; text-transform: uppercase; margin-bottom: 5px; letter-spacing: 0.05em; }
    .auth-title { font-weight: 700; color: #F1F5F9; font-size: 1rem; margin-bottom: 2px; }
    .auth-cite { color: #94A3B8; font-size: 0.85rem; font-style: italic; margin-bottom: 8px; }
    .auth-snippet { color: #CBD5E1; font-size: 0.85rem; line-height: 1.4; border-top: 1px solid #334155; padding-top: 8px; margin-top: 8px; }
    .scholar-link-inline { color: #38BDF8; text-decoration: none; font-size: 0.75rem; font-weight: 600; margin-left: 10px; }
</style>
""", unsafe_allow_html=True)

# --- AUTHENTICATION GATE (WITH AUTHOR CREDIT) ---
def check_password():
    """Returns True if the user had the correct username/password."""
    
    def password_entered():
        if "username" in st.session_state and "password" in st.session_state:
            user = st.session_state["username"]
            pwd = st.session_state["password"]
            
            # Multi-user check against secrets
            if (user in st.secrets["passwords"] and pwd == st.secrets["passwords"][user]):
                st.session_state["password_correct"] = True
                del st.session_state["password"]
                del st.session_state["username"]
            else:
                st.session_state["password_correct"] = False

    credit_html = """
    <div style="text-align: center; margin-bottom: 15px;">
        <span style="font-family: 'Helvetica Neue', sans-serif; font-size: 0.75rem; color: #64748B; letter-spacing: 0.1em; text-transform: uppercase;">
            Created by <span style="color: #E2E8F0; font-weight: 600;">Ernest Anemone, Esq.</span>
        </span>
    </div>
    """

    if "password_correct" not in st.session_state:
        # First Run
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown('<div style="margin-top: 80px;"></div>', unsafe_allow_html=True)
            st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
            st.markdown(credit_html, unsafe_allow_html=True)
            st.markdown('<div class="subtitle">AUTHORIZED PERSONNEL ONLY</div>', unsafe_allow_html=True)
            st.text_input("Username", key="username")
            st.text_input("Password", type="password", on_change=password_entered, key="password")
        return False
        
    elif not st.session_state["password_correct"]:
        # Failed Attempt
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown('<div style="margin-top: 80px;"></div>', unsafe_allow_html=True)
            st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
            st.markdown(credit_html, unsafe_allow_html=True)
            st.markdown('<div class="subtitle">AUTHORIZED PERSONNEL ONLY</div>', unsafe_allow_html=True)
            st.text_input("Username", key="username")
            st.text_input("Password", type="password", on_change=password_entered, key="password")
            st.error("😕 Access Denied")
        return False
        
    return True

if not check_password():
    st.stop()

# --- UTILITIES ---
def clean_plain_text(text: str) -> str:
    if not text: return ""
    clean = re.compile('<.*?>')
    return re.sub(clean, '', str(text)).strip()

def clean_llm_output(text: str) -> str:
    text = re.sub(r'^```html\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

# --- WORD DOC GENERATOR ---
def create_docx(content: str) -> BytesIO:
    doc = Document()
    
    # Title
    title = doc.add_heading('Legal Research Memo', 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    doc.add_paragraph(f"Generated by AdLitem Pro | {time.strftime('%B %d, %Y')}")
    doc.add_paragraph("__________________________________________________________________")

    # Split content by lines to process headers and tags
    # First, handle the SECTION BREAK split
    parts = content.split("===SECTION_BREAK===")
    
    # Combine parts back with a clear delimiter for processing line-by-line
    # We will treat the Brief Answer special if it exists
    full_text_lines = []
    
    if len(parts) > 1:
        full_text_lines.append("BRIEF ANSWER")
        full_text_lines.extend(parts[0].split('\n'))
        full_text_lines.append("") # Spacer
        full_text_lines.append("DISCUSSION")
        full_text_lines.extend(parts[1].split('\n'))
    else:
        full_text_lines = content.split('\n')

    # Regex to find our custom HTML tags
    header_re = re.compile(r'<div class="memo-header">(.*?)</div>', re.IGNORECASE)
    citation_re = re.compile(r'<span class="inline-citation">(.*?)</span>', re.IGNORECASE)
    
    for line in full_text_lines:
        line = line.strip()
        if not line:
            continue
            
        # Check for Header
        header_match = header_re.search(line)
        if header_match:
            doc.add_heading(header_match.group(1), level=1)
            continue
            
        # Check for explicit section labels we added above
        if line in ["BRIEF ANSWER", "DISCUSSION"]:
            doc.add_heading(line, level=1)
            continue

        # Process Body Text (Remove citation tags but keep text)
        clean_line = citation_re.sub(r'\1', line) # Replace <span...>cite</span> with cite
        
        # Remove any other leftover HTML tags
        clean_line = clean_plain_text(clean_line)
        
        p = doc.add_paragraph(clean_line)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    # Save to buffer
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer

def render_memo_ui(content: str, key_idx: int):
    # Render HTML Display
    if "===SECTION_BREAK===" in content:
        parts = content.split("===SECTION_BREAK===")
        brief_section = parts[0]
        discussion_section = "".join(parts[1:])
        st.markdown(f'''
            <div class="memo-container">
                <div class="brief-answer">{brief_section}</div>
                <div class="discussion-box">{discussion_section}</div>
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="memo-container"><div class="discussion-box">{content}</div></div>', unsafe_allow_html=True)

    # Render Download Button
    doc_file = create_docx(content)
    st.download_button(
        label="📄 Download Memo as Word Doc",
        data=doc_file,
        file_name=f"Legal_Memo_{key_idx}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        key=f"dl_btn_{key_idx}"
    )

def get_badge_label(metadata):
    dtype = metadata.get("type", "case")
    if dtype == "policy": return "DCF POLICY"
    if dtype == "statute": return "STATUTE"
    if dtype == "cic_manual": return "CIC MANUAL"
    return "PUBLISHED" if metadata.get("is_published") else "UNPUBLISHED"

# --- NEW: HISTORY CONTEXTUALIZATION ---
def rewrite_query(original_input, chat_history):
    """Rewrites the user query to include context from history."""
    if not chat_history:
        return original_input
        
    llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=0.3)
    
    prompt = f"""
    Given the following conversation history and a follow-up question, 
    rephrase the follow-up question to be a standalone query that includes all necessary context.
    
    Chat History:
    {chat_history}
    
    Follow-Up Input: {original_input}
    
    Standalone Question:
    """
    
    response = llm.invoke(prompt)
    return response.content

# --- DATABASE LOADING ---
@st.cache_resource
def get_retriever():
    if not os.path.exists(ST_DB_PATH): 
        return None
    
    vectorstore = Chroma(
        collection_name=COLLECTION_NAME, 
        embedding_function=OpenAIEmbeddings(), 
        persist_directory=ST_DB_PATH
    )
    
    fs = LocalFileStore(DOC_STORE_PATH)
    store = create_kv_docstore(fs)
    
    pdr = ParentDocumentRetriever(
        vectorstore=vectorstore, 
        docstore=store, 
        child_splitter=RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100),
        search_kwargs={"k": 15}
    )
    
    if os.path.exists(BM25_PATH):
        try:
            with open(BM25_PATH, "rb") as f:
                bm25 = pickle.load(f)
                return EnsembleRetriever(retrievers=[pdr, bm25], weights=[0.7, 0.3])
        except Exception:
            return pdr
    return pdr

# --- MAIN APP UI ---
st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">NEW JERSEY CHILD WELFARE LAW RESEARCH ENGINE</div>', unsafe_allow_html=True)

if "messages" not in st.session_state: st.session_state.messages = []
if "last_sources" not in st.session_state: st.session_state.last_sources = []

# --- LANDING PAGE ---
if not st.session_state.messages:
    st.markdown('<div style="height: 4vh;"></div>', unsafe_allow_html=True)
    
    # Author Credit
    st.markdown("""
    <div style="text-align: center; margin-bottom: 40px;">
        <span style="font-family: 'Helvetica Neue', sans-serif; font-size: 0.85rem; color: #64748B; letter-spacing: 0.1em; text-transform: uppercase;">
            Created by <span style="color: #E2E8F0; font-weight: 600;">Ernest Anemone, Esq.</span>
        </span>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
    <div style="text-align: center; color: #94A3B8; margin-bottom: 30px; font-size: 0.9rem;">
        Select a sample fact pattern to test the research engine:
    </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    
    def set_prompt(text):
        st.session_state.messages.append({"role": "user", "content": text})
    
    with col1:
        # Scenario: Cannabis / CREAMMA Act
        if st.button("🌿 Cannabis & 'Imminent Harm'", use_container_width=True, help="Analyze impact of CREAMMA on Title 9"):
            set_prompt("Analyze the impact of the NJ CREAMMA Act on Title 9 litigation. Specifically: Does a positive newborn toxicology screen for THC, absent evidence of actual parenting impairment, legally sustain a finding of abuse or neglect? Cite recent Appellate Division precedents.")
            st.rerun()
        st.caption("Newborn toxicology vs. actual impairment under CREAMMA.")
            
    with col2:
        # Scenario: The "Safety Plan" Trap (Due Process)
        if st.button("🚧 'Safety Plans' & Due Process", use_container_width=True, help="Analyze legality of extra-judicial removals"):
            set_prompt("Analyze the legality of DCPP 'Safety Protection Plans' that require a parent to leave the home under threat of removal. Does this constitute a 'constructive removal' requiring a Dodd hearing?")
            st.rerun()
        st.caption("Constructive removal vs. voluntary agreement.")
            
    with col3:
        # Scenario: KLG Defense (The "P.P." Standard)
        if st.button("👨‍👩‍👧 KLG vs. Adoption Preference", use_container_width=True, help="Kinship Legal Guardianship standards"):
            set_prompt("If a resource parent unequivocally prefers adoption, can the court still grant Kinship Legal Guardianship (KLG) to avoid Termination of Parental Rights (TPR)? Analyze the 'clear and convincing' evidence standard under the KLG Act amendments.")
            st.rerun()
        st.caption("Resource parent preference vs. statutory KLG defense.")
    
    st.markdown("""
    <div style="text-align: center; color: #475569; font-size: 0.75rem; margin-top: 50px;">
        AdLitem Pro is an AI-assisted research tool. Results must be verified by a licensed attorney.
    </div>
    """, unsafe_allow_html=True)

# Render History
for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            # Pass index to generate unique keys for buttons
            render_memo_ui(msg["content"], idx)
        else:
            st.markdown(msg["content"])

# Input
if prompt := st.chat_input("Start legal research task..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.rerun()

# Execution
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    current_prompt = st.session_state.messages[-1]["content"]
    
    with st.chat_message("assistant"):
        retriever = get_retriever()
        
        if not retriever:
            st.error("Database not found. Please check repository file structure.")
        else:
            progress_bar = st.progress(0, text="Analyzing request history...")
            
            with st.status("Conducting Deep Research...", expanded=False) as status:
                
                # --- STEP 1: CONTEXTUALIZE ---
                chat_history_str = "\n".join([f"{m['role']}: {m['content']}" for m in st.session_state.messages[:-1]])
                search_query = rewrite_query(current_prompt, chat_history_str)
                
                if search_query != current_prompt:
                    logger.info(f"Rewrote query: {current_prompt} -> {search_query}")
                    status.update(label=f"Refined Query: {search_query[:50]}...")

                try:
                    # --- STEP 2: RETRIEVE (WITH AUTHORITY MIX + CIC MANUAL + TITLE 9) ---
                    progress_bar.progress(20, text="Querying database...")
                    
                    # Primary search
                    docs = retriever.invoke(search_query)
                    
                    # Supplemental search (UPDATED to include TITLE 9)
                    supp_query = f"{search_query} DCF Policy CP&P N.J.A.C. N.J.S.A. Title 9 Title 30 CIC Manual"
                    supplemental = retriever.invoke(supp_query)[:15]
                    
                    # Deduplication
                    unique_docs = []
                    seen_content = set()
                    for d in (docs + supplemental):
                        content_hash = hash(d.page_content[:150])
                        if content_hash not in seen_content:
                            seen_content.add(content_hash)
                            unique_docs.append(d)
                    
                    unique_docs = unique_docs[:30] # Increased context window
                    context_blocks = []
                    st.session_state.last_sources = []
                    citation_map = {}

                    progress_bar.progress(50, text="Sanitizing authorities...")
                    for i, doc in enumerate(unique_docs):
                        meta = doc.metadata
                        
                        # --- CITATION HYGIENE ---
                        cite_str = clean_plain_text(meta.get("bluebook", meta.get("source", "")))
                        
                        # Fix PDF filenames & Special Handling for CIC Manuals
                        if ".pdf" in cite_str.lower() or ".txt" in cite_str.lower():
                            if "cic" in cite_str.lower():
                                # Extract section number from filename like "CIC_Manual_1601_1" -> "1601.1"
                                sec_match = re.search(r'(\d+)[_.](\d+)', cite_str)
                                if sec_match:
                                    cite_str = f"CIC Manual § {sec_match.group(1)}.{sec_match.group(2)}"
                                else:
                                    # Fallback simple number
                                    sec_match_simple = re.search(r'(\d+)', cite_str)
                                    if sec_match_simple:
                                        cite_str = f"CIC Manual § {sec_match_simple.group(1)}"
                                    else:
                                        cite_str = "NJ DCF Concurrent Planning (CIC) Manual"
                            else:
                                cite_str = "NJ DCF Internal Policy / Administrative Record"
                        
                        if not cite_str:
                            cite_str = "Unknown Legal Authority"

                        content = clean_plain_text(doc.page_content)
                        title = clean_plain_text(meta.get("display_name", "Authority"))
                        
                        source_id = i + 1
                        doc_type = meta.get("type", "case").upper()
                        context_blocks.append(f"SOURCE {source_id} [{doc_type} - {cite_str}]:\n{content}\n")
                        citation_map[source_id] = cite_str
                        
                        # Generate Link
                        link = None
                        if meta.get("type", "case") == "case":
                            search_query_url = meta.get("docket", "")
                            if not search_query_url:
                                match = re.search(r'No\.\s*([\w-]+)', cite_str)
                                search_query_url = match.group(1) if match else cite_str
                            
                            badge_label = get_badge_label(meta)
                            if badge_label == "UNPUBLISHED":
                                link = f"https://scholar.google.com/scholar?as_sdt=4,31&q={urllib.parse.quote(search_query_url)}"
                            else:
                                link = f"https://scholar.google.com/scholar?q={urllib.parse.quote(search_query_url)}"

                        st.session_state.last_sources.append({
                            "label": get_badge_label(meta),
                            "title": title,
                            "cite": cite_str,
                            "snippet": content[:350],
                            "link": link
                        })
                        
                    status.update(label=f"Found {len(st.session_state.last_sources)} authorities (Cases, Statutes, Policies).", state="complete")
                    progress_bar.progress(70, text="Drafting Research Memo...")

                    # --- STEP 3: GENERATE (WITH STRICT PERIOD PLACEMENT) ---
                    llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=TEMPERATURE)
                    sys_prompt = """You are a Senior Legal Research Attorney. Write a formal Research Memo based ONLY on provided SOURCES.
                    
                    CRITICAL GRAMMAR ENFORCEMENT (PERIOD PLACEMENT):
                    - You MUST place a period '.' immediately AFTER the legal claim, BEFORE opening the citation span.
                    - INCORRECT: "The court disagreed <span class="inline-citation">State v. Jones</span>."
                    - INCORRECT: "The court disagreed <span class="inline-citation">State v. Jones</span>."
                    - CORRECT: "The court disagreed. <span class="inline-citation">State v. Jones</span>."
                    
                    CRITICAL INSTRUCTION 1: CITATION HYGIENE
                    - NEVER cite a file path. Use the citation provided in the map (e.g., 'CIC Manual § 1601.1').
                    - If a specific authority is not named, cite as 'NJ DCF Policy Manual' or 'Administrative Record'.
                    
                    CRITICAL INSTRUCTION 2: PRECEDENTIAL VALUE
                    - When citing an UNPUBLISHED opinion, you must check if it relies on a PUBLISHED case for the relevant point of law.
                    - If so, append the published citation in a parenthetical. 
                    - Example Format: *Div. v. A.B.*, No. A-1234-20 (Unpublished) (citing *Div. v. G.M.*, 198 N.J. 382).
                    
                    CRITICAL INSTRUCTION 3: DIVERSE AUTHORITIES
                    - Integrate statutes (Title 9/30), Case Law, DCF Policies, and the CIC (Concurrent Planning) Manual if available.
                    
                    STRICT FORMATTING RULES:
                    1. No memo headers. Start at 'Question Presented'.
                    2. Wrap main section headers in <div class="memo-header">HEADER TEXT</div>.
                    3. For claims, use inline citations: <span class="inline-citation">Bluebook Cite</span>.
                    4. Use '===SECTION_BREAK===' ONLY once, after 'Brief Answer'."""
                    
                    chain_prompt = ChatPromptTemplate.from_messages([
                        ("system", sys_prompt),
                        ("user", "CITATIONS: {citations}\n\nCONTEXT: {context}\n\nISSUE: {input}")
                    ])
                    
                    response = (chain_prompt | llm | StrOutputParser()).invoke({
                        "input": search_query, "context": "\n\n".join(context_blocks), "citations": citation_map
                    })
                    
                    clean_resp = clean_llm_output(response)
                    progress_bar.progress(100, text="Memo Complete.")
                    time.sleep(0.5)
                    progress_bar.empty()
                    
                    st.session_state.messages.append({"role": "assistant", "content": clean_resp})
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"An error occurred during research: {e}")
                    logger.error(f"Execution Error: {e}")
                    progress_bar.empty()

# --- FOOTER ---
if st.session_state.last_sources:
    st.markdown("---")
    with st.expander("📚 View Authority Library", expanded=False):
        for src in st.session_state.last_sources:
            link_html = f'<a href="{src["link"]}" target="_blank" class="scholar-link-inline">View on Google Scholar ↗</a>' if src["link"] else ""
            st.markdown(f"""
            <div class="auth-item">
                <div class="auth-label">{src['label']}{link_html}</div>
                <div class="auth-title">{src['title']}</div>
                <div class="auth-cite">{src['cite']}</div>
                <div class="auth-snippet">"{src['snippet']}..."</div>
            </div>
            """, unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ Clear Research Session", use_container_width=True):
            st.session_state.messages = []
            st.session_state.last_sources = []
            st.rerun()