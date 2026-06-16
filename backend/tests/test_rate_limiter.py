import time
import pytest
try:
    from backend.rate_limiter import TokenBucket, APIKeyInfo, KeyManager
    from backend.config import settings
except ModuleNotFoundError:
    from rate_limiter import TokenBucket, APIKeyInfo, KeyManager
    from config import settings

def test_token_bucket_consumption():
    # Capacity 2, refill rate 1 per second
    bucket = TokenBucket(capacity=2.0, fill_rate=1.0)
    assert bucket.consume(1.0) is True
    assert bucket.consume(1.0) is True
    # Bucket should be empty now
    assert bucket.consume(1.0) is False

    # Wait 1.1s for a token to refill
    time.sleep(1.1)
    assert bucket.consume(1.0) is True
    assert bucket.consume(1.0) is False

def test_key_manager_rotation():
    # Let's mock key manager keys manually
    km = KeyManager()
    km.keys["groq"] = [
        APIKeyInfo("key_1", "groq", capacity=10, fill_rate=1),
        APIKeyInfo("key_2", "groq", capacity=10, fill_rate=1),
    ]
    km.current_indices["groq"] = 0

    # First request should yield key_1
    assert km.get_key("groq") == "key_1"
    # Second request should yield key_2 (round-robin)
    assert km.get_key("groq") == "key_2"
    # Third request should yield key_1 again
    assert km.get_key("groq") == "key_1"

def test_key_manager_circuit_breaker():
    km = KeyManager()
    km.keys["gemini"] = [
        APIKeyInfo("gemini_key_1", "gemini", capacity=10, fill_rate=1),
        APIKeyInfo("gemini_key_2", "gemini", capacity=10, fill_rate=1),
    ]
    km.current_indices["gemini"] = 0

    # Mark gemini_key_1 unhealthy
    km.mark_unhealthy("gemini", "gemini_key_1", duration=2.0)

    # It should skip gemini_key_1 and yield gemini_key_2 twice
    assert km.get_key("gemini") == "gemini_key_2"
    assert km.get_key("gemini") == "gemini_key_2"

    # Wait 2.1s for gemini_key_1 to become healthy again
    time.sleep(2.1)
    # The pointer was at index 1 (next is index 0 - gemini_key_1)
    assert km.get_key("gemini") == "gemini_key_1"

def test_key_manager_rebuild_preserves_state():
    km = KeyManager()
    km.keys["groq"] = [
        APIKeyInfo("groq_key_1", "groq", capacity=5.0, fill_rate=0.5),
        APIKeyInfo("groq_key_2", "groq", capacity=5.0, fill_rate=0.5)
    ]
    # Consume one token from key 1
    assert km.keys["groq"][0].rate_limiter.consume(1.0) is True
    initial_tokens_1 = km.keys["groq"][0].rate_limiter.tokens
    
    # Rebuild provider with groq_key_1 (existing) and groq_key_3 (new), discarding groq_key_2
    km.rebuild_provider("groq", ["groq_key_1", "groq_key_3"])
    
    # Verify rebuilt list structure
    assert len(km.keys["groq"]) == 2
    assert km.keys["groq"][0].key == "groq_key_1"
    assert km.keys["groq"][1].key == "groq_key_3"
    
    # Verify the remaining key preserved its token count
    assert km.keys["groq"][0].rate_limiter.tokens == initial_tokens_1
    # Verify the new key starts full (tokens == capacity)
    assert km.keys["groq"][1].rate_limiter.tokens == 5.0
