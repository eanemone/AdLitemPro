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
TEMPERATURE = 0.1  # Precision-first for legal formatting

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AdLitemPro")

# --- UI SETUP ---
st.set_page_config(page_title="AdLitem Pro", layout="wide", page_icon="⚖️")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .stApp { max-width: 1100px; margin: 0 auto; }
    .main-header { font-family: 'Helvetica Neue', sans-serif; font-size: 2.8rem; color: #FFFFFF; font-weight: 800; text-align: center; margin-bottom: 0.2rem; }
    .subtitle { font-size: 0.95rem; color: #94A3B8; text-align: center; margin-bottom: 2rem; font-weight: 400; letter-spacing: 0.05em; }
    input:focus, textarea:focus { border-color: #38BDF8 !important; box-shadow: 0 0 0 1px #38BDF8 !important; }
    .memo-container { background: #FFFFFF; border-radius: 8px; border: 1px solid #E2E8F0; overflow: hidden; margin-bottom: 1.5rem; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
    .brief-answer { background-color: #F8FAFC; color: #0F172A; padding: 24px; border-bottom: 2px solid #38BDF8; font-family: 'Georgia', serif; font-size: 1.05rem; }
    .discussion-box { background-color: #FFFFFF; color: #1E293B; padding: 32px; font-family: 'Georgia', serif; font-size: 1.1rem; line-height: 1.8; }
    .inline-citation { color: #0284c7; font-weight: 700; font-size: 0.95em; }
    .auth-item { border-left: 4px solid #38BDF8; background: #1E293B; padding: 15px; margin-bottom: 12px; border-radius: 0 4px 4px 0; }
</style>
""", unsafe_allow_html=True)

# --- UTILITIES & BRIDGE EXTRACTION ---
def extract_internal_citations(text: str) -> str:
    """Scans text for published N.J. citations to create a factual bridge for the LLM."""
    pattern = r'\d+\s+N\.J\.(?:\s+Super\.)?\s+\d+'
    matches = re.findall(pattern, text)
    return ", ".join(list(set(matches))) if matches else "None detected in this segment."

def clean_plain_text(text: str) -> str:
    if not text: return ""
    clean = re.compile('<.*?>')
    return re.sub(clean, '', str(text)).strip()

def clean_llm_output(text: str) -> str:
    text = re.sub(r'^```html\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

def enforce_citations(text: str) -> str:
    # 1. REMOVE PARENTHESES FROM STANDALONE CITES
    text = re.sub(r'([a-z])\s*\(([^)]*?(?:N\.J\.|N\.J\.S\.A\.|N\.J\.A\.C\.|No\. A-).*?)\)[\.\s]*$', "\\1. \\2", text, flags=re.MULTILINE|re.IGNORECASE)
    
    # 2. APPLY NON-BREAKING SPACES (\u00A0)
    text = re.sub(r'(\bN\.?J\.?[SA]\.?[AC]\.?)\s+([\d\w:\-\.]+)', "\\1\u00A0\\2", text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+)\s+(N\.J\.|Super\.)\s+(\d+)', "\\1\u00A0\\2\u00A0\\3", text, flags=re.IGNORECASE)

    # 3. BLUE HIGHLIGHTING (Including 'citing' bridges)
    statute_pattern = r'(?<!class="inline-citation">)\b(N\.J\.A\.C\.|N\.J\.S\.A\.)\s*(\d+[:\-][\d\-\.\w]+)'
    text = re.sub(statute_pattern, '<span class="inline-citation">\\1 \\2</span>', text, flags=re.IGNORECASE)
    
    case_pattern = r'(?<!class="inline-citation">)((?:\*[^*]+?\*,\s+)?(?:\d+[\u00A0\s]+N\.J\.|No\.\s+A-)[\d\w\-\.\u00A0\s,]+(?:\s*\([^)]+\))?(?:\s*\(citing.*?\))?)'
    text = re.sub(case_pattern, '<span class="inline-citation">\\1</span>', text, flags=re.IGNORECASE)
    
    return text

# --- CORE RETRIEVAL LOGIC ---
def rewrite_query_hermeneutic(original_input, chat_history):
    """Refining query through the iterative clarification/hermeneutic circle."""
    if not chat_history: return original_input
    llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=0.3)
    prompt = f"Chat History:\n{chat_history}\n\nFollow-Up Input: {original_input}\n\nRefine this task based on the hermeneutic circle of understanding—bridge the user's specific intent with the broader legal context established in history. Standalone Task:"
    return llm.invoke(prompt).content

@st.cache_resource
def get_retriever():
    if not os.path.exists(ST_DB_PATH): return None
    vectorstore = Chroma(collection_name=COLLECTION_NAME, embedding_function=OpenAIEmbeddings(), persist_directory=ST_DB_PATH)
    fs = LocalFileStore(DOC_STORE_PATH)
    store = create_kv_docstore(fs)
    pdr = ParentDocumentRetriever(
        vectorstore=vectorstore, docstore=store, 
        child_splitter=RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100),
        search_kwargs={"k": 20}
    )
    if os.path.exists(BM25_PATH):
        with open(BM25_PATH, "rb") as f:
            bm25 = pickle.load(f)
            return EnsembleRetriever(retrievers=[pdr, bm25], weights=[0.7, 0.3])
    return pdr

# --- UI GATING & APP START ---
# (check_password omitted for brevity, assuming existing logic)

if "messages" not in st.session_state: st.session_state.messages = []
if "last_sources" not in st.session_state: st.session_state.last_sources = []

# (Landing page logic omitted for brevity)

# Execution
if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
    current_prompt = st.session_state.messages[-1]["content"]
    with st.chat_message("assistant"):
        retriever = get_retriever()
        if not retriever:
            st.error("Database not found.")
        else:
            progress_bar = st.progress(0, text="Iterative clarification...")
            chat_history_str = "\n".join([f"{m['role']}: {m['content']}" for m in st.session_state.messages[:-1]])
            search_query = rewrite_query_hermeneutic(current_prompt, chat_history_str)
            
            with st.status("Conducting Deep Research...", expanded=False) as status:
                # Stratified Retrieval
                docs_cases = retriever.invoke(f"{search_query} case law precedent")
                docs_statutes = retriever.invoke(f"{search_query} N.J.S.A. N.J.A.C.")
                
                unique_docs = docs_cases[:10] + docs_statutes[:5]
                context_blocks = []
                
                for i, doc in enumerate(unique_docs):
                    content = clean_plain_text(doc.page_content)
                    found_bridges = extract_internal_citations(content)
                    cite_str = doc.metadata.get("bluebook", doc.metadata.get("display_name", "Authority"))
                    
                    context_blocks.append(
                        f"SOURCE {i+1} [{cite_str}]:\n"
                        f"INTERNAL AUTHORITIES CITED: {found_bridges}\n"
                        f"CONTENT: {content}\n"
                    )
                    st.session_state.last_sources.append({"label": "RESEARCH", "title": cite_str, "cite": cite_str, "snippet": content[:300]})

                # --- UPGRADED SYSTEM PROMPT (FEW-SHOT BRIDGE) ---
                llm = ChatOpenAI(model=PREFERRED_MODEL, temperature=TEMPERATURE)
                sys_prompt = """You are a Senior Legal Research Attorney. Write a Memo based ONLY on provided SOURCES.

STRICT BRIDGE CITATION RULE:
Every unpublished case (No. A-XXXX) MUST be used to bridge to the published precedent it relies on.
- FORMAT: *Unpublished Case*, No. A-XXXX (App. Div. Date) (citing *Published Case*, Vol N.J. Page (Year)).

BLUEBOOK PERFECTION:
1. NO PARENTHESES for standalone citations at end of sentences.
   - RIGHT: The court affirmed the decision. *State v. Smith*, 123 N.J. 456 (2010).
2. Use 'N.J.S.A.' and 'N.J.A.C.' (with periods).
3. Use '===SECTION_BREAK===' only once after Brief Answer.

EXAMPLE:
"Removal requires a showing of imminent risk. *N.J. Div. of Child Prot. & Permanency v. S.M.*, No. A-1234-22 (App. Div. 2023)(unpublished)(citing *N.J. Div. of Youth & Fam. Servs. v. I.S.*, 202 N.J. 145 (2010))."
"""
                chain = ChatPromptTemplate.from_messages([("system", sys_prompt), ("user", "CONTEXT: {context}\n\nISSUE: {input}")]) | llm | StrOutputParser()
                response = chain.invoke({"input": search_query, "context": "\n\n".join(context_blocks)})
                
                progress_bar.progress(100)
                st.session_state.messages.append({"role": "assistant", "content": clean_llm_output(response)})
                st.rerun()

# (render_memo_ui and create_docx functions remain same as provided in first snippet)