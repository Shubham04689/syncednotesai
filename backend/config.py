import os
from pathlib import Path
from typing import List, Dict
from dotenv import load_dotenv

# Load variables from current working directory .env
load_dotenv()

# Load variables from backend/.env relative to this file
backend_env_path = Path(__file__).resolve().parent / ".env"
if backend_env_path.exists():
    load_dotenv(dotenv_path=backend_env_path)

class Settings:
    def __init__(self):
        self.ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        
        # Load providers API keys dynamically
        self.gemini_keys = self._get_keys_for_provider("GEMINI")
        self.groq_keys = self._get_keys_for_provider("GROQ")
        self.mistral_keys = self._get_keys_for_provider("MISTRAL")
        
        # Free image generation providers (no cost, no key required)
        # Pollinations.AI: completely free, no auth needed, uses FLUX/SDXL/etc.
        self.pollinations_api_base = os.environ.get("POLLINATIONS_API_BASE", "https://image.pollinations.ai")
        # Optional Pollinations key for higher rate limits (get one at enter.pollinations.ai)
        self.pollinations_key = os.environ.get("POLLINATIONS_API_KEY", "")
        
        # Hugging Face Inference API (free tier with HF token, or without for public models)
        self.huggingface_token = os.environ.get("HUGGINGFACE_API_KEY", os.environ.get("HF_TOKEN", ""))
        
        # Together AI (free tier available)
        self.together_api_key = os.environ.get("TOGETHER_API_KEY", "")
        
        # Load dynamic keys if any
        self.reload_from_keys_json()

    def reload_from_keys_json(self):
        keys_json_path = Path(__file__).resolve().parent / "keys.json"
        # Reset to environment variables first, then load keys.json
        self.gemini_keys = self._get_keys_for_provider("GEMINI")
        self.groq_keys = self._get_keys_for_provider("GROQ")
        self.mistral_keys = self._get_keys_for_provider("MISTRAL")
        
        if keys_json_path.exists():
            try:
                import json
                with open(keys_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Merge gemini
                gk = data.get("gemini", data.get("gemini_keys", []))
                if isinstance(gk, list):
                    for k in gk:
                        if k and k not in self.gemini_keys:
                            self.gemini_keys.append(k)
                elif isinstance(gk, str) and gk:
                    if gk not in self.gemini_keys:
                        self.gemini_keys.append(gk)
                        
                # Merge groq
                gq = data.get("groq", data.get("groq_keys", []))
                if isinstance(gq, list):
                    for k in gq:
                        if k and k not in self.groq_keys:
                            self.groq_keys.append(k)
                elif isinstance(gq, str) and gq:
                    if gq not in self.groq_keys:
                        self.groq_keys.append(gq)
                        
                # Merge mistral
                mk = data.get("mistral", data.get("mistral_keys", []))
                if isinstance(mk, list):
                    for k in mk:
                        if k and k not in self.mistral_keys:
                            self.mistral_keys.append(k)
                elif isinstance(mk, str) and mk:
                    if mk not in self.mistral_keys:
                        self.mistral_keys.append(mk)
            except Exception:
                pass

    def _get_keys_for_provider(self, provider_prefix: str) -> List[str]:
        keys = []
        # Support both indexed suffixes (e.g. GEMINI_API_KEY_1) and simple name if single (e.g. GEMINI_API_KEY)
        single_key = os.environ.get(f"{provider_prefix}_API_KEY")
        if single_key:
            keys.append(single_key)
        
        # Scan indices 1 to 10
        for i in range(1, 11):
            key = os.environ.get(f"{provider_prefix}_API_KEY_{i}")
            if key and key not in keys:
                keys.append(key)
        return keys

    @property
    def has_gemini(self) -> bool:
        return len(self.gemini_keys) > 0

    @property
    def has_groq(self) -> bool:
        return len(self.groq_keys) > 0

    @property
    def has_mistral(self) -> bool:
        return len(self.mistral_keys) > 0

    @property
    def has_pollinations(self) -> bool:
        # Pollinations works without a key (free, no signup) — always available
        return True

    @property
    def has_huggingface(self) -> bool:
        return len(self.huggingface_token) > 0

    @property
    def has_together(self) -> bool:
        return len(self.together_api_key) > 0

settings = Settings()
