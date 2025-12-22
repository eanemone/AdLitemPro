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

# --- STANDARD LANGCHAIN IMPORTS ---
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
TEMPERATURE = 0.5 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AdLitemPro")

# --- UI SETUP ---
st.set_page_config(page_title="AdLitem Pro", layout="wide", page_icon="⚖️")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { max-width: 1100px; margin: 0 auto; }
    
    /* BRANDING */
    .main-header { font-family: 'Helvetica Neue', sans-serif; font-size: 2.8rem; color: #FFFFFF; font-weight: 800; text-align: center; margin-bottom: 0.2rem; }
    .subtitle { font-size: 0.95rem; color: #94A3B8; text-align: center; margin-bottom: 2rem; font-weight: 400; letter-spacing: 0.05em; }
    
    /* --- 1. GLOBAL INPUT OVERRIDES --- */
    input:focus, textarea:focus {
        border-color: #38BDF8 !important;
        box-shadow: 0 0 0 1px #38BDF8 !important;
        outline: none !important;
    }
    
    /* --- 2. SPECIFIC CHAT INPUT TARGETING --- */
    [data-testid="stChatInput"] {
        border-color: #38BDF8 !important;
        background-color: transparent !important;
    }
    
    [data-testid="stChatInput"] textarea {
        caret-color: #38BDF8 !important; 
    }
    
    [data-testid="stChatInput"] textarea:focus {
        border-color: #38BDF8 !important;
        box-shadow: 0 0 0 1px #38BDF8 !important; 
    }
    
    /* --- 3. STANDARD INPUTS --- */
    div[data-baseweb="input"]:focus-within {
        border-color: #38BDF8 !important;
        box-shadow: 0 0 0 1px #38BDF8 !important;
    }
    
    /* --- 4. BUTTONS --- */
    .stButton button { border-color: #38BDF8 !important; color: #38BDF8 !important; }
    .stButton button:hover { border-color: #0EA5E9 !important; color: #0EA5E9 !important; }
    .stButton button:focus { border-color: #38BDF8 !important; color: #38BDF8 !important; box-shadow: 0 0 0 1px #38BDF8 !important; }

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

# --- AUTHENTICATION GATE ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if "login_attempted" not in st.session_state:
        st.session_state["login_attempted"] = False

    def password_entered():
        st.session_state["login_attempted"] = True
        user = st.session_state.get("username", "")
        pwd = st.session_state.get("password", "")
        
        if (user in st.secrets["passwords"] and pwd == st.secrets["passwords"][user]):
            st.session_state["password_correct"] = True
            del st.session_state["password"] 
            del st.session_state["username"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state["password_correct"]:
        return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown('<div style="margin-top: 80px;"></div>', unsafe_allow_html=True)
        st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
        st.markdown("""<div style="text-align: center; margin-bottom: 15px;"><span style="font-family: 'Helvetica Neue', sans-serif; font-size: 0.75rem; color: #64748B; letter-spacing: 0.1em; text-transform: uppercase;">Created by <span style="color: #E2E8F0; font-weight: 600;">Ernest Anemone, Esq.</span></span></div>""", unsafe_allow_html=True)
        st.markdown('<div class="subtitle">AUTHORIZED PERSONNEL ONLY</div>', unsafe_allow_html=True)
        
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", on_change=password_entered, key="password")
        
        if st.session_state["login_attempted"] and not st.session_state["password_correct"]:
            st.error("😕 Access Denied")
            
    return False

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

# --- CLEANER & CITATION ENFORCER ---
def enforce_citations(text: str) -> str:
    # 1. Fix broken newlines
    text = re.sub(r'(N\.J\.A\.C\.|N\.J\.S\.A\.|N\.J\.|N\.J\. Super\.)\s*\n\s*', r'\1 ', text, flags=re.IGNORECASE)
    
    # 2. Normalize "N.J. Admin. Code" or "N.J. Admin. Code §" to "N.J.A.C."
    text = re.sub(r'(?i)N\.?J\.?\s*Admin\.?\s*Code\s*§?\s*', 'N.J.A.C. ', text)

    # 3. Normalize "NJAC" -> "N.J.A.C."
    text = re.sub(r'(?i)\bN\.?J\.?A\.?C\.?\s*(\d+[:\-])', r'N.J.A.C. \1', text)

    # 4. Normalize "NJSA" -> "N.J.S.A."
    text = re.sub(r'(?i)\bN\.?J\.?S\.?A\.?\s*(\d+[:\-])', r'N.J.S.A. \1', text)
    
    # 5. Highlight Statutes/Codes
    statute_pattern = r'(?<!class="inline-citation">)(N\.J\.A\.C\.|N\.J\.S\.A\.|N\.J\.|N\.J\. Super\.)\s*(\d+[:\-]\d+[\d\-\.\w]*)'
    text = re.sub(statute_pattern, r'<span class="inline-citation">\1 \2</span>', text, flags=re.IGNORECASE)
    
    # 6. Highlight Policy
    policy_pattern = r'(?<!class="inline-citation">)(CP\s*&\s*P-[IVX\d\-\w]+)'
    text = re.sub(policy_pattern, r'<span class="inline-citation">\1</span>', text, flags=re.IGNORECASE)
    
    return text

def strip_redundant_headers(text: str) -> str:
    lines = text.split('\n')
    cleaned_lines = []
    redundant_pattern = re.compile(r'^[\*\#\s]*(Brief Answer|Discussion)[\*\#\s]*$', re.IGNORECASE)
    for line in lines:
        if redundant_pattern.match(line.strip()): continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)

# --- WORD DOC GENERATOR ---
def create_docx(content: str) -> BytesIO:
    doc = Document()
    title = doc.add_heading('Legal Research Memo', 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(f"Generated by AdLitem Pro | {time.strftime('%B %d, %Y')}")
    doc.add_paragraph("__________________________________________________________________")

    content = enforce_citations(content)
    content = strip_redundant_headers(content)

    parts = content.split("===SECTION_BREAK===")
    full_text_lines = []
    
    if len(parts) > 1:
        full_text_lines.append("BRIEF ANSWER")
        full_text_lines.extend(parts[0].split('\n'))
        full_text_lines.append("") 
        full_text_lines.append("DISCUSSION")
        full_text_lines.extend(parts[1].split('\n'))
    else:
        full_text_lines = content.split('\n')

    header_re = re.compile(r'<div class="memo-header">(.*?)</div>', re.IGNORECASE)
    citation_re = re.compile(r'<span class="inline-citation">(.*?)</span>', re.IGNORECASE)
    bold_re = re.compile(r'\*\*(.*?)\*\*')
    
    for line in full_text_lines:
        line = line.strip()
        if not line: continue
            
        if header_re.search(line):
            doc.add_heading(header_re.search(line).group(1), level=1)
            continue
        if line in ["BRIEF ANSWER", "DISCUSSION"]:
            doc.add_heading(line, level=1)
            continue

        clean_line = citation_re.sub(r'\1', line) 
        clean_line = clean_plain_text(clean_line)
        
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        segments = bold_re.split(clean_line)
        for i, segment in enumerate(segments):
            if not segment: continue
            run = p.add_run(segment)
            if i % 2 == 1: run.bold = True

    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer

def render_memo_ui(content: str, key_idx: int):
    content = strip_redundant_headers(content)
    content = enforce_citations(content)
    
    if "===SECTION_BREAK===" in content:
        parts = content.split("===SECTION_BREAK===")
        brief = parts[0].strip()
        disc = "".join(parts[1:]).strip()
        st.markdown(f'<div class="memo-container"><div class="brief-answer">{brief}</div><div class="discussion-box">{disc}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="memo-container"><div class="discussion-box">{content}</div></div>', unsafe_allow_html=True)

    doc_file = create_docx(content)
    st.download_button(
        label="📄 Download Memo as Word Doc",
        data=doc_file,
        file_name=f"Legal_Memo_{key_idx}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        key=f"dl_btn_{key_idx}"
    )

# --- UPDATED: BADGE LABEL LOGIC ---
def get_badge_label(metadata):
    dtype = metadata.get("type", "case").lower() # Normalize to lowercase
    
    if "policy" in dtype: return "DCF POLICY"
    if "statute" in dtype: return "STATUTE"
    if "code" in dtype: return "ADMIN CODE"
    if "manual" in dtype: return "CIC MANUAL"
    if "reference" in dtype: return "REFERENCE"
    
    return "PUBLISHED" if metadata.get("is_published") else "UNPUBLISHED"

# --- HISTORY CONTEXT ---
def rewrite_query(original_input, chat_history):
    if not chat_history: return original_input
    llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=0.3)
    prompt = f"Chat History:\n{chat_history}\n\nFollow-Up Input: {original_input}\n\nStandalone Question:"
    return llm.invoke(prompt).content

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
        search_kwargs={"k": 20}
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
    st.markdown("""<div style="text-align: center; margin-bottom: 40px;"><span style="font-family: 'Helvetica Neue', sans-serif; font-size: 0.85rem; color: #64748B; letter-spacing: 0.1em; text-transform: uppercase;">Created by <span style="color: #E2E8F0; font-weight: 600;">Ernest Anemone, Esq.</span></span></div>""", unsafe_allow_html=True)
    st.markdown("""<div style="text-align: center; color: #94A3B8; margin-bottom: 30px; font-size: 0.9rem;">Select a sample fact pattern to test the research engine:</div>""", unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)
    def set_prompt(text): st.session_state.messages.append({"role": "user", "content": text})
    
    with col1:
        if st.button("🌿 Cannabis & 'Imminent Harm'", use_container_width=True):
            set_prompt("Analyze the impact of the NJ CREAMMA Act on Title 9 litigation. Specifically: Does a positive newborn toxicology screen for THC, absent evidence of actual parenting impairment, legally sustain a finding of abuse or neglect? Cite recent Appellate Division precedents.")
            st.rerun()
    with col2:
        if st.button("🚧 'Safety Plans' & Due Process", use_container_width=True):
            set_prompt("Analyze the legality of DCPP 'Safety Protection Plans' that require a parent to leave the home under threat of removal. Does this constitute a 'constructive removal' requiring a Dodd hearing?")
            st.rerun()
    with col3:
        if st.button("👨‍👩‍👧 KLG vs. Adoption Preference", use_container_width=True):
            set_prompt("If a resource parent unequivocally prefers adoption, can the court still grant Kinship Legal Guardianship (KLG) to avoid Termination of Parental Rights (TPR)? Analyze the 'clear and convincing' evidence standard under the KLG Act amendments.")
            st.rerun()

# Render History
for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
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
                chat_history_str = "\n".join([f"{m['role']}: {m['content']}" for m in st.session_state.messages[:-1]])
                search_query = rewrite_query(current_prompt, chat_history_str)
                
                try:
                    progress_bar.progress(20, text="Executing stratified retrieval...")
                    
                    # --- UPDATED: STRATIFIED RETRIEVAL STRATEGY ---
                    # We now explicitly target tags found in your database (reference, manual, code)
                    
                    # 1. Cases (Broad & Specific)
                    docs_cases = retriever.invoke(f"{search_query} case law appellate division precedent")
                    
                    # 2. Statutes/Codes (Targets 'code', 'statute' tags)
                    docs_statutes = retriever.invoke(f"{search_query} N.J.S.A. N.J.A.C. statute administrative code")
                    
                    # 3. Policy/Manuals (Targets 'policy', 'manual', 'reference' tags)
                    docs_policy = retriever.invoke(f"{search_query} DCF Policy CP&P CIC Manual internal procedures reference")
                    
                    # --- MERGE & DEDUPLICATE ---
                    unique_docs = []
                    seen_hashes = set()

                    def add_docs_to_context(doc_list, limit):
                        count = 0
                        for d in doc_list:
                            if count >= limit: break
                            h = hash(d.page_content[:150])
                            if h not in seen_hashes:
                                seen_hashes.add(h)
                                unique_docs.append(d)
                                count += 1
                    
                    add_docs_to_context(docs_cases, 10)
                    add_docs_to_context(docs_statutes, 8)
                    add_docs_to_context(docs_policy, 8)
                    
                    context_blocks = []
                    st.session_state.last_sources = []
                    citation_map = {}

                    progress_bar.progress(50, text="Sanitizing authorities...")
                    for i, doc in enumerate(unique_docs):
                        meta = doc.metadata
                        
                        # Use bluebook if available, otherwise try display_name, then source
                        cite_str = clean_plain_text(meta.get("bluebook", ""))
                        if not cite_str:
                            cite_str = clean_plain_text(meta.get("display_name", meta.get("source", "Unknown Authority")))
                        
                        # Clean up formatting for non-bluebook entries
                        if ".pdf" in cite_str.lower() or ".txt" in cite_str.lower():
                            # Remove file extension for cleaner display
                            cite_str = re.sub(r'\.(pdf|txt)$', '', cite_str, flags=re.IGNORECASE)
                            cite_str = cite_str.replace("_", " ") # "Division_Procedure" -> "Division Procedure"
                        
                        content = clean_plain_text(doc.page_content)
                        title = clean_plain_text(meta.get("display_name", "Authority"))
                        context_blocks.append(f"SOURCE {i+1} [{meta.get('type','case').upper()} - {cite_str}]:\n{content}\n")
                        citation_map[i+1] = cite_str
                        
                        link = None
                        if meta.get("type", "case") == "case":
                            docket = meta.get("docket", "")
                            if not docket:
                                m = re.search(r'No\.\s*([\w-]+)', cite_str)
                                docket = m.group(1) if m else cite_str
                            link = f"https://scholar.google.com/scholar?q={urllib.parse.quote(docket)}"
                            
                        st.session_state.last_sources.append({"label": get_badge_label(meta), "title": title, "cite": cite_str, "snippet": content[:350], "link": link})
                        
                    status.update(label=f"Found {len(st.session_state.last_sources)} authorities.", state="complete")
                    progress_bar.progress(70, text="Drafting Research Memo...")

                    # --- SYSTEM PROMPT ---
                    llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=TEMPERATURE)
                    sys_prompt = """You are a Senior Legal Research Attorney. Write a formal Research Memo based ONLY on provided SOURCES.

HERMENEUTIC REASONING RULE: 
Before drafting, interpret the query within the broader context of New Jersey child welfare law. 
If a specific fact pattern (e.g., age, specific placement type) is not an exact match, do not lead with a 'no sources found' disclaimer. 
Instead, extract and apply the underlying legal principles, standards of proof, and judicial reasoning from the sources that are most analogous to the user's issue.

STRICT FORMATTING RULES:
1. No memo headers. Start at 'Question Presented'.
2. Wrap all main section headers in <div class="memo-header">HEADER TEXT</div>.
3. For claims, use inline citations: <span class="inline-citation">Bluebook Cite</span>.
4. STRICT BLUEBOOK CITATIONS: Refer to statutes as 'N.J.S.A.' and administrative code as 'N.J.A.C.' (always with periods).

CITATION FORMATTING FOR UNPUBLISHED CASES:
- You must cite unpublished cases in this EXACT format:
  "[Case Name], [Docket No.] (unpublished) (App. Div. [Year])"
- CRITICAL: If the unpublished case relies on a specific published precedent to make its ruling, you MUST append "(citing [Published Case Name])" to the citation.
- Example: "DCPP v. A.B., No. A-1234-20 (unpublished) (App. Div. 2022) (citing N.J. Div. of Youth & Family Servs. v. I.S.)."

6. Use '===SECTION_BREAK===' ONLY once, after 'Brief Answer'."""
                    
                    chain = ChatPromptTemplate.from_messages([("system", sys_prompt), ("user", "CITATIONS: {citations}\n\nCONTEXT: {context}\n\nISSUE: {input}")]) | llm | StrOutputParser()
                    response = chain.invoke({"input": search_query, "context": "\n\n".join(context_blocks), "citations": citation_map})
                    
                    clean_resp = clean_llm_output(response)
                    progress_bar.progress(100, text="Memo Complete.")
                    time.sleep(0.5)
                    progress_bar.empty()
                    st.session_state.messages.append({"role": "assistant", "content": clean_resp})
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error: {e}")
                    progress_bar.empty()

# Footer
if st.session_state.last_sources:
    st.markdown("---")
    with st.expander("📚 View Authority Library", expanded=False):
        for src in st.session_state.last_sources:
            link = f'<a href="{src["link"]}" target="_blank" class="scholar-link-inline">View on Scholar ↗</a>' if src["link"] else ""
            st.markdown(f'<div class="auth-item"><div class="auth-label">{src["label"]}{link}</div><div class="auth-title">{src["title"]}</div><div class="auth-cite">{src["cite"]}</div><div class="auth-snippet">"{src["snippet"]}..."</div></div>', unsafe_allow_html=True)
    if st.button("🗑️ Clear Research Session", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_sources = []
        st.rerun()