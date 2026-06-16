"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import mermaid from "mermaid";

const getModelStatusEmoji = (modelName: string, statuses?: Record<string, string>) => {
  if (!statuses) return "";
  const s = statuses[modelName];
  if (s === "ok") return "🟢 ";
  if (s === "error") return "🔴 ";
  if (s === "testing") return "🟡 ";
  return "";
};

const API_BASE = "http://localhost:8000";
const WS_BASE  = "ws://localhost:8000";

// ─── Types ───────────────────────────────────────────────────────────────────
interface CodeBlock { language: string; code: string; }
interface ComparisonTable {
  caption: string | null;
  headers: string[];
  rows: string[][];
}
interface NoteSection {
  heading: string;
  points: string[];
  code_blocks?: CodeBlock[];
  comparison_table?: ComparisonTable | null;
}
interface PracticeQuestion {
  difficulty?: "basic" | "intermediate" | "advanced";
  question: string;
  answer: string;
}
interface KeyConcept {
  term: string;
  definition: string;
  example?: string;
}
interface FormulaRule {
  name: string;
  expression: string;
  variables?: string;
  when_to_use?: string;
}
interface NoteData {
  title: string;
  summary: string;
  tldr?: string;
  key_concepts?: KeyConcept[];
  sections: NoteSection[];
  formulas_and_rules?: FormulaRule[];
  practice_questions: PracticeQuestion[];
  common_mistakes?: string[];
  brainstorming_ideas: string[];
}
interface PageData { page_number: number; text: string; }
interface ImageProvider { id: string; name: string; description: string; models: string[]; free: boolean; requires_key: boolean; }
interface ServerResponse {
  notes: NoteData; mind_map: string; infographic: string;
  image_pending?: boolean;
  _meta?: { provider: string; model: string };
}
interface NoteState {
  status: "idle" | "generating" | "success" | "error";
  data?: ServerResponse; errorMsg?: string;
}

// ─── Safe rendering helpers ───────────────────────────────────────────────────
const asString = (v: unknown): string => {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (o.text) return asString(o.text);
    if (o.content) return asString(o.content);
    if (o.title && o.points) {
      const pts = Array.isArray(o.points)
        ? (o.points as unknown[]).map(asString).join("; ")
        : "";
      return `${asString(o.title)}${pts ? ": " + pts : ""}`;
    }
    if (o.title) return asString(o.title);
    if (o.name) return asString(o.name);
    if (o.description) return asString(o.description);
    if (o.idea) return asString(o.idea);
    if (o.application) return asString(o.application);
    try { return JSON.stringify(v); } catch { return "[object]"; }
  }
  return String(v);
};

const asStringArray = (v: unknown): string[] => {
  if (!v) return [];
  if (!Array.isArray(v)) return [asString(v)];
  return v.map(asString).filter(Boolean);
};

const normaliseSections = (sections: unknown): NoteSection[] => {
  if (!Array.isArray(sections)) return [];
  return sections.map((s: unknown) => {
    if (typeof s === "string") return { heading: s, points: [], code_blocks: [] };
    const o = s as Record<string, unknown>;
    return {
      heading: asString(o.heading || o.title || o.name || ""),
      points: asStringArray(o.points || o.items || o.bullets || []),
      code_blocks: Array.isArray(o.code_blocks) ? o.code_blocks as CodeBlock[] : [],
      comparison_table: (o.comparison_table as ComparisonTable | null) || null,
    };
  });
};

export function preprocessMermaidDiagram(raw: string): string {
  if (!raw) return "";
  let src = raw.trim();

  if (src.startsWith("```")) {
    const lines = src.split("\n");
    const s = lines[0].startsWith("```") ? 1 : 0;
    const e = lines[lines.length - 1].trim() === "```" ? lines.length - 1 : lines.length;
    src = lines.slice(s, e).join("\n").trim();
  }

  if (!src.startsWith("graph ") && !src.startsWith("flowchart ")) {
    src = "graph TD\n" + src;
  }

  src = src.replace(/→/g, "-->").replace(/—>/g, "-->").replace(/(?<![=-])->/g, "-->");

  const labelMap: Record<string, string> = {};
  let counter = 0;

  const toSafeId = (raw: string): string => {
    const key = raw.trim();
    if (!key) return `n${++counter}`;
    if (labelMap[key]) return labelMap[key];
    if (/^[A-Za-z][A-Za-z0-9_]*$/.test(key)) { labelMap[key] = key; return key; }
    let safe = key.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^([0-9])/, "n$1");
    if (!safe) safe = "n";
    safe = `${safe}_${++counter}`.substring(0, 32);
    labelMap[key] = safe;
    return safe;
  };

  const q = (txt: string) => `"${txt.trim().replace(/^["']|["']$/g, "").replace(/"/g, "'")}"`;

  const rewriteNode = (token: string): string => {
    token = token.trim();
    if (!token) return "";
    const m = token.match(/^([^([\]{}"'\s]+?)\s*((?:\[{1,2}|\({1,2}|\{{1,2})\s*["']?)(.+?)(?:["']?\s*(?:\]{1,2}|\){1,2}|\}{1,2}))$/s);
    if (m) {
      const open  = m[2].replace(/["']/g, "").trimEnd();
      const label = m[3].replace(/^["']|["']$/g, "").trim();
      const close = open === "[[" ? "]]" : open === "((" ? "))" : open === "{{" ? "}}" : open === "([" ? "])" : open === "[" ? "]" : open === "(" ? ")" : open === "{" ? "}" : "]";
      return `${toSafeId(m[1])}${open}${q(label)}${close}`;
    }
    return toSafeId(token);
  };

  const out: string[] = [];
  for (const line of src.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("graph ") || t.startsWith("flowchart ") || t.startsWith("subgraph ")
        || t === "end" || t.startsWith("%%") || t.startsWith("style ")
        || t.startsWith("classDef ") || t.startsWith("class ") || t.startsWith("linkStyle ")) {
      out.push(line); continue;
    }
    if (t.includes("-->")) {
      const am = t.match(/^(.+?)\s*(-->[|][^|]*[|]|-->)\s*(.+)$/s);
      if (am) { out.push(`  ${rewriteNode(am[1])} ${am[2].trim()} ${rewriteNode(am[3])}`); continue; }
    }
    out.push(`  ${rewriteNode(t)}`);
  }

  const injections = Object.entries(labelMap)
    .filter(([orig, safe]) => orig !== safe)
    .map(([orig, safe]) => `  ${safe}[${q(orig)}]`);
  if (injections.length > 0) {
    const idx = out.findIndex(l => l.trim().startsWith("graph ") || l.trim().startsWith("flowchart "));
    if (idx !== -1) out.splice(idx + 1, 0, ...injections);
  }
  return out.join("\n");
}

// ─── MermaidRenderer ─────────────────────────────────────────────────────────
function MermaidRenderer({ chart, id }: { chart: string; id: string }) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState(false);
  const [rawChart, setRawChart] = useState("");

  useEffect(() => {
    if (!chart) return;
    let active = true;
    setSvg(""); setError(false); setRawChart("");
    const render = async () => {
      try {
        const clean = preprocessMermaidDiagram(chart);
        setRawChart(clean);
        const uid = `mm-${id.replace(/[^a-zA-Z0-9]/g, "-")}-${Date.now()}`;
        const { svg: out } = await mermaid.render(uid, clean);
        if (active) { setSvg(out); setError(false); }
      } catch (err) {
        console.error("Mermaid render failed:", err);
        if (active) setError(true);
      }
    };
    const t = setTimeout(render, 80);
    return () => { active = false; clearTimeout(t); };
  }, [chart, id]);

  if (error) return (
    <div style={{ width: "100%", padding: "1.5rem" }}>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "#ef4444", fontWeight: 600, marginBottom: "1rem", textTransform: "uppercase" }}>⚠ Diagram Syntax Error</div>
      <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", fontFamily: "var(--font-mono)", fontSize: "0.75rem", lineHeight: 1.6, color: "var(--text-muted)", background: "var(--bg-code)", borderRadius: "6px", padding: "1rem", overflow: "auto", maxHeight: "280px", border: "1px solid var(--accent-muted)" }}>{rawChart || chart}</pre>
    </div>
  );

  if (!svg) return (
    <div className="generation-status"><div className="spinner" /><span>Rendering mind map…</span></div>
  );

  return <div style={{ width: "100%", overflowX: "auto", display: "flex", justifyContent: "center" }} dangerouslySetInnerHTML={{ __html: svg }} />;
}

