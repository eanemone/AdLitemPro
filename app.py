import streamlit as st
import os
import logging
import urllib.parse
import pickle
import re
import time
from io import BytesIO
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain.retrievers import ParentDocumentRetriever, EnsembleRetriever
from langchain.storage import LocalFileStore
from langchain.storage._lc_store import create_kv_docstore
from langchain_community.retrievers import BM25Retriever
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
TEMPERATURE = 0.3 

# --- HEADER CLEANER ---
def clean_markdown_headers(text: str) -> str:
    headers = ["QUESTION PRESENTED", "BRIEF ANSWER", "DISCUSSION", "ANALYSIS", "CONCLUSION"]
    for header in headers:
        pattern = re.compile(r'[\#\*\_]+' + re.escape(header) + r'[\#\*\_]*', re.IGNORECASE)
        text = re.sub(pattern, header, text)
    return text

# --- CITATION PERFECTION ENGINE (V7 - CRASH-PROOF) ---
def enforce_citations(text: str) -> str:
    text = re.sub(r'\[Source \d+\]', '', text, flags=re.IGNORECASE)
    
    # 1. STANDALONE CITES (Parenthesis Stripper)
    text = re.sub(r'([a-z])\s*\(([^)]*?(?:N\.J\.|N\.J\.S\.A\.|N\.J\.A\.C\.|No\. A-).*?)\)[\.\s]*$', "\\1. \\2", text, flags=re.MULTILINE|re.IGNORECASE)

    # 2. NON-BREAKING SPACE WELDS (Fixed Escaping)
    # Statute Welds
    text = re.sub(r'(N\.J\.S\.A\.|N\.J\.A\.C\.)\s+([\d\w:\-\.]+)', "\\1\u00A0\\2", text, flags=re.IGNORECASE)
    # Reporter Welds
    text = re.sub(r'(\d+)\s+(N\.J\.|Super\.)\s+(\d+)', "\\1\u00A0\\2\u00A0\\3", text, flags=re.IGNORECASE)
    # Caption Welds
    text = re.sub(r'(\bv\.)\s+', "\\1\u00A0", text, flags=re.IGNORECASE)
    text = re.sub(r'(in\s+re)\s+', "in\u00A0re\u00A0", text, flags=re.IGNORECASE)

    # 3. BLUE HIGHLIGHTING (Full Block Capture)
    # Published
    case_pub_pattern = r'(?<!class="inline-citation">)((?:\*[^*]+?\*,\s+)?\d+[\u00A0\s]+N\.J\.(?:[\u00A0\s]+Super\.)?[\u00A0\s]+\d+(?:\s*\(citing.*?\))?)'
    text = re.sub(case_pub_pattern, '<span class="inline-citation">\\1</span>', text, flags=re.IGNORECASE)
    # Unpublished
    docket_pattern = r'(?<!class="inline-citation">)((?:\*[^*]+?\*,\s+)?No\.\s+A-[\d\w-]+(?:\s*\([^)]+\))?(?:\s*\(citing.*?\))?)'
    text = re.sub(docket_pattern, '<span class="inline-citation">\\1</span>', text, flags=re.IGNORECASE)
    
    return text