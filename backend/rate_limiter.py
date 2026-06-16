"""
Rate limiter — conservative token buckets matching real free-tier limits.

Real free-tier limits (June 2026):
  Gemini:   5 RPM per key  → 1 token / 12 s,  burst = 2
  Groq:     30 RPM per key → 1 token / 2 s,   burst = 5
  Mistral:  ~2 RPM per key → 1 token / 30 s,  burst = 1

Keys start FULL so the first few requests go through immediately.
After the burst is consumed, the sustained rate kicks in.
"""
import time
import threading
from typing import List, Dict, Optional

try:
    from backend.config import settings
except ModuleNotFoundError:
    from config import settings


class TokenBucket:
    def __init__(self, capacity: float, fill_rate: float):
        self.capacity = capacity
        self.fill_rate = fill_rate          # tokens per second
        self.tokens = float(capacity)       # start FULL
        self.last_refill = time.time()
        self.lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> bool:
        with self.lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            self.last_refill = now
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False

    def seconds_until_token(self) -> float:
        """Seconds until this bucket has at least 1 token."""
        with self.lock:
            if self.tokens >= 1.0:
                return 0.0
            needed = 1.0 - self.tokens
            return needed / self.fill_rate if self.fill_rate > 0 else 9999.0


class APIKeyInfo:
    def __init__(self, key: str, provider: str, capacity: float, fill_rate: float):
        self.key = key
        self.provider = provider
        self.unhealthy_until = 0.0
        self.rate_limiter = TokenBucket(capacity=capacity, fill_rate=fill_rate)

    @property
    def is_healthy(self) -> bool:
        return time.time() >= self.unhealthy_until

    def seconds_until_available(self) -> float:
        """Seconds until this specific key can be used again."""
        now = time.time()
        health_wait = max(0.0, self.unhealthy_until - now)
        bucket_wait = self.rate_limiter.seconds_until_token()
        return max(health_wait, bucket_wait)


class KeyManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.keys: Dict[str, List[APIKeyInfo]] = {
            # Gemini free tier: 5 RPM → fill_rate = 5/60 ≈ 0.083/s, burst = 2
            "gemini": [
                APIKeyInfo(k, "gemini", capacity=2.0, fill_rate=5 / 60)
                for k in settings.gemini_keys
            ],
            # Groq free tier: 30 RPM → fill_rate = 30/60 = 0.5/s, burst = 5
            "groq": [
                APIKeyInfo(k, "groq", capacity=5.0, fill_rate=30 / 60)
                for k in settings.groq_keys
            ],
            # Mistral Experiment tier: ~2 RPM → fill_rate = 2/60 ≈ 0.033/s, burst = 1
            "mistral": [
                APIKeyInfo(k, "mistral", capacity=1.0, fill_rate=2 / 60)
                for k in settings.mistral_keys
            ],
            "huggingface": [
                APIKeyInfo(k, "huggingface", capacity=3.0, fill_rate=0.1)
                for k in ([settings.huggingface_token] if settings.huggingface_token else [])
            ],
        }
        self.current_indices: Dict[str, int] = {p: 0 for p in self.keys}

    def get_key(self, provider: str) -> Optional[str]:
        """Round-robin key selection. Returns None if all keys are blocked."""
        provider = provider.lower()
        with self.lock:
            key_list = self.keys.get(provider, [])
            if not key_list:
                return None
            n = len(key_list)
            start = self.current_indices.get(provider, 0)
            for i in range(n):
                idx = (start + i) % n
                ki = key_list[idx]
                if ki.is_healthy and ki.rate_limiter.consume(1.0):
                    self.current_indices[provider] = (idx + 1) % n
                    return ki.key
            return None

    def mark_unhealthy(self, provider: str, key_val: str, duration: float = 60.0):
        """Circuit-breaker: block a key for `duration` seconds."""
        provider = provider.lower()
        with self.lock:
            for ki in self.keys.get(provider, []):
                if ki.key == key_val:
                    ki.unhealthy_until = time.time() + duration
                    break

    def mark_ok(self, provider: str, key_val: str):
        """Successful call — reset circuit breaker."""
        provider = provider.lower()
        with self.lock:
            for ki in self.keys.get(provider, []):
                if ki.key == key_val:
                    ki.unhealthy_until = 0.0
                    break

    def seconds_until_available(self, provider: str) -> float:
        """Minimum seconds until ANY key for this provider can be used."""
        provider = provider.lower()
        with self.lock:
            waits = [ki.seconds_until_available() for ki in self.keys.get(provider, [])]
            return min(waits) if waits else 9999.0

    def has_any_key(self, provider: str) -> bool:
        """True if this provider has at least one key configured."""
        return len(self.keys.get(provider.lower(), [])) > 0

    def rebuild_provider(self, provider: str, new_keys: List[str]):
        """
        Rebuilds the APIKeyInfo list for a provider, preserving existing state for keys that are still present.
        """
        provider = provider.lower()
        
        # Determine capacity & fill_rate based on provider
        if provider == "gemini":
            capacity = 2.0
            fill_rate = 5 / 60
        elif provider == "groq":
            capacity = 5.0
            fill_rate = 30 / 60
        elif provider == "mistral":
            capacity = 1.0
            fill_rate = 2 / 60
        elif provider == "huggingface":
            capacity = 3.0
            fill_rate = 0.1
        else:
            capacity = 2.0
            fill_rate = 5 / 60
            
        with self.lock:
            # Map existing keys to their APIKeyInfo objects
            existing_infos = {ki.key: ki for ki in self.keys.get(provider, [])}
            
            rebuilt_list = []
            for k in new_keys:
                if k in existing_infos:
                    # Keep the existing state intact
                    rebuilt_list.append(existing_infos[k])
                else:
                    # Create new APIKeyInfo
                    rebuilt_list.append(APIKeyInfo(k, provider, capacity, fill_rate))
                    
            self.keys[provider] = rebuilt_list
            if provider not in self.current_indices or not rebuilt_list:
                self.current_indices[provider] = 0
            else:
                self.current_indices[provider] = self.current_indices[provider] % len(rebuilt_list)


# Singleton
key_manager = KeyManager()
