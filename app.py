import streamlit as st
import os
import logging
import urllib.parse
import pickle
import tiktoken
import re
import time
from io import BytesIO

# --- WORD DOC GENERATION ---
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
TEMPERATURE = 0.2 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AdLitemPro")

# --- UI SETUP ---
st.set_page_config(page_title="AdLitem Pro", layout="wide", page_icon="⚖️")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { max-width: 1100px; margin: 0 auto; }
    .main-header { font-family: 'Helvetica Neue', sans-serif; font-size: 2.8rem; color: #FFFFFF; font-weight: 800; text-align: center; margin-bottom: 0.2rem; }
    .subtitle { font-size: 0.95rem; color: #94A3B8; text-align: center; margin-bottom: 2rem; font-weight: 400; letter-spacing: 0.05em; text-transform: uppercase; }
    
    /* INPUT BOX HIGHLIGHTS */
    div[data-baseweb="input"]:focus-within { border-color: #38BDF8 !important; box-shadow: 0 0 0 1px #38BDF8 !important; }
    .stButton button { border-color: #38BDF8 !important; color: #38BDF8 !important; }

    /* MEMO STYLES */
    .memo-container { background: #FFFFFF; border-radius: 8px; border: 1px solid #E2E8F0; overflow: hidden; margin-bottom: 1.5rem; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
    .brief-answer { background-color: #F8FAFC; color: #0F172A; padding: 24px; border-bottom: 2px solid #38BDF8; font-family: 'Georgia', serif; font-size: 1.05rem; line-height: 1.6; }
    .discussion-box { background-color: #FFFFFF; color: #1E293B; padding: 32px; font-family: 'Georgia', serif; font-size: 1.1rem; line-height: 1.8; }
    .inline-citation { color: #0284c7; font-weight: 700; font-size: 0.95em; }
    
    /* SUGGESTION CARDS */
    .card-tag { font-size: 0.65rem; font-weight: 800; color: #38BDF8; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 0.05em; text-align: center; }
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
            del st.session_state["password"]; del st.session_state["username"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state["password_correct"]: return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown('<div style="margin-top: 80px;"></div>', unsafe_allow_html=True)
        st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
        st.markdown('<div style="text-align: center; margin-bottom: 15px;"><span style="color: #E2E8F0; font-weight: 600;">Ernest Anemone, Esq.</span></div>', unsafe_allow_html=True)
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", on_change=password_entered, key="password")
        if st.session_state["login_attempted"] and not st.session_state["password_correct"]:
            st.error("😕 Access Denied")
    return False

if not check_password(): st.stop()

# --- UTILITIES ---
def clean_plain_text(text: str) -> str:
    if not text: return ""
    return re.sub(re.compile('<.*?>'), '', str(text)).strip()

def enforce_citations(text: str) -> str:
    # Remove standalone parentheses from citations
    text = re.sub(r'([a-z])\s*\(([^)]*?(?:N\.J\.|N\.J\.S\.A\.|N\.J\.A\.C\.|No\. A-).*?)\)[\.\s]*$', "\\1. \\2", text, flags=re.MULTILINE|re.IGNORECASE)
    # Highlight Statutes & Bridges
    stat_pat = r'(?<!class="inline-citation">)\b(N\.J\.A\.C\.|N\.J\.S\.A\.)\s*(\d+[:\-][\d\-\.\w]+)'
    text = re.sub(stat_pat, r'<span class="inline-citation">\1 \2</span>', text, flags=re.IGNORECASE)
    case_pat = r'(?<!class="inline-citation">)((?:\*[^*]+?\*,\s+)?(?:\d+[\u00A0\s]+N\.J\.|No\.\s+A-)[\d\w\-\.\u00A0\s,]+(?:\s*\([^)]+\))?(?:\s*\(citing.*?\))?)'
    text = re.sub(case_pat, r'<span class="inline-citation">\1</span>', text, flags=re.IGNORECASE)
    return text

# --- WORD DOC GENERATOR ---
def create_docx(content: str) -> BytesIO:
    doc = Document()
    doc.add_heading('Legal Research Memo', 0).alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(f"Generated by AdLitem Pro | {time.strftime('%B %d, %Y')}")
    clean_content = enforce_citations(content)
    for line in clean_content.split('\n'):
        if any(h in line.upper() for h in ["QUESTION PRESENTED", "BRIEF ANSWER", "DISCUSSION"]):
            doc.add_heading(line.strip(), level=1)
        else:
            p = doc.add_paragraph(clean_plain_text(line))
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    buffer = BytesIO(); doc.save(buffer); buffer.seek(0)
    return buffer

def render_memo_ui(content: str, key_idx: int):
    formatted = enforce_citations(content)
    if "===SECTION_BREAK===" in formatted:
        parts = formatted.split("===SECTION_BREAK===")
        st.markdown(f'<div class="memo-container"><div class="brief-answer">{parts[0]}</div><div class="discussion-box">{"".join(parts[1:])}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="memo-container"><div class="discussion-box">{formatted}</div></div>', unsafe_allow_html=True)
    st.download_button("📄 Download Memo", create_docx(content), file_name=f"Memo_{key_idx}.docx", key=f"dl_{key_idx}")

# --- DATABASE LOADING ---
@st.cache_resource
def get_retriever():
    if not os.path.exists(ST_DB_PATH): return None
    vectorstore = Chroma(collection_name=COLLECTION_NAME, embedding_function=OpenAIEmbeddings(), persist_directory=ST_DB_PATH)
    fs = LocalFileStore(DOC_STORE_PATH); store = create_kv_docstore(fs)
    pdr = ParentDocumentRetriever(vectorstore=vectorstore, docstore=store, child_splitter=RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=125), search_kwargs={"k": 20})
    if os.path.exists(BM25_PATH):
        with open(BM25_PATH, "rb") as f:
            bm25 = pickle.load(f)
            return EnsembleRetriever(retrievers=[pdr, bm25], weights=[0.7, 0.3])
    return pdr

# --- UI EXECUTION ---
st.markdown('<div class="main-header">AdLitem<span style="color:#38BDF8">Pro</span></div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">NEW JERSEY CHILD WELFARE LAW RESEARCH ENGINE</div>', unsafe_allow_html=True)

if "messages" not in st.session_state: st.session_state.messages = []
if "last_sources" not in st.session_state: st.session_state.last_sources = []

# --- LANDING PAGE SUGGESTION CARDS ---
if not st.session_state.messages:
    st.markdown('<div style="height: 2vh;"></div>', unsafe_allow_html=True)
    def set_prompt(text): st.session_state.messages.append({"role": "user", "content": text}); st.rerun()
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown('<div class="card-tag">Title 9 / Neglect</div>', unsafe_allow_html=True)
        if st.button("🌿 Cannabis & Imminent Harm", use_container_width=True):
            set_prompt("Analyze if a positive newborn toxicology for THC, absent evidence of parenting impairment, legally sustains a finding of abuse or neglect under Title 9. Cite CREAMMA and recent precedents.")
    with col2:
        st.markdown('<div class="card-tag">TPR / KLG</div>', unsafe_allow_html=True)
        if st.button("👨‍👩‍👧 KLG vs. Adoption", use_container_width=True):
            set_prompt("Analyze the 'clear and convincing' standard for KLG under the 2021 amendments. If a resource parent prefers adoption but is open to KLG, can the court grant KLG over DCPP's objection?")
    with col3:
        st.markdown('<div class="card-tag">Due Process</div>', unsafe_allow_html=True)
        if st.button("🚧 Constructive Removal", use_container_width=True):
            set_prompt("Analyze DCPP 'Safety Protection Plans' that require a parent to leave the home. Does this constitute a 'constructive removal' requiring a Dodd hearing under current NJ Law?")

for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant": render_memo_ui(msg["content"], idx)
        else: st.markdown(msg["content"])

if prompt := st.chat_input("Start research task..."):
    st.session_state.messages.append({"role": "user", "content": prompt}); st.rerun()

if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    current_prompt = st.session_state.messages[-1]["content"]
    with st.chat_message("assistant"):
        retriever = get_retriever()
        with st.status("Analyzing Legal Graph...") as status:
            docs = retriever.invoke(current_prompt)
            context_blocks = []
            for i, doc in enumerate(docs):
                meta = doc.metadata; content = clean_plain_text(doc.page_content)
                bridge = meta.get("internal_cites", "N/A"); cite = meta.get("bluebook", "Authority")
                context_blocks.append(f"SOURCE {i+1} [{cite}]:\nPRE-EXTRACTED BRIDGE AUTHORITIES: {bridge}\nTEXT CONTENT: {content}\n")
                st.session_state.last_sources.append({"label": "RESEARCH", "title": cite, "cite": cite, "snippet": content[:300]})
            
            sys_prompt = """You are a Senior Legal Research Attorney. Write a Research Memo based ONLY on provided SOURCES.
            HERMENEUTIC REASONING: Interpret the query through the circle of understanding—bridge specific facts with the 'PRE-EXTRACTED BRIDGE AUTHORITIES' provided.
            RULES: 
            1. Every unpublished case (No. A-XXXX) MUST bridge to a published precedent. 
            2. FORMAT: *Name*, No. A-XXXX (Date) (citing *Name*, Vol N.J. Page (Year)).
            3. NO PARENTHESES for standalone citations.
            4. Use '===SECTION_BREAK===' only once after the Brief Answer."""

            chain = ChatPromptTemplate.from_messages([("system", sys_prompt), ("user", "CONTEXT: {context}\n\nISSUE: {input}")]) | ChatOpenAI(model=PREFERRED_MODEL, temperature=TEMPERATURE) | StrOutputParser()
            res = chain.invoke({"input": current_prompt, "context": "\n\n".join(context_blocks)})
            st.session_state.messages.append({"role": "assistant", "content": res}); st.rerun()