// ─── InfographicRenderer ─────────────────────────────────────────────────────
function InfographicRenderer({ svgCode, pending }: { svgCode: string; pending?: boolean }) {
  const isImage = svgCode?.startsWith("data:image/") || svgCode?.startsWith("http");

  if (!svgCode) return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "0.75rem", padding: "3rem 2rem", fontFamily: "var(--font-mono)", fontSize: "0.75rem", color: "var(--text-muted)", minHeight: "300px" }}>
      {pending ? <><div className="spinner" /><span>Generating image in background…</span></> : <><span style={{ fontSize: "1.5rem", opacity: 0.4 }}>◻</span><span>No infographic available.</span></>}
    </div>
  );

  const handleDownload = () => {
    const a = document.createElement("a");
    a.href = svgCode; a.download = isImage ? "infographic.jpg" : "infographic.svg"; a.click();
  };

  return (
    <div className="infographic-container">
      <div style={{ position: "absolute", top: "0.6rem", right: "0.75rem", display: "flex", gap: "0.5rem", zIndex: 2 }}>
        {pending && <div className="spinner" style={{ width: "14px", height: "14px" }} />}
        <button onClick={handleDownload} style={{ background: "var(--bg-secondary)", border: "1px solid var(--accent-muted)", borderRadius: "4px", padding: "0.2rem 0.5rem", fontFamily: "var(--font-mono)", fontSize: "0.6rem", color: "var(--text-muted)", cursor: "pointer", fontWeight: 600 }}>↓ SAVE</button>
      </div>
      {isImage
        ? <div style={{ display: "flex", justifyContent: "center", width: "100%" }}><img src={svgCode} alt="AI Infographic" style={{ maxWidth: "100%", height: "auto" }} /></div>
        : <div style={{ width: "100%", display: "flex", justifyContent: "center" }} dangerouslySetInnerHTML={{ __html: svgCode }} />
      }
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────────
export default function Home() {
  const [pages, setPages] = useState<PageData[]>([]);
  const [documentId, setDocumentId] = useState<number | null>(null);
  const [modelChoices, setModelChoices] = useState<{
    cloud: Record<string, string[]>; local: string[];
    statuses?: Record<string, string>;
    pollinations_image_models?: string[];
    image_providers?: ImageProvider[];
  }>({ cloud: {}, local: [], statuses: {}, image_providers: [] });

  const [selectedModel, setSelectedModel]               = useState("");
  const [selectedProvider, setSelectedProvider]         = useState("");
  const [selectedImageModel, setSelectedImageModel]     = useState("");
  const [selectedImageProvider, setSelectedImageProvider] = useState("gemini_imagen");

  const [notes, setNotes]               = useState<Record<number, NoteState>>({});
  const [syncScroll, setSyncScroll]     = useState(true);
  const [useStreaming, setUseStreaming] = useState(true);
  const [activePage, setActivePage]     = useState(1);
  const [isUploading, setIsUploading]   = useState(false);
  const [uploadError, setUploadError]   = useState("");
  const [theme, setTheme]               = useState<"light" | "dark" | "sepia" | "nord">("dark");
  const [previousBooks, setPreviousBooks] = useState<any[]>([]);
  const [activeTabs, setActiveTabs]     = useState<Record<number, "notes" | "mindmap" | "infographic">>({});
  const [retryModels, setRetryModels]   = useState<Record<number, string>>({});
  const [openAccordions, setOpenAccordions] = useState<Record<string, boolean>>({});
  const [copyFeedback, setCopyFeedback] = useState<Record<string, boolean>>({});

  // ─── New States for Feature Extensions ─────────────────────────────────────
  // API Key Manager
  const [showKeyModal, setShowKeyModal] = useState(false);
  const [keysInput, setKeysInput] = useState({ gemini: "", groq: "", mistral: "" });
  const [keysMasked, setKeysMasked] = useState<{ gemini: string[], groq: string[], mistral: string[] }>({ gemini: [], groq: [], mistral: [] });
  const [probeStatus, setProbeStatus] = useState<Record<string, { valid?: boolean, message?: string, loading?: boolean }>>({});

  // Flashcards & Spaced Repetition
  const [showFlashcardPanel, setShowFlashcardPanel] = useState(false);
  const [flashcards, setFlashcards] = useState<any[]>([]);
  const [currentCardIndex, setCurrentCardIndex] = useState(0);
  const [showFlashcardAnswer, setShowFlashcardAnswer] = useState(false);
  const [isGeneratingCards, setIsGeneratingCards] = useState(false);
  const [dueOnlyCards, setDueOnlyCards] = useState(false);

  // Highlight & Explain Overlay
  const [selectedHighlightText, setSelectedHighlightText] = useState("");
  const [showHighlightMenu, setShowHighlightMenu] = useState(false);
  const [selectionCoords, setSelectionCoords] = useState({ x: 0, y: 0 });
  const [explanationResult, setExplanationResult] = useState<string | null>(null);
  const [explainingAction, setExplainingAction] = useState("");
  const [isExplaining, setIsExplaining] = useState(false);

  // Document RAG Chat
  const [showChatDrawer, setShowChatDrawer] = useState(false);
  const [chatSessionId, setChatSessionId] = useState<number | null>(null);
  const [chatMessages, setChatMessages] = useState<any[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [isChatTyping, setIsChatTyping] = useState(false);
  const [chatMeta, setChatMeta] = useState({ provider: "", model: "" });
  const [currentCitations, setCurrentCitations] = useState<number[]>([]);

  // Document Intelligence (Executive summary, concept indexes, prerequisites)
  const [showDocIntelModal, setShowDocIntelModal] = useState(false);
  const [docIntelData, setDocIntelData] = useState<any>(null);
  const [isGeneratingDocIntel, setIsGeneratingDocIntel] = useState(false);
  const [docIntelError, setDocIntelError] = useState("");

  // Multi-Document Synthesis
  const [showSynthesisModal, setShowSynthesisModal] = useState(false);
  const [selectedSynthDocs, setSelectedSynthDocs] = useState<number[]>([]);
  const [synthQuestion, setSynthQuestion] = useState("");
  const [synthAnswer, setSynthAnswer] = useState("");
  const [synthCitations, setSynthCitations] = useState<any[]>([]);
  const [isSynthesizing, setIsSynthesizing] = useState(false);
  const [synthMeta, setSynthMeta] = useState({ provider: "", model: "" });

  // Batch queue processing
  const [showBatchDrawer, setShowBatchDrawer] = useState(false);
  const [batchJobs, setBatchJobs] = useState<any[]>([]);

  const leftPaneRef  = useRef<HTMLDivElement>(null);
  const rightPaneRef = useRef<HTMLDivElement>(null);
  const observerRef  = useRef<IntersectionObserver | null>(null);

  // Mermaid init
  useEffect(() => {
    mermaid.initialize({
      startOnLoad: false, theme: "base", securityLevel: "loose", logLevel: 5,
      flowchart: { curve: "basis", useMaxWidth: true, htmlLabels: true, padding: 20 },
      themeVariables: {
        primaryColor: "#f4f4f5", primaryTextColor: "#18181b", primaryBorderColor: "#e4e4e7",
        lineColor: "#a1a1aa", secondaryColor: "#fafafa", tertiaryColor: "#ffffff",
        edgeLabelBackground: "#ffffff", clusterBkg: "#fafafa", titleColor: "#18181b", nodeTextColor: "#18181b"
      }
    });
  }, []);

  // Theme sync
  useEffect(() => {
    document.body.classList.remove("theme-light", "theme-dark", "theme-sepia", "theme-nord");
    document.body.classList.add(`theme-${theme}`);
  }, [theme]);

  // Sync default streaming state based on provider capability
  useEffect(() => {
    if (selectedProvider === "groq" || selectedProvider === "gemini" || selectedProvider === "ollama") {
      setUseStreaming(true);
    } else {
      setUseStreaming(false);
    }
  }, [selectedProvider]);

  // Sync default image model from Gemini list
  useEffect(() => {
    const g = modelChoices.cloud["gemini"] || [];
    if (!g.length) return;
    setSelectedImageModel(prev => {
      if (g.includes(prev)) return prev;
      return g.find(m => m.includes("banana")) || g.find(m => m.includes("imagen")) || g[0];
    });
  }, [modelChoices]);

  const fetchPreviousBooks = useCallback(async () => {
    try { const r = await fetch(`${API_BASE}/api/documents`); if (r.ok) setPreviousBooks(await r.json()); }
    catch (e) { console.error(e); }
  }, []);

  // Load models ONCE on mount
  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/models`);
        if (!r.ok) return;
        const data = await r.json();
        setModelChoices(data);
        return data;
      } catch { /* backend offline */ }
    };
    load().then(data => {
      if (!data) return;
      const provs = Object.keys(data.cloud || {});
      if (provs.length > 0) {
        const prov = provs[0];
        const text = (data.cloud[prov] as string[]).filter(m => !(prov === "gemini" && (m.includes("imagen") || m.includes("banana"))));
        setSelectedProvider(prov);
        setSelectedModel(text[0] || data.cloud[prov][0]);
      } else if (data.local?.length) {
        setSelectedProvider("ollama"); setSelectedModel(data.local[0]);
      }
    });
    fetchPreviousBooks();
    const iv = setInterval(load, 3600_000);
    return () => clearInterval(iv);
  }, [fetchPreviousBooks]);

  const handleModelChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const [prov, ...rest] = e.target.value.split("|");
    setSelectedProvider(prov); setSelectedModel(rest.join("|"));
  };

  // ─── New API Actions Helpers ───────────────────────────────────────────────
  // Keys Manager API
  const fetchKeys = async () => {
    try {
      const r = await fetch(`${API_BASE}/api/keys`);
      if (r.ok) {
        const data = await r.json();
        setKeysMasked(data);
      }
    } catch (e) { console.error(e); }
  };

  const handleSaveKeys = async () => {
    const parseKeysList = (str: string) => str.split(/\n|,/).map(k => k.trim()).filter(Boolean);
    const payload = {
      gemini: parseKeysList(keysInput.gemini),
      groq: parseKeysList(keysInput.groq),
      mistral: parseKeysList(keysInput.mistral)
    };
    try {
      const r = await fetch(`${API_BASE}/api/keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (r.ok) {
        alert("API keys saved and KeyManager reloaded!");
        fetchKeys();
        setShowKeyModal(false);
        const r2 = await fetch(`${API_BASE}/api/models`);
        if (r2.ok) setModelChoices(await r2.json());
      } else {
        alert("Failed to save keys.");
      }
    } catch (e) { alert("Error saving keys: " + e); }
  };

  const probeKey = async (provider: string, keyVal: string) => {
    const cleaned = keyVal.trim();
    if (!cleaned) {
      alert("Please enter a key to test first.");
      return;
    }
    setProbeStatus(prev => ({ ...prev, [provider]: { loading: true } }));
    try {
      const r = await fetch(`${API_BASE}/api/keys/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, key: cleaned })
      });
      const data = await r.json();
      setProbeStatus(prev => ({ ...prev, [provider]: { valid: data.valid, message: data.message } }));
    } catch (e: any) {
      setProbeStatus(prev => ({ ...prev, [provider]: { valid: false, message: e.message } }));
    }
  };

  // Flashcards API
  const fetchFlashcards = async (dueOnly = false) => {
    if (!documentId) return;
    try {
      const r = await fetch(`${API_BASE}/api/flashcards/${documentId}?due_only=${dueOnly}`);
      if (r.ok) {
        const data = await r.json();
        setFlashcards(data.cards || []);
        setCurrentCardIndex(0);
        setShowFlashcardAnswer(false);
      }
    } catch (e) { console.error(e); }
  };

  const handleGenerateFlashcards = async () => {
    if (!documentId) return;
    setIsGeneratingCards(true);
    try {
      const r = await fetch(`${API_BASE}/api/flashcards/generate/${documentId}`, { method: "POST" });
      if (r.ok) {
        const data = await r.json();
        alert(`Generated ${data.generated} flashcards! Skipped ${data.skipped} duplicates.`);
        fetchFlashcards(dueOnlyCards);
      }
    } catch (e) { console.error(e); }
    finally { setIsGeneratingCards(false); }
  };

  const handleReviewFlashcard = async (quality: number) => {
    if (!flashcards.length) return;
    const card = flashcards[currentCardIndex];
    try {
      const r = await fetch(`${API_BASE}/api/flashcards/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card_id: card.id, quality })
      });
      if (r.ok) {
        const updated = [...flashcards];
        updated.splice(currentCardIndex, 1);
        setFlashcards(updated);
        setShowFlashcardAnswer(false);
        if (currentCardIndex >= updated.length) {
          setCurrentCardIndex(0);
        }
      }
    } catch (e) { console.error(e); }
  };

  const triggerExport = (format: "csv" | "apkg") => {
    if (!documentId) return;
    window.open(`${API_BASE}/api/flashcards/export/${documentId}?format=${format}`);
  };

  // Highlight Selection Explainers
  const handleTextSelection = (e: React.MouseEvent) => {
    const sel = window.getSelection();
    const selectedText = sel ? sel.toString().trim() : "";
    if (selectedText && selectedText.length > 2) {
      const range = sel!.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      
      const leftPane = leftPaneRef.current;
      if (leftPane) {
        const paneRect = leftPane.getBoundingClientRect();
        setSelectionCoords({
          x: rect.left - paneRect.left + leftPane.scrollLeft,
          y: rect.top - paneRect.top + leftPane.scrollTop - 40
        });
      }
      setSelectedHighlightText(selectedText);
      setShowHighlightMenu(true);
    } else {
      setShowHighlightMenu(false);
    }
  };

  const handleHighlightExplain = async (action: string) => {
    if (!selectedHighlightText) return;
    setShowHighlightMenu(false);
    setIsExplaining(true);
    setExplanationResult(null);
    setExplainingAction(action);
    try {
      const r = await fetch(`${API_BASE}/api/explain`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: selectedHighlightText,
          action,
          provider: selectedProvider,
          model: selectedModel
        })
      });
      if (r.ok) {
        const data = await r.json();
        setExplanationResult(data.explanation);
      } else {
        setExplanationResult("Failed to generate explanation.");
      }
    } catch (e: any) {
      setExplanationResult("Error: " + e.message);
    } finally {
      setIsExplaining(false);
    }
  };

  // Chat session
  const initChatSession = async (docId: number) => {
    try {
      const r = await fetch(`${API_BASE}/api/chat/session/${docId}`, { method: "POST" });
      if (r.ok) {
        const data = await r.json();
        setChatSessionId(data.session_id);
        const r2 = await fetch(`${API_BASE}/api/chat/history/${data.session_id}`);
        if (r2.ok) {
          const hData = await r2.json();
          setChatMessages(hData.messages || []);
        }
      }
    } catch (e) { console.error(e); }
  };

  const handleSendChatMessage = async () => {
    if (!chatInput.trim() || !chatSessionId) return;
    const msg = chatInput.trim();
    setChatInput("");
    setChatMessages(prev => [...prev, { role: "user", content: msg }]);
    setIsChatTyping(true);
    setCurrentCitations([]);
    
    // Add dummy message for SSE stream
    setChatMessages(prev => [...prev, { role: "assistant", content: "" }]);
    
    try {
      const response = await fetch(`${API_BASE}/api/chat/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: chatSessionId,
          message: msg,
          provider: selectedProvider,
          model: selectedModel
        })
      });
      
      if (!response.ok) throw new Error("Chat request failed.");
      
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      
      if (reader) {
        let fullContent = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const data = JSON.parse(line.slice(6));
              if (data.type === "citations") {
                setCurrentCitations(data.pages);
              } else if (data.type === "token") {
                fullContent += data.text;
                setChatMessages(prev => {
                  const updated = [...prev];
                  updated[updated.length - 1] = {
                    role: "assistant",
                    content: fullContent,
                    cited_pages: data.pages || []
                  };
                  return updated;
                });
              } else if (data.type === "meta") {
                setChatMeta({ provider: data.provider, model: data.model });
              } else if (data.type === "error") {
                setChatMessages(prev => {
                  const updated = [...prev];
                  updated[updated.length - 1] = {
                    role: "assistant",
                    content: `Error: ${data.message}`
                  };
                  return updated;
                });
              }
            }
          }
        }
      }
    } catch (e: any) {
      setChatMessages(prev => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: "assistant",
          content: `Failed to get response. ${e.message}`
        };
        return updated;
      });
    } finally {
      setIsChatTyping(false);
    }
  };

  // Document Intelligence
  const handleLoadDocIntel = async () => {
    if (!documentId) return;
    setDocIntelError("");
    setDocIntelData(null);
    try {
      const r = await fetch(`${API_BASE}/api/document-intelligence/${documentId}`);
      if (r.ok) {
        const data = await r.json();
        if (data.cached) {
          setDocIntelData(data);
          return;
        }
      }
    } catch (e) { console.error(e); }
  };

  const handleGenerateDocIntel = async () => {
    if (!documentId) return;
    setIsGeneratingDocIntel(true);
    setDocIntelError("");
    try {
      const r = await fetch(`${API_BASE}/api/document-intelligence/${documentId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: selectedProvider,
          model: selectedModel
        })
      });
      if (r.ok) {
        const data = await r.json();
        setDocIntelData(data);
      } else {
        const err = await r.json();
        setDocIntelError(err.detail || "Failed to generate. Ensure all document pages have notes.");
      }
    } catch (e: any) {
      setDocIntelError(e.message || "Failed to generate document intelligence.");
    } finally {
      setIsGeneratingDocIntel(false);
    }
  };

  // Multi-Document Synthesis
  const handleSynthesize = async () => {
    if (selectedSynthDocs.length < 2 || !synthQuestion.trim()) return;
    setIsSynthesizing(true);
    setSynthAnswer("");
    setSynthCitations([]);
    try {
      const r = await fetch(`${API_BASE}/api/synthesise`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          document_ids: selectedSynthDocs,
          question: synthQuestion.trim()
        })
      });
      if (r.ok) {
        const data = await r.json();
        setSynthAnswer(data.answer);
        setSynthCitations(data.citations || []);
        setSynthMeta({ provider: data.provider, model: data.model });
      } else {
        const err = await r.json();
        setSynthAnswer("Error: " + (err.detail || "Failed to synthesize. Ensure all documents are processed."));
      }
    } catch (e: any) {
      setSynthAnswer("Error: " + e.message);
    } finally {
      setIsSynthesizing(false);
    }
  };

  // WebSocket for Batch Processing
  useEffect(() => {
    let ws: WebSocket | null = null;
    if (showBatchDrawer) {
      ws = new WebSocket(`${WS_BASE}/ws/batch-progress`);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "snapshot") {
            setBatchJobs(msg.jobs || []);
          } else if (msg.type === "page_done") {
            setBatchJobs(prev => prev.map(j => {
              if (j.id === msg.job_id) {
                return { ...j, completed_pages: msg.completed_pages, total_pages: msg.total_pages, status: "processing" };
              }
              return j;
            }));
          } else if (msg.type === "job_done") {
            setBatchJobs(prev => prev.map(j => {
              if (j.id === msg.job_id) {
                return { ...j, status: "completed", completed_pages: j.total_pages };
              }
              return j;
            }));
            fetchPreviousBooks();
          } else if (msg.type === "job_failed") {
            setBatchJobs(prev => prev.map(j => {
              if (j.id === msg.job_id) {
                return { ...j, status: "failed", error_message: msg.error };
              }
              return j;
            }));
          }
        } catch (e) { console.error(e); }
      };
    }
    return () => { if (ws) ws.close(); };
  }, [showBatchDrawer, fetchPreviousBooks]);

  // Citation scroller
  const scrollToPage = (pageNum: number) => {
    const el = document.getElementById(`pdf-page-block-${pageNum}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  const handleBookChange = async (e: React.ChangeEvent<HTMLSelectElement>) => {
    const val = e.target.value;
    if (!val) { setDocumentId(null); setPages([]); setNotes({}); return; }
    const id = parseInt(val);
    setDocumentId(id); setPages([]); setNotes({});
    initChatSession(id);
    try {
      const r = await fetch(`${API_BASE}/api/document/${id}`);
      if (!r.ok) return;
      const doc = await r.json();
      setPages((doc.pages || []).map((p: any) => ({ page_number: p.page_number, text: p.text })));
      const init: Record<number, NoteState> = {};
      doc.pages.forEach((p: any) => {
        if (p.cached_note) {
          init[p.page_number] = { status: "success", data: { notes: JSON.parse(p.cached_note.note_data), mind_map: p.cached_note.mind_map, infographic: p.cached_note.infographic, _meta: { provider: p.cached_note.provider, model: p.cached_note.model } } };
          setActiveTabs(prev => ({ ...prev, [p.page_number]: "notes" }));
        } else {
          init[p.page_number] = { status: "idle" };
        }
      });
      setNotes(init);
    } catch (e) { console.error(e); }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files?.length) return;
    const fd = new FormData(); fd.append("file", files[0]);
    setIsUploading(true); setUploadError(""); setPages([]); setNotes({}); setDocumentId(null);
    try {
      const r = await fetch(`${API_BASE}/api/upload`, { method: "POST", body: fd });
      if (!r.ok) { const e = await r.json(); throw new Error(e.detail || "Upload failed."); }
      const data = await r.json();
      setDocumentId(data.document_id); setPages(data.pages);
      initChatSession(data.document_id);
      const init: Record<number, NoteState> = {};
      data.pages.forEach((p: PageData) => { init[p.page_number] = { status: "idle" }; });
      setNotes(init);
      fetchPreviousBooks();
      if (data.cached) {
        const runCached = async () => {
          for (const p of data.pages as PageData[]) {
            await generateNoteForPage(data.document_id, p.page_number, p.text, selectedProvider, selectedModel, selectedImageModel, selectedImageProvider);
            await new Promise(resolve => setTimeout(resolve, 2000));
          }
        };
        runCached();
      }
    } catch (err: any) {
      setUploadError(err.message || "Upload error.");
    } finally { setIsUploading(false); }
  };

  // WebSocket: receive image when background job finishes
  const connectImageWs = useCallback((docId: number, pageNum: number) => {
    const ws = new WebSocket(`${WS_BASE}/ws/note-infographic/${docId}/${pageNum}`);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as { type: string; infographic?: string };
        if (msg.type === "infographic_ready" && msg.infographic) {
          setNotes(prev => {
            const ex = prev[pageNum];
            if (!ex?.data) return prev;
            return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, infographic: msg.infographic!, image_pending: false } } };
          });
        } else {
          setNotes(prev => {
            const ex = prev[pageNum];
            if (!ex?.data) return prev;
            return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, image_pending: false } } };
          });
        }
        ws.close();
      } catch { ws.close(); }
    };
    ws.onerror = () => ws.close();
  }, []);

  const generateNoteForPage = useCallback(async (
    docId: number, pageNum: number, pageText: string,
    provider: string, model: string, imageModel: string, imageProvider: string
  ) => {
    if (!provider || !model) return;

    const stream = useStreaming && (provider === "groq" || provider === "gemini" || provider === "ollama");

    if (!stream) {
      setNotes(prev => ({ ...prev, [pageNum]: { status: "generating" } }));
      try {
        const r = await fetch(`${API_BASE}/api/generate-page-note`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_id: docId, page_number: pageNum, text: pageText, model, provider, image_model: imageModel, image_provider: imageProvider })
        });
        if (!r.ok) { const e = await r.json(); throw new Error(e.detail || "Generation failed."); }
        const resp: ServerResponse = await r.json();
        setNotes(prev => ({ ...prev, [pageNum]: { status: "success", data: resp } }));
        setActiveTabs(prev => ({ ...prev, [pageNum]: "notes" }));
        if (resp.image_pending) connectImageWs(docId, pageNum);
      } catch (err: any) {
        setNotes(prev => ({ ...prev, [pageNum]: { status: "error", errorMsg: err.message || "Failed." } }));
      }
      return;
    }

    setNotes(prev => ({
      ...prev,
      [pageNum]: {
        status: "generating",
        data: {
          notes: {
            title: "",
            summary: "",
            tldr: "",
            key_concepts: [],
            sections: [],
            formulas_and_rules: [],
            practice_questions: [],
            common_mistakes: [],
            brainstorming_ideas: []
          },
          mind_map: "",
          infographic: ""
        }
      }
    }));

    try {
      const response = await fetch(`${API_BASE}/api/generate-page-note/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: docId, page_number: pageNum, text: pageText, model, provider, image_model: imageModel, image_provider: imageProvider })
      });

      if (!response.ok) {
        const e = await response.json();
        throw new Error(e.detail || "Streaming failed.");
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const dataStr = line.slice(6).trim();
              if (dataStr === "[DONE]") {
                break;
              }
              try {
                const event = JSON.parse(dataStr);
                if (event.type === "meta") {
                  setNotes(prev => {
                    const pageNote = prev[pageNum];
                    if (!pageNote || !pageNote.data) return prev;
                    return {
                      ...prev,
                      [pageNum]: {
                        ...pageNote,
                        data: {
                          ...pageNote.data,
                          _meta: { provider: event.provider, model: event.model }
                        }
                      }
                    };
                  });
                } else if (event.type === "field") {
                  setNotes(prev => {
                    const pageNote = prev[pageNum];
                    if (!pageNote || !pageNote.data) return prev;
                    return {
                      ...prev,
                      [pageNum]: {
                        ...pageNote,
                        data: {
                          ...pageNote.data,
                          notes: {
                            ...pageNote.data.notes,
                            [event.field]: event.value
                          }
                        }
                      }
                    };
                  });
                } else if (event.type === "section") {
                  setNotes(prev => {
                    const pageNote = prev[pageNum];
                    if (!pageNote || !pageNote.data) return prev;
                    const currentSections = [...(pageNote.data.notes.sections || [])];
                    currentSections[event.index] = event.section;
                    return {
                      ...prev,
                      [pageNum]: {
                        ...pageNote,
                        data: {
                          ...pageNote.data,
                          notes: {
                            ...pageNote.data.notes,
                            sections: currentSections
                          }
                        }
                      }
                    };
                  });
                } else if (event.type === "final") {
                  setNotes(prev => {
                    return {
                      ...prev,
                      [pageNum]: {
                        status: "success",
                        data: {
                          notes: event.notes,
                          mind_map: "",
                          infographic: "",
                          _meta: event.meta
                        }
                      }
                    };
                  });
                  setActiveTabs(prev => ({ ...prev, [pageNum]: "notes" }));
                } else if (event.type === "error") {
                  throw new Error(event.message);
                }
              } catch (e: any) {
                if (e.message && e.message.includes("Rate limited")) {
                  throw e;
                }
              }
            }
          }
        }
      }
    } catch (err: any) {
      setNotes(prev => ({ ...prev, [pageNum]: { status: "error", errorMsg: err.message || "Failed." } }));
    }
  }, [useStreaming, connectImageWs]);

  const handleGenerateAll = useCallback(() => {
    if (documentId === null) return;
    const pending = pages.filter(p => {
      const s = notes[p.page_number]?.status;
      return s === "idle" || s === "error";
    });
    if (pending.length === 0) return;

    const runQueue = async () => {
      for (const p of pending) {
        const current = notes[p.page_number]?.status;
        if (current === "success" || current === "generating") continue;
        await generateNoteForPage(
          documentId, p.page_number, p.text,
          selectedProvider, selectedModel,
          selectedImageModel, selectedImageProvider
        );
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    };
    runQueue();
  }, [documentId, pages, notes, selectedProvider, selectedModel, selectedImageModel, selectedImageProvider, generateNoteForPage]);

  const retryPage = useCallback(async (pageNum: number, pageText: string) => {
    if (documentId === null) return;
    const ov = retryModels[pageNum] || `${selectedProvider}|${selectedModel}`;
    const [rp, ...rm] = ov.split("|");
    if (!rp || !rm.length) return;
    try { await fetch(`${API_BASE}/api/note/${documentId}/${pageNum}`, { method: "DELETE" }); } catch {}
    await generateNoteForPage(documentId, pageNum, pageText, rp, rm.join("|"), selectedImageModel, selectedImageProvider);
  }, [documentId, retryModels, selectedProvider, selectedModel, selectedImageModel, selectedImageProvider, generateNoteForPage]);

  const toggleAccordion = (pg: number, qi: number) => {
    const k = `${pg}-${qi}`;
    setOpenAccordions(prev => ({ ...prev, [k]: !prev[k] }));
  };

  const copyToClipboard = (text: string, key: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopyFeedback(prev => ({ ...prev, [key]: true }));
      setTimeout(() => setCopyFeedback(prev => ({ ...prev, [key]: false })), 2000);
    });
  };

  const handleTabSwitch = (n: number, tab: "notes" | "mindmap" | "infographic") => {
    setActiveTabs(prev => ({ ...prev, [n]: tab }));
    const ns = notes[n];
    if (!ns?.data || ns.status !== "success") return;
    if (tab === "mindmap" && !ns.data.mind_map) {
      generateMindMap(n);
    }
    if (tab === "infographic" && !ns.data.infographic) {
      generateInfographic(n);
    }
  };

  const generateMindMap = useCallback(async (pageNum: number) => {
    if (documentId === null) return;
    setNotes(prev => {
      const ex = prev[pageNum];
      if (!ex?.data) return prev;
      return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, mind_map: "__loading__" } } };
    });
    try {
      const r = await fetch(`${API_BASE}/api/generate-mindmap`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: documentId, page_number: pageNum, model: selectedModel, provider: selectedProvider })
      });
      if (!r.ok) throw new Error((await r.json()).detail || "Mind map generation failed.");
      const data = await r.json();
      setNotes(prev => {
        const ex = prev[pageNum];
        if (!ex?.data) return prev;
        return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, mind_map: data.mind_map } } };
      });
    } catch (err: any) {
      setNotes(prev => {
        const ex = prev[pageNum];
        if (!ex?.data) return prev;
        return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, mind_map: "" } } };
      });
      console.error("Mind map generation failed:", err.message);
    }
  }, [documentId, selectedModel, selectedProvider]);

  const generateInfographic = useCallback(async (pageNum: number) => {
    if (documentId === null) return;
    setNotes(prev => {
      const ex = prev[pageNum];
      if (!ex?.data) return prev;
      return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, image_pending: true } } };
    });
    try {
      const r = await fetch(`${API_BASE}/api/generate-infographic`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: documentId, page_number: pageNum, model: selectedModel, provider: selectedProvider, image_model: selectedImageModel })
      });
      if (!r.ok) throw new Error((await r.json()).detail || "Infographic generation failed.");
      const data = await r.json();
      setNotes(prev => {
        const ex = prev[pageNum];
        if (!ex?.data) return prev;
        return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, infographic: data.infographic, image_pending: data.image_pending } } };
      });
      if (data.image_pending) connectImageWs(documentId, pageNum);
    } catch (err: any) {
      setNotes(prev => {
        const ex = prev[pageNum];
        if (!ex?.data) return prev;
        return { ...prev, [pageNum]: { ...ex, data: { ...ex.data, image_pending: false } } };
      });
      console.error("Infographic generation failed:", err.message);
    }
  }, [documentId, selectedModel, selectedProvider, selectedImageModel, connectImageWs]);

  const handleExportNotes = () => {
    if (!pages.length) return;
    let content = "# SyncedNotes AI Study Guide\n\n---\n\n";
    pages.forEach(p => {
      const ns = notes[p.page_number];
      if (ns?.status !== "success" || !ns.data) return;
      const d = ns.data.notes;
      content += `## Page ${p.page_number}: ${d.title || "Untitled"}\n\n`;
      if (d.tldr) content += `> **TL;DR:** ${d.tldr}\n\n`;
      if (d.summary) content += `${d.summary}\n\n`;

      if (d.key_concepts?.length) {
        content += `### Key Concepts\n\n`;
        d.key_concepts.forEach(kc => {
          content += `**${kc.term}** — ${kc.definition}`;
          if (kc.example) content += ` *(e.g. ${kc.example})*`;
          content += "\n\n";
        });
      }

      if (d.formulas_and_rules?.length) {
        content += `### Formulas & Rules\n\n`;
        d.formulas_and_rules.forEach(f => {
          content += `**${f.name}:** \`${f.expression}\`\n`;
          if (f.variables) content += `- Variables: ${f.variables}\n`;
          if (f.when_to_use) content += `- Use when: ${f.when_to_use}\n`;
          content += "\n";
        });
      }

      d.sections?.forEach(s => {
        content += `### ${s.heading}\n\n`;
        s.points?.forEach(pt => { content += `- ${pt}\n`; }); content += "\n";
        if (s.comparison_table?.headers?.length) {
          content += `| ${s.comparison_table.headers.join(" | ")} |\n`;
          content += `| ${s.comparison_table.headers.map(() => "---").join(" | ")} |\n`;
          s.comparison_table.rows?.forEach(row => { content += `| ${row.join(" | ")} |\n`; });
          content += "\n";
        }
        s.code_blocks?.forEach(cb => { if (cb) content += `\`\`\`${cb.language}\n${cb.code}\n\`\`\`\n\n`; });
      });

      if (d.common_mistakes?.length) {
        content += `### ⚠ Common Mistakes\n\n`;
        d.common_mistakes.forEach(m => { content += `- ${m}\n`; }); content += "\n";
      }

      if (d.practice_questions?.length) {
        content += `### Practice Questions\n\n`;
        d.practice_questions.forEach(pq => {
          const diff = pq.difficulty ? ` [${pq.difficulty}]` : "";
          content += `**Q${diff}: ${pq.question}**\n*A: ${pq.answer}*\n\n`;
        });
      }

      if (d.brainstorming_ideas?.length) {
        content += `### Brainstorming & Applications\n\n`;
        d.brainstorming_ideas.forEach(i => { content += `- ${i}\n`; }); content += "\n";
      }

      if (ns.data.mind_map) content += `### Mind Map\n\n\`\`\`mermaid\n${ns.data.mind_map.trim()}\n\`\`\`\n\n`;
      content += "---\n\n";
    });
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8;" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `notes-${documentId || "export"}.md`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  };

  // Scroll sync via IntersectionObserver
  useEffect(() => {
    if (!pages.length) return;
    observerRef.current?.disconnect();
    const obs = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const n = parseInt(en.target.getAttribute("data-pagenumber") || "1");
        setActivePage(n);
        if (syncScroll) document.getElementById(`note-page-block-${n}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }, { root: leftPaneRef.current, threshold: 0.45 });
    pages.forEach(p => { const el = document.getElementById(`pdf-page-block-${p.page_number}`); if (el) obs.observe(el); });
    observerRef.current = obs;
    return () => observerRef.current?.disconnect();
  }, [pages, syncScroll]);

  // Dropdown builders
  const textModelOptions = () => {
    const groups: React.ReactNode[] = [];
    Object.entries(modelChoices.cloud).forEach(([prov, models]) => {
      const seen = new Set<string>();
      const filtered = (models as string[]).filter(m => {
        if (prov === "gemini" && (m.includes("imagen") || m.includes("banana"))) return false;
        if (seen.has(m)) return false; seen.add(m); return true;
      });
      if (!filtered.length) return;
      groups.push(<optgroup key={prov} label={prov.toUpperCase()}>{filtered.map(m => <option key={`${prov}|${m}`} value={`${prov}|${m}`}>{getModelStatusEmoji(m, modelChoices.statuses)}{m}</option>)}</optgroup>);
    });
    if (modelChoices.local?.length) {
      const seen = new Set<string>();
      groups.push(<optgroup key="ollama" label="LOCAL OLLAMA">{modelChoices.local.filter(m => { if (seen.has(m)) return false; seen.add(m); return true; }).map(m => <option key={`ollama|${m}`} value={`ollama|${m}`}>{getModelStatusEmoji(m, modelChoices.statuses)}{m}</option>)}</optgroup>);
    }
    return groups;
  };

  const retryModelOptions = () => {
    const opts: React.ReactNode[] = []; const seen = new Set<string>();
    Object.entries(modelChoices.cloud).forEach(([prov, models]) => {
      (models as string[]).filter(m => {
        if (prov === "gemini" && (m.includes("imagen") || m.includes("banana"))) return false;
        const k = `${prov}|${m}`; if (seen.has(k)) return false; seen.add(k); return true;
      }).forEach(m => { const s = modelChoices.statuses?.[m]; const ic = s === "ok" ? "🟢" : s === "error" ? "🔴" : "🟡"; opts.push(<option key={`${prov}|${m}`} value={`${prov}|${m}`}>{ic} [{prov}] {m}</option>); });
    });
    (modelChoices.local || []).forEach(m => { const k = `ollama|${m}`; if (!seen.has(k)) { seen.add(k); opts.push(<option key={k} value={k}>🟡 [ollama] {m}</option>); } });
    return opts;
  };

  const hasGeminiImagen = (modelChoices.image_providers || []).some(p => p.id === "gemini_imagen");
  const hasHuggingFace  = (modelChoices.image_providers || []).some(p => p.id === "huggingface");
  const hasPollinations = (modelChoices.image_providers || []).some(p => p.id === "pollinations");

  // ─── Render ──────────────────────────────────────────────────────────────
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }} onMouseUp={handleTextSelection}>
      {/* Header */}
      <header className="app-header">
        <div className="app-title" style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <span>SyncedNotes AI</span>
          <button className="btn-action-outline" style={{ fontSize: "0.7rem", padding: "0.3rem 0.6rem" }} onClick={() => { fetchKeys(); setShowKeyModal(true); }}>⚙️ API Keys</button>
          <button className="btn-action-outline" style={{ fontSize: "0.7rem", padding: "0.3rem 0.6rem" }} onClick={() => setShowBatchDrawer(true)}>📥 Batch Inbox</button>
        </div>
        <div className="header-controls">

          {/* Library */}
          <div className="control-item">
            <span>Library</span>
            <div className="select-wrapper">
              <select id="book-selector" value={documentId || ""} onChange={handleBookChange}>
                <option value="">📂 SELECT BOOK…</option>
                {previousBooks.map(b => <option key={b.id} value={b.id}>{b.filename} ({new Date(b.created_at).toLocaleDateString()})</option>)}
              </select>
            </div>
          </div>

          {/* Theme */}
          <div className="control-item">
            <span>Theme</span>
            <div className="select-wrapper">
              <select id="theme-selector" value={theme} onChange={e => setTheme(e.target.value as any)}>
                <option value="light">LIGHT</option>
                <option value="dark">DARK</option>
                <option value="sepia">SEPIA</option>
                <option value="nord">NORD</option>
              </select>
            </div>
          </div>

          {/* Sync scroll */}
          <div className="control-item checkbox-item">
            <span>Sync</span>
            <input type="checkbox" id="sync-scroll-chk" checked={syncScroll} onChange={e => setSyncScroll(e.target.checked)} />
          </div>

          {/* Stream notes */}
          <div className="control-item checkbox-item">
            <span>Stream</span>
            <input type="checkbox" id="stream-chk" checked={useStreaming} onChange={e => setUseStreaming(e.target.checked)} />
          </div>

          {/* Text model */}
          <div className="control-item">
            <span>Text Model</span>
            <div className="select-wrapper">
              <select id="model-selector" value={`${selectedProvider}|${selectedModel}`} onChange={handleModelChange}>
                {textModelOptions()}
              </select>
            </div>
          </div>

          {/* Image provider */}
          <div className="control-item">
            <span>Image Source</span>
            <div className="select-wrapper">
              <select id="image-provider-selector" style={{ width: "175px" }} value={selectedImageProvider} onChange={e => setSelectedImageProvider(e.target.value)}>
                {hasGeminiImagen && <option value="gemini_imagen">🆓 Gemini Imagen</option>}
                {hasHuggingFace  && <option value="huggingface">🆓 HuggingFace FLUX</option>}
                {hasPollinations && <option value="pollinations">🔑 Pollinations.AI</option>}
                <option value="ollama">⚙ Ollama SVG</option>
              </select>
            </div>
          </div>

          {/* Actions */}
          {pages.length > 0 && (
            <div className="action-buttons-container" style={{ display: "flex", gap: "0.5rem" }}>
              <button className="btn-action-outline" onClick={() => { fetchFlashcards(dueOnlyCards); setShowFlashcardPanel(true); }}>⚡ Flashcards</button>
              <button className="btn-action-outline" onClick={() => { handleLoadDocIntel(); setShowDocIntelModal(true); }}>📊 Doc Intel</button>
              <button className="btn-action-outline" onClick={() => setShowSynthesisModal(true)}>🔀 Synthesise</button>
              <button className="btn-action-outline" onClick={() => setShowChatDrawer(true)}>💬 Chat</button>
              <button className="btn-action-outline" onClick={handleExportNotes}>Export MD</button>
              <button className="btn-action-primary" onClick={handleGenerateAll}>Generate All</button>
            </div>
          )}
        </div>
      </header>

      {/* Workspace */}
      <main className="workspace-container">

        {/* Left pane — PDF viewer */}
        <div className="left-pane" ref={leftPaneRef} style={{ position: "relative" }}>
          {pages.length === 0 ? (
            <div className="empty-notes-state" style={{ height: "100%" }}>
              <label className="upload-dropzone" htmlFor="pdf-file-input">
                <span className="upload-icon">↑</span>
                <span className="upload-text">Upload PDF Document</span>
                <span className="upload-subtext">Cached locally in SQLite automatically</span>
                <input type="file" id="pdf-file-input" accept=".pdf" onChange={handleFileUpload} style={{ display: "none" }} />
              </label>
              {isUploading && <div style={{ marginTop: "1rem", fontFamily: "var(--font-mono)", fontSize: "0.8rem", color: "var(--text-secondary)" }}>Parsing PDF…</div>}
              {uploadError && <div className="error-alert" style={{ marginTop: "1rem" }}>⚠️ {uploadError}</div>}
            </div>
          ) : (
            <div id="pdf-scroll-list">
              {pages.map(p => (
                <div key={p.page_number} id={`pdf-page-block-${p.page_number}`} data-pagenumber={p.page_number} className={`pdf-page-block ${activePage === p.page_number ? "active" : ""}`}>
                  <div className="page-header">
                    <span className="page-number-tag">PAGE {String(p.page_number).padStart(2, "0")}</span>
                    {notes[p.page_number]?.status === "success" && <span style={{ color: "var(--text-muted)", fontSize: "0.7rem" }}>Synced ✓</span>}
                  </div>
                  <div className="pdf-page-image-container" style={{ position: "relative" }}>
                    <img src={`${API_BASE}/api/document/${documentId}/page/${p.page_number}/image`} alt={`Page ${p.page_number}`} className="pdf-page-image" loading="lazy" />
                    
                    {/* Transparent overlay text layer for text selection */}
                    <div className="pdf-text-layer">
                      {p.text}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Floating Highlight / Explain Action Menu */}
          {showHighlightMenu && (
            <div className="highlight-menu" style={{ position: "absolute", left: selectionCoords.x, top: selectionCoords.y }}>
              <button onClick={() => handleHighlightExplain("explain")}>Explain</button>
              <button onClick={() => handleHighlightExplain("define")}>Define</button>
              <button onClick={() => handleHighlightExplain("simplify")}>Simplify</button>
              <button onClick={() => handleHighlightExplain("example")}>Example</button>
            </div>
          )}

          {/* Floating Explain Popover */}
          {(isExplaining || explanationResult) && (
            <div className="explain-popover" style={{ position: "absolute", left: selectionCoords.x, top: selectionCoords.y + 40 }}>
              <div className="popover-header">
                <strong>{explainingAction.toUpperCase()}</strong>
                <button onClick={() => { setExplanationResult(null); setSelectedHighlightText(""); }} style={{ border: "none", background: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: "1.1rem" }}>×</button>
              </div>
              <div className="popover-content">
                {isExplaining ? (
                  <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                    <div className="spinner" style={{ width: "12px", height: "12px" }} />
                    <span>Analyzing selection…</span>
                  </div>
                ) : (
                  explanationResult
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right pane — AI notes */}
        <div className="right-pane" ref={rightPaneRef}>
          {pages.length === 0 ? (
            <div className="empty-notes-state">
              <span className="empty-notes-title">AI Workspace</span>
              <p className="empty-notes-desc">Upload a PDF. Notes, mind maps, and infographics are generated and cached locally.</p>
              <div style={{ marginTop: "1rem", padding: "0.75rem 1rem", background: "var(--bg-secondary)", borderRadius: "6px", border: "1px solid var(--accent-muted)", fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text-muted)", maxWidth: "320px", lineHeight: 1.6 }}>
                🆓 Images generated free via <strong style={{ color: "var(--text-secondary)" }}>Gemini Imagen</strong> using your existing API key.
              </div>
            </div>
          ) : (
            <div id="notes-scroll-list">
              {pages.map(p => {
                const ns   = notes[p.page_number] || { status: "idle" };
                const tab  = activeTabs[p.page_number] || "notes";
                return (
                  <div key={p.page_number} id={`note-page-block-${p.page_number}`} className={`note-page-block ${activePage === p.page_number ? "active" : ""}`}>

                    {/* Page header */}
                    <div className="note-header" style={{ marginBottom: "1rem" }}>
                      <span className="active-dot" />
                      <span>PAGE {p.page_number}</span>
                      {ns.data?._meta && <span className="audit-meta-tag">{ns.data._meta.provider.toUpperCase()} — {ns.data._meta.model}</span>}
                    </div>

                    {/* Idle */}
                    {ns.status === "idle" && (
                      <div className="generation-status">
                        <span>Notes pending.</span>
                        <button className="btn-generate-all" style={{ padding: "0.4rem 1rem", fontSize: "0.7rem", marginTop: "0.5rem" }}
                          onClick={() => generateNoteForPage(documentId!, p.page_number, p.text, selectedProvider, selectedModel, selectedImageModel, selectedImageProvider)}>
                          Generate Notes
                        </button>
                      </div>
                    )}

                    {/* Generating (initial loading) */}
                    {ns.status === "generating" && !(ns.data?.notes && (ns.data.notes.title || ns.data.notes.summary)) && (
                      <div className="generation-status"><div className="spinner" /><span>Generating notes, mind map, and infographic…</span></div>
                    )}

                    {/* Error */}
                    {ns.status === "error" && (
                      <div className="error-alert">
                        <span>⚠ Generation failed.</span>
                        <span style={{ fontSize: "0.75rem", opacity: 0.8 }}>{ns.errorMsg}</span>
                        <div className="retry-bar">
                          <select className="retry-model-select" value={retryModels[p.page_number] || `${selectedProvider}|${selectedModel}`} onChange={e => setRetryModels(prev => ({ ...prev, [p.page_number]: e.target.value }))}>{retryModelOptions()}</select>
                          <button className="btn-retry-model" onClick={() => retryPage(p.page_number, p.text)}>↺ Retry</button>
                        </div>
                      </div>
                    )}

                    {/* Success or Streaming notes */}
                    {((ns.status === "success" && ns.data) || (ns.status === "generating" && ns.data?.notes && (ns.data.notes.title || ns.data.notes.summary))) && ns.data && (
                      <div>
                        {ns.status === "generating" && (
                          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", padding: "0.6rem 0.8rem", background: "var(--bg-secondary)", border: "1px solid var(--accent-muted)", borderRadius: "4px", marginBottom: "1.25rem", fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text-secondary)" }}>
                            <div className="spinner" style={{ width: "12px", height: "12px" }} />
                            <span>Streaming note content in real-time…</span>
                          </div>
                        )}
                        {/* Tabs */}
                        {ns.status === "success" ? (
                          <div className="note-tabs-header">
                            {(["notes", "mindmap", "infographic"] as const).map(t => (
                              <button key={t} className={`note-tab-btn ${tab === t ? "active" : ""}`} onClick={() => handleTabSwitch(p.page_number, t)}>
                                {t === "notes" ? "Study Notes" : t === "mindmap" ? "Mind Map" : (
                                  <span style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
                                    Infographic
                                    {ns.data!.image_pending && <span style={{ width: "6px", height: "6px", borderRadius: "50%", border: "1.5px solid var(--text-muted)", borderTopColor: "var(--accent)", display: "inline-block", animation: "spin 0.8s linear infinite" }} />}
                                  </span>
                                )}
                              </button>
                            ))}
                            <div style={{ flex: 1 }} />
                            {/* Inline retry */}
                            <div className="retry-bar-inline">
                              <select className="retry-model-select-sm" value={retryModels[p.page_number] || `${selectedProvider}|${selectedModel}`} onChange={e => setRetryModels(prev => ({ ...prev, [p.page_number]: e.target.value }))}>{retryModelOptions()}</select>
                              <button className="btn-retry-sm" onClick={() => retryPage(p.page_number, p.text)}>↺ Regen</button>
                            </div>
                          </div>
                        ) : (
                          <div className="note-tabs-header">
                            <button className="note-tab-btn active">Study Notes</button>
                          </div>
                        )}

                        {/* Notes tab */}
                        {tab === "notes" && (
                          <div>
                            <h2 className="note-title">{asString(ns.data.notes.title) || "Untitled"}</h2>

                            {/* TL;DR */}
                            {ns.data.notes.tldr && (
                              <div className="tldr-pill">
                                <span className="tldr-label">TL;DR</span>
                                {asString(ns.data.notes.tldr)}
                              </div>
                            )}

                            {/* Summary */}
                            <p className="note-summary">{asString(ns.data.notes.summary)}</p>

                            {/* Key Concepts */}
                            {ns.data.notes.key_concepts && ns.data.notes.key_concepts.length > 0 && (
                              <div className="note-section">
                                <h3 className="note-section-heading">Key Concepts</h3>
                                <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                                  {ns.data.notes.key_concepts.map((kc, ki) => {
                                    const term = asString((kc as any).term);
                                    const def  = asString((kc as any).definition);
                                    const ex   = asString((kc as any).example);
                                    return (
                                      <div key={ki} className="key-concept-card">
                                        <div className="key-concept-term">{term}</div>
                                        <div className="key-concept-def">{def}</div>
                                        {ex && <div className="key-concept-example"><span style={{ fontWeight: 600 }}>e.g. </span>{ex}</div>}
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            )}

                            {/* Formulas & Rules */}
                            {ns.data.notes.formulas_and_rules && ns.data.notes.formulas_and_rules.length > 0 && (
                              <div className="note-section">
                                <h3 className="note-section-heading">Formulas &amp; Rules</h3>
                                <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                                  {ns.data.notes.formulas_and_rules.map((f, fi) => {
                                    const name  = asString((f as any).name);
                                    const expr  = asString((f as any).expression);
                                    const vars  = asString((f as any).variables);
                                    const when  = asString((f as any).when_to_use);
                                    return (
                                      <div key={fi} className="formula-card">
                                        <div className="formula-name">{name}</div>
                                        <div className="formula-expression">{expr}</div>
                                        {vars && <div className="formula-meta"><span style={{ fontWeight: 600 }}>Where: </span>{vars}</div>}
                                        {when && <div className="formula-meta" style={{ fontStyle: "italic" }}><span style={{ fontStyle: "normal", fontWeight: 600 }}>Use when: </span>{when}</div>}
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            )}

                            {/* Main Sections */}
                            {normaliseSections(ns.data.notes.sections).map((sec, si) => (
                              <div key={si} className="note-section">
                                <h3 className="note-section-heading">{sec.heading}</h3>
                                <ul className="note-section-points">
                                  {sec.points.map((pt, pi) => <li key={pi}>{asString(pt)}</li>)}
                                </ul>

                                {/* Comparison table */}
                                {sec.comparison_table && (sec.comparison_table.headers?.length ?? 0) > 0 && (
                                  <div style={{ overflowX: "auto", margin: "1rem 0" }}>
                                    {sec.comparison_table.caption && (
                                      <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text-muted)", marginBottom: "0.5rem", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                                        {asString(sec.comparison_table.caption)}
                                      </div>
                                    )}
                                    <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "var(--font-mono)", fontSize: "0.8rem" }}>
                                      <thead>
                                        <tr>{sec.comparison_table.headers.map((h, hi) => (
                                          <th key={hi} style={{ padding: "0.5rem 0.75rem", textAlign: "left", borderBottom: "2px solid var(--accent-muted)", color: "var(--text-primary)", fontWeight: 700, background: "var(--bg-secondary)", whiteSpace: "nowrap" }}>{asString(h)}</th>
                                        ))}</tr>
                                      </thead>
                                      <tbody>
                                        {(sec.comparison_table.rows || []).map((row, ri) => (
                                          <tr key={ri} style={{ borderBottom: "1px solid var(--accent-muted)" }}>
                                            {(Array.isArray(row) ? row : []).map((cell, ci) => (
                                              <td key={ci} style={{ padding: "0.5rem 0.75rem", color: ci === 0 ? "var(--text-primary)" : "var(--text-secondary)", fontWeight: ci === 0 ? 600 : 400, background: ri % 2 === 0 ? "transparent" : "var(--bg-secondary)", verticalAlign: "top", lineHeight: 1.5 }}>
                                                {asString(cell)}
                                              </td>
                                            ))}
                                          </tr>
                                        ))}
                                      </tbody>
                                    </table>
                                  </div>
                                )}

                                {/* Code blocks */}
                                {(sec.code_blocks || []).map((cb, ci) => {
                                  if (!cb) return null;
                                  const ck = `${p.page_number}-${si}-${ci}`;
                                  return (
                                    <div key={ci} className="code-block-container">
                                      <div className="code-block-header">
                                        <span>{asString((cb as any).language || "code").toUpperCase()}</span>
                                        <button className="btn-copy" onClick={() => copyToClipboard(asString((cb as any).code), ck)}>{copyFeedback[ck] ? "COPIED ✓" : "COPY"}</button>
                                      </div>
                                      <pre className="code-block-pre"><code>{asString((cb as any).code)}</code></pre>
                                    </div>
                                  );
                                })}
                              </div>
                            ))}

                            {/* Common Mistakes */}
                            {ns.data.notes.common_mistakes && ns.data.notes.common_mistakes.length > 0 && (
                              <div className="note-section">
                                <h3 className="note-section-heading" style={{ color: "#b45309" }}>⚠ Common Mistakes</h3>
                                <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                                  {asStringArray(ns.data.notes.common_mistakes).map((m, mi) => (
                                    <div key={mi} className="mistake-card">{m}</div>
                                  ))}
                                </div>
                              </div>
                            )}

                            {/* Practice Questions */}
                            {ns.data.notes.practice_questions?.length > 0 && (
                              <div className="accordion-wrapper">
                                <div className="accordion-title">Practice Questions</div>
                                {ns.data.notes.practice_questions.map((pq, qi) => {
                                  if (!pq) return null;
                                  const q    = asString((pq as any).question);
                                  const a    = asString((pq as any).answer);
                                  const diff = asString((pq as any).difficulty) as "basic" | "intermediate" | "advanced" | "";
                                  const open = !!openAccordions[`${p.page_number}-${qi}`];
                                  const diffClass = diff === "advanced" ? "diff-badge diff-badge-advanced" : diff === "intermediate" ? "diff-badge diff-badge-intermediate" : diff === "basic" ? "diff-badge diff-badge-basic" : "";
                                  return (
                                    <div key={qi} className={`accordion-item ${open ? "open" : ""}`}>
                                      <button className="accordion-trigger" onClick={() => toggleAccordion(p.page_number, qi)}>
                                        <span style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                                          {diff && <span className={diffClass}>{diff}</span>}
                                          {q}
                                        </span>
                                        <span className="accordion-icon">↓</span>
                                      </button>
                                      <div className="accordion-content"><div className="accordion-answer">{a}</div></div>
                                    </div>
                                  );
                                })}
                              </div>
                            )}

                            {/* Brainstorming */}
                            {ns.data.notes.brainstorming_ideas?.length > 0 && (
                              <div className="brainstorming-wrapper">
                                <div className="brainstorming-title">Brainstorming &amp; Applications</div>
                                <div className="brainstorming-list">
                                  {asStringArray(ns.data.notes.brainstorming_ideas).map((idea, ii) => (
                                    <div key={ii} className="brainstorming-card">{idea}</div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}

                        {/* Mind map tab */}
                        {tab === "mindmap" && (
                          <div className="mindmap-container">
                            {ns.data.mind_map === "__loading__" ? (
                              <div className="generation-status"><div className="spinner" /><span>Generating mind map…</span></div>
                            ) : ns.data.mind_map ? (
                              <MermaidRenderer chart={ns.data.mind_map} id={`mm-${p.page_number}`} />
                            ) : (
                              <div className="generation-status">
                                <span>Mind map not yet generated.</span>
                                <button className="btn-generate-all" style={{ padding: "0.4rem 1rem", fontSize: "0.7rem", marginTop: "0.5rem" }} onClick={() => generateMindMap(p.page_number)}>Generate Mind Map</button>
                              </div>
                            )}
                          </div>
                        )}

                        {/* Infographic tab */}
                        {tab === "infographic" && (
                          !ns.data.infographic && !ns.data.image_pending ? (
                            <div className="generation-status">
                              <span>Infographic not yet generated.</span>
                              <button className="btn-generate-all" style={{ padding: "0.4rem 1rem", fontSize: "0.7rem", marginTop: "0.5rem" }} onClick={() => generateInfographic(p.page_number)}>Generate Infographic</button>
                            </div>
                          ) : (
                            <InfographicRenderer svgCode={ns.data.infographic} pending={ns.data.image_pending} />
                          )
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </main>

      {/* ─── MODALS & DRAWERS (New Integrations) ────────────────────────────── */}

      {/* API Key Manager Modal */}
      {showKeyModal && (
        <div className="modal-overlay">
          <div className="modal-content">
            <div className="modal-header">
              <span className="modal-title">API Key Configuration</span>
              <button className="btn-close-drawer" onClick={() => setShowKeyModal(false)}>×</button>
            </div>
            <div className="modal-body">
              <p style={{ fontSize: "0.8rem", color: "var(--text-secondary)", marginBottom: "1rem" }}>
                Add your API keys below (one per line, or comma-separated). These keys are saved locally in <code>keys.json</code>.
              </p>
              
              {(["gemini", "groq", "mistral"] as const).map(provider => (
                <div key={provider} className="key-form-group">
                  <label className="key-form-label">
                    {provider.toUpperCase()} KEYS
                    {keysMasked[provider]?.length > 0 && (
                      <span style={{ marginLeft: "0.5rem", color: "#22c55e", fontSize: "0.6rem" }}>
                        ({keysMasked[provider].length} configured: {keysMasked[provider].join(", ")})
                      </span>
                    )}
                  </label>
                  <div className="key-input-container">
                    <textarea
                      className="key-textarea"
                      placeholder={`Enter ${provider} keys...`}
                      value={keysInput[provider]}
                      onChange={e => setKeysInput(prev => ({ ...prev, [provider]: e.target.value }))}
                    />
                    <button
                      className="btn-action-outline"
                      style={{ fontSize: "0.7rem", height: "fit-content", padding: "0.4rem 0.6rem" }}
                      disabled={probeStatus[provider]?.loading}
                      onClick={() => probeKey(provider, keysInput[provider])}
                    >
                      {probeStatus[provider]?.loading ? "Testing..." : "Test Key"}
                    </button>
                  </div>
                  {probeStatus[provider] && (
                    <div style={{ marginTop: "0.3rem", fontSize: "0.7rem", color: probeStatus[provider].valid ? "#22c55e" : "#ef4444", fontWeight: 500 }}>
                      {probeStatus[provider].valid ? "✓ Valid Key: " : "✗ Invalid: "}{probeStatus[provider].message}
                    </div>
                  )}
                </div>
              ))}
            </div>
            <div className="modal-footer">
              <button className="btn-action-outline" onClick={() => setShowKeyModal(false)}>Cancel</button>
              <button className="btn-action-primary" onClick={handleSaveKeys}>Save &amp; Apply</button>
            </div>
          </div>
        </div>
      )}

      {/* Flashcards & Spaced Repetition Drawer */}
      {showFlashcardPanel && (
        <div className="drawer-overlay" onClick={() => setShowFlashcardPanel(false)}>
          <div className="drawer" onClick={e => e.stopPropagation()}>
            <div className="drawer-header">
              <span className="drawer-title">⚡ Practice Decks</span>
              <button className="btn-close-drawer" onClick={() => setShowFlashcardPanel(false)}>×</button>
            </div>
            <div className="drawer-body">
              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1rem" }}>
                <button
                  className="btn-action-primary"
                  style={{ flex: 1, fontSize: "0.75rem" }}
                  disabled={isGeneratingCards}
                  onClick={handleGenerateFlashcards}
                >
                  {isGeneratingCards ? "Generating..." : "Generate Cards from Notes"}
                </button>
                <button className="btn-action-outline" style={{ fontSize: "0.75rem" }} onClick={() => triggerExport("apkg")}>Anki</button>
                <button className="btn-action-outline" style={{ fontSize: "0.75rem" }} onClick={() => triggerExport("csv")}>CSV</button>
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "1.25rem", fontSize: "0.8rem", color: "var(--text-secondary)" }}>
                <input
                  type="checkbox"
                  id="due-only-chk"
                  checked={dueOnlyCards}
                  onChange={e => {
                    setDueOnlyCards(e.target.checked);
                    fetchFlashcards(e.target.checked);
                  }}
                />
                <label htmlFor="due-only-chk">Sustained Review (SM-2 Due Cards Only)</label>
              </div>

              {flashcards.length === 0 ? (
                <div style={{ padding: "3rem 1rem", textAlign: "center", color: "var(--text-muted)", fontSize: "0.85rem" }}>
                  No cards available. Generate cards or check back later.
                </div>
              ) : (
                <div className="flashcard-deck-container">
                  <div style={{ alignSelf: "flex-start", fontSize: "0.72rem", color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: "0.5rem" }}>
                    CARD {currentCardIndex + 1} OF {flashcards.length}
                  </div>
                  
                  <div
                    className={`flashcard-card-outer ${showFlashcardAnswer ? "flipped" : ""}`}
                    onClick={() => setShowFlashcardAnswer(!showFlashcardAnswer)}
                  >
                    <div className="flashcard-card-inner">
                      <div className="flashcard-face front">
                        <div style={{ fontSize: "0.6rem", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "0.5rem", letterSpacing: "0.05em" }}>Question</div>
                        <div className="flashcard-text">{flashcards[currentCardIndex]?.front}</div>
                      </div>
                      <div className="flashcard-face back">
                        <div style={{ fontSize: "0.6rem", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "0.5rem", letterSpacing: "0.05em" }}>Answer</div>
                        <div className="flashcard-text" style={{ fontSize: "0.9rem" }}>{flashcards[currentCardIndex]?.back}</div>
                      </div>
                    </div>
                  </div>

                  {showFlashcardAnswer ? (
                    <div className="flashcard-controls">
                      <button className="btn-flashcard unsure" onClick={() => handleReviewFlashcard(2)}>Unsure (Redo)</button>
                      <button className="btn-flashcard gotit" onClick={() => handleReviewFlashcard(5)}>Got It!</button>
                    </div>
                  ) : (
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", fontStyle: "italic" }}>
                      Click card to flip
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Chat with Document (RAG) Drawer */}
      {showChatDrawer && (
        <div className="drawer-overlay" onClick={() => setShowChatDrawer(false)}>
          <div className="drawer" onClick={e => e.stopPropagation()} style={{ width: "450px" }}>
            <div className="drawer-header">
              <div>
                <span className="drawer-title">💬 Document Dialogue</span>
                {chatMeta.provider && (
                  <div style={{ fontSize: "0.6rem", color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: "0.1rem" }}>
                    {chatMeta.provider.toUpperCase()} — {chatMeta.model}
                  </div>
                )}
              </div>
              <button className="btn-close-drawer" onClick={() => setShowChatDrawer(false)}>×</button>
            </div>
            
            <div className="drawer-body" style={{ display: "flex", flexDirection: "column" }}>
              <div style={{ flex: 1, overflowY: "auto", paddingRight: "0.5rem" }}>
                {chatMessages.length === 0 ? (
                  <div style={{ padding: "4rem 1rem", textAlign: "center", color: "var(--text-muted)", fontSize: "0.85rem", lineHeight: 1.6 }}>
                    Ask questions about this PDF. The assistant queries relevant pages dynamically using local similarity vectors.
                  </div>
                ) : (
                  <div className="chat-messages-container">
                    {chatMessages.map((msg, idx) => (
                      <div key={idx} className={`chat-message ${msg.role}`}>
                        <div>{msg.content}</div>
                        {msg.role === "assistant" && msg.cited_pages && (
                          <div style={{ marginTop: "0.5rem" }}>
                            {asString(msg.cited_pages).split(",").map(pStr => {
                              const pNum = parseInt(pStr);
                              if (isNaN(pNum)) return null;
                              return (
                                <span key={pNum} className="citation-chip" onClick={() => scrollToPage(pNum)}>
                                  Page {pNum}
                                </span>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ))}
                    {isChatTyping && (
                      <div className="chat-message assistant">
                        <div style={{ display: "flex", gap: "0.2rem" }}>
                          <span className="spinner" style={{ width: "10px", height: "10px" }} />
                          <span>Streaming reply…</span>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            <div className="chat-input-area">
              <input
                type="text"
                className="chat-input"
                placeholder="Ask about this document..."
                value={chatInput}
                onChange={e => setChatInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") handleSendChatMessage(); }}
              />
              <button className="btn-action-primary" onClick={handleSendChatMessage}>Send</button>
            </div>
          </div>
        </div>
      )}

      {/* Document Intelligence Modal */}
      {showDocIntelModal && (
        <div className="modal-overlay">
          <div className="modal-content" style={{ width: "640px" }}>
            <div className="modal-header">
              <span className="modal-title">📊 Document-Level Insights</span>
              <button className="btn-close-drawer" onClick={() => setShowDocIntelModal(false)}>×</button>
            </div>
            <div className="modal-body">
              {docIntelError && <div className="error-alert" style={{ marginBottom: "1rem" }}>⚠️ {docIntelError}</div>}
              
              {!docIntelData ? (
                <div style={{ padding: "3rem 1rem", textAlign: "center" }}>
                  <p style={{ color: "var(--text-secondary)", fontSize: "0.85rem", marginBottom: "1rem" }}>
                    Generate a full document report including executive summary, topic clusters, difficulty, and prereqs.
                  </p>
                  <button
                    className="btn-action-primary"
                    disabled={isGeneratingDocIntel}
                    onClick={handleGenerateDocIntel}
                  >
                    {isGeneratingDocIntel ? "Generating Map-Reduce Report..." : "Generate Document Intelligence"}
                  </button>
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
                  <div>
                    <h4 style={{ fontFamily: "var(--font-sans)", fontSize: "0.95rem", fontWeight: 700, marginBottom: "0.5rem" }}>Executive Summary</h4>
                    <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)", whiteSpace: "pre-line", lineHeight: 1.6 }}>
                      {docIntelData.executive_summary}
                    </p>
                  </div>

                  <div style={{ display: "flex", gap: "1rem" }}>
                    <div style={{ flex: 1 }}>
                      <h4 style={{ fontFamily: "var(--font-sans)", fontSize: "0.95rem", fontWeight: 700, marginBottom: "0.5rem" }}>Difficulty Score</h4>
                      <div style={{ fontSize: "1.25rem", color: "#fbbf24", fontWeight: 700 }}>
                        {"★".repeat(docIntelData.difficulty_score)}{"☆".repeat(5 - docIntelData.difficulty_score)}
                        <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginLeft: "0.5rem" }}>({docIntelData.difficulty_score} / 5)</span>
                      </div>
                    </div>
                    
                    <div style={{ flex: 2 }}>
                      <h4 style={{ fontFamily: "var(--font-sans)", fontSize: "0.95rem", fontWeight: 700, marginBottom: "0.5rem" }}>Prerequisite Knowledge</h4>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
                        {docIntelData.prerequisite_knowledge?.map((item: string, i: number) => (
                          <span key={i} className="citation-chip" style={{ cursor: "default" }}>{item}</span>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div>
                    <h4 style={{ fontFamily: "var(--font-sans)", fontSize: "0.95rem", fontWeight: 700, marginBottom: "0.5rem" }}>Concept Glossary</h4>
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                      {docIntelData.concept_index?.map((item: any, i: number) => (
                        <div key={i} style={{ borderBottom: "1px solid var(--accent-muted)", paddingBottom: "0.5rem" }}>
                          <div style={{ fontWeight: 600, fontSize: "0.82rem", color: "var(--text-primary)" }}>{item.term}</div>
                          <div style={{ fontSize: "0.8rem", color: "var(--text-secondary)", margin: "0.2rem 0" }}>{item.definition}</div>
                          <div style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
                            Appears on pages:{" "}
                            {item.pages?.map((pNum: number) => (
                              <span key={pNum} className="citation-chip" onClick={() => { setShowDocIntelModal(false); scrollToPage(pNum); }}>
                                Page {pNum}
                              </span>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <h4 style={{ fontFamily: "var(--font-sans)", fontSize: "0.95rem", fontWeight: 700, marginBottom: "0.5rem" }}>Chapter &amp; Topic Groupings</h4>
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                      {docIntelData.chapter_groups?.map((chap: any, i: number) => (
                        <div key={i} style={{ padding: "0.6rem 0.8rem", background: "var(--bg-secondary)", borderRadius: "6px", border: "1px solid var(--accent-muted)" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", fontWeight: 700, fontSize: "0.85rem", marginBottom: "0.3rem" }}>
                            <span>{chap.title}</span>
                            <span style={{ color: "var(--text-muted)", fontSize: "0.72rem", cursor: "pointer" }} onClick={() => { setShowDocIntelModal(false); scrollToPage(chap.page_start); }}>
                              Pages {chap.page_start} - {chap.page_end}
                            </span>
                          </div>
                          <p style={{ fontSize: "0.8rem", color: "var(--text-secondary)", lineHeight: 1.5 }}>{chap.summary}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="modal-footer">
              {docIntelData && (
                <button
                  className="btn-action-outline"
                  style={{ marginRight: "auto" }}
                  disabled={isGeneratingDocIntel}
                  onClick={handleGenerateDocIntel}
                >
                  Regenerate
                </button>
              )}
              <button className="btn-action-primary" onClick={() => setShowDocIntelModal(false)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* Multi-Document Synthesis Modal */}
      {showSynthesisModal && (
        <div className="modal-overlay">
          <div className="modal-content" style={{ width: "680px" }}>
            <div className="modal-header">
              <span className="modal-title">🔀 Cross-Document Synthesis</span>
              <button className="btn-close-drawer" onClick={() => setShowSynthesisModal(false)}>×</button>
            </div>
            <div className="modal-body">
              <p style={{ fontSize: "0.8rem", color: "var(--text-secondary)", marginBottom: "1rem" }}>
                Select 2-10 processed PDFs from your library to cross-compare and answer synthesised queries.
              </p>
              
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", background: "var(--bg-secondary)", border: "1px solid var(--accent-muted)", padding: "1rem", borderRadius: "6px", marginBottom: "1.25rem", maxHeight: "150px", overflowY: "auto" }}>
                {previousBooks.map(b => {
                  const checked = selectedSynthDocs.includes(b.id);
                  return (
                    <label key={b.id} style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.75rem", padding: "0.2rem 0.5rem", background: "var(--bg-primary)", border: "1px solid var(--accent-muted)", borderRadius: "4px", cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => {
                          if (checked) {
                            setSelectedSynthDocs(prev => prev.filter(id => id !== b.id));
                          } else {
                            setSelectedSynthDocs(prev => [...prev, b.id]);
                          }
                        }}
                      />
                      {b.filename}
                    </label>
                  );
                })}
              </div>

              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1.5rem" }}>
                <input
                  type="text"
                  className="chat-input"
                  placeholder="E.g., What are the conflicting viewpoints on X between document A and B?"
                  value={synthQuestion}
                  onChange={e => setSynthQuestion(e.target.value)}
                  onKeyDown={e => { if (e.key === "Enter") handleSynthesize(); }}
                />
                <button
                  className="btn-action-primary"
                  disabled={isSynthesizing || selectedSynthDocs.length < 2 || !synthQuestion.trim()}
                  onClick={handleSynthesize}
                >
                  {isSynthesizing ? "Synthesizing..." : "Synthesize"}
                </button>
              </div>

              {synthAnswer && (
                <div style={{ padding: "1rem", background: "var(--bg-secondary)", border: "1px solid var(--accent-muted)", borderRadius: "6px" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: "1px solid var(--accent-muted)", paddingBottom: "0.5rem", marginBottom: "0.5rem" }}>
                    <strong style={{ fontSize: "0.85rem" }}>Synthesised Response</strong>
                    {synthMeta.provider && (
                      <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.6rem", color: "var(--text-muted)" }}>
                        {synthMeta.provider.toUpperCase()} — {synthMeta.model}
                      </span>
                    )}
                  </div>
                  
                  <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)", lineHeight: 1.6, whiteSpace: "pre-wrap", marginBottom: "1rem" }}>
                    {synthAnswer}
                  </p>
                  
                  {synthCitations.length > 0 && (
                    <div>
                      <div style={{ fontSize: "0.72rem", fontWeight: 700, textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "0.4rem" }}>Citations</div>
                      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                        {synthCitations.map((cit: any, i: number) => (
                          <div key={i} style={{ fontSize: "0.75rem", padding: "0.4rem 0.6rem", background: "var(--bg-primary)", border: "1px solid var(--accent-muted)", borderRadius: "4px" }}>
                            <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>{cit.filename}</div>
                            {cit.excerpt && <blockquote style={{ fontStyle: "italic", color: "var(--text-secondary)", margin: "0.2rem 0", paddingLeft: "0.5rem", borderLeft: "2px solid var(--text-muted)" }}>&ldquo;{cit.excerpt}&rdquo;</blockquote>}
                            <div style={{ fontSize: "0.65rem", color: "var(--text-muted)" }}>
                              Pages cited: {cit.pages?.join(", ")}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Batch Processing Inbox & Queue Drawer */}
      {showBatchDrawer && (
        <div className="drawer-overlay" onClick={() => setShowBatchDrawer(false)}>
          <div className="drawer" onClick={e => e.stopPropagation()}>
            <div className="drawer-header">
              <span className="drawer-title">📥 Batch Processing Queue</span>
              <button className="btn-close-drawer" onClick={() => setShowBatchDrawer(false)}>×</button>
            </div>
            <div className="drawer-body">
              <p style={{ fontSize: "0.8rem", color: "var(--text-secondary)", marginBottom: "1.25rem", lineHeight: 1.5 }}>
                Drop PDF files into the <code>inbox/</code> folder. The system watches the folder and processes uploads sequentially in the background.
              </p>

              {batchJobs.length === 0 ? (
                <div style={{ padding: "4rem 1rem", textAlign: "center", color: "var(--text-muted)", fontSize: "0.85rem" }}>
                  No batch jobs running or queued.
                </div>
              ) : (
                <div className="batch-jobs-list">
                  {batchJobs.map((job: any) => {
                    const progress = job.total_pages > 0 ? (job.completed_pages / job.total_pages) * 100 : 0;
                    return (
                      <div key={job.id} className="batch-job-card">
                        <div className="batch-job-info">
                          <span className="batch-job-filename">{job.filename}</span>
                          <span className={`batch-job-status ${job.status}`}>{job.status}</span>
                        </div>
                        <div className="batch-progress-bar-bg">
                          <div className="batch-progress-bar-fg" style={{ width: `${progress}%` }} />
                        </div>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.65rem", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                          <span>{job.completed_pages} / {job.total_pages} pages</span>
                          <span>{progress.toFixed(0)}%</span>
                        </div>
                        {job.error_message && (
                          <div style={{ fontSize: "0.65rem", color: "#ef4444", marginTop: "0.2rem", wordBreak: "break-all" }}>
                            Error: {job.error_message}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
